import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ServerManager:
    """Starts, monitors and stops the Core-Bridge server as a subprocess."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000, bind_host: str = "0.0.0.0") -> None:
        """`host` is the local URL used by the desktop app; `bind_host` is what uvicorn listens on."""
        self.host = host
        self.port = port
        self.bind_host = bind_host
        self._proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 20.0, reuse_existing: bool = False) -> bool:
        """Start the server; wait until ready."""
        if reuse_existing and self._healthy():
            return True
        # 仅杀自己上次残留的子进程；不再 taskkill 任意占用进程（防误杀）
        self.stop()
        if self._proc is None:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            log_path = PROJECT_ROOT / "logs" / "server.out"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_file = open(log_path, "ab")
            except Exception:
                log_file = None  # type: ignore[assignment]
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "server.app:app", "--host", self.bind_host, "--port", str(self.port)],
                    cwd=str(PROJECT_ROOT),
                    stdout=log_file or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if log_file else subprocess.DEVNULL,
                    **kwargs,
                )
            finally:
                # 子进程已继承 fd，父进程侧可关（Windows 上保持打开也无妨）
                pass
        return self.wait_ready(timeout)

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._healthy():
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _healthy(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/command", timeout=1)
            return resp.status_code == 200
        except Exception:
            return False

    def free_port(self, wait: float = 5.0) -> None:
        """兼容旧接口：不再杀任意进程，仅等待端口释放（由调用方确认）。

        如确需清理陈旧进程，请手动处理或传入 force=True 的新接口。
        """
        deadline = time.time() + wait
        while time.time() < deadline:
            if not self._port_in_use():
                return
            time.sleep(0.2)

    def _port_in_use(self) -> bool:
        with socket.socket() as s:
            try:
                s.bind((self.bind_host, self.port))
                return False
            except OSError:
                return True

    def _listening_pids(self) -> set[int]:
        """精确匹配本地监听端口的 PID（尾缀匹配，避免 :8000 命中 :80001）。

        Windows 用 netstat（gbk/utf8 兼容解码），POSIX 用 /proc 或 lsof 回退。
        """
        pids: set[int] = set()
        port_re = re.compile(rf"[.:]{self.port}\s")
        try:
            if sys.platform == "win32":
                proc = subprocess.run(
                    ["netstat", "-ano", "-p", "TCP"],
                    capture_output=True,
                    check=False,
                )
                raw = proc.stdout
                out = ""
                for enc in ("gbk", "utf-8", "cp936"):
                    try:
                        out = raw.decode(enc)
                        break
                    except Exception:
                        continue
                for line in out.splitlines():
                    if "LISTENING" not in line:
                        continue
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    local = parts[1]
                    # 精确尾缀：**:8000 结尾
                    if not (local.endswith(f":{self.port}")):
                        continue
                    try:
                        pids.add(int(parts[-1]))
                    except ValueError:
                        continue
            else:
                proc = subprocess.run(
                    ["lsof", "-ti", f"TCP:{self.port}", "-sTCP:LISTEN"],
                    capture_output=True, check=False, text=True,
                )
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
        except Exception:
            pass
        _ = port_re  # 保留正则以备扩展
        return pids

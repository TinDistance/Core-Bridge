import atexit
import os
import socket
import subprocess
import sys
import time
import weakref
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LIVE_MANAGERS: "weakref.WeakSet[ServerManager]" = weakref.WeakSet()


def _shutdown_all() -> None:
    """进程退出时兜底：只清理自己拉起的 uvicorn 子进程，不碰任何其他进程。"""
    for manager in list(_LIVE_MANAGERS):
        try:
            manager._release()
        except Exception:
            pass


atexit.register(_shutdown_all)

LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_KEEP_BYTES = 2 * 1024 * 1024


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _rotate_log(path: Path) -> None:
    """uvicorn access log 会无限长（已涨到 1.4MB+），超阈值时保留尾部一半。"""
    size = _log_size(path)
    if size <= LOG_MAX_BYTES:
        return
    keep = max(min(LOG_KEEP_BYTES, size // 2), 1)
    try:
        with open(path, "rb") as f:
            f.seek(size - keep, os.SEEK_SET)
            f.readline()  # 丢掉被截断的半行
            tail = f.read()
        with open(path, "wb") as f:
            f.write(b"[log truncated by ServerManager]\n")
            f.write(tail)
    except OSError:
        pass


class ServerManager:
    """Starts, monitors and stops the Core-Bridge server as a subprocess."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000, bind_host: str = "0.0.0.0") -> None:
        """`host` is the local URL used by the desktop app; `bind_host` is what uvicorn listens on."""
        self.host = host
        self.port = port
        self.bind_host = bind_host
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self._log_mark = 0
        self.last_error = ""
        _LIVE_MANAGERS.add(self)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 20.0, reuse_existing: bool = True) -> bool:
        """拉起 server 并等到可用；失败返回 False（不抛异常给 UI）。"""
        # 端口已有人 listen：健康就直接复用（上次残留 / 手动起的），不重启、不误杀
        if self._port_in_use():
            if reuse_existing and self._healthy():
                return True
            # 不健康 = 僵死的残留，控制台被强杀时 atexit 跑不到，这里精确认领后回收
            self._reap_own_leftovers()
            if self._port_in_use():
                self.last_error = self._port_holder_hint()
                return False
        self.stop()
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        # 独立进程组：控制台 Ctrl+C / 关窗口不会波及 server，退出时由我们自己收尸
        if sys.platform == "win32":
            kwargs["creationflags"] |= subprocess.CREATE_NEW_PROCESS_GROUP
        log_path = PROJECT_ROOT / "logs" / "server.out"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_log(log_path)
            log_file = open(log_path, "ab")
        except Exception:
            log_file = None
        self._log_mark = _log_size(log_path)
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server.app:app", "--host", self.bind_host, "--port", str(self.port)],
                cwd=str(PROJECT_ROOT),
                stdout=log_file if log_file else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_file else subprocess.DEVNULL,
                **kwargs,
            )
            self._log_file = log_file
        except Exception as e:
            if log_file is not None:
                log_file.close()
            self.last_error = f"无法启动 python 子进程：{e}"[:200]
            raise
        self.last_error = ""
        ok = self.wait_ready(timeout)
        if not ok:
            # 日志里的真实原因优先于"进程退出/超时"这类兜底描述
            self.last_error = self._log_tail_error() or self.last_error
        return ok

    def _log_tail_error(self) -> str:
        """从 server.out 尾部摘出真正的失败原因（winerror 10048 / traceback 等）。"""
        path = PROJECT_ROOT / "logs" / "server.out"
        try:
            with open(path, "rb") as f:
                if self._log_mark:
                    f.seek(self._log_mark)
                raw = f.read(64 * 1024)
        except Exception:
            return ""
        lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
        for line in reversed(lines):
            if "ERROR" in line or "error while attempting" in line:
                return line[:160]
        if any("Traceback" in ln for ln in lines):
            return "子进程抛出异常，详见 logs/server.out"
        return ""

    def _port_holder_hint(self) -> str:
        pids = self._listening_pids()
        if not pids:
            return f"端口 {self.port} 被占用，但查不到监听进程（可能需要管理员权限）"
        detail = "、".join(
            f"{pid}{'（本项目残留）' if self._is_own_server(pid) else ''}" for pid in sorted(pids)
        )
        return f"端口 {self.port} 被其他进程占用：PID {detail}。请关闭它或换一个端口"

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._healthy():
                return True
            if self._proc is not None and self._proc.poll() is not None:
                self.last_error = f"server 进程已退出（returncode={self._proc.returncode}）"
                return False
            time.sleep(0.2)
        self.last_error = self.last_error or f"{timeout:.0f}s 内 server 未就绪"
        return False

    def stop(self) -> None:
        self._release(graceful=True)

    def _release(self, graceful: bool = False) -> None:
        """收掉自己拉起的 server 子进程；无子进程时静默返回。"""
        proc, self._proc = self._proc, None
        log_file, self._log_file = self._log_file, None
        if proc is not None and proc.poll() is None:
            self._kill_tree(proc, graceful=graceful)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

    def _kill_tree(self, proc: subprocess.Popen, graceful: bool = False) -> None:
        """只针对自己的 pid 杀进程树，避免留下占端口的孤儿。"""
        if proc.poll() is not None:
            return
        if not graceful:
            if sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True, check=False, timeout=10,
                    )
                except Exception:
                    pass
                if proc.poll() is not None:
                    return
            else:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        except Exception:
            pass

    def _healthy(self) -> bool:
        """探 /ping：专用的轻量存活接口，且显式绕过系统代理（trust_env=False）。"""
        try:
            resp = httpx.get(f"{self.base_url}/ping", timeout=1.0, trust_env=False)
            return resp.status_code == 200
        except Exception:
            return False

    def _reap_own_leftovers(self, wait: float = 5.0) -> list[int]:
        """回收占用端口且**确认是本项目 server** 的残留进程。

        三重校验：监听端口匹配 + 命令行含本项目的 uvicorn 入口 + pid 非自身/非父进程，
        因此不会误杀任何无关进程（IDE、其他项目、svchost 等）。
        """
        if not self._port_in_use():
            return []
        reaped: list[int] = []
        for pid in self._listening_pids():
            if pid in (os.getpid(), os.getppid()) or not self._is_own_server(pid):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, check=False, timeout=10,
                )
                reaped.append(pid)
            except Exception:
                continue
        if reaped:
            deadline = time.time() + wait
            while time.time() < deadline and self._port_in_use():
                time.sleep(0.2)
        return reaped

    def _is_own_server(self, pid: int) -> bool:
        """命令行是否就是本项目的 `uvicorn server.app:app`。"""
        cmdline = " ".join(self._proc_cmdline(pid).split())
        return bool(cmdline) and "uvicorn" in cmdline and "server.app:app" in cmdline

    def _proc_cmdline(self, pid: int) -> str:
        try:
            if sys.platform == "win32":
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                    capture_output=True, check=False, timeout=15,
                )
            else:
                proc = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, check=False, timeout=10,
                )
        except Exception:
            return ""
        raw = proc.stdout
        if isinstance(raw, bytes):
            for enc in ("gbk", "cp936", "utf-8"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("latin-1", "replace")
        return raw

    def free_port(self, wait: float = 5.0) -> None:
        """兼容旧接口：不再杀任意进程，仅等待端口释放（由调用方确认）。"""
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
            except (OSError, OverflowError):
                return True

    def _listening_pids(self) -> set[int]:
        """精确匹配本地监听端口的 PID（尾缀匹配，避免 :8000 命中 :80001）。

        Windows 用 netstat（gbk/utf8 兼容解码），POSIX 用 /proc 或 lsof 回退。
        """
        pids: set[int] = set()
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
        return pids

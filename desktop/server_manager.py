import os
import socket
import subprocess
import sys
import time

import httpx

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


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
        """Start the server; wait until ready.

        By default stale processes on the port are killed first, because an
        old uvicorn (e.g. pre-raw-SDP `/webrtc/push`) keeps serving 422 while
        looking healthy on `GET /command`. Pass `reuse_existing=True` to keep
        the old reuse behaviour.
        """
        if reuse_existing and self._healthy():
            return True
        self.free_port()
        if self._proc is None:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server.app:app", "--host", self.bind_host, "--port", str(self.port)],
                cwd=str(PROJECT_ROOT),
                **kwargs,
            )
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
        """Kill stale processes listening on our port (not just our child)."""
        for pid in self._listening_pids():
            if pid == os.getpid():
                continue
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        check=False,
                    )
                else:
                    os.kill(pid, 9)
            except Exception:
                continue
        deadline = time.time() + wait
        while time.time() < deadline:
            if not self._listening_pids():
                return
            time.sleep(0.2)

    def _listening_pids(self) -> set[int]:
        pids: set[int] = set()
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                check=False,
            )
            out = proc.stdout.decode("gbk", errors="ignore").splitlines()
        except Exception:
            return pids
        for line in out:
            if "LISTENING" not in line or f":{self.port}" not in line:
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                continue
        # Fallback: port is free if we can bind it.
        if not pids:
            with socket.socket() as s:
                try:
                    s.bind((self.bind_host, self.port))
                except OSError:
                    pass
        return pids

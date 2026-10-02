import subprocess
import sys
import time

import httpx

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


class ServerManager:
    """Starts, monitors and stops the Core-Bridge server as a subprocess."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self._proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 20.0) -> bool:
        """Start the server (reusing an external one if reachable); wait until ready."""
        if self._healthy():
            return True
        if self._proc is None:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server.app:app", "--host", self.host, "--port", str(self.port)],
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

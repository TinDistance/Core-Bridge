import asyncio
import threading
from collections import deque

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription

from desktop.streaming.screen_track import ScreenCaptureTrack


class Pusher:
    """Signaling client: pushes the local screen to the server over WebRTC.

    Runs its own asyncio loop in a background thread so it can live inside a GUI.
    """

    def __init__(self, server_url: str, fps: int = 30) -> None:
        self.server_url = server_url.rstrip("/")
        self.fps = fps
        self._status: deque[str] = deque(maxlen=10)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def status(self) -> list[str]:
        return list(self._status)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="webrtc-pusher")
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()
            self._loop = None
            self._thread = None

    def _set_status(self, text: str) -> None:
        self._status.append(text)

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        track = ScreenCaptureTrack(fps=self.fps)
        pc = RTCPeerConnection()
        session: str | None = None
        try:
            self._set_status("capturing screen")
            pc.addTrack(track)

            @pc.on("connectionstatechange")
            def on_state() -> None:
                self._set_status(f"connection: {pc.connectionState}")

            self._set_status("signaling")
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.server_url}/webrtc/push",
                    json={"sdp": pc.localDescription.sdp, "type": "offer"},
                )
                resp.raise_for_status()
                answer = resp.json()
            session = answer.get("session")
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

            self._set_status("streaming")
            await self._stop_event.wait()
        except Exception as e:
            self._set_status(f"error: {e}")
        finally:
            self._set_status("stopped")
            await pc.close()
            track.stop()
            if session is not None:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(
                            f"{self.server_url}/webrtc/push/stop",
                            json={"session": session},
                        )
                except Exception:
                    pass

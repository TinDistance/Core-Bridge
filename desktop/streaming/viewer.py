import asyncio
import threading
from collections import deque
from typing import Optional, Union

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

Event = tuple[str, Union[str, "object"]]


class Viewer:
    """Pulls the currently pushed stream from the server and yields PIL frames.

    Runs its own asyncio loop in a background thread; reconnects automatically.
    Events: ("frame", PIL.Image) | ("status", "streaming"|"no_stream"|text)
    """

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")
        self._events: deque[Event] = deque(maxlen=64)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="webrtc-viewer")
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def events(self) -> list[Event]:
        out = list(self._events)
        self._events.clear()
        return out

    def _emit(self, event: Event) -> None:
        self._events.append(event)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()
            self._loop = None
            self._thread = None

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            try:
                await self._watch_once()
            except Exception as e:
                self._emit(("status", f"连接失败: {e}"))
            if not self._stop_event.is_set():
                await asyncio.sleep(3)

    async def _watch_once(self) -> None:
        pc = RTCPeerConnection()
        track_holder: dict[str, Optional[MediaStreamTrack]] = {"track": None}

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            track_holder["track"] = track

        pc.addTransceiver("video", direction="recvonly")

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                f"{self.server_url}/webrtc/watch",
                json={"sdp": pc.localDescription.sdp, "type": "offer"},
            )
            if resp.status_code == 404:
                self._emit(("status", "no_stream"))
                await self._wait_stop_or_timeout(3)
                return
            resp.raise_for_status()
            answer = resp.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

        track = track_holder["track"]
        if track is None:
            self._emit(("status", "未收到视频轨"))
            await self._wait_stop_or_timeout(3)
            return

        self._emit(("status", "streaming"))
        try:
            while not self._stop_event.is_set():
                frame = await track.recv()
                self._emit(("frame", frame.to_image()))
        except MediaStreamError:
            self._emit(("status", "no_stream"))
        finally:
            self._emit(("status", "disconnected"))
            await pc.close()

    async def _wait_stop_or_timeout(self, timeout: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

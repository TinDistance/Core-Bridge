import asyncio
import uuid
from typing import Awaitable, Callable, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/webrtc", tags=["webrtc"])

pcs: set[RTCPeerConnection] = set()
push_sessions: dict[str, RTCPeerConnection] = {}
watchers: set[RTCPeerConnection] = set()


class StreamHub:
    """Consumes the pushed track and re-serves the latest frame to watchers."""

    def __init__(self) -> None:
        self.live = False
        self.session: Optional[str] = None
        self._latest = None
        self._feed_task: Optional[asyncio.Task] = None
        self.on_idle: Optional[Callable[[], Awaitable[None]]] = None

    @property
    def available(self) -> bool:
        return self.live

    def start_feed(self, track: MediaStreamTrack, session: str) -> None:
        self.stop_feed(notify=False)
        self.session = session
        self._feed_task = asyncio.create_task(self._feed(track))

    def stop_feed(self, notify: bool = True) -> None:
        task, self._feed_task = self._feed_task, None
        if task is not None:
            task.cancel()
        was_active = self.live or self.session is not None
        self.live = False
        self.session = None
        self._latest = None
        if notify and was_active:
            self._notify_idle()

    async def _feed(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                self._latest = await track.recv()
                self.live = True
        except asyncio.CancelledError:
            raise
        except MediaStreamError:
            if self._feed_task is asyncio.current_task():
                self._feed_task = None
                self.live = False
                self.session = None
                self._latest = None
                self._notify_idle()

    def _notify_idle(self) -> None:
        if self.on_idle is not None:
            asyncio.create_task(self.on_idle())

    async def next_frame(self):
        while True:
            if self.live and self._latest is not None:
                return self._latest
            if not self.live:
                raise MediaStreamError
            await asyncio.sleep(0.01)


class RelayTrack(VideoStreamTrack):
    """Server-owned track that replays the hub's latest frame."""

    kind = "video"

    def __init__(self, hub: StreamHub, fps: int = 30) -> None:
        super().__init__()
        self._hub = hub
        self._fps = fps

    async def recv(self):
        if not self._hub.live:
            raise MediaStreamError
        pts, time_base = await self.next_timestamp()
        frame = await self._hub.next_frame()
        frame.pts = pts
        frame.time_base = time_base
        return frame


async def _close_watchers() -> None:
    for pc in list(watchers):
        watchers.discard(pc)
        await pc.close()


hub = StreamHub()
hub.on_idle = _close_watchers


class SDPOffer(BaseModel):
    sdp: str
    type: str = "offer"


class StopRequest(BaseModel):
    session: str


@router.post("/push")
async def push(offer: SDPOffer) -> dict:
    pc = RTCPeerConnection()
    pcs.add(pc)
    session = uuid.uuid4().hex
    push_sessions[session] = pc

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        hub.start_feed(track, session)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)
            if push_sessions.get(session) is pc:
                push_sessions.pop(session, None)
            if hub.session == session:
                hub.stop_feed()

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        pcs.discard(pc)
        push_sessions.pop(session, None)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"invalid SDP offer: {e}")

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "session": session}


@router.post("/push/stop")
async def push_stop(req: StopRequest) -> dict:
    pc = push_sessions.pop(req.session, None)
    if pc is not None:
        pcs.discard(pc)
        await pc.close()
    if hub.session == req.session:
        hub.stop_feed()
    return {"ok": True}


@router.post("/watch")
async def watch(offer: SDPOffer) -> dict:
    if not hub.available:
        raise HTTPException(status_code=404, detail="no device streaming")

    pc = RTCPeerConnection()
    pcs.add(pc)
    watchers.add(pc)
    track = RelayTrack(hub)
    pc.addTrack(track)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)
            watchers.discard(pc)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        pcs.discard(pc)
        watchers.discard(pc)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"invalid SDP offer: {e}")

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

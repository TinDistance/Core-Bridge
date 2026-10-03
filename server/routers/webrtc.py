import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("webrtc")

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


async def _read_offer(request: Request) -> tuple[str, str]:
    """Accept either a JSON {sdp, type} body or a raw SDP body."""
    raw = await request.body()
    content_type = request.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            data = json.loads(raw)
            return data["sdp"], data.get("type", "offer")
        except (json.JSONDecodeError, KeyError) as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON offer: {e}")
    sdp = raw.decode("utf-8", "replace").strip()
    if not sdp:
        raise HTTPException(status_code=400, detail="empty SDP offer")
    return sdp, "offer"


def _normalize_k230_offer(sdp: str) -> str:
    """Fix K230 firmware SDP so aiortc accepts it.

    K230 omits `packetization-mode` on its H264 fmtp line (defaults to 0),
    but aiortc only ships H264 with `packetization-mode=1` and requires an
    exact match, otherwise setRemoteDescription fails with "Failed to set
    remote video description send parameters". The K230 sends fragmented
    1080p frames that only fit in FU-A (mode 1), so advertising mode 1 is
    the correct description of the wire format.
    """
    lines = []
    for line in sdp.splitlines():
        if (
            line.startswith("a=fmtp:")
            and "profile-level-id" in line.lower()
            and "packetization-mode" not in line.lower()
        ):
            line = line.rstrip() + ";packetization-mode=1"
        lines.append(line)
    return "\r\n".join(lines) + "\r\n"


@router.post("/push")
async def push(request: Request):
    sdp, offer_type = await _read_offer(request)
    sdp = _normalize_k230_offer(sdp)
    as_json = "json" in request.headers.get("content-type", "").lower()

    pc = RTCPeerConnection()
    pcs.add(pc)
    session = uuid.uuid4().hex
    push_sessions[session] = pc

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        logger.info("push %s: got remote %s track", session, track.kind)
        hub.start_feed(track, session)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        logger.info("push %s: connectionState -> %s", session, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)
            if push_sessions.get(session) is pc:
                push_sessions.pop(session, None)
            if hub.session == session:
                hub.stop_feed()

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        pcs.discard(pc)
        push_sessions.pop(session, None)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"invalid SDP offer: {e}")

    answer_sdp = pc.localDescription.sdp
    n_cands = sum(1 for ln in answer_sdp.splitlines() if ln.startswith("a=candidate"))
    logger.info(
        "push %s: offer %d bytes -> answer %d bytes, %d candidates",
        session,
        len(sdp),
        len(answer_sdp),
        n_cands,
    )
    for ln in answer_sdp.splitlines():
        if ln.startswith("a=candidate") or ln.startswith("a=setup"):
            logger.info("push %s: answer %s", session, ln)

    if as_json:
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "session": session}
    return Response(content=pc.localDescription.sdp, media_type="application/sdp")


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

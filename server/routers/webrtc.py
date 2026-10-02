from typing import Optional

from aiortc import RTCPeerConnection, RTCSessionDescription, mediastreams
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/webrtc", tags=["webrtc"])

pcs: set[RTCPeerConnection] = set()
stream_track: Optional[mediastreams.MediaStreamTrack] = None


class SDPOffer(BaseModel):
    sdp: str
    type: str = "offer"


@router.post("/push")
async def push(offer: SDPOffer) -> dict:
    pc = RTCPeerConnection()
    pcs.add(pc)
    received: list[mediastreams.MediaStreamTrack] = []

    @pc.on("track")
    def on_track(track: mediastreams.MediaStreamTrack) -> None:
        received.append(track)
        _set_stream_track(track)
        track.on("ended")(lambda: _set_stream_track(None, only_if=track))

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)
            for track in received:
                _set_stream_track(None, only_if=track)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        pcs.discard(pc)
        for track in received:
            _set_stream_track(None, only_if=track)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"invalid SDP offer: {e}")

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


@router.post("/watch")
async def watch(offer: SDPOffer) -> dict:
    if stream_track is None:
        raise HTTPException(status_code=404, detail="no device streaming")

    pc = RTCPeerConnection()
    pcs.add(pc)
    pc.addTrack(stream_track)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        pcs.discard(pc)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"invalid SDP offer: {e}")

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def _set_stream_track(track: Optional[mediastreams.MediaStreamTrack], only_if: Optional[mediastreams.MediaStreamTrack] = None) -> None:
    global stream_track
    if only_if is not None and stream_track is not only_if:
        return
    stream_track = track

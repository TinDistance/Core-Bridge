from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from aiortc import RTCPeerConnection, RTCSessionDescription

router = APIRouter(prefix="/webrtc", tags=["webrtc"])

pcs: set[RTCPeerConnection] = set()


class SDPOffer(BaseModel):
    sdp: str
    type: str = "offer"


def handle_track(track, kind: str) -> None:
    """Extension point: consume incoming tracks (e.g. forward to the desktop app)."""
    print(f"[webrtc] incoming {kind} track")


@router.post("/push")
async def push(offer: SDPOffer) -> dict:
    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("track")
    def on_track(track):
        handle_track(track, track.kind)

    @pc.on("connectionstatechange")
    async def on_state():
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

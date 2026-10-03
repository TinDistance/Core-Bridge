"""UDP 监听 + HTTP 输出。"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from server import command_udp
from server.rtp_relay import relay_status
from server.rtp_relay import SO_RCVBUF_BYTES
from server.video_hub import hub

logger = logging.getLogger("video_udp")

router = APIRouter(prefix="/video", tags=["video"])

UDP_PORT = int(os.environ.get("CORE_BRIDGE_VIDEO_UDP_PORT", "8001"))
UDP_HOST = os.environ.get("CORE_BRIDGE_VIDEO_UDP_HOST", "0.0.0.0")


_transport: asyncio.DatagramTransport | None = None

CLIENT_TIMING_KEYS = (
    "render_age_ms",
    "queue_ms",
    "backlog_packets",
    "backlog_bytes",
    "drops",
    "loop_us_avg",
    "loop_us_max",
)
_client_timing: dict[str, float | None] = {k: None for k in CLIENT_TIMING_KEYS}
_client_timing_at: float = 0.0
_client_timing_recv: int = 0


def _coerce_ms(v: object) -> float | None:
    """上报值安全转 float；缺失/None/NaN/负数一律 None（优雅降级）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x or x < 0:
        return None
    return round(x, 1)


def _client_timing_payload() -> dict[str, float | None]:
    """上报字段的对外视图。没有上报时全部 None，而不是缺键。"""
    return {k: _client_timing.get(k) for k in CLIENT_TIMING_KEYS}


class _VideoProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        try:
            try:
                host = addr[0] if isinstance(addr, tuple) else str(addr)
                port = int(addr[1]) if isinstance(addr, tuple) else 0
            except Exception:
                host, port = "", 0
            try:
                valid = hub.feed_datagram(data, host)
            except Exception:
                hub.chunks_bad += 1
                return
            if valid and port:
                try:
                    command_udp.note_video_sender(host, port)
                except Exception:
                    pass
        except Exception:
            pass


async def start_udp_listener(host: str = UDP_HOST, port: int = UDP_PORT) -> bool:
    """在 server lifespan 中调用；端口被占则记错但不让 HTTP 挂掉。返回是否成功。"""
    global _transport
    if _transport is not None:
        return True
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _VideoProtocol,
            local_addr=(host, port),
        )
        try:
            sock_obj = transport.get_extra_info("socket")
            if isinstance(sock_obj, socket.socket):
                try:
                    sock_obj.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except Exception:
                    pass
        except Exception:
            sock_obj = None
        _transport = transport
        logger.info("video UDP listening on %s:%d", host, port)
        # 命令推送使用独立 socket，不再跨线程复用 asyncio transport 的底层 socket
        command_udp.start_command_udp()
        return True
    except Exception as e:
        logger.error("video UDP bind %s:%d failed: %s", host, port, e)
        return False


async def stop_udp_listener() -> None:
    global _transport
    command_udp.stop_command_udp()
    t, _transport = _transport, None
    if t is not None:
        t.close()


@router.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(hub.status(UDP_PORT))


@router.get("/rtp_status")
async def rtp_status() -> JSONResponse:
    """H264 裸 RTP 中转状态（server/rtp_relay.py，udp:8002）。"""
    return JSONResponse(relay_status())


@router.post("/timing/client")
async def timing_client_report(request: Request) -> JSONResponse:
    """桌面端上报它**本机**测到的链路指标（Agent A 的 viewer.stats()）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=422)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "body must be object"}, status_code=422)
    clean: dict[str, float | None] = {}
    for key in CLIENT_TIMING_KEYS:
        clean[key] = _coerce_ms(payload.get(key))
    global _client_timing, _client_timing_at, _client_timing_recv
    _client_timing = clean
    _client_timing_at = time.time()
    _client_timing_recv = sum(1 for v in clean.values() if v is not None)
    return JSONResponse({"ok": True, "accepted": _client_timing_recv})


@router.get("/timing")
async def timing(n: int = Query(default=60, ge=5, le=120)) -> JSONResponse:
    """到达节奏诊断 + 延迟上界（原 /video/latency）。"""
    st = hub.status(UDP_PORT)
    rs = relay_status()
    now = time.time()
    payload: dict = {
        "live": st["live"],
        "frame_id": st["frame_id"],
        "staleness_ms": st["staleness_ms"],
        "fps": st["fps"],
        "jitter_ms": st["jitter_ms"],
        "intervals_ms": hub.recent_intervals_ms(n),
        "kbps": rs.get("kbps", 0.0),
        "pps": rs.get("pps", 0.0),
        "rcvbuf_bytes": rs.get("rcvbuf_bytes", SO_RCVBUF_BYTES),
        "rcvbuf_max_queue_ms": rs.get("rcvbuf_max_queue_ms", -1.0),
        "rtp_live": rs.get("live", False),
        "upstream": rs.get("upstream"),
        "downstreams": rs.get("downstreams", 0),
        "server_time": st["server_time"],
        "client_reported_at": _client_timing_at or None,
        "client_report_age_ms": round((now - _client_timing_at) * 1000.0, 1)
        if _client_timing_at else None,
        "client_fields": _client_timing_recv,
    }
    payload.update(_client_timing_payload())
    return JSONResponse(payload)


@router.get("/latest.jpg")
async def latest(since: int | None = Query(default=None)) -> Response:
    if hub.latest_jpeg is None:
        return Response(status_code=404, content="no frame yet")
    if since is not None and since == hub.latest_frame_id:
        return Response(status_code=304)
    return Response(
        content=hub.latest_jpeg,
        media_type="image/jpeg",
        headers={
            "X-Frame-Id": str(hub.latest_frame_id),
            "X-Frame-Staleness-Ms": str(hub.staleness_ms()),
            "Cache-Control": "no-store",
        },
    )


@router.get("/mjpeg")
async def mjpeg(fps: int = Query(default=10, ge=1, le=30)):
    boundary = "frame"
    interval = 1.0 / fps

    async def gen():
        last_id = -1
        while True:
            if hub.latest_jpeg is not None and hub.latest_frame_id != last_id:
                last_id = hub.latest_frame_id
                chunk = (
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(hub.latest_jpeg)}\r\n"
                    f"X-Frame-Id: {last_id}\r\n\r\n"
                ).encode() + hub.latest_jpeg + b"\r\n"
                yield chunk
            await asyncio.sleep(interval)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store"},
    )

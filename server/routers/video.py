"""UDP 监听 + HTTP 输出。

UDP 端口默认 8001，可用环境变量 CORE_BRIDGE_VIDEO_UDP_PORT 覆盖。
HTTP：
  GET /video/status      状态/统计（desktop 轮询健康度）
  GET /video/latest.jpg  最新完整帧；?since=<frame_id> 若无新帧返回 304
  GET /video/mjpeg       multipart MJPEG（浏览器调试用，非桌面主链路）
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from server import command_udp
from server.rtp_relay import relay_status
from server.video_hub import hub

logger = logging.getLogger("video_udp")

router = APIRouter(prefix="/video", tags=["video"])

UDP_PORT = int(os.environ.get("CORE_BRIDGE_VIDEO_UDP_PORT", "8001"))
UDP_HOST = os.environ.get("CORE_BRIDGE_VIDEO_UDP_HOST", "0.0.0.0")

_transport: asyncio.DatagramTransport | None = None


class _VideoProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        try:
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            port = int(addr[1]) if isinstance(addr, tuple) else 0
        except Exception:
            host, port = "", 0
        valid = hub.feed_datagram(data, host)
        # 学习视频发送地址，作为命令反向推送目标（头部校验通过即算）
        if valid and port:
            command_udp.note_video_sender(host, port)


async def start_udp_listener(host: str = UDP_HOST, port: int = UDP_PORT) -> None:
    """在 server lifespan 中调用；端口被占则记错但不让 HTTP 挂掉。"""
    global _transport
    if _transport is not None:
        return
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _VideoProtocol,
            local_addr=(host, port),
        )
        # 加大内核收包缓冲，WiFi 突发下减少丢包
        sock_obj = None
        try:
            sock_obj = transport.get_extra_info("socket")
            if isinstance(sock_obj, socket.socket):
                sock_obj.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except Exception:
            pass
        _transport = transport
        logger.info("video UDP listening on %s:%d", host, port)
        # 命令反向推送复用同一视频 socket：目标地址由收到的视频分片学习
        # （note_video_sender），无需 K230 注册；取底层 socket.sendto
        # （py3.14 的 get_extra_info 返回 TransportSocket 包装，需剥出 _sock）
        raw_sock = getattr(sock_obj, "_sock", sock_obj)
        command_udp.start_command_udp(sendto=raw_sock.sendto)
    except OSError as e:
        logger.error("video UDP bind %s:%d failed: %s", host, port, e)


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


@router.get("/timing")
async def timing(n: int = Query(default=60, ge=5, le=120)) -> JSONResponse:
    """到达节奏诊断（原 /video/latency，改名因为它不含任何延迟）。

    只给帧间隔序列 / 抖动 / fps / 陈旧度，供桌面端画曲线或做二次分析。
    这里的 staleness_ms 是"最后一帧到 server 有多久"，不是端到端延迟 ——
    旧名 /video/latency 会让人以为它能回答"画面滞后多少"，实际上链路压 4s
    它照样读 33ms。真延迟要等 K230 侧 capture_ts（protocol v2）。
    """
    st = hub.status(UDP_PORT)
    return JSONResponse(
        {
            "live": st["live"],
            "frame_id": st["frame_id"],
            "staleness_ms": st["staleness_ms"],
            "fps": st["fps"],
            "jitter_ms": st["jitter_ms"],
            "intervals_ms": hub.recent_intervals_ms(n),
            "server_time": st["server_time"],
        }
    )


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
            # 陈旧度不是延迟，名字必须说清楚，否则客户端会拿它当管线延迟
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

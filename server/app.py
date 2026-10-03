from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.logs import hub as log_hub
from server.routers import command, heartbeat, logs, video, webrtc
from server.routers.video import start_udp_listener, stop_udp_listener
from server.rtp_relay import start_relay, stop_relay


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    log_hub.set_loop(asyncio.get_running_loop())
    await start_udp_listener()
    # 命令由 start_udp_listener 内部在同一视频 socket 上反向推送
    # （server/command_udp.py，目标地址从视频分片学习，无需 K230 注册）
    start_relay()  # H264 裸 RTP 中转（udp:8002），与 JPEG 链路并存
    try:
        yield
    finally:
        stop_relay()
        await stop_udp_listener()


app = FastAPI(title="Core-Bridge Server", version="0.2.0", lifespan=lifespan)

app.include_router(command.router)
app.include_router(heartbeat.router)
app.include_router(video.router)
# 旧 WebRTC 链路保留做回滚，但桌面默认走 /video（UDP+JPEG）
app.include_router(webrtc.router)
app.include_router(logs.router)


def main() -> None:
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000)

from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.command_udp import start_command_udp, stop_command_udp
from server.logs import hub as log_hub
from server.routers import command, heartbeat, logs, video, webrtc
from server.routers.video import start_udp_listener, stop_udp_listener


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    log_hub.set_loop(asyncio.get_running_loop())
    await start_udp_listener()
    start_command_udp()
    try:
        yield
    finally:
        stop_command_udp()
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

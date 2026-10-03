import os

from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.logs import hub as log_hub
from server.routers import command, heartbeat, logs, video
from server.routers.video import start_udp_listener, stop_udp_listener
from server.rtp_relay import start_relay, stop_relay


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import logging

    logger = logging.getLogger("core-bridge")
    log_hub.set_loop(asyncio.get_running_loop())
    try:
        ok_udp = await start_udp_listener()
        if not ok_udp:
            logger.warning("video UDP listener not started; JPEG chain disabled")
    except Exception as e:
        logger.error("video UDP listener crashed: %s", e)
    try:
        ok_rtp = start_relay()
        if not ok_rtp:
            logger.warning("RTP relay not started; H265 chain disabled")
    except Exception as e:
        logger.error("RTP relay crashed: %s", e)
    try:
        yield
    finally:
        try:
            stop_relay()
        except Exception:
            pass
        try:
            await stop_udp_listener()
        except Exception:
            pass
        log_hub.set_loop(None)


app = FastAPI(title="Core-Bridge Server", version="0.2.0", lifespan=lifespan)

app.include_router(command.router)
app.include_router(heartbeat.router)
app.include_router(video.router)
app.include_router(logs.router)


def main() -> None:
    import uvicorn

    host = os.environ.get("CORE_BRIDGE_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("CORE_BRIDGE_HTTP_PORT", "8000"))
    uvicorn.run("server.app:app", host=host, port=port)

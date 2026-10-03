import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["heartbeat"])


@router.get("/test")
async def test() -> PlainTextResponse:
    return PlainTextResponse("pong")


@router.get("/ping")
async def ping() -> dict:
    """链路存活探针：桌面端用它测 server 往返 RTT + 对时。"""
    return {"ok": True, "server_time": round(time.time(), 3)}

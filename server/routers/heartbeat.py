import time

from fastapi import APIRouter

router = APIRouter(tags=["heartbeat"])


@router.get("/test")
async def test() -> str:
    return "pong"


@router.get("/ping")
async def ping() -> dict:
    """延迟探针：桌面端用它测量 server 往返 RTT + 对时。

    返回 server 的 wall clock，桌面端可估算时钟偏移，
    e2e 延迟 = (client_rx_time - server_latest_wall) 的近似。
    """
    return {"ok": True, "server_time": round(time.time(), 3)}

import time

from fastapi import APIRouter

router = APIRouter(tags=["heartbeat"])


@router.get("/test")
async def test() -> str:
    return "pong"


@router.get("/ping")
async def ping() -> dict:
    """链路存活探针：桌面端用它测 server 往返 RTT + 对时。

    只返回 ok + server wall clock。⚠️ 这里**不能**算出 e2e 延迟：旧文档写过的
    "e2e = client_rx_time - server_latest_wall" 是错的，本接口根本不返回
    latest_wall（那是 /video/status 的陈旧度口径，见 video_hub.staleness_ms），
    而且即使用上也是拿收包时刻减到达时刻，算的是同一个"到达陈旧度"，测不出
    WiFi 空中段的排队延迟。真正的 e2e 需要 K230 采集时刻戳（protocol v2）。
    """
    return {"ok": True, "server_time": round(time.time(), 3)}

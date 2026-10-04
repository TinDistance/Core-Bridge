"""RTP 中继线程监督：每 interval_s 检查一次，死了就重启并记 error。

为什么需要它（S1 + S2，见 `docs/video-latency-optimization.md` §2.1）
    `RtpRelay._loop` 已经不再因为 `ConnectionResetError` 退出，但那个修复
    只覆盖**已知**的一类错误。任何未预料的异常、线程被外部杀掉、或将来
    新加的代码引入的缺陷，都会让中继线程静默死亡。

    后果不是「视频卡一下」，而是**视频和控制同时消失**：
    `command_udp.note_video_sender()` 的唯一调用点在 `RtpRelay._on_rtp`
    里，端点 2.5 s 后过期 → `command_udp._run` 停发 → 机器人在没有画面、
    也没有提示的情况下失去控制。`/video/rtp_status` 只能看出 `live=false`，
    没有任何东西会把它拉起来。

    所以把「进程还活着」和「中继线程还活着」解耦：supervisor 是最后一道
    保险，宁可打一条 error 日志，也不要静默失去链路。

本模块**不在 import 时启动任何东西**：由 `server/app.py` 的 lifespan 创建
asyncio task（主控 agent 接线）。
"""
from __future__ import annotations

import asyncio
import logging

from server import rtp_relay

logger = logging.getLogger("relay_supervisor")


def relay_thread_alive() -> bool:
    """中继线程当前是否在跑。未启动过也算「不在跑」（触发首次启动）。"""
    relay = rtp_relay._relay
    if relay is None:
        return False
    thread = relay._thread
    return thread is not None and thread.is_alive()


async def supervise_relay(interval_s: float = 2.0,
                          backoff_max_s: float = 30.0) -> None:
    """每 interval_s 秒检查一次中继线程，已死则重启。

    - `asyncio.CancelledError`（lifespan 关闭时的 task cancel）正常退出，不打堆栈。
    - 其他异常兜底捕获后继续循环：supervisor 自己挂掉等于失去最后一道保险。
    - `start_relay()` 幂等：线程还活着时直接返回 True，不会误重启。
    - **重启失败时指数退避**（间隔 -> 2x -> 4x … -> backoff_max_s，成功的检查
      周期会把退避清零）。端口被别的进程长期占用是可持续的失败状态，没有退避
      就会每 interval_s 刷一条 `logger.error`，把真正的错误淹没在噪声里
      （本机实测：bind 永久失败时 50 次尝试 = 50 行 error）。
    """
    try:
        interval = max(0.2, float(interval_s))
    except (TypeError, ValueError):
        interval = 2.0
    try:
        cap = max(interval, float(backoff_max_s))
    except (TypeError, ValueError):
        cap = 30.0
    delay = interval
    logger.info("RTP relay supervisor started (interval %.1fs)", interval)
    try:
        while True:
            await asyncio.sleep(delay)
            try:
                if relay_thread_alive():
                    delay = interval
                    continue
                logger.error("RTP relay thread died; restarting")
                if rtp_relay.start_relay():
                    logger.warning("RTP relay restarted")
                    delay = interval
                else:
                    delay = min(cap, max(interval, delay * 2.0))
                    logger.error("RTP relay restart failed; next attempt in %.1fs",
                                 delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = min(cap, max(interval, delay * 2.0))
                logger.error("RTP relay supervisor check failed: %s; "
                             "next attempt in %.1fs", e, delay)
    except asyncio.CancelledError:
        logger.info("RTP relay supervisor stopped")
        raise
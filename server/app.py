import asyncio
import contextlib
import logging
import os
import sys

from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.logs import hub as log_hub
from server.routers import command, heartbeat, logs, video
from server.routers.video import (start_command_pusher, start_udp_listener,
                                  stop_udp_listener)
from server.command_udp_in import (start_command_udp_in, stop_command_udp_in)
from server.rtp_relay import start_relay, stop_relay
from server.relay_supervisor import supervise_relay

logger = logging.getLogger("core-bridge")

# 进程级调优。放在 lifespan 而不是 main() 里，因为**生产的启动路径不是 main()**：
# `desktop/server_manager.py` 用 `[python, -m, "uvicorn", "server.app:app", ...]`
# 拉起本进程，`main()` 永远不会被调用，那里的 S4/S6 设置是死代码。
# lifespan 对所有启动方式都生效，且早于中继/推送线程创建。
_SWITCHINTERVAL_S = 0.001     # S4：GIL 切换粒度，默认 5ms
_TIME_PERIOD_MS = 1           # S6/D5：Windows 定时器粒度，默认 15.6ms
_time_period_active = False
_orig_switchinterval: float | None = None


def _tune_process() -> None:
    """S4 + S6/D5 的进程级调优。任何启动路径都必须经过这里，故放在 lifespan。"""
    global _time_period_active, _orig_switchinterval
    # S4：中继/日志/uvicorn 线程争用 GIL，默认 5ms 切换间隔会让中继最多等 5ms。
    # 效果未实测，代价是上下文切换变频繁（对本进程内的其它线程同样生效）。
    if _orig_switchinterval is None:
        try:
            _orig_switchinterval = sys.getswitchinterval()
            sys.setswitchinterval(_SWITCHINTERVAL_S)
        except Exception:
            _orig_switchinterval = None
    # S6/D5：Windows 定时器默认 15.6ms 粒度，会把 Event.wait / 带超时的 recv
    # 全部取整到 15.6ms。请求 1ms（未实测，失败即退回默认）。
    if sys.platform == "win32" and not _time_period_active:
        try:
            import ctypes

            ctypes.windll.winmm.timeBeginPeriod(_TIME_PERIOD_MS)
            _time_period_active = True
        except Exception:
            pass


def _restore_process() -> None:
    """与 `_tune_process` 配对：还原定时器粒度和 GIL 切换间隔。

    `timeEndPeriod` 必须配对，否则进程存活期间系统一直停在 1ms 粒度（功耗、
    笔记本风扇都会变差）。`setswitchinterval` 只影响本进程，还原它只是为了
    lifespan 可重入（测试里反复 enter/exit）时不留下累积状态。
    """
    global _time_period_active
    if sys.platform == "win32" and _time_period_active:
        try:
            import ctypes

            ctypes.windll.winmm.timeEndPeriod(_TIME_PERIOD_MS)
        except Exception:
            pass
        _time_period_active = False
    if _orig_switchinterval is not None:
        try:
            sys.setswitchinterval(_orig_switchinterval)
        except Exception:
            pass


def _silence_uvicorn_access_console() -> None:
    """S6：掐掉 uvicorn access log 的控制台/文件 handler，保留广播 handler。

    文档 §4.1 给的 `uvicorn.run(..., access_log=False)` 只在 `main()` 这条启动
    路径上有效；走 `python -m uvicorn` 时 access log 照开：手柄每次
    `POST /command` 都写一行到 stderr（Windows 控制台同步写很慢，QuickEdit
    选中文本还会整体挂起事件循环），`desktop/server_manager.py` 也记录过
    access log 涨到 1.4MB+。这里在 lifespan 里摘掉 `uvicorn.access` 上的
    `StreamHandler`（uvicorn 装的 `default` 就是它），保留 `logs.LogHub` 的
    `BroadcastHandler`（它继承 `logging.Handler` 而非 `StreamHandler`，
    所以面板的日志流不受影响）。
    """
    try:
        acc = logging.getLogger("uvicorn.access")
        for h in [h for h in acc.handlers
                  if isinstance(h, logging.StreamHandler)]:
            acc.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _tune_process()
    _silence_uvicorn_access_console()
    log_hub.set_loop(asyncio.get_running_loop())

    # 启停顺序：推送线程先于端点学习启动，这样即便 JPEG 链路默认关闭，
    # K230 的 AA55 命令帧推送也不会因为监听器没起而丢失（S2 的级联教训）。
    try:
        if not start_command_pusher():
            logger.warning("command UDP pusher not started; control link degraded")
    except Exception as e:
        logger.error("command pusher crashed: %s", e)
    try:
        if not start_command_udp_in():
            logger.warning(
                "command UDP in not started; desktop->server UDP command "
                "channel disabled (HTTP POST /command still works)")
    except Exception as e:
        logger.error("command UDP in crashed: %s", e)
    try:
        ok_udp = await start_udp_listener()
        if not ok_udp:
            logger.warning("video UDP listener not started; JPEG chain disabled")
    except Exception as e:
        logger.error("video UDP listener crashed: %s", e)
    try:
        ok_rtp = start_relay()
        if not ok_rtp:
            logger.warning("RTP relay not started; H264 chain disabled")
    except Exception as e:
        logger.error("RTP relay crashed: %s", e)

    # S1/S2 的最后一道保险：中继线程一旦因未知异常退出，2s 内自动重启。
    # 模块自身不启动任何东西，task 归 lifespan 所有。
    relay_task = None
    try:
        relay_task = asyncio.create_task(supervise_relay(2.0))
    except Exception as e:
        logger.error("RTP relay supervisor not started: %s", e)

    try:
        yield
    finally:
        # 必须先撤监督再停中继，否则 supervisor 会把刚停掉的 relay 拉回来。
        if relay_task is not None:
            relay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay_task
        try:
            stop_relay()
        except Exception:
            pass
        try:
            stop_command_udp_in()
        except Exception:
            pass
        try:
            await stop_udp_listener()   # 内部含 stop_command_pusher()
        except Exception:
            pass
        _restore_process()
        log_hub.set_loop(None)


app = FastAPI(title="Core-Bridge Server", version="0.3.0", lifespan=lifespan)

app.include_router(command.router)
app.include_router(heartbeat.router)
app.include_router(video.router)
app.include_router(logs.router)


def main() -> None:
    import uvicorn

    host = os.environ.get("CORE_BRIDGE_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("CORE_BRIDGE_HTTP_PORT", "8000"))
    # S4/S6 的进程级调优在 lifespan 里做（`_tune_process`），因为生产启动路径是
    # `python -m uvicorn server.app:app`，不经过 main()。
    uvicorn.run("server.app:app", host=host, port=port,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()

"""桌面 → 服务端 UDP 命令通道（`docs/video-latency-optimization.md` §4.4 方案 A）。

为什么有这个模块
    原来的命令方向是 `GamepadPusher` --HTTP POST--> FastAPI --轮询(store)--> UDP --> K230。
    两个延迟来源都出在中间那一跳：
      - 服务端要等轮询周期才知道指令变了（150 Hz 也只是 6.7 ms 的平均值，
        Windows 上 `Event.wait` 还会被 15.6 ms 粒度取整，见 §2.1 S5）；
      - HTTP over TCP 有队头阻塞，一次重传（Windows 最小 RTO 约 200–300 ms）
        会把后面所有指令一起排队，而且恰好发生在视频同样变差的时刻（D7）。
    改成 UDP 之后：桌面「输入变化时立即发、无变化时 50 ms 保活」，服务端
    **到达即转发**（不再等任何轮询），控制路径稳态估算从 ≈40–80 ms 降到 ≈15–25 ms
    （§4.4 的对比表）。

线路格式（与 A5 桌面端 / A3 对齐，字段顺序权威）
    b"CBC" | ver(u8) | seq(u32 BE) | ts_ms(u32 BE) | raw16      共 28 字节

    ⚠️ 规格里写的是「共 24 字节」，但按字段逐项相加是 3 + 1 + 4 + 4 + 16 = **28**。
       文档的 24 是算术笔误（§4.4 同一节的字段列表本身也是 28 字节）。
       本实现以**字段列表**为准，接受 28 字节；同时为了兼容按 24 字节写的客户端，
       也接受缺 ts_ms 的 24 字节形式（此时 `client_ts` 用本地收包时刻代替）。
       `CMDIN_PACKET_LEN` 暴露规范值给对端断言。

与既有逻辑的关系
    - 写入 store 走 `server.routers.command` 的 `_store`/`_lock`（不能改那个文件），
      字段与 `post_command` 一致；`cmds=None`，让服务端走 `_resolve_cmds` 兜底解析，
      桌面端仍然是唯一的语义来源。
    - 迟滞 active 状态复用 `command_udp.update_active_state`，帧编码复用
      `command_udp.build_uart_frame`，目标端点用 `command_udp._current_endpoint()`
      （由视频包学习）。这个模块不重复实现任何协议逻辑。
    - 300 ms 静默 = 停车：与 `command_udp.COMMAND_STALE_S` 同一阈值，语义一致。

健壮性
    解析/发送的所有异常都被吞掉，warning 最多每 `LOG_THROTTLE_S` 一条；
    线程绝不因数据异常而死（它一旦死，桌面重启后控制链路就静默消失了，
    和 §2.1 S1/S2 是同一类事故）。
"""
from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time

from server import command_udp
from server.routers import command as command_router

logger = logging.getLogger("command_udp_in")

CMDIN_MAGIC = b"CBC"
CMDIN_VER = 1
CMDIN_PORT = int(os.environ.get("CORE_BRIDGE_CMD_UDP_PORT", "8010"))

# 规范包长（含 ts_ms）。见模块 docstring 关于「24 vs 28」的说明。
CMDIN_PACKET_LEN = len(CMDIN_MAGIC) + 1 + 4 + 4 + 16      # 28
# 兼容形式：缺 ts_ms 的 24 字节包。
CMDIN_PACKET_LEN_NO_TS = len(CMDIN_MAGIC) + 1 + 4 + 16     # 24

# 立即转发的最小间隔（上限 50 Hz）。桌面端输入变化时的包率可达 100 Hz，
# 但 K230 每 ≤4 ms 就泵一次控制 socket，同一条指令重复下发是幂等的；把上限
# 压在 50 Hz 可以挡住恶意/失控客户端的洪泛，也把下行小包压到 ~2 KB/s。
# 被节流掉的包照样更新 store 和「最新状态」，下一周期由 command_udp 的兜底
# 推送线程补发，不会丢指令。
CMDIN_MIN_INTERVAL_S = 0.02

# 「静默 = 停车」阈值。与 command_udp.COMMAND_STALE_S 一致（0.3 s）。
#
# ⚠️ 这是**服务端侧唯一的停车超时**，不是「配合 K230 的 200ms 自停」。
#    K230 的 CMD_STALE_MS = 200 只打印 "[CMD] feed lost" 并置 `cmdline.ok=False`，
#    而 `cmdline.ok` 在 k230/rtp_push.py 里只被 stats_line() 读，不参与任何停机
#    决策；`build_uart_frame()` 无激活条目时返回 None（不发帧），所以也不会有
#    显式零速帧。真实停车依赖 UART 下游 MCU 固件自己的超时（不在本仓库）。
#    详见 server/command_udp.py 的模块 docstring。
#
# 桌面端 50 ms 保活，连续丢 6 个包才会触发；留 6 倍余量是为了抗 WiFi 突发丢包
# （文档 §2.2 K1：突发丢包高度相关，不能按独立丢包算）。要更短的 fail-safe
# 必须先确认 MCU 固件的 UART 超时，否则只是把风险挪到固件侧。
CMD_SILENCE_S = 0.30

RECV_SIZE = 2048
SELECT_TIMEOUT_S = 0.2
LOG_THROTTLE_S = 2.0

# seq 回绕用 32 位比较：`(seq - last) & 0xFFFFFFFF < 0x80000000` 视为「更新」。
SEQ_HALF = 0x80000000


_stop = threading.Event()
_thread: threading.Thread | None = None
_sock: socket.socket | None = None
_send_sock: socket.socket | None = None
_port = 0

_lock = threading.Lock()
_active: dict[str, bool] = {n: False for n in command_udp._CMD_ORDER}

_rx = 0
_tx = 0
_dropped_bad = 0
_dropped_stale = 0
_resyncs = 0
_throttled = 0
_last_seq: int | None = None
_last_client_ts_ms: int | None = None
_last_rx_at = 0.0            # time.monotonic()
_last_tx_at = 0.0            # time.monotonic()
_last_warn_at = 0.0


def _warn(msg: str, *args) -> None:
    """限频 warning：任何异常都不许刷屏，但也不能完全静默。"""
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at < LOG_THROTTLE_S:
        return
    _last_warn_at = now
    logger.warning(msg, *args)


def _parse(data: bytes) -> tuple[int, int | None, bytes] | None:
    """校验并拆包；非法返回 None。返回 (seq, ts_ms|None, raw16)。"""
    if len(data) != CMDIN_PACKET_LEN and len(data) != CMDIN_PACKET_LEN_NO_TS:
        return None
    if data[:3] != CMDIN_MAGIC:
        return None
    if data[3] != CMDIN_VER:
        return None
    if len(data) == CMDIN_PACKET_LEN:
        seq = int.from_bytes(data[4:8], "big")
        ts_ms = int.from_bytes(data[8:12], "big")
        raw16 = data[12:28]
    else:
        seq = int.from_bytes(data[4:8], "big")
        ts_ms = None
        raw16 = data[8:24]
    if len(raw16) != 16:
        return None
    return seq, ts_ms, raw16


def _publish_store(seq: int, ts_ms: int | None, raw16: bytes) -> str | None:
    """把指令写进 `command._store`，返回规范化后的 32 字符大写 hex。

    字段与 `CommandPush`/`post_command` 对齐。直接操作 `_store`/`_lock` 是因为
    UDP 通道不能改动 `routers/command.py`（它的 Pydantic 模型是给 HTTP 用的）。

    `cmds=None` 是刻意的：UDP 线路里没有语义命令字段，而服务端本来就有
    `_resolve_cmds(raw_hex, None)` 兜底解析（摇杆轴 → MOVE/TURRET，与桌面端
    `commands.build_commands` 同语义）。这样**桌面端仍是唯一的语义来源**，
    服务端不会因为两条通道的解析逻辑漂移而下发不同的指令。代价是 hat/btn
    按键在 UDP 通道上无法表达——这是 `_resolve_cmds` 已有的取舍，不是这里引入的。

    `slot`/`packet` 保留上一次的值而不是清成 None：UDP 包不携带它们，写 None
    会让面板上的 slot/packet 在 UDP 通道活跃期间恒为 null。
    """
    global _rx
    cleaned = command_router._clean_hex(raw16.hex())
    if cleaned is None:
        # 理论上 16 字节 hex 一定合法（_clean_hex 只看长度和字符集），
        # 仍然当作坏包处理，不污染 store。
        return None
    now = time.time()
    with command_router._lock:
        store = command_router._store
        store["raw_hex"] = cleaned
        store["cmds"] = None
        store["client_ts"] = (ts_ms / 1000.0) if ts_ms is not None else round(now, 3)
        store["seq"] = seq
        store["updated_at"] = now
        store["count"] += 1
    _rx += 1
    return cleaned


def _forward(cleaned: str) -> None:
    """到达即转发：迟滞 → 编码 → 发给已学习的 K230 端点。"""
    global _active, _tx, _throttled, _last_tx_at
    snap = {"raw_hex": cleaned, "cmds": None}
    with _lock:
        _active = command_udp.update_active_state(
            _active, command_udp._known_cmds_by_name(snap))
        active = dict(_active)
    now = time.monotonic()
    if now - _last_tx_at < CMDIN_MIN_INTERVAL_S:
        # 洪泛限流：store 已更新，兜底线程会在下一周期补发这一帧。
        _throttled += 1
        return
    frame = command_udp.build_uart_frame(snap, active)
    if frame is None:
        return          # 无激活条目 → 本次不发送（K230 侧保持上一条）
    endpoint = command_udp._current_endpoint()
    if endpoint is None:
        return          # 端点未学习（还没有视频包）→ 不崩、不发，等下一个包
    s = _send_sock
    if s is None:
        return
    try:
        s.sendto(frame, endpoint)
    except OSError:
        return
    _tx += 1
    _last_tx_at = time.monotonic()


def _on_packet(data: bytes) -> None:
    global _dropped_bad, _dropped_stale, _resyncs, _last_seq, _last_client_ts_ms
    global _last_rx_at
    parsed = _parse(data)
    if parsed is None:
        _dropped_bad += 1
        return
    seq, ts_ms, raw16 = parsed
    # 严格递增判定（seq 回绕按 32 位比较）。乱序或重复的旧包直接丢弃。
    #
    # 「桌面端重启 → seq 归 1」在数学上与「乱序」无法区分（回退量只有几十，
    # 和局域网内的乱序窗口同量级）。判据用的是**静默时长**：能收到回退的包，
    # 说明我们已经静默超过 CMD_SILENCE_S，这期间本通道早已停发、K230 侧
    # CMD_STALE_MS = 200 也已经自停——此时接受一个更小的 seq 没有任何代价，
    # 却能恢复控制（§8.2 验收项「桌面重启/关闭后自动恢复」）。而链路正忙时
    # 出现的小回退就是真乱序，照丢。
    if _last_seq is not None:
        forward = (seq - _last_seq) & 0xFFFFFFFF
        if forward == 0:
            _dropped_stale += 1
            return
        if forward >= SEQ_HALF:
            silent_for = (time.monotonic() - _last_rx_at) if _last_rx_at else 1e9
            if silent_for > CMD_SILENCE_S:
                _resyncs += 1
                logger.info("command UDP seq reset detected after %.0f ms silence "
                            "(%d -> %d), resynced", silent_for * 1000.0,
                            _last_seq, seq)
            else:
                _dropped_stale += 1
                return
    cleaned = _publish_store(seq, ts_ms, raw16)
    if cleaned is None:
        _dropped_bad += 1
        return
    _last_seq = seq
    _last_client_ts_ms = ts_ms
    _last_rx_at = time.monotonic()
    _forward(cleaned)


def _check_silence() -> None:
    """静默超过 CMD_SILENCE_S → 清空迟滞 active（语义同快照过期）。"""
    global _active
    with _lock:
        if not _last_rx_at or _last_rx_at <= 0.0:
            return
        if time.monotonic() - _last_rx_at <= CMD_SILENCE_S:
            return
        if not any(_active.values()):
            return
        _active = {n: False for n in command_udp._CMD_ORDER}


def _drain(s: socket.socket) -> None:
    resets = 0
    while True:
        try:
            data, _addr = s.recvfrom(RECV_SIZE)
            resets = 0
        except BlockingIOError:
            return
        except ConnectionResetError:
            # Windows: 上一次 sendto 的 ICMP 端口不可达。本 socket 只收不发，
            # 理论上不该出现；出现也不能让它杀掉线程，也不能无限自旋。
            resets += 1
            if resets > 32:
                return
            continue
        except OSError as e:
            if _stop.is_set():
                return
            _warn("command UDP in recv error: %s", e)
            return
        if _stop.is_set():
            return
        try:
            _on_packet(data)
        except Exception as e:
            _warn("command UDP in packet failed: %s", e)


def _run(s: socket.socket) -> None:
    """线程主体。唯一出口是 `_stop`，任何异常都被吞掉后继续。"""
    while not _stop.is_set():
        # 下一件事：静默到期（把 select 的等待上限压到「离停车还有多久」），
        # 这样 active 状态在 300 ms 边界后 ~1 ms 内被清掉，而不是最多再等 200 ms。
        #
        # 只在 remaining **为正**（还没到边界）时收紧；remaining <= 0 表示边界已经
        # 过了，`_check_silence()` 每轮都会跑、没有可清的东西，此时必须退回
        # SELECT_TIMEOUT_S 的空转。曾经的写法是 `timeout = max(0.0, remaining)`：
        # 一旦收过包（`_rx > 0`）且静默超过 CMD_SILENCE_S，remaining 恒为负 →
        # timeout 变成 0.0 → `select(..., 0)` 立刻返回 → 线程从「桌面端断开」那一刻
        # 起永久空转占满一个核（本机实测 99.5% CPU，且不会自愈，因为 _last_rx_at
        # 不会被清零）。这直接踩 S4：GIL 被一个纯空转线程占着。
        timeout = SELECT_TIMEOUT_S
        last_rx = _last_rx_at
        if last_rx and _rx:
            remaining = CMD_SILENCE_S - (time.monotonic() - last_rx)
            if 0.0 < remaining < timeout:
                timeout = remaining
        try:
            try:
                readable, _, _ = select.select([s], [], [], timeout)
            except (OSError, ValueError) as e:
                if _stop.is_set():
                    return
                _warn("command UDP in select stopped: %s", e)
                time.sleep(0.05)
                continue
            if readable:
                _drain(s)
            _check_silence()
        except Exception as e:
            _warn("command UDP in loop error: %s", e)
            time.sleep(0.01)


def start_command_udp_in(port: int | None = None) -> bool:
    """启动桌面→服务端 UDP 命令通道监听。幂等，返回是否成功。

    同步阻塞线程（不用 asyncio），可以直接在纯线程环境或 lifespan 里调用。
    端口被占 → 记 error 并返回 False，**不影响 HTTP 服务和其他链路**。
    """
    global _thread, _sock, _send_sock, _port
    if _thread is not None and _thread.is_alive():
        return True
    # 上一轮残留（线程意外死掉后重入）：先回收旧 socket，避免句柄泄漏
    for stale in (_sock, _send_sock):
        try:
            if stale is not None:
                stale.close()
        except Exception:
            pass
    _sock = None
    _send_sock = None
    try:
        p = int(port) if port is not None else CMDIN_PORT
    except (TypeError, ValueError):
        p = CMDIN_PORT
    _stop.clear()
    s = None
    out = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 << 10)
        except OSError:
            pass
        s.bind(("0.0.0.0", p))
        s.setblocking(False)
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.setblocking(False)
    except OSError as e:
        logger.error("command UDP in bind 0.0.0.0:%d failed: %s", p, e)
        for sock in (s, out):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
        return False
    _sock = s
    _send_sock = out
    _port = p
    _thread = threading.Thread(target=_run, args=(s,), daemon=True,
                                name="command-udp-in")
    _thread.start()
    logger.info("command UDP in listening on 0.0.0.0:%d (v%d, %d B packets, "
                "silence stop %.0f ms)", p, CMDIN_VER, CMDIN_PACKET_LEN,
                CMD_SILENCE_S * 1000)
    return True


def stop_command_udp_in(timeout: float = 2.0) -> None:
    """停止监听并回收线程/socket。幂等（未启动时是空操作）。"""
    global _thread, _sock, _send_sock, _port
    _stop.set()
    t, _thread = _thread, None
    # 自唤醒：select 阻塞中的线程立即返回，不需要等满 SELECT_TIMEOUT_S
    if _port:
        w = _send_sock
        if w is not None:
            try:
                w.sendto(b"", ("127.0.0.1", _port))
            except OSError:
                pass
    if t is not None and t is not threading.current_thread():
        t.join(timeout=timeout)
    s, _sock = _sock, None
    o, _send_sock = _send_sock, None
    _port = 0
    for sock in (s, o):
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def command_udp_in_status() -> dict:
    """诊断快照（供 `/video/rtp_status` 一类的端点使用）。线程安全。"""
    now = time.monotonic()
    with _lock:
        active = dict(_active)
    return {
        "listening": bool(_thread is not None and _thread.is_alive()),
        "port": _port or CMDIN_PORT,
        "rx": _rx,
        "tx": _tx,
        "last_seq": _last_seq,
        "last_rx_age_ms": round((now - _last_rx_at) * 1000.0, 1)
        if _last_rx_at else -1.0,
        "dropped_bad": _dropped_bad,
        "dropped_stale": _dropped_stale,
        "active": active,
        # 以下为附加诊断项
        "resyncs": _resyncs,
        "throttled": _throttled,
        "packet_len": CMDIN_PACKET_LEN,
        "ver": CMDIN_VER,
        "last_client_ts_ms": _last_client_ts_ms,
        "silence_stop_ms": int(CMD_SILENCE_S * 1000),
        "min_forward_interval_ms": int(CMDIN_MIN_INTERVAL_S * 1000),
    }

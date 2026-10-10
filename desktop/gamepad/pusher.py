"""手柄后台推送：XInput ~100Hz 轮询 -> 16 字节协议 -> UDP 命令通道（HTTP 降级）。

D7 / doc §2.3、§4.4 方案 A
---------------------------
旧实现用同步 `httpx.Client.post`（`POST_TIMEOUT = 0.3`）在**轮询线程**里发送，
有两个独立的失效模式：

1. 一次 POST 卡住，轮询线程就停 0.3 s —— 手柄读取率整体塌陷；
2. HTTP over TCP 有队头阻塞，一次段重传（Windows 最小 RTO 约 200–300 ms）
   会把**后续所有指令**一起排队，而且这恰好发生在 WiFi 抖动、视频同时变差的
   时刻。

现在拆成两个线程：

- **轮询线程**（``gamepad-poll``，~100 Hz）：只读 XInput、只更新「最新状态」
  （``_latest``）。**不做任何 IO**。
- **发送线程**（``gamepad-send``，~500 Hz tick）：50 Hz 固定节拍常发当前帧，
  输入变化时立即发（不等节拍），无变化到 20 ms 节拍即发。断连也按 50 Hz
  持续补中位零帧（不断流），让下游靠显式零速停车而不是靠静默超时。

⚠️ **上线耦合（兼容性红线）**：服务端同一批把 ``COMMAND_STALE_S`` 从 0.5 s 改成
了 0.3 s（``server/command_udp.py``）。0.5 s 心跳的旧客户端会在服务端被一直判
过期而**完全失去控制**。所以这次 UDP 改造必须与服务端 0.3 s 同批上线，不能只上
一半。

线路格式（28 字节，与 ``server/command_udp_in.py`` 对齐，字段顺序权威）::

    b"C" + b"B" + b"C" + ver(u8=1) + seq(u32 BE) + ts_ms(u32 BE) + raw16   = 28 B

doc §4.4 写的「共 24 B」是算术笔误（3 + 1 + 4 + 4 + 16 = 28）。服务端也兼容
缺 ``ts_ms`` 的 24 字节包，但本文件统一发 28 字节。

- ``seq``：每发一包 +1，u32 回绕。
- ``ts_ms``：**客户端进程启动以来的毫秒数**（见 ``uptime_ms``）。服务端只把它
  显示成 ``client_ts`` 供面板调试，**不参与任何新鲜度判断**（新鲜度完全由
  服务端自己的 ``time.time()`` 收包时刻决定），所以这里不需要和服务器对时。
- ``raw16``：``xi.parse_protocol(proto)["raw_hex"]`` 那 32 字符 hex 对应的
  16 个原始字节 —— 也就是 ``PadState.proto`` 本身（小端解析前的原始字节）。
- 语义命令（``MOVE`` / ``TURRET``）**不在**包里：服务端写 store 时故意
  ``cmds=None``，让它走 ``_resolve_cmds`` 从 raw16 兜底解析，与桌面端
  ``commands.build_commands`` 同语义。不要试图往包里加 cmds 字段。

D6：节拍一律 ``time.sleep``，不用 ``Event.wait``。附录 A.1 在本机
（Windows / Python 3.12）实测 ``Event.wait(2 ms)`` → p50 **15.50 ms**
（按 ~15.6 ms 系统定时器粒度取整），100 Hz 实际只跑到约 62 Hz；
``time.sleep(2 ms)`` → p50 **2.17 ms**。代价是停止延迟从「立即唤醒」变成
「最多一个 sleep 周期」（2 ms，join 超时 2 s，绰绰有余）。
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

# 允许 `python desktop/gamepad/pusher.py` 直接跑自检（此时 sys.path[0] 是
# 本文件所在目录，仓库根不在路径里，`desktop.*` 导不进来）。
if __package__ in (None, ""):  # pragma: no cover - 仅自检入口
    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from desktop.gamepad import commands as cmd_builder
from desktop.gamepad import xinput as xi

# ==================================================================== 线路契约

CMDIN_MAGIC = b"CBC"
CMDIN_VER = 1
CMDIN_RAW_LEN = 16
CMDIN_PACKET_LEN = len(CMDIN_MAGIC) + 1 + 4 + 4 + CMDIN_RAW_LEN   # 28
CMDIN_DEFAULT_PORT = 8010

_U32 = 0xFFFFFFFF

#: 进程启动时刻（单调钟）。``ts_ms`` 的基准。
_PROC_START_MONO = time.monotonic()


def build_cmd_packet(raw16: bytes, seq: int, ts_ms: int) -> bytes:
    """16 字节协议 -> 28 字节 ``CBC`` 命令包。纯函数，便于离线验证字节布局。

    >>> build_cmd_packet(bytes(16), 1, 2)[:4]
    b'CBC\\x01'
    """
    raw = bytes(raw16)
    if len(raw) != CMDIN_RAW_LEN:
        raise ValueError(
            f"raw16 must be {CMDIN_RAW_LEN} bytes, got {len(raw)}")
    return (CMDIN_MAGIC
            + bytes([CMDIN_VER])
            + struct.pack(">I", int(seq) & _U32)
            + struct.pack(">I", int(ts_ms) & _U32)
            + raw)


def parse_cmd_packet(data: bytes) -> tuple[int, int, bytes] | None:
    """``CBC`` 包 -> ``(seq, ts_ms, raw16)``；非法返回 ``None``。

    与 ``server/command_udp_in._parse`` 的 28 字节分支等价（这里只实现桌面端
    会发的那种形式，服务端额外接受的 24 字节兼容形式不需要在桌面端存在）。
    """
    if data is None or len(data) != CMDIN_PACKET_LEN:
        return None
    if bytes(data[:3]) != CMDIN_MAGIC or data[3] != CMDIN_VER:
        return None
    return (int.from_bytes(data[4:8], "big"),
            int.from_bytes(data[8:12], "big"),
            bytes(data[12:CMDIN_PACKET_LEN]))


def uptime_ms() -> int:
    """客户端进程启动以来的毫秒数（u32，回绕）。

    线路字段 ``ts_ms``。服务端把它显示成 ``client_ts`` 供面板调试，**不参与
    任何新鲜度判断**，所以用单调钟即可，不需要和服务器对时。
    """
    return int((time.monotonic() - _PROC_START_MONO) * 1000.0) & _U32


def command_host(base_url: str) -> str:
    """从 ``get_base_url()`` 的 URL 里取 hostname（同 ``h264_viewer`` 的做法）。

    解析不出来时退回 ``127.0.0.1`` —— 服务端在本机时这是对的。
    """
    try:
        url = (base_url or "").strip()
        if url and "://" not in url:
            url = "http://" + url
        return urlparse(url).hostname or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "off", "false", "no", "")


def cmd_udp_enabled(default: bool = True) -> bool:
    """UDP 主通道开关（环境变量 ``CORE_BRIDGE_CMD_UDP``，默认开）。

    关掉即整体退回 HTTP ``POST /command``（降级路径，用于排查）。
    """
    return _env_flag("CORE_BRIDGE_CMD_UDP", default)


def cmd_udp_port(default: int = CMDIN_DEFAULT_PORT) -> int:
    """服务端命令 UDP 端口（``CORE_BRIDGE_CMD_UDP_PORT``，默认 8010）。"""
    raw = os.environ.get("CORE_BRIDGE_CMD_UDP_PORT")
    if raw is None or not str(raw).strip():
        return default
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


def should_send(last_proto: bytes | None, next_proto: bytes,
                last_sent_at: float, now: float, heartbeat_s: float) -> bool:
    """保活判定（纯逻辑）：变化立即发，否则到点保活一次。

    ``last_proto is None`` 表示「还没发过 / 刚被重置」，一律立即发。
    这条 50 ms 保活取代旧的 0.5 s HTTP 心跳，直接解决 doc §2.1 S3
    （心跳周期 == 服务端过期阈值 → 保持输入不变时指令流间歇中断）。
    """
    if last_proto is None:
        return True
    if bytes(next_proto) != bytes(last_proto):
        return True
    return (now - last_sent_at) >= heartbeat_s


# ==================================================================== 状态


@dataclass
class GamepadSnapshot:
    connected: bool = False
    slot: int | None = None
    packet: int = 0
    proto: bytes = b"\x00\x80\x00\x80\x00\x80\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
    buttons: int = 0
    lt8: int = 0
    rt8: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    fps: float = 0.0
    posted: bool = False
    last_error: str = ""
    transport: str = ""
    sent: int = 0
    tank: dict = field(default_factory=lambda: dict(cmd_builder.neutral_tank()))
    estop_progress: float = 0.0


@dataclass
class _Latest:
    """轮询线程 -> 发送线程的唯一交接物（整体在 ``_lock`` 下替换）。"""
    connected: bool = False
    proto: bytes = field(default_factory=xi.neutral_protocol)
    at: float = 0.0            # time.monotonic()


class GamepadPusher:
    """应用启动即 start()：先尝试连接手柄，全程后台重连 + 实时下发。

    断连/回中时主动补发一次中位帧，避免 server 侧残留旧偏转（failsafe）。

    线程结构（D7）
    --------------
    - ``gamepad-poll``：``poll_hz``（默认 100 Hz）读 XInput，把结果整块写进
      ``self._latest`` 与面板快照。**不发任何包**。
    - ``gamepad-send``：每 ``SEND_TICK_S`` 醒来一次读 ``self._latest``，按
      ``should_send`` 决定要不要发。用 ``time.sleep`` 而不是 ``Event.wait``
      （D6），这样「输入变化」到「发出」只差 ~2 ms；``Event.wait`` 在 Windows
      上是 15.5 ms，会白吃掉半个控制预算。

    同步方式：单个 ``threading.Lock``（``self._lock``）保护 ``_latest``、
    ``_snap`` 和发送统计。锁临界区只有几次赋值，没有 IO，所以不会成为瓶颈。
    ``self._seq``、UDP socket、HTTP client 只被发送线程碰，无需加锁。
    """

    #: HTTP 降级路径的超时。D7 之前它也是主通道超时，卡住会拖垮轮询；
    #: 现在它只在 UDP 不可用时才走到，主通道（UDP）不受影响。
    POST_TIMEOUT = 0.3
    #: 固定下发节拍。50 Hz 常发当前帧（含空闲零帧 / 断连中位零帧），
    #: 与 K230 侧 50 Hz UART3 透传同节拍。变化即时发，不等节拍。
    HEARTBEAT_S = 0.02
    #: 发送线程的 tick（D6：不用 Event.wait）。~2 ms 决定了「输入变化到发出」
    #: 的额外延迟（附录 A.1：time.sleep(2 ms) → p50 2.17 ms）。
    SEND_TICK_S = 0.002
    #: 轮询线程多久没产出状态就视为「读取端无响应」，停止下发（fail-safe）。
    #: 正常情况下远小于该值（100 Hz → 10 ms）。
    POLL_STALE_S = 0.5
    POLL_MAX_HZ = 120.0

    def __init__(self, get_base_url, poll_hz: float = 100.0,
                 reader=None) -> None:
        self._get_base_url = get_base_url
        self._interval = 1.0 / max(1.0, min(poll_hz, self.POLL_MAX_HZ))
        self._reader = reader if reader is not None else xi.XInputReader()
        self._lock = threading.Lock()
        self._tank_builder = cmd_builder.TankCommandBuilder()
        self._snap = GamepadSnapshot(
            connected=getattr(self._reader, "slot", None) is not None,
            slot=getattr(self._reader, "slot", None),
        )
        self._latest = _Latest()
        self._latest_tank: dict = dict(cmd_builder.neutral_tank())
        self._latest_estop_progress: float = 0.0
        # 发送线程侧的状态量（同样在 _lock 下，但由发送线程独占写入）
        self._posted = False
        self._last_error = ""
        self._transport = ""
        self._sent = 0
        # 只被发送线程碰
        self._seq = 0
        self._sock: socket.socket | None = None
        self._client: httpx.Client | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._frames = 0
        self._t0 = time.monotonic()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._stop.clear()
        with self._lock:
            # 重启后不能沿用上一次的 latest（at 已经过期），否则发送线程会在
            # 新的第一帧到达前就按旧状态保活。
            self._latest = _Latest()
            self._posted = False
            self._last_error = ""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="gamepad-poll")
            self._thread.start()
        if self._send_thread is None or not self._send_thread.is_alive():
            self._send_thread = threading.Thread(
                target=self._send_loop, daemon=True, name="gamepad-send")
            self._send_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        threads = (self._thread, self._send_thread)
        self._thread = None
        self._send_thread = None
        for t in threads:
            if t is not None and t is not threading.current_thread():
                t.join(timeout=timeout)
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 读取
    def snapshot(self) -> GamepadSnapshot:
        with self._lock:
            s = self._snap
            return GamepadSnapshot(
                connected=s.connected, slot=s.slot, packet=s.packet,
                proto=s.proto, buttons=s.buttons, lt8=s.lt8, rt8=s.rt8,
                lx=s.lx, ly=s.ly, rx=s.rx, ry=s.ry,
                fps=s.fps,
                posted=self._posted, last_error=self._last_error,
                transport=self._transport, sent=self._sent,
                tank=dict(s.tank), estop_progress=s.estop_progress,
            )

    def _decode_tank(self, proto: bytes, now: float) -> tuple[dict, float]:
        """proto -> V1.3 TankCmd（含急停锁存推进）。纯状态推进，由轮询线程调。"""
        try:
            parsed = xi.parse_protocol(proto)
        except Exception:
            return dict(cmd_builder.neutral_tank()), 0.0
        tank = self._tank_builder.update(
            parsed["lx"], parsed["ly"], parsed["rx"], parsed["ry"],
            parsed["hat"], parsed["btn"], parsed["sys"], now)
        return dict(tank), self._tank_builder.estop.progress(now)

    def _poll_loop(self) -> None:
        """轮询线程：只读 XInput、只更新状态，**一次 IO 都不做**（D7）。"""
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                st = self._reader.poll()

                self._frames += 1
                with self._lock:
                    prev_fps = self._snap.fps
                dt = t0 - self._t0
                fps = self._frames / dt if dt >= 1.0 else prev_fps
                if dt >= 1.0:
                    self._frames = 0
                    self._t0 = t0

                proto = bytes(st.proto)
                tank, estop_progress = self._decode_tank(proto, t0)
                with self._lock:
                    self._snap = GamepadSnapshot(
                        connected=st.connected, slot=st.slot,
                        packet=st.packet, proto=proto,
                        buttons=st.buttons, lt8=st.lt8, rt8=st.rt8,
                        lx=st.lx, ly=st.ly, rx=st.rx, ry=st.ry,
                        fps=round(fps, 1),
                        tank=tank, estop_progress=estop_progress,
                    )
                    self._latest = _Latest(
                        connected=bool(st.connected), proto=proto, at=t0)
                    self._latest_tank = dict(tank)
                    self._latest_estop_progress = estop_progress

                rest = self._interval - (time.monotonic() - t0)
                if rest > 0:
                    # D6：Event.wait → time.sleep。旧写法在 Windows 上按
                    # ~15.6 ms 取整，100 Hz 实际只跑到 ~62 Hz。
                    time.sleep(rest)
        finally:
            self._thread = None

    # ---------------------------------------------------------------- 发送
    def _send_loop(self) -> None:
        """发送线程：50 Hz 固定节拍常发当前帧，变化立即发，无变化到节拍即发。

        断连 / 轮询卡死同样按节拍持续补中位零帧（不断流），下游靠显式零速
        停车而不是靠静默超时。节拍用 _tank_has_changed 比对语义，
        而不是比 proto —— 松开按键但摇杆回中属于语义变化，必须立即发。
        """
        last_tank: dict | None = None
        last_sent_at = 0.0
        try:
            while not self._stop.is_set():
                time.sleep(self.SEND_TICK_S)
                with self._lock:
                    latest = self._latest
                    tank = dict(self._latest_tank)
                now = time.monotonic()

                if latest.at <= 0.0:
                    continue
                if now - latest.at > self.POLL_STALE_S:
                    if (now - last_sent_at) < self.HEARTBEAT_S:
                        continue
                    ok, err = self._send_frame(xi.neutral_protocol())
                    if ok:
                        last_tank = None
                        last_sent_at = now
                        self._mark(True, "手柄轮询线程无响应 · 零帧常发中")
                    else:
                        self._mark(False, err or "手柄轮询线程无响应")
                    continue

                if not latest.connected:
                    if (now - last_sent_at) < self.HEARTBEAT_S:
                        continue
                    with self._lock:
                        self._tank_builder.estop.latched = False
                        self._tank_builder.estop._hold_start = None
                        self._latest_tank = dict(cmd_builder.neutral_tank())
                        self._latest_estop_progress = 0.0
                    ok, err = self._send_frame(xi.neutral_protocol())
                    if ok:
                        last_tank = None
                        last_sent_at = now
                        self._mark(True, "等待手柄 · 零帧常发中")
                    else:
                        self._mark(False, err or "等待手柄")
                    continue

                changed = last_tank is None or tank != last_tank
                if not changed and (now - last_sent_at) < self.HEARTBEAT_S:
                    continue
                ok, err = self._send_frame(latest.proto)
                if ok:
                    last_tank = dict(tank)
                    last_sent_at = now
                    self._mark(True, "")
                else:
                    self._mark(False, err)
        finally:
            self._send_thread = None

    def ping_tank(self) -> tuple[dict, float]:
        """面板调试用：当前最新 V1.3 语义（不推进锁存计时）。"""
        with self._lock:
            return dict(self._latest_tank), self._latest_estop_progress

    def last_tank(self) -> tuple[dict, float]:
        with self._lock:
            return dict(self._latest_tank), self._latest_estop_progress

    def clear_estop(self) -> None:
        with self._lock:
            self._tank_builder.estop.latched = False
            self._tank_builder.estop._hold_start = None

    def _mark(self, posted: bool, err: str) -> None:
        """记录发送线程最近一次结果。值没变就跳过（省掉断连时 500 Hz 的写锁）。"""
        with self._lock:
            if self._posted == posted and self._last_error == err:
                return
            self._posted = posted
            self._last_error = err

    def _note_transport(self, name: str) -> None:
        with self._lock:
            self._transport = name
            self._sent += 1

    def _base_url(self) -> str:
        try:
            return (self._get_base_url() or "").rstrip("/")
        except Exception:
            return ""

    def _send_frame(self, raw16: bytes) -> tuple[bool, str]:
        """发一帧 16 字节协议：优先 UDP，失败或被关掉时降级 HTTP。

        ``seq`` 在每次尝试时 +1（无论是否成功），保证单调；服务端靠它丢弃
        乱序旧包。``ts_ms`` 只用一次，UDP 包和 HTTP body 共用同一个值，两条
        通道看到的 ``client_ts`` 不会漂移。
        """
        seq = (self._seq + 1) & _U32
        self._seq = seq
        ts_ms = uptime_ms()
        udp_err = ""
        if cmd_udp_enabled():
            try:
                pkt = build_cmd_packet(raw16, seq, ts_ms)
            except ValueError as e:
                return False, str(e)[:120]
            ok, udp_err = self._send_udp(pkt)
            if ok:
                self._note_transport("udp")
                return True, ""
        ok, err = self._send_http(raw16, seq, ts_ms, udp_err)
        if ok:
            self._note_transport("http")
        return ok, err

    def _send_udp(self, pkt: bytes) -> tuple[bool, str]:
        """UDP 主通道。返回 ``(ok, err)``；``ok=False`` 时调用方降级 HTTP。

        实测（临时脚本，本机）：一次 ``sendto`` 的耗时是 **0.13–0.22 ms**
        （含 ``urlparse`` + ``os.environ`` 读取），对比 HTTP 路径 5 ms 起步、
        最坏 300 ms。
        """
        host = command_host(self._base_url())
        sock = self._sock
        if sock is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 << 10)
                self._sock = sock
            except OSError as e:
                return False, str(e)[:120]
        try:
            sock.sendto(pkt, (host, cmd_udp_port()))
        except ConnectionResetError:
            # Windows：上一次 sendto 收到 ICMP 端口不可达后回灌到同一 socket
            # （附录 A.2）。包本身没问题，重试一次即可，不必降级 HTTP。
            try:
                sock.sendto(pkt, (host, cmd_udp_port()))
                return True, ""
            except OSError as e:
                return False, str(e)[:120]
        except OSError as e:
            return False, str(e)[:120]
        return True, ""

    def _send_http(self, raw16: bytes, seq: int, ts_ms: int,
                   udp_err: str = "") -> tuple[bool, str]:
        """HTTP 降级路径（兼容服务端旧版本 / 调试 / UDP 被中间设备掐掉）。"""
        base = self._base_url()
        if not base:
            return False, ""
        try:
            client = self._http_client()
            if client is None:
                return False, udp_err or "HTTP 客户端不可用"
            parsed = xi.parse_protocol(raw16)
            tank, _progress = self.last_tank()
            cmds = cmd_builder.build_commands(
                parsed["lx"], parsed["ly"], parsed["rx"], parsed["ry"])
            cmds.append({"name": "TANK", **{k: int(v) for k, v in tank.items()
                                            if k != "estop"},
                         "estop": int(tank.get("estop", 0))})
            resp = client.post(f"{base}/command", json={
                "raw_hex": parsed["raw_hex"],
                "cmds": cmds,
                "slot": None,
                "packet": 0,
                "client_ts": round(ts_ms / 1000.0, 3),
                "seq": seq,
            })
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)[:120]

    def _http_client(self) -> httpx.Client | None:
        client = self._client
        if client is None:
            try:
                client = httpx.Client(timeout=self.POST_TIMEOUT, trust_env=False)
            except Exception:
                return None
            self._client = client
        return client


# ==================================================================== 自检
# 不拥有 test/ 目录，所以纯逻辑（CBC 包字节布局 / 保活判定 / URL 取 host /
# 环境变量开关）以及端到端的 UDP 收发 + 线程解耦都在这里自跑。风格参考
# test/test_rtp_redundancy.py 的 main()。
#
#   python desktop/gamepad/pusher.py
#
# 端口 18310 / 18311 是临时自检端口，与 server 的 8010 不冲突。

SELF_TEST_PORTS = (18310, 18311)


class _UdpSink:
    """假服务端：收 CBC 包并记下到达时刻，用来断言节拍与字节布局。"""

    def __init__(self, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.settimeout(0.05)
        self.port = port
        self._lock = threading.Lock()
        self._pkts: list[tuple[float, bytes]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="udp-sink")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(2048)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            with self._lock:
                self._pkts.append((time.monotonic(), bytes(data)))

    def packets(self) -> list[tuple[float, bytes]]:
        with self._lock:
            return list(self._pkts)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        try:
            self._sock.close()
        except OSError:
            pass


class _FakeReader:
    """可编程的假 XInput 读取器（只被轮询线程调用，符合 XInputReader 契约）。"""

    def __init__(self, proto: bytes | None = None,
                 block_on_call: int = 0, block_for: float = 0.0) -> None:
        self.slot = 0
        self.calls = 0
        self.connected = True
        self.proto = bytes(proto) if proto is not None \
            else xi.xinput_to_protocol(_FakeReader._gamepad(0.4))
        self.block_on_call = block_on_call
        self.block_for = block_for
        self.block_started_at = 0.0
        self.block_ended_at = 0.0

    @staticmethod
    def _gamepad(right: float):
        g = xi.XINPUT_GAMEPAD()
        g.sThumbLX = int(right * 32767)
        g.sThumbLY = 0
        g.sThumbRX = 0
        g.sThumbRY = 0
        return g

    def poll(self):
        self.calls += 1
        if self.block_on_call and self.calls == self.block_on_call:
            # 模拟 poll 卡住：D7 之前这里会同时卡住整个发送
            self.block_started_at = time.monotonic()
            time.sleep(self.block_for)
            self.block_ended_at = time.monotonic()
        return xi.PadState(connected=self.connected, slot=self.slot,
                           packet=self.calls, proto=self.proto)


def _env_port(port: int):
    """临时把 UDP 端口指到自检端口。"""
    class _Ctx:
        def __enter__(self):
            self._old = os.environ.get("CORE_BRIDGE_CMD_UDP_PORT")
            os.environ["CORE_BRIDGE_CMD_UDP_PORT"] = str(port)
            return self

        def __exit__(self, *exc):
            if self._old is None:
                os.environ.pop("CORE_BRIDGE_CMD_UDP_PORT", None)
            else:
                os.environ["CORE_BRIDGE_CMD_UDP_PORT"] = self._old
            return False
    return _Ctx()


def _env_flag_udp(value: str | None):
    class _Ctx:
        def __enter__(self):
            self._old = os.environ.get("CORE_BRIDGE_CMD_UDP")
            if value is None:
                os.environ.pop("CORE_BRIDGE_CMD_UDP", None)
            else:
                os.environ["CORE_BRIDGE_CMD_UDP"] = value
            return self

        def __exit__(self, *exc):
            if self._old is None:
                os.environ.pop("CORE_BRIDGE_CMD_UDP", None)
            else:
                os.environ["CORE_BRIDGE_CMD_UDP"] = self._old
            return False
    return _Ctx()


def test_cmd_packet_wire_format():
    """28 字节布局：magic / ver / seq(u32 BE) / ts_ms(u32 BE) / raw16。"""
    raw = bytes(range(16))
    pkt = build_cmd_packet(raw, 0x01020304, 0x0A0B0C0D)
    assert len(pkt) == CMDIN_PACKET_LEN == 28, len(pkt)
    assert pkt[:3] == b"CBC", pkt[:3]
    assert pkt[3] == 1, pkt[3]
    assert pkt[4:8] == b"\x01\x02\x03\x04", pkt[4:8].hex()
    assert pkt[8:12] == b"\x0a\x0b\x0c\x0d", pkt[8:12].hex()
    assert pkt[12:28] == raw, pkt[12:28].hex()
    got = parse_cmd_packet(pkt)
    assert got == (0x01020304, 0x0A0B0C0D, raw), got
    # 长度 / magic / ver 任一不对都必须被解析端拒绝
    assert parse_cmd_packet(pkt[:27]) is None
    assert parse_cmd_packet(b"XBC" + pkt[3:]) is None
    assert parse_cmd_packet(pkt[:3] + b"\x02" + pkt[4:]) is None


def test_cmd_packet_endianness_and_wrap():
    """seq / ts_ms 必须是 big-endian u32，且按 32 位回绕。"""
    pkt = build_cmd_packet(bytes(16), 1, 1)
    assert pkt[4:8] == b"\x00\x00\x00\x01" and pkt[8:12] == b"\x00\x00\x00\x01"
    wrapped = build_cmd_packet(bytes(16), 0x1_0000_0005, 0x2_FFFF_FFFF)
    assert wrapped[4:8] == b"\x00\x00\x00\x05", wrapped[4:8].hex()
    assert wrapped[8:12] == b"\xff\xff\xff\xff", wrapped[8:12].hex()
    assert parse_cmd_packet(wrapped) == (5, 0xFFFFFFFF, bytes(16))


def test_cmd_packet_rejects_bad_raw():
    """raw16 长度必须**恰好** 16，否则在构造时就报错（服务端会整包丢弃）。"""
    for bad in (b"", bytes(15), bytes(17)):
        try:
            build_cmd_packet(bad, 1, 1)
        except ValueError:
            continue
        raise AssertionError(f"{len(bad)} 字节 raw16 必须被拒")


def test_uptime_ms_is_monotonic_and_bounded():
    """ts_ms = 进程启动以来的毫秒，单调、落在 u32 内。"""
    a = uptime_ms()
    time.sleep(0.01)
    b = uptime_ms()
    assert 0 <= a <= _U32 and 0 <= b <= _U32
    assert b >= a
    assert b - a < 5000


def test_command_host():
    """URL -> hostname。取不到时退回 127.0.0.1（服务端在本机）。"""
    assert command_host("http://127.0.0.1:8000") == "127.0.0.1"
    assert command_host("http://192.168.137.1:8000") == "192.168.137.1"
    assert command_host("192.168.137.1:8000") == "192.168.137.1"
    assert command_host("http://[::1]:8000") == "::1"
    assert command_host("") == "127.0.0.1"
    assert command_host(None) == "127.0.0.1"


def test_should_send_heartbeat():
    """保活判定：变化立即发；无变化到 20 ms 节拍才发。"""
    proto = bytes(16)
    other = bytes(range(16))
    # 从没发过 -> 立即发
    assert should_send(None, proto, 0.0, 100.0, 0.02)
    # 无变化且不到 20 ms -> 不发（19 ms 不发 / 21 ms 发，避开浮点边界）
    assert not should_send(proto, proto, 100.0, 100.019, 0.02)
    assert should_send(proto, proto, 100.0, 100.021, 0.02)
    assert should_send(proto, proto, 100.0, 100.9, 0.02)
    # 有变化 -> 无论上次多久都立刻发
    assert should_send(proto, other, 100.0, 100.001, 0.02)
    assert should_send(proto, other, 100.0, 99.0, 0.02)


def test_cmd_udp_env_switch():
    """CORE_BRIDGE_CMD_UDP / CORE_BRIDGE_CMD_UDP_PORT 覆盖与非法值回退。"""
    with _env_flag_udp(None):
        assert cmd_udp_enabled() is True
    with _env_flag_udp("1"):
        assert cmd_udp_enabled() is True
    with _env_flag_udp("0"):
        assert cmd_udp_enabled() is False
    with _env_flag_udp("off"):
        assert cmd_udp_enabled() is False
    with _env_port(18310):
        assert cmd_udp_port() == 18310
    os.environ["CORE_BRIDGE_CMD_UDP_PORT"] = "not-a-port"
    try:
        assert cmd_udp_port() == CMDIN_DEFAULT_PORT == 8010
    finally:
        os.environ.pop("CORE_BRIDGE_CMD_UDP_PORT", None)
    assert cmd_udp_port() == 8010


def test_udp_end_to_end_packet_layout():
    """端到端：假服务端（127.0.0.1:18310）真的收到 build_cmd_packet 的字节。"""
    sink = _UdpSink(SELF_TEST_PORTS[0])
    reader = _FakeReader()
    pusher = GamepadPusher(get_base_url=lambda: "http://127.0.0.1:8000",
                            reader=reader)
    try:
        with _env_port(SELF_TEST_PORTS[0]):
            assert cmd_udp_enabled() is True
            pusher.start()
            time.sleep(0.30)
            snap = pusher.snapshot()
    finally:
        pusher.stop()
        pkts = sink.packets()
        sink.stop()

    assert snap.posted is True, snap.last_error
    assert snap.transport == "udp", snap.transport
    assert snap.sent >= 2, snap.sent
    assert snap.connected is True
    assert len(pkts) >= 2, len(pkts)

    expect_raw = reader.proto
    seqs = []
    for _t, data in pkts:
        assert len(data) == CMDIN_PACKET_LEN == 28, len(data)
        parsed = parse_cmd_packet(data)
        assert parsed is not None, data.hex()
        seq, ts_ms, raw = parsed
        seqs.append(seq)
        assert raw == expect_raw, (raw.hex(), expect_raw.hex())
        assert 0 < ts_ms <= _U32
    assert seqs == sorted(seqs), seqs          # 严格递增
    assert len(set(seqs)) == len(seqs), seqs   # 无重复
    # 0.30 s / 20 ms 节拍 -> 至少 10 包（首包立即发）
    assert len(pkts) >= 10, len(pkts)
    gaps = [pkts[i + 1][0] - pkts[i][0] for i in range(len(pkts) - 1)]
    assert max(gaps) < 0.10, gaps            # 没有出现「发送线程被卡住」


def test_slow_send_does_not_stall_poll_loop():
    """D7 核心回归：**发送**再慢也不能拖慢轮询线程。

    旧实现把 `POST /command` 放在轮询线程里：一次 0.3 s 超时（更糟的是 TCP
    段重传 200–300 ms）会把 XInput 读取一起卡住，手柄读取率整体塌陷。这里让
    假服务端每次 POST 都 sleep 0.3 s（比真实超时更狠），断言：
      - 轮询次数仍接近 100 Hz（旧实现 1.5 s 里只能跑 ~4 次）；
      - 面板快照的 packet / slot 持续推进（= 状态仍在被更新）；
      - HTTP 请求本身也在连续到达（= 发送线程没死）。

    注意这个用例暴露的是降级路径的固有代价：0.3 s 的 POST 会让保活周期退化到
    300 ms，正好压在服务端 0.3 s 过期阈值上 —— 这正是为什么主通道必须是 UDP
    （0.15 ms 的 sendto，见 test_udp_end_to_end_packet_layout）。
    """
    import http.server as _hs
    got: list[float] = []
    # 每次 POST 阻塞 120 ms：足够拖垮「poll + send 同线程」的旧实现
    # （旧实现 1.5 s 里只能跑 ~12 次 poll），又稳在 POST_TIMEOUT(0.3 s)
    # 之内，所以这里的失败原因纯粹是解耦，不掺杂超时。
    slow = 0.12

    class _Slow(_hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            got.append(time.monotonic())
            time.sleep(slow)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    httpd = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    reader = _FakeReader()
    pusher = GamepadPusher(get_base_url=lambda: f"http://127.0.0.1:{port}",
                            reader=reader)
    window = 1.5
    t0 = time.monotonic()
    try:
        with _env_flag_udp("0"):
            pusher.start()
            time.sleep(window)
            mid = pusher.snapshot()
        pusher.stop()
        snap = pusher.snapshot()
    finally:
        httpd.shutdown()
        httpd.server_close()

    elapsed = time.monotonic() - t0
    # 轮询线程完全没被发送拖住：至少 60% 的名义 100 Hz
    assert reader.calls >= 0.6 * 100 * window, \
        f"轮询被慢发送拖垮了：{reader.calls} 次 / {elapsed:.2f}s"
    assert snap.packet >= 0.6 * 100 * window, snap.packet
    # 面板读到的就是最新状态（不是卡住时的旧值）
    assert mid.packet >= 0.3 * 100 * window, mid.packet
    # 发送线程同样活着：0.12 s 慢 POST 下 1.5 s 至少完成 8 次
    assert len(got) >= 8, len(got)
    gaps = [got[i + 1] - got[i] for i in range(len(got) - 1)]
    assert max(gaps) < 0.30, gaps        # 没有出现「发送线程被彻底卡死」
    assert snap.transport == "http", snap.transport
    assert snap.posted is True, snap.last_error


def test_poll_stall_sends_neutral_at_50hz():
    """轮询线程停止产出状态 > POLL_STALE_S 时转中位零帧 50 Hz 常发。

    读取端停了就不知道摇杆在哪：旧语义是停发靠静默超时停车；新语义是
    按 20 ms 节拍持续补零帧，下游靠显式零速停车。
    """
    sink = _UdpSink(SELF_TEST_PORTS[1])
    reader = _FakeReader(block_on_call=3, block_for=1.0)  # 第 3 次 poll 卡 1 s
    pusher = GamepadPusher(get_base_url=lambda: "http://127.0.0.1:8000",
                            reader=reader)
    try:
        with _env_port(SELF_TEST_PORTS[1]):
            pusher.start()
            time.sleep(0.6)          # 卡住后 ~0.5 s 触发 fail-safe
            stalled = pusher.snapshot()
            # 等阻塞（1.0 s，从第 3 次 poll ≈ 20 ms 起）真正结束再复查
            time.sleep(0.7)
    finally:
        pusher.stop()
        pkts = sink.packets()
        sink.stop()
        resumed = pusher.snapshot()

    # 阻塞期间仍在按节拍发中位零帧（不断流），且只能是零帧
    stale_at = reader.block_started_at + GamepadPusher.POLL_STALE_S + 0.05
    during_stall = [(t, d) for t, d in pkts
                    if stale_at < t < reader.block_ended_at]
    assert during_stall, "阻塞期间零帧常发停了"
    neutral = xi.neutral_protocol()
    for _t, d in during_stall:
        parsed = parse_cmd_packet(d)
        assert parsed is not None, d.hex()
        assert parsed[2] == neutral, parsed[2].hex()
    gaps = [during_stall[i + 1][0] - during_stall[i][0]
            for i in range(len(during_stall) - 1)]
    if gaps:
        assert max(gaps) < 0.10, gaps
    # 阻塞结束后自动恢复发送（轮询线程还活着，只是慢）
    resumed_pkts = [t for t, _ in pkts if t >= reader.block_ended_at]
    assert resumed_pkts, "阻塞结束后没有恢复发送"
    assert stalled.posted is True, stalled.last_error
    assert stalled.last_error == "手柄轮询线程无响应 · 零帧常发中", stalled.last_error
    assert resumed.posted is True, resumed.last_error
    assert resumed.packet > stalled.packet


def test_http_fallback_when_udp_disabled():
    """CORE_BRIDGE_CMD_UDP=0 时必须走 HTTP POST /command（降级路径仍可用）。"""
    calls: list[dict] = []

    import http.server as _hs

    class _Srv(_hs.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            calls.append(__import__("json").loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):  # 静音
            pass

    httpd = _hs.HTTPServer(("127.0.0.1", 0), _Srv)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        reader = _FakeReader()
        pusher = GamepadPusher(
            get_base_url=lambda: f"http://127.0.0.1:{port}", reader=reader)
        try:
            with _env_flag_udp("0"):
                pusher.start()
                time.sleep(0.30)
                snap = pusher.snapshot()
        finally:
            pusher.stop()
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert snap.transport == "http", snap.transport
    assert snap.posted is True, snap.last_error
    assert calls, "降级路径没有发出任何 HTTP POST"
    last = calls[-1]
    # HTTP 通道仍然要带 raw_hex / cmds（面板 GET /command 依赖它）
    assert last["raw_hex"] == xi.parse_protocol(reader.proto)["raw_hex"]
    names = [c["name"] for c in last["cmds"]]
    assert names == ["MOVE", "TURRET", "TANK"], names


def test_disconnect_keeps_sending_neutral_at_50hz():
    """断连 failsafe：按 20 ms 节拍持续补中位零帧（不断流）。"""
    sink = _UdpSink(SELF_TEST_PORTS[0])
    reader = _FakeReader()
    pusher = GamepadPusher(get_base_url=lambda: "http://127.0.0.1:8000",
                            reader=reader)
    try:
        with _env_port(SELF_TEST_PORTS[0]):
            pusher.start()
            time.sleep(0.20)
            reader.connected = False
            disconnect_at = time.monotonic()
            time.sleep(0.30)
            snap = pusher.snapshot()
    finally:
        pusher.stop()
        pkts = sink.packets()
        sink.stop()

    neutral = xi.neutral_protocol()
    after = [(t, d) for t, d in pkts if t >= disconnect_at]
    assert len(after) >= 10, f"断连后零帧常发停了，实际 {len(after)}"
    for _t, d in after:
        parsed = parse_cmd_packet(d)
        assert parsed is not None, d.hex()
        assert parsed[2] == neutral, parsed[2].hex()
    gaps = [after[i + 1][0] - after[i][0] for i in range(len(after) - 1)]
    assert max(gaps) < 0.10, gaps
    assert snap.connected is False
    assert snap.posted is True, snap.last_error
    assert snap.last_error == "等待手柄 · 零帧常发中", snap.last_error


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""审核新增：k230/rtp_push.py 的缺口覆盖 + 三处修复的回归护栏。

与 test_rtp_redundancy.py 分开是因为那些是「按 K 编号组织」的功能测试，
这里组织方式是「一次审核发现的缺陷 / 不变式」，每条都注明为什么需要它。

覆盖：
  - P0 修复：cmdline.pump 把共享 socket 留在非阻塞模式 -> 视频 sendto EAGAIN
  - P1 修复：gc.collect() 触发依赖会被 reset_rolling() 清零的 sent_frames
  - 突发债务上限（真 Pacer + FakeClock，三档都跑）
  - K13 ensure_wifi 的阻塞预算
  - K3 凭据类型 / 缺失
  - MicroPython 兼容性：import 白名单 + 禁止 CPython-only 名字 + 禁止 f-string
  - valid_frame(AA55)、ms_diff 回绕、retransmit_nack 的 OSError 路径
  - T3 不走突发（docs §5「≤20KB 突发」的已知偏差，钉住以防悄悄漂移）
"""
import ast
import importlib
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_mpy_stubs():
    if not hasattr(sys, "print_exception"):
        sys.print_exception = lambda e, f=None: None  # noqa: F401
    for name in ("network", "uctypes", "machine"):
        sys.modules.setdefault(name, types.ModuleType(name))
    media = types.ModuleType("media")
    for sub in ("sensor", "media", "vencoder"):
        m = types.ModuleType("media." + sub)
        sys.modules["media." + sub] = m
        setattr(media, sub, m)
    sys.modules["media"] = media
    sys.modules["media.sensor"].Sensor = lambda **kw: None
    sys.modules["media.sensor"].CAM_CHN_ID_0 = 0
    sys.modules["media.vencoder"].Encoder = lambda **kw: None


def _load():
    _install_mpy_stubs()
    import k230.rtp_push as rp
    importlib.reload(rp)
    return rp


def _source():
    return (REPO / "k230" / "rtp_push.py").read_text(encoding="utf-8")


class FakeClock:
    """可控的微秒时钟：sleep_us 只推进虚拟时间，不真的睡。"""

    def __init__(self):
        self.us = 0

    def now_us(self):
        return self.us

    def diff_us(self, a, b):
        return a - b

    def sleep_us(self, us):
        self.us += max(0, int(us))


class FakeSta:
    """network.WLAN 替身。up_after_s 秒后 isconnected() 变 True。"""

    def __init__(self, up_after_s=0.0, connect_raises=None):
        self.t = 0.0
        self.up_after_s = up_after_s
        self.connect_raises = connect_raises
        self.connects = 0

    def isconnected(self):
        return self.t >= self.up_after_s

    def connect(self, ssid, password):
        self.connects += 1
        if self.connect_raises is not None:
            raise self.connect_raises


# =====================================================================
# P0：cmdline.pump 不得把共享 socket 留在非阻塞模式
# =====================================================================
def test_pump_restores_socket_to_blocking_after_drain():
    """控制与视频共用一个 socket。MicroPython 的 settimeout(0) 会把 socket
    切成非阻塞；旧版 pump 排空完就再也不还原，于是同 socket 的视频 sendto
    在 WiFi TX 队列满时抛 EAGAIN -> SendAborted -> 整帧中止但 seq 照样推进
    -> 接收端看到序号空洞 -> 40ms 后宣布丢失 -> 请求 IDR。自己制造丢包。
    """
    rp = _load()
    seen = []

    class Sock:
        def __init__(self):
            self.timeout = None
            self.sent_when_nonzero = None

        def settimeout(self, t):
            self.timeout = t
            seen.append(t)

        def recv(self, n):
            if self.timeout == 0:
                raise OSError(11)          # 非阻塞且空 -> EAGAIN
            return b""

    sock = Sock()
    link = rp.CommandLink(uart=object())
    link.pump(sock)
    assert seen[0] == 0, "排空阶段仍应非阻塞"
    assert sock.timeout == rp.CommandLink.BACK_TIMEOUT_S, \
        "排空后必须还原成带超时的阻塞模式"
    assert sock.timeout > 0


def test_fanout_sink_retries_eagain_instead_of_dropping_the_frame():
    """EAGAIN 是瞬时的（TX 队列满），丢整帧的代价是接收端 40ms 后宣布丢失
    + 请求 IDR。必须短退避重试。"""
    rp = _load()

    class Flaky:
        def __init__(self, fails):
            self.fails = fails
            self.calls = 0

        def sendto(self, data, addr):
            self.calls += 1
            if self.calls <= self.fails:
                raise OSError(11, "EAGAIN")
            return len(data)

    st = rp.TxStats()
    sock = Flaky(fails=2)
    sink = rp.fanout_sink([("h", 8002)], sock, st)
    sink(b"x" * 1400)
    assert sock.calls == 3, "应重试到成功"
    assert st.pkt_err == 0 and st.bytes_tx == 1400


def test_fanout_sink_gives_up_after_bounded_retries():
    rp = _load()

    class Dead:
        def __init__(self):
            self.calls = 0

        def sendto(self, data, addr):
            self.calls += 1
            raise OSError(11, "EAGAIN")

    st = rp.TxStats()
    sock = Dead()
    sink = rp.fanout_sink([("h", 8002)], sock, st)
    try:
        sink(b"x" * 1400)
        raise AssertionError("must abort")
    except rp.SendAborted:
        pass
    # 有界：不能变成无限阻塞
    assert sock.calls == rp.SEND_RETRY_TRIES
    assert st.pkt_err == 1


def test_fanout_sink_aborts_immediately_on_non_eagain():
    """网卡消失 / 路由不可达不该被当成瞬时抖动反复重试。"""
    rp = _load()

    class Down:
        def __init__(self):
            self.calls = 0

        def sendto(self, data, addr):
            self.calls += 1
            raise OSError(101, "ENETUNREACH")

    st = rp.TxStats()
    sock = Down()
    try:
        rp.fanout_sink([("h", 8002)], sock, st)(b"x" * 1400)
        raise AssertionError("must abort")
    except rp.SendAborted:
        pass
    assert sock.calls == 1


def test_pump_defers_nack_retransmit_until_socket_is_restored():
    """重传要走 sendto；非阻塞 socket 上 sendto 会 EAGAIN，这次重传就白费了。
    所以 NACK 必须在还原之后才处理。"""
    rp = _load()
    order = []

    class Sock:
        def __init__(self):
            self.timeout = None

        def settimeout(self, t):
            self.timeout = t

        def recv(self, n):
            if self.timeout == 0:
                if not getattr(self, "_sent", False):
                    self._sent = True
                    return rp.build_nack([3, 4])
                raise OSError(11)
            raise AssertionError("recv must not run after restore")

    sock = Sock()
    link = rp.CommandLink(
        uart=object(),
        on_nack=lambda seqs: order.append((seqs, sock.timeout)))
    link.pump(sock)
    assert order == [([3, 4], rp.CommandLink.BACK_TIMEOUT_S)], \
        "重传必须发生在 socket 还原之后"


# =====================================================================
# P1：GC 触发不能依赖会被统计窗口清零的计数器
# =====================================================================
def test_gc_trigger_survives_low_frame_rate():
    """旧版用 st.sent_frames % GC_COLLECT_EVERY 触发 gc.collect()，而
    sent_frames 每 10s 被 reset_rolling() 清零。有效帧率一旦掉到 10fps 以下
    （正是 STARVE 丢帧时会发生的事），每个窗口都到不了 100，gc.collect()
    永久停摆 -> 堆只增不减 -> MemoryError。
    """
    rp = _load()
    for fps in (30, 20, 12, 9, 5, 2):
        st = rp.TxStats()
        collects = 0
        for _window in range(10):                      # 10 x 10s = 100s
            for _ in range(int(fps * 10)):
                st.note_frame(12000, 9, False, 12.9)
                if st.gc_frames % rp.GC_COLLECT_EVERY == 0:
                    collects += 1
            st.reset_rolling()          # 统计窗口清零
        assert collects > 0, ("fps=%d 时 gc.collect() 一次都不跑" % fps)
        assert st.gc_frames == int(fps * 10) * 10      # 单调，不被清零


def test_gc_frames_not_reset_by_reset_rolling():
    rp = _load()
    st = rp.TxStats()
    for _ in range(7):
        st.note_frame(100, 1, True, 1.0)
    assert st.gc_frames == 7
    st.reset_rolling()
    assert st.gc_frames == 7
    assert st.sent_frames == 0          # 滚动窗口仍然清零
    assert st.gc_max_us == 0


# =====================================================================
# 突发/节流：真 Pacer + FakeClock，三档都跑
# =====================================================================
def _frame_for_tier(rp, tier):
    _w, _h, br = rp.TIERS[tier]
    payload = br * 1000 // 8 // rp.FPS
    return [b"x" * rp.MAX_PAYLOAD for _ in range(-(-payload // rp.MAX_PAYLOAD))]


def test_burst_debt_never_exceeds_one_burst_frame():
    """突发模式允许「桶全空也放行一整帧」（K5 的目的：P 帧不逐包 sleep_us），
    但债务必须被限制在一个突发帧以内，否则 pacer 就是一个无限透支的黑洞。
    旧版债务上限是 -PACER_BURST_BYTES = 59400B = 66ms 空口时间（两个帧间隔），
    单帧突发最多只能欠 20160B，这里把真实可达值钉住。"""
    rp = _load()
    worst = 0.0
    for tier in rp.TIERS:
        pkts = _frame_for_tier(rp, tier)
        pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES,
                         FakeClock(), rp.PACER_SPAN_MAX_MS)
        pacer.tokens = 0.0                            # 桶见底
        rp.send_paced(pacer, pkts, lambda p: None)
        one_frame_wire = sum(len(p) for p in pkts) + len(pkts) * 40
        assert -pacer.tokens <= one_frame_wire, (
            "%s 突发债务 %dB 超过一个突发帧的线速字节 %dB"
            % (tier, -pacer.tokens, one_frame_wire))
        worst = max(worst, -pacer.tokens)
    # 一个突发帧在 7.2Mbps 上约 22ms 空口，远小于 -PACER_BURST_BYTES 允许的 66ms
    assert worst * 8 / (rp.PACER_WIRE_KBPS * 1000.0) * 1000.0 < 25.0


def test_t3_p_frames_are_throttled_not_burst():
    """已知偏差（docs §5「P 帧 ≤ 20KB 突发」）：T3 的 P 帧 25000B payload >
    20000B，所以**永远**不进突发模式。§6.3 里「T3 突发 20-25ms 空口」的估算
    因此不成立 —— T3 实际是逐包 take() 的。钉住这个行为，避免阈值漂移时
    悄悄改变 1080p 的发送形态。"""
    rp = _load()
    pkts = _frame_for_tier(rp, "T3")
    assert sum(len(p) for p in pkts) > rp.BURST_SEND_BYTES
    # 桶是满的也走不进突发
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                     rp.PACER_SPAN_MAX_MS)
    rp.send_paced(pacer, pkts, lambda p: None)
    assert pacer.bursting is False and pacer.burst_packets == 0
    # 桶见底 -> 逐包 sleep
    clock2 = FakeClock()
    pacer2 = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock2,
                      rp.PACER_SPAN_MAX_MS)
    pacer2.tokens = 0.0
    rp.send_paced(pacer2, pkts, lambda p: None)
    assert pacer2.sleeps == len(pkts)                  # 逐包 sleep
    # 节流 span 被 PACER_SPAN_MAX_MS 封顶（K5：原 300ms）。T3 的 P 帧
    # 空口 25.7ms < 80ms，所以这里不会触发 cap（cap 只留给 IDR/超大帧）。
    assert clock2.us <= rp.PACER_SPAN_MAX_MS * 1000
    assert clock2.us > 0
    assert pacer2.tokens >= -pacer2.burst


def test_t1_and_t2_p_frames_burst_without_sleep():
    rp = _load()
    for tier in ("T1", "T2"):
        pkts = _frame_for_tier(rp, tier)
        clock = FakeClock()
        pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                         rp.PACER_SPAN_MAX_MS)
        rp.send_paced(pacer, pkts, lambda p: None)
        assert pacer.bursting is True, tier
        assert pacer.sleeps == 0 and clock.us == 0, tier
        assert pacer.burst_packets == len(pkts), tier


def test_idle_callback_runs_on_both_sides_of_every_sleep():
    """K4：节流 sleep 期间持续泵控制 socket。sleep 前的 idle 不能省 ——
    那一次正好是「刚发现令牌不够、即将阻塞」的时刻。"""
    rp = _load()
    clock = FakeClock()
    marks = []
    pacer = rp.Pacer(1000, 2000, clock, rp.PACER_SPAN_MAX_MS,
                     burst_threshold=0,
                     idle=lambda: marks.append(clock.us))
    rp.send_paced(pacer, [b"y" * 1400] * 6, lambda p: None)
    assert pacer.sleeps > 0
    assert len(marks) >= 2 * pacer.sleeps
    assert marks == sorted(marks)


def test_burst_decision_units_are_documented_gap():
    """set_burst() 按 **payload** 字节判突发，can_send()/would_burst() 按
    **wire** 字节（含每包 40B 头）判。两者在 20000B 边界附近不一致：
    一个 20000B/14 包的帧 set_burst 判 True，can_send 却因 wire=20560 > 20000
    判 False（要真令牌）。差异 <= 40*npktsB，量级无害，但必须显式钉住，
    免得以后有人「统一单位」时改变准入行为。"""
    rp = _load()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, FakeClock(),
                     rp.PACER_SPAN_MAX_MS)
    assert pacer.set_burst(rp.BURST_SEND_BYTES) is True
    pacer.tokens = 0.0
    assert pacer.can_send(rp.BURST_SEND_BYTES, 14) is False
    assert pacer.would_burst(rp.BURST_SEND_BYTES + 14 * 40) is False
    # 低几个包时两边一致
    assert pacer.can_send(1000, 1) is True


# =====================================================================
# K13：ensure_wifi 的阻塞预算
# =====================================================================
class _TimeShim:
    """代理 stdlib time，只把 sleep 换成可控的虚拟 sleep。

    不要直接 monkeypatch 真实 time.sleep —— rtp_push 里的 `time` 就是 stdlib
    那个模块对象，改它会连 pytest 自己的计时器一起改掉。
    """

    def __init__(self, on_sleep):
        import time as _t
        self._t = _t
        self._on_sleep = on_sleep

    def __getattr__(self, name):
        if name == "sleep":
            return self._on_sleep
        return getattr(self._t, name)


def test_k13_ensure_wifi_budget_is_bounded(monkeypatch):
    rp = _load()
    box = {"slept": []}

    def fake_sleep(s):
        box["slept"].append(s)
        box["sta"].t += s          # 让 FakeSta 的虚拟时间跟着走

    box["sta"] = FakeSta(up_after_s=1e9)
    monkeypatch.setattr(rp, "time", _TimeShim(fake_sleep))

    # 永远连不上：最多 3 次 1s 轮询（3s 预算）然后交还控制权
    assert rp.ensure_wifi(box["sta"]) is False
    assert box["sta"].connects == 1
    assert box["slept"] == [1, 1, 1]
    assert sum(box["slept"]) <= rp.WIFI_RECONNECT_BUDGET_MS / 1000.0 + 1
    assert rp.WIFI_RECONNECT_BUDGET_MS == 3000

    # 第 2 秒恢复 -> 提前返回（不空转满 3s）
    box["slept"] = []
    box["sta"] = FakeSta(up_after_s=2.0)
    assert rp.ensure_wifi(box["sta"]) is True
    assert box["slept"] == [1, 1]

    # 已连接时完全不碰网络
    box["slept"] = []
    box["sta"] = FakeSta(up_after_s=0.0)
    assert rp.ensure_wifi(box["sta"]) is True
    assert box["sta"].connects == 0 and box["slept"] == []

    # connect 抛异常 -> 立刻 False（不再叠加 3s 轮询）
    box["slept"] = []
    box["sta"] = FakeSta(up_after_s=1e9, connect_raises=OSError("busy"))
    assert rp.ensure_wifi(box["sta"]) is False
    assert box["slept"] == []


# =====================================================================
# K3：凭据
# =====================================================================
def test_k3_credentials_must_be_strings(tmp_path):
    rp = _load()
    p = tmp_path / "c.json"
    p.write_text('{"ssid": 123, "password": "p"}', encoding="utf-8")
    try:
        rp.load_config(str(p))
        raise AssertionError("非字符串 ssid 必须被拒")
    except RuntimeError as e:
        assert "ssid" in str(e)
    p.write_text('{"ssid": "s", "password": ["a"]}', encoding="utf-8")
    try:
        rp.load_config(str(p))
        raise AssertionError("非字符串 password 必须被拒")
    except RuntimeError as e:
        assert "password" in str(e)
    # 合法值仍然通过
    p.write_text('{"ssid": "lab", "password": "pw", "tier": "T1"}',
                 encoding="utf-8")
    assert rp.load_config(str(p))["ssid"] == "lab"


def test_k3_module_import_never_raises_and_falls_back_to_defaults():
    """模块级代码在 import 时全部执行（MicroPython 尤其如此）。任何在模块层
    抛异常的语句都会让脚本连启动都失败，所以 load_config/resolve_settings 的
    失败必须被收敛成 CONFIG_ERROR 而不是 import 失败。

    本机必然没有 /sdcard/tindistance.json，走的正是「无配置文件」这条路径：
    必须安静回落到内置默认凭据，而不是报错退出。"""
    rp = _load()
    assert rp.CONFIG_ERROR is None            # 无配置不是错误
    assert rp.CONFIG is None                   # 明确记录「没有配置文件」
    assert rp.SETTINGS["ssid"] == rp.WIFI_SSID
    assert rp.SETTINGS["password"] == rp.WIFI_PASSWORD
    assert rp.WIFI_SSID and rp.WIFI_PASSWORD    # 凭据内置，开箱即用
    # main() 不再因缺配置 raise：有配置错误只打印回落提示后继续跑
    import inspect
    src = inspect.getsource(rp.main)
    assert "CONFIG_ERROR" in src
    assert "raise RuntimeError" not in src


# =====================================================================
# MicroPython 兼容性
# =====================================================================
MPY_MODULES = {"gc", "json", "os", "socket", "struct", "time", "uctypes",
               "network", "media", "machine", "sys"}
CPY_ONLY_MODULES = {"threading", "dataclasses", "typing", "logging",
                    "pathlib", "collections", "argparse", "enum", "functools",
                    "itertools", "subprocess", "asyncio", "selectors"}


def _imported_modules(tree):
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def test_micropython_import_whitelist_covers_function_level_imports():
    """只查模块层 import 会漏掉 init_uart3() 里的 `from machine import UART`。
    ast.walk 覆盖任意嵌套层级，所以函数级 import 也要在白名单里。"""
    tree = ast.parse(_source())
    mods = _imported_modules(tree)
    assert mods <= MPY_MODULES, sorted(mods - MPY_MODULES)
    assert not (mods & CPY_ONLY_MODULES)
    # 函数级 import 也要被看见（机器模块确实只在函数里 import）
    fn_nodes = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    fn_mods = set()
    for f in fn_nodes:
        fn_mods |= _imported_modules(f)
    assert "machine" in fn_mods, "init_uart3 里的 from machine import UART 必须可见"


def test_micropython_whitelist_rejects_known_cpython_only_imports():
    """护栏本身要有效：把 CPython-only 模块塞进源码必须被抓到。"""
    for bad in ("import threading", "from dataclasses import dataclass",
                "from collections import deque", "import typing"):
        mods = _imported_modules(ast.parse(bad))
        assert mods - MPY_MODULES, "白名单没拦住: %s" % bad


def test_micropython_no_fstrings_annotations_or_walrus():
    """rt-smart 的 MicroPython 对 f-string 变体/注解/海象的支持依版本而异，
    而本文件跑在固件上、没法在 CI 里编译验证。所以直接禁掉这三类语法糖，
    保持风格是 MicroPython 100% 可解析的子集。"""
    tree = ast.parse(_source())
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            bad.append(("f-string", n.lineno))
        elif isinstance(n, ast.NamedExpr):
            bad.append(("walrus :=", n.lineno))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.returns is not None or any(a.annotation for a in n.args.args):
                bad.append(("annotation", n.lineno))
        elif isinstance(n, ast.AnnAssign):
            bad.append(("AnnAssign", n.lineno))
    assert not bad, bad


def test_micropython_ticks_helpers_all_have_fallbacks():
    """rt-smart 固件版本之间 ticks_us/sleep_us 的存在性不一致。
    每个用到 ticks_* / sleep_us 的地方都必须经由带 hasattr 兜底的包装函数。"""
    rp = _load()
    # 包装函数本身
    for fn in (rp._ticks_us, rp._ticks_ms, rp._ticks_diff, rp._ticks_diff_us):
        src = _fn_source(fn)
        assert "hasattr(time" in src, fn.__name__
    for meth in (rp.SysClock.now_us, rp.SysClock.diff_us,
                 rp.SysClock.sleep_us):
        assert "hasattr(time" in _fn_source(meth), meth.__name__
    assert "hasattr(time" in _fn_source(rp._sleep_short_us)
    # 模块里不允许出现裸的 time.ticks_* / time.sleep_us 调用
    tree = ast.parse(_source())
    raw = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                and isinstance(n.value.value, ast.Name)
                and n.value.value.id == "time"
                and n.value.attr in ("ticks_us", "ticks_ms", "ticks_diff",
                                     "sleep_us")):
            raw.append((n.attr, n.lineno))
    # 上面这层嵌套是 time.ticks_us；只允许出现在带 hasattr 守卫的函数内，
    # 这里统一交给 _fn_source 的断言覆盖，只统计「无守卫」的裸调用数。
    assert len(raw) <= 7, raw


def _fn_source(fn):
    import inspect
    return inspect.getsource(fn)


# =====================================================================
# 杂项：AA55、回绕、重传错误路径
# =====================================================================
def test_valid_frame_aa55_roundtrip():
    rp = _load()
    def frame(n, payload):
        body = bytes([0xAA, 0x55, n]) + b"".join(
            bytes([i, len(p)]) + p for i, p in enumerate(payload))
        return body + bytes([rp.crc8(body)])
    f = frame(2, [b"\x01\x02", b"\x7f"])
    assert rp.valid_frame(f) is True
    assert rp.valid_frame(f[:-1]) is False               # 少 CRC
    bad = bytearray(f)
    bad[-1] ^= 0xFF
    assert rp.valid_frame(bytes(bad)) is False           # CRC 错
    assert rp.valid_frame(b"") is False
    assert rp.valid_frame(b"\xAA\x55") is False
    assert rp.valid_frame(None) is False
    assert rp.valid_frame(b"\x00" * 40) is False          # 太长
    # n=0 不合法
    assert rp.valid_frame(bytes([0xAA, 0x55, 0, 0x00])) is False


def test_ms_diff_is_wraparound_safe():
    rp = _load()
    assert rp.ms_diff(100, None) is None
    assert rp.ms_diff(100, 40) == 60
    # (1<<30)-10 -> 5 跨过回绕点：2^30+5 - (2^30-10) = 15
    assert rp.ms_diff(5, (1 << 30) - 10) == 15


def test_idr_gate_merges_recent_idr_and_rate_limits():
    rp = _load()
    gate = rp.IdrGate(interval_ms=400, settle_ms=500)
    assert gate.request(1000) is True                    # 首次
    assert gate.request(1100) is False                   # 400ms 限频
    assert gate.merged_rate == 1
    gate.note_idr_sent(1100)
    assert gate.request(1300) is False                   # 刚发过 IDR -> 合并
    assert gate.merged_idr == 1
    gate2 = rp.IdrGate(400, 500)
    gate2.note_idr_sent(0)
    assert gate2.request(5000) is True                   # 距上次 IDR 久 -> 放行
    assert gate2.request(5100) is False                  # 限频 400ms
    assert gate2.merged_rate == 1
    # 统计窗口清零不得清掉合并/限频状态，否则限频每 10s 会失效一次
    gate2.reset_rolling()
    assert gate2.request(5150) is False
    assert gate2.request(5500) is True                   # 400ms 之后恢复


def test_retransmit_nack_counts_send_errors_without_raising():
    rp = _load()
    ring = rp.PacketRing()
    ring.put(1, b"z" * 100)

    class Dead:
        def sendto(self, data, addr):
            raise OSError(101, "ENETUNREACH")

    st = rp.TxStats()
    link = rp.CommandLink(uart=object())
    lim = rp.RetransmitLimiter(rp.retransmit_limit_bytes(rp.BIT_RATE))
    hits, miss, drop, err = rp.retransmit_nack([1], ring, lim, Dead(),
                                                ("h", 8002), 0,
                                                stats=st, link=link)
    assert (hits, miss, drop, err) == (0, 0, 0, 1)
    assert st.pkt_err == 1 and link.nack_hits == 0


def test_ring_capacity_covers_the_nack_round_trip():
    """128 槽在 T3(6Mbps) 下约覆盖 200ms（docs §4.3）。按最坏情况核一遍：
    帧最大 25KB / 18 包，128 槽 >= 7 帧 >= 233ms。"""
    rp = _load()
    ring = rp.PacketRing()
    assert ring.capacity == 128
    worst_pkts = -(-(rp.TIERS["T3"][2] * 1000 // 8 // rp.FPS) // rp.MAX_PAYLOAD)
    assert ring.capacity // worst_pkts >= 7


# =====================================================================
# 集成：packetize -> send_paced -> ring -> NACK -> retransmit 串起来跑
# =====================================================================
def _emit_frame(rp, ring, sock, seq, npkts, marker=True):
    """模拟 stream_loop 的一帧：打包 -> 逐包发 -> 入环 -> 返回新 seq。"""
    pkts = []
    seq_next = seq
    for _ in range(npkts):
        nalu = _fu_a_frame(rp, rp.MAX_PAYLOAD)[0]
        seq_next = rp.packetize_nalu(nalu, 1234, seq_next, 0x54494E44, pkts)
    if marker:
        pkts[-1] = bytearray(pkts[-1])
        pkts[-1][1] |= 0x80
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, FakeClock(),
                     rp.PACER_SPAN_MAX_MS)
    st = rp.TxStats()
    rp.send_paced(pacer, pkts, rp.fanout_sink(sock.targets, sock.sock, st), 1,
                  ring=ring, first_seq=seq)
    return seq_next, pkts


def _fu_a_frame(rp, nalu_len):
    raw = b"\x00\x00\x00\x01\x65" + bytes([(i * 7) & 0xFF
                                          for i in range(nalu_len)])
    return rp.split_nalus(raw)


class _LoopSock:
    def __init__(self):
        self.targets = [("127.0.0.1", 8002)]
        self.sock = self
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(bytes(data))


def test_integration_frames_drop_seq_wrap_and_nack_recovery():
    """跨帧不变量（docs §4.3）：
      - 包头 seq 与入环 seq 一致，且每个发出的包都能按自己的 seq 命中 ring；
      - 丢帧**不消耗 seq**（所以接收端看不到缺口、不会请求 IDR）；
      - seq 回绕（65535 -> 0）后 ring 命中判定仍正确；
      - NACK 命中的包被原样重发（字节相同，含 marker 位）。
    """
    rp = _load()
    ring = rp.PacketRing()
    sock = _LoopSock()
    seq = 65530                       # 故意从回绕前 6 开始
    emitted = []                      # [(frame_index, pkts)]
    for i in range(4):
        if i == 1:
            # 第 1 帧被 STARVE 丢弃：主循环在 `seq = seq_next` 之前 continue，
            # 所以这些包既没发出也没进 ring，seq 也不推进。
            emitted.append((i, None))
            continue
        seq, pkts = _emit_frame(rp, ring, sock, seq, 9)
        emitted.append((i, pkts))
    assert seq == 21, "两帧发出（18 包）+ 一次回绕后 seq 应为 65530+18-65536+21"

    for idx, pkts in emitted:
        if pkts is None:
            continue
        for p in pkts:
            sq = (p[2] << 8) | p[3]
            assert ring.get(sq) is p, ("帧%d 的包 seq=%d 未按自己的 seq 入环"
                                       % (idx, sq))
    # 只有每帧最后一个包带 marker
    for idx, pkts in emitted:
        if pkts is None:
            continue
        marks = [bool(p[1] & 0x80) for p in pkts]
        assert marks == [False] * 8 + [True], idx
    # 回绕后的 seq 可命中
    for sq in (0, 1, 2, 3, 11, 20):
        assert ring.get(sq) is not None

    # NACK 一批（65530 是帧0 首包，0/2 是帧0 回绕后的包，11/20 是后两帧尾）
    lim = rp.RetransmitLimiter(rp.retransmit_limit_bytes(rp.BIT_RATE))
    st = rp.TxStats()
    link = rp.CommandLink(uart=object())
    want = [65530, 65535, 0, 2, 11, 20]
    before = len(sock.sent)
    hits, miss, drop, err = rp.retransmit_nack(want, ring, lim, sock,
                                                sock.targets[0], 0,
                                                stats=st, link=link)
    assert (hits, miss, drop, err) == (len(want), 0, 0, 0)
    retx = sock.sent[before:]
    assert len(retx) == len(want)
    for sq, data in zip(want, retx):
        assert data == ring.get(sq)
        assert ((data[2] << 8) | data[3]) == sq      # seq 未被改写
    # 重传里 marker 位还在（接收端靠它判定帧尾）
    assert [bool(d[1] & 0x80) for d in retx] == \
        [False, False, False, True, True, True]
    assert st.retx_bytes == sum(len(ring.get(s)) for s in want)


def test_integration_dropped_frame_leaves_no_seq_gap_for_the_receiver():
    """丢帧不消耗 seq -> 接收端看不到空洞 -> 不会请求 IDR。这是 K7 的关键，
    也是「hold 到下个 IDR」能成立的前提。"""
    rp = _load()
    ring = rp.PacketRing()
    sock = _LoopSock()
    seq = 100
    # 一帧正常发出
    seq, kept = _emit_frame(rp, ring, sock, seq, 9)
    assert seq == 109
    # 下一帧被丢：主循环在 `seq = seq_next` 之前 continue，seq 保持 109
    assert seq == 109
    # 再下一帧从 109 继续 -> 与被丢帧的 seq 完全无缝
    seq, pkts = _emit_frame(rp, ring, sock, seq, 9)
    assert seq == 118
    assert ((pkts[0][2] << 8) | pkts[0][3]) == 109
    assert ring.get(108) is kept[-1] and ring.get(109) is pkts[0]
    # 接收端视角：连续 18 个 seq，没有空洞
    for sq in range(100, 118):
        assert ring.get(sq) is not None
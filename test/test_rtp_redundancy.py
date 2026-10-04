# -*- coding: utf-8 -*-
"""K230 RTP 纯逻辑仿真 + relay 多端口扇出集成测试。

不依赖 K230 硬件：micropython 模块用 stub 替身；relay 用 localhost 真 socket。
覆盖：单链路准入、pacer（突发/节流/span 记账）、NACK 环形缓冲与重传限流、
RTP 时间戳、配置与档位（K1/K2/K3/K4/K5/K6/K7/K9/K10/K12）。
"""
import ast
import os
import socket
import struct
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------- micropython stubs ----------
def _install_mpy_stubs():
    if "k230.rtp_push" in sys.modules:
        return
    if not hasattr(sys, "print_exception"):
        sys.print_exception = lambda e, f=None: None  # noqa: F401
    for name in ("network", "uctypes", "machine"):
        sys.modules.setdefault(name, types.ModuleType(name))
    media = types.ModuleType("media")
    for sub in ("sensor", "media", "vencoder"):
        m = types.ModuleType(f"media.{sub}")
        sys.modules[f"media.{sub}"] = m
        setattr(media, sub, m)
    sys.modules["media"] = media
    sensor = sys.modules["media.sensor"]
    sensor.Sensor = lambda **kw: (_ for _ in ()).throw(RuntimeError("stub"))
    sensor.CAM_CHN_ID_0 = 0
    venc = sys.modules["media.vencoder"]
    venc.Encoder = lambda **kw: (_ for _ in ()).throw(RuntimeError("stub"))
    venc.CHN_STR = None


def _load_k230():
    import importlib
    _install_mpy_stubs()
    import k230.rtp_push as rp
    importlib.reload(rp)
    return rp


class FakeClock:
    def __init__(self):
        self.us = 0

    def now_us(self):
        return self.us

    def diff_us(self, a, b):
        return a - b

    def sleep_us(self, us):
        self.us += max(0, int(us))


class FakePacer:
    """**只用于 frame_admission 的准入测试**的极简替身。

    它刻意不实现节流（take 恒返回 0、token 不消耗），所以任何依赖节流语义的
    测试都必须用真 Pacer + FakeClock，不要用这个替身 —— 否则测的是一个假的。
    set_burst 的阈值从构造参数取，和真 Pacer 的 burst_threshold 同源。
    """

    def __init__(self, stock, burst_threshold=20000):
        self.tokens = float(stock)
        self.burst = float(stock)
        self.calls = []
        self.burst_threshold = burst_threshold
        self.bursting = False

    def refill(self):
        return self.tokens

    def begin_frame(self):
        pass

    def set_burst(self, frame_wire_bytes):
        self.bursting = frame_wire_bytes <= self.burst_threshold
        return self.bursting

    def can_send(self, frame_bytes, npkts):
        return self.tokens >= frame_bytes + npkts * 40

    def take(self, nbytes):
        self.calls.append(nbytes)
        return 0


def test_frame_admission_copies_adaptive():
    rp = _load_k230()
    # 拷贝阶梯要能在**默认档位**上同时验到 2 拷和 1 拷两条分支，所以第一个用例
    # 刻意用比标称帧小的 payload：默认档是 T3/10 Mbps（见
    # test_bandwidth_budget_math），标称帧 25000B 的单拷 airtime 已经是 20.6ms，
    # 2 拷 41.2ms 直接超 31.35ms 预算 —— 拿它验「能发 2 拷」会失败，但那不代表
    # 阶梯坏了，只代表这个档位下 2 拷本来就贵。
    #
    # 13000B/10 包：单拷 = (13000 + 10*40)*8/10000 = 10.72ms
    # 预算 = FRAME_INTERVAL_MS(33) * MAX_PFRAME_DUTY(0.95) = 31.35ms
    # 2 拷 = 21.44ms <= 31.35 -> 发 2 拷；3 拷 = 32.16ms 超预算。
    action, copies, air = rp.frame_admission(
        13000, 10, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND
    assert copies == 2
    # airtime_ms 的单位就是 ms，返回值是**所有拷贝之和**：
    # 13400*8/10000 = 10.72ms/拷，2 拷 = 21.44ms
    assert abs(air - 2 * (13000 + 10 * 40) * 8.0 / 10000) < 0.01

    # 默认档（T3）的标称帧：25000B/18 包 -> 单拷 = (25000+18*40)*8/10000
    # = 20.58ms，2 拷 41.2ms 超预算 -> 只发 1 拷。
    # 这条是 T3 默认档的**预期行为**，不是回归：它说明 1080p 在 10 Mbps 上已经
    # 没有多拷贝冗余的余量，一次丢包就是一整个 1080p 帧。
    nominal = rp.BIT_RATE * 1000 // 8 // rp.FPS
    nominal_pkts = -(-nominal // rp.MAX_PAYLOAD)
    action, copies, air = rp.frame_admission(
        nominal, nominal_pkts, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 1

    # 巨帧连 1 拷都超 duty -> 丢（duty 方向，仍需 IDR 重同步）
    action, copies, air = rp.frame_admission(
        200_000, 170, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_DROP_DUTY and copies == 0

    # I 帧直发（IDR_COPIES 拷，不看 duty 预算）
    action, copies, air = rp.frame_admission(
        200_000, 170, True, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 2

    # 生产默认：单链路单拷贝（K1）—— 拷贝数不再 >1
    action, copies, _ = rp.frame_admission(
        13000, 10, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None,
        max_copies=rp.LINK_COPIES, idr_copies=rp.IDR_COPIES)
    assert rp.LINK_COPIES == 1 and rp.IDR_COPIES == 1
    assert (action, copies) == (rp.FRAME_SEND, 1)


def test_frame_admission_starve_steps_down_copies():
    rp = _load_k230()
    # 令牌只够 1 拷（12500 + 9*40 = 12860）：2 拷要 25720，拿不到 -> 降到 1 拷
    p = FakePacer(stock=12500 + 9 * 40)
    action, copies, _ = rp.frame_admission(
        12500, 9, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, p, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 1

    # 令牌连 1 拷都不够 -> STARVE（K7：不再请求 IDR，只丢到下一个 IDR）
    p = FakePacer(stock=100)
    action, copies, _ = rp.frame_admission(
        12500, 9, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, p, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_DROP_STARVE and copies == 0


def test_send_paced_bills_copies():
    rp = _load_k230()
    # 用**真** Pacer + FakeClock（而不是 FakePacer）：这里同时验证了
    # 「copies×包长计费」和「突发模式下不 sleep」，用 FakePacer 只会测到一个
    # 恒返回 0 的假 take。
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                     rp.PACER_SPAN_MAX_MS)
    pkts = [b"x" * 100, b"y" * 50]
    sent = []

    def sink(pkt):
        sent.append(pkt)

    n = rp.send_paced(pacer, pkts, sink, copies=3)
    assert n == 2 and len(sent) == 2
    # 令牌按 copies×包长计费。注意 take() 只加**一次** 40B 头开销（不是
    # copies 次），所以多拷时每包少记 40×(copies-1)B —— copies=1 的生产
    # 配置下无影响，但这里如实钉住当前算术，别让测试替实现圆场。
    assert pacer.wire_bytes == (100 * 3 + 40) + (50 * 3 + 40)
    # 突发模式（整帧线速 450B ≤ 20000）不应 sleep
    assert pacer.sleeps == 0 and pacer.wait_us == 0
    assert clock.us == 0


def test_fanout_sink_sends_all_ports():
    rp = _load_k230()
    tx = []

    class FakeSock:
        def sendto(self, data, addr):
            tx.append(addr)

    st = rp.TxStats()
    sink = rp.fanout_sink([("h", 8002), ("h", 8003), ("h", 8004)],
                          FakeSock(), st)
    sink(b"1234")
    assert tx == [("h", 8002), ("h", 8003), ("h", 8004)]
    assert st.bytes_tx == 4 * 3
    assert st.pkt_err == 0


def test_fanout_sink_aborts_on_oserror():
    rp = _load_k230()

    class DeadSock:
        def sendto(self, data, addr):
            raise OSError("down")

    st = rp.TxStats()
    sink = rp.fanout_sink([("h", 8002), ("h", 8003)], DeadSock(), st)
    try:
        sink(b"1234")
    except rp.SendAborted:
        pass
    else:
        raise AssertionError("must abort")
    assert st.pkt_err == 1


def test_bandwidth_budget_math():
    rp = _load_k230()
    # 单链路预算：节流池 = 实测可用带宽 × duty（K6，不再是硬编码 15Mbps）
    wire_kbps = rp.PACER_WIRE_KBPS
    assert wire_kbps == int(rp.WIFI_LINK_KBPS * rp.PACER_DUTY)
    assert (rp.WIFI_LINK_KBPS, rp.PACER_DUTY, wire_kbps) == (10000, 0.9, 9000)
    assert rp.PACER_SPAN_MAX_MS == 80          # K5：原 300ms
    assert rp.GOP_LEN == 60                    # K5：长 GOP
    assert rp.BIT_RATE == 6000                 # 默认档 T3

    p_payload = rp.BIT_RATE * 1000 / 8 // rp.FPS   # 25000B
    npkts = -(-p_payload // rp.MAX_PAYLOAD)        # 18
    single_air = rp.airtime_ms(p_payload, npkts, rp.WIFI_LINK_KBPS)
    assert abs(single_air - 20.58) < 0.01
    assert single_air * rp.LINK_COPIES <= rp.FRAME_INTERVAL_MS * rp.MAX_PFRAME_DUTY

    # 文档 §1.1 的线上速率公式：码率 ×(1 + 40/载荷)× 1.1 ≤ 可用带宽的 70%
    online = rp.BIT_RATE * (1 + 40.0 / rp.MAX_PAYLOAD) * 1.1
    assert online <= 0.7 * rp.WIFI_LINK_KBPS
    # 默认档 T3 在默认 10 Mbps 上只剩 3% 余量（6788.6 / 7000）。把这条钉住是
    # 为了让「改 BIT_RATE / payload / WIFI_LINK_KBPS 任一个」时余量被吃掉这件事
    # 必须显式改测试，而不是悄悄把 §1.1 的预算表撑破。
    assert online > 0.9 * 0.7 * rp.WIFI_LINK_KBPS, "默认档带宽余量已不足 10%"


# ---------- K2：按时钟丢帧的闸门必须不存在 ----------
def _rtp_source():
    return (REPO / "k230" / "rtp_push.py").read_text(encoding="utf-8")


def _names(node):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}


def test_k7_starve_hold_freeze_is_bounded():
    rp = _load_k230()
    # K7：STARVE 后不再立刻请求 IDR（切断丢帧→IDR→更拥塞的正反馈）
    assert rp.starve_hold_should_request_idr(0) is False
    assert rp.starve_hold_should_request_idr(rp.STARVE_HOLD_MAX_MS) is False
    # 但冻结必须有上界：GOP=60 时「丢到下个 IDR」最坏黑屏 2s
    assert rp.starve_hold_should_request_idr(rp.STARVE_HOLD_MAX_MS + 1) is True
    assert rp.STARVE_HOLD_MAX_MS == 500
    # 结构性断言（不是源码字符串计数）：need_resync = True 必须**只**出现在
    # FRAME_DROP_DUTY 分支里。STARVE 分支一旦也置位，「丢帧→IDR→更拥塞」的
    # 正反馈就回来了，而字符串计数在重构（改名/换写法）时会误报或漏报。
    tree = ast.parse(_rtp_source())
    sets = [n for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "need_resync"
            and isinstance(n.value, ast.Constant) and n.value.value is True]
    assert len(sets) == 1, "need_resync = True 应恰好出现一次"
    duty_ifs = [n for n in ast.walk(tree)
                if isinstance(n, ast.If) and "FRAME_DROP_DUTY" in _names(n.test)]
    assert len(duty_ifs) == 1
    assert any(s in list(ast.walk(duty_ifs[0])) for s in sets), \
        "need_resync=True 必须只在 FRAME_DROP_DUTY 分支里（K7）"
    assert "starve_pending = True" in _rtp_source()   # STARVE 走 hold 而非 need_resync


def test_k2_clock_gate_is_gone_and_cannot_come_back():
    """K2：闸门必须从可执行代码里彻底消失，而不是被包成一个恒真的钩子。

    纯源码字符串断言（"if ... < FRAME_PERIOD_MS not in src"）是脆弱反模式：
    改个变量名就红、把条件挪进函数就绿。这里全部改成 AST 语义断言 ——
      1) admit_frame_by_clock 的函数体只剩一条 return FRAME_SEND；
      2) 模块里**任何地方都没有调用它**（旧版在主循环里调用着）；
      3) 没有任何 Compare 拿 FRAME_PERIOD_MS 当阈值（旧闸门的形状）；
      4) overfps 是恒 0 的兼容字段：不被自增、只被初始化。
    """
    rp = _load_k230()
    src = _rtp_source()
    tree = ast.parse(src)

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "admit_frame_by_clock")
    stmts = [s for s in fn.body                 # 去掉 docstring
             if not (isinstance(s, ast.Expr)
                     and isinstance(s.value, ast.Constant))]
    assert len(stmts) == 1 and isinstance(stmts[0], ast.Return), \
        "admit_frame_by_clock 只应剩一条 return"
    ret = stmts[0].value
    assert isinstance(ret, ast.Name) and ret.id == "FRAME_SEND"

    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "admit_frame_by_clock"], \
        "运行期不得再调用按时钟丢帧的闸门"

    bad = [n for n in ast.walk(tree)
           if isinstance(n, ast.Compare) and "FRAME_PERIOD_MS" in _names(n)]
    assert not bad, "仍有代码拿 FRAME_PERIOD_MS 做比较（旧闸门形状）"

    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.AugAssign)
                and isinstance(n.target, ast.Attribute)
                and n.target.attr == "overfps"], "overfps 恒为 0，不该被自增"
    inits = [n for n in ast.walk(tree)
             if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Attribute)
             and n.targets[0].attr == "overfps"]
    assert len(inits) == 1 and getattr(inits[0].value, "value", None) == 0
    assert rp.TxStats().overfps == 0
    assert "overfps=%d" in src          # 统计行仍打印（兼容旧日志解析）


def test_k2_clock_gate_never_drops_encoded_frames():
    rp = _load_k230()
    # 旧闸门：到达间隔 < FRAME_PERIOD_MS(33) 就 ReleaseStream 丢帧。
    # 编码器自己按 dst_frame_rate 出帧，间隔抖动几毫秒就会误丢已编码的 P 帧，
    # 且不置 need_resync、不消耗 seq -> 接收端看不到缺口，不会请求 IDR。
    assert rp.FRAME_PERIOD_MS == 33
    assert rp.admit_frame_by_clock(1000, 1000 - 32) == rp.FRAME_SEND
    assert rp.admit_frame_by_clock(1000, 999) == rp.FRAME_SEND
    assert rp.admit_frame_by_clock(1000, 0) == rp.FRAME_SEND   # 首次
    assert rp.admit_frame_by_clock(0, 0) == rp.FRAME_SEND       # 回绕


def test_k3_credentials_hardcoded_with_optional_config_override(tmp_path):
    """凭据硬编码在代码里（团队已接受该风险），配置文件只做覆盖。"""
    rp = _load_k230()
    # 1. 默认值来自代码常量，不再要求「配置文件必须存在」
    assert rp.CONFIG_PATH == "/sdcard/tindistance.json"
    assert rp.WIFI_SSID and rp.WIFI_PASSWORD
    # 2. 优先级：env > 配置文件 > 代码默认值
    assert rp.resolve_settings(None, {})["password"] == rp.WIFI_PASSWORD
    assert rp.resolve_settings(None, {})["ssid"] == rp.WIFI_SSID
    assert rp.resolve_settings({"password": "fromfile"}, {})["password"] == "fromfile"
    assert rp.resolve_settings(
        {"password": "fromfile"}, {"password": "fromenv"})["password"] == "fromenv"
    # 3. 配置文件不存在不抛错，回落默认值（这是 K3 改回硬编码的关键行为）
    assert rp.load_config(str(tmp_path / "nope.json")) is None
    # 4. 但文件存在且字段类型非法必须报出来，不能被默认值悄悄掩盖
    for name, text in (("badtype", '{"ssid": 123}'),
                       ("badkbps", '{"link_kbps": "abc"}')):
        p = tmp_path / (name + ".json")
        p.write_text(text, encoding="utf-8")
        try:
            rp.load_config(str(p))
        except RuntimeError:
            pass
        else:
            raise AssertionError("must raise on " + name)


# ---------- K5/K6：Pacer 突发 vs 节流 ----------
def test_pacer_bursts_small_frames_without_sleep():
    rp = _load_k230()
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                     rp.PACER_SPAN_MAX_MS)
    pkts = [b"x" * rp.MAX_PAYLOAD for _ in range(9)]     # 一帧 9 包 ≈ 12.6KB
    assert rp.send_paced(pacer, pkts, lambda p: None) == 9
    assert pacer.sleeps == 0 and pacer.wait_us == 0      # P 帧直接突发
    assert pacer.burst_packets == 9
    assert clock.us == 0


def test_pacer_throttles_large_frames_and_calls_idle():
    rp = _load_k230()
    clock = FakeClock()
    idles = []
    # 令牌池刻意很小 -> 大帧必然进入 sleep 分支
    pacer = rp.Pacer(1000, 2000, clock, rp.PACER_SPAN_MAX_MS,
                     idle=lambda: idles.append(clock.us))
    big = [b"y" * rp.MAX_PAYLOAD for _ in range(20)]     # 28KB > burst 阈值
    assert rp.send_paced(pacer, big, lambda p: None) == 20
    assert pacer.sleeps > 0
    assert len(idles) >= pacer.sleeps                    # K4：sleep 前后都服务控制 socket
    assert clock.us > 0
    assert pacer.burst_packets == 0


def test_pacer_burst_threshold_is_bounded_by_frame():
    rp = _load_k230()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, FakeClock(),
                     rp.PACER_SPAN_MAX_MS)
    assert pacer.burst_threshold == rp.BURST_SEND_BYTES == 20000
    assert pacer.set_burst(20000) is True
    assert pacer.set_burst(20001) is False
    assert pacer.bursting is False


def test_pacer_burst_debt_is_bounded_by_bucket_capacity():
    rp = _load_k230()
    clock = FakeClock()
    pacer = rp.Pacer(1000, 2000, clock, rp.PACER_SPAN_MAX_MS)   # burst=2000B
    pacer.tokens = 0                                            # 桶已见底
    pacer.set_burst(1200)                                       # 突发模式
    assert pacer.take(1200) == 0                                # 债务 = -(1200+40)
    assert pacer.tokens == -1240
    assert pacer.sleeps == 0
    # 债务再往下就越过 -burst(2000) -> 必须回到等待循环，不能无限透支
    waited = pacer.take(1000)
    assert waited > 0 and pacer.sleeps > 0
    assert pacer.tokens >= -pacer.burst


def test_pacer_can_send_respects_burst_debt():
    rp = _load_k230()
    clock = FakeClock()
    pacer = rp.Pacer(1000, 2000, clock, rp.PACER_SPAN_MAX_MS)
    # 小帧在突发容差内 -> 即使桶空也放行（K5：P 帧宁可突发也不 sleep）
    pacer.tokens = 0
    assert pacer.can_send(1000, 1) is True
    assert pacer.can_send(1960, 1) is True                     # 债务 2000 == -burst
    assert pacer.can_send(2100, 1) is False                   # 债务会超 -burst
    # 大帧不看突发容差，只看令牌
    assert pacer.can_send(5000, 1) is False


def test_pacer_span_cap_records_token_debt():
    rp = _load_k230()
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, 1000, clock, rp.PACER_SPAN_MAX_MS,
                     burst_threshold=0)
    before = clock.us
    pacer.take(4000)                                     # 远超 span 预算
    assert pacer.cap_events >= 1                         # 截断记账，不无限拉长
    assert clock.us - before <= rp.PACER_SPAN_MAX_MS * 1000
    assert pacer.tokens < 0                              # 记为令牌债务


# ---------- 阶段 2：NACK 环形缓冲 / 解析 / 重传限流 ----------
def test_packet_ring_hit_miss_and_wrap():
    rp = _load_k230()
    ring = rp.PacketRing()
    assert len(ring) == 128
    for i in range(300):                              # 远超容量，必然环绕覆盖
        ring.put(i, ("p%d" % i).encode())
    assert ring.get(299) == b"p299"                    # 最新 128 个：命中
    assert ring.get(298) == b"p298"
    assert ring.get(172) == b"p172"                    # 299-127，仍在窗口内
    assert ring.get(171) is None                       # 刚被覆盖：未命中
    # 槽位复用：seq 0 与 seq 256 落同一槽（0 & 127 == 256 & 127）
    assert 0 & ring.mask == 256 & ring.mask
    assert ring.get(256) == b"p256"
    assert ring.get(0) is None                         # 槽里已是 seq 256 -> 未命中
    assert ring.puts == 300


def test_packet_ring_wraps_seq_space():
    rp = _load_k230()
    ring = rp.PacketRing()
    ring.put(65535, b"a")
    ring.put(0, b"b")
    assert ring.get(65535) == b"a" and ring.get(0) == b"b"
    assert ring.get(1) is None
    try:
        rp.PacketRing(100)                               # 非 2 的幂
        raise AssertionError("must reject non power-of-two capacity")
    except ValueError:
        pass


def test_parse_nack_single_and_batch():
    rp = _load_k230()
    seqs, ok = rp.parse_nack(rp.build_nack([7]))
    assert ok and seqs == [7]

    many = list(range(1000, 1032))                        # 满包 32 个
    seqs, ok = rp.parse_nack(rp.build_nack(many))
    assert ok and seqs == many
    assert len(rp.build_nack(many)) == 5 + 32 * 2         # 69B -> recv 缓冲必须 >= 69

    # 线路格式逐字节核对：C B R 0x03 count seq(u16 BE)
    raw = rp.build_nack([0x0102])
    assert raw == b"CBR" + bytes([0x03, 1, 0x01, 0x02])


def test_parse_nack_rejects_malformed():
    rp = _load_k230()
    assert rp.parse_nack(b"") == (None, False)
    assert rp.parse_nack(b"CBR\x03") == (None, False)     # 缺 count
    assert rp.parse_nack(b"CBR\x03\x00\x00\x01") == (None, False)   # count=0
    assert rp.parse_nack(b"CBR\x03\x21\x00\x01") == (None, False)   # count=33 > 32
    # count 与实际字节数不符（截断包）
    assert rp.parse_nack(b"CBR\x03\x02\x00\x01") == (None, False)
    # 多余尾巴
    assert rp.parse_nack(b"CBR\x03\x01\x00\x01\xff") == (None, False)
    # 别的 kind
    assert rp.parse_nack(b"CBR\x01\x00") == (None, False)
    assert rp.parse_nack(None) == (None, False)
    assert rp.build_nack([]) is None


def test_retransmit_budget_is_15_percent():
    rp = _load_k230()
    # 15% × 码率 × 1s 窗口
    assert rp.retransmit_limit_bytes(3000) == 56250       # T1
    assert rp.retransmit_limit_bytes(6000) == 112500      # T3
    assert rp.RETRANS_BUDGET_RATIO == 0.15

    lim = rp.RetransmitLimiter(rp.retransmit_limit_bytes(3000))
    assert lim.allow(0, 56250) is True                    # 刚好用完预算
    assert lim.allow(0, 1) is False                       # 超预算 -> 丢弃本轮
    assert lim.blocked == 1
    assert lim.allow(999, 1) is False                     # 同一窗口内继续超
    assert lim.allow(1000, 1400) is True                  # 窗口滚动后恢复
    assert lim.used == 1400


def _fu_a_frame(rp, nalu_len):
    """造一个会被 FU-A 切成多包的 Annex-B NALU，返回 (nalus, ts, seq)。"""
    raw = b"\x00\x00\x00\x01\x65" + bytes([(i * 7) & 0xFF for i in range(nalu_len)])
    return rp.split_nalus(raw)


def test_packetize_fu_a_slices_and_sets_marker_on_last():
    rp = _load_k230()
    ts, seq = 12345, 7
    pkts = []
    nalu = _fu_a_frame(rp, rp.MAX_PAYLOAD * 2 + 17)[0]
    seq = rp.packetize_nalu(nalu, ts, seq, 0x54494E44, pkts)
    # NAL 去掉 4B start code = 2813B，再去掉 1B NAL 头 -> 2812B 负载
    # / 1400 -> 3 片（1400 + 1400 + 12）
    assert len(pkts) == 3
    assert seq == 10                       # 3 个包推进了 3 个 seq
    for i, p in enumerate(pkts):
        assert len(p) == 12 + 2 + min(rp.MAX_PAYLOAD,
                                      len(nalu) - 1 - i * rp.MAX_PAYLOAD)
        assert p[0] == 0x80 and (p[1] & 0x80) == 0    # V=2，无 marker
        assert ((p[2] << 8) | p[3]) == 7 + i
        assert ((p[4] << 24) | (p[5] << 16) | (p[6] << 8) | p[7]) == ts
        assert p[12] == (0x65 & 0xE0) | 28            # FU indicator
        assert (p[13] & 0x1F) == 0x05                 # 原 nal_type
    assert (pkts[0][13] & 0x80) == 0x80               # S 起始
    assert (pkts[-1][13] & 0x40) == 0x40              # E 结束
    assert (pkts[1][13] & 0xC0) == 0
    # 打 marker（stream_loop 的做法）+ 入环：ring 里必须是打了 marker 的那个对象
    pkts[-1] = bytearray(pkts[-1])
    pkts[-1][1] |= 0x80
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                     rp.PACER_SPAN_MAX_MS)
    ring = rp.PacketRing()
    assert rp.send_paced(pacer, pkts, lambda x: None, 1, ring=ring,
                         first_seq=7) == 3
    assert ring.get(9) is pkts[-1]
    assert ring.get(9)[1] & 0x80, "marker 位必须在入环前打好（否则接收端等不到帧尾）"
    for i in range(3):
        held = ring.get(7 + i)
        assert ((held[2] << 8) | held[3]) == 7 + i, "ring 的 seq 必须与包头一致"
        assert held is pkts[i]


def test_packetize_single_nal_and_mtu_budget():
    rp = _load_k230()
    # 单 NAL：整体作为一个 RTP 负载，不加 FU 头
    pkts = []
    small = _fu_a_frame(rp, 100)[0]
    seq = rp.packetize_nalu(small, 1, 5, 0x54494E44, pkts)
    assert len(pkts) == 1 and seq == 6
    assert pkts[0][:12] == rp.rtp_header(5, 1, False, 0x54494E44)
    # MTU 预算：FU-A 2 + RTP 12 + UDP 8 + IP 20 = 1442 < 1500（docs §4.2）
    big = _fu_a_frame(rp, rp.MAX_PAYLOAD * 4)[0]
    pkts = []
    rp.packetize_nalu(big, 1, 0, 0x54494E44, pkts)
    wire = max(len(p) for p in pkts) + 8 + 20
    assert wire == 1442 and wire < 1500
    assert rp.MAX_PAYLOAD + 12 + 2 + 8 + 20 <= 1500


def test_send_paced_records_actual_seq_in_ring():
    rp = _load_k230()
    clock = FakeClock()
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, clock,
                     rp.PACER_SPAN_MAX_MS)
    ring = rp.PacketRing()
    # 用真 packetize_nalu 造包，才能把「包头 seq」和「ring 的 seq」对上
    pkts = []
    seq_next = 65534
    for size in (100, 50, 25):
        seq_next = rp.packetize_nalu(_fu_a_frame(rp, size)[0], 9, seq_next,
                                     0x54494E44, pkts)
    assert seq_next == (65534 + 3) & 0xFFFF
    sent = []
    assert rp.send_paced(pacer, pkts, sent.append, 1, ring=ring,
                         first_seq=65534) == 3
    # seq 用各包实际使用的值（first_seq 推进），不是「发完之后」的 seq；
    # 并且存的是**同一个对象**（不是拷贝）。
    assert ring.get(65534) is pkts[0]
    assert ring.get(65535) is pkts[1]
    assert ring.get(0) is pkts[2]
    assert ring.get(1) is None
    assert len(sent) == 3
    for i, p in enumerate(pkts):
        seq = (65534 + i) & 0xFFFF
        assert ((p[2] << 8) | p[3]) == seq
        assert ring.get(seq) is p


# ---------- K12：RTP 时间戳 ----------
def test_rtp_timestamp_monotonic_and_masked():
    rp = _load_k230()
    assert rp.RTP_TICKS_PER_MS == 90
    assert rp.rtp_timestamp(0) == 0
    assert rp.rtp_timestamp(1) == 90
    assert rp.rtp_timestamp(33) == 2970                   # 1 帧 @30fps
    prev = -1
    for ms in range(0, 5000, 7):                         # 严格单调
        cur = rp.rtp_timestamp(ms)
        assert 0 <= cur <= 0xFFFFFFFF
        assert cur > prev
        prev = cur
    # 回绕：超过 2^32 后被掩码，仍然是 32 位量
    big = rp.rtp_timestamp((1 << 32) // 90 + 1)
    assert big == ((1 << 32) // 90 + 1) * 90 & 0xFFFFFFFF
    assert big < (1 << 32)


def test_rtp_timestamp_does_not_compress_on_drop():
    rp = _load_k230()
    # 丢帧只是不调用 rtp_timestamp；时间戳直接来自采集时钟，
    # 所以帧间隔仍反映真实流逝时间（旧的 frame_count*3000 会被压缩）。
    assert rp.rtp_timestamp(1000) - rp.rtp_timestamp(0) == 90000


# ---------- K9/K10：配置与档位 ----------
def test_tier_table_maps_to_resolution_and_bitrate():
    rp = _load_k230()
    assert rp.TIERS == {"T1": (1280, 720, 3000),
                        "T2": (1280, 720, 4500),
                        "T3": (1920, 1080, 6000)}
    for tier, (w, h, br) in rp.TIERS.items():
        s = rp.resolve_settings({"ssid": "a", "password": "b", "tier": tier})
        assert (s["width"], s["height"], s["bit_rate"]) == (w, h, br)
    # 缺 tier -> 落到 DEFAULT_TIER（当前 T3 = 1920x1080 / 6000kbps）。
    # 这里刻意跟 DEFAULT_TIER / DEFAULT_WIFI_LINK_KBPS 走而不是写死字面量：
    # 换默认档是产品决定，不该被测试挡下；但「默认值必须能解析成一个真档位」
    # 和「默认带宽必须 >= 该档的最低要求」是硬约束，继续断言。
    s = rp.resolve_settings({"ssid": "a", "password": "b"})
    assert s["tier"] == rp.DEFAULT_TIER
    w, h, br = rp.TIERS[rp.DEFAULT_TIER]
    assert (s["width"], s["height"], s["bit_rate"]) == (w, h, br)
    assert s["link_kbps"] == rp.DEFAULT_WIFI_LINK_KBPS
    assert not s["warnings"], "默认档 + 默认带宽不该触发带宽不足告警"
    assert rp.DEFAULT_WIFI_LINK_KBPS >= rp.TIER_MIN_LINK_KBPS[rp.DEFAULT_TIER]
    # 未知档位 -> RuntimeError
    try:
        rp.resolve_settings({"ssid": "a", "password": "b", "tier": "T9"})
        raise AssertionError("must reject unknown tier")
    except RuntimeError:
        pass


def test_resolve_settings_link_kbps_and_warning():
    rp = _load_k230()
    s = rp.resolve_settings({"ssid": "a", "password": "b", "tier": "T3",
                             "link_kbps": 12000})
    assert s["link_kbps"] == 12000 and s["warnings"] == []
    # 低于该档位要求（§1.1：T3 需 ≥10 Mbps）-> 只警告不阻断
    s = rp.resolve_settings({"ssid": "a", "password": "b", "tier": "T3",
                             "link_kbps": 6000})
    assert s["link_kbps"] == 6000 and len(s["warnings"]) == 1
    assert "T3" in s["warnings"][0] and "10000" in s["warnings"][0]
    # env 覆盖优先于配置文件（可选调试通道）
    s = rp.resolve_settings({"ssid": "a", "password": "b", "tier": "T1"},
                            env={"tier": "T2", "link_kbps": "9000",
                                 "ssid": "env-ssid"})
    assert s["tier"] == "T2" and s["bit_rate"] == 4500
    assert s["link_kbps"] == 9000 and s["ssid"] == "env-ssid"
    # 非法 link_kbps
    try:
        rp.resolve_settings({"ssid": "a", "password": "b",
                             "link_kbps": "abc"})
        raise AssertionError("must reject bad link_kbps")
    except RuntimeError:
        pass


def test_load_config_missing_bad_json_and_partial_override(tmp_path):
    rp = _load_k230()
    # 「没有配置文件」不是错误：缺失 / 坏 JSON / 顶层不是对象 -> None
    assert rp.load_config(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert rp.load_config(str(bad)) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2]", encoding="utf-8")
    assert rp.load_config(str(arr)) is None
    # 部分覆盖是合法的：只写 tier 也行，其余回落默认值
    part = tmp_path / "part.json"
    part.write_text(__import__("json").dumps({"tier": "T2"}), encoding="utf-8")
    cfg = rp.load_config(str(part))
    assert cfg == {"tier": "T2"}
    s = rp.resolve_settings(cfg)
    assert s["ssid"] == rp.WIFI_SSID and s["password"] == rp.WIFI_PASSWORD
    assert s["tier"] == "T2" and s["bit_rate"] == 4500
    # 类型非法必须报错（配置写错不该被默认值掩盖）
    for name, text in (("badtype", '{"ssid": 123}'),
                       ("badkbps", '{"link_kbps": "abc"}'),
                       ("zerokbps", '{"link_kbps": 0}')):
        p = tmp_path / (name + ".json")
        p.write_text(text, encoding="utf-8")
        try:
            rp.load_config(str(p))
        except RuntimeError:
            pass
        else:
            raise AssertionError("must raise on " + name)
    # 正常配置
    good = tmp_path / "good.json"
    good.write_text(__import__("json").dumps(
        {"ssid": "lab", "password": "secret", "server_ip": "192.168.137.1",
         "tier": "T2", "link_kbps": 9000}), encoding="utf-8")
    cfg = rp.load_config(str(good))
    assert cfg["tier"] == "T2" and cfg["link_kbps"] == 9000
    s = rp.resolve_settings(cfg)
    assert (s["ssid"], s["server_ip"], s["tier"], s["bit_rate"]) == (
        "lab", "192.168.137.1", "T2", 4500)


def test_module_defaults_are_single_link_t1():
    rp = _load_k230()
    # K1：单链路单拷贝单端口
    assert (rp.LINK_COPIES, rp.IDR_COPIES) == (1, 1)
    assert len(rp.RTP_PORTS) == 1
    # K5/K6：长 GOP + 80ms 节流上限 + duty 0.9
    assert (rp.GOP_LEN, rp.PACER_SPAN_MAX_MS, rp.PACER_DUTY) == (60, 80, 0.9)
    # 凭据默认值内置，且解析不报错（本机必然没有 /sdcard/tindistance.json）
    assert rp.WIFI_SSID and rp.WIFI_PASSWORD
    assert rp.CONFIG_ERROR is None
    assert (rp.WIFI_SSID, rp.WIFI_PASSWORD) == (
        rp.SETTINGS["ssid"], rp.SETTINGS["password"])
    # overfps 字段仍在（统计兼容）
    st = rp.TxStats()
    assert st.overfps == 0
    st.reset_rolling()
    assert hasattr(st, "overfps")


# ---------- CommandLink：0x01 IDR + 0x03 NACK ----------
class _FakeCmdSock:
    def __init__(self, packets):
        self.packets = list(packets)
        self.recv_sizes = []

    def settimeout(self, _t):
        pass

    def recv(self, n):
        self.recv_sizes.append(n)
        return self.packets.pop(0) if self.packets else b""


class _FakeUart:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)


def _aa55_frame(payload=b"\x01"):
    body = bytes([0xAA, 0x55, 1, 0x01, len(payload)]) + payload
    return body + bytes([rp_crc8(body)])


def rp_crc8(payload):
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def test_command_link_pump_handles_idr_and_nack():
    rp = _load_k230()
    idr_hits = []
    nack_hits = []
    uart = _FakeUart()
    link = rp.CommandLink(uart, on_ctrl=idr_hits.append,
                          on_nack=nack_hits.append)
    sock = _FakeCmdSock([
        rp.build_nack([1, 2, 3]),
        b"CBR" + bytes([rp.CTRL_IDR]),
        b"CBR" + bytes([rp.CTRL_NACK, 2, 0, 5]),          # 截断 -> 整包丢弃
        _aa55_frame(b"\x7f"),
    ])
    link.pump(sock)

    assert link.nack_rx == 1 and link.nack_seqs == 3
    assert nack_hits == [[1, 2, 3]]
    assert link.nack_bad == 1
    assert idr_hits == [b"IDR"] and link.ctrl_rx == 1
    assert link.rx == 1 and link.ok is True
    assert uart.writes == [_aa55_frame(b"\x7f")]
    # 一包 32 seq 的 NACK 有 69B，recv 缓冲必须放得下（原来 64 会截断）
    assert max(sock.recv_sizes) >= 69
    line = link.stats_line()
    for token in ("nack rx=1", "hit=0", "miss=0", "budget=0", "bad=1"):
        assert token in line


def test_retransmit_nack_end_to_end():
    rp = _load_k230()
    ring = rp.PacketRing()
    pacer = FakePacer(stock=1 << 20)
    pkts = [b"r" * 100 for _ in range(4)]
    rp.send_paced(pacer, pkts, lambda p: None, 1, ring=ring, first_seq=10)
    # 已发出 seq 10..13

    sent = []

    class Sock:
        def sendto(self, data, addr):
            sent.append((bytes(data), addr))

    limiter = rp.RetransmitLimiter(rp.retransmit_limit_bytes(rp.BIT_RATE))
    st = rp.TxStats()
    link = rp.CommandLink(_FakeUart())
    dst = ("192.168.137.1", rp.RTP_PORTS[0])

    hits, miss, drop, err = rp.retransmit_nack([10, 13], ring, limiter, Sock(),
                                               dst, 0, stats=st, link=link)
    assert (hits, miss, drop, err) == (2, 0, 0, 0)
    assert [d for d, _a in sent] == [pkts[0], pkts[3]]
    assert {a for _d, a in sent} == {dst}          # 同一目的地
    assert st.retx_bytes == 200 and link.nack_hits == 2

    # 未命中 + 预算受限：11 在环形缓冲里但 100B > 50B 预算 -> 拦；
    # 99 从未发出 -> miss
    sent[:] = []
    tiny = rp.RetransmitLimiter(50)
    hits, miss, drop, err = rp.retransmit_nack([11, 99], ring, tiny, Sock(),
                                               dst, 1000, stats=st, link=link)
    assert (hits, miss, drop, err) == (0, 1, 1, 0)
    assert sent == []
    assert link.nack_misses == 1 and link.nack_budget == 1


def test_packet_ring_stores_reference_without_copy():
    rp = _load_k230()
    ring = rp.PacketRing()
    pkt = b"z" * 500
    ring.put(1, pkt)
    assert ring.get(1) is pkt          # 同一对象，没有 bytes() 拷贝


def test_k230_module_imports_only_micropython_modules():
    import ast
    tree = ast.parse(_rtp_source())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    allowed = {"gc", "json", "os", "socket", "struct", "time", "uctypes",
               "network", "media", "machine", "sys"}
    assert mods <= allowed, sorted(mods - allowed)
    # 没有 f-string / 类型注解 / dataclass 之类的进阶语法糖
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree))


def test_tx_stats_line_reports_nack_retx_and_gc():
    rp = _load_k230()
    st = rp.TxStats()
    st.sent_frames = 30
    st.bytes_tx = 60000
    st.retx_bytes = 1500
    st.starve_hold = 2
    st.note_gc(7000)
    st.gc_window_n = 1
    pacer = rp.Pacer(rp.PACER_WIRE_KBPS, rp.PACER_BURST_BYTES, FakeClock(),
                     rp.PACER_SPAN_MAX_MS)
    pacer.packets = 300
    gate = rp.IdrGate(400, 500)
    line = st.line(1.0, pacer, gate, 123456)
    assert "overfps=0" in line          # 字段/统计位保留，恒为 0
    assert "hold=2" in line
    assert "retx=12.0kbps" in line      # 1500B/1s*8/1000
    assert "gc=7.0ms/1" in line
    assert "burst=0pkt" in st.detail(pacer, gate)
    st.reset_rolling()
    assert st.overfps == 0 and st.gc_max_us == 7000       # max 不随窗口清零


# ---------- relay 多端口扇出集成（真 socket） ----------
def _rtp_pkt(seq=1, marker=True, payload=b"Z" * 20):
    return struct.pack(">BBHII", 0x80, 0x60 | (0x80 if marker else 0),
                       seq & 0xFFFF, 1000, 0x54494E44) + payload


def _bind_ephemeral():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    return s


def test_relay_multiport_fanout_and_ctrl():
    from server.rtp_relay import RtpRelay, CONTROL_MAGIC, CTRL_IDR, CTRL_PONG

    relay = RtpRelay([18102, 18103])
    assert relay.start() is True
    try:
        ds = _bind_ephemeral()
        us = _bind_ephemeral()

        # 下游注册（发 CBR ping 到 18102）
        ds.sendto(CONTROL_MAGIC + bytes([0x00]), ("127.0.0.1", 18102))
        # 等 pong
        got_pong = False
        for _ in range(50):
            try:
                data, _ = ds.recvfrom(2048)
            except socket.timeout:
                break
            if data[:3] == CONTROL_MAGIC and data[3] == CTRL_PONG:
                got_pong = True
                break
            time.sleep(0.02)
        assert got_pong

        # K230 两链同 seq 同包分别发到两个入口
        pkt = _rtp_pkt(seq=7)
        us.sendto(pkt, ("127.0.0.1", 18102))
        us.sendto(pkt, ("127.0.0.1", 18103))

        # 下游应收齐 2 份拷贝
        seen = []
        for _ in range(50):
            try:
                data, _ = ds.recvfrom(2048)
            except socket.timeout:
                break
            if (data[0] & 0xC0) == 0x80:
                seen.append(data)
            time.sleep(0.01)
        assert len(seen) == 2 and seen[0] == seen[1] == pkt

        # 下游发 IDR -> 中继转发到上游
        ds.sendto(CONTROL_MAGIC + bytes([CTRL_IDR]), ("127.0.0.1", 18102))
        data, _ = us.recvfrom(2048)
        assert data[:3] == CONTROL_MAGIC and data[3] == CTRL_IDR

        # 状态聚合（pkts_rx 含 CBR ping+IDR，原语义：所有入站数据报都计；
        # 两份同 seq marker 拷贝 -> frame_id 从 -1 加到 1）
        time.sleep(0.1)
        st = relay.status()
        if (st["pkts_rx"], st["pkts_tx"], st["frame_id"]) != (4, 2, 1):
            print("debug status:", st)
        assert st["pkts_rx"] == 4 and st["pkts_tx"] == 2
        assert st["frame_id"] == 1
        assert st["upstreams"]["18102"][0] is not None
        assert st["upstreams"]["18103"][0] is not None
    finally:
        relay.stop()
    assert relay.start() is True  # 停止后可重启
    relay.stop()


def test_relay_port_collision_reports_false():
    from server.rtp_relay import RtpRelay
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", 18109))
    relay = RtpRelay([18109])
    try:
        assert relay.start() is False
    finally:
        blocker.close()
        relay.stop()


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

"""丢包率的窗口化统计（纯函数，无 I/O、无 Tk）。

为什么单独一个模块
------------------
三处都要用同一套算术：面板画丢包曲线、探针出窗口数字、测试单独跑边界。
它们都不该被 ``httpx``（``latency_monitor``）或 ``tkinter``（``latency_panel``）
拖住依赖。所以这里只吃「一串累计计数快照」，用鸭子类型读属性，不 import
``LatencySample`` —— 测试可以拿 ``types.SimpleNamespace`` 直接喂。

口径：两级（下游）+ 一段（上游）
--------------------------------
``wire``（线上）
    接收端发现空洞时缺失的 RTP 序号数，**含**后来被 NACK 补回的那些。这是链路
    真实丢过的包。
``final``（最终）
    空洞过 ``REORDER_S``(40 ms) 仍未补上、被 ``declare_loss`` 判丢的部分，也就是
    真正打到画面上的损伤。
``up``（上游 = K230→server）
    中继侧观测到的序号缺口。**只有线上口径**：中继没有重排缓冲，一个补回来的
    重传包在它看来只是「乱序」，无法与真正的重复拷贝区分 —— 所以不能假装它能
    分出最终口径。面板上这一格必须标注，测试里也钉死。
恢复率 = ``(wire - final) / wire``，即两级恢复（NACK 重传）实际救回的比例。

窗口 vs 累计
------------
累计适合日志和长时间统计，但**不适合实时判断**：一次 WiFi 抖动会把 ``loss_pct``
永久拉高，之后再也回不来（改造前 ``h264_viewer.loss_pct`` 就是这个问题）。
于是两者都算：累计进日志/底栏，10 s 窗口驱动面板主数字与告警色。

窗口怎么算
----------
计数器单调，所以窗口值 = 累计差。四个坑，都在下面显式处理并各有测试：

1. **计数器复位**：K230 重启（seq 归零）或 viewer 重建会让累计值**变小**，差值
   为负。按 0 处理（:func:`_delta`），宁可少报也不报负丢包率。
2. **未上报样本**：``LatencySample`` 的默认值是 ``-1``。含 -1 的样本整条丢弃
   （:func:`_usable`），绝不让 -1 参与减法。
3. **窗口不满**：启动不足一个窗口时没有基线样本。退化成「用最老的一条做基线」，
   并把真实覆盖时长 ``span_s`` 一并返回，由面板决定要不要标注 —— 不外推、不
   假装满窗。
4. **跨度为零**：所有样本同一时刻（单测常见）时没有速率可言，返回无数据而不是
   0%。

分母统一是 ``lost + rx``：``rx`` 是**入队**包数（已扣掉重复与过期包），所以
``rx + lost`` 就是「本该到达的序号数」。用别的分母会在重传生效时把丢包率压低。

口径与展示方案的完整说明（含阈值依据与已知局限）见
``docs/packet-loss-telemetry.md``。
"""
from __future__ import annotations

from typing import Iterable, Sequence

WINDOW_S = 10.0
SPAN_S = 60.0
BUCKET_S = 1.0

#: 线上丢包率告警线（窗口值，百分比）。0.5% 在 30 fps / ~1200 pps 下约等于
#: 每秒 6 个包 —— 低于 NACK 一轮 4 ms 的反应时间对应的可感知阈值，高于它说明
#: 恢复层已经在满负荷工作。
WARN_PCT = 0.5
#: 明显劣化线。超过它画面上必然能看出损伤（宏块花屏 / 等 IDR）。
BAD_PCT = 2.0

#: 丢包率是小数量级（0.062% 这种），保留 3 位小数才有信息量。
PRECISION = 3

DOWNSTREAM_COUNTERS = ("pkts_rx", "wire_lost", "final_lost")
UPSTREAM_COUNTERS = ("up_rx", "up_lost")
ALL_COUNTERS = DOWNSTREAM_COUNTERS + UPSTREAM_COUNTERS

_IDX = {name: i for i, name in enumerate(ALL_COUNTERS)}
RX = _IDX["pkts_rx"]
WIRE = _IDX["wire_lost"]
FINAL = _IDX["final_lost"]
UP_RX = _IDX["up_rx"]
UP_LOST = _IDX["up_lost"]

#: 「没有数据」哨兵。与 ``latency_monitor._num(..., lo=0)`` 的 -1 约定一致：
#: 宁可显示「—」，也不显示一个编出来的 0%。
UNKNOWN = -1.0


def _delta(new: float, old: float) -> float:
    """累计计数器的窗口增量。复位导致的负差按 0 处理（模块 docstring 坑 1）。"""
    d = float(new) - float(old)
    return d if d > 0.0 else 0.0


def _pct(lost: float, rx: float) -> float:
    """丢包率 = lost / (lost + rx)。分母为 0 时返回 UNKNOWN 而不是 0。"""
    total = lost + rx
    if total <= 0.0:
        return UNKNOWN
    return round(100.0 * lost / total, PRECISION)


def _usable(sample: object, keys: Sequence[str]) -> bool:
    """样本是否带着全部所需计数器（任一为负 = 没上报 = 不可用，坑 2）。"""
    for k in keys:
        v = getattr(sample, k, None)
        if v is None or isinstance(v, bool):
            return False
        try:
            if float(v) < 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _snapshots(samples: Iterable[object],
               keys: Sequence[str]) -> list[tuple[float, tuple[float, ...]]]:
    """抽出 ``(t, 计数器元组)`` 的时序，丢弃不可用样本并按 t 升序。

    探针是 ``deque.append`` 顺序写入，天然按时间升序；这里仍然显式排序，因为
    ``samples()`` 是对外 API，不该把这个不变量泄漏给调用方。
    """
    out: list[tuple[float, tuple[float, ...]]] = []
    for s in samples:
        t = getattr(s, "t", None)
        if t is None or not _usable(s, keys):
            continue
        out.append((float(t), tuple(float(getattr(s, k)) for k in keys)))
    out.sort(key=lambda item: item[0])
    return out


def _base_index(snaps: Sequence[tuple[float, tuple[float, ...]]],
                cutoff: float) -> int:
    """时间上不晚于 ``cutoff`` 的最后一条的下标；没有则取 0（坑 3）。"""
    base_i = 0
    for i, (t, _v) in enumerate(snaps):
        if t <= cutoff:
            base_i = i
        else:
            break
    return base_i


def _slice(snaps: Sequence[tuple[float, tuple[float, ...]]],
           span_s: float, now: float) -> dict:
    """窗口内的增量切片（对所有计数器同时成立，避免重复实现基线查找）。"""
    if not snaps:
        return {"deltas": (), "span_s": 0.0, "samples": 0}
    base_i = _base_index(snaps, now - span_s)
    base_t, base = snaps[base_i]
    newest_t, newest = snaps[-1]
    span_actual = newest_t - base_t
    if span_actual <= 0.0:
        return {"deltas": (), "span_s": 0.0, "samples": 0}   # 坑 4
    return {
        "deltas": tuple(_delta(newest[i], base[i]) for i in range(len(base))),
        "span_s": span_actual,
        "samples": len(snaps) - base_i,
    }


def window(samples: Sequence[object], span_s: float = WINDOW_S,
           now: float | None = None) -> dict:
    """丢包窗口（默认最近 10 s）。

    返回::

        {
          "wire_lost": float, "wire_pct": float,   # 线上（下游）
          "final_lost": float, "final_pct": float, # 最终（下游）
          "up_lost": float, "up_pct": float,       # 上游（只有线上口径）
          "recovery_pct": float,                   # 两级恢复救回的比例
          "span_s": float,                         # 实际覆盖时长
          "covered": bool,                         # 是否满窗
          "samples": int,
        }

    任一口径没有数据时对应字段为 ``UNKNOWN``（-1.0），调用方应显示「—」。
    """
    snaps = _snapshots(samples, ALL_COUNTERS)
    if now is None:
        now = snaps[-1][0] if snaps else 0.0
    sl = _slice(snaps, span_s, now)
    if not sl["samples"]:
        return {
            "rx": UNKNOWN, "wire_lost": UNKNOWN, "wire_pct": UNKNOWN,
            "final_lost": UNKNOWN, "final_pct": UNKNOWN,
            "up_rx": UNKNOWN, "up_lost": UNKNOWN, "up_pct": UNKNOWN,
            "recovery_pct": UNKNOWN,
            "span_s": 0.0, "covered": False, "samples": 0,
        }
    d = sl["deltas"]
    wire_lost, final_lost, up_rx, up_lost = d[WIRE], d[FINAL], d[UP_RX], d[UP_LOST]
    rx = d[RX]
    return {
        "rx": rx,
        "wire_lost": wire_lost,
        "wire_pct": _pct(wire_lost, rx),
        "final_lost": final_lost,
        "final_pct": _pct(final_lost, rx),
        "up_rx": up_rx,
        "up_lost": up_lost,
        "up_pct": _pct(up_lost, up_rx),
        "recovery_pct": recovery_pct(wire_lost, final_lost),
        "span_s": round(sl["span_s"], 3),
        "covered": sl["span_s"] >= span_s - 1e-6,
        "samples": sl["samples"],
    }


def totals(samples: Sequence[object]) -> dict:
    """累计（自流开始）丢包统计 —— 长时间口径，进日志与底栏。"""
    snaps = _snapshots(samples, ALL_COUNTERS)
    if not snaps:
        return {"rx": 0, "wire_lost": 0, "final_lost": 0, "wire_pct": UNKNOWN,
                "final_pct": UNKNOWN, "up_lost": 0, "up_pct": UNKNOWN,
                "recovery_pct": UNKNOWN}
    first, last = snaps[0][1], snaps[-1][1]
    # 累计差同样要防复位：从 **最早一条** 起算，而不是取绝对末值 ——
    # 否则一次重启就把累计丢包清零。
    rx = _delta(last[RX], first[RX])
    wire_lost = _delta(last[WIRE], first[WIRE])
    final_lost = _delta(last[FINAL], first[FINAL])
    up_rx, up_lost = _delta(last[UP_RX], first[UP_RX]), _delta(last[UP_LOST], first[UP_LOST])
    return {
        "rx": rx,
        "wire_lost": wire_lost,
        "final_lost": final_lost,
        "wire_pct": _pct(wire_lost, rx),
        "final_pct": _pct(final_lost, rx),
        "up_lost": up_lost,
        "up_pct": _pct(up_lost, up_rx),
        "recovery_pct": recovery_pct(wire_lost, final_lost),
    }


def recovery_pct(wire_lost: float, final_lost: float) -> float:
    """两级恢复的救回比例（%）。无线上丢包时为 ``UNKNOWN``。"""
    if wire_lost is None:
        return UNKNOWN
    w = float(wire_lost)
    if w <= 0.0:
        return UNKNOWN
    f = 0.0 if final_lost is None else float(final_lost)
    return round(100.0 * (w - f) / w, 1)


def series(samples: Sequence[object], span_s: float = SPAN_S,
           bucket_s: float = BUCKET_S, metric: str = "final",
           now: float | None = None) -> list[tuple[float, float]]:
    """等宽分桶的丢包率时间序列，给面板画细条曲线。

    返回 ``[(桶结束时刻, 丢包率%), ...]``，**最老在前**（与 ``latency_panel``
    里延迟曲线 ``pts`` 的顺序一致：``xs`` 从左到右 = 从老到新）。
    桶内无数据的桶给 ``UNKNOWN``，面板会断开而不是画成 0 —— 把「没数据」画成
    0% 是这类图最常见的撒谎方式。

    刻意复用 :func:`window` 的算术（而不是手写前缀和）：一帧
    O(桶数 × 样本数) ≈ 7200 次浮点减法，2 Hz 重绘完全无感，而正确性只有一份实现。
    """
    if bucket_s <= 0.0:
        raise ValueError("bucket_s 必须为正")
    if metric not in ("wire", "final"):
        raise ValueError("metric 只支持 wire / final")
    lost_idx = WIRE if metric == "wire" else FINAL
    snaps = _snapshots(samples, DOWNSTREAM_COUNTERS)
    if not snaps:
        return []
    if now is None:
        now = snaps[-1][0]
    end_t = min(now, snaps[-1][0])
    start_t = end_t - span_s

    out: list[tuple[float, float]] = []
    t = start_t + bucket_s
    while t <= end_t + 1e-9:
        lo = t - bucket_s
        seg = [s for s in snaps if lo < s[0] <= t + 1e-9]
        if len(seg) < 2:
            out.append((t, UNKNOWN))
        else:
            base, newest = seg[0], seg[-1]
            lost = _delta(newest[1][lost_idx], base[1][lost_idx])
            rx = _delta(newest[1][RX], base[1][RX])
            out.append((t, _pct(lost, rx)))
        t += bucket_s
    return out

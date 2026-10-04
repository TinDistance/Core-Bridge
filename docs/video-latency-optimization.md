# K230 / 服务端视频与控制链路：问题分析与优化方案

> 目标：可用带宽 5–10 Mbps，720p–1080p @30fps，端到端（玻璃到玻璃）延迟 ≤ 100 ms。
> 范围：`k230/rtp_push.py`、`server/*`；因渲染路径在关键延迟路径上，附带分析 `desktop/streaming/h264_viewer.py`、`desktop/modules/stream_panel.py`、`desktop/gamepad/pusher.py`。
> 本文只做分析与设计，**未修改任何源码**。

---

## 0. 结论摘要

1. 现有方案按 15 Mbps 链路设计，用 3 份冗余拷贝对抗丢包。在 5–10 Mbps 下它是根本性错误：默认 `BIT_RATE=4200 kbps × 3 拷贝 = 12.6 Mbps` 载荷，线上约 13 Mbps，超过 10 Mbps 上限。链路被打满后，发送排队会直接转化为延迟。
2. 有三个正确性 bug，与带宽无关：
   - **服务端中继线程会因 Windows 的 UDP `ConnectionResetError` 永久退出**（已复现）。它会同时带走视频和控制链路。
   - **K230 的 `overfps` 闸门会静默丢弃已编码的 P 帧**，造成参考链断裂和花屏。
   - **手柄心跳周期（0.5 s）等于服务端过期阈值（0.5 s）**，保持输入不变时指令流会间歇中断。
3. 延迟被一批「实现层」问题吃掉，不是网络本身。Windows 上 `Event.wait` / 带超时的 `recv` 实测按约 15.6 ms 取整；接收端等待乱序 60 ms；IDR 被拉长到 300 ms 发送；Tk 33 ms 轮询。叠加起来，这些就能耗尽 100 ms 预算。
4. 单链路 + **NACK 重传（可选 XOR FEC）+ 长 GOP + 按带宽分档的固定码率**，才是 5–10 Mbps 下的正确组合。
5. **诚实评估**：720p 可以把 p99 压进 100 ms。1080p 在 IDR 和丢包恢复时必然冲破 100 ms，因为一个 IDR 就有 75–150 KB，串行化时间超过预算。1080p 只能保证 p50/p95，不能保证 p99（见 §6.3）。

---

## 1. 约束与预算

### 1.1 带宽预算（假设「5–10 Mbps」是 K230 与服务端之间的应用层可用吞吐）

线上速率 = 视频码率 × (1 + 40/1200) × 1.1，其中 40 B 是 IP/UDP/RTP 头，1.1 是为 NACK 重传和 FEC 预留的 10%。规则：**线上速率 ≤ 可用带宽的 70%**，剩下的 30% 吸收 WiFi 抖动。

| 档位 | 分辨率/帧率 | 视频码率 | 平均帧大小 | 包/秒 (1200B) | 线上速率 | 要求可用带宽 |
|---|---|---|---|---|---|---|
| T1 | 720p30 | 3000 kbps | 12.5 KB | ~312 | ~3.4 Mbps | ≥ 5 Mbps |
| T2 | 720p30 | 4500 kbps | 18.8 KB | ~469 | ~5.1 Mbps | ≥ 7.5 Mbps |
| T3 | 1080p30 | 6000 kbps | 25.0 KB | ~625 | ~6.8 Mbps | ≥ 10 Mbps |
| 现状 | 720p30 | 4200 kbps × 3 | 17.5 KB × 3 | ~1180 | ~13.0 Mbps | 15 Mbps（假设） |

三档的每像素码率约 0.10–0.11 bpp，H.264 对低运动场景（机器人第一视角）够用。

### 1.2 延迟预算（设计目标，**不是测量值**）

| 阶段 | 目标 (ms) | 依据 |
|---|---|---|
| 传感器曝光 + 读出 + ISP | ~33 | 1 帧间隔，未在设备上测量 |
| 硬件编码 | 8–20 | 未测量，需用 `pts` 与取流时间差实测 |
| K230 打包 + 发送 | ≤ 5（P 帧） | P 帧 ≤ 12 包直接突发发送，不节流 |
| WiFi 空口 | 2–8（p99 可到 30+） | 经验值 |
| 服务端中继 | ≤ 1（GIL 竞争下可能更高） | 见 S4 |
| 接收 + 重排 | 1–5 | 修复后 |
| 解码 | 1–2 | **已实测**（§附录 A） |
| 转换 + UI 唤醒 | ≤ 10 | 修复后 |
| 显示器扫描 | ≤ 16.7 | 60 Hz 显示器 |

典型总和约 75–85 ms。传感器和显示器合计固定吃掉约 50 ms，所以软件侧只有约 20–30 ms 余量，**任何一个 15 ms 量级的实现问题都会让预算失守**。

---

## 2. 问题清单

严重度：P0 = 正确性/可用性；P1 = 延迟；P2 = 带宽/丢包；P3 = 卫生。
证据等级：【实测】在本机跑过；【复现】用真实代码复现；【代码】读代码得出，未运行；【待验证】需要上设备。

### 2.1 服务端

| ID | 级别 | 问题 | 证据 |
|---|---|---|---|
| S1 | P0 | `RtpRelay._loop` 收到 `ConnectionResetError` 时 `return`，线程永久退出。Windows 上向已关闭端口 `sendto` 后，同一 socket 的下一次 `recvfrom` 会抛 WinError 10054。桌面端关闭/重启后，中继仍会向它扇出至多 `DOWNSTREAM_STALE_S = 10 s`，必然触发。 | 【复现】见 S1 详情 |
| S2 | P0 | S1 的级联效应：`command_udp` 的目标端点只由 `note_video_sender()` 学习，而它只在 `_on_rtp` 里被调用。中继线程一死，端点 2.5 s 后过期，**控制链路一起停止**。机器人在视频消失的同时失去控制。 | 【代码】 |
| S3 | P0 | `GET/POST /command` 的新鲜度判定：`COMMAND_STALE_S = 0.5`，而桌面端 `HEARTBEAT_S = 0.5`。输入不变时（打满摇杆到机械限位、按住方向键），客户端每 0.5 s 才重发一次，服务端同时在 0.5 s 判过期，指令流会间歇中断；过期分支还会清零迟滞状态。 | 【代码】 |
| S4 | P1 | 中继在 Python 线程中运行，与 uvicorn 事件循环、`command-udp` 线程、日志线程争用 GIL。默认切换间隔 5 ms，CPU 密集线程会让中继最多等 5 ms。此外 `_on_rtp` 每包做一次 `sock.getsockname()` 系统调用和 3 次加锁。 | 【代码】 |
| S5 | P1 | `command_udp._run` 用 `_stop.wait(...)` 做 150 Hz 节拍。Windows 上 `Event.wait` 实测 6.67 ms 目标得到 p50 15.56 ms，实际约 64 Hz，抖动 16 ms。 | 【实测】 |
| S6 | P1 | `uvicorn.run(...)` 没有 `access_log=False`。手柄每次 `POST /command` 都向控制台写一行；`logs.py` 的「噪声过滤」只作用于广播 handler，不作用于 uvicorn 默认的控制台 handler。Windows 控制台同步写入慢，QuickEdit 选中文本还会整体挂起事件循环。 | 【代码】，uvicorn 默认日志行为 |
| S7 | P2 | 中继保留 `DOWNSTREAM_STALE_S = 10 s`，桌面端每 1 s 就 ping 一次。保留 10 s 只是在放大 S1 的触发窗口。 | 【代码】 |
| S8 | P3 | JPEG 链路（`video_hub.py`、UDP 8001、`/video/latest.jpg`、`/video/mjpeg`）仍随服务启动，但 K230 已不再发 JPEG。`CTRL_BITRATE` 被中继转发，K230 的 `CommandLink.pump` 只处理 `0x01`，该指令是死代码。 | 【代码】 |
| S9 | P3 | `_bytes_at` 的 `deque(maxlen=20000)` 在 5 s 窗口内、包速率 > 4000 pps 时被截断，`kbps` 会低估。 | 【代码】 |

**S1 详情（复现）**：用真实 `RtpRelay` 在 localhost 上做了这个序列：下游注册 → 上游发包，扇出正常 → 下游 socket 关闭 → 上游继续发 5 个包 → 检查线程。结果：

```
RTP relay recv stopped: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
relay thread alive after downstream vanished: False
```

### 2.2 K230（`k230/rtp_push.py`）

| ID | 级别 | 问题 | 证据 |
|---|---|---|---|
| K1 | P0 | **3 份冗余拷贝与 5–10 Mbps 不兼容**。`WIFI_LINK_KBPS=15000` 是硬编码假设，`frame_admission`、`Pacer` 的速率和 `BIT_RATE` 全部由它推导。真实链路 8 Mbps 时，一帧 17.5 KB × 3 = 52.5 KB 需要约 52 ms 空口时间，超过 33 ms 帧间隔，发送队列每帧累积，延迟无界增长。另外三份拷贝走同一块无线电，WiFi 的突发丢包高度相关，**冗余几乎不提供保护**，却付出了 3 倍带宽。 | 【代码】，算术 |
| K2 | P0 | `overfps` 闸门：`if ticks_diff(now_ms, last_sent_ms) < FRAME_PERIOD_MS: ReleaseStream; continue`。`FRAME_PERIOD_MS = 33`（整数），编码器输出间隔天然有 ±数 ms 抖动，到达间隔 < 33 ms 的帧会被**丢弃**。丢弃的是已编码帧，P 帧丢失 = 参考链断裂；该分支没有置 `need_resync`，而且丢帧不消耗 `seq`，接收端看不到缺口，**不会请求 IDR**，画面带着错误一直持续到下一个周期 IDR。代码注释自己也承认了「丢帧不消耗 seq」这一机制。 | 【代码】 |
| K3 | P0 | **WiFi 密码硬编码并已提交进仓库**（`WIFI_SSID` / `WIFI_PASSWORD`）。注释声称可用环境变量覆盖，但 MicroPython/CanMV（rt-smart）通常没有 `os.environ`，覆盖逻辑被 `hasattr` 吞掉，形同虚设（未在设备上验证）。注释里提到的 `wifi.cfg` 没有实现。 | 【代码】 |
| K4 | P1 | **控制下行被视频发送阻塞**。主循环顺序是 `cmdline.pump` → `GetStream(timeout=20)` → 打包 → `send_paced`。`pump` 每帧才跑一次，且在 `send_paced` 节流期间（IDR 最多 `PACER_SPAN_MAX_MS = 300 ms`）完全不跑。控制指令在 K230 侧的等待时间：稳态最多一帧（33 ms），IDR 时最多 300 ms。 | 【代码】 |
| K5 | P1 | **IDR 被拉长到 300 ms 发送**（`PACER_SPAN_MAX_MS = 300`），`OUT_BUFS` 也按此取 12 个。这与 100 ms 预算直接冲突。同时 `GOP_LEN = 15`（每 0.5 s 一个 IDR）让这个问题每秒出现两次。设 IDR 约为 P 帧的 4 倍（假设，需用 `idrshare` 统计验证），GOP=15 时 IDR 占流量 4/18 ≈ 22%，GOP=60 时 ≈ 6%。 | 【代码】，算术 |
| K6 | P1 | **Pacer 速率高于真实链路**：`PACER_WIRE_KBPS = 15000 × 0.92 = 13.8 Mbps`。当链路只有 5–10 Mbps，节流器放行的速率比链路能吃的高，不起任何限流作用，排队转移到 WiFi 驱动里，更难观测。 | 【代码】 |
| K7 | P1 | 丢帧策略放大拥塞：`FRAME_DROP_DUTY` / `FRAME_DROP_STARVE` 丢 P 帧后要求 IDR，IDR 又是最大的帧，进一步拥塞，形成「丢帧 → IDR → 更拥塞 → 再丢帧」的正反馈。`IdrGate` 的 400 ms 限频只是限制了频率，不是解了环。 | 【代码】 |
| K8 | P2 | 没有丢包恢复手段，除了「整帧作废 + 等 IDR」。 | 【代码】 |
| K9 | P2 | 没有可用的码率自适应：`ChnAttrStr` 在官方 VENC 文档里只暴露 `bit_rate` 构造参数，没有 `rc_mode`，也没有运行时改码率的接口；`CTRL_BITRATE` 在 K230 侧未实现。码率档位只能在创建编码器时确定。 | 【代码】+ [VENC 手册](https://www.kendryte.com/k230_canmv/en/main/api/mpp/k230_canmv_venc_module_api_manual.html) |
| K10 | P2 | 只有 720p（`WIDTH=1280, HEIGHT=720`），1080p 没有可配置入口。 | 【代码】 |
| K11 | P3 | 每包产生新对象：`bytes(data)`、`bytes(nalu)`、`header + bytes(...)`；`split_nalus` 用 `data.find` 反复扫描；`gc.collect()` 每 100 帧（约 3.3 s）才一次。MicroPython 的 GC 停顿随堆增大，可能产生数十 ms 的周期性卡顿。 | 【待验证】 |
| K12 | P3 | RTP 时间戳 = `frame_count × 3000`，不是采集时间；丢帧时计数不推进，时间戳被压缩。当前接收端不使用时间戳，但它让测量采集到显示延迟变得不可能。 | 【代码】 |
| K13 | P3 | `ensure_wifi` 断线重连最长阻塞 10 s（主循环里）。 | 【代码】 |

### 2.3 桌面端（关联项，在延迟关键路径上）

| ID | 级别 | 问题 | 证据 |
|---|---|---|---|
| D1 | P1 | 接收线程没解出帧时 `self._stop.wait(0.002)`，Windows 上实测约 15.5 ms。接收循环以 `settimeout(0.0)` 轮询，所以每个包平均多等约 8 ms，最多 16 ms。 | 【实测】 |
| D2 | P1 | `REORDER_S = 0.060`：序号出现空洞后等 60 ms 才宣布丢包，这一项就吃掉 100 ms 预算的 60%，然后还要等 IDR（K5）。 | 【代码】 |
| D3 | P1 | 丢一个包 → `wait_idr = True` → 丢弃所有后续 P 帧直到 IDR 到达。在帧级别，丢包对画面的影响被放大：1200 B 载荷下 T1 平均 11 包/帧，T3 平均 21 包/帧，独立丢包假设下的**含丢包帧比例**见 §6.1 表。丢包率 1% 时，720p 约 10% 的帧、1080p 约 19% 的帧会触发一次冻结 + IDR。 | 【代码】，算术 |
| D4 | P1 | UI 线程每帧：`img.copy()`（720p 0.94 ms / 1080p 2.27 ms）+ `img.resize()`（1080p→1280×720 bilinear **8.26 ms**）+ `PhotoImage`。解码线程里 `frame.to_image()` 又花 5.0 ms (720p) / 9.5 ms (1080p)。同样的 YUV→RGB + 缩放用 swscale `frame.reformat(...)` 只要 0.5–0.6 ms。 | 【实测】 |
| D5 | P1 | `after(33, _poll)` 轮询，Windows 上 Tk 的 `after` 同样受 15.6 ms 定时器粒度影响，帧在事件队列里平均多等 16 ms，最多 33–48 ms。 | 【代码】+ 【实测】（定时器粒度） |
| D6 | P1 | 手柄推送线程 `self._stop.wait(rest)` 目标 100 Hz，实测 `Event.wait` 取整到 15.6 ms，约 62 Hz。UI 上显示的 fps 会反映这个数。 | 【实测】 |
| D7 | P1 | `GamepadPusher` 用同步 `httpx.Client.post`，`POST_TIMEOUT = 0.3 s`。一次 POST 卡住，轮询线程就停 0.3 s。HTTP 走 TCP，WiFi 丢包时一个段的重传（Windows 最小 RTO 约 200–300 ms）会让**后续所有指令排队等待**，并且这发生的时机正好是视频同样变差的时候。 | 【代码】+ TCP 行为 |
| D8 | P2 | 迟到包/重复包没有「落后于 playhead」过滤：`pending[seq] = data` 之前只检查 `seq in pending`，但第一份拷贝已被 `pending.pop(playhead)` 消费，晚到的重复包会作为新条目存入且永不被消费。累积到 `BACKLOG_MAX_BYTES = 2 MB`（约 1700 包）会误触发 `trim_backlog` + IDR 请求。引入 NACK 重传后这个问题会更频繁，**必须先修**。 | 【代码】，未复现 |
| D9 | P3 | 解码器只设了 `flags=low_delay`，没有显式固定 `thread_type`。依赖 PyAV 默认（本机 av 17.1.0 默认为 `SLICE`）；`requirements.txt` 是 `av>=11.0`，若落到 FRAME 线程会引入 N−1 帧延迟。 | 【代码】 |

---

## 3. 目标架构

```
K230 ──UDP RTP/H.264(单端口,单拷贝)──▶ Server relay ──UDP──▶ Desktop viewer
  ▲                                         ▲   │                  │
  │◀─ CBR 控制: IDR / NACK(0x03) ───────────┘◀──┴──────────────────┘
  │
  └◀─ 命令帧(AA55) ◀─ Server ◀─ UDP 命令包(seq) ◀─ Desktop 手柄
```

要点：

1. **单链路单拷贝**，带宽全部给视频。
2. **按可用带宽分档的固定码率**（§1.1），启动时由配置确定，不做运行时自适应（K9：API 不支持）。
3. **长 GOP + 按需 IDR**，GOP = 60。IDR 只在新观看者加入、NACK 失败或解码错误时产生。
4. **NACK 重传**作为主要丢包恢复手段；**XOR FEC** 仅在实测丢包率 > 1% 或 WiFi 抖动大时启用（可选）。
5. **控制通道改 UDP**，服务端收到即转发，K230 在发送间隙持续泵控制 socket。
6. **事件驱动接收**，彻底去掉依赖 15.6 ms 定时器的轮询。

---

## 4. 修复方案（按文件）

### 4.1 服务端

**`server/rtp_relay.py`**（S1、S4、S7）

```python
DOWNSTREAM_STALE_S = 3.0          # 原 10.0；桌面端每 1s ping 一次

# start() 中建立 self._port_of = {sock: port}
# _loop 中：
for sock in readable:
    port = self._port_of[sock]
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except BlockingIOError:
            break
        except ConnectionResetError:     # Windows: 上一次 sendto 的 ICMP 不可达，非致命
            continue
        except OSError as e:
            if self._stop.is_set():
                return
            logger.warning("RTP relay recv error: %s", e)
            break                        # 不再 return
        self._on_datagram(data, addr, sock, port)   # 传入 port，去掉 getsockname()
```

另外：

- `ConnectionResetError` 必须写在通用 `OSError` 之前（它是 `OSError` 子类）。
- 在 `server/app.py` 的 `lifespan` 中加监督任务：每 2 s 检查中继线程，若已死则调用 `start_relay()` 重启并记录一条 error 日志（`RtpRelay.start()` 已支持死线程重建）。这样即便将来出现新的未知异常，也不会再静默失去视频和控制。
- 进程启动处调用 `sys.setswitchinterval(0.001)`，降低 GIL 切换等待（S4；效果待实测）。

**`server/command_udp.py`**（S3、S5）

- `COMMAND_STALE_S: 0.5 → 0.3`。
- 150 Hz 节拍的 `_stop.wait(...)` 改为 `time.sleep(...)`（实测 `time.sleep(6.67ms)` → p50 7.05 ms / max 7.85 ms），并在每轮检查 `_stop.is_set()`。
- 采用 §4.4 的 UDP 命令通道后，此线程退化为「活动时每 50 ms 重发最新帧」的保活，不再是 150 Hz 轮询。

**`server/app.py`**（S6）

```python
uvicorn.run("server.app:app", host=host, port=port,
            access_log=False, log_level="warning")
```

进程入口处启用高精度定时器（**未实测**，需验证）：

```python
import ctypes
ctypes.windll.winmm.timeBeginPeriod(1)
```

**S8 清理**：默认不启动 JPEG 监听（`CORE_BRIDGE_ENABLE_JPEG=1` 才启用）；删除或实现 `CTRL_BITRATE`。

### 4.2 K230（`k230/rtp_push.py`）

**阶段 0：只改默认值，风险最小**

```python
LINK_COPIES = 1
IDR_COPIES  = 1
RTP_PORTS   = (RTP_PORT,)        # 单端口
WIFI_LINK_KBPS = 8000            # 改为从配置读取，见下
BIT_RATE    = 3000               # T1；按 §1.1 分档
GOP_LEN     = 60
PACER_SPAN_MAX_MS = 80           # 原 300
```

`PACER_WIRE_KBPS` 改为 `WIFI_LINK_KBPS × 0.9`，`WIFI_LINK_KBPS` 必须是**实测可用带宽**，不能再是 15000。

**删除 `overfps` 闸门（K2）**：编码器自己已按 `dst_frame_rate` 出帧，整段 `if ticks_diff(...) < FRAME_PERIOD_MS` 删掉。如果确实需要限帧，只允许在 IDR 之前整 GOP 地丢，并必须置 `need_resync`。

**配置外置（K3）**：从 `/sdcard/tindistance.json` 读取，缺失则报错退出，不再有默认密码：

```json
{"ssid": "...", "password": "...", "server_ip": "192.168.137.1",
 "tier": "T1", "link_kbps": 8000}
```

同时**轮换已泄露的 WiFi 密码**，并考虑清理 git 历史。

**让主循环持续泵控制 socket（K4）**

```python
# 1. GetStream 超时缩短，pump 的执行频率从「每帧」变为「每 ≤4ms」
if encoder.GetStream(stream, timeout=4) != 0:
    cmdline.pump(sock)
    continue

# 2. Pacer 在每次 sleep 前后调用 idle 回调
class Pacer:
    def __init__(self, ..., idle=None):
        self.idle = idle
    def take(self, nbytes):
        ...
            if self.idle:
                self.idle()
            self.clock.sleep_us(wait_us)
```

`idle` 绑定为 `lambda: cmdline.pump(sock)`，这样节流 sleep 期间控制指令和 NACK 也在被服务。若固件支持 `_thread`（**待验证**），也可把控制泵放到独立线程。

**只对大帧节流（K5、K6）**：P 帧 ≤ 约 20 KB 时（≤ 约 17 包，空口 2–3 ms）直接突发发送，不做逐包 sleep；只有 IDR 和超大帧才按 `PACER_WIRE_KBPS` 的令牌桶摊开。这减少 rt-smart 上 `sleep_us` 的调用次数和不确定性，并且 P 帧的发送耗时从「按链路速率摊平」降到「几 ms」。

**打破丢帧正反馈（K7）**：`FRAME_DROP_STARVE` 时不再请求 IDR，改为「丢到下一个 IDR 为止」并记录统计，同时用 §4.3 的 NACK 把可恢复的丢包在接收端消化。只有解码器真正报错才由接收端请求 IDR。

**MTU（可选）**：`MAX_PAYLOAD` 1200 → 1400（FU-A 头 2 B + RTP 12 B + UDP/IP 28 B = 1442 < 1500），每帧包数减少约 14%，丢包放大效应相应降低。

**K12 时间戳**：RTP 时间戳改用采集时钟 `(time.ticks_ms() × 90) & 0xFFFFFFFF`（或确认 `stream.pts` 单位后使用），丢帧不再压缩时间戳。

**K11 GC（待设备验证）**：先实测 `gc.collect()` 耗时；若 > 5 ms，改为每约 10 帧在帧间空闲期做一次小回收。包缓冲改为预分配 `bytearray` + `memoryview` 切片，`sendto` 直接传 `memoryview`（现有代码已有 `TypeError` 兜底）。

### 4.3 丢包恢复：NACK（必做）与 FEC（可选）

**NACK 协议**（复用现有 `CBR` 控制通道，中继已经会把控制包转发给上游）：

```
'C' 'B' 'R' 0x03 | count(u8) | seq(u16 BE) × count      count ≤ 32
```

**接收端（`h264_viewer.py`）**

1. 在收包循环里发现序号空洞后，**等 3–5 ms**（吸收 WiFi 轻微乱序）立即发 NACK；25 ms 后若仍缺，重发一次。
2. `REORDER_S: 60 ms → 40 ms`，超过即宣布丢失并走现有 `declare_loss`（→ 等 IDR）。
3. **先修 D8**：在 `pending[seq] = data` 前丢弃落后于 playhead 的包：

```python
if ((seq - playhead) & 0xFFFF) >= 0x8000:   # 已经过期（含重复/迟到的重传）
    self._dup += 1
    continue
```

4. NACK 发送限速：每个空洞最多 2 次，每秒总量设上限，防止拥塞时 NACK 风暴。

**K230 端**

- 保留最近 128 个包的环形缓冲（引用已构造好的 `bytes`，不拷贝）。1080p 6 Mbps 时 128 包 ≈ 200 ms，足够覆盖 NACK 往返；内存约 150 KB。
- 环形缓冲存 `(seq, pkt)`，收到 NACK 时校验 `ring[seq & 127][0] == seq`，命中则用同一 socket、同一目的地重发。
- 重传带宽限制在码率预算的 15% 以内。
- NACK 由 §4.2 的 `idle` 回调在节流 sleep 期间服务，所以不会因为大帧发送而被饿死。

**NACK 时间预算**（局域网 WiFi，估算）：检测 5 + 上行（经中继）2–10 + K230 响应 ≤ 4 + 下行 2–10 ≈ 15–30 ms。落在 40 ms 重排上限以内，恢复成功时画面**不冻结、不请求 IDR**。WiFi 突发重传（p99 50–100 ms）会超时，此时回退到 IDR。

**XOR FEC（可选，第二阶段）**：每帧 N 个包按奇偶序号分成 2 组，每组加 1 个 XOR 校验包（PT=97，头里带 `base_seq / count / 组号`），每组可恢复 1 个丢包，开销约 `2/N`（T1 约 18%，T3 约 10%）。仅当实测丢包率持续 > 1% 且 NACK 命中率低时启用，此时需要把 §1.1 的 10% 预留相应调高并降一档码率。

### 4.4 命令通道

**方案 A（推荐）：桌面 → 服务端改 UDP**

- 包格式：`'CBC' | ver(u8) | seq(u32) | ts_ms(u32) | raw16`（共 24 B）。
- 桌面：输入变化时立即发；无变化时每 50 ms 保活一次，**取代 0.5 s 的 HTTP 心跳**，这直接解决 S3。
- 服务端：新增 UDP 监听（如 `CORE_BRIDGE_CMD_UDP_PORT=8010`）。收到包 → 比对 `seq`（丢弃乱序旧包）→ 立即 `build_uart_frame` → 转发给已学习的 K230 端点；同时刷新「最新状态」供保活线程重发。300 ms 没有新包则停止发送（「静默 = 停车」语义保持不变）。
- 单个包丢失由 50 ms 保活自愈，不再有 TCP 队头阻塞（D7）。
- 保留 HTTP `GET /command` 供面板调试读取，`POST /command` 保留兼容。

**方案 B（最小改动，保留 HTTP）**

- `HEARTBEAT_S 0.5 → 0.1`；`COMMAND_STALE_S 0.5 → 0.3`。
- `GamepadPusher` 的轮询线程和 POST 解耦：轮询线程只更新「最新状态」，另起发送线程做 POST，避免 0.3 s 超时卡住轮询。
- `Event.wait` 换成 `time.sleep`（D6）。

**控制路径时延对比**（估算）

| 阶段 | 现状 | 方案 A |
|---|---|---|
| 桌面轮询取整 | ≤ 16 | ≤ 10 |
| 桌面→服务端 | HTTP 1–5（+TCP 重传风险） | UDP < 1 |
| 服务端轮询 | ≤ 16 | 0（到达即转发） |
| WiFi | 2–10 | 2–10 |
| K230 泵 socket | ≤ 33（IDR 时 ≤ 300） | ≤ 4 |
| 合计（稳态） | ≈ 40–80 | ≈ 15–25 |

此外，K230 下行小包（40 B 量级、20–50 pps）会各自占用一次信道竞争，约占空口时间 1–3%，保活 50 ms 比 150 Hz 轮询小得多。

### 4.5 桌面接收与渲染

**`h264_viewer.py`**

```python
# 事件驱动：有包立即唤醒；无包最多等 20ms，且 select 不依赖 15.6ms 取整的 sleep
r, _, _ = select.select([sock], [], [], 0.02)
if r:
    # 非阻塞 drain（沿用现有循环）
    ...
```

- 去掉 `self._stop.wait(0.002)`（D1）；超时只用于周期性任务（ping、统计）。
- 解码器：

```python
codec = av.CodecContext.create("h264", "r")
codec.options = {"flags": "low_delay"}
codec.thread_type = "SLICE"        # 显式，避免 FRAME 线程引入 N-1 帧延迟（D9）
```

- **在解码线程里做缩放 + 色彩转换**（D4），目标尺寸由 UI 线程写入一个共享变量：

```python
img = frame.reformat(width=cw, height=ch, format="rgb24").to_ndarray()
```

  实测 swscale 0.5–0.6 ms，对比 `to_image()` 5.0 / 9.5 ms 加 UI 线程 `resize()` 8.3 ms。UI 线程只做 `PhotoImage` 的创建与绘制。
- `trim_backlog` 的 `BACKLOG_*` 参数在分档后重新按码率标定（例如以 `BACKLOG_KEEP_MS` 为准，不要以包数为准）。

**`stream_panel.py`**（D4、D5）

- `after(33, ...)` → `after(8, ...)`，配合进程启动时 `timeBeginPeriod(1)`（未实测）。
- `_draw_frame` 里的 `img.copy()` 仅在窗口缩放重绘需要缓存时做；事件驱动下帧已经是目标尺寸，不再需要每帧 `copy()`（720p 省 0.9 ms，1080p 省 2.3 ms）。

**`pusher.py`**（D6、D7）：`self._stop.wait(rest)` → `time.sleep(rest)`；若采用方案 A，`httpx` 发送换成 UDP `sendto`。

---

## 5. 档位与 GOP 取舍

| 项目 | 现状 | 建议 | 取舍 |
|---|---|---|---|
| GOP | 15（0.5 s） | 60（2 s） | IDR 流量占比从约 22% 降到约 6%（假设 IDR ≈ 4×P）；代价是 NACK 失败后最长要等 IDR 请求的往返 + 一个 IDR 的发送时间，而不是等最多 0.5 s 的周期 IDR |
| 冗余 | 3× 拷贝 | 1× + NACK | 带宽降到 1/3；对抗 WiFi 相关性突发丢包更有效 |
| P 帧发送 | 逐包节流 | ≤ 20 KB 突发 | 减少 `sleep_us` 依赖，P 帧发送耗时降到几 ms |
| IDR 发送窗口 | ≤ 300 ms | ≤ 80 ms | 超出部分记账为令牌债务，不再无限拉长 |
| 默认档位 | 720p / 4200×3 | T1 720p 3000 kbps | 5 Mbps 链路也有 30% 余量 |

---

## 6. 风险与诚实评估

### 6.1 丢包放大效应（独立丢包假设下的含丢包帧比例）

| 链路丢包率 | T1 (11 包/帧) | T3 (21 包/帧) |
|---|---|---|
| 0.5% | 5.4% | 10.0% |
| 1% | 10.5% | 19.0% |
| 2% | 19.9% | 34.6% |

真实 WiFi 丢包是突发的，同样的平均丢包率下含丢包帧比例会更低，但单次中断更长。**这张表说明为什么没有恢复手段就不可能稳定**，也说明为什么 NACK 命中率是整个方案的核心指标。

### 6.2 K230 侧不确定项（需上设备验证）

- 编码默认码控模式（CBR / VBR）以及 IDR 与 P 帧的实际大小比。**这决定 §5 的 IDR 占比估算是否成立**，现有统计行里的 `max=` 和 `idrshare=` 可直接读出。
- 硬件编码延迟、传感器到编码器的延迟。
- `time.sleep_us` 在 rt-smart 上的真实精度。
- `gc.collect()` 单次耗时。
- `_thread` 是否可用；Wi-Fi 省电模式是否可关闭。
- `ChnAttrStr` 在当前固件版本是否有文档外的运行时调码率接口。
- K230 Wi-Fi 模块的工作频段。如果只有 2.4 GHz，5–10 Mbps 上限与高抖动很可能来自信道干扰（推测，未验证），建议热点固定信道，并关闭笔记本网卡省电。

### 6.3 1080p 能否满足 100 ms

- 稳态 P 帧：25 KB 平均、约 21 个包，突发发送在 8–10 Mbps 链路上约 20–25 ms 空口时间，**p50 / p95 可达 100 ms 以内**。
- IDR：按 4× 估算约 100 KB，10 Mbps 下串行化就要约 80 ms，叠加其后排队的 P 帧，这段时间**必然超过 100 ms**。在 5–8 Mbps 下更严重。
- 结论：1080p 只能通过「极少产生 IDR」来降低超标概率（长 GOP、NACK 让丢包不触发 IDR），**无法保证 p99 ≤ 100 ms**。720p（T1/T2）是唯一有希望把 p99 控制在 100 ms 内的档位。**默认应为 720p，1080p 作为可选项**，并在验收中明确标注。

### 6.4 本次分析的范围边界

- 没有 K230 硬件、没有真实 WiFi 环境；上述时延数字只有「已实测」标注的部分来自本机。
- 未阅读 `desktop/server_manager.py`、`desktop/telemetry/latency_monitor.py`、`stream_panel.py` 第 265 行之后的部分、`start_desktop.bat`；`test/test_rtp_redundancy.py` 只读了前 83 行。默认值变更后，该测试里围绕多拷贝逻辑的用例（`test_frame_admission_*`、`test_bandwidth_budget_math` 等）需要逐个核对。
- D8 是读代码得出的推断，没有复现。

---

## 7. 实施顺序

**阶段 0：热修（几十行，立即做）**

1. S1：中继捕获 `ConnectionResetError`；`DOWNSTREAM_STALE_S = 3`；lifespan 加监督重启。
2. K2：删除 `overfps` 闸门。
3. K1：`LINK_COPIES = IDR_COPIES = 1`、单端口、`WIFI_LINK_KBPS` 改为实测值、`BIT_RATE = 3000`。
4. S3：心跳 0.1 s、过期 0.3 s。
5. S6：`access_log=False`。
6. K3：WiFi 凭据移出仓库并轮换。

**阶段 1：延迟（单项收益明确）**

1. 全局把短 `Event.wait` 换成 `time.sleep`；接收循环改 `select` 事件驱动（D1、D6、S5）。
2. K230 主循环 `GetStream(timeout=4)` + `Pacer.idle` 泵 socket（K4）。
3. `GOP_LEN = 60`、`PACER_SPAN_MAX_MS = 80`、P 帧突发发送（K5、K6）。
4. 桌面渲染：解码线程 `reformat`、`after(8)`、去掉每帧 `copy()`（D4、D5）。
5. 修 D8（过期包过滤）。

**阶段 2：丢包恢复与控制通道**

1. NACK（接收端 + 中继转发验证 + K230 环形缓冲）。
2. UDP 命令通道（方案 A）。
3. 视实测丢包率决定是否上 XOR FEC。

**阶段 3：分档与 1080p**

1. 配置化分档（T1/T2/T3）。
2. 1080p 入口与实机验收（接受 §6.3 的限制）。

---

## 8. 验证与验收

### 8.1 测量方法

- **玻璃到玻璃延迟（真值）**：在摄像头前放高刷新率屏幕显示毫秒计时器，同一画面里同时拍下计时器和桌面端渲染窗口（手机连拍或高速录像），读两个时间差，≥ 30 次，统计 p50 / p95 / p99。
- **分段计时**：K230 打印 `取流时间 − pts`（编码延迟）；桌面端沿用现有 `render_age_ms`（注意它从 AU 首包到达算起，**不含**采集和编码）；`CBR` 的 PING/PONG 让 PONG 回带服务端时间戳以测 RTT。
- **丢包与恢复**：NACK 发出数 / 命中数、`loss_events`、IDR 请求数、每分钟冻结次数。
- **控制延迟**：手柄状态变化到 K230 UART 输出之间的时间，K230 打印收到时间，桌面端 `client_ts` 对时。

### 8.2 验收标准（建议）

| 指标 | T1 / T2 (720p) | T3 (1080p) |
|---|---|---|
| 玻璃到玻璃 p50 | ≤ 80 ms | ≤ 90 ms |
| 玻璃到玻璃 p95 | ≤ 100 ms | ≤ 110 ms |
| 玻璃到玻璃 p99 | ≤ 100 ms | 仅记录，不作硬指标（§6.3） |
| 稳态帧率 | ≥ 29.5 fps | ≥ 29.5 fps |
| 线上速率 | ≤ 可用带宽 70% | ≤ 可用带宽 70% |
| 1% 丢包下每分钟冻结次数 | ≤ 2 | ≤ 4 |
| 控制延迟 p95 | ≤ 30 ms | ≤ 30 ms |
| 桌面重启 / 关闭后中继存活 | 必须存活，视频 + 控制自动恢复 | 同左 |

### 8.3 回归测试建议

- 把 S1 复现脚本整理为 pytest：下游关闭后继续发包，断言中继线程存活，且新下游注册后扇出恢复。
- 为 NACK 编解码、环形缓冲命中/未命中、过期包过滤（D8）加单元测试。
- 可借助现有 `test_rtp_redundancy.py` 的 micropython stub 和 `FakeClock`，模拟「P 帧到达间隔 32 ms 不被丢弃」（K2）。
- 阶段 0 完成后，核对 `test_rtp_redundancy.py` 第 84 行之后的断言是否依赖旧默认值。

---

## 附录 A：本次实测数据

环境：Windows，Python 3.12.13，PyAV 17.1.0，16 个逻辑线程。测试码流由 libx264（ultrafast、baseline、zerolatency、GOP 30、无 B 帧）对合成噪声内容编码得到，**不是 K230 硬件码流**。解码和转换耗时对内容不敏感，可作量级参考，但不应当作 K230 码流的精确值。

**A.1 定时器粒度**

| 操作 | 目标 | p50 | p95 | max |
|---|---|---|---|---|
| `Event.wait` | 6.67 ms | 15.56 ms | 16.15 ms | 16.62 ms |
| `time.sleep` | 6.67 ms | 7.05 ms | 7.22 ms | 7.85 ms |
| `Event.wait` | 2 ms | 15.50 ms | 16.15 ms | 16.50 ms |
| `time.sleep` | 2 ms | 2.17 ms | 2.53 ms | 3.07 ms |
| `sock.recv`（超时 2 ms，无数据） | 2 ms | 15.33 ms | 16.06 ms | 16.66 ms |

结论：`Event.wait` 与带超时的 socket 等待在 Windows 上按约 15.6 ms 取整；`time.sleep`（Python 3.11+ 高精度定时器）接近目标。有数据到达时，阻塞 `recv` / `select` 会立即返回，不受该粒度影响。

**A.2 UDP ICMP 回灌**

向已关闭的本机 UDP 端口 `sendto` 后，同一 socket 的 `recvfrom` 抛出 `ConnectionResetError`（`isinstance(e, BlockingIOError)` 为 False）。用真实 `RtpRelay` 复现：下游消失后中继线程退出。

**A.3 解码与转换**

| 项目 | 720p | 1080p |
|---|---|---|
| 解码（低延迟，默认多线程）p50 / p95 / max | 0.22–0.26 / 0.31–0.34 / 1.0–1.5 ms | 0.39–0.41 / 0.56–0.87 / 2.4–2.8 ms |
| 解码（`thread_count=1`）p50 / p95 / max | 0.55 / 0.75 / 6.35 ms | 0.98 / 1.71 / 12.57 ms |
| 首帧输出延迟（以包数计） | 0 包（无解码器内部缓帧） | 0 包 |
| `frame.to_image()` p50 / p95 | 5.0 / 5.5 ms | 9.5 / 10.4 ms |
| UI 线程 `img.copy()` p50 | 0.94 ms | 2.27 ms |
| UI 线程 `resize` →1280×720 bilinear p50 | 0.96 ms（同尺寸） | 8.26 ms（p95 9.67 ms） |
| swscale `reformat` yuv420p→rgb24 1280×720 p50 / p95 | 0.50 / 0.68 ms | 0.60 / 1.00 ms |

结论：解码不是瓶颈（≤ 2 ms）；`to_image()` + UI 缩放才是桌面端的主要开销，用 `reformat` 可降低一个数量级。低延迟模式下解码器没有额外缓帧，符合 baseline 无 B 帧的预期。

## 附录 B：参考

- [VENC Module API Manual — CanMV K230](https://www.kendryte.com/k230_canmv/en/main/api/mpp/k230_canmv_venc_module_api_manual.html)：`ChnAttrStr` 仅有 `bit_rate / gopLen / src_frame_rate / dst_frame_rate` 等参数，`Encoder.RequestIDR()` 为异步请求，接收端仍需 SPS/PPS 才能从 IDR 起解。

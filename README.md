# Core-Bridge

FF校内赛-TinDistance队电脑核心控制端。

一套「K230 摄像头机器人 → 局域网服务器 → 桌面操作台」的低延迟图传 + 遥控系统：K230 硬件编码 H.264 通过**单链路** RTP 推流（丢包靠 NACK 重传而非冗余拷贝），服务器中继转发，桌面端解码显示并以手柄遥控行走/云台，K230 经 UART3 下发指令给底盘 MCU。

## 目录结构

```
Core-Bridge/
├── server/        # FastAPI 中继服务器（HTTP/WS + RTP/JPEG 中继 + 反向控制）
├── desktop/       # Tkinter 桌面操作台（视频墙、手柄、遥测、日志）
├── k230/          # K230 MicroPython 端（采集/编码/RTP 推流/指令接收）
├── test/          # pytest 回归（线路契约、丢包窗口、V1.3 坦克串口）
├── docs/          # 设计文档（延迟优化方案、丢包遥测口径）
├── start_desktop.bat  # 一键启动桌面端（python -m desktop.app）
└── requirements.txt
```

## 系统架构总览

```
┌─────────── K230 (MicroPython) ───────────┐
│ Sensor(1920x1080) → H.264 硬编(6Mbps,     │
│ GOP60=2s, 30fps) → RTP 分包(FU-A, ≤1400B) │
│ → airtime 令牌桶限速 → 单链路单拷贝        │
│ → UDP 发往服务器 8002                     │
│                                          │
│ CommandLink: 同一 UDP socket 收 AA 55     │
│ UART帧 → 写 UART3(115200) → 底盘 MCU      │
│ (200ms 喂狗超时自动停车)                   │
└────────────────┬─────────────────────────┘
                  │ UDP: RTP video + CBR/NACK 控制
                  ▼
┌─────────── Server (FastAPI :8000) ───────┐
│ rtp_relay: 8002/8003/8004 收 RTP,          │
│   学习上游, 扇出到所有注册下游 + 控制包转发│
│   (NACK/IDR 转发上游, 统计上游缺口)        │
│ relay_supervisor: 中继线程 2s 内自动重启  │
│ command_udp_in: 8010 收桌面 CBC 包,       │
│   到达即转发(不再等轮询)                   │
│ command_udp: 反向控制推 UART 帧到          │
│   「视频源地址」, 30Hz 兜底 + 0.3s 过期     │
│ video_hub: 8001 收 JPEG 分块(默认关闭)     │
│ routers: /ping /command /video/* /logs    │
│ ws /ws/logs: 日志实时下发                  │
│ lifespan: GIL 1ms + 定时器 1ms 粒度调优    │
└────────────────┬─────────────────────────┘
                  │ UDP 8010 命令 + RTP 下行
                  ▼
┌─────────── Desktop (Tkinter 操作台) ──────┐
│ ServerManager: 子进程拉起 uvicorn            │
│ H264Viewer: 绑定临时端口收 RTP,             │
│   select 事件驱动 → RFC6184 解包 → PyAV     │
│   解码 → swscale 缩放 → 画布(UI 线程零变换)│
│   NACK 重传 / IDR 请求 / PING 捎带          │
│ GamepadPusher: XInput ~100Hz 轮询(无 IO)   │
│   → 发送线程变更即发 → UDP 8010 (28B CBC)   │
│   HTTP POST /command 降级                   │
│ LatencyMonitor + loss_window: 延迟 p50/p95 │
│   + 两级丢包口径窗口统计                     │
│ Viewer(JPEG HTTP 轮询) 备用视频              │
│ StartScreen / StreamPanel / LatencyPanel   │
│ / GamepadPanel / LogPanel / 底状态条        │
└──────────────────────────────────────────┘
```

## 端口与协议

| 端口/链路 | 协议 | 用途 |
|---|---|---|
| 8000 | HTTP / WebSocket | FastAPI 控制面（`/ping`、`/command`、`/video/*`、`/ws/logs`） |
| 8001 | UDP | JPEG 分块视频上行（**默认关闭**，`CORE_BRIDGE_ENABLE_JPEG=1` 打开），喂 `video_hub` |
| 8002 | UDP | H.264 裸 RTP 上行（K230 单链路单拷贝实际使用的端口，`RTP_PORT` 可覆盖） |
| 8003/8004 | UDP | 备用 RTP 端口（服务端默认监听，`CORE_BRIDGE_RTP_PORTS` 可裁剪） |
| 8002+ | UDP | RTP 扇出下行到桌面 H264Viewer（viewer 绑定 `0.0.0.0:0` 临时端口并上报），控制包走同链路 |
| 8010 | UDP | 桌面 → 服务端命令通道：`CBC` + ver(u8) + seq(u32 BE) + ts_ms(u32 BE) + raw16 = 28 字节 |
| 115200 | UART3 | K230 → 底盘 MCU：V1.3 固定 15 字节 `AA 55 TRACK_L(i16BE) TRACK_R(i16BE) YAW PITCH REAR FIRE ESTOP CRC16-CCITT-FALSE(BE) 0D 0A` |

`CBR` 控制包（K230 ↔ 中继/桌面共用）：`b"CBR" + type(u8)`，`0x00 PING` / `0x10 PONG` / `0x01 IDR 请求` / `0x02 BITRATE` / `0x03 NACK`（`+count(u8) + seq(u16 BE)*count`，count ∈ [1,32]，长度必须恰好 `5+2*count`）。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CORE_BRIDGE_HTTP_HOST` / `CORE_BRIDGE_HTTP_PORT` | `0.0.0.0` / `8000` | 控制面监听 |
| `CORE_BRIDGE_VIDEO_UDP_PORT` | `8001` | JPEG 上行端口 |
| `CORE_BRIDGE_RTP_PORTS`（别名 `CORE_BRIDGE_RTP_UDP_PORT`） | `8002,8003,8004` | RTP 中继监听端口 |
| `CORE_BRIDGE_CMD_UDP_PORT` | `8010` | 桌面命令 UDP 端口 |
| `CORE_BRIDGE_CMD_UDP` | `1` | 桌面端关掉后回退 HTTP `POST /command` |
| `CORE_BRIDGE_CMD_FALLBACK_HZ` | `30` | HTTP 路径兜底推送频率（夹取到 30–300 Hz） |
| `CORE_BRIDGE_ENABLE_JPEG` | `0` | 打开 JPEG 备用链路 |

K230 端（也可写进 `/sdcard/tindistance.json`，优先级 环境变量 > 配置文件 > 文件内常量）：`SERVER_IP`、`TIER`（`T1/T2/T3`）、`WIFI_LINK_KBPS`（默认 10000）、`RTP_PORT`。

## 视频档位

| 档位 | 分辨率 | 码率 | 要求实测可用带宽 |
|---|---|---|---|
| T1 | 1280×720 | 3000 kbps | ≥ 5000 kbps |
| T2 | 1280×720 | 4500 kbps | ≥ 7500 kbps |
| **T3（默认）** | **1920×1080** | **6000 kbps** | **≥ 10000 kbps** |

带宽不足时只警告不阻断。节流池 = `WIFI_LINK_KBPS × PACER_DUTY(0.9)`。

## 数据流详解

### 1. 视频链路（主：H.264/RTP）

1. **K230 `k230/rtp_push.py`**：摄像头 → 硬件 H.264 编码（`gopLen=60`）→ Annex-B 拆 NALU → RTP FU-A 打包（SSRC `0x54494E44` "TIND"，载荷 ≤1400B）→ airtime 令牌桶 pacer 按实测链路带宽限速（整帧 ≤20KB 时突发直发，不逐包 sleep）→ **单端口单拷贝**发往 8002。
   - **为什么不再多链路冗余拷贝**：旧设计按 15 Mbps 链路做 3 份拷贝（4.2 Mbps × 3 ≈ 13 Mbps 线上）。在 5–10 Mbps 链路上排队直接转成无界延迟，而且三份拷贝走同一块无线电，WiFi 突发丢包高度相关，冗余几乎不提供保护。带宽全部给视频，丢包交给 NACK 重传。
2. **服务端 `rtp_relay.py`**：每个端口独立学习上游，扇出到所有注册下游；转发 `CBR` 控制包（IDR 请求、NACK 回上游）；统计上游序号缺口（区分乱序/dup 与真回退，`SEQ_RESET_DISTANCE=0x8000`）。中继线程被未知异常打死时由 `relay_supervisor.py` 在 2s 内重启。
3. **桌面 `desktop/streaming/h264_viewer.py`**：`select` 事件驱动接收（有数据立即返回，不受 Windows 15.6ms 定时器粒度影响）→ 序号空洞 **4ms** 后发 NACK，未补上 **25ms** 后重发一次（每洞最多 2 轮，总量限速 60 seq/s）→ 空洞过 `REORDER_S=40ms` 仍缺才判丢 → RFC 6184 解包（FU-A/STAP-A）→ PyAV decode（固定 `thread_type=SLICE`，避免解码器自带 N−1 帧缓冲）→ swscale `reformat` 缩放 → 送 UI 线程。积压队列以**时间**为准（≤500ms，保留 150ms），字节上限按实测 bps 动态计算。
4. **NACK 重传（K230 侧）**：桌面报回的 seq 命中环形缓冲就用同一 socket、同一目的地重发原始字节；重传带宽 ≤ 码率预算的 15%，窗口 1000ms。

### 2. 视频链路（备：JPEG/HTTP，默认关闭）

K230 JPEG 分块（header `>HBBHHHH`，块 ≤1200B）→ UDP 8001 → `video_hub` 重组（≤512 块、≤1MiB、0.6s 超时）→ 桌面 `viewer.py` 轮询 `GET /video/latest.jpg?since=<id>`（304 跳过），或浏览器看 `/video/mjpeg?fps=10`。`CORE_BRIDGE_ENABLE_JPEG=1` 才监听。

### 3. 指令链路（手柄 → 机器人）

1. **采集**：`desktop/gamepad/xinput.py`（ctypes，4 槽位）→ 16 字节协议帧（lx/ly/rx/ry u16 偏移码，lt/rt 0–1023）。轮询线程 `gamepad-poll` ~100Hz **只读手柄、只更新最新状态，不做任何 IO**。
2. **语义化**：`desktop/gamepad/commands.py` → V1.3 坦克语义：左摇杆差速混合出 TRACK_L/R（±100），右摇杆阈值 ±30 出炮塔持续旋转（0/1/2），A=开火（FIRE 上升沿），B=后臂顺时针(1)/Y=逆时针(2)，十字键右按住 2s 锁存急停（左键/Start 解除）。UI 在手柄面板显性展示当前命令（履带/炮塔/后臂/开火/急停进度）。
3. **发送**：`GamepadPusher` 发送线程 **20Hz 固定节拍**（50ms）下发，变化立即发、无变化到节拍保活；封包 `CBC|ver|seq|ts_ms|raw16`（28B）走 UDP 8010。`ts_ms` 是客户端进程启动以来的毫秒数，**只用于面板调试，不参与新鲜度判断**，无需对时。语义 `TANK` 经 HTTP 降级进 `cmds`，UDP 主通道服务端走 raw 兜底重算，桌面端是唯一语义来源。关掉 `CORE_BRIDGE_CMD_UDP` 时降级走 HTTP `POST /command`（30Hz 兜底）。
   - 相比 HTTP 的收益：去掉服务端轮询等待（150Hz 平均也只省 6.7ms，Windows 上 `Event.wait` 还按 15.6ms 取整）和 TCP 队头阻塞（一次重传 200–300ms 会把后续指令一起排队，且恰好发生在 WiFi 抖动时刻）。控制路径稳态估算从 ≈40–80ms 降到 ≈15–25ms。
4. **服务端 `command_udp_in.py` → `command_udp.py`**：8010 收包解析后**到达即转发**（上限 50Hz），写入 store；`command_udp` 编码 V1.3 固定 15 字节帧（含 CRC16-CCITT-FALSE + `0D 0A`）推给「从视频包学到的 K230 地址」。快照 >0.3s 不发（`COMMAND_STALE_S`），V1.3 固定 20Hz 节拍不断流（静止回中帧照发，靠 MCU 200ms 串口超时停车）。
   - ⚠️ **上线耦合**：桌面端 50ms 心跳与服务端 0.3s 过期阈值必须同批上线，否则旧客户端会被一直判过期而完全失去控制。
5. **K230 `CommandLink.pump`** 校验帧格式 → 写 UART3 115200 → 底盘/云台 MCU 执行。

### 4. 心跳与安全

- `GET /ping`：桌面 RTT 测量与服务端健康检查。
- 服务端指令快照过期 0.3s 即停止下发 + K230 200ms 喂狗自停：双层失效安全。
- 手柄断开发中立帧，机器人立刻停止。
- K230 侧 WiFi 单次重连阻塞预算 3000ms；`server_ip` 三处都没配时**启动即报错退出**，绝不往网关发包（旧行为会「一直黑屏/一直重连」，排查成本全在猜地址上）。

### 5. 日志链路

服务器 logging → `LogHub`（500 行历史）→ `WS /ws/logs` → 桌面 `LogClient` → LogPanel；`DELETE /logs` 清空。服务器子进程 stdout 同时落盘 `logs/server.out`（8MB 轮转）。`uvicorn.access` 的 StreamHandler 在 lifespan 里被摘掉（面板的 BroadcastHandler 不受影响），否则每次 `POST /command` 都同步写 stderr。

### 6. 遥测（延迟 + 丢包）

`LatencyMonitor` 每 0.5s 轮询 `/video/rtp_status`（RTT）并合并 H264Viewer 与中继统计；采样线程的异常被隔离，不会带走探针线程，面板区分「探针故障」与「无信号」。

**丢包两级口径**（`desktop/telemetry/loss_window.py`，无 I/O 无 Tk 的纯函数模块，面板/探针/测试共用）：

| 口径 | 含义 |
|---|---|
| `wire`（线上，下游） | 接收端发现空洞时缺失的 RTP 序号数，**含**后来被 NACK 补回的 —— 链路真实丢过 |
| `final`（最终，下游） | 空洞过 40ms 重排窗口仍未补上、真正打到画面上的损伤 |
| `up`（上游 = K230→server） | 中继侧观测的序号缺口，**只有线上口径**：中继没有重排缓冲，补回的重传包在它看来只是乱序，无法与重复拷贝区分 |
| `recovery`（重传救回） | `(wire - final) / wire`，两级恢复实际救回的比例 |

分母统一 `lost + rx`（`rx` 是**入队**包数，已扣掉重复与过期）。窗口值 = 累计差，显式处理四个坑：计数器复位（负差按 0）、未上报样本（`-1` 整条丢弃）、窗口不满（返回真实 `span_s` 不外推）、跨度为零（返回无数据而非 0%）。累计适合日志，**10s 窗口驱动面板主数字与告警色**（`WARN_PCT=0.5`、`BAD_PCT=2.0`）；60s 序列按 1s 分桶画细条曲线，无数据的桶断开而不是画成 0%。

延迟头部 pill：`render_age` ≥500ms 显示「滞后高」，否则 LIVE。完整口径与阈值依据见 [`docs/packet-loss-telemetry.md`](docs/packet-loss-telemetry.md)。

## 各组件要点

### server/
- `app.py`：FastAPI 入口。进程调优（GIL 切换 1ms、Windows 定时器 1ms 粒度）放在 **lifespan** 而非 `main()`——生产启动路径是 `python -m uvicorn server.app:app`，`main()` 从不被调用，那里的设置是死代码。
- `rtp_relay.py`：裸 RTP 多端口扇出中继（端口元组形态保留给多链路），`SO_RCVBUF` 256KiB；不受 Windows ICMP 回灌影响而永久退出。
- `relay_supervisor.py`：中继线程 2s 看护重启。
- `command_udp_in.py`：桌面 → 服务端 UDP 命令通道（28B `CBC`；也兼容缺 `ts_ms` 的 24B 形式）。
- `command_udp.py`：反向控制推帧器（迟滞 active、0.3s stale 保护、端点学习）。
- `video_hub.py`：JPEG 分块重组（magic 0x4A50"JP"）。
- `routers/`：heartbeat(`/ping`,`/test`)、command、video（status/rtp_status/timing/latest.jpg/mjpeg）、logs。

### desktop/
- `app.py` / `start_desktop.bat`：入口 `python -m desktop.app`，1280×880 pit-wall 三段式（顶栏 / 左图传右遥测 / 手柄条 / 底状态条）。
- `server_manager.py`：子进程起 uvicorn、健康检查、遗留进程清理、日志轮转。
- `streaming/h264_viewer.py`：主视频链路（事件驱动接收 + NACK + 两级恢复 + 解码线程内缩放）；`streaming/viewer.py`：JPEG 备用。
- `gamepad/`：`xinput` 采集 → `commands` 语义化 → `pusher` 双线程推送（轮询无 IO / 发送按需）。
- `telemetry/loss_window.py`（丢包窗口算术）、`telemetry/latency_monitor.py`（探针）。
- `modules/`：stream / latency / gamepad / log 四面板。

### k230/
- `rtp_push.py`（MicroPython，唯一脚本，1553 行）：集成采集/硬编/RTP 打包/pacer/单链路推流/NACK 重传/指令接收（UART3 桥接）/IDR 门控；GC 每 100 帧，统计每 10s 打印。启动即打印 `[cfg]` 生效配置。

### docs/
- [`docs/video-latency-optimization.md`](docs/video-latency-optimization.md)：端到端延迟分析、瓶颈编号（D1–D9 / S1–S6 / A5）与各阶段改造方案。
- [`docs/packet-loss-telemetry.md`](docs/packet-loss-telemetry.md)：丢包遥测的口径定义、阈值依据与已知局限。

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`
2. 双击 `start_desktop.bat`（或 `python -m desktop.app`）：桌面端自动拉起服务器（端口 8000）。
3. K230 烧录 `k230/rtp_push.py`，**必须设置接收端地址**（改文件里的 `SERVER_IP`、或写 `/sdcard/tindistance.json` 的 `server_ip`、或环境变量 `SERVER_IP`——三处都没有就启动即报错），再确认 `TIER` 与 `WIFI_LINK_KBPS` 和实际链路匹配。上电自动推流至服务器并被桌面端接收。
4. 插入手柄即可遥控；`CORE_BRIDGE_ENABLE_JPEG=1` 后 `/video/mjpeg` 可供浏览器调试查看。

## 测试

```
pytest test -q          # 133 passed
```

| 文件 | 覆盖 |
|---|---|
| `test/test_rtp_redundancy.py` | frame_admission 冗余决策、pacer/airtime 计费、fanout 下发、真实 RtpRelay 本地集成（临时端口 + ping/pong/IDR 回路） |
| `test/test_k230_push_audit.py` | K230 发送侧：配置解析、tier 解析、IDR 门控、pacer 纯逻辑仿真、NACK 线路契约 |
| `test/test_loss_rate.py` | 丢包窗口算术（复位/未上报/不满窗/零跨度）、两级口径一致性、中继上游缺口与重启重置 |
| `test/test_desktop_wire_contracts.py` | 跨端线路契约：`CBC` 命令包、NACK 包、UART 帧字节布局 |

## 已知取舍

- **WiFi 凭据硬编码在仓库里**（`WIFI_SSID` / `WIFI_PASSWORD`）：团队已知并接受的风险；`/sdcard/tindistance.json` 可覆盖。
- **K230 依赖码流连续性**：长 GOP（2s）下若 NACK 与 IDR 都失败，最坏黑屏到下一个 IDR。
- **上游丢包只有线上口径**：中继无重排缓冲，无法分辨「重传补回」与「重复拷贝」，面板上该格已标注。
- **未实测的调优项**：`sys.setswitchinterval(0.001)` 与 `timeBeginPeriod(1)` 的收益未在本机量化，失败即静默退回默认。

## 许可

见 [LICENSE](LICENSE)。
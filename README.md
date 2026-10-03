# Core-Bridge

FF校内赛-TinDistance队电脑核心控制端。

一套「K230 摄像头机器人 → 局域网服务器 → 桌面操作台」的低延迟图传 + 遥控系统：K230 硬件编码 H.264 通过多链路冗余 RTP 推流，服务器中继转发，桌面端解码显示并以手柄遥控行走/云台，K230 经 UART3 下发指令给底盘 MCU。

## 目录结构

```
Core-Bridge/
├── server/        # FastAPI 中继服务器（HTTP/WS + RTP/JPEG 中继 + 反向控制）
├── desktop/       # Tkinter 桌面操作台（视频墙、手柄、延迟面板、日志）
├── k230/          # K230 MicroPython 端（采集/编码/RTP 推流/指令接收）
├── test/          # 单元与集成测试
├── start_desktop.bat  # 一键启动桌面端（python -m desktop.app）
└── requirements.txt
```

## 系统架构总览

```
┌─────────── K230 (MicroPython) ───────────┐
│ Sensor(1280x720) → H.264 硬编(4.2Mbps,    │
│ GOP15, 30fps) → RTP 分包(FU-A, ≤1200B)    │
│ → Pacer 令牌桶限速 → 同帧多份冗余           │
│ → UDP 发往服务器 8002/8003/8004            │
│                                          │
│ CommandLink: 同一 UDP socket 收 AA 55     │
│ UART帧 → 写 UART3(115200) → 底盘 MCU      │
│ (200ms 喂狗超时自动停车)                   │
└────────────────┬─────────────────────────┘
                 │ UDP: RTP video + CBR 控制
                 ▼
┌─────────── Server (FastAPI :8000) ───────┐
│ rtp_relay: 8002/8003/8004 收 RTP,          │
│   学习上游, 扇出到所有注册下游 + IDR 命令    │
│ video_hub: 8001 收 JPEG 分块重组(备用链路)  │
│ command_udp: 反向控制, 150Hz 推 UART 帧     │
│   到「视频源地址」(2.5s 过期)               │
│ routers: /ping /command /video/* /logs    │
│ ws /ws/logs: 日志实时下发                  │
└────────────────┬─────────────────────────┘
                 │ HTTP POST /command、RTP 下行
                 ▼
┌─────────── Desktop (Tkinter 操作台) ──────┐
│ ServerManager: 子进程拉起 uvicorn            │
│ H264Viewer: 绑定临时端口收 RTP,             │
│   RFC6184 解包 → PyAV 解码 → 画布          │
│   (丢帧丢卡等 CBR/PING/IDR 控制)            │
│ GamepadPusher: XInput ~100Hz →            │
│   POST /command (16字节 raw_hex + 语义cmds) │
│ LatencyMonitor: render_age_ms p50/p95      │
│ Viewer(JPEG HTTP 轮询) 备用视频              │
│ LogPanel / LatencyPanel / StreamPanel      │
└──────────────────────────────────────────┘
```

## 端口与协议

| 端口/链路 | 协议 | 用途 |
|---|---|---|
| 8000 | HTTP / WebSocket | FastAPI 控制面（`/ping`、`/command`、`/video/*`、`/ws/logs`） |
| 8001 | UDP | JPEG 分块视频上行（备用链路），喂 `video_hub` |
| 8002/8003/8004 | UDP | H.264 裸 RTP 多链路上行（K230 同帧冗余发 3 份，K230 `RTP_PORT` 可覆盖） |
| 8002 | UDP | RTP 扇出下行到桌面 H264Viewer（viewer 绑定临时端口），并捎带 CBR 控制包 |
| 115200 | UART3 | K230 → 底盘 MCU：`AA 55 <count> [<cmd,len,payload>]...+XOR校验`（MOVE=0x01，TURRET=0x02） |

环境变量：`CORE_BRIDGE_HTTP_HOST/PORT`（默认 0.0.0.0:8000）、`CORE_BRIDGE_VIDEO_UDP_PORT`（8001）、`CORE_BRIDGE_RTP_PORTS`（8002,8003,8004）；K230 端 `SERVER_IP`、`RTP_PORT`、`WIFI_LINK_KBPS=15000`、`PACER_DUTY=0.92`、`BIT_RATE=4200` 等。

## 数据流详解

### 1. 视频链路（主：H.264/RTP）
1. K230 `k230/rtp_push.py`：摄像头 → 硬件 H.264 编码 → Annex-B 拆 NALU → RTP FU-A 打包（SSRC `0x54494E44` "TIND"）→ 令牌桶 pacer 按 WiFi 链路预算限速 → `frame_admission` 决定 P 帧/IDR 的复制份数（最多 3 链路，IDR 2 份）→ 发往 8002–8004。
2. 服务器 `rtp_relay.py`：每个端口独立学习上游（5s 过期），全部有效报文扇出到所有注册下游；处理 `CBR` 控制协议（0x00 PING→0x10 PONG、0x01 IDR 请求转发上游、0x02 BITRATE）。
3. 桌面 `desktop/streaming/h264_viewer.py`：RTP 去重/重排（丢帧时请求 IDR，IdrGate 限流 ≥400ms）→ RFC 6184 解包（FU-A/STAP-A）→ PyAV decode → Tk 画布；统计核心指标 `render_age_ms`（收包→解码完成的真实延迟）。

### 2. 视频链路（备：JPEG/HTTP）
K230 JPEG 分块（header `>HBBHHHH`，块 ≤1200B）→ UDP 8001 → `video_hub` 重组（≤512 块、≤1MiB、0.6s 超时）→ 桌面 `viewer.py` 轮询 `GET /video/latest.jpg?since=<id>`（304 跳过），或浏览器看 `/video/mjpeg?fps=10`。

### 3. 指令链路（手柄 → 机器人）
1. XInput（`desktop/gamepad/xinput.py`，ctypes，4 槽位）→ 16 字节协议帧（lx/ly/rx/ry u16 偏移码，lt/rt 0–1023）+ 语义 `cmds`（MOVE speed/turn、TURRET yaw/pitch，−100..100，死区 ±4）。
2. `GamepadPusher` ~100Hz `POST /command`；断连时发一次中立帧（failsafe）。
3. 服务器 `command_udp.py`： buildup UART v2 帧 `AA 55 ...`，**≤150Hz** 推送到「从视频包学到的 K230 地址」（2.5s 过期）；快照 >0.5s 不发，K230 侧 200ms 喂狗自动停车（dead-man）；轴向幅值滞回 ON>30 / OFF<28 决定指令是否激活。
4. K230 `CommandLink.pump` 校验帧格式 → 写 UART3 115200 → 底盘/云台 MCU 执行。

### 4. 心跳与安全
- `GET /ping`：桌面 RTT 测量与服务端健康检查。
- 指令快照过期 0.5s 即停止下发 + K230 200ms 喂狗自停：双层失效安全。
- 手柄断开发中立帧，机器人立刻停止。

### 5. 日志链路
服务器 logging → `LogHub`（500 行历史）→ `WS /ws/logs` → 桌面 `LogClient` → LogPanel；`DELETE /logs` 清空。服务器子进程 stdout 同时落盘 `logs/server.out`（8MB 轮转）。

### 6. 延迟监控
`LatencyMonitor` 每 0.5s 轮询 `/video/rtp_status`（RTT）并合并 H264Viewer 统计（render_age_ms、queue、backlog、drops）；历史 120 样本算 p50/p95；报告回传 `POST /video/timing/client`；头部 pill：render_age ≥500ms 显示「滞后高」，否则 LIVE。

## 各组件要点

### server/
- `app.py`：FastAPI 入口，lifespan 内启动 UDP 监听/RTP 中继/反向控制线程。
- `rtp_relay.py`：裸 RTP 多链路扇出中继，`SO_RCVBUF` 256KiB。
- `video_hub.py`：JPEG 分块重组（magic 0x4A50"JP"）。
- `command_udp.py`：反向控制推帧器（150Hz、滞回、stale 保护）。
- `routers/`：heartbeat(`/ping`,`/test`)、command、video（status/rtp_status/timing/latest.jpg/mjpeg）、logs。

### desktop/
- `app.py` / `start_desktop.bat`：入口 `python -m desktop.app`，1280×880 布局。
- `server_manager.py`：子进程起 uvicorn、健康检查、遗留进程清理、日志轮转。
- `streaming/h264_viewer.py` / `viewer.py`：主/备视频接收。
- `gamepad/`：xinput 采集 → commands 语义化 → pusher 推送。
- `telemetry/latency_monitor.py`、`modules/`（stream/latency/log/gamepad 四面板）。

### k230/
- `rtp_push.py`（MicroPython）：唯一的 K230 脚本，集成采集/硬编/RTP 打包/pacer/多链路冗余/指令接收（UART3 桥接）/IDR 门控；GC 每 100 帧，统计每 10s 打印。

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`
2. 双击 `start_desktop.bat`（或 `python -m desktop.app`）：桌面端自动拉起服务器（端口 8000）。
3. K230 烧录 `k230/rtp_push.py`，配置 `SERVER_IP`/WiFi，上电自动推流至服务器并被桌面端接收。
4. 插入手柄即可遥控；`/video/mjpeg` 可供浏览器调试查看。

## 测试

```
pytest test/test_rtp_redundancy.py
```

覆盖：frame_admission 冗余份数决策、pacer 令牌计费、fanout 下发、真实 RtpRelay 本地集成（临时端口 + ping/pong/IDR 回路）。

## 许可

见 [LICENSE](LICENSE)。

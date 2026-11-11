# TinDistance 上位机画面重写规格

日期：2026-11-11
范围：桌面 Tkinter 操作台的信息架构、死代码删除、可并行落地的文件边界。
不改：视频解码、NACK、RTP 中继、V1.3 坦克语义、丢包窗口算术、端口与环境变量。
不引入 Qt、Web 前端或新的 Python 依赖。界面文案用中文。数字用等宽字体。

事实来源是仓库里的 `README.md` 与当前代码。本文里的「删除」只覆盖下面第 2 节列出的符号。JPEG 备用链路和 8003/8004 备用 RTP 端口不在删除范围内。

---

## 1. 现状

### 1.1 画面信息架构

进程入口是 `python -m desktop.app`（`start_desktop.bat` 同样走这条）。`DesktopApp` 先铺满一个启动屏，服务起来之后把启动屏 `destroy` 掉，再搭主界面。窗口 1280×880，最小 960×680。

启动屏 `StartScreen`（`desktop/start_screen.py`）是一整页卡片：标题、端口输入（默认 8000）、按钮「启动 Server」、一行状态。它不显示画面，也不显示手柄。

主界面（`desktop/app.py` 的 `_build_main`）从上到下是四段：

| 区域 | 内容 |
|---|---|
| 顶栏 | 英文标题、`base_url`、LIVE / 无信号 / 滞后高 药丸、`render_age` 毫秒数 |
| 左主区（列权重 3） | `StreamPanel`：图传画布、Server 输入框、暂停按钮、帧号与尺寸 |
| 右栏（列权重 1，最小宽 300） | 上：`LatencyPanel`（26px 延迟数字、110px 曲线、40px 丢包细条、6 格诊断、4 格丢包）；下：`LogPanel`（占满剩余高度） |
| 手柄条 | `GamepadPanel` 横跨两列：按键灯、两个摇杆点、LT/RT 条、16 字节 hex、坦克语义一行、发送一行、服务端一行 |
| 底栏 | 一行状态：源、fps、帧号、队列、溢出丢、10 秒最终丢包、p95 |

同一时刻「有没有画面」「滞后高不高」在顶栏、图传药丸、延迟面板三处各画一遍。急停只出现在手柄条的坦克行里，窗口矮下来会被顶出视野。

### 1.2 各面板职责

`BasePanel`（`desktop/modules/base_panel.py`）只提供深色底和 1px 描边。`build()` 是子类钩子。`section_head()` 没有任何调用方。

`StreamPanel`（`desktop/modules/stream_panel.py`）

- 主链路：`H264Viewer`。`h264_available()` 为假时才 `Viewer.start()`（JPEG HTTP）。
- UI 线程每 8ms（`POLL_INTERVAL_MS`）调用 `events()`，只取最后一个 `("frame", PIL.Image)` 和最后一条 `("status", str)`。
- 有 PyAV 时，缩放在解码线程完成：本文件用 `fit_render_size` / `ratio_changed` 算偶数目标尺寸，变化时才 `set_render_size`。UI 线程只做 `ImageTk.PhotoImage` 和 `create_image`。
- 没有 `set_render_size` 的旧对象才在 UI 线程 `resize`。当前 `H264Viewer` 有这个方法，这条是兼容分支。
- 首帧到达前不下发目标尺寸（`_src_known` 闸门）。`DEFAULT_SRC_SIZE = (1280, 720)` 只是占位比例，不代表 K230 只有 720p。README 的默认档是 T3 1920×1080。
- 超过 `SIGNAL_LOSS_GRACE_S`（5 秒）没有新帧才清画面，换成「没有设备在推流」。
- 把 `lambda: self._h264.stats()` 交给 `LatencyMonitor.set_stream_stats_provider`，并把 Server 输入框写进 `set_base_url`。
- JPEG 分支只 `start()`，`_poll` 从不调用 `_jpeg.events()`。PyAV 缺失时线程在拉 JPEG，画面仍是占位符。这条链路要修好，不是删掉。

`LatencyPanel`（`desktop/modules/latency_panel.py`）

- 每 500ms 读 `monitor.stats()`，再读 `monitor.samples()` 画两条曲线。
- 主数字是 `current_ms`（等于最新样本的 `render_age_ms`）。`>= WARN_MS`（500）显示「滞后高」。
- 判色顺序由静态方法 `_probe_fault` 钉死：先探针故障，再无信号，再无 `render_age`，再滞后高，否则正常。`test/test_desktop_wire_contracts.py` 直接调用这个方法。
- 丢包主数字是 10 秒窗口的 `final_pct`。颜色用 `loss_window.WARN_PCT`（0.5）和 `BAD_PCT`（2.0）。`wire_pct`、`up_pct`、`recovery_pct` 在四格里。窗口未满时脚注带 `窗口 {span:.0f}s`。
- 曲线是装饰性仪表：110px 滞后折线、40px 最终丢包细条。诊断六格是队列、积压/溢出、循环、到达陈旧、本机往返、抖动。重写后这些不再上屏。产生它们的样本字段继续保留，给 `/video/timing/client` 和测试用。

`GamepadPanel`（`desktop/modules/gamepad_panel.py`）

- 每 50ms 在 UI 线程读 `pusher.snapshot()`，画按键灯、摇杆、扳机、hex、坦克行、发送行。
- 后台线程 `gamepad-server-poll` 每 0.5 秒 `GET /command` 和 `GET /video/rtp_status`，结果放进锁保护的字典，UI 线程读 `format_server_line`。
- 操作员真正要看的是 `snapshot.tank`（`track_l` / `track_r` / `yaw_rot` / `pitch_rot` / `rear_rot` / `fire` / `estop`）和 `estop_progress`。灯、点和 hex 是原始报文的第二套展示。

`LogPanel`（`desktop/modules/log_panel.py`）

- 默认自动连 `ws://<host>/ws/logs`，每 200ms 把队列里的行写进 `Text`。
- 默认折叠重复、隐藏轮询访问日志。超过约 2000 行删掉前 500 行。
- 「清空远端」在后台线程 `DELETE /logs`。
- 模块级 `_NUM` 编译后没有使用。

`theme.py` 是颜色和字体的唯一来源。中文必须走 `cjk_font()`。大量 ttk 样式名没有任何控件使用 `style=`。

### 1.3 线程模型

Tk 的 `mainloop` 是唯一允许碰控件、`StringVar`、`PhotoImage`、`Canvas` 的线程。后台线程只写自己的锁或队列，由 `after` 回调在 UI 线程取走。

| 线程 | 名字 | 做什么 | 禁止 |
|---|---|---|---|
| UI | Tk mainloop | `after` 调度、画视频、改顶栏/面板/日志、`set_render_size`、`clear_estop()`（只碰锁） | `httpx`、`socket`、`ServerManager.start/stop`、解码、XInput 轮询 |
| 启动 | `StartScreen` 里的 daemon，无名字 | `ServerManager.start()`，结果放 `queue.Queue` | 碰任何控件。窗口已关时在另一条 daemon 里 `stop()` |
| 视频 | `h264-viewer` | `select` 收 RTP、NACK、解码、按目标尺寸 `reformat`，`events()` 最多留 2 条 | 创建 `PhotoImage`、调用 `Canvas` |
| JPEG | `jpeg-viewer` | `GET /video/latest.jpg`，`events()` 最多留 8 条 | 同上。仅 `h264_available()` 为假时启动 |
| 手柄采集 | `gamepad-poll` | 约 100Hz 读 XInput，写 `GamepadSnapshot`，不算网络 | 发包、碰控件 |
| 手柄发送 | `gamepad-send` | 50Hz 节拍（`HEARTBEAT_S = 0.02`）发 UDP 8010，失败再 HTTP | 碰控件 |
| 探针 | `latency-probe` | 每 0.5 秒 `GET /video/status` 与 `GET /video/rtp_status`，合并 `viewer.stats()`，最多每 5 秒 `POST /video/timing/client` | 碰控件。单次异常必须吞掉，线程继续 |
| 日志 | `log-client` | asyncio WebSocket，行写入 `queue.Queue`（上限 1000） | 碰 `Text` |
| 指令轮询 | `gamepad-server-poll` | 每 0.5 秒拉 `/command` 与 `/video/rtp_status` | 碰控件 |

必须留在 UI 线程的更新：

1. `ImageTk.PhotoImage` 与 `canvas.create_image` / `delete`。解码线程已经把帧缩成偶数目标尺寸，UI 线程禁止再 `resize`、禁止 `img.copy()`。
2. `H264Viewer.events()` 与 `Viewer.events()` 的排空。周期保持 `POLL_INTERVAL_MS = 8`。`desktop/app.py` 的 `timeBeginPeriod(1)` 必须留在 `main()`，否则 Windows 上 `after(8)` 会落到约 15.6ms。
3. `set_render_size` 只在画布尺寸或源比例变化时、从 UI 线程调用。首帧之前不调用。
4. 顶栏、健康、手柄文字、日志 `Text.insert` 都在各自的 `after` 回调里做。
5. `LatencyMonitor.stats()` 和 `GamepadPusher.snapshot()` 可以在 UI 线程调用：它们只取锁、不做 IO。
6. `DELETE /logs`、`ServerManager.start/stop`、HTTP 探针、UDP 发送继续留在现在的后台线程。

关窗顺序保持现有 `_on_close`：先置关闭标志，停探针和手柄推送，再对图传/日志/延迟/手柄调用 `stop()`（有的话）并 `destroy`，最后 `ServerManager.stop()` 和 `destroy()`。启动尚未完成时关窗，必须沿用 `StartScreen.destroy` 的语义：置取消标志，已经拉起的子进程在 daemon 线程里停掉，避免占着 8000。

### 1.4 桌面实际调用的服务端接口

这些路由有桌面调用方，重写 UI 不得停用，也不得改路径和字段含义。

| 接口 | 调用方 |
|---|---|
| `GET /ping` | `ServerManager._healthy` |
| `GET /video/status` | `LatencyMonitor._sample_once`（JPEG hub 状态；不 live 时改信 RTP） |
| `GET /video/rtp_status` | 探针（上游丢包、`live`、`fps`）和 `fetch_server_state`（`command_udp_in`、`command_udp`） |
| `POST /video/timing/client` | 探针，键集合等于 `STREAM_METRIC_KEYS`，测试锁死 |
| `GET /video/latest.jpg` | `desktop/streaming/viewer.py` |
| `POST /command` | `GamepadPusher._send_http`，仅 `CORE_BRIDGE_CMD_UDP=0` 或 UDP 失败 |
| `GET /command` | `fetch_server_state` |
| `DELETE /logs` | `LogPanel._clear_worker` |
| `WS /ws/logs` | `LogClient` |
| UDP 8010，28 字节 `CBC` | `GamepadPusher._send_udp`，主控制通道 |
| UDP 8002（及 `CORE_BRIDGE_RTP_PORTS`） | `H264Viewer` 向中继登记的临时端口收 RTP；中继默认还听 8003、8004 |

`GET /video/timing` 与 `GET /video/mjpeg` 桌面不轮询。前者是探针上报的读出口，后者是浏览器调试。两者保留。

面板消费的状态，重写时只读这些字段，不改生产者：

- `H264Viewer.stats()`：`streaming`、`frames`、`render_age_ms`、`queue_ms`、`drops`、`pkts_rx`、`wire_lost`、`final_lost`，以及 `STREAM_METRIC_KEYS` 里的其余键。UI 画帧不读 `stats()`，读 `events()`。
- `H264Viewer` 公开方法：`start`、`stop`、`events`、`set_render_size`、`render_size`（测试读取）、`stats`、`nack_stats`（测试读取）、`available`。
- `Viewer`：`start`、`stop`、`events`。事件与 H.264 相同，`("frame", Image)` 与 `("status", str)`。
- `LatencyMonitor.stats()`：第 5 节列出的键。`samples()` 继续存在，新面板不再为了画曲线去读它。
- `loss_window.window` 的返回：`wire_pct`、`final_pct`、`up_pct`、`recovery_pct`、`span_s`、`covered`。未知值是 `-1.0`。
- `GamepadSnapshot`：`connected`、`slot`、`fps`、`posted`、`last_error`、`transport`、`sent`、`tank`、`estop_progress`。新面板不再读 `buttons`、`lx/ly/rx/ry`、`lt8/rt8`、`proto`。这些字段仍由推送器填充，协议测试还在用原始报文。

---

## 2. 死代码清单

判定方法：对符号做全仓库检索。只有定义、没有读取，且测试不导入，才算可安全删除。默认关闭的备用链路如果仍有启动代码、环境变量或测试，算保留。

「可安全删除」共 18 条。第 6 节规定每条由哪个包动手。包与包不改同一个文件。

### 2.1 可安全删除

1. `desktop/theme.py` · `FONT_DISPLAY`
   证据：只有定义。全仓库没有读取。现有界面不用 32px 展示字体。包 A 删除。

2. `desktop/theme.py` · `CYAN`
   证据：只有定义。手柄条的青色是写死的 `#7FD4FF`，不读这个 token。包 A 删除。

3. `desktop/theme.py` · `RADIUS_NOTE`
   证据：只有定义，是一句设计笔记，没有代码读取。包 A 删除。

4. `desktop/theme.py` · 10 个 ttk 样式
   名字：`Card.TFrame`、`Header.TFrame`、`Sidebar.TFrame`、`Card.TLabel`、`Mute.TLabel`、`CardMute.TLabel`、`Eyebrow.TLabel`、`HeaderEyebrow.TLabel`、`Title.TLabel`、`Card.TScrollbar`。
   证据：`apply_theme` 里 `style.configure` 了它们，全仓库没有 `style=` 指向这些名字。控件实际用到的是默认 `TButton` / `TLabel` / `TFrame` / `TEntry` / `TScrollbar` 和 `Accent.TButton`。包 A 删除这 10 个 `configure`，保留用得到的样式。

5. `desktop/modules/base_panel.py` · `BasePanel.section_head`
   证据：只有定义。四个面板都自己画眉题，不调用它。包 A 删除。

6. `desktop/modules/log_panel.py` · `_NUM`
   证据：`re.compile(r"\d+")` 之后没有读取。折叠键用的是 `_normalize_key` 里的另一个替换。包 C 删除。测试不导入它。

7. `desktop/modules/stream_panel.py` · `_test_fit_render_size_even_and_contained`、`_test_fit_render_size_keeps_aspect`、`_test_fit_render_size_invalid_input`、`_test_ratio_changed_tolerance`、`_test_poll_interval_is_8ms`、`main`
   证据：只被本文件 `if __name__` 调用。`pytest test` 不收集这个文件。偶数尺寸和比例已由 `test_fit_render_size_is_even_contained_and_aspect_correct` 锁定 `fit_render_size` 与 `ratio_changed`。包 B 重写文件时删除自检入口。必须留下 `fit_render_size`、`ratio_changed`、`POLL_INTERVAL_MS = 8`。

8. `desktop/server_manager.py` · `ServerManager.free_port`
   证据：只有定义。注释写「兼容旧接口」。`start()` 走的是 `_port_in_use` / `_reap_own_leftovers`，没有调用 `free_port`。测试不覆盖它。包 D 删除这一个方法，不动 `start` / `stop` / `_healthy`。

9. `desktop/gamepad/xinput.py` · `guide_supported`
   证据：只有定义和返回值，没有调用方。包 D 删除函数。

10. `desktop/gamepad/xinput.py` · `GUIDE_SUPPORTED`
    证据：只被 `guide_supported` 读取。函数删除后常量也没有读取。包 D 一并删除。
    `_GUIDE_OK` 与按序号 100 取 `XInputGetState` 的分支必须留下。那是 DLL 入口选择，不是这个无人调用的包装函数。

11. `desktop/gamepad/commands.py` · `MoveCmd`
    证据：TypedDict 零引用。坦克命令用的是 `TankCmd`。包 D 删除。

12. `desktop/gamepad/commands.py` · `TurretCmd`
    证据：同上。包 D 删除。

13. `desktop/gamepad/commands.py` · `neutral_commands`
    证据：只有定义。`build_commands` 仍被 `GamepadPusher._send_http` 调用，必须留下。包 D 只删 `neutral_commands`。

14. `desktop/gamepad/pusher.py` · `GamepadPusher.ping_tank`
    证据：只有定义。同文件的 `last_tank` 被 `_send_http` 使用。面板读的是 `snapshot()`。包 D 删除 `ping_tank`。`clear_estop` 不删，顶栏解除急停要调用它。

15. `desktop/telemetry/latency_monitor.py` · `LatencyMonitor.current`
    证据：只有定义。顶栏和延迟面板都读 `stats()`。测试不调用 `current()`。包 D 删除方法。

16. `desktop/telemetry/latency_monitor.py` · `stats()` 里没有读者的键
    键：`p50_ms`、`loss_totals`、`staleness_max_ms`、`queue_max_ms`、`latency_source`、`count`。
    证据：全仓库只有 `stats()` 在写入。面板读的是 `current_ms`、`p95_ms`、`loss_window`、探针五个键，以及队列/丢包等样本字段。`test_loss_rate.py` 直接测 `loss_window.totals`，不读 `stats()["loss_totals"]`。包 D 从返回字典去掉这 6 个键；若 `totals` 的 import 因此变成未使用，一并去掉 import。`loss_window.totals` 函数本身不删。

17. `desktop/streaming/h264_viewer.py` · 八个没有调用方的只读属性
    属性：`render_age_ms`、`queue_ms`、`backlog_packets`、`backlog_bytes`、`drops`、`loop_us_avg`、`loop_us_max`、`bitrate_kbps`。
    证据：`stats()` 在锁内直接读 `_render_age_ms` 等私有字段。面板、探针、测试都走 `stats()` 字典。没有任何 `viewer.render_age_ms` 这类属性读取。包 D 删除这八个属性。`set_render_size` 与 `render_size` 不删，`test_set_render_size_contract` 会读 `render_size`。

18. `desktop/streaming/h264_viewer.py` · pytest 不引用的自检尾部
    符号：`_SkipCheck`、`_parse_nack`、`_check_nack_wire_format`、`_check_nack_chunking`、`_check_nack_plan_cap`、`_check_nack_two_stage`、`_check_nack_resolve`、`_check_nack_rate_limit`、`_check_nack_ttl_gc`、`_check_stale_seq`、`_check_backlog_budget`、`_check_render_size_api`、`_check_end_to_end_localhost`、`_check_depacketizer`、`NACK_STATS_KEYS`、`main`。
    证据：这些名字只在本文件的 `main()` 里互相调用。`pytest test` 使用的是 `test/test_desktop_wire_contracts.py` 与 `test/test_loss_rate.py`。包 D 删除上述符号。必须留下 `STATS_KEYS`、`_annexb_nals`、`_rtp_packets`、`_encode_test_aus`、`encode_nack`、`stats`、`nack_stats`。禁止改 `# ==== 自检` 注释以上的接收循环。

### 2.2 保留

下列项看起来像多余代码，检索之后是活路径或被测试锁住。

- `desktop/streaming/viewer.py` 的 `Viewer`，以及 `server/video_hub.py`、`GET /video/latest.jpg`、`GET /video/mjpeg`、`JPEG_ENABLED` / `CORE_BRIDGE_ENABLE_JPEG`。
  原因：README 写明 JPEG 是默认关闭的备用链路。`StreamPanel._ensure_jpeg` 在 PyAV 不可用时启动它。`requirements.txt` 依赖 `av`，但 DLL 加载失败时 `available()` 仍会返回假。重写要补上 `events()` 排空，而不是删除。

- `server/rtp_relay.py` 的 `RTP_PORTS` 默认 `8002,8003,8004`，环境变量 `CORE_BRIDGE_RTP_PORTS`。
  原因：K230 实际单播 8002，但中继默认仍监听另外两个端口。`test_fanout_sink_sends_all_ports` 明确向 8002/8003/8004 发包。这是可配置的备用入口，不是死代码。

- `commands.build_commands`
  原因：`GamepadPusher._send_http` 用它填 HTTP `cmds`。UDP 主通道不走它，降级路径还在。

- `GamepadPusher.clear_estop` 与 `last_tank`
  原因：`last_tank` 被 HTTP 降级读取。`clear_estop` 现在没有 UI 调用方，顶栏「解除急停」会调用它。物理解除（十字键左 / Start）仍在 `TankCommandBuilder` 里，按钮是同一把锁的第二个入口，不新增协议。

- `H264Viewer.render_size`、`set_render_size`、`nack_stats`、`STATS_KEYS`
  原因：`test_set_render_size_contract`、`test_nack_stats_keys_are_present`、`test_loss_keys_are_identical_across_all_three_layers` 直接使用。

- `LatencyPanel._probe_fault`、`fit_render_size`、`ratio_changed`
  原因：`test_desktop_wire_contracts.py` 导入它们。行为保持第 5 节的签名和断言。

- `loss_window.py` 全部公开函数，包括 UI 不再调用的 `series` 与 `totals`
  原因：`test/test_loss_rate.py` 覆盖窗口四坑、两级口径和分桶。算术模块不改。

- `desktop/gamepad/pusher.py` 底部的 `test_*` 与 `main`
  原因：`pytest test` 不收集这个文件，但这里是发送线程解耦、HTTP 降级、断连零帧的可执行自检。`test/test_desktop_wire_contracts.py` 只锁 CBC 字节布局，不覆盖这些线程行为。整段留下。

- `server/app.py` 的 `main`
  原因：`python -m uvicorn server.app:app` 不走它，`python server/app.py` 仍走它。lifespan 里的定时器调优已经不在 `main` 里，函数本身是手动入口。

- `GET /video/timing`、`GET /video/mjpeg`
  原因：见 1.4。桌面不画它们，路由仍是调试口。

- `xinput.LAMPS_13`、`LAMPS_14`、`HAT_NAMES`
  原因：当前手柄面板还在用。新面板改读 `snapshot.tank` 后会停止引用。本轮不从 `xinput.py` 删除，避免和包 C 抢文件，也避免改按键到位的映射。

- 图传上的「暂停」、两条曲线、摇杆点、hex、按键灯
  原因：它们有调用方，是正在运行的界面，不是死代码。第 4 节规定新界面不再画。由包 B、包 C 在自己的面板文件里去掉，不单列进包 D。

### 2.3 不确定，需人工确认

- `GET /test`（`server/routers/heartbeat.py`，返回正文 `pong`）
  证据：仓库内没有 HTTP 客户端请求它。README 的组件说明把它和 `/ping` 并列。`server/logs.py` 与 `LogPanel._POLLING_PATHS` 把它列进要过滤的访问路径。没有测试断言它的状态码。
  本轮不删。确认没有外部脚本在打 `http://<host>:8000/test` 之前，保留路由和日志过滤项。

---

## 3. 上位机目标

操作员看一眼要能回答四件事：画面在不在、链路好不好、车正在执行什么、急停有没有锁上。

保留的阈值，面板禁止写死数字，必须引用常量：

| 常量 | 值 | 位置 | 用法 |
|---|---|---|---|
| `WARN_MS` | 500 | `desktop/telemetry/latency_monitor.py` | `render_age >= 500` 显示「滞后高」 |
| `WARN_PCT` | 0.5 | `desktop/telemetry/loss_window.py` | 10 秒窗口最终丢包率达到此值用警告色 |
| `BAD_PCT` | 2.0 | 同上 | 达到此值用故障色 |
| `WINDOW_S` | 10 | 同上 | 主数字的窗口 |
| `POLL_INTERVAL_MS` | 8 | 图传面板文件顶部 | 视频 `after` 周期 |
| `SIGNAL_LOSS_GRACE_S` | 5 | 图传面板文件顶部 | 超过则回到无画面 |
| `RECONNECT_AFTER_S` | 0.5 | 图传面板文件顶部 | 最后一帧老于此时长、未过宽限，算重连 |
| `ESTOP_HOLD_S` | 2 | `desktop/gamepad/commands.py` | 十字键右按住锁存。面板只显示 `estop_progress`，不自己计时 |

上屏信号只留这些：

- 滞后：`stats()["current_ms"]`，也就是 `render_age`。旁边一个小字 RTT，数据是 `stats()["rtt_ms"]`（HTTP 探针往返）。两个数都标名字，避免把探针 RTT 当成画面滞后。
- FPS：`stats()["fps"]`。
- 丢包：顶栏用最终口径上色；右侧健康区给出线上、最终、上游、重传救回四个数。未知值显示「—」。
- 链路：服务是否在线、画面是否在推、指令 UDP/HTTP 是否在发、K230 端点是否学到。
- 手柄语义：左履带、右履带、炮塔水平、炮塔俯仰、后臂、开火、急停。文案沿用现有映射：炮塔 0 停 / 1 右转或上仰 / 2 左转或下俯；后臂 0 停 / 1 顺时针 / 2 逆时针。
- 档位：桌面协议里没有 K230 的 `TIER` 字段，本轮不加字段。用首帧解码尺寸显示。1920×1080 显示「T3 1080p」；1280×720 显示「720p」（T1 与 T2 分辨率相同，不猜测码率）；其他尺寸显示 `宽×高`；还没有帧显示「—」。

不上屏：延迟折线、丢包细条、队列/循环/抖动/到达陈旧/本机往返、摇杆点、扳机条、按键灯、16 字节 hex、原始轴数值、Server 地址双输入框、暂停按钮、启动页大标题。

急停始终在顶栏，不依赖右栏是否被挡住。按钮在未锁存时只显示状态，点击不触发急停（锁存仍是十字键右按住 2 秒）。锁存后按钮可点，调用 `GamepadPusher.clear_estop()`，与十字键左 / Start 解除的是同一状态。

继续 Tkinter。视频画布仍是主视区，列权重最大。

---

## 4. 画面结构

一个壳，没有第二套首页。进程启动就看到顶栏、黑画布、右侧控制、底栏日志。服务在后台自己拉起。

```
┌ 顶栏 ──────────────────────────────────────────────────────────┐
│ TinDistance │ 连接 │ 档位 │ 滞后 │ RTT │ FPS │ 丢包 │ 急停按钮 │
├ 主区 ──────────────────────────────────────┬ 右侧 300px ───────┤
│                                            │ 控制：履带/炮塔/   │
│              视频画布（主视区）              │ 后臂/开火/急停进度 │
│                                            │ 指令链路一行       │
│                                            ├───────────────────┤
│                                            │ 健康：四格丢包、   │
│                                            │ 探针一句话         │
├ 日志（固定约 150px，不抢视频高度）──────────────────────────────┤
├ 状态条：源、帧号、溢出丢、p95、启动失败时的端口与重试 ──────────┤
└────────────────────────────────────────────────────────────────┘
```

窗口仍是 1280×880，最小 960×680。主区网格：第 0 列权重 1，第 1 列权重 0、`minsize=320`；第 0 行权重 1，第 1 行权重 0。日志容器高度 150，不 `expand`。

### 4.1 顶栏

从左到右，全部常驻：

1. 标题「TinDistance」。
2. 连接。文案与级别：

| 条件（按此顺序） | 文案 | 级别 |
|---|---|---|
| 服务还在启动 | 正在启动 | mute |
| 启动失败 | 未连接 | bad |
| `_probe_fault(stats)` 为真 | 探针中断 | bad |
| `signal_state()` 为 `"wait"` | 在线 · 无画面 | mute |
| `signal_state()` 为 `"reconnect"` | 在线 · 重连 | warn |
| `signal_state()` 为 `"live"` | 在线 · 有画面 | ok |

画面状态以图传面板的 `signal_state()` 为准，定义见第 5.2 节。探针故障优先于「无画面」。壳每 500ms 读一次，不在 8ms 视频回调里改顶栏。

3. 档位。`format_tier(width, height)`：

- `width >= 1600` 且 `height >= 900` → `T3 1080p`
- `width == 1280` 且 `height == 720` → `720p`
- `width <= 0` 或 `height <= 0` → `—`
- 其余 → `f"{width}×{height}"`

尺寸来自 `StreamPanel.source_size()`，那是首帧学到的解码尺寸，不是画布上缩放后的尺寸。

4. 滞后。`int(current_ms)` 加单位「ms」。`current_ms >= WARN_MS` 时级别 bad，否则在有数且 `live` 时级别 ok。探针故障、无信号、`current_ms < 0` 都显示「—」，探针故障为 bad，其余 mute。
5. RTT。`rtt_ms >= 0` 时显示整数毫秒，否则「—」。级别固定 mute。它不参与 500ms 判定。
6. FPS。探针故障或未 `live` 时「—」，否则 `f"{fps:.1f}"`。
7. 丢包。只显示最终口径，规则：

- `final_pct < 0` → 「—」，mute
- 未满窗且 `final_pct < WARN_PCT` → `format_percent(final_pct) + " ·未满"`，mute
- `final_pct >= BAD_PCT` → 同一格式，未满窗加「 ·未满」，bad
- `final_pct >= WARN_PCT` → warn
- 已满窗且低于 `WARN_PCT` → ok

`format_percent` 与现面板一致：负数「—」；0 是 `0%`；小于 1 保留 3 位并去掉尾零；小于 10 保留 2 位并去掉尾零；其余 1 位。后缀 `%`。

8. 急停按钮，始终可见。

- `estop == 0` 且 `estop_progress <= 0`：文案「急停」，mute，`state=disabled`
- `0 < estop_progress < 1` 且未锁存：文案 `急停 {round(progress*100)}%`，warn，disabled
- `estop == 1`：文案「急停中 · 解除」，白字红底 `#C22F2F`，`state=normal`，点击调用构造时传入的 `on_release_estop`

没有手柄时按钮仍在，显示「急停」。推送器会发零帧，`estop` 为 0。

级别到颜色：ok → `theme.OK`，warn → `theme.WARN`，bad → `theme.BAD`，mute → `theme.MUTE`。背景保持顶栏的 `theme.PANEL`，急停锁存时按钮自身用 `#C22F2F`。

启动失败不在顶栏放输入框。端口和重试在状态条。

### 4.2 主视频

画布占满左格，背景 `#0B0F16`。有帧时居中贴图，像素尺寸等于解码线程给出的尺寸，UI 线程不缩放。

无帧时画两行中文，文案保持现常量：

- `没有设备在推流`
- `检查 K230 是否已上电，确认推流地址指向本机 UDP 8002`

`signal_state()` 为 `"reconnect"` 时，画布上叠加「重连中 {秒:.1f}s」，保留最后一帧。为 `"wait"` 时清掉最后一帧，回到两行占位。判定与顶栏共用这一方法，避免 8ms 轮询里「这一拍 deque 是空的」被显示成断流。帧间隔约 33ms，空 deque 是常态。

去掉 Server 输入框和暂停按钮。地址只用壳传入的 `default_server`。关窗才停 viewer。

PyAV 不可用时启动 `Viewer`，并且在同一次 `_poll` 里用与 H.264 相同的方式排空 `events()`：本拍多个 `frame` 只画最后一张，`status` 记最后一条。PyAV 可用时不启动 JPEG，避免双路抢画布。

`<Configure>` 只重算目标尺寸并重新居中现有 `PhotoImage`，不在 UI 线程做像素变换。

### 4.3 右侧

上卡「控制」，下卡「健康」。控制卡高度随内容，健康卡吃掉剩余空间。两张卡都不要曲线。

控制卡，数据每 100ms 从 `snapshot()` 读一次（现为 50ms，摇杆点没了，10Hz 足够）。一行一个量：

| 行 | 内容 |
|---|---|
| 手柄 | 已连接时「槽位 {slot} · {fps:.0f} Hz」；`xinput.available()` 为假时「本机无 XInput」；否则「无手柄 · 零帧常发」 |
| 履带 | `左 {track_l:+d}  右 {track_r:+d}` |
| 炮塔 | `{水平词} / {俯仰词}`，词表与现在 `_YAW_TEXT` / `_PITCH_TEXT` 相同 |
| 后臂 | `_REAR_TEXT` |
| 开火 | `estop` 已锁存时显示「停」；否则 `fire==1` 显示「开火」，bad；否则「关」 |
| 急停 | 与顶栏同一套进度文案，这里是文字，不是第二个按钮 |
| 发送 | `transport` 为 `udp` 显示「UDP」，`http` 显示「HTTP」。`posted` 为真且 `last_error` 含「零帧」时 warn，文案带 `last_error`。`posted` 为假时 bad，文案「发送停滞」加 `last_error` |
| 指令 | 继续用 `format_server_line` 的那一行中文。颜色：ok `#7FD4A8`，warn `theme.WARN`，bad `theme.BAD`，mute `theme.MUTE` |

后台 `gamepad-server-poll` 保持 0.5 秒，仍只写锁内字典。

健康卡，每 500ms 读 `stats()`，不读 `samples()`，不调用 `loss_window.series`。

- 一行状态，顺序与现 `_render_numbers` 相同：探针中断、无信号、无 render_age、滞后高、正常。探针中断时把 `probe_last_error` 第一行截到 80 字，并带 `probe_error_count`。
- 四格：线上 `wire_pct`、最终 `final_pct`、上游 `up_pct`、救回 `recovery_pct`。前三个走 `format_percent`。救回 `>= 0` 时显示整数百分比，否则「—」。最终格用第 4.1 节的丢包配色。上游格的眉题写「上游」，脚注一行：「上游是中继看到的 K230→服务缺口，中继不做重排」。
- 未满窗时脚注追加 `窗口 {span_s:.0f}s`。`span_s` 来自 `loss_window` 的返回值。

健康卡不重复顶栏的大号毫秒数。

### 4.4 底部日志与状态条

日志在视频下方，固定高度。行为保持：

- 用壳传入的 `http://127.0.0.1:{port}` 转成 `ws://.../ws/logs`，启动后 500ms 自动连接。
- 默认跟随、折叠重复、隐藏轮询。
- 按钮文案：「连接」/「断开」、「清空远端」、「跟随开」/「跟随关」。勾选「折叠重复」「隐藏轮询」。
- 去掉本面板的 Server 输入框。
- 着色：行内含 error / exception / failed / `err:` 用 bad；含 warn 用 warn；含独立词 ok、done、connected 用 ok。逻辑保持 `_classify_tag`。
- `_normalize_key` 与 `_is_polling` 的规则不变。删除未使用的 `_NUM`。

状态条一行，左侧只放视频源与排障里容易看错的量：

- 未 `live`：`{source} · 等待 K230 推流`
- `live` 但没有 `render_age`：`{source} · fps {fps:.1f} · 帧 #{frame_id} · 无滞后上报`
- 有滞后：`{source} · 帧 #{frame_id} · p95 {p95_ms:.0f} ms`，`drops >= 0` 时追加 ` · 溢出丢 {drops:.0f}`

「溢出丢」是本机积压主动丢掉的包，与顶栏「丢包」不是同一个数，所以状态条继续用这三个字，顶栏不写溢出。

启动失败时状态条右侧显示端口输入（默认文本 `8000`）和按钮「重试」。校验：能解析成整数且在 1024 到 65535 之间才调用重试回调；否则状态文案设为「端口无效，填 1024~65535 之间的数字」。成功后隐藏输入和按钮。

### 4.5 启动如何并进主壳

删除 `StartScreen` 这个全屏卡片。`DesktopApp.__init__` 立刻创建顶栏、空的主区、日志占位、状态条，然后 `ServerBoot.start(8000, ...)`。

`ServerBoot` 不碰 Tk。行为与现在的启动屏相同：

- 工作线程里 `ServerManager(port=port).start()`。`host` 默认 `127.0.0.1`，`bind_host` 默认 `0.0.0.0`。
- 结果 `(manager, ok, error)` 放入队列。UI 线程 `after(200)` 取走。
- 成功：状态条写「服务已就绪」，用 `manager.base_url` 创建探针、手柄推送、四个面板。顶栏连接从「正在启动」换成画面状态。
- 失败：按钮可再次点击。`error` 用 `manager.last_error`。若管理器对象非空，在 daemon 线程里 `stop()`，避免 UI 线程卡住。
- `cancel()` 置事件。工作线程若发现已取消，停掉刚刚拉起的进程并不再投递成功。窗口在 `poll` 看到结果时若已取消，同样停进程。
- 重复 `start()`：已有健康子进程时 `ServerManager.start` 自己会直接返回真。壳在一次启动未完成时忽略第二次点击。

不展示「TINDISTANCE · PIT-WALL」启动卡片，不要求操作员先按「启动 Server」。端口只有失败时才出现。

---

## 5. 模块边界

视频解码、手柄封包、丢包窗口算术不重写。新代码只改面板和壳。下面每个文件只属于一个实现包。

### 5.1 包 A 拥有的文件

`desktop/theme.py` 职责：颜色、字体、DPI、clam 主题。

保留并继续作为唯一颜色来源：`BG`、`PANEL`、`PANEL_2`、`LINE`、`INK`、`MUTE`、`FAINT`、`AMBER`、`OK`、`BAD`、`WARN`、`FONT_TITLE`、`FONT_BODY`、`FONT_SMALL`、`FONT_EYEBROW`、`FONT_MONO`、`FONT_MONO_BIG`、`FONT_MONO_SM`、`PAD`、`cjk_family`、`cjk_font`、`enable_dpi_awareness`、`apply_theme`。

`FONT_MONO_BIG` 本轮保留。旧延迟面板在包 B 落地前还引用它，先删会在并行中间态炸掉字体。

删除第 2.1 节的 1–4 条。`apply_theme` 继续配置默认 ttk 与 `Accent.TButton`。中文标签用 `cjk_font`，数字用 `FONT_MONO`。

`desktop/modules/base_panel.py`

```python
class BasePanel(tk.Frame):
    def __init__(self, master, title: str, **kwargs) -> None: ...
    def build(self) -> None: ...
```

删除 `section_head`。子类仍在 `build` 里画内容。`title` 参数保留，供子类眉题使用。

`desktop/modules/header_bar.py`（新文件）

```python
def format_tier(width: int, height: int) -> str: ...
def format_percent(value: float) -> str: ...

class HeaderBar(tk.Frame):
    def __init__(self, master, on_release_estop) -> None: ...
    def set_connection(self, text: str, level: str) -> None: ...
    def set_tier(self, text: str) -> None: ...
    def set_latency(self, text: str, level: str) -> None: ...
    def set_rtt(self, text: str) -> None: ...
    def set_fps(self, text: str) -> None: ...
    def set_loss(self, text: str, level: str) -> None: ...
    def set_estop(self, latched: bool, progress: float) -> None: ...
```

`level` 只接受 `ok`、`warn`、`bad`、`mute`。`on_release_estop` 无参数。本类不做 HTTP，不读探针。壳把已经算好的字符串传进来。`format_percent` 规则见第 4.1 节。

`desktop/modules/status_bar.py`（新文件）

```python
class StatusBar(tk.Frame):
    def __init__(self, master, on_retry) -> None: ...
    def set_status(self, text: str) -> None: ...
    def show_retry(self, visible: bool) -> None: ...
```

`on_retry(port: int) -> None`。校验失败时不调用它，状态文案用第 4.4 节那句。`show_retry(False)` 隐藏端口框和按钮。

`desktop/boot.py`（新文件）

```python
class ServerBoot:
    def start(self, port: int, on_ready, on_failed) -> None: ...
    def cancel(self) -> None: ...
    def poll(self) -> tuple | None: ...
```

`poll` 非阻塞。没有结果时返回 `None`。有结果时返回 `(manager, ok, error)`，`manager` 是 `ServerManager | None`，`error` 是 `str`。回调不在工作线程里调用；由 `DesktopApp` 在 UI 线程看到 `poll()` 的结果后调用 `on_ready(manager)` 或 `on_failed(error)`。`cancel` 后 `poll` 若仍取出一个已启动的管理器，调用方负责在 daemon 线程执行 `stop()`。

`desktop/app.py`

```python
class DesktopApp(tk.Tk):
    def __init__(self) -> None: ...

def main() -> None: ...
```

`main` 继续：`enable_dpi_awareness()`、`_high_precision_timer(True)`、`mainloop`、`finally` 里 `timeEndPeriod`。

`__init__` 搭第 4 节的空壳并 `ServerBoot.start(8000, ...)`，然后 `after(200, self._poll_boot)`。成功后：

```python
self._monitor = LatencyMonitor(get_base_url=lambda: base_url)
self._monitor.start()
self._gamepad = GamepadPusher(get_base_url=lambda: base_url)
self._gamepad.start()
self.stream_panel = StreamPanel(video_parent, default_server=base_url, monitor=self._monitor)
self.gamepad_panel = GamepadPanel(side, pusher=self._gamepad, get_base_url=lambda: base_url)
self.latency_panel = LatencyPanel(side, monitor=self._monitor)
self.log_panel = LogPanel(log_parent, default_server=base_url)
```

格子：图传 `(0,0)`，右栏 `(0,1)`，右栏内控制在上、健康在下，日志 `(1,0)` 跨两列。顶栏 `on_release_estop` 指向：

```python
def _release_estop(self) -> None:
    if self._gamepad is not None:
        self._gamepad.clear_estop()
```

壳每 500ms 在 UI 线程做一次 `_tick_header`：

- `st = self._monitor.stats()`
- 探针故障用 `LatencyPanel._probe_fault(st)`，不复制判定式
- 档位：`size = self.stream_panel.source_size` 若可调用则调用，否则 `(0, 0)`。这个 `getattr` 是为了包 A 先于包 B 落地时进程仍能开
- 重连：同样方式调用 `signal_state()`，缺方法时当作 `"wait"`
- 急停：`self._gamepad.snapshot()` 的 `tank["estop"]` 与 `estop_progress`
- 把结果写入 `HeaderBar` 与 `StatusBar`

状态条文案按第 4.4 节拼。壳不画曲线，不 `httpx`。

删除对 `desktop.start_screen` 的导入，并删除文件 `desktop/start_screen.py`。

依赖：`theme`、`boot`、`header_bar`、`status_bar`、`base_panel`（经面板间接）、现有的 `ServerManager`、`LatencyMonitor`、`GamepadPusher`、四个面板类。不改这些生产者的文件。

### 5.2 包 B 拥有的文件

`desktop/modules/stream_panel.py`

保留并保持行为：

```python
POLL_INTERVAL_MS = 8
SIGNAL_LOSS_GRACE_S = 5.0
RECONNECT_AFTER_S = 0.5
NO_STREAM_TEXT = "没有设备在推流"
NO_STREAM_HINT = "检查 K230 是否已上电，确认推流地址指向本机 UDP 8002"

def ratio_changed(new_w: int, new_h: int, ref_w: int, ref_h: int, tol: float = 0.01) -> bool: ...
def fit_render_size(src_w: int, src_h: int, canvas_w: int, canvas_h: int) -> tuple[int, int]: ...

class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000", monitor=None, **kwargs) -> None: ...
    def source_size(self) -> tuple[int, int]: ...
    def signal_state(self) -> str: ...
```

`source_size`：`_src_known` 为真时返回首帧学到的 `(w, h)`，否则 `(0, 0)`。禁止返回画布缩放结果。

`signal_state` 只返回 `"wait"`、`"live"`、`"reconnect"`。用最后一帧的年龄，不用「本拍有没有从 deque 取出帧」：

- 还没有帧，或年龄 `>= SIGNAL_LOSS_GRACE_S` → `"wait"`
- 年龄 `>= RECONNECT_AFTER_S` 且小于宽限 → `"reconnect"`
- 年龄 `< RECONNECT_AFTER_S` → `"live"`

`RECONNECT_AFTER_S` 放在文件顶部。0.5 秒长于帧间隔、短于 5 秒宽限。新界面没有暂停。

`__init__` 签名保持不变，壳继续用关键字 `default_server` 和 `monitor`。

`build` 里仍然调用：

```python
self._monitor.set_stream_stats_provider(lambda: self._h264.stats() if self._h264 is not None else {})
self._monitor.set_base_url(self._default_server)
```

`monitor` 为 `None` 时跳过。不再提供可编辑 Server，因此不再需要 `_on_server_changed`。

`_poll` 继续 `after(POLL_INTERVAL_MS)`。JPEG 排空见第 4.2 节。`destroy` 时 `stop` 两个 viewer 并 `after_cancel`。

删除本文件自检 `main` 与 `_test_*`。删除暂停按钮相关状态。

依赖：`theme`、`BasePanel`、`H264Viewer`、`available`、`Viewer`、`PIL`。不改 `h264_viewer.py` 与 `viewer.py`。

`desktop/modules/latency_panel.py`

类名保持 `LatencyPanel`，构造保持 `(master, monitor)`。

```python
class LatencyPanel(BasePanel):
    def __init__(self, master, monitor, **kwargs) -> None: ...

    @staticmethod
    def _probe_fault(st: dict) -> bool:
        if not st.get("probe_alive", True):
            return True
        return bool(st.get("sample_stale", False))
```

`_probe_fault` 的这两行保持不变，测试按类方法调用。

`build` 只建第 4.3 节的健康卡。`after(500)` 刷新。`format_percent` 可以在本文件再写一份，规则与顶栏相同，避免包 A 与包 B 共改一个工具文件。配色引用 `WARN_PCT`、`BAD_PCT`、`WARN_MS`，不写死 0.5、2.0、500。

不再创建 110px 与 40px 的 `Canvas`，不再调用 `loss_window.series`，不再读诊断六格。`destroy` 置关闭标志，停止 `after`。

依赖：`theme`、`BasePanel`、`loss_window` 的常量与 `stats()["loss_window"]` 字典、`WARN_MS`。不改 `latency_monitor.py` 与 `loss_window.py`。

### 5.3 包 C 拥有的文件

`desktop/modules/gamepad_panel.py`

```python
def format_server_line(state: dict) -> tuple[str, str]: ...
def fetch_server_state(client: httpx.Client, base_url: str) -> dict: ...

class GamepadPanel(BasePanel):
    def __init__(self, master, pusher=None, get_base_url=None, **kwargs) -> None: ...
    def stop(self) -> None: ...
    def server_snapshot(self) -> dict: ...
```

`format_server_line` 与 `fetch_server_state` 的对外行为保持现在的实现：前者返回 `(文本, "ok"|"warn"|"bad"|"mute")`，后者请求 `/command` 和 `/video/rtp_status`。壳不调用它们，但它们是指令行的唯一解析，留在这个文件里。

`__init__` 签名不变。`stop` 仍设置轮询线程的 `Event`。刷新周期改为文件顶部的常量：

```python
UI_TICK_MS = 100
SERVER_POLL_S = 0.5
```

`_render` 只读第 1.4 节列出的快照字段，按第 4.3 节的行显示。删除灯、摇杆 `Canvas`、扳机 `Canvas`、hex、`parse_protocol` 的轴数字行。急停锁存时履带行仍显示推送器给出的 0，不在面板里把非零值改掉。

无 XInput 时的提示保留。`destroy` 调用 `stop`。

依赖：`theme`、`BasePanel`、`desktop.gamepad.xinput` 的 `available`、`httpx`、推送器的 `snapshot`。不改 `pusher.py`、`commands.py`、`xinput.py`。

`desktop/modules/log_panel.py`

```python
class LogPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000", **kwargs) -> None: ...
    def stop(self) -> None: ...
```

签名不变。内部保持 `LogClient`、折叠、隐藏轮询、自动连接、`DELETE /logs` 的后台线程。删除 `_NUM` 和 Server 输入框。`default_server` 以 `http://` 开头时换成 `ws://` 再加 `/ws/logs`，这段逻辑留下。

依赖：`theme`、`BasePanel`、`LogClient`、`httpx`。不改 `desktop/logs/log_client.py`。

### 5.4 包 D 拥有的文件

只删除第 2.1 节里标成包 D 的符号，即第 8–18 条。允许改的文件只有：

- `desktop/server_manager.py`（只删 `free_port`）
- `desktop/gamepad/xinput.py`（只删 `guide_supported` 与 `GUIDE_SUPPORTED`）
- `desktop/gamepad/commands.py`（只删 `MoveCmd`、`TurretCmd`、`neutral_commands`）
- `desktop/gamepad/pusher.py`（只删 `ping_tank`）
- `desktop/telemetry/latency_monitor.py`（只删 `current` 与第 16 条的 6 个键，以及因此闲置的 import）
- `desktop/streaming/h264_viewer.py`（只删第 17、18 条列出的属性与自检符号）

`stats()` 必须继续返回这些键，包 A 和包 B 要读：

`probe_alive`、`sample_age_s`、`sample_stale`、`probe_error_count`、`probe_last_error`、`current_ms`、`render_age_ms`、`rtt_ms`、`fps`、`frame_id`、`live`、`source`、`p95_ms`、`drops`、`queue_ms`、`latency_available`、`loss_window`。

`render_age_ms` 与 `current_ms` 数值相同。两个键都留下，避免壳和面板各读一个名字时一边落空。第 16 条没有把 `render_age_ms` 列进删除键。

---

## 6. 并行实现切分

四个包的允许路径没有交集。禁止改 `test/`、`server/`、`k230/`、`README.md`、`requirements.txt`。禁止 git commit。集成时各自的 diff 只出现在自己的允许列表里。

### 包 A · 壳层与主题

允许改：

- `desktop/app.py`
- `desktop/theme.py`
- `desktop/boot.py`（新建）
- `desktop/modules/base_panel.py`
- `desktop/modules/header_bar.py`（新建）
- `desktop/modules/status_bar.py`（新建）
- 删除 `desktop/start_screen.py`

禁止改：四个现有面板文件、`server_manager.py`、`streaming/`、`gamepad/`、`telemetry/`、`logs/`，以及包 B/C/D 的全部文件。

完成标准：

- 启动后第一眼是第 4 节的壳，不是整页启动卡片。
- 服务在后台按 8000 自启；失败时状态条出现端口和「重试」，校验规则与现启动屏一致。
- 关窗在启动中途会停掉已拉起的 uvicorn，不留占端口的子进程。
- 顶栏含连接、档位、滞后、RTT、FPS、丢包、急停。急停未锁存时按钮不可触发锁存；锁存后点击调用 `clear_estop`。
- `_tick_header` 不做 HTTP。`timeBeginPeriod(1)` 仍在 `main`。
- 第 2.1 节 1–5 条已从本包文件中删除。
- `pytest test -q` 仍通过。本包不改测试。

### 包 B · 视频与健康

允许改：

- `desktop/modules/stream_panel.py`
- `desktop/modules/latency_panel.py`

禁止改：`app.py`、`theme.py`、`boot.py`、`header_bar.py`、`status_bar.py`、`base_panel.py`、`start_screen.py`、`gamepad_panel.py`、`log_panel.py`、`h264_viewer.py`、`viewer.py`、`latency_monitor.py`、`loss_window.py`。

完成标准：

- `from desktop.modules.stream_panel import fit_render_size, ratio_changed` 仍成功，偶数、不超出画布、比例误差 `<= 2 * src_w`、`(0, 0)` 输入返回 `(0, 0)` 这些现有断言继续成立。`POLL_INTERVAL_MS == 8`。
- `LatencyPanel._probe_fault` 对现有三则测试的输入返回原值。
- 构造签名不变，并提供 `source_size()` 与 `signal_state()`。
- 有 PyAV 时 UI 线程不 `resize`。无 PyAV 时 JPEG 的 `events()` 会被画出来。
- 健康卡没有曲线，四格丢包和探针文案按第 4.3 节。配色引用 `WARN_PCT`、`BAD_PCT`、`WARN_MS`。
- 第 2.1 节第 7 条的自检函数已删除。
- `pytest test -q` 仍通过。

### 包 C · 手柄与日志

允许改：

- `desktop/modules/gamepad_panel.py`
- `desktop/modules/log_panel.py`

禁止改：`pusher.py`、`xinput.py`、`commands.py`、`log_client.py`、`app.py`，以及包 A/B/D 的文件。

完成标准：

- `GamepadPanel(master, pusher=, get_base_url=)` 与 `LogPanel(master, default_server=)` 签名不变，两者都有 `stop()`。
- 控制卡显示第 4.3 节的行，不再创建摇杆、扳机、按键灯、hex。
- `UI_TICK_MS == 100`，`SERVER_POLL_S == 0.5`，HTTP 仍在后台线程。
- 日志自动连接、折叠、隐藏轮询、清空远端仍在。没有 Server 输入框。`_NUM` 已删除。
- `pytest test -q` 仍通过。现有测试不导入这两个面板，回归点是导入链没有断。

### 包 D · 只删死代码

允许改的文件与符号就是第 5.4 节。禁止改任何 UI 文件，包括包 A 新建的 `boot.py`、`header_bar.py`、`status_bar.py`。禁止改 `viewer.py`、`loss_window.py`、`log_client.py`、`server/`、`k230/`、`test/`。

完成标准：

- 第 2.1 节 8–18 条的符号用检索确认消失。
- `build_commands`、`clear_estop`、`last_tank`、`render_size`、`STATS_KEYS`、`_annexb_nals`、`_rtp_packets`、`_encode_test_aus`、`free_port` 以外的 `ServerManager` 方法都还在。
- `_GUIDE_OK` 分支还在。
- `stats()` 仍含第 5.4 节列出的保留键。
- `pytest test -q` 仍通过。特别是 `test_set_render_size_contract`、`test_nack_stats_keys_are_present`、`test_loss_keys_are_identical_across_all_three_layers`、`test_viewer_stats_exposes_exactly_the_telemetry_keys`。

建议落地顺序：四个包同时改各自文件。壳在包 B 尚未提供 `source_size` 时用 `getattr` 退回 `(0, 0)`，档位显示「—」，进程可开。全部落地后再走第 7 节的手工路径。

---

## 7. 验证

### 7.1 现有 pytest

在仓库根目录执行：

```
pytest test -q
```

README 记录的结果是 `133 passed`。本轮不改 `test/`，通过标准是这条命令仍全绿。和 UI 直接相关、重写时最容易碰红的用例：

| 测试 | 锁住的符号 |
|---|---|
| `test_fit_render_size_is_even_contained_and_aspect_correct` | `fit_render_size`、`ratio_changed` |
| `test_latency_panel_reports_probe_fault_instead_of_no_signal` | `LatencyPanel._probe_fault` |
| `test_stats_flags_a_dead_probe_separately_from_a_dead_stream` | 同上，外加 `stats()` 的探针键 |
| `test_stats_flags_a_stalled_probe_via_sample_age` | 同上 |
| `test_set_render_size_contract` | `H264Viewer.render_size` |
| `test_nack_stats_keys_are_present` | `nack_stats` 与 `stats` 的 nack 键 |
| `test_viewer_stats_exposes_exactly_the_telemetry_keys` | `STREAM_METRIC_KEYS` 与 `CLIENT_TIMING_KEYS` |
| `test_loss_keys_are_identical_across_all_three_layers` | `STATS_KEYS` |
| `test_server_manager_start_is_idempotent_for_a_healthy_child` | `ServerManager.start` 重复调用不杀活进程 |

`test/test_tank_v13.py` 锁的是 `commands` 与串口帧，不导入面板。包 D 删除 `MoveCmd` 时不要碰到 `TankCommandBuilder`、`mix_tracks`、`EstopLatch`。

### 7.2 手工点击路径

桌面没有自动化 UI 测试。四个包都落地后，在 Windows 上走这条路径。

1. 启动。双击 `start_desktop.bat` 或执行 `python -m desktop.app`。第一眼是操作台壳：顶栏、黑画布、右侧、底栏。没有整页「启动 Server」卡片。顶栏先显示「正在启动」，急停按钮可见且为「急停」。
2. 无信号。服务就绪后画布出现「没有设备在推流」和 UDP 8002 提示。顶栏连接为「在线 · 无画面」，档位「—」，滞后「—」，丢包「—」或「未满」。不要求 K230 在线。
3. 有画面。K230 推流后画布出现图像，改变窗口大小画面不变形、不闪成拉伸帧。顶栏 FPS 接近推流帧率，1080p 档位显示「T3 1080p」，720p 显示「720p」。滞后数字随 `render_age` 变化，达到 500ms 时文案侧为故障色。最终丢包达到 0.5% 为警告色，达到 2.0% 为故障色。右侧四格能同时看到线上与最终不是同一个数。
4. 无手柄。拔掉手柄或在没有手柄的机器上打开。右侧显示「无手柄 · 零帧常发」，发送行不是「发送停滞」（零帧在发）。急停按钮仍显示未锁存。有 XInput 但未插入时文案用这一句；非 Windows 或没有 DLL 时显示「本机无 XInput」。
5. 急停。插入手柄，十字键右按住。顶栏百分比上升，未满 2 秒时按钮不可按下。满 2 秒后按钮变为「急停中 · 解除」，履带显示 0。点击按钮后锁存清除，履带恢复随摇杆变化。十字键左或 Start 同样能解除。点击未锁存的急停按钮不会让车进入急停。
6. 启动失败。先占用 8000 再打开操作台。状态条给出端口占用说明，出现端口框和「重试」。填入一个空闲端口后服务起来，四个面板使用新的 `base_url`。填 `abc` 或 `80` 时状态条显示「端口无效，填 1024~65535 之间的数字」，不发起启动。
7. 关闭。播放中关闭窗口，进程退出，8000 可再次绑定。在「正在启动」时立刻关闭，也不留下 `uvicorn server.app:app` 子进程。

无 PyAV 的 JPEG 路径不作为本轮必测。若本机 `import av` 失败，再确认画布能画出 `/video/latest.jpg` 的帧，且 `CORE_BRIDGE_ENABLE_JPEG` 未打开时占位文案仍是无信号，而不是异常栈。

---

## 8. 风险

### 8.1 删了会让测试变红

- 删或改名 `LatencyPanel._probe_fault`，或改成先判断 `live`：三则探针测试失败。
- 改 `fit_render_size` 的偶数、包含关系或 `(0,0)` 语义，或把它移出 `stream_panel.py`：`test_fit_render_size_is_even_contained_and_aspect_correct` 失败。
- 删 `H264Viewer.render_size` 或改 `set_render_size(0, *)` 的「不缩放」语义：`test_set_render_size_contract` 失败。
- 从 `stats()` 或 `nack_stats()` 去掉 `nack_sent` 等键：`test_nack_stats_keys_are_present` 失败。
- 从 `STATS_KEYS` 去掉 `STREAM_METRIC_KEYS` 中的任何一个：`test_loss_keys_are_identical_across_all_three_layers` 失败。包 D 第 18 条禁止动 `STATS_KEYS`。
- 从 `LatencyMonitor.stats()` 去掉 `probe_alive`、`sample_stale`、`probe_error_count`、`probe_last_error`、`sample_age_s`：探针存活测试失败。
- 删 `ServerManager.start` 的「活着的子进程直接返回」：`test_server_manager_start_is_idempotent_for_a_healthy_child` 失败。`free_port` 不在这条路径上。
- 删 `build_commands` 不会让 `pytest test` 变红，但 `CORE_BRIDGE_CMD_UDP=0` 时 HTTP `cmds` 变成空，服务端坦克语义丢失。所以它列在保留，不列在删除。

第 2.1 节 18 条本身没有测试导入。按第 5.4 节的保留名单删，`pytest test -q` 应保持绿色。

### 8.2 会挡住视频线程的 UI 更新

视频线程不碰 Tk。它会被挡住的方式是 UI 线程太忙，`after(8)` 排不上队，`events()` 的 deque 只有 2 个位置，帧在进 UI 之前就被挤掉。

禁止：

- 在 `_poll` 或 `<Configure>` 里 `Image.resize`、`img.copy()`。兼容分支只留给没有 `set_render_size` 的对象；现网对象必须走解码线程缩放。
- 在 8ms 回调里读 HTTP、画曲线、刷新日志 `Text`、重建手柄控件。
- 把健康卡或顶栏的 500ms 刷新改成在视频回调里同步执行。
- 从解码线程调用 `set_render_size` 以外的 Tk。`set_render_size` 本身有锁，仍只从 UI 线程调，避免和画布尺寸计算交错。
- 关掉 `timeBeginPeriod(1)`。8ms 周期依赖它。

允许且应当保持很短的 UI 工作：每 8ms 一次 `PhotoImage` + `create_image`；每 100ms 一次手柄文字；每 200ms 一批日志行；每 500ms 一次顶栏和健康文字。曲线删除之后，500ms 回调不再 `Canvas.delete("all")` 重绘 120 个点。

`clear_estop()` 只持有推送器的锁，可以放在按钮回调里。`ServerManager.stop()` 可能等进程数秒，关窗时沿用现在的调用即可，不要放进 8ms 视频回调。

### 8.3 并行包互相踩文件

包 A 若去改 `stream_panel.py` 来加 `source_size`，会和包 B 冲突。壳用 `getattr` 调用，方法由包 B 添加。

包 D 若顺手删 `LAMPS_*`、`Viewer`、8003/8004 或 `clear_estop`，会拆掉现有面板、备用链路或新的解除按钮。第 2.2 节是禁止删除的名单。

包 C 落地前，旧的摇杆条会被壳放进 320px 右栏，中间态很挤。这不是包 A 要回退格子的理由。包 C 的完成标准是新控制卡在这个宽度里逐行可读。

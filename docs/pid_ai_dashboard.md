# PID AI 本地 Web 上位机

本文档说明仓库内置的 `pid-ai-serial` skill 上位机。它用于自动识别板端 COM 口、读取 PID AI 串口协议帧、显示实时波形、多环状态和自动调参建议，并在用户显式确认或显式启用 `auto-tune` 后发送 `{CMD}` 命令。

## 1. 启动命令

在仓库根目录运行：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

如果已经知道串口：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --serial-port COM5 --baud 115200 --open
```

| 参数 | 含义 |
|---|---|
| `--host` | HTTP 监听地址，默认 `127.0.0.1` |
| `--port` | 首选 HTTP 端口，默认 `8765`；占用时自动选择空闲端口 |
| `--open` | 启动后用默认浏览器打开页面 |
| `--auto` | 启动后后台自动扫描 PID AI 板端串口 |
| `--serial-port` | 手动指定串口，例如 `COM5` |
| `--baud` | 手动指定串口波特率，默认 `115200` |
| `--baud-rates` | 自动扫描候选波特率，例如 `115200,9600` |
| `--include-bluetooth` | 自动扫描时包含 Windows 蓝牙虚拟串口 |
| `--max-samples` | Python 侧保留的最大 `{PID}` 样本数 |

## 2. 数据流

```text
板端 UART 行文本
    ↓
pid_ai_serial.parse_frame()
    ↓
DashboardState typed state
    ↓
本地 HTTP JSON API
    ↓
浏览器 UI / Canvas 波形 / 命令历史
```

| 边界 | 输入 | 输出 | 校验责任 |
|---|---|---|---|
| 串口到 parser | 文本协议行或二进制协议帧 | `kind`、`valid`、`data`、`error` 字典 | `pid_ai_serial.py` 校验前缀/帧头、字段数量、数值类型、枚举范围、二进制 CRC |
| parser 到 dashboard state | typed frame | 最新 `{PID}` / `{PIDX}` / `{CFG}` / `{CFGX}` / `{SENS}` / 命令历史 | `DashboardState.ingest_line()` 只接受 `valid=True` 的 typed frame 进入主状态 |
| state 到 HTTP API | 内存状态 | JSON 快照或样本列表 | HTTP 层只返回深拷贝，避免调用方修改内部状态 |
| UI 到命令 API | `{CMD}` 文本 | pending / ack / err / local_error 命令历史 | `normalize_command()` 和 `extract_command_metadata()` 校验命令前缀、命令名和 `loop_id` |
| 自动调参状态机 | `{PIDX}`、`{CFGX}`、`{SENS}`、`{ACK}`、`{ERR}` | `autotune`、`scores`、`rollback_history`、可选 `{CMD}SET_PIDX` | `AutoTuneController` 执行安全门槛、ACK 等待、评分和回滚判断 |

串口读取使用混合流解码器：文本帧仍按 `\r\n` 分行；二进制帧按 `0xA5 0x5A`、payload 长度和 CRC 切帧。Dashboard 状态层收到的二进制 PID/CFG typed frame 与文本帧使用同一套字段名，因此图表、实验记录和自动调参不需要区分传输格式。

二进制 CRC 只证明帧传输完整，不代表 payload 数值安全。文本和二进制解析都必须拒绝 `nan`、`inf`、`-inf`，并且 `{PIDX}` / `{CFGX}` 的 `loop_id`、`loop_name` 只允许 `A-Za-z0-9_.:-` 字符集，避免坏帧进入 loop 状态、命令匹配或浏览器渲染。

## 3. 本地 API

所有接口只监听本机地址，默认 `127.0.0.1`。响应使用 UTF-8 JSON。Dashboard HTML 和 JSON API 同源访问，不开放 `Access-Control-Allow-Origin: *`，避免其他网页读取本地串口状态。

所有会改变本机串口或板端状态的 POST 接口都必须携带页面启动时注入的随机 token：

```text
X-PID-AI-Token: <window.PID_AI_API_TOKEN>
```

当前前端会自动在 `POST /api/connect`、`POST /api/disconnect`、`POST /api/command` 和 `POST /api/autotune` 上带这个 header。缺失或不匹配时接口返回错误，且不应修改连接、命令历史或自动调参状态。

### `GET /api/ports`

返回当前系统可见串口。

响应字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ports[].device` | string | 串口名，例如 `COM5` |
| `ports[].description` | string | 系统串口描述 |
| `ports[].hwid` | string | 硬件 ID |
| `ports[].manufacturer` | string/null | 厂商信息 |
| `ports[].vid` | number/null | USB VID |
| `ports[].pid` | number/null | USB PID |
| `ports[].is_bluetooth` | boolean | 是否判断为蓝牙虚拟串口 |

### `GET /api/status`

返回上位机当前状态。

核心响应字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `connected` | boolean | 当前是否已打开串口 |
| `connecting` | boolean | 是否正在后台扫描或连接 |
| `port` | string/null | 当前连接串口 |
| `baud` | number/null | 当前连接波特率 |
| `connection_error` | string/null | 最近一次连接或读取错误 |
| `latest_pid` | object/null | 最近一条有效 `{PID}` 样本 |
| `latest_cfg` | object/null | 最近一条有效 `{CFG}` 配置 |
| `latest_sens` | object/null | 最近一条有效 `{SENS}` 小车传感器帧 |
| `loops` | object | 按 `loop_id` 索引的多环状态，包含 `latest_pid` 和 `latest_cfg` |
| `autotune` | object | 自动调参开关、模式、状态、当前 loop 和最近动作 |
| `scores` | object | 按 `loop_id` 索引的自动调参评分 |
| `rollback_history` | array | 自动调参回滚历史 |
| `parse_errors` | number | 坏帧计数 |
| `last_bad_line` | string/null | 最近一条坏帧 |
| `sample_count` | number | 当前保留样本数量 |
| `latest_sample_id` | number | 最新样本自增 ID |
| `command_history` | array | 命令交易历史 |
| `experiment_recording` | object | 实验记录开关、目录、窗口秒数、记录数量和最近记录摘要 |

### `GET /api/samples?since=0&limit=500`

返回自增 ID 大于 `since` 的 `{PID}` 或 `{PIDX}` 样本。

| 参数 | 类型 | 默认值 | 含义 |
|---|---|---:|---|
| `since` | integer | `0` | 前端已收到的最后样本 ID |
| `limit` | integer | `500` | 最多返回样本数 |

响应字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `samples[].id` | number | 上位机侧样本自增 ID |
| `samples[].received_at` | number | 本机接收时间戳 |
| `samples[].kind` | string | `pid` 或 `pidx` |
| `samples[].valid` | boolean | 固定为 `true` |
| `samples[].data` | object | 按协议字段名解析后的 `{PID}` / `{PIDX}` 数据 |

## 4. 实验记录落盘

Dashboard 默认把参数修改实验保存为 JSON 文件，目录默认为仓库根目录下的 `experiments/`。可通过 `--experiment-dir` 修改路径，或用 `--disable-experiment-recording` 关闭。

| 触发点 | 行为 |
|---|---|
| 参数/控制类 `{CMD}` 入队 | 创建 pending 实验记录，保存命令前窗口样本和当前配置快照 |
| 收到匹配 `{ACK}` | 标记记录为 `ack`，后续窗口开始接收 ACK 后样本 |
| 收到匹配 `{ERR}` | 标记记录为 `err`，保留板端拒绝原因 |
| 串口未连接或写入失败 | 标记记录为 `error`，保留本地错误，不伪装成板端回复 |
| ACK 后 `{PID}` / `{PIDX}` 到达 | 追加到 `after_samples`，并刷新 `result.after_score` |
| ACK 后 `{CFG}` / `{CFGX}` 到达 | 更新 `after_config` |

实验 JSON 核心字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | number | 记录格式版本，当前为 1 |
| `record_id` | string | 文件名同源 ID，包含时间、命令 id、命令名和 loop_id |
| `command` | object | 命令文本、命令名、`loop_id`、reason 和命令历史 id |
| `response` | object/null | `{ACK}`、`{ERR}` 或本地错误结构 |
| `before_config` / `after_config` | object/null | 命令前后配置快照 |
| `before_samples` / `after_samples` | array | 命令前窗口和 ACK 后窗口的 typed PID 样本 |
| `result` | object | 状态、基础评分和自动调参 keep/rollback 信息 |

基础评分只用于回放和人工对比，字段包括 `mean_abs_error`、`max_abs_error`、`sat_ratio`、`anti_windup_ratio`、`sensor_bad_ratio` 和 `score`。自动调参是否保留或回滚仍由 `AutoTuneController` 的 ACK 后窗口决策负责。自动调参动作的 `last_action` 会包含 `strategy` 和 `changed_param`，用于说明本次是增加 `ki`、增加 `kd`、降低 `ki` 还是保守增加 `kp`。

### `POST /api/connect`

连接串口。连接和自动扫描在后台线程执行，接口会立即返回当前状态。

请求 JSON：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `auto` | boolean | 否 | 是否自动识别 PID AI 板端串口 |
| `port` | string | 否 | 手动串口名；未传且 `auto=true` 时自动扫描 |
| `baud` | number | 否 | 手动波特率，默认 `115200` |
| `baud_rates` | string | 否 | 自动扫描波特率列表，例如 `115200,9600` |
| `sample_seconds` | number | 否 | 每组端口/波特率采样秒数 |
| `max_lines` | number | 否 | 每组最多读取行数 |
| `include_bluetooth` | boolean | 否 | 是否扫描蓝牙虚拟串口 |

### `POST /api/disconnect`

断开当前串口并释放资源。请求体可以为空 JSON。

### `POST /api/command`

发送一条用户显式提交的 `{CMD}` 命令。

请求 JSON：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `command` | string | 是 | 完整 `{CMD}` 文本，例如 `{CMD}GET_CFG` |
| `reason` | string | 否 | 命令来源或调参原因，会写入命令历史 |

响应字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `command.command` | string | 原始 `{CMD}` 文本 |
| `command.command_name` | string | 命令名，例如 `GET_CFG` |
| `command.loop_id` | string/null | 分环命令的 loop_id，例如 `speed_l` |
| `command.reason` | string/null | 命令来源或调参原因 |
| `command.status` | string | `pending`、`ack`、`err` 或 `error` |
| `command.response` | object/null | 板端 `{ACK}` / `{ERR}`，或本地错误 |

### `POST /api/autotune`

配置 dashboard 内置自动调参状态机。默认关闭；只有 `enabled=true` 且 `mode="auto-tune"` 时才会自动发送 `SET_PIDX` 或回滚命令。

请求 JSON：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `enabled` | boolean | 否 | 是否启用自动调参状态机 |
| `mode` | string | 否 | `observe`、`suggest` 或 `auto-tune` |
| `profile` | string | 否 | 当前支持 `line-car-cascade` |
| `max_step` | number | 否 | 单次 `kp/ki/kd` 最大变化比例，默认 `0.10` |
| `window_seconds` | number | 否 | 评分窗口秒数，默认 `3.0` |
| `ack_timeout_seconds` | number | 否 | 等待 step 或 rollback ACK 的最长秒数，默认 `2.0` |
| `rollback_on_regression` | boolean | 否 | 评分变差时是否回滚，默认 `true` |

响应为完整 `/api/status` 快照。

## 5. 错误矩阵

| 场景 | 行为 | 用户可见状态 |
|---|---|---|
| 无串口或没有 PID AI 协议输出 | 自动连接失败，不退出 HTTP 服务 | `connection_error = "No PID AI board port detected."` |
| 串口被其他工具占用 | 连接失败，不保留半开连接 | `connection_error` 显示 pyserial 错误 |
| `{PID}` 字段数量不足或枚举越界 | 不进入样本缓冲 | `parse_errors` 增加，`last_bad_line` 记录原始行 |
| `{PID}` / `{CFG}` 文本或二进制浮点为 `nan` / `inf` / `-inf` | 不进入样本缓冲、配置或评分状态 | `parse_errors` 增加，错误说明对应字段必须为有限数 |
| `{PIDX}` / `{CFGX}` 字段数量不足、坏数字或非法枚举 | 不进入多环状态 | `parse_errors` 增加，已有 `loops[loop_id]` 不被坏帧覆盖 |
| `{PIDX}` / `{CFGX}` 的 `loop_id` 或 `loop_name` 包含空格、逗号、换行、HTML 字符或非安全字符 | 不进入多环状态和 ACK 匹配 | `parse_errors` 增加，错误说明文本字段不安全 |
| 二进制帧 CRC 错误或长度不匹配 | 不进入样本缓冲或多环状态 | `parse_errors` 增加，`last_bad_line` 记录 CRC/长度错误摘要 |
| JSON 或 OPTIONS 响应被跨站页面探测 | 不返回 wildcard CORS 允许头 | 浏览器不能把本地 API 响应暴露给其他 origin |
| POST 写接口缺失或带错 `X-PID-AI-Token` | 拒绝请求，不执行串口连接、断开、命令或自动调参配置 | HTTP 错误响应包含 `invalid API token` |
| `{SENS}` 中 `line_lost=1` | 自动调参进入停止状态 | `autotune.last_action.type=abort` |
| 未连接时发送命令 | 不伪装成板端回复 | 命令历史为 `status=error`，`response.kind=local_error` |
| 板端回复 `{ACK}` | 更新匹配的 pending 命令 | 命令历史为 `status=ack` |
| 板端回复 `{ERR}` | 更新匹配的 pending 命令 | 命令历史为 `status=err` 并保留错误详情 |
| 收到无法匹配的 `{ACK}` / `{ERR}` | 保留为 unsolicited 历史 | 命令历史 `unsolicited=true` |
| 旧 ACK/ERR 不带 `loop_id` 且已有同名分环命令 pending | 拒绝记录第二条同名分环 pending 命令 | `POST /api/command` 返回错误，避免 ACK 错配 |
| `auto-tune` 模式收到 ACK 后尚无新遥测 | 等待 post-ACK 窗口 | 不提前 keep/rollback |
| step 或 rollback 超过 `ack_timeout_seconds` 未收到 ACK/ERR，即使串口没有新帧 | 中止自动调参 | `autotune.state=ABORT`，`last_action.reason` 包含 ACK timeout |
| 串口断开或蓝牙 SPP 断流 | 中止自动调参并关闭自动写参 | `autotune.enabled=false`，保留 ABORT 原因 |
| post-ACK 评分变差 | 生成旧参数 `SET_PIDX` | `rollback_history` 增加，并记录回滚命令 |
| rollback 命令收到 ACK | 才把当前 loop 标记完成 | `rollback_history[].status=ack` 后继续下一个 loop |
| rollback 命令收到 ERR 或 ACK 超时 | 中止自动调参 | 不进入下一个 loop |

## 6. Good / Base / Bad 用例

| 类型 | 用例 | 期望 |
|---|---|---|
| Good | 板端持续发送合法 `{PID}`，页面打开后自动连接 | 曲线持续刷新，`latest_sample_id` 递增 |
| Good | 板端发送合法二进制 PID 帧 | 以同样 typed state 更新曲线和 `latest_pid` |
| Good | 板端发送 `{PIDX}` 和 `{CFGX}` | `loops[loop_id]` 显示对应最新遥测和配置 |
| Good | 启用 `suggest` 自动调参 | 只更新 `autotune.last_action.command`，不发送命令 |
| Good | 启用 `auto-tune` 且评分变差 | ACK 后等待新遥测，再发送旧参数回滚命令，并等待 rollback ACK |
| Base | POST `/api/autotune` 传 `ack_timeout_seconds=4.5` | `/api/status.autotune.ack_timeout_seconds` 为 `4.5` |
| Base | POST `/api/command` 缺少 `X-PID-AI-Token` | 返回错误，不写串口 |
| Base | 没有插入板端，启动 `--auto --open` | 页面正常打开，显示连接失败原因，不崩溃 |
| Bad | 发送 `{PID}1,2,3` 截断帧 | 样本缓冲不增加，`parse_errors` 增加 |
| Bad | 发送二进制 PID 帧且 `feedback=NaN`，CRC 正确 | 样本缓冲不增加，`parse_errors` 增加 |
| Bad | 发送 `{PIDX}<img>,left_speed,...` | 多环状态不更新，动态 UI 不渲染未转义文本 |
| Bad | 发送 `{PIDX}speed_l,left_speed,1,2,3` 截断帧 | `loops.speed_l.latest_pid` 保持上一条有效值 |
| Bad | 未连接时 POST `{CMD}GET_CFG` | 命令历史为 `error/local_error`，不显示为 ACK |
| Bad | 在 `{ACK}SET_PIDX,OK` 前连续发送 `SET_PIDX,speed_l` 和 `SET_PIDX,speed_r` | 第二条被拒绝，避免旧 ACK 格式错配 |
| Bad | auto-tune 发出 `SET_PIDX` 后串口保持静默 | `/api/status` 轮询或读线程空闲 tick 仍推进 ACK timeout 并进入 `ABORT` |

## 7. 验证命令

```powershell
python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v
python -B -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py
python -X utf8 C:\Users\caofengrui\.codex\skills\.system\skill-creator\scripts\quick_validate.py .codex\skills\pid-ai-serial
```

无真实板端时，也应能启动服务并请求 `/api/status`，状态中应包含 `connection_error`，但 HTTP 服务本身保持可用。

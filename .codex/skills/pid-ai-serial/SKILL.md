---
name: pid-ai-serial
description: Use when Codex needs to find a PID AI board COM port, inspect Windows serial ports, open a local PID tuning dashboard, read live UART or Bluetooth SPP protocol frames, capture PID telemetry, parse PID AI serial output, run observe/suggest/auto-tune workflows, or send explicit PID AI {CMD} commands.
---

# PID AI Serial

## 适用场景

当任务涉及 PID AI 板端串口、蓝牙 SPP、实时遥测、Web 上位机、命令下发或自动调参时使用本 skill。脚本是串口扫描、协议解析、dashboard 启动和自动调参状态机的事实来源，优先运行仓库内脚本，不要临时写一套 COM 口探测逻辑。

| 场景 | 推荐入口 |
|---|---|
| 查看 Windows 串口 | `pid_ai_serial.py list` |
| 自动识别 USB 串口板端 | `pid_ai_serial.py scan` |
| 蓝牙 SPP 小车 | `pid_ai_serial.py scan --include-bluetooth` |
| 读取实时协议帧 | `pid_ai_serial.py read --auto --duration 10` |
| 打开本地 Web 上位机 | `pid_ai_dashboard.py --auto --open` |
| 只观察自动调参指标 | `pid_ai_serial.py autotune --mode observe` |
| 生成但不发送建议命令 | `pid_ai_serial.py autotune --mode suggest` |
| 全自动小步写参 | `pid_ai_serial.py autotune --mode auto-tune --auto` |
| 紧急停止 | 发送停机/禁用类 `{CMD}`，并停止自动调参进程 |

## 快速命令

在仓库根目录运行：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py list
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py scan
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --auto --duration 10
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

如果确认小车通过蓝牙 SPP 暴露为 COM 口，扫描和 dashboard 可以显式包含蓝牙虚拟端口：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py scan --include-bluetooth
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --include-bluetooth --open
```

已知串口时直接指定端口：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --port COM5 --baud 115200 --jsonl --duration 10
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --serial-port COM5 --baud 115200 --open
```

## 工作模式

| 模式 | 是否写参 | 使用场景 | 行为 |
|---|---:|---|---|
| `observe` | 否 | 初次接车、排查串口、确认传感器有效 | 只读取 `{PID}` / `{PIDX}` / `{CFG}` / `{CFGX}` / `{SENS}`，计算评分和安全状态 |
| `suggest` | 否 | 需要 AI 给出建议但不希望自动改车 | 按当前窗口生成建议 `{CMD}SET_PIDX,...`，只打印或展示，不写串口 |
| `auto-tune` | 是 | 用户明确允许全自动小步调参 | 每次只改一个 loop，必须等待 `{ACK}`，观察变差时回滚 |
| `emergency-stop` | 是 | 失控、丢线、故障、蓝牙断流、用户要求停止 | 停止自动调参，优先发送禁用/停机命令并保留原始串口证据 |

`observe` 和 `suggest` 是默认安全模式；只有用户显式启用 `auto-tune` 后，脚本或 dashboard 才允许自动发送 `SET_PIDX` 或回滚命令。

## 自动调参 CLI

串级小车默认 profile：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode observe
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode suggest
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode auto-tune --max-step 0.10 --window-seconds 2.0 --ack-timeout-seconds 2.0 --rollback-on-regression
```

核心状态机：

```text
DISCOVER -> SYNC_CONFIG -> OBSERVE_BASELINE -> SELECT_LOOP -> PROPOSE_STEP
-> SEND_STEP -> OBSERVE_RESULT -> KEEP_OR_ROLLBACK -> NEXT_LOOP
```

任一安全门槛失败、收到 `{ERR}`、ACK 超时、蓝牙断流或用户停止时进入 `ABORT`。`window_seconds` 必须按接收时间或板端 `ms` 裁剪评分窗口；不能把固定样本数伪装成秒级窗口。

## 串级 PID 顺序

默认 `line-car-cascade` profile 按内环到外环调参：

| 顺序 | loop_id | 含义 | 说明 |
|---:|---|---|---|
| 1 | `speed_l` | 左轮速度内环 | 内环未稳定时不要调中环或外环 |
| 2 | `speed_r` | 右轮速度内环 | 每次只修改当前轮的一组 PID 参数 |
| 3 | `yaw_rate` | 角速度中环 | 依赖左右轮速度环响应 |
| 4 | `line_outer` | 8 路循迹外环 | 依赖中环和内环已可控 |

自动调参每次只改一个 loop 的 `kp/ki/kd`，默认单步变化不超过 `--max-step 0.10`，下发参数保留三位小数。策略会在稳态偏差时小步增加 `ki`，震荡时优先增加 `kd`，输出饱和且 `anti_windup` 频繁触发时降低 `ki`，`line_outer` 外环慢响应时用半步 `kp`。不要自动放大输出限幅、自动反转方向或自动启用危险手动输出；这些只能作为人工确认项或紧急停机项处理。

## 协议帧

识别串口时接受以下前缀：

```text
{PID} {PIDX} {CFG} {CFGX} {SENS} {ACK} {ERR} {STAT} {EVT}
```

脚本同时支持可选二进制遥测/配置帧。二进制帧以 `0xA5 0x5A` 开头，header 包含 `version/type/flags/transport_seq/payload_len`，末尾使用 CRC-16/CCITT-FALSE；CRC 标准向量 `123456789` 必须为 `0x29B1`。文本帧和二进制帧可以混合出现在同一串口流，解析后进入相同 typed frame 字段。

多环自动调参依赖扩展帧：

```text
{PIDX}loop_id,loop_name,seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
{CFGX}loop_id,loop_name,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
{SENS}ms,line0,line1,line2,line3,line4,line5,line6,line7,line_pos,line_lost,yaw,yaw_rate,enc_l,enc_r,v_l,v_r,v_avg,battery
```

解析规则：

| 规则 | 要求 |
|---|---|
| 字段数量 | 必须精确匹配协议文档，缺字段、多字段、尾随逗号都视为坏帧 |
| 数值字段 | 必须能解析且为有限数，拒绝 `nan`、`inf`、`-inf` |
| 枚举字段 | `sat`、`mode`、`enable`、`sensor_ok`、`line_lost` 等必须在合法范围 |
| 文本字段 | `loop_id` 和 `loop_name` 只允许安全文本，不能为空 |
| 坏帧处理 | 保留 raw line 和 parse error，不覆盖最新有效状态 |

## 分环命令

旧单环命令保持兼容，例如 `{CMD}SET_PID,kp,ki,kd` 仍作用于单个 `PIDAI_Handle`。多环命令必须先精确匹配 `loop_id`，再解析后续数值；未知 loop 即使后续参数是坏数字，也必须返回 `LOOP_NOT_FOUND`，不能修改任何 handle：

| 命令 | 示例 | 说明 |
|---|---|---|
| `SET_PIDX` | `{CMD}SET_PIDX,speed_l,1.200,0.030,0.080` | 设置指定 loop 的 `kp/ki/kd` |
| `SET_KFX` | `{CMD}SET_KFX,speed_l,0.000` | 设置指定 loop 的前馈系数 |
| `SET_TARGETX` | `{CMD}SET_TARGETX,yaw_rate,2.500` | 设置指定 loop 的目标值 |
| `SET_OUT_LIMITX` | `{CMD}SET_OUT_LIMITX,speed_l,0.000,1000.000` | 设置输出限幅，自动调参不得擅自放大 |
| `SET_I_LIMITX` | `{CMD}SET_I_LIMITX,line_outer,-500.000,500.000` | 设置积分限幅 |
| `RESET_IX` | `{CMD}RESET_IX,line_outer` | 清空指定 loop 积分 |
| `ENABLEX` | `{CMD}ENABLEX,speed_l,1` | 使能/禁用指定 loop |
| `GET_CFGX` | `{CMD}GET_CFGX,speed_l` | 请求单个 `{CFGX}` |
| `GET_ALL_CFG` | `{CMD}GET_ALL_CFG` | 请求全部 `{CFGX}` |

未知 loop 必须视为失败，典型返回：

```text
{ERR}SET_PIDX,ARG_INVALID,LOOP_NOT_FOUND
```

## ACK / ERR 规则

| 规则 | 要求 |
|---|---|
| 写参确认 | 串口 write 成功不代表板端应用成功，必须等待匹配 `{ACK}` |
| ACK/ERR 格式 | 兼容旧 `{ACK}SET_PIDX,OK`，推荐新 `{ACK}SET_PIDX,speed_l,OK`；ERR 同理可带 `loop_id` |
| Pending 匹配 | host 侧按命令名和 `loop_id` 匹配 pending 命令；旧 ACK/ERR 不带 `loop_id` 时禁止同名分环命令并发 pending |
| ERR 处理 | 收到 `{ERR}` 后命令失败，自动调参进入停止状态 |
| ACK 后观察 | `auto-tune` 必须等 ACK 后再开始 post-ACK 观察窗口 |
| ACK 超时 | step 和 rollback 都要记录发送时间，超过 `--ack-timeout-seconds` 未收到 ACK/ERR 必须 `ABORT` |
| 回滚条件 | post-ACK 窗口评分变差且启用 `--rollback-on-regression` 时，发送旧参数 `SET_PIDX`；rollback 是独立 pending 命令，收到匹配 ACK 后才算当前 loop 完成 |

## 安全门槛

自动调参窗口必须同时满足：

| 检查项 | 停止条件 |
|---|---|
| PID fault | 任一当前 loop 的 `fault != 0` |
| 传感器状态 | 当前 loop 的 `sensor_ok != 1` |
| 循迹状态 | `{SENS}.line_lost == 1` |
| 串口状态 | 断流、端口关闭、蓝牙 SPP 断开或持续无有效帧 |
| 命令结果 | `{ERR}`、ACK 超时或 pending 命令不匹配 |
| 用户操作 | dashboard stop、CLI 中断或明确要求 emergency-stop |

评分窗口至少考虑平均误差、最大误差、过零/震荡、饱和比例、anti-windup 比例、传感器异常比例和丢线次数。内环未达标时不要继续调外环。回滚收到 `{ERR}` 或 rollback ACK 超时必须中止，不得继续调下一环。

## Dashboard

启动：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --include-bluetooth --open
```

关键 API：

```text
GET  /api/ports
GET  /api/status
GET  /api/samples?since=0
POST /api/connect
POST /api/disconnect
POST /api/command
POST /api/autotune
```

`/api/status` 应包含 `loops`、`autotune`、`scores`、`rollback_history` 和命令历史；`POST /api/command` 记录 `loop_id` 和调参原因；`POST /api/autotune` 默认关闭，只有 `enabled=true` 且 `mode="auto-tune"` 时自动写参。

## 验证命令

修改本 skill、协议、C 库、parser、dashboard 或测试后，运行：

```powershell
gcc -Iinclude src/pid_ai.c src/pid_ai_protocol.c src/pid_ai_binary_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; if ($LASTEXITCODE -eq 0) { .\tests\test_pid_ai.exe }
python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v
python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py
python C:\Users\caofengrui\.codex\skills\.system\skill-creator\scripts\quick_validate.py .codex\skills\pid-ai-serial
```

提交前确认 `.spec-workflow/` 仍是未跟踪目录，不要加入本任务提交。

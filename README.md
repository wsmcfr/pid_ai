# PID AI 调参通信项目说明

## 项目定位

本项目用于构建一套 **板端 PID 控制器与上位机 AI 调参程序之间的通信和控制基础库**。

它的目标不是只做一个普通串口助手，而是让单片机把 PID 控制过程中的关键数据持续上传给上位机，上位机或 AI 根据这些数据分析当前 PID 的控制效果，并通过串口协议把新的 `kp`、`ki`、`kd` 等参数下发回板端，从而形成一个可诊断、可记录、可调参的闭环系统。

简单来说，本项目要解决的问题是：

```text
板端运行 PID
    ↓
上传目标值、反馈值、误差、P/I/D 输出、饱和状态等数据
    ↓
上位机/AI 分析响应慢、超调、震荡、积分饱和、输出饱和等问题
    ↓
上位机通过协议下发新的 PID 参数
    ↓
板端应用新参数并继续上传效果数据
```

## 为什么需要这个项目

传统 PID 调参通常依赖人工观察曲线和经验判断，例如：

| 常见问题 | 人工调参时的困难 |
|---|---|
| 响应慢 | 不容易判断是 `kp` 太小、输出限幅太低，还是执行器能力不足 |
| 超调严重 | 需要同时观察目标值、反馈值、误差、D 项和输出饱和情况 |
| 持续震荡 | 需要判断是 `kp` 过大、`kd` 不够，还是采样周期抖动 |
| 稳态误差 | 需要判断 `ki` 是否不足，或者积分项是否被限制 |
| 积分饱和 | 只看最终输出很难发现 `integral` 和 `i_out` 已经堆积 |
| 电机跑不动 | 需要判断 PID 是不是已经输出到上限，即 `sat=1` |

因此，板端不能只上传一个 `out`。要让 AI 有能力诊断 PID，板端必须上传完整的 PID 内部状态。

本项目正是为了统一这些数据字段、串口协议和板端 PID 驱动接口。

## 当前项目包含什么

| 路径 | 作用 |
|---|---|
| `include/pid_ai.h` | 通用 PID 驱动库头文件，定义 PID 状态结构体、模式、故障码和核心 API |
| `src/pid_ai.c` | 通用 PID 驱动库实现，包含 PID 计算、输出限幅、积分限幅、抗积分饱和和模式控制 |
| `include/pid_ai_protocol_types.h` | 文本协议和二进制协议共享的多环 route/table 类型，不包含具体协议 API |
| `include/pid_ai_protocol.h` | 文本协议头文件，定义 `{CMD}` 命令结果、ACK/ERR、文本遥测/配置打包接口 |
| `src/pid_ai_protocol.c` | 文本协议实现，负责解析 `{CMD}` 命令并打包 `{PID}`、`{CFG}`、`{ACK}`、`{ERR}` |
| `include/pid_ai_binary_protocol.h` | 二进制协议头文件，定义 PID/CFG 二进制帧、CRC 和校验接口 |
| `src/pid_ai_binary_protocol.c` | 二进制协议实现，负责打包和校验高频 PID/CFG 二进制帧 |
| `docs/pid_ai_serial_protocol.md` | 串口协议详细文档，说明每个上传字段和每条下发命令的含义 |
| `docs/pid_ai_dashboard.md` | 本地 Web 上位机启动方式、API 契约、错误矩阵和验证命令 |
| `examples/pid_ai_board_example.c` | 板端接入示例，演示如何初始化 PID、周期计算、上传遥测、处理上位机命令 |
| `tests/test_pid_ai.c` | PC 侧 C 测试，用于验证 PID 计算、限幅、手动模式和协议解析是否正确 |
| `.codex/skills/pid-ai-serial/` | Codex skill：自动识别板端 COM 口、读取串口帧，并可打开本地 Web 上位机 |

## C 库模块选择

PID 核心、文本协议和二进制协议现在是可独立选择的三层。用户可以按项目带宽、调试方式和上位机支持情况自行决定使用哪一套协议。

| 用法 | 需要加入工程的文件 | 适用场景 |
|---|---|---|
| 只用 PID 核心 | `include/pid_ai.h`、`src/pid_ai.c` | 已有自定义通信协议，只需要 PID 计算和状态字段 |
| 使用文本协议 | `include/pid_ai.h`、`include/pid_ai_protocol_types.h`、`include/pid_ai_protocol.h`、`src/pid_ai.c`、`src/pid_ai_protocol.c` | 优先可读性、串口调试和 `{CMD}` / `{ACK}` / `{ERR}` 闭环 |
| 使用二进制协议 | `include/pid_ai.h`、`include/pid_ai_protocol_types.h`、`include/pid_ai_binary_protocol.h`、`src/pid_ai.c`、`src/pid_ai_binary_protocol.c` | 高频遥测/配置快照，需要更低带宽和 CRC 校验 |
| 文本 + 二进制混合 | 以上两套协议文件全部加入 | 用文本 `{CMD}` 控制参数，用二进制帧上传高频遥测 |

二进制协议头不依赖文本协议头；如果项目只需要二进制遥测，可以不编译 `src/pid_ai_protocol.c`，也不包含 `include/pid_ai_protocol.h`。

## 系统整体架构

```text
+-------------------+         UART         +----------------------+
|                   |  {PID}/{CFG}/{EVT}   |                      |
|      单片机板端    | -------------------> |     上位机 / AI       |
|                   |                      |                      |
|  PID 控制器        | <------------------- |  诊断 / 曲线 / 调参    |
|  传感器读取        |      {CMD}SET_PID    |  参数建议 / 参数下发    |
|  PWM/DAC 输出      |                      |                      |
+-------------------+                      +----------------------+
```

板端主要负责：

| 板端职责 | 说明 |
|---|---|
| 运行 PID 控制算法 | 根据目标值和实际反馈值计算控制输出 |
| 上传实时遥测数据 | 上传 `target`、`feedback`、`error`、`p_out`、`i_out`、`d_out`、`out_raw`、`sat` 等字段 |
| 接收上位机命令 | 解析 `{CMD}` 命令并修改 PID 参数或控制模式 |
| 回复执行结果 | 命令成功回复 `{ACK}`，失败回复 `{ERR}` |
| 保护执行器安全 | 对输出、积分和模式切换进行限制 |

上位机主要负责：

| 上位机职责 | 说明 |
|---|---|
| 读取串口数据 | 按协议解析 `{PID}`、`{CFG}`、`{ACK}`、`{ERR}` |
| 实时显示曲线 | 绘制目标值、反馈值、误差、P/I/D 输出、总输出等曲线 |
| 记录实验数据 | 保存 CSV 或日志，便于回放和对比 |
| AI 分析诊断 | 判断超调、震荡、响应慢、积分饱和、输出饱和等问题 |
| 下发参数修改 | 通过 `{CMD}SET_PID,kp,ki,kd` 修改板端参数 |

## 本地 Web 上位机

仓库内置 `pid-ai-serial` Codex skill，可用于自动识别 PID AI 板端串口，并打开本地 Web 上位机查看曲线和发送参数命令。

### 启动方式

在仓库根目录运行：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

如果已经知道板端串口，也可以指定端口和波特率：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --serial-port COM5 --baud 115200 --open
```

上位机默认监听 `127.0.0.1:8765`。如果端口被占用，脚本会自动选择一个空闲端口并在终端输出 URL。

### 当前上位机能力

| 区域 | 功能 |
|---|---|
| 串口连接 | 列出 COM 口、自动识别 PID AI 协议串口、连接、断开 |
| 实时波形 | Canvas 显示 `target`、`feedback`、`error`、`p_out`、`i_out`、`d_out`、`out_limited`、`actuator` |
| 安全状态 | 显示 `sensor_ok`、`fault`、`sat`、`anti_windup`、`mode`、`enable` |
| 多环状态 | 显示 `{PIDX}` / `{CFGX}` 中每个 `loop_id` 的最新误差、参数、饱和和故障 |
| 自动调参 | 支持 `observe`、`suggest`、`auto-tune`，按串级顺序生成小步 `SET_PIDX`，根据 `ki/kd`、外环和饱和场景选择策略，ACK 后观察，变差时回滚并等待 rollback ACK |
| 参数设置 | 生成并发送 `SET_PID`、`SET_KF`、`SET_TARGET`、`SET_OUT_LIMIT`、`SET_I_LIMIT`、`SET_MODE`、`SET_MANUAL_OUT`、`ENABLE`、`SET_REVERSE`、`SET_SENSOR_OK`、`RESET_I`、`CLEAR_FAULT`、`GET_CFG` |
| 命令历史 | 记录 pending 命令、`loop_id` 和调参原因，并只在收到 `{ACK}` 后标记为确认；收到 `{ERR}` 或本地串口错误时显示失败 |
| 实验记录 | 默认把每次参数/控制命令的前后曲线、ACK/ERR、配置快照和基础评分保存到 `experiments/*.json` |

### 实验记录

Dashboard 启动后会默认启用实验记录。每次发送 `SET_PID` / `SET_PIDX` / `SET_TARGETX` / `SET_OUT_LIMITX` 等会改变控制行为的 `{CMD}` 时，上位机会先截取命令前最近一段 `{PID}` / `{PIDX}` 曲线；收到匹配 `{ACK}` 后，再保存 ACK 后窗口内的新曲线样本。记录文件使用 JSON，便于后续回放、脚本分析和对比。

| 字段 | 说明 |
|---|---|
| `command` | 命令文本、命令名、`loop_id`、发送原因和命令历史 id |
| `response` | 板端 `{ACK}` / `{ERR}` 或本地串口错误 |
| `before_config` / `after_config` | 命令前后能观测到的 `{CFG}` / `{CFGX}` 配置快照 |
| `before_samples` / `after_samples` | 命令前窗口和 ACK 后窗口的 typed 曲线样本 |
| `result.before_score` / `result.after_score` | 基于误差、饱和、anti-windup 和传感器状态的基础评分 |

可通过参数调整保存位置和窗口长度：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --experiment-dir .\experiments --experiment-window-seconds 3.0 --open
```

如只想查看实时曲线、不保存文件：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --disable-experiment-recording --open
```

### 安全约束

| 约束 | 说明 |
|---|---|
| 自动识别和打开页面是只读操作 | 启动上位机不会主动修改板端参数 |
| 所有 `{CMD}` 必须由用户点击按钮或显式 API 请求触发 | 避免 AI 或脚本在无人确认时改变真实硬件状态 |
| `auto-tune` 必须显式启用 | 启用后允许全自动发送 `SET_PIDX` 和回滚命令，但 step/rollback 都必须进入 ACK 闭环 |
| 每次只改一个 loop 的一组 PID 参数 | 降低串级系统耦合风险，便于判断效果 |
| 没有 `{ACK}` 不认为命令生效 | 串口写入成功不等于板端应用成功 |
| 同名分环命令不并发 pending | 兼容旧 `{ACK}SET_PIDX,OK` 格式时，避免左右轮等同名命令 ACK 错配 |
| `ENABLE,1`、`SET_MODE,2`、`SET_OUT_LIMIT` 会二次确认 | 这些命令可能直接影响执行器输出 |

## 板端上传什么数据

当前核心实时遥测帧为 `{PID}`。

字段顺序如下：

```text
seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

示例：

```text
{PID}1024,123456,10.000,1000.000,850.000,150.000,5.000,3200.000,120.000,40.000,10.000,0.000,170.000,170.000,170.000,0.000,1000.000,0,0,1,1,1,0
```

关键字段含义：

| 字段 | 含义 |
|---|---|
| `target` | 目标值，例如目标速度、目标角度、目标温度 |
| `feedback` | 实际反馈值，必须来自传感器，不是 PID 输出 |
| `error` | 当前误差，通常为 `target - feedback` |
| `integral` | 积分累计量，用于判断积分是否堆积 |
| `p_out` | P 项输出 |
| `i_out` | I 项输出 |
| `d_out` | D 项输出 |
| `out_raw` | PID 原始输出，还没有经过限幅 |
| `out_limited` | 限幅后的 PID 输出 |
| `actuator` | 真正给执行器的输出，例如 PWM、DAC 或电流指令 |
| `sat` | 输出饱和标志，`1` 上限饱和，`-1` 下限饱和，`0` 未饱和 |
| `anti_windup` | 是否触发积分抗饱和 |
| `sensor_ok` | 传感器数据是否有效 |
| `fault` | 板端故障位图 |

多环或串级小车场景使用兼容扩展帧：

```text
{PIDX}loop_id,loop_name,seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
{CFGX}loop_id,loop_name,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
{SENS}ms,line0,line1,line2,line3,line4,line5,line6,line7,line_pos,line_lost,yaw,yaw_rate,enc_l,enc_r,v_l,v_r,v_avg,battery
```

默认小车 profile 使用 `speed_l`、`speed_r` 左右轮速度内环，`yaw_rate` 角速度中环，`line_outer` 8 路循迹外环。自动调参顺序固定为先内环后外环：`speed_l -> speed_r -> yaw_rate -> line_outer`。

## 上位机如何修改 PID 参数

上位机通过 `{CMD}` 命令帧给板端下发控制命令。

常用命令如下：

| 命令 | 示例 | 作用 |
|---|---|---|
| 设置 PID 参数 | `{CMD}SET_PID,0.900,0.030,0.120` | 修改 `kp`、`ki`、`kd` |
| 设置目标值 | `{CMD}SET_TARGET,1000.000` | 修改 PID 目标值 |
| 设置输出范围 | `{CMD}SET_OUT_LIMIT,0.000,1000.000` | 修改执行器输出上下限 |
| 设置积分范围 | `{CMD}SET_I_LIMIT,-5000.000,5000.000` | 修改积分累计上下限 |
| 设置模式 | `{CMD}SET_MODE,1` | `0` 停止，`1` 自动 PID，`2` 手动输出 |
| 使能输出 | `{CMD}ENABLE,1` | 开启或关闭 PID 输出 |
| 清空积分 | `{CMD}RESET_I` | 清空积分项 |
| 请求配置 | `{CMD}GET_CFG` | 让板端回复当前 `{CFG}` 配置 |

多环命令在命令名后增加 `loop_id`，例如：

| 命令 | 示例 | 作用 |
|---|---|---|
| 分环设置 PID 参数 | `{CMD}SET_PIDX,speed_l,0.900,0.030,0.120` | 修改 `speed_l` 的 `kp/ki/kd` |
| 分环设置目标 | `{CMD}SET_TARGETX,yaw_rate,2.500` | 修改 `yaw_rate` 目标值 |
| 分环清空积分 | `{CMD}RESET_IX,line_outer` | 清空 `line_outer` 积分项 |
| 请求单环配置 | `{CMD}GET_CFGX,speed_l` | 让板端回复对应 `{CFGX}` |
| 请求全部配置 | `{CMD}GET_ALL_CFG` | 让板端按 loop 表回复全部 `{CFGX}` |

未知 `loop_id` 会返回 `{ERR}SET_PIDX,ARG_INVALID,LOOP_NOT_FOUND`，不会修改任何 PID 参数。

命令成功时，板端回复：

```text
{ACK}SET_PID,OK
```

命令失败时，板端回复：

```text
{ERR}SET_PID,ARG_INVALID,FLOAT_PARSE_FAIL
```

## AI 能基于这些数据诊断什么

| 现象 | 典型数据表现 | 可能建议 |
|---|---|---|
| 响应慢 | `feedback` 靠近 `target` 很慢，`error` 长时间较大 | 增大 `kp`，检查输出限幅或执行器能力 |
| 超调大 | `feedback` 明显超过 `target` | 降低 `kp`，增大 `kd`，检查目标阶跃是否过猛 |
| 持续震荡 | `feedback` 围绕 `target` 来回摆动 | 降低 `kp`，增加 `kd`，检查采样周期 |
| 稳态误差 | `error` 长时间不为 0，`sat=0` | 适当增大 `ki` |
| 积分饱和 | `integral` 或 `i_out` 长时间很大，且 `anti_windup=1` | 降低 `ki`，调整积分限幅，检查输出能力 |
| 输出能力不足 | `sat=1` 长时间存在，`feedback` 仍达不到目标 | 目标值可能过高，电源/电机/负载能力可能不足 |
| 传感器异常 | `sensor_ok=0` 或 `fault` 非 0 | 停止自动调参，先排查传感器 |

## 自动调参 CLI

串口脚本提供 `autotune` 子命令。默认 `observe` 只读，`suggest` 只输出建议命令，只有 `auto-tune` 会自动写参：

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode observe
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode suggest
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py autotune --auto --include-bluetooth --profile line-car-cascade --mode auto-tune --max-step 0.10 --window-seconds 3.0 --ack-timeout-seconds 2.0 --rollback-on-regression
```

自动调参状态机按 `DISCOVER -> SYNC_CONFIG -> OBSERVE_BASELINE -> SELECT_LOOP -> PROPOSE_STEP -> SEND_STEP -> OBSERVE_RESULT -> KEEP_OR_ROLLBACK -> NEXT_LOOP` 推进。写参前会检查 `sensor_ok`、`fault` 和 `{SENS}.line_lost`；发送后必须等 `{ACK}`，评分窗口按 `--window-seconds` 使用接收时间或板端 `ms` 裁剪。策略会在稳态偏差时小步增加 `ki`，震荡时优先增加 `kd`，饱和且 `anti_windup` 触发时降低 `ki`，`line_outer` 外环慢响应时用半步 `kp`。若 post-ACK 窗口评分变差则发送旧参数 `SET_PIDX` 回滚；回滚命令必须等匹配 `{ACK}` 后才算完成，收到 `{ERR}` 或超过 `--ack-timeout-seconds` 会中止自动调参。

## 当前阶段和下一步

当前项目已经完成：

| 状态 | 内容 |
|---|---|
| 已完成 | 通用 PID C 库 |
| 已完成 | 串口文本协议 |
| 已完成 | PID 遥测帧 `{PID}` |
| 已完成 | 配置帧 `{CFG}` |
| 已完成 | 多环遥测 `{PIDX}`、多环配置 `{CFGX}`、小车传感器 `{SENS}` parser |
| 已完成 | 分环命令 `SET_PIDX`、`SET_KFX`、`SET_TARGETX`、`SET_OUT_LIMITX`、`SET_I_LIMITX`、`RESET_IX`、`ENABLEX`、`GET_CFGX`、`GET_ALL_CFG` |
| 已完成 | 上位机命令 `{CMD}` |
| 已完成 | ACK/ERR 回复机制 |
| 已完成 | Python `autotune` 状态机、命令构建、ACK 匹配和变差回滚 |
| 已完成 | Dashboard 多环状态、自动调参状态、评分和回滚历史 |
| 已完成 | 实验记录 JSON 落盘，保存每次参数修改前后的曲线、ACK/ERR、配置快照和基础评分 |
| 已完成 | 更细的自动调参策略，覆盖 `ki/kd`、外环保守步长和积分饱和场景 |
| 已完成 | 可选二进制遥测/配置协议与 CRC-16/CCITT-FALSE，Python 上位机支持文本和二进制混合流 |
| 已完成 | 板端接入示例 |
| 已完成 | 基础 C 测试 |

下一步建议开发：

| 优先级 | 下一步 | 目标 |
|---|---|---|
| 高 | 真实小车板端接入 `{PIDX}` / `{CFGX}` / `{SENS}` | 在硬件上验证串级自动调参闭环 |
| 中 | 在真实车上标定自动调参策略阈值 | 根据实测振荡、饱和和外环轨迹表现调整策略边界 |
| 低 | 二进制命令帧扩展 | 在保留文本 `{CMD}` 的基础上评估是否需要命令也二进制化 |

## 移植到真实板子的最小步骤

1. 先加入 PID 核心文件：`include/pid_ai.h`、`src/pid_ai.c`。
2. 按通信方式选择协议模块：文本协议加入 `include/pid_ai_protocol_types.h`、`include/pid_ai_protocol.h`、`src/pid_ai_protocol.c`；二进制协议加入 `include/pid_ai_protocol_types.h`、`include/pid_ai_binary_protocol.h`、`src/pid_ai_binary_protocol.c`；两者混用时全部加入。
3. 在板端初始化时调用 `PIDAI_Init()`、`PIDAI_SetTunings()`、`PIDAI_SetOutputLimits()`、`PIDAI_SetTarget()`。
4. 在固定控制周期里读取传感器反馈值，并调用 `PIDAI_Update()`。
5. 把 `PIDAI_Update()` 返回的 `actuator` 写入 PWM、DAC、电机驱动或其他执行器。
6. 使用文本协议时，周期性调用 `PIDAI_ProtocolBuildTelemetry()` 生成 `{PID}`，串口收到 `{CMD}` 后调用 `PIDAI_ProtocolHandleCommand()` 并用 `PIDAI_ProtocolBuildAck()` / `PIDAI_ProtocolBuildError()` 回复。
7. 使用二进制协议时，周期性调用 `PIDAI_BinaryBuildTelemetry()` 或 `PIDAI_BinaryBuildTelemetryX()` 生成二进制 PID/PIDX 帧；如果仍需要上位机下发参数，建议同时保留文本协议的 `{CMD}` / `{ACK}` / `{ERR}` 闭环。

## 重要注意事项

| 注意事项 | 原因 |
|---|---|
| `feedback` 必须是真实传感器反馈 | PID 输出 `out` 不是实际反馈值，二者不能混淆 |
| `out_raw` 和 `out_limited` 必须分开 | AI 需要判断 PID 是否被输出限幅卡住 |
| `sat` 长时间为 `1` 或 `-1` 时不要盲目加参数 | 这可能是执行器能力不足，不一定是 PID 参数问题 |
| 自动调参前必须检查 `sensor_ok` 和 `fault` | 传感器异常时调参没有意义，还可能危险 |
| 第一版建议人工确认 AI 建议 | 不建议一开始就让 AI 无限制自动修改板端参数 |
| 上位机没有收到 `{ACK}` 不应认为命令生效 | 串口可能丢包、格式可能错误、参数可能越界 |

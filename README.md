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
| `include/pid_ai_protocol.h` | 串口协议头文件，定义命令解析结果、ACK/ERR、遥测打包接口 |
| `src/pid_ai_protocol.c` | 串口协议实现，负责解析 `{CMD}` 命令并打包 `{PID}`、`{CFG}`、`{ACK}`、`{ERR}` |
| `docs/pid_ai_serial_protocol.md` | 串口协议详细文档，说明每个上传字段和每条下发命令的含义 |
| `docs/pid_ai_dashboard.md` | 本地 Web 上位机启动方式、API 契约、错误矩阵和验证命令 |
| `examples/pid_ai_board_example.c` | 板端接入示例，演示如何初始化 PID、周期计算、上传遥测、处理上位机命令 |
| `tests/test_pid_ai.c` | PC 侧 C 测试，用于验证 PID 计算、限幅、手动模式和协议解析是否正确 |
| `.codex/skills/pid-ai-serial/` | Codex skill：自动识别板端 COM 口、读取串口帧，并可打开本地 Web 上位机 |

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
| 参数设置 | 生成并发送 `SET_PID`、`SET_KF`、`SET_TARGET`、`SET_OUT_LIMIT`、`SET_I_LIMIT`、`SET_MODE`、`SET_MANUAL_OUT`、`ENABLE`、`SET_REVERSE`、`SET_SENSOR_OK`、`RESET_I`、`CLEAR_FAULT`、`GET_CFG` |
| 命令历史 | 记录 pending 命令，并只在收到 `{ACK}` 后标记为确认；收到 `{ERR}` 或本地串口错误时显示失败 |

### 安全约束

| 约束 | 说明 |
|---|---|
| 自动识别和打开页面是只读操作 | 启动上位机不会主动修改板端参数 |
| 所有 `{CMD}` 必须由用户点击按钮或显式 API 请求触发 | 避免 AI 或脚本在无人确认时改变真实硬件状态 |
| 没有 `{ACK}` 不认为命令生效 | 串口写入成功不等于板端应用成功 |
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

## 当前阶段和下一步

当前项目已经完成：

| 状态 | 内容 |
|---|---|
| 已完成 | 通用 PID C 库 |
| 已完成 | 串口文本协议 |
| 已完成 | PID 遥测帧 `{PID}` |
| 已完成 | 配置帧 `{CFG}` |
| 已完成 | 上位机命令 `{CMD}` |
| 已完成 | ACK/ERR 回复机制 |
| 已完成 | 板端接入示例 |
| 已完成 | 基础 C 测试 |

下一步建议开发：

| 优先级 | 下一步 | 目标 |
|---|---|---|
| 高 | Python 串口读取脚本 | 读取 `{PID}` 并保存 CSV |
| 高 | 上位机曲线界面 | 实时显示目标值、反馈值、误差、P/I/D 输出 |
| 高 | AI 诊断模块 | 根据一段窗口数据给出调参建议 |
| 中 | 参数确认和下发界面 | 用户确认后发送 `{CMD}SET_PID` |
| 中 | 实验记录系统 | 保存每次参数修改前后的曲线和结果 |
| 低 | 二进制协议和 CRC | 提升高频传输可靠性和带宽效率 |

## 移植到真实板子的最小步骤

1. 把 `include/pid_ai.h`、`include/pid_ai_protocol.h`、`src/pid_ai.c`、`src/pid_ai_protocol.c` 加入单片机工程。
2. 在板端初始化时调用 `PIDAI_Init()`、`PIDAI_SetTunings()`、`PIDAI_SetOutputLimits()`、`PIDAI_SetTarget()`。
3. 在固定控制周期里读取传感器反馈值，并调用 `PIDAI_Update()`。
4. 把 `PIDAI_Update()` 返回的 `actuator` 写入 PWM、DAC、电机驱动或其他执行器。
5. 周期性调用 `PIDAI_ProtocolBuildTelemetry()` 生成 `{PID}` 并通过串口发送。
6. 串口收到一整行 `{CMD}` 后调用 `PIDAI_ProtocolHandleCommand()`。
7. 根据命令结果调用 `PIDAI_ProtocolBuildAck()` 或 `PIDAI_ProtocolBuildError()` 回复上位机。

## 重要注意事项

| 注意事项 | 原因 |
|---|---|
| `feedback` 必须是真实传感器反馈 | PID 输出 `out` 不是实际反馈值，二者不能混淆 |
| `out_raw` 和 `out_limited` 必须分开 | AI 需要判断 PID 是否被输出限幅卡住 |
| `sat` 长时间为 `1` 或 `-1` 时不要盲目加参数 | 这可能是执行器能力不足，不一定是 PID 参数问题 |
| 自动调参前必须检查 `sensor_ok` 和 `fault` | 传感器异常时调参没有意义，还可能危险 |
| 第一版建议人工确认 AI 建议 | 不建议一开始就让 AI 无限制自动修改板端参数 |
| 上位机没有收到 `{ACK}` 不应认为命令生效 | 串口可能丢包、格式可能错误、参数可能越界 |

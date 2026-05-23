# PID AI 串口协议说明

本文档定义板端 PID 控制器与上位机/AI 调参程序之间的串口文本协议。协议目标是让板端持续上传足够完整的 PID 遥测数据，上位机基于数据分析超调、震荡、响应慢、积分饱和、输出饱和和传感器异常，再通过命令帧修改 `kp`、`ki`、`kd` 等参数。

## 1. 基本约定

| 项目 | 约定 |
|---|---|
| 编码 | ASCII 或 UTF-8，协议关键字仅使用 ASCII |
| 传输 | UART 串口，推荐 `115200 8N1` 起步 |
| 分帧 | 一行一帧，帧尾使用 `\r\n` |
| 分隔符 | 英文逗号 `,` |
| 数值格式 | 十进制文本，浮点数推荐保留 3 到 6 位小数 |
| 字段顺序 | 固定顺序，上位机必须按字段表解析 |
| 板端上传前缀 | `{PID}`、`{PIDX}`、`{CFG}`、`{CFGX}`、`{SENS}`、`{STAT}`、`{EVT}`、`{ACK}`、`{ERR}` |
| 上位机命令前缀 | `{CMD}` |

注意：文本协议牺牲了一部分带宽，但换来了可读性和调试便利。当前推荐先用文本协议调通板端；高频遥测可以使用第 8 节定义的可选二进制协议，字段语义保持一致，并通过 CRC 提升截断/错包检测能力。

命令帧参数数量必须与下表格式精确匹配。参数不足返回 `ARG_MISSING`，参数格式错误或存在多余参数返回 `ARG_INVALID`，多余参数的 `detail` 为 `UNEXPECTED_ARG`。这样可以避免上位机拼接错误被板端静默忽略。

### 1.1 C 库模块选择

板端通用 C 库把协议实现分成文本和二进制两套，用户可以自行选择编译哪一套：

| 模块 | 文件 | 作用 |
|---|---|---|
| PID 核心 | `include/pid_ai.h`、`src/pid_ai.c` | 必选，提供 PID 计算、限幅、模式和状态字段 |
| 公共协议类型 | `include/pid_ai_protocol_types.h` | 多环 `PIDAI_LoopRoute` / `PIDAI_LoopTable`，供文本和二进制协议共享 |
| 文本协议 | `include/pid_ai_protocol.h`、`src/pid_ai_protocol.c` | `{CMD}` 解析、`{PID}` / `{CFG}` / `{PIDX}` / `{CFGX}` 文本帧、`{ACK}` / `{ERR}` |
| 二进制协议 | `include/pid_ai_binary_protocol.h`、`src/pid_ai_binary_protocol.c` | PID/CFG/PIDX/CFGX 二进制帧、CRC-16 校验和接收端帧校验 |

只使用二进制遥测时不需要包含 `pid_ai_protocol.h` 或编译 `src/pid_ai_protocol.c`；只使用文本协议时不需要包含 `pid_ai_binary_protocol.h` 或编译 `src/pid_ai_binary_protocol.c`。

## 2. 高频 PID 遥测帧 `{PID}`

`{PID}` 是最重要的实时数据帧，建议按 PID 控制周期或降频后上传。普通调参可以 50 Hz 到 200 Hz；如果串口带宽不足，可以板端 1 kHz 控制、100 Hz 上传。

### 2.1 字段顺序

```text
{PID}seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

### 2.2 示例

```text
{PID}1024,123456,10.000,1000.000,850.000,150.000,5.000,3200.000,120.000,40.000,10.000,0.000,170.000,170.000,170.000,0.000,1000.000,0,0,1,1,1,0
```

### 2.3 字段说明

| 序号 | 字段 | 类型 | 含义 | AI 诊断用途 |
|---:|---|---|---|---|
| 1 | `seq` | uint32 | 遥测递增序号 | 判断丢包、乱序、数据间断 |
| 2 | `ms` | uint32 | 板端时间戳，单位 ms | 计算真实采样间隔和响应时间 |
| 3 | `dt_ms` | float | 本次 PID 实际周期，单位 ms | 判断控制周期抖动 |
| 4 | `target` | float | 目标值 | 识别阶跃、目标变化和稳态误差 |
| 5 | `feedback` | float | 实际反馈值 | 判断系统响应、超调、震荡 |
| 6 | `error` | float | 当前误差，通常为 `target - feedback` | 判断收敛速度和稳态误差 |
| 7 | `d_error` | float | 误差变化量，通常为 `error - last_error` | 判断震荡趋势和微分项合理性 |
| 8 | `integral` | float | 积分累计量 | 判断积分饱和、长期偏差 |
| 9 | `p_out` | float | P 项输出 | 判断比例项是否过大或过小 |
| 10 | `i_out` | float | I 项输出 | 判断积分项是否堆积 |
| 11 | `d_out` | float | D 项输出 | 判断微分抑制是否过强或过弱 |
| 12 | `ff_out` | float | 前馈输出，没有前馈时为 `0` | 区分 PID 输出和前馈补偿 |
| 13 | `out_raw` | float | 未限幅原始输出 | 判断 PID 算法想输出多少 |
| 14 | `out_limited` | float | 限幅后的 PID 输出 | 判断执行器限制后的控制量 |
| 15 | `actuator` | float | 真正给执行器的输出 | 对应 PWM、DAC、电流或速度指令 |
| 16 | `out_min` | float | 输出下限 | 判断输出范围配置是否合理 |
| 17 | `out_max` | float | 输出上限 | 判断输出范围配置是否合理 |
| 18 | `sat` | int | 饱和标志，`-1` 下限，`0` 未饱和，`1` 上限 | 判断是否被执行器能力卡住 |
| 19 | `anti_windup` | int | 本次是否触发积分抗饱和 | 判断积分是否被抑制 |
| 20 | `mode` | int | `0` 停止，`1` 自动 PID，`2` 手动输出 | 区分自动控制和手动测试 |
| 21 | `enable` | int | `0` 禁止输出，`1` 允许输出 | 判断控制器是否真正使能 |
| 22 | `sensor_ok` | int | `0` 传感器异常，`1` 传感器正常 | 避免用坏数据调参 |
| 23 | `fault` | uint32 | 故障位图 | 记录参数、周期、传感器等异常 |

## 3. 配置帧 `{CFG}`

`{CFG}` 用于同步当前 PID 参数和限制条件。建议上电后发送一次，每次参数变化后发送一次，上位机请求 `{CMD}GET_CFG` 后也发送一次。

### 3.1 字段顺序

```text
{CFG}profile_id,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

当前通用库的 `PIDAI_ProtocolBuildConfig()` 使用 `pid->dt_ms` 输出 `sample_ms` 字段。固定周期项目可以在首次 `PIDAI_Update()` 后发送配置，或在应用层用标称周期维护该字段。

### 3.2 示例

```text
{CFG}0,1.200000,0.030000,0.080000,0.000000,10.000,-5000.000,5000.000,0.000,1000.000,0,1,1,0
```

### 3.3 字段说明

| 序号 | 字段 | 类型 | 含义 |
|---:|---|---|---|
| 1 | `profile_id` | int | 当前 PID 配置编号 |
| 2 | `kp` | float | 比例系数 |
| 3 | `ki` | float | 积分系数 |
| 4 | `kd` | float | 微分系数 |
| 5 | `kf` | float | 前馈系数 |
| 6 | `sample_ms` | float | 标称或最近一次 PID 周期 |
| 7 | `integral_min` | float | 积分累计下限 |
| 8 | `integral_max` | float | 积分累计上限 |
| 9 | `out_min` | float | 输出下限 |
| 10 | `out_max` | float | 输出上限 |
| 11 | `reverse` | int | 控制方向是否反向 |
| 12 | `mode` | int | 当前控制模式 |
| 13 | `version` | int | 协议或配置版本 |
| 14 | `fault` | uint32 | 当前故障位图 |

## 3.4 多环兼容扩展帧 `{PIDX}` / `{CFGX}` / `{SENS}`

`{PID}` 和 `{CFG}` 保持单环兼容。串级小车、左右轮速度环、角速度中环和循迹外环等多 PID 场景使用扩展帧。扩展帧的原则是：先输出 `loop_id` 和 `loop_name`，再复用单环帧的字段顺序，避免上位机为每个环路维护不同 schema。

### 3.4.1 多环遥测 `{PIDX}`

字段顺序：

```text
{PIDX}loop_id,loop_name,seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

示例：

```text
{PIDX}speed_l,left_speed,1024,123456,10.000,1000.000,850.000,150.000,5.000,3200.000,120.000,40.000,10.000,0.000,170.000,170.000,170.000,0.000,1000.000,0,0,1,1,1,0
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `loop_id` | string | 稳定环路 ID，例如 `speed_l`、`speed_r`、`yaw_rate`、`line_outer` |
| `loop_name` | string | 展示名称，例如 `left_speed`；为空时建议板端用 `loop_id` 代替 |
| 其余字段 | 同 `{PID}` | 与 `{PID}` 第 1 到 23 字段完全一致 |

### 3.4.2 多环配置 `{CFGX}`

字段顺序：

```text
{CFGX}loop_id,loop_name,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

示例：

```text
{CFGX}speed_l,left_speed,1.200000,0.030000,0.080000,0.000000,10.000,-5000.000,5000.000,0.000,1000.000,0,1,3,0
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `loop_id` | string | 稳定环路 ID，必须和 `*X` 命令中的 loop_id 精确一致 |
| `loop_name` | string | 展示名称 |
| 其余字段 | 同 `{CFG}` 去掉 `profile_id` | `kp` 到 `fault` 的含义与 `{CFG}` 一致 |

### 3.4.3 小车传感器 `{SENS}`

`{SENS}` 由板端应用层生成，用于自动调参安全门槛和串级外环诊断。通用 C 库不强制生成该帧。

字段顺序：

```text
{SENS}ms,line0,line1,line2,line3,line4,line5,line6,line7,line_pos,line_lost,yaw,yaw_rate,enc_l,enc_r,v_l,v_r,v_avg,battery
```

示例：

```text
{SENS}123456,1,0,1,0,1,0,1,0,0.125,0,1.500,0.250,1234,1235,0.800,0.810,0.805,7.400
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `line0` 到 `line7` | int | 8 路循迹传感器状态，`0/1` |
| `line_pos` | float | 归一化或应用层定义的线位置 |
| `line_lost` | int | `1` 表示丢线，自动调参必须停止 |
| `yaw` / `yaw_rate` | float | 姿态角和角速度 |
| `enc_l` / `enc_r` | int | 左右编码器计数 |
| `v_l` / `v_r` / `v_avg` | float | 左右轮和平均速度 |
| `battery` | float | 电池电压 |

## 4. 状态帧 `{STAT}`

`{STAT}` 是推荐扩展帧，不在当前通用库中强制生成，因为不同板子的电源、电机、编码器字段差异很大。建议应用层按下面格式实现。

```text
{STAT}ms,vbus,current,temperature,pwm,dir,encoder,rpm,fault,rx_cmd_count,tx_frame_count
```

| 字段 | 含义 |
|---|---|
| `ms` | 板端时间戳 |
| `vbus` | 电源电压 |
| `current` | 电机或负载电流 |
| `temperature` | 驱动或板载温度 |
| `pwm` | 当前 PWM 输出 |
| `dir` | 电机方向或执行器方向 |
| `encoder` | 编码器累计值 |
| `rpm` | 电机速度 |
| `fault` | 应用层故障码 |
| `rx_cmd_count` | 已接收命令数 |
| `tx_frame_count` | 已发送帧数 |

## 5. 事件帧 `{EVT}`

`{EVT}` 用于记录低频事件，例如目标变化、模式切换、参数修改和故障。事件帧便于上位机在曲线上打标记。

```text
{EVT}ms,event_name,value1,value2,value3
```

示例：

```text
{EVT}123456,SET_PID,1.200,0.030,0.080
{EVT}123900,SET_TARGET,1000.000
{EVT}124000,FAULT,SENSOR_LOST,0,0
```

## 6. 上位机命令帧 `{CMD}`

上位机或 AI 调参程序通过 `{CMD}` 修改板端参数。板端收到命令后必须回复 `{ACK}` 或 `{ERR}`，上位机只有收到 `{ACK}` 后才能认为参数已经生效。

| 命令 | 格式 | 作用 |
|---|---|---|
| 设置 PID 参数 | `{CMD}SET_PID,kp,ki,kd` | 修改 `kp`、`ki`、`kd` |
| 设置前馈 | `{CMD}SET_KF,kf` | 修改 `kf` |
| 设置目标值 | `{CMD}SET_TARGET,target` | 修改目标值 |
| 设置输出范围 | `{CMD}SET_OUT_LIMIT,out_min,out_max` | 修改执行器输出上下限 |
| 设置积分范围 | `{CMD}SET_I_LIMIT,integral_min,integral_max` | 修改积分累计上下限 |
| 设置模式 | `{CMD}SET_MODE,mode` | `0` 停止，`1` 自动 PID，`2` 手动输出 |
| 设置手动输出 | `{CMD}SET_MANUAL_OUT,value` | 手动模式下的执行器输出 |
| 使能输出 | `{CMD}ENABLE,enable` | `0` 禁止输出，非 `0` 允许输出 |
| 设置控制方向 | `{CMD}SET_REVERSE,reverse` | `0` 正常，非 `0` 反向 |
| 设置传感器状态 | `{CMD}SET_SENSOR_OK,sensor_ok` | `0` 异常，非 `0` 正常 |
| 清空积分 | `{CMD}RESET_I` | 将 `integral` 和 `i_out` 清零 |
| 复位状态 | `{CMD}RESET` | 复位运行状态但保留 PID 参数、目标值、手动输出、限幅、模式、使能和方向配置 |
| 清除故障 | `{CMD}CLEAR_FAULT` | 清除 PID 库故障位图 |
| 请求配置 | `{CMD}GET_CFG` | 板端应回复 `{CFG}` |
| 请求状态 | `{CMD}GET_STAT` | 板端应回复 `{STAT}` |

多环扩展命令：

| 命令 | 格式 | 作用 |
|---|---|---|
| 分环设置 PID 参数 | `{CMD}SET_PIDX,loop_id,kp,ki,kd` | 精确匹配 `loop_id` 后修改该环路 `kp/ki/kd` |
| 分环设置前馈 | `{CMD}SET_KFX,loop_id,kf` | 修改该环路 `kf` |
| 分环设置目标值 | `{CMD}SET_TARGETX,loop_id,target` | 修改该环路目标值 |
| 分环设置输出范围 | `{CMD}SET_OUT_LIMITX,loop_id,out_min,out_max` | 修改该环路输出上下限 |
| 分环设置积分范围 | `{CMD}SET_I_LIMITX,loop_id,integral_min,integral_max` | 修改该环路积分上下限 |
| 分环清空积分 | `{CMD}RESET_IX,loop_id` | 清空该环路积分 |
| 分环使能输出 | `{CMD}ENABLEX,loop_id,enable` | 修改该环路 enable |
| 请求单环配置 | `{CMD}GET_CFGX,loop_id` | 板端应回复对应 `{CFGX}` |
| 请求全部配置 | `{CMD}GET_ALL_CFG` | 板端应按 loop 表顺序回复多条 `{CFGX}` |

未知 `loop_id` 必须返回：

```text
{ERR}SET_PIDX,ARG_INVALID,LOOP_NOT_FOUND
```

所有 `*X` 命令仍然遵守精确字段数量规则。参数不足返回 `ARG_MISSING`，多余字段或尾随逗号返回 `ARG_INVALID,UNEXPECTED_ARG`，坏数字返回 `ARG_INVALID,FLOAT_PARSE_FAIL`。分环命令必须先精确查找 `loop_id`，再解析后续数值；因此 `{CMD}SET_PIDX,missing,nope,0,0` 仍应返回 `ARG_INVALID,LOOP_NOT_FOUND`，且不修改任何环路。若板端集成层没有提供有效 loop 表，应返回 `INTERNAL_ERROR,NO_VALID_LOOP_TABLE`。

示例：

```text
{CMD}SET_PID,0.900,0.030,0.120
{CMD}SET_TARGET,1000.000
{CMD}SET_OUT_LIMIT,0.000,1000.000
{CMD}SET_MODE,1
{CMD}ENABLE,1
```

## 7. 确认帧 `{ACK}` 与错误帧 `{ERR}`

命令成功时，板端回复旧兼容格式：

```text
{ACK}command,detail
```

示例：

```text
{ACK}SET_PID,OK
```

分环命令推荐回复带 `loop_id` 的扩展格式，便于上位机精确匹配 pending 命令：

```text
{ACK}command,loop_id,detail
```

示例：

```text
{ACK}SET_PIDX,speed_l,OK
```

命令失败时，板端回复旧兼容格式：

```text
{ERR}command,status,detail
```

示例：

```text
{ERR}SET_PID,ARG_INVALID,FLOAT_PARSE_FAIL
{ERR}SET_OUT_LIMIT,PARAM_RANGE,OUT_LIMIT_INVALID
{ERR}UNKNOWN,BAD_PREFIX,EXPECTED_CMD_PREFIX
```

分环错误也推荐带 `loop_id`：

```text
{ERR}SET_PIDX,speed_l,ARG_INVALID,FLOAT_PARSE_FAIL
```

为兼容旧固件，上位机仍可接受不带 `loop_id` 的 `{ACK}SET_PIDX,OK` 和 `{ERR}SET_PIDX,ARG_INVALID,LOOP_NOT_FOUND`。在旧格式下，上位机必须禁止同名分环命令并发 pending，否则无法判断 ACK/ERR 对应哪个 `loop_id`。

错误状态说明：

| 状态 | 含义 |
|---|---|
| `BAD_PREFIX` | 不是 `{CMD}` 命令帧 |
| `UNKNOWN` | 命令名不支持 |
| `ARG_MISSING` | 参数数量不足 |
| `ARG_INVALID` | 参数格式错误 |
| `PARAM_RANGE` | 参数越界或上下限非法 |
| `INTERNAL_ERROR` | 库接口调用失败 |

## 8. 可选二进制协议和 CRC

二进制协议是独立的可选 C 模块，与文本协议并存，不替换 `{CMD}` / `{ACK}` / `{ERR}` 的确认语义。它主要用于高频 `{PID}` / `{PIDX}` / `{CFG}` / `{CFGX}` 遥测和配置快照，板端和上位机可以在同一串口流中混合发送文本行和二进制帧；也可以只编译二进制协议模块，由应用层自行处理参数下发。

### 8.1 帧格式

| 字段 | 长度 | 端序 | 含义 |
|---|---:|---|---|
| `magic` | 2 | 固定 | `0xA5 0x5A` |
| `version` | 1 | - | 当前为 `1` |
| `type` | 1 | - | `1=PID`、`2=PIDX`、`3=CFG`、`4=CFGX` |
| `flags` | 1 | - | 保留，当前为 `0` |
| `transport_seq` | 4 | little-endian | 二进制传输层序号，用于检测丢包 |
| `payload_len` | 2 | little-endian | payload 字节数 |
| `payload` | 可变 | little-endian | 固定字段 payload |
| `crc16` | 2 | little-endian | CRC-16/CCITT-FALSE |

CRC-16/CCITT-FALSE 参数：初值 `0xFFFF`，多项式 `0x1021`，不反转输入/输出，不做最终异或。CRC 覆盖 `version` 到 `payload` 的所有字节，不覆盖 `magic` 和 `crc16` 自身。标准向量 `123456789` 的 CRC 必须为 `0x29B1`。

### 8.2 Payload 字段

| 类型 | Payload | 字节数 |
|---|---|---:|
| `PID` | `{PID}` 的 23 个字段，`seq/ms/fault` 为 `uint32`，`sat/anti_windup/mode/enable/sensor_ok` 为 `int32`，其余为 `float32` | 92 |
| `PIDX` | `loop_id_len:u8` + `loop_id` + `loop_name_len:u8` + `loop_name` + `PID` payload | 可变 |
| `CFG` | `{CFG}` 的 14 个字段，`profile_id/reverse/mode/version` 为 `int32`，`fault` 为 `uint32`，其余为 `float32` | 56 |
| `CFGX` | `loop_id_len:u8` + `loop_id` + `loop_name_len:u8` + `loop_name` + `{CFGX}` 中 `kp` 到 `fault` 的 13 个字段 | 可变 |

文本字段必须为非空 ASCII，且不能包含逗号、回车或换行。二进制接收端必须先校验长度和 CRC，再把 payload 解析为 typed frame；CRC 错误或字段非法时不得覆盖最新有效状态。

## 9. AI 调参闭环建议

| 步骤 | 操作 | 保护要求 |
|---|---|---|
| 1 | 板端持续上传 `{PID}` | 上位机检查 `sensor_ok=1` 且 `fault=0` |
| 2 | AI 分析最近一段曲线 | 剔除 `sat != 0` 和 `anti_windup=1` 的估参样本 |
| 3 | AI 生成参数建议 | 参数变化幅度建议限制在当前值的 10% 到 30% |
| 4 | 用户确认或自动模式批准 | 第一版建议必须人工确认 |
| 5 | 上位机发送 `{CMD}SET_PID` | 发送前检查参数上下限 |
| 6 | 板端回复 `{ACK}` | 没有 ACK 不认为生效 |
| 7 | 继续观察 `{PID}` | 对比超调、稳定时间和稳态误差 |

串级小车 profile 的默认顺序为 `speed_l`、`speed_r`、`yaw_rate`、`line_outer`。内环没有完成前不要调外环；每次只修改一个 loop 的一组 `kp/ki/kd`，默认最大变化幅度不超过 10%。自动写参必须等待 `{ACK}` 后进入观察窗口，观察评分应按 `window_seconds` 使用本机接收时间或板端 `ms` 裁剪窗口，不能固定取任意样本数。策略规则：稳态同向误差且未饱和时增加 `ki`；误差频繁过零时增加 `kd`；输出饱和且 `anti_windup` 频繁触发时降低 `ki`；`line_outer` 外环慢响应时使用半步 `kp`，避免外环过激。若评分变差则发送旧参数的 `SET_PIDX` 回滚命令；回滚本身也是独立 pending 命令，必须收到匹配 `{ACK}` 后才允许把该 loop 标记完成，回滚 `{ERR}` 或回滚 ACK 超时必须进入停止状态。出现 `fault != 0`、`sensor_ok = 0`、`line_lost = 1`、`{ERR}`、ACK 超时或蓝牙断流时进入停止状态。

## 10. 上位机字段配置模板

如果上位机支持用户自定义字段映射，第一版可以内置下面两个模板。

`{PID}` 模板：

```text
seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

`{CFG}` 模板：

```text
profile_id,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

## 11. 板端接入注意事项

| 注意事项 | 说明 |
|---|---|
| 不要在高频中断里直接 `printf` | 建议中断只置标志或写环形缓冲，主循环里发送 |
| `dt_ms` 要尽量真实 | AI 诊断周期抖动和微分项时依赖这个字段 |
| `feedback` 必须来自传感器 | 不要把 PID `out` 当作反馈值 |
| `out_raw` 和 `out_limited` 要分开 | 这样才能判断输出是否被执行器限制 |
| 参数下发要回复 ACK/ERR | 否则上位机无法确认参数是否生效 |
| 自动调参要有限制 | 必须设置 `kp/ki/kd` 的允许范围、输出范围和急停逻辑 |

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
| 板端上传前缀 | `{PID}`、`{CFG}`、`{STAT}`、`{EVT}`、`{ACK}`、`{ERR}` |
| 上位机命令前缀 | `{CMD}` |

注意：文本协议牺牲了一部分带宽，但换来了可读性和调试便利。第一版推荐先用文本协议跑通 AI 诊断闭环，后续再按相同字段设计二进制协议。

命令帧参数数量必须与下表格式精确匹配。参数不足返回 `ARG_MISSING`，参数格式错误或存在多余参数返回 `ARG_INVALID`，多余参数的 `detail` 为 `UNEXPECTED_ARG`。这样可以避免上位机拼接错误被板端静默忽略。

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

示例：

```text
{CMD}SET_PID,0.900,0.030,0.120
{CMD}SET_TARGET,1000.000
{CMD}SET_OUT_LIMIT,0.000,1000.000
{CMD}SET_MODE,1
{CMD}ENABLE,1
```

## 7. 确认帧 `{ACK}` 与错误帧 `{ERR}`

命令成功时，板端回复：

```text
{ACK}command,detail
```

示例：

```text
{ACK}SET_PID,OK
```

命令失败时，板端回复：

```text
{ERR}command,status,detail
```

示例：

```text
{ERR}SET_PID,ARG_INVALID,FLOAT_PARSE_FAIL
{ERR}SET_OUT_LIMIT,PARAM_RANGE,OUT_LIMIT_INVALID
{ERR}UNKNOWN,BAD_PREFIX,EXPECTED_CMD_PREFIX
```

错误状态说明：

| 状态 | 含义 |
|---|---|
| `BAD_PREFIX` | 不是 `{CMD}` 命令帧 |
| `UNKNOWN` | 命令名不支持 |
| `ARG_MISSING` | 参数数量不足 |
| `ARG_INVALID` | 参数格式错误 |
| `PARAM_RANGE` | 参数越界或上下限非法 |
| `INTERNAL_ERROR` | 库接口调用失败 |

## 8. AI 调参闭环建议

| 步骤 | 操作 | 保护要求 |
|---|---|---|
| 1 | 板端持续上传 `{PID}` | 上位机检查 `sensor_ok=1` 且 `fault=0` |
| 2 | AI 分析最近一段曲线 | 剔除 `sat != 0` 和 `anti_windup=1` 的估参样本 |
| 3 | AI 生成参数建议 | 参数变化幅度建议限制在当前值的 10% 到 30% |
| 4 | 用户确认或自动模式批准 | 第一版建议必须人工确认 |
| 5 | 上位机发送 `{CMD}SET_PID` | 发送前检查参数上下限 |
| 6 | 板端回复 `{ACK}` | 没有 ACK 不认为生效 |
| 7 | 继续观察 `{PID}` | 对比超调、稳定时间和稳态误差 |

## 9. 上位机字段配置模板

如果上位机支持用户自定义字段映射，第一版可以内置下面两个模板。

`{PID}` 模板：

```text
seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

`{CFG}` 模板：

```text
profile_id,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

## 10. 板端接入注意事项

| 注意事项 | 说明 |
|---|---|
| 不要在高频中断里直接 `printf` | 建议中断只置标志或写环形缓冲，主循环里发送 |
| `dt_ms` 要尽量真实 | AI 诊断周期抖动和微分项时依赖这个字段 |
| `feedback` 必须来自传感器 | 不要把 PID `out` 当作反馈值 |
| `out_raw` 和 `out_limited` 要分开 | 这样才能判断输出是否被执行器限制 |
| 参数下发要回复 ACK/ERR | 否则上位机无法确认参数是否生效 |
| 自动调参要有限制 | 必须设置 `kp/ki/kd` 的允许范围、输出范围和急停逻辑 |

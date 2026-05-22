#ifndef PID_AI_H
#define PID_AI_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 枚举作用：
 *   描述 PID 控制器当前的控制模式。
 *
 * 取值说明：
 *   PIDAI_MODE_STOP   表示停止输出，执行器输出会被置为 0。
 *   PIDAI_MODE_AUTO   表示自动 PID 模式，执行器输出来自 PID 计算结果。
 *   PIDAI_MODE_MANUAL 表示手动输出模式，执行器输出来自 manual_out。
 */
typedef enum
{
    PIDAI_MODE_STOP = 0,
    PIDAI_MODE_AUTO = 1,
    PIDAI_MODE_MANUAL = 2
} PIDAI_Mode;

/*
 * 枚举作用：
 *   描述 PID 输出是否被执行器上下限限制。
 *
 * 取值说明：
 *   PIDAI_SAT_LOW  表示输出低于下限，已经被限制到 out_min。
 *   PIDAI_SAT_NONE 表示输出未饱和。
 *   PIDAI_SAT_HIGH 表示输出高于上限，已经被限制到 out_max。
 */
typedef enum
{
    PIDAI_SAT_LOW = -1,
    PIDAI_SAT_NONE = 0,
    PIDAI_SAT_HIGH = 1
} PIDAI_Saturation;

/*
 * 枚举作用：
 *   描述 PID 库内部检测到的故障或异常状态。
 *
 * 取值说明：
 *   PIDAI_FAULT_NONE        表示没有故障。
 *   PIDAI_FAULT_BAD_POINTER 表示调用者传入了空指针。
 *   PIDAI_FAULT_BAD_DT      表示 PID 周期无效。
 *   PIDAI_FAULT_BAD_LIMIT   表示上下限配置无效。
 *   PIDAI_FAULT_PARAM_RANGE 表示命令参数超出库允许范围。
 */
typedef enum
{
    PIDAI_FAULT_NONE = 0,
    PIDAI_FAULT_BAD_POINTER = 1,
    PIDAI_FAULT_BAD_DT = 2,
    PIDAI_FAULT_BAD_LIMIT = 4,
    PIDAI_FAULT_PARAM_RANGE = 8
} PIDAI_Fault;

/*
 * 结构体作用：
 *   保存一个 PID 控制器的所有运行状态、调试量和上位机诊断所需字段。
 *
 * 字段分组说明：
 *   参数区保存 kp/ki/kd/kf 和各种限幅值。
 *   输入区保存 target、feedback 和传感器状态。
 *   计算区保存 error、integral、P/I/D 输出和限幅前后输出。
 *   状态区保存模式、使能、饱和、故障、时间戳和遥测序号。
 *
 * 移植说明：
 *   该结构体不包含任何平台相关句柄，STM32 HAL、ESP-IDF、Arduino 都可以直接使用。
 */
typedef struct
{
    float kp;              /* 比例系数，用于根据当前误差计算 P 项输出。 */
    float ki;              /* 积分系数，用于根据累计误差计算 I 项输出。 */
    float kd;              /* 微分系数，用于根据误差变化量计算 D 项输出。 */
    float kf;              /* 前馈系数，用于按 target 生成 ff_out；不需要前馈时保持 0。 */

    float target;          /* 控制目标值，例如目标速度、目标角度或目标温度。 */
    float feedback;        /* 实际反馈值，由传感器测量得到。 */
    int sensor_ok;         /* 传感器状态，1 表示数据可信，0 表示传感器异常或数据无效。 */

    float error;           /* 当前误差，通常等于 target - feedback。 */
    float last_error;      /* 上一次误差，用于计算误差变化量。 */
    float last_feedback;   /* 上一次反馈值，用于对 feedback 求导计算 D 项，避免 target 阶跃时产生尖峰。 */
    float d_error;         /* 误差变化量，新算法下等于 feedback - last_feedback，仅用于 D 项物理诊断。 */
    float integral;        /* 积分累计量，用于计算 I 项输出。 */

    float p_out;           /* P 项输出，等于 kp * error。 */
    float i_out;           /* I 项输出，等于 ki * integral。 */
    float d_out;           /* D 项输出，等于 kd * d_error。 */
    float ff_out;          /* 前馈输出，默认等于 kf * target。 */

    float out_raw;         /* PID 原始输出，尚未经过输出限幅。 */
    float out_limited;     /* 限幅后的 PID 输出。 */
    float actuator;        /* 最终给执行器的值，例如 PWM、DAC 或电流指令。 */
    float manual_out;      /* 手动模式下请求的执行器输出。 */

    float out_min;         /* 执行器输出下限。 */
    float out_max;         /* 执行器输出上限。 */
    float integral_min;    /* 积分累计下限，用于限制积分项继续变大。 */
    float integral_max;    /* 积分累计上限，用于限制积分项继续变大。 */

    PIDAI_Saturation sat;  /* 饱和标志，用于上位机判断输出是否被限幅。 */
    PIDAI_Mode mode;       /* 当前控制模式，决定 actuator 来自 PID、手动输出还是停止输出。 */
    int enable;            /* PID 使能标志，1 表示允许输出，0 表示强制停止输出。 */
    int anti_windup;       /* 本次计算是否触发积分抗饱和，1 表示触发，0 表示未触发。 */
    int reverse;           /* 控制方向反向标志，1 表示误差取反，0 表示正常方向。 */
    uint32_t fault;        /* 故障位图，用于记录配置或运行异常。 */

    uint32_t seq;          /* 遥测帧序号，每执行一次 PIDAI_Update 会递增一次。 */
    uint32_t ms;           /* 单片机当前时间戳，单位 ms，由调用者传入。 */
    float dt_ms;           /* 本次 PID 周期，单位 ms，由调用者传入。 */
} PIDAI_Handle;

/*
 * 函数作用：
 *   初始化 PID 控制器结构体，设置安全默认值。
 *
 * 主要流程：
 *   1. 清空结构体中的运行状态。
 *   2. 设置默认输出范围为 -1000 到 1000。
 *   3. 设置默认积分范围为 -10000 到 10000。
 *   4. 设置默认模式为停止，传感器状态为正常。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *
 * 返回值：
 *   返回 0 表示初始化成功，返回负数表示参数错误。
 */
int PIDAI_Init(PIDAI_Handle *pid);

/*
 * 函数作用：
 *   复位 PID 运行状态，但保留 kp/ki/kd、target、manual_out、输出范围、积分范围等配置。
 *
 * 主要流程：
 *   清空误差、积分、P/I/D 输出、饱和状态、故障状态和遥测序号，同时保留上位机配置。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *
 * 返回值：
 *   返回 0 表示复位成功，返回负数表示参数错误。
 */
int PIDAI_Reset(PIDAI_Handle *pid);

/*
 * 函数作用：
 *   设置 PID 三个核心参数。
 *
 * 主要流程：
 *   检查参数范围后更新 kp、ki、kd。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *   kp  比例系数。
 *   ki  积分系数。
 *   kd  微分系数。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误或越界。
 */
int PIDAI_SetTunings(PIDAI_Handle *pid, float kp, float ki, float kd);

/*
 * 函数作用：
 *   设置前馈系数。
 *
 * 主要流程：
 *   检查参数范围后更新 kf，后续 ff_out 会按 kf * target 计算。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *   kf  前馈系数，不使用前馈时设置为 0。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误或越界。
 */
int PIDAI_SetFeedForward(PIDAI_Handle *pid, float kf);

/*
 * 函数作用：
 *   设置 PID 输出上下限。
 *
 * 主要流程：
 *   检查 out_min 必须小于 out_max，然后更新输出限幅。
 *
 * 参数说明：
 *   pid     指向 PID 控制器结构体。
 *   out_min 执行器输出下限。
 *   out_max 执行器输出上限。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误或上下限无效。
 */
int PIDAI_SetOutputLimits(PIDAI_Handle *pid, float out_min, float out_max);

/*
 * 函数作用：
 *   设置积分累计上下限。
 *
 * 主要流程：
 *   检查 integral_min 必须小于 integral_max，然后更新积分限幅。
 *
 * 参数说明：
 *   pid          指向 PID 控制器结构体。
 *   integral_min 积分累计下限。
 *   integral_max 积分累计上限。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误或上下限无效。
 */
int PIDAI_SetIntegralLimits(PIDAI_Handle *pid, float integral_min, float integral_max);

/*
 * 函数作用：
 *   设置目标值。
 *
 * 主要流程：
 *   将新的目标值写入 pid->target，后续 PIDAI_Update 会使用该目标。
 *
 * 参数说明：
 *   pid    指向 PID 控制器结构体。
 *   target 新的目标值。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误。
 */
int PIDAI_SetTarget(PIDAI_Handle *pid, float target);

/*
 * 函数作用：
 *   设置 PID 控制模式。
 *
 * 主要流程：
 *   检查模式枚举是否有效，然后更新 pid->mode。
 *
 * 参数说明：
 *   pid  指向 PID 控制器结构体。
 *   mode 新的控制模式。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误或模式无效。
 */
int PIDAI_SetMode(PIDAI_Handle *pid, PIDAI_Mode mode);

/*
 * 函数作用：
 *   设置 PID 使能状态。
 *
 * 主要流程：
 *   将任意非零 enable 规范化为 1，将 0 保持为 0。
 *
 * 参数说明：
 *   pid    指向 PID 控制器结构体。
 *   enable 1 表示使能输出，0 表示禁止输出。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误。
 */
int PIDAI_Enable(PIDAI_Handle *pid, int enable);

/*
 * 函数作用：
 *   设置传感器状态。
 *
 * 主要流程：
 *   将 sensor_ok 写入 PID 状态，传感器异常时 PIDAI_Update 会强制停止输出。
 *
 * 参数说明：
 *   pid       指向 PID 控制器结构体。
 *   sensor_ok 1 表示传感器正常，0 表示传感器异常。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误。
 */
int PIDAI_SetSensorOk(PIDAI_Handle *pid, int sensor_ok);

/*
 * 函数作用：
 *   设置控制方向是否反向。
 *
 * 主要流程：
 *   将 reverse 规范化为 0 或 1，反向时误差会变为 feedback - target。
 *
 * 参数说明：
 *   pid     指向 PID 控制器结构体。
 *   reverse 1 表示反向控制，0 表示正常控制。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误。
 */
int PIDAI_SetReverse(PIDAI_Handle *pid, int reverse);

/*
 * 函数作用：
 *   设置手动模式请求输出。
 *
 * 主要流程：
 *   保存 manual_out；在手动模式下 PIDAI_Update 会对该值限幅后写入 actuator。
 *
 * 参数说明：
 *   pid        指向 PID 控制器结构体。
 *   manual_out 手动输出请求值。
 *
 * 返回值：
 *   返回 0 表示设置成功，返回负数表示参数错误。
 */
int PIDAI_SetManualOutput(PIDAI_Handle *pid, float manual_out);

/*
 * 函数作用：
 *   清空积分累计和 I 项输出。
 *
 * 主要流程：
 *   将 integral 和 i_out 置零，用于上位机切换参数或目标后避免旧积分影响。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *
 * 返回值：
 *   返回 0 表示清空成功，返回负数表示参数错误。
 */
int PIDAI_ResetIntegral(PIDAI_Handle *pid);

/*
 * 函数作用：
 *   清除 PID 故障位图。
 *
 * 主要流程：
 *   将 fault 设置为 PIDAI_FAULT_NONE。
 *
 * 参数说明：
 *   pid 指向 PID 控制器结构体。
 *
 * 返回值：
 *   返回 0 表示清除成功，返回负数表示参数错误。
 */
int PIDAI_ClearFault(PIDAI_Handle *pid);

/*
 * 函数作用：
 *   执行一次 PID 计算并更新全部遥测字段。
 *
 * 主要流程：
 *   1. 保存反馈值、时间戳和周期。
 *   2. 根据目标值和反馈值计算误差。
 *   3. 更新积分并计算 P/I/D/前馈输出。
 *   4. 计算原始输出并执行输出限幅。
 *   5. 根据模式、使能和传感器状态决定 actuator。
 *   6. 更新 last_error、seq 和饱和状态。
 *
 * 参数说明：
 *   pid      指向 PID 控制器结构体。
 *   feedback 本次传感器反馈值。
 *   ms       当前单片机时间戳，单位 ms。
 *   dt_ms    本次 PID 周期，单位 ms。
 *
 * 返回值：
 *   返回最终执行器输出 actuator；如果 pid 为空则返回 0。
 */
float PIDAI_Update(PIDAI_Handle *pid, float feedback, uint32_t ms, float dt_ms);

#ifdef __cplusplus
}
#endif

#endif

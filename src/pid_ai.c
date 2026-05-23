#include "pid_ai.h"

#include <float.h>
#include <stdint.h>
#include <string.h>

/* 参数绝对值上限，用于拦截串口误包导致的离谱 PID 参数。 */
#define PIDAI_PARAM_ABS_MAX 1000000.0f

/* 默认输出下限，兼容双向执行器；单向 PWM 项目可通过 SET_OUT_LIMIT 改为 0。 */
#define PIDAI_DEFAULT_OUT_MIN (-1000.0f)

/* 默认输出上限，兼容常见 PWM 或归一化控制输出。 */
#define PIDAI_DEFAULT_OUT_MAX (1000.0f)

/* 默认积分下限，用于避免积分无限堆积。 */
#define PIDAI_DEFAULT_I_MIN (-10000.0f)

/* 默认积分上限，用于避免积分无限堆积。 */
#define PIDAI_DEFAULT_I_MAX (10000.0f)

/*
 * last_feedback 未初始化哨兵的 IEEE 754 位模式（quiet NaN）。
 *
 * 使用 NaN 而不是 seq 计数器来判断"首次闭环计算"，原因：
 *   seq 在 Update() 入口就递增，STOP/disable/MANUAL/bad-dt 帧都会增加 seq，
 *   导致 seq==1 条件在真正的第一次 AUTO 计算到达之前就已失效。
 *   NaN 哨兵不依赖计数器，只要 last_feedback 未被有效 feedback 覆盖，条件就成立。
 */
#define PIDAI_LAST_FEEDBACK_UNINIT_BITS (0x7FC00000U)

/*
 * 函数作用：
 *   将 last_feedback 写成内部 NaN 哨兵，表示 D 项尚未拥有有效前一帧 feedback。
 *
 * 主要流程：
 *   1. 使用 uint32_t 保存 quiet NaN 的固定 IEEE 754 位模式。
 *   2. 通过 memcpy 写入 float 字段，避免违反严格别名规则。
 *   3. 不使用 C99 compound literal，兼容更保守的 C90 MCU 编译器警告设置。
 *
 * 参数说明：
 *   pid 指向需要标记 D 项前值未初始化的 PID 控制器，调用方保证非空。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_MarkLastFeedbackUninitialized(PIDAI_Handle *pid)
{
    uint32_t bits;

    bits = PIDAI_LAST_FEEDBACK_UNINIT_BITS;
    memcpy(&pid->last_feedback, &bits, sizeof(pid->last_feedback));
}

/*
 * 函数作用：
 *   判断一个浮点数是否是可接受的有限参数。
 *
 * 主要流程：
 *   1. 通过 value == value 排除 NaN。
 *   2. 通过 FLT_MAX 排除无穷大。
 *   3. 通过库内固定上限排除串口误码造成的异常大参数。
 *
 * 参数说明：
 *   value 需要检查的浮点值。
 *
 * 返回值：
 *   返回 1 表示参数可接受，返回 0 表示参数无效。
 */
static int PIDAI_IsFiniteParam(float value)
{
    if (value != value) {
        return 0;
    }

    if (value > FLT_MAX || value < -FLT_MAX) {
        return 0;
    }

    if (value > PIDAI_PARAM_ABS_MAX || value < -PIDAI_PARAM_ABS_MAX) {
        return 0;
    }

    return 1;
}

/*
 * 函数作用：
 *   将输入值限制在指定上下限内，同时返回饱和方向。
 *
 * 主要流程：
 *   1. 如果 value 高于 max，则返回 max 并标记上限饱和。
 *   2. 如果 value 低于 min，则返回 min 并标记下限饱和。
 *   3. 如果 value 在范围内，则原样返回并标记未饱和。
 *
 * 参数说明：
 *   value 需要限幅的原始值。
 *   min   允许的最小值。
 *   max   允许的最大值。
 *   sat   输出参数，用于保存饱和方向。
 *
 * 返回值：
 *   返回限幅后的值。
 */
static float PIDAI_ClampWithSat(float value, float min, float max, PIDAI_Saturation *sat)
{
    if (value > max) {
        if (sat != 0) {
            *sat = PIDAI_SAT_HIGH;
        }
        return max;
    }

    if (value < min) {
        if (sat != 0) {
            *sat = PIDAI_SAT_LOW;
        }
        return min;
    }

    if (sat != 0) {
        *sat = PIDAI_SAT_NONE;
    }
    return value;
}

/*
 * 函数作用：
 *   验证输出或积分上下限是否合法。
 *
 * 主要流程：
 *   1. 检查两个边界是否为有限参数。
 *   2. 检查下限必须严格小于上限。
 *
 * 参数说明：
 *   min 下限。
 *   max 上限。
 *
 * 返回值：
 *   返回 1 表示上下限合法，返回 0 表示上下限无效。
 */
static int PIDAI_IsValidLimit(float min, float max)
{
    if (!PIDAI_IsFiniteParam(min) || !PIDAI_IsFiniteParam(max)) {
        return 0;
    }

    return min < max;
}

int PIDAI_Init(PIDAI_Handle *pid)
{
    if (pid == 0) {
        return -1;
    }

    /* 初始化时整体清零，确保所有遥测字段都有确定初值。 */
    memset(pid, 0, sizeof(*pid));

    pid->out_min = PIDAI_DEFAULT_OUT_MIN;
    pid->out_max = PIDAI_DEFAULT_OUT_MAX;
    pid->integral_min = PIDAI_DEFAULT_I_MIN;
    pid->integral_max = PIDAI_DEFAULT_I_MAX;
    pid->mode = PIDAI_MODE_STOP;
    pid->enable = 0;
    pid->sensor_ok = 1;
    pid->sat = PIDAI_SAT_NONE;
    pid->fault = PIDAI_FAULT_NONE;
    /* last_feedback NaN 哨兵：标记"尚未有有效前值"，首次 AUTO 帧到达时跳过 D 项计算。 */
    PIDAI_MarkLastFeedbackUninitialized(pid);

    return 0;
}

int PIDAI_Reset(PIDAI_Handle *pid)
{
    float kp;
    float ki;
    float kd;
    float kf;
    float target;       /* RESET 后继续使用的控制目标值，避免上位机刚下发的目标被清零。 */
    float manual_out;   /* RESET 后继续使用的手动输出请求，避免手动模式恢复时输出配置丢失。 */
    float out_min;
    float out_max;
    float integral_min;
    float integral_max;
    PIDAI_Mode mode;
    int enable;
    int reverse;

    if (pid == 0) {
        return -1;
    }

    /* 复位运行状态时保留用户配置，避免 RESET 后丢失上位机刚下发的参数、目标或手动输出。 */
    kp = pid->kp;
    ki = pid->ki;
    kd = pid->kd;
    kf = pid->kf;
    target = pid->target;
    manual_out = pid->manual_out;
    out_min = pid->out_min;
    out_max = pid->out_max;
    integral_min = pid->integral_min;
    integral_max = pid->integral_max;
    mode = pid->mode;
    enable = pid->enable;
    reverse = pid->reverse;

    memset(pid, 0, sizeof(*pid));

    pid->kp = kp;
    pid->ki = ki;
    pid->kd = kd;
    pid->kf = kf;
    pid->target = target;
    pid->manual_out = manual_out;
    pid->out_min = out_min;
    pid->out_max = out_max;
    pid->integral_min = integral_min;
    pid->integral_max = integral_max;
    pid->mode = mode;
    pid->enable = enable;
    pid->reverse = reverse;
    pid->sensor_ok = 1;
    pid->sat = PIDAI_SAT_NONE;
    pid->fault = PIDAI_FAULT_NONE;
    /* Reset 后 last_feedback 同样置为 NaN 哨兵，确保下一次 AUTO 帧不产生 D 项尖峰。 */
    PIDAI_MarkLastFeedbackUninitialized(pid);

    return 0;
}

int PIDAI_SetTunings(PIDAI_Handle *pid, float kp, float ki, float kd)
{
    if (pid == 0) {
        return -1;
    }

    /* 本库约定 PID 系数非负，控制方向由 reverse 字段统一处理。 */
    if (!PIDAI_IsFiniteParam(kp) || !PIDAI_IsFiniteParam(ki) || !PIDAI_IsFiniteParam(kd) ||
        kp < 0.0f || ki < 0.0f || kd < 0.0f) {
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
        return -2;
    }

    pid->kp = kp;
    pid->ki = ki;
    pid->kd = kd;
    return 0;
}

int PIDAI_SetFeedForward(PIDAI_Handle *pid, float kf)
{
    if (pid == 0) {
        return -1;
    }

    /* 前馈允许为负，因为某些机械结构需要目标值反向补偿。 */
    if (!PIDAI_IsFiniteParam(kf)) {
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
        return -2;
    }

    pid->kf = kf;
    return 0;
}

int PIDAI_SetOutputLimits(PIDAI_Handle *pid, float out_min, float out_max)
{
    if (pid == 0) {
        return -1;
    }

    if (!PIDAI_IsValidLimit(out_min, out_max)) {
        pid->fault |= PIDAI_FAULT_BAD_LIMIT;
        return -2;
    }

    pid->out_min = out_min;
    pid->out_max = out_max;

    /* 限幅改变后同步裁剪已有输出，防止下一帧遥测出现旧范围下的值。 */
    pid->out_limited = PIDAI_ClampWithSat(pid->out_limited, pid->out_min, pid->out_max, &pid->sat);
    pid->actuator = PIDAI_ClampWithSat(pid->actuator, pid->out_min, pid->out_max, &pid->sat);
    return 0;
}

int PIDAI_SetIntegralLimits(PIDAI_Handle *pid, float integral_min, float integral_max)
{
    if (pid == 0) {
        return -1;
    }

    if (!PIDAI_IsValidLimit(integral_min, integral_max)) {
        pid->fault |= PIDAI_FAULT_BAD_LIMIT;
        return -2;
    }

    pid->integral_min = integral_min;
    pid->integral_max = integral_max;
    pid->integral = PIDAI_ClampWithSat(pid->integral, pid->integral_min, pid->integral_max, 0);
    pid->i_out = pid->ki * pid->integral;
    return 0;
}

int PIDAI_SetTarget(PIDAI_Handle *pid, float target)
{
    if (pid == 0) {
        return -1;
    }

    if (!PIDAI_IsFiniteParam(target)) {
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
        return -2;
    }

    pid->target = target;
    return 0;
}

int PIDAI_SetMode(PIDAI_Handle *pid, PIDAI_Mode mode)
{
    if (pid == 0) {
        return -1;
    }

    if (mode != PIDAI_MODE_STOP && mode != PIDAI_MODE_AUTO && mode != PIDAI_MODE_MANUAL) {
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
        return -2;
    }

    pid->mode = mode;
    return 0;
}

int PIDAI_Enable(PIDAI_Handle *pid, int enable)
{
    if (pid == 0) {
        return -1;
    }

    pid->enable = enable ? 1 : 0;
    return 0;
}

int PIDAI_SetSensorOk(PIDAI_Handle *pid, int sensor_ok)
{
    if (pid == 0) {
        return -1;
    }

    pid->sensor_ok = sensor_ok ? 1 : 0;
    return 0;
}

int PIDAI_SetReverse(PIDAI_Handle *pid, int reverse)
{
    if (pid == 0) {
        return -1;
    }

    pid->reverse = reverse ? 1 : 0;
    return 0;
}

int PIDAI_SetManualOutput(PIDAI_Handle *pid, float manual_out)
{
    if (pid == 0) {
        return -1;
    }

    if (!PIDAI_IsFiniteParam(manual_out)) {
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
        return -2;
    }

    pid->manual_out = manual_out;
    return 0;
}

int PIDAI_ResetIntegral(PIDAI_Handle *pid)
{
    if (pid == 0) {
        return -1;
    }

    pid->integral = 0.0f;
    pid->i_out = 0.0f;
    return 0;
}

int PIDAI_ClearFault(PIDAI_Handle *pid)
{
    if (pid == 0) {
        return -1;
    }

    pid->fault = PIDAI_FAULT_NONE;
    return 0;
}

float PIDAI_Update(PIDAI_Handle *pid, float feedback, uint32_t ms, float dt_ms)
{
    float next_integral;
    float computed_error;
    float candidate_raw;
    float candidate_limited;
    float d_sign;
    PIDAI_Saturation candidate_sat;

    if (pid == 0) {
        return 0.0f;
    }

    pid->ms = ms;
    pid->seq += 1U;
    pid->anti_windup = 0;

    if (!PIDAI_IsFiniteParam(feedback)) {
        /*
         * feedback 非有限时不能直接写入遥测字段，否则 {PID} 会输出 nan/inf 并被上位机丢弃。
         * 保留上一帧有限 feedback，同时用 sensor_ok=0 和 fault 明确暴露传感器异常。
         */
        pid->sensor_ok = 0;
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
    } else {
        pid->feedback = feedback;
    }

    if (dt_ms <= 0.0f || !PIDAI_IsFiniteParam(dt_ms)) {
        /*
         * dt_ms 非法时将可序列化周期规范为 0.0，避免安全停机帧中出现 nan/inf。
         * BAD_DT fault 是真实错误信号，调用方和上位机据此停止调参或控制输出。
         */
        pid->dt_ms = 0.0f;
        pid->fault |= PIDAI_FAULT_BAD_DT;
        pid->out_raw = 0.0f;
        pid->out_limited = 0.0f;
        pid->actuator = 0.0f;
        pid->sat = PIDAI_SAT_NONE;
        return pid->actuator;
    }
    pid->dt_ms = dt_ms;

    if (pid->enable == 0 || pid->mode == PIDAI_MODE_STOP || pid->sensor_ok == 0) {
        pid->out_raw = 0.0f;
        pid->out_limited = 0.0f;
        pid->actuator = 0.0f;
        pid->sat = PIDAI_SAT_NONE;
        return pid->actuator;
    }

    if (pid->mode == PIDAI_MODE_MANUAL) {
        pid->out_raw = pid->manual_out;
        pid->out_limited = PIDAI_ClampWithSat(pid->manual_out, pid->out_min, pid->out_max, &pid->sat);
        pid->actuator = pid->out_limited;
        return pid->actuator;
    }

    /* reverse 用于统一处理反向执行器，避免用户通过负 PID 参数绕过诊断逻辑。 */
    computed_error = pid->reverse ? (pid->feedback - pid->target) : (pid->target - pid->feedback);
    pid->error = computed_error;

    /*
     * D 项对 feedback 求值，避免 target 阶跃时产生尖峰（Derivative Kick 问题）。
     * 普通方向下 feedback 增大代表误差减小，D 项取负；reverse 下误差定义反转，D 项也要反向。
     * dt_ms / 1000.0f 把变化量归一到秒，使 kd 的物理含义与采样周期无关。
     *
     * 用 NaN 哨兵判断"首次闭环计算"，而不是 seq 计数器：
     *   seq 在 Update() 入口递增，STOP/disable/MANUAL/bad-dt 帧都会增加 seq，
     *   导致 seq==1 条件在真正的第一次 AUTO 帧到达之前就已失效。
     *   Init() 和 Reset() 将 last_feedback 置为 NaN 哨兵；
     *   只要 last_feedback 是 NaN（自比较不等），就跳过 D 项计算并初始化前值。
     */
    if (pid->last_feedback != pid->last_feedback)
    {
        pid->last_feedback = pid->feedback;
        pid->d_error = 0.0f;
        pid->d_out   = 0.0f;
    }
    else
    {
        pid->d_error = pid->feedback - pid->last_feedback;
        d_sign = pid->reverse ? 1.0f : -1.0f;
        pid->d_out = d_sign * pid->kd * pid->d_error / (dt_ms / 1000.0f);
    }

    /* 积分归一化到物理时间单位（秒），使 ki 的含义与控制周期无关。 */
    next_integral = pid->integral + pid->error * (dt_ms / 1000.0f);
    next_integral = PIDAI_ClampWithSat(next_integral, pid->integral_min, pid->integral_max, 0);

    pid->p_out  = pid->kp * pid->error;
    pid->i_out  = pid->ki * next_integral;
    /* d_out 已在上面计算。 */
    pid->ff_out = pid->kf * pid->target;

    candidate_raw     = pid->p_out + pid->i_out + pid->d_out + pid->ff_out;
    candidate_limited = PIDAI_ClampWithSat(candidate_raw, pid->out_min, pid->out_max, &candidate_sat);

    /*
     * Conditional Integration 抗饱和策略：
     *   仅在输出未饱和，或饱和方向与误差方向相反（误差正在把输出拉离饱和边界）时
     *   才累计积分；比 clamping 策略更稳定，不会在饱和边界附近振荡。
     */
    if (candidate_sat == PIDAI_SAT_NONE ||
        (candidate_sat == PIDAI_SAT_HIGH && pid->error < 0.0f) ||
        (candidate_sat == PIDAI_SAT_LOW  && pid->error > 0.0f))
    {
        pid->integral = next_integral;
        pid->anti_windup = 0;
    }
    else
    {
        pid->anti_windup = 1;
        pid->i_out = pid->ki * pid->integral;
        candidate_raw     = pid->p_out + pid->i_out + pid->d_out + pid->ff_out;
        candidate_limited = PIDAI_ClampWithSat(candidate_raw, pid->out_min, pid->out_max, &candidate_sat);
    }

    pid->out_raw       = candidate_raw;
    pid->out_limited   = candidate_limited;
    pid->actuator      = candidate_limited;
    pid->sat           = candidate_sat;
    pid->last_error    = pid->error;
    pid->last_feedback = pid->feedback;  /* 保存本次 feedback，供下次 D 项计算使用。 */

    return pid->actuator;
}

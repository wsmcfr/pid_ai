#include "pid_ai.h"

#include <float.h>
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

    return 0;
}

int PIDAI_Reset(PIDAI_Handle *pid)
{
    float kp;
    float ki;
    float kd;
    float kf;
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

    /* 复位运行状态时保留用户配置，避免 RESET 后丢失上位机刚下发的参数。 */
    kp = pid->kp;
    ki = pid->ki;
    kd = pid->kd;
    kf = pid->kf;
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
    PIDAI_Saturation candidate_sat;

    if (pid == 0) {
        return 0.0f;
    }

    pid->ms = ms;
    pid->dt_ms = dt_ms;
    pid->feedback = feedback;
    pid->seq += 1U;
    pid->anti_windup = 0;

    if (!PIDAI_IsFiniteParam(feedback)) {
        pid->sensor_ok = 0;
        pid->fault |= PIDAI_FAULT_PARAM_RANGE;
    }

    if (dt_ms <= 0.0f || !PIDAI_IsFiniteParam(dt_ms)) {
        pid->fault |= PIDAI_FAULT_BAD_DT;
        pid->out_raw = 0.0f;
        pid->out_limited = 0.0f;
        pid->actuator = 0.0f;
        pid->sat = PIDAI_SAT_NONE;
        return pid->actuator;
    }

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
    pid->d_error = pid->error - pid->last_error;

    next_integral = pid->integral + pid->error;
    next_integral = PIDAI_ClampWithSat(next_integral, pid->integral_min, pid->integral_max, 0);

    pid->p_out = pid->kp * pid->error;
    pid->i_out = pid->ki * next_integral;
    pid->d_out = pid->kd * pid->d_error;
    pid->ff_out = pid->kf * pid->target;

    candidate_raw = pid->p_out + pid->i_out + pid->d_out + pid->ff_out;
    candidate_limited = PIDAI_ClampWithSat(candidate_raw, pid->out_min, pid->out_max, &candidate_sat);

    /*
     * 简单积分抗饱和策略：
     *   当输出已顶到上限且误差仍推动输出继续增大，或输出已顶到下限且误差仍推动输出继续减小时，
     *   撤销本次积分，避免 integral 越积越大导致解除饱和后严重超调。
     */
    if ((candidate_sat == PIDAI_SAT_HIGH && pid->error > 0.0f) ||
        (candidate_sat == PIDAI_SAT_LOW && pid->error < 0.0f)) {
        pid->anti_windup = 1;
        pid->i_out = pid->ki * pid->integral;
        candidate_raw = pid->p_out + pid->i_out + pid->d_out + pid->ff_out;
        candidate_limited = PIDAI_ClampWithSat(candidate_raw, pid->out_min, pid->out_max, &candidate_sat);
    } else {
        pid->integral = next_integral;
    }

    pid->out_raw = candidate_raw;
    pid->out_limited = candidate_limited;
    pid->actuator = candidate_limited;
    pid->sat = candidate_sat;
    pid->last_error = pid->error;

    return pid->actuator;
}

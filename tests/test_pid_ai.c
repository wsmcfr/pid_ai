#include <math.h>
#include <stdio.h>
#include <string.h>

#include "pid_ai.h"
#include "pid_ai_protocol.h"

/* 测试容差，用于比较浮点 PID 计算结果，避免二进制浮点误差造成误判。 */
#define TEST_EPSILON 0.0001f

/*
 * 函数作用：
 *   判断两个浮点数是否在允许误差范围内相等。
 *
 * 主要流程：
 *   1. 计算两个数的差值绝对值。
 *   2. 与固定测试容差进行比较。
 *
 * 参数说明：
 *   actual   实际计算得到的值。
 *   expected 期望得到的值。
 *
 * 返回值：
 *   返回 1 表示相等，返回 0 表示不相等。
 */
static int float_close(float actual, float expected)
{
    return fabsf(actual - expected) < TEST_EPSILON;
}

/*
 * 函数作用：
 *   测试 PID 正常计算流程是否符合公式（dt 归一化 + Derivative on Measurement 算法）。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器并设置参数。
 *   2. 第一次调用 Update 让 last_feedback 同步为 7，避免初始 last_feedback=0 引起 D 项尖峰。
 *   3. 第二次调用 Update（feedback 不变），此时 d_error=0，整体输出可预测。
 *   4. 校验误差、积分、P/I/D 项、原始输出和限幅输出。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_pid_update_normal(void)
{
    PIDAI_Handle pid;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 2.0f, 0.5f, 1.0f);
    PIDAI_SetOutputLimits(&pid, -100.0f, 100.0f);
    PIDAI_SetIntegralLimits(&pid, -50.0f, 50.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);

    /* 先调用一次让 last_feedback 同步为 7，避免初始 last_feedback=0 时 D 项产生大冲击。 */
    PIDAI_Update(&pid, 7.0f, 10U, 10.0f);
    /* 第二次调用时 d_error=0，D 项为 0，整体输出可预测。 */
    PIDAI_Update(&pid, 7.0f, 20U, 10.0f);

    /*
     * 期望值（dt=10ms=0.01s 归一化后）：
     *   error    = 10 - 7 = 3
     *   d_error  = 7 - 7 = 0（feedback 不变）
     *   integral = 0.03 + 3 * 0.01 = 0.06
     *   p_out    = 2 * 3 = 6
     *   i_out    = 0.5 * 0.06 = 0.03
     *   d_out    = -1 * 0 / 0.01 = 0
     *   out_raw  = 6 + 0.03 + 0 = 6.03
     */
    if (!float_close(pid.error, 3.0f)) return 0;
    if (!float_close(pid.d_error, 0.0f)) return 0;
    if (!float_close(pid.integral, 0.06f)) return 0;
    if (!float_close(pid.p_out, 6.0f)) return 0;
    if (!float_close(pid.i_out, 0.03f)) return 0;
    if (!float_close(pid.d_out, 0.0f)) return 0;
    if (!float_close(pid.out_raw, 6.03f)) return 0;
    if (!float_close(pid.out_limited, 6.03f)) return 0;
    if (!float_close(pid.actuator, 6.03f)) return 0;
    if (pid.sat != PIDAI_SAT_NONE) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 PID 输出超过执行器范围时是否正确限幅、设置饱和标志并触发抗饱和。
 *
 * 主要流程：
 *   1. 设置较大的 Kp 让输出超过上限。
 *   2. 执行一次 PID 更新。
 *   3. 校验原始输出、限幅输出、饱和标志，以及 Conditional Integration 的 anti_windup 标志。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_pid_output_saturation(void)
{
    PIDAI_Handle pid;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 20.0f, 0.1f, 0.0f);
    PIDAI_SetOutputLimits(&pid, 0.0f, 100.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);

    PIDAI_Update(&pid, 0.0f, 20U, 20.0f);

    /*
     * P 项主导输出：p_out = 20 * 10 = 200，已经远超上限 100，
     * Conditional Integration 检测到饱和方向（HIGH）与误差方向（>0）一致，
     * 因此 anti_windup=1，integral 不累加。
     */
    if (!float_close(pid.out_limited, 100.0f)) return 0;
    if (!float_close(pid.actuator, 100.0f)) return 0;
    if (pid.sat != PIDAI_SAT_HIGH) return 0;
    if (pid.anti_windup != 1) return 0;
    if (!float_close(pid.integral, 0.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试手动模式是否绕开 PID 自动输出并直接使用手动输出。
 *
 * 主要流程：
 *   1. 设置手动模式和手动输出。
 *   2. 执行一次 PID 更新。
 *   3. 校验执行器输出是否等于限幅后的手动输出。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_manual_mode(void)
{
    PIDAI_Handle pid;

    PIDAI_Init(&pid);
    PIDAI_SetOutputLimits(&pid, 0.0f, 100.0f);
    PIDAI_SetManualOutput(&pid, 150.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_MANUAL);
    PIDAI_Enable(&pid, 1);

    PIDAI_Update(&pid, 20.0f, 30U, 30.0f);

    if (!float_close(pid.actuator, 100.0f)) return 0;
    if (pid.sat != PIDAI_SAT_HIGH) return 0;
    if (pid.mode != PIDAI_MODE_MANUAL) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试串口命令解析是否能正确修改 PID 参数。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器。
 *   2. 解析 SET_PID 命令。
 *   3. 校验 Kp、Ki、Kd 是否被更新。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_protocol_set_pid(void)
{
    PIDAI_Handle pid;
    PIDAI_CommandResult result;

    PIDAI_Init(&pid);
    result = PIDAI_ProtocolHandleCommand(&pid, "{CMD}SET_PID,1.200,0.030,0.080");

    if (result.status != PIDAI_CMD_OK) return 0;
    if (!float_close(pid.kp, 1.2f)) return 0;
    if (!float_close(pid.ki, 0.03f)) return 0;
    if (!float_close(pid.kd, 0.08f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 RESET 只复位运行状态，不丢失上位机已经下发的目标值和手动输出配置。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器并写入 target、manual_out、模式和使能状态。
 *   2. 执行一次 PID 更新，让运行态字段产生非零值。
 *   3. 通过协议 RESET 命令复位运行状态。
 *   4. 校验 result.status 为 PIDAI_CMD_OK，说明 RESET 命令已被协议层接受。
 *   5. 校验配置字段保留，运行态字段清零。
 *
 * 返回值：
 *   返回 1 表示 RESET 保留操作配置且清空运行状态。
 *   返回 0 表示命令状态、保留字段或清零字段任意一项不符合预期。
 */
static int test_protocol_reset_preserves_operator_config(void)
{
    PIDAI_Handle pid;
    PIDAI_CommandResult result;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.0f, 0.2f, 0.1f);
    PIDAI_SetOutputLimits(&pid, -50.0f, 50.0f);
    PIDAI_SetTarget(&pid, 25.0f);
    PIDAI_SetManualOutput(&pid, 12.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);
    PIDAI_Update(&pid, 20.0f, 10U, 10.0f);

    result = PIDAI_ProtocolHandleCommand(&pid, "{CMD}RESET");

    if (result.status != PIDAI_CMD_OK) return 0;
    if (!float_close(pid.target, 25.0f)) return 0;
    if (!float_close(pid.manual_out, 12.0f)) return 0;
    if (pid.mode != PIDAI_MODE_AUTO) return 0;
    if (pid.enable != 1) return 0;
    if (!float_close(pid.error, 0.0f)) return 0;
    if (!float_close(pid.integral, 0.0f)) return 0;
    if (pid.seq != 0U) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试命令参数数量必须精确匹配，避免多余字段被静默忽略。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器。
 *   2. 发送带 4 个参数的 SET_PID 命令。
 *   3. 校验 result.status 为 PIDAI_CMD_ARG_INVALID。
 *   4. 校验 result.detail 为 UNEXPECTED_ARG，且 PID 参数没有被部分更新。
 *
 * 返回值：
 *   返回 1 表示协议层拒绝多余参数且没有修改 PID 参数。
 *   返回 0 表示多余参数被错误接受、错误明细不对或状态被部分更新。
 */
static int test_protocol_rejects_extra_arguments(void)
{
    PIDAI_Handle pid;
    PIDAI_CommandResult result;

    PIDAI_Init(&pid);
    result = PIDAI_ProtocolHandleCommand(&pid, "{CMD}SET_PID,1.200,0.030,0.080,99.000");

    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "UNEXPECTED_ARG") != 0) return 0;
    if (!float_close(pid.kp, 0.0f)) return 0;
    if (!float_close(pid.ki, 0.0f)) return 0;
    if (!float_close(pid.kd, 0.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试命令末尾多出的空字段也会被拒绝，避免尾随逗号被当作合法命令。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器。
 *   2. 发送尾随逗号的 SET_PID 命令。
 *   3. 校验 result.status 为 PIDAI_CMD_ARG_INVALID。
 *   4. 校验 result.detail 为 UNEXPECTED_ARG，说明尾随空字段按多余参数处理。
 *
 * 返回值：
 *   返回 1 表示尾随空字段被拒绝。
 *   返回 0 表示尾随逗号被错误当作合法命令或错误明细不符合协议。
 */
static int test_protocol_rejects_trailing_empty_argument(void)
{
    PIDAI_Handle pid;
    PIDAI_CommandResult result;

    PIDAI_Init(&pid);
    result = PIDAI_ProtocolHandleCommand(&pid, "{CMD}SET_PID,1.200,0.030,0.080,");

    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "UNEXPECTED_ARG") != 0) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试遥测帧打包是否包含固定前缀和关键字段。
 *
 * 主要流程：
 *   1. 构造一次 PID 计算结果。
 *   2. 将当前状态打包成 {PID} 文本帧。
 *   3. 校验帧前缀和关键数值是否存在。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_build_telemetry_frame(void)
{
    char frame[256];
    PIDAI_Handle pid;
    int written;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.0f, 0.0f, 0.0f);
    PIDAI_SetOutputLimits(&pid, 0.0f, 100.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);
    PIDAI_Update(&pid, 7.0f, 40U, 10.0f);

    written = PIDAI_ProtocolBuildTelemetry(&pid, frame, sizeof(frame));

    if (written <= 0) return 0;
    if (strncmp(frame, "{PID}", 5U) != 0) return 0;
    if (strstr(frame, "10.000") == NULL) return 0;
    if (strstr(frame, "7.000") == NULL) return 0;
    if (strstr(frame, "3.000") == NULL) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 Derivative on Measurement 算法在 target 阶跃时不产生 D 项尖峰。
 *
 * 主要流程：
 *   1. 让 feedback 稳定在 10，调用一次 Update 让控制器进入稳态。
 *   2. 把 target 从 10 阶跃到 100，但 feedback 保持不变。
 *   3. 校验 D 项为 0（feedback 没变化），输出仅由 P 项构成。
 *
 * 返回值：
 *   返回 1 表示 D 项被正确抑制；返回 0 表示出现 Derivative Kick。
 */
static int test_derivative_kick_suppressed(void)
{
    PIDAI_Handle pid;
    float out2;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.0f, 0.0f, 1.0f);
    PIDAI_SetOutputLimits(&pid, -10000.0f, 10000.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);

    /* 第一次：feedback 稳定在 10，让 last_feedback 同步为 10。 */
    PIDAI_Update(&pid, 10.0f, 10U, 10.0f);

    /* target 阶跃到 100，但 feedback 不变；D 项应当为 0。 */
    PIDAI_SetTarget(&pid, 100.0f);
    out2 = PIDAI_Update(&pid, 10.0f, 20U, 10.0f);

    /* feedback 没变化，d_error=0，d_out=0；输出只剩 P 项 = kp*(100-10) = 90。 */
    if (!float_close(pid.d_out, 0.0f)) return 0;
    if (!float_close(out2, 90.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   运行所有 PID AI 库的基础测试。
 *
 * 主要流程：
 *   逐个执行测试函数；任何一个测试失败都会打印失败名称并返回非零退出码。
 *
 * 参数说明：
 *   无命令行参数。
 *
 * 返回值：
 *   返回 0 表示全部测试通过，返回 1 表示至少一个测试失败。
 */
int main(void)
{
    if (!test_pid_update_normal()) {
        printf("FAIL: test_pid_update_normal\n");
        return 1;
    }
    if (!test_pid_output_saturation()) {
        printf("FAIL: test_pid_output_saturation\n");
        return 1;
    }
    if (!test_manual_mode()) {
        printf("FAIL: test_manual_mode\n");
        return 1;
    }
    if (!test_protocol_set_pid()) {
        printf("FAIL: test_protocol_set_pid\n");
        return 1;
    }
    if (!test_protocol_reset_preserves_operator_config()) {
        printf("FAIL: test_protocol_reset_preserves_operator_config\n");
        return 1;
    }
    if (!test_protocol_rejects_extra_arguments()) {
        printf("FAIL: test_protocol_rejects_extra_arguments\n");
        return 1;
    }
    if (!test_protocol_rejects_trailing_empty_argument()) {
        printf("FAIL: test_protocol_rejects_trailing_empty_argument\n");
        return 1;
    }
    if (!test_build_telemetry_frame()) {
        printf("FAIL: test_build_telemetry_frame\n");
        return 1;
    }
    if (!test_derivative_kick_suppressed()) {
        printf("FAIL: test_derivative_kick_suppressed\n");
        return 1;
    }

    printf("PASS: all pid_ai tests\n");
    return 0;
}

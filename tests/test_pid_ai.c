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
 *   测试 PID 正常计算流程是否符合公式。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器并设置参数。
 *   2. 执行一次 PID 更新。
 *   3. 校验误差、P/I/D 项、原始输出和限幅输出。
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

    PIDAI_Update(&pid, 7.0f, 10U, 10.0f);

    if (!float_close(pid.error, 3.0f)) return 0;
    if (!float_close(pid.integral, 3.0f)) return 0;
    if (!float_close(pid.p_out, 6.0f)) return 0;
    if (!float_close(pid.i_out, 1.5f)) return 0;
    if (!float_close(pid.d_out, 3.0f)) return 0;
    if (!float_close(pid.out_raw, 10.5f)) return 0;
    if (!float_close(pid.out_limited, 10.5f)) return 0;
    if (!float_close(pid.actuator, 10.5f)) return 0;
    if (pid.sat != PIDAI_SAT_NONE) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 PID 输出超过执行器范围时是否正确限幅并设置饱和标志。
 *
 * 主要流程：
 *   1. 设置较大的 Kp 让输出超过上限。
 *   2. 执行一次 PID 更新。
 *   3. 校验原始输出、限幅输出和饱和标志。
 *
 * 返回值：
 *   返回 1 表示测试通过，返回 0 表示测试失败。
 */
static int test_pid_output_saturation(void)
{
    PIDAI_Handle pid;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 20.0f, 0.0f, 0.0f);
    PIDAI_SetOutputLimits(&pid, 0.0f, 100.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);

    PIDAI_Update(&pid, 0.0f, 20U, 20.0f);

    if (!float_close(pid.out_raw, 200.0f)) return 0;
    if (!float_close(pid.out_limited, 100.0f)) return 0;
    if (!float_close(pid.actuator, 100.0f)) return 0;
    if (pid.sat != PIDAI_SAT_HIGH) return 0;

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
    if (!test_build_telemetry_frame()) {
        printf("FAIL: test_build_telemetry_frame\n");
        return 1;
    }

    printf("PASS: all pid_ai tests\n");
    return 0;
}

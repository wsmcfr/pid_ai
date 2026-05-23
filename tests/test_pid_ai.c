#include <math.h>
#include <stdio.h>
#include <string.h>

#include "pid_ai.h"
#include "pid_ai_binary_protocol.h"
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
 *   构造一个用于多环协议测试的 loop 表。
 *
 * 主要流程：
 *   1. 初始化左右两个 PID 控制器。
 *   2. 写入不同目标值，便于确认命令只路由到匹配 loop。
 *   3. 填充调用方持有的固定 loop 表，不使用动态内存。
 *
 * 参数说明：
 *   left   左轮速度环 PID 句柄，不能为空。
 *   right  右轮速度环 PID 句柄，不能为空。
 *   routes 输出 loop 路由数组，至少包含 2 个元素。
 *
 * 返回值：
 *   无返回值；测试调用方保证参数有效。
 */
static void setup_two_loop_routes(PIDAI_Handle *left, PIDAI_Handle *right, PIDAI_LoopRoute routes[2])
{
    PIDAI_Init(left);
    PIDAI_Init(right);
    PIDAI_SetTarget(left, 100.0f);
    PIDAI_SetTarget(right, 200.0f);

    routes[0].loop_id = "speed_l";
    routes[0].loop_name = "left_speed";
    routes[0].pid = left;
    routes[0].profile_id = 1;
    routes[0].version = 3;

    routes[1].loop_id = "speed_r";
    routes[1].loop_name = "right_speed";
    routes[1].pid = right;
    routes[1].profile_id = 2;
    routes[1].version = 3;
}

/*
 * 函数作用：
 *   测试多环 {PIDX} 遥测帧是否按 loop_id、loop_name 和原 {PID} 字段顺序打包。
 *
 * 主要流程：
 *   1. 构造 speed_l loop 并执行一次 PID 更新。
 *   2. 调用 PIDAI_ProtocolBuildTelemetryX 打包多环遥测。
 *   3. 校验前缀、loop 标识和关键数值字段。
 *
 * 返回值：
 *   返回 1 表示 {PIDX} 字段顺序和关键内容正确；否则返回 0。
 */
static int test_build_pidx_frame(void)
{
    char frame[320];
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    int written;

    setup_two_loop_routes(&left, &right, routes);
    PIDAI_SetTunings(&left, 1.0f, 0.0f, 0.0f);
    PIDAI_SetOutputLimits(&left, 0.0f, 100.0f);
    PIDAI_SetMode(&left, PIDAI_MODE_AUTO);
    PIDAI_Enable(&left, 1);
    PIDAI_Update(&left, 80.0f, 50U, 10.0f);

    written = PIDAI_ProtocolBuildTelemetryX(&routes[0], frame, sizeof(frame));

    if (written <= 0) return 0;
    if (strncmp(frame, "{PIDX}speed_l,left_speed,", 25U) != 0) return 0;
    if (strstr(frame, ",50,10.000,100.000,80.000,20.000,") == NULL) return 0;
    if (strstr(frame, ",0,0,1,1,1,0") == NULL) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试多环 {CFGX} 配置帧是否按 loop 标识和原 {CFG} 配置字段顺序打包。
 *
 * 主要流程：
 *   1. 构造 speed_l loop 并写入 PID 参数和限幅。
 *   2. 调用 PIDAI_ProtocolBuildConfigX。
 *   3. 校验前缀、loop 标识、PID 参数、版本号和故障位图。
 *
 * 返回值：
 *   返回 1 表示 {CFGX} 配置帧符合扩展协议；否则返回 0。
 */
static int test_build_cfgx_frame(void)
{
    char frame[256];
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    int written;

    setup_two_loop_routes(&left, &right, routes);
    PIDAI_SetTunings(&left, 1.2f, 0.03f, 0.08f);
    PIDAI_SetFeedForward(&left, 0.01f);
    PIDAI_SetOutputLimits(&left, 0.0f, 1000.0f);
    PIDAI_SetIntegralLimits(&left, -500.0f, 500.0f);
    PIDAI_SetMode(&left, PIDAI_MODE_AUTO);

    written = PIDAI_ProtocolBuildConfigX(&routes[0], frame, sizeof(frame));

    if (written <= 0) return 0;
    if (strncmp(frame, "{CFGX}speed_l,left_speed,", 25U) != 0) return 0;
    if (strstr(frame, "1.200000,0.030000,0.080000,0.010000") == NULL) return 0;
    if (strstr(frame, ",0,1,3,0") == NULL) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 SET_PIDX 命令只会修改 loop_id 精确匹配的 PID 控制器。
 *
 * 主要流程：
 *   1. 构造左右轮两个 loop。
 *   2. 对 speed_r 发送 SET_PIDX。
 *   3. 校验右轮参数被更新，左轮参数保持不变。
 *
 * 返回值：
 *   返回 1 表示多环命令路由正确；否则返回 0。
 */
static int test_protocol_set_pidx_routes_to_matching_loop(void)
{
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_LoopTable table;
    PIDAI_CommandResult result;

    setup_two_loop_routes(&left, &right, routes);
    table.loops = routes;
    table.count = 2U;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,speed_r,1.200,0.030,0.080");

    if (result.status != PIDAI_CMD_OK) return 0;
    if (!float_close(left.kp, 0.0f) || !float_close(left.ki, 0.0f) || !float_close(left.kd, 0.0f)) return 0;
    if (!float_close(right.kp, 1.2f)) return 0;
    if (!float_close(right.ki, 0.03f)) return 0;
    if (!float_close(right.kd, 0.08f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试未知 loop_id 会被 SET_PIDX 拒绝，且不会修改任何 PID 参数。
 *
 * 主要流程：
 *   1. 构造只包含 speed_l/speed_r 的 loop 表。
 *   2. 发送 loop_id 为 yaw_rate_bad 的 SET_PIDX。
 *   3. 校验返回 ARG_INVALID/LOOP_NOT_FOUND，并确认左右 PID 均未改变。
 *
 * 返回值：
 *   返回 1 表示未知 loop 被安全拒绝；否则返回 0。
 */
static int test_protocol_set_pidx_rejects_unknown_loop(void)
{
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_LoopTable table;
    PIDAI_CommandResult result;

    setup_two_loop_routes(&left, &right, routes);
    table.loops = routes;
    table.count = 2U;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,yaw_rate_bad,1.200,0.030,0.080");

    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "LOOP_NOT_FOUND") != 0) return 0;
    if (!float_close(left.kp, 0.0f) || !float_close(right.kp, 0.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试多环命令入口会把无效 loop table 视为板端集成错误，而不是误报为未知 loop。
 *
 * 主要流程：
 *   1. 构造三类无效表：table 指针为空、loops 指针为空、count 为 0。
 *   2. 分别发送只读 GET_ALL_CFG 或分环 SET_PIDX 命令。
 *   3. 校验全部返回 INTERNAL_ERROR/NO_VALID_LOOP_TABLE，防止上位机误判为普通参数错误。
 *
 * 返回值：
 *   返回 1 表示无效 loop table 被统一拒绝；否则返回 0。
 */
static int test_protocol_x_rejects_invalid_loop_table(void)
{
    PIDAI_LoopTable null_routes;
    PIDAI_LoopRoute dummy_route;
    PIDAI_LoopTable zero_count;
    PIDAI_CommandResult result;

    null_routes.loops = 0;
    null_routes.count = 1U;
    zero_count.loops = &dummy_route;
    zero_count.count = 0U;

    result = PIDAI_ProtocolHandleCommandX(0, "{CMD}GET_ALL_CFG");
    if (result.status != PIDAI_CMD_INTERNAL_ERROR) return 0;
    if (strcmp(result.detail, "NO_VALID_LOOP_TABLE") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&null_routes, "{CMD}GET_ALL_CFG");
    if (result.status != PIDAI_CMD_INTERNAL_ERROR) return 0;
    if (strcmp(result.detail, "NO_VALID_LOOP_TABLE") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&zero_count, "{CMD}SET_PIDX,speed_l,1.000,0.100,0.010");
    if (result.status != PIDAI_CMD_INTERNAL_ERROR) return 0;
    if (strcmp(result.detail, "NO_VALID_LOOP_TABLE") != 0) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试分环命令必须先验证 loop_id，再解析后续数值参数。
 *
 * 主要流程：
 *   1. 构造只包含 speed_l/speed_r 的 loop 表。
 *   2. 对未知 loop_id 发送带坏数字的 SET_PIDX 和 SET_KFX。
 *   3. 校验错误原因仍为 LOOP_NOT_FOUND，且任何 PID 参数都不被修改。
 *
 * 返回值：
 *   返回 1 表示 loop 路由错误优先级符合协议；否则返回 0。
 */
static int test_protocol_x_unknown_loop_wins_over_bad_args(void)
{
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_LoopTable table;
    PIDAI_CommandResult result;

    setup_two_loop_routes(&left, &right, routes);
    table.loops = routes;
    table.count = 2U;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,missing,nope,0.100,0.010");
    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "LOOP_NOT_FOUND") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_KFX,missing,nope");
    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "LOOP_NOT_FOUND") != 0) return 0;

    if (!float_close(left.kp, 0.0f) || !float_close(right.kp, 0.0f)) return 0;
    if (!float_close(left.kf, 0.0f) || !float_close(right.kf, 0.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 SET_PIDX 对缺参、坏数字、多余参数和尾随逗号都执行精确拒绝。
 *
 * 主要流程：
 *   1. 构造多环表。
 *   2. 分别发送缺参、坏数字、多余参数、尾随逗号四类非法命令。
 *   3. 校验错误状态和 detail，且目标 PID 参数不发生部分更新。
 *
 * 返回值：
 *   返回 1 表示所有非法输入都被拒绝；否则返回 0。
 */
static int test_protocol_set_pidx_rejects_malformed_commands(void)
{
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_LoopTable table;
    PIDAI_CommandResult result;

    setup_two_loop_routes(&left, &right, routes);
    table.loops = routes;
    table.count = 2U;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,speed_l,1.000,0.200");
    if (result.status != PIDAI_CMD_ARG_MISSING) return 0;
    if (strcmp(result.detail, "NEED_LOOP_KP_KI_KD") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,speed_l,1.000,nope,0.200");
    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "FLOAT_PARSE_FAIL") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,speed_l,1.000,0.100,0.200,9.000");
    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "UNEXPECTED_ARG") != 0) return 0;

    result = PIDAI_ProtocolHandleCommandX(&table, "{CMD}SET_PIDX,speed_l,1.000,0.100,0.200,");
    if (result.status != PIDAI_CMD_ARG_INVALID) return 0;
    if (strcmp(result.detail, "UNEXPECTED_ARG") != 0) return 0;

    if (!float_close(left.kp, 0.0f) || !float_close(left.ki, 0.0f) || !float_close(left.kd, 0.0f)) return 0;

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
 *   测试非法 feedback 和 dt_ms 不会把 NaN/Inf 写入可序列化遥测字段。
 *
 * 主要流程：
 *   1. 先运行一帧合法 PID，建立上一帧有限 feedback。
 *   2. 注入 NAN feedback，要求输出停机、sensor_ok=0、fault 可见，遥测字符串不含 nan/inf。
 *   3. 注入 INFINITY dt_ms，要求 dt_ms 在遥测中规范为 0.000，避免上位机丢弃安全帧。
 *
 * 返回值：
 *   返回 1 表示安全遥测保持有限数；返回 0 表示非法输入污染协议帧。
 */
static int test_invalid_runtime_inputs_emit_finite_fault_telemetry(void)
{
    PIDAI_Handle pid;
    char frame[512];
    int written;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.0f, 0.1f, 0.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);
    PIDAI_Update(&pid, 7.0f, 10U, 10.0f);

    PIDAI_Update(&pid, NAN, 20U, 10.0f);
    written = PIDAI_ProtocolBuildTelemetry(&pid, frame, sizeof(frame));
    if (written <= 0) return 0;
    if (strstr(frame, "nan") != NULL || strstr(frame, "inf") != NULL) return 0;
    if (pid.sensor_ok != 0) return 0;
    if ((pid.fault & PIDAI_FAULT_PARAM_RANGE) == 0U) return 0;
    if (strstr(frame, ",0,") == NULL) return 0;
    if (strstr(frame, "7.000") == NULL) return 0;

    PIDAI_ClearFault(&pid);
    PIDAI_SetSensorOk(&pid, 1);
    PIDAI_Update(&pid, 8.0f, 30U, INFINITY);
    written = PIDAI_ProtocolBuildTelemetry(&pid, frame, sizeof(frame));
    if (written <= 0) return 0;
    if (strstr(frame, "nan") != NULL || strstr(frame, "inf") != NULL) return 0;
    if ((pid.fault & PIDAI_FAULT_BAD_DT) == 0U) return 0;
    if (strstr(frame, "{PID}3,30,0.000") == NULL) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试 reverse 模式下 D 项方向会随控制方向反转。
 *
 * 主要流程：
 *   1. 在 reverse=1 下让 feedback 先稳定到 10。
 *   2. feedback 上升到 12 时，反向误差 error=feedback-target 增大。
 *   3. 校验 d_out 为正，说明 D 项跟反向误差变化方向一致。
 *
 * 返回值：
 *   返回 1 表示 reverse 微分方向正确；返回 0 表示 D 项仍按普通方向计算。
 */
static int test_reverse_derivative_direction_tracks_reversed_error(void)
{
    PIDAI_Handle pid;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 0.0f, 0.0f, 1.0f);
    PIDAI_SetOutputLimits(&pid, -10000.0f, 10000.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetReverse(&pid, 1);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);

    PIDAI_Update(&pid, 10.0f, 10U, 10.0f);
    PIDAI_Update(&pid, 12.0f, 20U, 10.0f);

    if (!float_close(pid.d_error, 2.0f)) return 0;
    if (!float_close(pid.d_out, 200.0f)) return 0;
    if (!float_close(pid.out_raw, 200.0f)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试二进制协议使用的 CRC-16/CCITT-FALSE 是否符合标准向量。
 *
 * 主要流程：
 *   1. 使用 ASCII 文本 "123456789" 作为行业通用校验向量。
 *   2. 调用 PIDAI_BinaryCrc16。
 *   3. 校验结果等于 0x29B1，避免主机和板端 CRC 实现发生偏移。
 *
 * 返回值：
 *   返回 1 表示 CRC 实现符合约定；返回 0 表示 CRC 多项式、初值或移位方向错误。
 */
static int test_binary_crc16_standard_vector(void)
{
    const uint8_t data[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};

    return PIDAI_BinaryCrc16(data, sizeof(data)) == 0x29B1U;
}

/*
 * 函数作用：
 *   测试二进制 {PID} 遥测帧构建后的头部、载荷长度和 CRC 校验。
 *
 * 主要流程：
 *   1. 构造一次有效 PID 运行状态。
 *   2. 调用 PIDAI_BinaryBuildTelemetry 生成二进制帧。
 *   3. 校验 magic、版本、帧类型、载荷长度和 CRC。
 *   4. 调用 PIDAI_BinaryValidateFrame 验证整帧可被接收端接受。
 *
 * 返回值：
 *   返回 1 表示二进制遥测帧格式和 CRC 正确；否则返回 0。
 */
static int test_binary_build_pid_frame_with_crc(void)
{
    uint8_t frame[160];
    PIDAI_Handle pid;
    PIDAI_BinaryFrameInfo info;
    int written;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.0f, 0.2f, 0.1f);
    PIDAI_SetOutputLimits(&pid, 0.0f, 100.0f);
    PIDAI_SetTarget(&pid, 10.0f);
    PIDAI_SetMode(&pid, PIDAI_MODE_AUTO);
    PIDAI_Enable(&pid, 1);
    PIDAI_Update(&pid, 7.0f, 40U, 10.0f);

    written = PIDAI_BinaryBuildTelemetry(&pid, frame, sizeof(frame), 77U);

    if (written <= 0) return 0;
    if (frame[0] != PIDAI_BINARY_MAGIC_0 || frame[1] != PIDAI_BINARY_MAGIC_1) return 0;
    if (frame[2] != PIDAI_BINARY_VERSION) return 0;
    if (frame[3] != PIDAI_BINARY_TYPE_PID) return 0;
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) != 0) return 0;
    if (info.type != PIDAI_BINARY_TYPE_PID) return 0;
    if (info.seq != 77U) return 0;
    if (info.payload_length != 92U) return 0;
    if (written != (int)(PIDAI_BINARY_HEADER_SIZE + 92U + PIDAI_BINARY_CRC_SIZE)) return 0;

    /* 任意翻转载荷字节后 CRC 必须拒绝，证明校验覆盖了 header 和 payload。 */
    frame[PIDAI_BINARY_HEADER_SIZE] ^= 0x01U;
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) == 0) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试二进制 {CFG} 和 {CFGX} 配置帧的长度、类型和 CRC。
 *
 * 主要流程：
 *   1. 构造单环 PID 配置并打包为 CFG 二进制帧。
 *   2. 构造多环 route 并打包为 CFGX 二进制帧。
 *   3. 校验两类帧均能通过 CRC，并使用约定的 payload 长度。
 *
 * 返回值：
 *   返回 1 表示配置二进制帧正确；否则返回 0。
 */
static int test_binary_build_config_frames_with_crc(void)
{
    uint8_t frame[192];
    PIDAI_Handle pid;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_BinaryFrameInfo info;
    int written;

    PIDAI_Init(&pid);
    PIDAI_SetTunings(&pid, 1.2f, 0.03f, 0.08f);
    PIDAI_SetFeedForward(&pid, 0.01f);
    PIDAI_SetOutputLimits(&pid, 0.0f, 1000.0f);
    PIDAI_SetIntegralLimits(&pid, -500.0f, 500.0f);

    written = PIDAI_BinaryBuildConfig(&pid, 3, 9, frame, sizeof(frame), 90U);
    if (written <= 0) return 0;
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) != 0) return 0;
    if (info.type != PIDAI_BINARY_TYPE_CFG) return 0;
    if (info.payload_length != 56U) return 0;

    setup_two_loop_routes(&pid, &right, routes);
    PIDAI_SetTunings(&pid, 1.2f, 0.03f, 0.08f);
    written = PIDAI_BinaryBuildConfigX(&routes[0], frame, sizeof(frame), 91U);
    if (written <= 0) return 0;
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) != 0) return 0;
    if (info.type != PIDAI_BINARY_TYPE_CFGX) return 0;
    if (info.payload_length != (uint16_t)(2U + strlen("speed_l") + strlen("left_speed") + 52U)) return 0;

    return 1;
}

/*
 * 函数作用：
 *   测试二进制多环 {PIDX} 帧会携带 loop_id、loop_name 和 PID payload。
 *
 * 主要流程：
 *   1. 构造 speed_l loop 并执行一次 PID 更新。
 *   2. 调用 PIDAI_BinaryBuildTelemetryX 生成二进制多环遥测。
 *   3. 校验接收端能验证 CRC，且载荷长度包含两个长度前缀文本字段和 PID payload。
 *
 * 返回值：
 *   返回 1 表示多环二进制帧基本结构正确；否则返回 0。
 */
static int test_binary_build_pidx_frame_contains_loop_identity(void)
{
    uint8_t frame[192];
    PIDAI_Handle left;
    PIDAI_Handle right;
    PIDAI_LoopRoute routes[2];
    PIDAI_BinaryFrameInfo info;
    int written;

    setup_two_loop_routes(&left, &right, routes);
    PIDAI_SetTunings(&left, 1.0f, 0.0f, 0.0f);
    PIDAI_SetOutputLimits(&left, 0.0f, 100.0f);
    PIDAI_SetMode(&left, PIDAI_MODE_AUTO);
    PIDAI_Enable(&left, 1);
    PIDAI_Update(&left, 80.0f, 50U, 10.0f);

    written = PIDAI_BinaryBuildTelemetryX(&routes[0], frame, sizeof(frame), 88U);

    if (written <= 0) return 0;
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) != 0) return 0;
    if (info.type != PIDAI_BINARY_TYPE_PIDX) return 0;
    if (info.payload_length != (uint16_t)(2U + strlen("speed_l") + strlen("left_speed") + PIDAI_BINARY_PID_PAYLOAD_SIZE)) return 0;
    if (frame[PIDAI_BINARY_HEADER_SIZE] != (uint8_t)strlen("speed_l")) return 0;

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
    if (!test_build_pidx_frame()) {
        printf("FAIL: test_build_pidx_frame\n");
        return 1;
    }
    if (!test_build_cfgx_frame()) {
        printf("FAIL: test_build_cfgx_frame\n");
        return 1;
    }
    if (!test_protocol_set_pidx_routes_to_matching_loop()) {
        printf("FAIL: test_protocol_set_pidx_routes_to_matching_loop\n");
        return 1;
    }
    if (!test_protocol_set_pidx_rejects_unknown_loop()) {
        printf("FAIL: test_protocol_set_pidx_rejects_unknown_loop\n");
        return 1;
    }
    if (!test_protocol_x_rejects_invalid_loop_table()) {
        printf("FAIL: test_protocol_x_rejects_invalid_loop_table\n");
        return 1;
    }
    if (!test_protocol_x_unknown_loop_wins_over_bad_args()) {
        printf("FAIL: test_protocol_x_unknown_loop_wins_over_bad_args\n");
        return 1;
    }
    if (!test_protocol_set_pidx_rejects_malformed_commands()) {
        printf("FAIL: test_protocol_set_pidx_rejects_malformed_commands\n");
        return 1;
    }
    if (!test_derivative_kick_suppressed()) {
        printf("FAIL: test_derivative_kick_suppressed\n");
        return 1;
    }
    if (!test_invalid_runtime_inputs_emit_finite_fault_telemetry()) {
        printf("FAIL: test_invalid_runtime_inputs_emit_finite_fault_telemetry\n");
        return 1;
    }
    if (!test_reverse_derivative_direction_tracks_reversed_error()) {
        printf("FAIL: test_reverse_derivative_direction_tracks_reversed_error\n");
        return 1;
    }
    if (!test_binary_crc16_standard_vector()) {
        printf("FAIL: test_binary_crc16_standard_vector\n");
        return 1;
    }
    if (!test_binary_build_pid_frame_with_crc()) {
        printf("FAIL: test_binary_build_pid_frame_with_crc\n");
        return 1;
    }
    if (!test_binary_build_pidx_frame_contains_loop_identity()) {
        printf("FAIL: test_binary_build_pidx_frame_contains_loop_identity\n");
        return 1;
    }
    if (!test_binary_build_config_frames_with_crc()) {
        printf("FAIL: test_binary_build_config_frames_with_crc\n");
        return 1;
    }

    printf("PASS: all pid_ai tests\n");
    return 0;
}

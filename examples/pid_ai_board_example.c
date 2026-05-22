#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "pid_ai.h"
#include "pid_ai_protocol.h"

/* 示例协议版本号，上位机可以用它判断字段顺序是否匹配。 */
#define BOARD_PROTOCOL_VERSION 1

/* 示例 PID 配置编号，多套 PID 参数时可以用不同 profile_id 区分。 */
#define BOARD_PID_PROFILE_ID 0

/* 示例控制周期，真实项目中应由定时器周期决定。 */
#define BOARD_CONTROL_DT_MS 10.0f

/* 遥测发送分频，1 表示每次 PID 计算都上传，5 表示每 5 次上传一次。 */
#define BOARD_TELEMETRY_DIVIDER 1U

/* 串口发送缓冲区长度，必须能容纳最长的 {PID}/{CFG}/{ACK}/{ERR} 文本帧。 */
#define BOARD_TX_BUFFER_SIZE 256U

/* 全局 PID 控制器实例，实际项目中一个被控对象对应一个 PIDAI_Handle。 */
static PIDAI_Handle g_pid;

/* 当前控制周期计数，用于低频发送遥测，避免串口带宽不足。 */
static uint32_t g_control_tick_count = 0U;

/*
 * 函数作用：
 *   获取当前板端时间戳。
 *
 * 主要流程：
 *   示例中使用静态变量模拟时间递增；移植到 STM32/ESP32/Arduino 时应替换为系统毫秒计数。
 *
 * 参数说明：
 *   无参数。
 *
 * 返回值：
 *   返回当前毫秒时间戳。
 */
static uint32_t Board_GetMillis(void)
{
    static uint32_t fake_ms = 0U;

    fake_ms += (uint32_t)BOARD_CONTROL_DT_MS;
    return fake_ms;
}

/*
 * 函数作用：
 *   读取被控对象的实际反馈值。
 *
 * 主要流程：
 *   示例中返回一个模拟反馈值；真实项目应在这里读取编码器、ADC、IMU 或温度传感器。
 *
 * 参数说明：
 *   无参数。
 *
 * 返回值：
 *   返回实际反馈值，单位由项目自行定义，例如 rpm、degree、celsius。
 */
static float Board_ReadFeedback(void)
{
    static float fake_feedback = 0.0f;

    fake_feedback += 5.0f;
    if (fake_feedback > 1000.0f) {
        fake_feedback = 1000.0f;
    }

    return fake_feedback;
}

/*
 * 函数作用：
 *   将 PID 输出写入实际执行器。
 *
 * 主要流程：
 *   示例中只保存参数避免未使用警告；真实项目应在这里设置 PWM、DAC、电机电流或阀门开度。
 *
 * 参数说明：
 *   actuator PID 库计算出的最终执行器输出，已经完成限幅。
 *
 * 返回值：
 *   无返回值。
 */
static void Board_WriteActuator(float actuator)
{
    (void)actuator;
}

/*
 * 函数作用：
 *   通过串口发送一行协议文本。
 *
 * 主要流程：
 *   示例中使用 printf 模拟串口输出；真实项目应替换为 HAL_UART_Transmit、uart_write_bytes 或 Serial.print。
 *
 * 参数说明：
 *   text 需要发送的协议文本，通常已经包含 \r\n。
 *
 * 返回值：
 *   无返回值。
 */
static void Board_UartWriteString(const char *text)
{
    (void)printf("%s", text);
}

/*
 * 函数作用：
 *   发送当前 PID 配置帧。
 *
 * 主要流程：
 *   1. 调用协议库把当前 PID 配置打包成 {CFG}。
 *   2. 打包成功后通过串口发送。
 *
 * 参数说明：
 *   无参数，直接使用全局 PID 控制器。
 *
 * 返回值：
 *   无返回值。
 */
static void Board_SendConfig(void)
{
    char tx_buffer[BOARD_TX_BUFFER_SIZE];
    int written;

    written = PIDAI_ProtocolBuildConfig(&g_pid,
                                        BOARD_PID_PROFILE_ID,
                                        BOARD_PROTOCOL_VERSION,
                                        tx_buffer,
                                        sizeof(tx_buffer));
    if (written > 0) {
        Board_UartWriteString(tx_buffer);
    }
}

/*
 * 函数作用：
 *   发送当前 PID 遥测帧。
 *
 * 主要流程：
 *   1. 调用协议库把当前 PID 状态打包成 {PID}。
 *   2. 打包成功后通过串口发送。
 *
 * 参数说明：
 *   无参数，直接使用全局 PID 控制器。
 *
 * 返回值：
 *   无返回值。
 */
static void Board_SendTelemetry(void)
{
    char tx_buffer[BOARD_TX_BUFFER_SIZE];
    int written;

    written = PIDAI_ProtocolBuildTelemetry(&g_pid, tx_buffer, sizeof(tx_buffer));
    if (written > 0) {
        Board_UartWriteString(tx_buffer);
    }
}

/*
 * 函数作用：
 *   初始化板端 PID AI 控制器。
 *
 * 主要流程：
 *   1. 初始化 PID 控制器结构体。
 *   2. 设置 PID 参数、输出范围、积分范围和目标值。
 *   3. 设置自动模式并使能输出。
 *   4. 上电后主动发送一次配置帧，方便上位机同步当前参数。
 *
 * 参数说明：
 *   无参数。
 *
 * 返回值：
 *   无返回值。
 */
void Board_PidAiInit(void)
{
    (void)PIDAI_Init(&g_pid);
    (void)PIDAI_SetTunings(&g_pid, 1.2f, 0.03f, 0.08f);
    (void)PIDAI_SetFeedForward(&g_pid, 0.0f);
    (void)PIDAI_SetOutputLimits(&g_pid, 0.0f, 1000.0f);
    (void)PIDAI_SetIntegralLimits(&g_pid, -5000.0f, 5000.0f);
    (void)PIDAI_SetTarget(&g_pid, 1000.0f);
    (void)PIDAI_SetMode(&g_pid, PIDAI_MODE_AUTO);
    (void)PIDAI_Enable(&g_pid, 1);
    (void)PIDAI_SetSensorOk(&g_pid, 1);

    Board_SendConfig();
}

/*
 * 函数作用：
 *   执行一次固定周期 PID 控制任务。
 *
 * 主要流程：
 *   1. 读取传感器反馈值。
 *   2. 调用 PIDAI_Update 完成 PID 计算。
 *   3. 将 actuator 写入执行器。
 *   4. 按配置分频上传 {PID} 遥测帧。
 *
 * 参数说明：
 *   无参数，调用周期应由定时器或主循环调度保证。
 *
 * 返回值：
 *   无返回值。
 */
void Board_PidAiControlTick(void)
{
    float feedback;
    float actuator;
    uint32_t now_ms;

    feedback = Board_ReadFeedback();
    now_ms = Board_GetMillis();

    actuator = PIDAI_Update(&g_pid, feedback, now_ms, BOARD_CONTROL_DT_MS);
    Board_WriteActuator(actuator);

    g_control_tick_count++;
    if ((g_control_tick_count % BOARD_TELEMETRY_DIVIDER) == 0U) {
        Board_SendTelemetry();
    }
}

/*
 * 函数作用：
 *   处理串口收到的一整行上位机命令。
 *
 * 主要流程：
 *   1. 调用协议库解析并执行 {CMD} 命令。
 *   2. 成功时回复 {ACK}。
 *   3. 失败时回复 {ERR}。
 *   4. 对 GET_CFG 命令额外回复当前 {CFG}。
 *   5. 对 GET_STAT 命令保留位置，真实项目应回送 {STAT} 帧。
 *
 * 参数说明：
 *   line 串口收到的一行文本，例如 "{CMD}SET_PID,0.9,0.03,0.12"。
 *
 * 返回值：
 *   无返回值。
 */
void Board_PidAiOnUartLine(const char *line)
{
    char tx_buffer[BOARD_TX_BUFFER_SIZE];
    PIDAI_CommandResult result;
    int written;

    result = PIDAI_ProtocolHandleCommand(&g_pid, line);
    if (result.status == PIDAI_CMD_OK) {
        written = PIDAI_ProtocolBuildAck(&result, tx_buffer, sizeof(tx_buffer));
        if (written > 0) {
            Board_UartWriteString(tx_buffer);
        }

        /* GET_CFG 命令额外回送一帧 {CFG}，让上位机一次拿到全部配置。 */
        if (strcmp(result.command, "GET_CFG") == 0) {
            Board_SendConfig();
        }

        /*
         * GET_STAT 命令：当前示例没有完整的 {STAT} 帧实现，仅以 ACK 占位即可；
         * 真实项目应在此调用 Board_SendStat() 输出电压、电流、温度等板端健康字段。
         */
        if (strcmp(result.command, "GET_STAT") == 0) {
            /* 真实项目：Board_SendStat(); */
        }
    } else {
        written = PIDAI_ProtocolBuildError(&result, tx_buffer, sizeof(tx_buffer));
        if (written > 0) {
            Board_UartWriteString(tx_buffer);
        }
    }
}

/*
 * 函数作用：
 *   演示一个最小主循环如何接入 PID AI 库。
 *
 * 主要流程：
 *   1. 初始化 PID AI。
 *   2. 模拟上位机下发 SET_PID 命令。
 *   3. 连续执行若干次控制周期。
 *
 * 参数说明：
 *   无参数。
 *
 * 返回值：
 *   无返回值。
 */
void Board_PidAiMainLoopExample(void)
{
    int i;

    Board_PidAiInit();
    Board_PidAiOnUartLine("{CMD}SET_PID,0.900,0.030,0.120");

    for (i = 0; i < 20; i++) {
        Board_PidAiControlTick();
    }
}

#ifdef PID_AI_EXAMPLE_BUILD_MAIN
/*
 * 函数作用：
 *   PC 侧演示入口。
 *
 * 主要流程：
 *   调用 Board_PidAiMainLoopExample，在终端输出示例协议帧。
 *
 * 参数说明：
 *   无命令行参数。
 *
 * 返回值：
 *   返回 0 表示示例执行结束。
 */
int main(void)
{
    Board_PidAiMainLoopExample();
    return 0;
}
#endif

#include "pid_ai_protocol.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 协议行最大长度，覆盖当前所有命令；更长的自定义命令建议由应用层单独处理。 */
#define PIDAI_PROTOCOL_LINE_MAX 192U

/* 命令前缀长度，固定为 strlen("{CMD}")，用宏避免运行时重复计算。 */
#define PIDAI_CMD_PREFIX_LEN 5U

/*
 * 函数作用：
 *   将字符串安全复制到固定长度缓冲区。
 *
 * 主要流程：
 *   1. 检查目标缓冲区是否有效。
 *   2. 使用 snprintf 限制写入长度。
 *   3. 保证目标字符串始终以 '\0' 结尾。
 *
 * 参数说明：
 *   dst      目标缓冲区。
 *   dst_size 目标缓冲区长度。
 *   src      源字符串，允许为空指针。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_CopyText(char *dst, size_t dst_size, const char *src)
{
    if (dst == 0 || dst_size == 0U) {
        return;
    }

    if (src == 0) {
        dst[0] = '\0';
        return;
    }

    (void)snprintf(dst, dst_size, "%s", src);
    dst[dst_size - 1U] = '\0';
}

/*
 * 函数作用：
 *   创建一份命令处理结果。
 *
 * 主要流程：
 *   1. 写入状态枚举。
 *   2. 写入命令名。
 *   3. 写入结果细节。
 *
 * 参数说明：
 *   status  命令状态。
 *   command 命令名。
 *   detail  结果细节。
 *
 * 返回值：
 *   返回填充完成的 PIDAI_CommandResult。
 */
static PIDAI_CommandResult PIDAI_MakeResult(PIDAI_CommandStatus status,
                                            const char *command,
                                            const char *detail)
{
    PIDAI_CommandResult result;

    result.status = status;
    PIDAI_CopyText(result.command, sizeof(result.command), command);
    PIDAI_CopyText(result.detail, sizeof(result.detail), detail);

    return result;
}

/*
 * 函数作用：
 *   判断字符串是否以指定前缀开头。
 *
 * 主要流程：
 *   逐字符比较 prefix 和 text 的开头部分。
 *
 * 参数说明：
 *   text   待检查字符串。
 *   prefix 期望前缀。
 *
 * 返回值：
 *   返回 1 表示匹配，返回 0 表示不匹配。
 */
static int PIDAI_StartsWith(const char *text, const char *prefix)
{
    if (text == 0 || prefix == 0) {
        return 0;
    }

    while (*prefix != '\0') {
        if (*text != *prefix) {
            return 0;
        }
        text++;
        prefix++;
    }

    return 1;
}

/*
 * 函数作用：
 *   去掉字符串尾部的换行、回车、空格和制表符。
 *
 * 主要流程：
 *   从字符串末尾向前扫描，把尾部空白字符替换为 '\0'。
 *
 * 参数说明：
 *   text 需要原地裁剪的字符串。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_TrimRight(char *text)
{
    size_t length;

    if (text == 0) {
        return;
    }

    length = strlen(text);
    while (length > 0U) {
        char ch = text[length - 1U];
        if (ch != '\r' && ch != '\n' && ch != ' ' && ch != '\t') {
            break;
        }
        text[length - 1U] = '\0';
        length--;
    }
}

/*
 * 函数作用：
 *   解析一个浮点参数。
 *
 * 主要流程：
 *   1. 使用 strtof 解析文本。
 *   2. 检查是否完全消费了参数文本。
 *   3. 检查是否发生 ERANGE。
 *
 * 参数说明：
 *   text  参数文本。
 *   value 输出浮点值。
 *
 * 返回值：
 *   返回 1 表示解析成功，返回 0 表示解析失败。
 */
static int PIDAI_ParseFloat(const char *text, float *value)
{
    char *end_ptr;
    float parsed;

    if (text == 0 || value == 0 || text[0] == '\0') {
        return 0;
    }

    errno = 0;
    parsed = strtof(text, &end_ptr);
    if (errno == ERANGE || end_ptr == text || *end_ptr != '\0') {
        return 0;
    }

    *value = parsed;
    return 1;
}

/*
 * 函数作用：
 *   解析一个整型参数。
 *
 * 主要流程：
 *   1. 使用 strtol 解析十进制整数。
 *   2. 检查是否完全消费了参数文本。
 *
 * 参数说明：
 *   text  参数文本。
 *   value 输出整数值。
 *
 * 返回值：
 *   返回 1 表示解析成功，返回 0 表示解析失败。
 */
static int PIDAI_ParseInt(const char *text, int *value)
{
    char *end_ptr;
    long parsed;

    if (text == 0 || value == 0 || text[0] == '\0') {
        return 0;
    }

    errno = 0;
    parsed = strtol(text, &end_ptr, 10);
    if (errno == ERANGE || end_ptr == text || *end_ptr != '\0') {
        return 0;
    }

    *value = (int)parsed;
    return 1;
}

/*
 * 函数作用：
 *   取出下一个逗号分隔字段。
 *
 * 主要流程：
 *   1. 从 cursor 当前指向位置开始查找逗号。
 *   2. 找到逗号则把逗号替换为 '\0' 并推进 cursor。
 *   3. 找不到逗号则返回最后一个字段并把 cursor 置空。
 *
 * 参数说明：
 *   cursor 指向当前解析位置的指针。
 *
 * 返回值：
 *   返回当前字段字符串；没有字段时返回空指针。
 */
static char *PIDAI_NextToken(char **cursor)
{
    char *start;
    char *comma;

    if (cursor == 0 || *cursor == 0) {
        return 0;
    }

    start = *cursor;
    comma = strchr(start, ',');
    if (comma == 0) {
        *cursor = 0;
        return start;
    }

    *comma = '\0';
    *cursor = comma + 1;
    return start;
}

/*
 * 函数作用：
 *   将 snprintf 的返回值转换成本库统一返回值。
 *
 * 主要流程：
 *   1. 小于 0 表示格式化失败。
 *   2. 大于等于缓冲区长度表示缓冲区不足。
 *   3. 其他情况返回写入字符数。
 *
 * 参数说明：
 *   written       snprintf 返回值。
 *   buffer_length 缓冲区总长度。
 *
 * 返回值：
 *   成功返回写入字符数，失败返回负数。
 */
static int PIDAI_CheckFormatResult(int written, size_t buffer_length)
{
    if (written < 0) {
        return -1;
    }

    if ((size_t)written >= buffer_length) {
        return -2;
    }

    return written;
}

const char *PIDAI_ProtocolStatusText(PIDAI_CommandStatus status)
{
    switch (status) {
    case PIDAI_CMD_OK:
        return "OK";
    case PIDAI_CMD_BAD_PREFIX:
        return "BAD_PREFIX";
    case PIDAI_CMD_UNKNOWN:
        return "UNKNOWN";
    case PIDAI_CMD_ARG_MISSING:
        return "ARG_MISSING";
    case PIDAI_CMD_ARG_INVALID:
        return "ARG_INVALID";
    case PIDAI_CMD_PARAM_RANGE:
        return "PARAM_RANGE";
    case PIDAI_CMD_INTERNAL_ERROR:
        return "INTERNAL_ERROR";
    default:
        return "UNKNOWN_STATUS";
    }
}

PIDAI_CommandResult PIDAI_ProtocolHandleCommand(PIDAI_Handle *pid, const char *line)
{
    char copy[PIDAI_PROTOCOL_LINE_MAX];
    char *cursor;
    char *command;
    int ret;

    if (pid == 0 || line == 0) {
        return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, "UNKNOWN", "NULL_ARGUMENT");
    }

    if (!PIDAI_StartsWith(line, "{CMD}")) {
        return PIDAI_MakeResult(PIDAI_CMD_BAD_PREFIX, "UNKNOWN", "EXPECTED_CMD_PREFIX");
    }

    /*
     * 解析命令时先复制到局部缓冲区，避免修改串口接收缓冲区原文。
     * 如果命令超长，snprintf 会截断；后续参数解析会失败并返回明确错误。
     */
    (void)snprintf(copy, sizeof(copy), "%s", line + PIDAI_CMD_PREFIX_LEN);
    copy[sizeof(copy) - 1U] = '\0';
    PIDAI_TrimRight(copy);

    cursor = copy;
    command = PIDAI_NextToken(&cursor);
    if (command == 0 || command[0] == '\0') {
        return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, "UNKNOWN", "MISSING_COMMAND");
    }

    if (strcmp(command, "SET_PID") == 0) {
        float kp;
        float ki;
        float kd;
        char *arg_kp = PIDAI_NextToken(&cursor);
        char *arg_ki = PIDAI_NextToken(&cursor);
        char *arg_kd = PIDAI_NextToken(&cursor);

        if (arg_kp == 0 || arg_ki == 0 || arg_kd == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_KP_KI_KD");
        }

        if (!PIDAI_ParseFloat(arg_kp, &kp) ||
            !PIDAI_ParseFloat(arg_ki, &ki) ||
            !PIDAI_ParseFloat(arg_kd, &kd)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetTunings(pid, kp, ki, kd);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "KP_KI_KD_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_TUNINGS_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_KF") == 0) {
        float kf;
        char *arg_kf = PIDAI_NextToken(&cursor);

        if (arg_kf == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_KF");
        }
        if (!PIDAI_ParseFloat(arg_kf, &kf)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetFeedForward(pid, kf);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "KF_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_KF_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_TARGET") == 0) {
        float target;
        char *arg_target = PIDAI_NextToken(&cursor);

        if (arg_target == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_TARGET");
        }
        if (!PIDAI_ParseFloat(arg_target, &target)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetTarget(pid, target);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "TARGET_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_TARGET_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_OUT_LIMIT") == 0) {
        float out_min;
        float out_max;
        char *arg_min = PIDAI_NextToken(&cursor);
        char *arg_max = PIDAI_NextToken(&cursor);

        if (arg_min == 0 || arg_max == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_OUT_MIN_MAX");
        }
        if (!PIDAI_ParseFloat(arg_min, &out_min) || !PIDAI_ParseFloat(arg_max, &out_max)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetOutputLimits(pid, out_min, out_max);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "OUT_LIMIT_INVALID");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_OUT_LIMIT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_I_LIMIT") == 0) {
        float integral_min;
        float integral_max;
        char *arg_min = PIDAI_NextToken(&cursor);
        char *arg_max = PIDAI_NextToken(&cursor);

        if (arg_min == 0 || arg_max == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_I_MIN_MAX");
        }
        if (!PIDAI_ParseFloat(arg_min, &integral_min) || !PIDAI_ParseFloat(arg_max, &integral_max)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetIntegralLimits(pid, integral_min, integral_max);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "I_LIMIT_INVALID");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_I_LIMIT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_MODE") == 0) {
        int mode_value;
        char *arg_mode = PIDAI_NextToken(&cursor);

        if (arg_mode == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_MODE");
        }
        if (!PIDAI_ParseInt(arg_mode, &mode_value)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "INT_PARSE_FAIL");
        }

        ret = PIDAI_SetMode(pid, (PIDAI_Mode)mode_value);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "MODE_INVALID");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_MODE_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_MANUAL_OUT") == 0) {
        float manual_out;
        char *arg_output = PIDAI_NextToken(&cursor);

        if (arg_output == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_MANUAL_OUT");
        }
        if (!PIDAI_ParseFloat(arg_output, &manual_out)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }

        ret = PIDAI_SetManualOutput(pid, manual_out);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "MANUAL_OUT_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_MANUAL_OUT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "ENABLE") == 0) {
        int enable;
        char *arg_enable = PIDAI_NextToken(&cursor);

        if (arg_enable == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_ENABLE");
        }
        if (!PIDAI_ParseInt(arg_enable, &enable)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "INT_PARSE_FAIL");
        }

        ret = PIDAI_Enable(pid, enable);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "ENABLE_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_REVERSE") == 0) {
        int reverse;
        char *arg_reverse = PIDAI_NextToken(&cursor);

        if (arg_reverse == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_REVERSE");
        }
        if (!PIDAI_ParseInt(arg_reverse, &reverse)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "INT_PARSE_FAIL");
        }

        ret = PIDAI_SetReverse(pid, reverse);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_REVERSE_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_SENSOR_OK") == 0) {
        int sensor_ok;
        char *arg_sensor = PIDAI_NextToken(&cursor);

        if (arg_sensor == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_SENSOR_OK");
        }
        if (!PIDAI_ParseInt(arg_sensor, &sensor_ok)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "INT_PARSE_FAIL");
        }

        ret = PIDAI_SetSensorOk(pid, sensor_ok);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_SENSOR_OK_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "RESET_I") == 0) {
        ret = PIDAI_ResetIntegral(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "RESET_I_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "RESET") == 0) {
        ret = PIDAI_Reset(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "RESET_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "CLEAR_FAULT") == 0) {
        ret = PIDAI_ClearFault(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "CLEAR_FAULT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "GET_CFG") == 0 || strcmp(command, "GET_STAT") == 0) {
        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    return PIDAI_MakeResult(PIDAI_CMD_UNKNOWN, command, "COMMAND_NOT_SUPPORTED");
}

int PIDAI_ProtocolBuildTelemetry(const PIDAI_Handle *pid, char *buffer, size_t buffer_length)
{
    int written;

    if (pid == 0 || buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    written = snprintf(buffer,
                       buffer_length,
                       "{PID}%lu,%lu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%d,%d,%d,%d,%lu\r\n",
                       (unsigned long)pid->seq,
                       (unsigned long)pid->ms,
                       pid->dt_ms,
                       pid->target,
                       pid->feedback,
                       pid->error,
                       pid->d_error,
                       pid->integral,
                       pid->p_out,
                       pid->i_out,
                       pid->d_out,
                       pid->ff_out,
                       pid->out_raw,
                       pid->out_limited,
                       pid->actuator,
                       pid->out_min,
                       pid->out_max,
                       (int)pid->sat,
                       pid->anti_windup,
                       (int)pid->mode,
                       pid->enable,
                       pid->sensor_ok,
                       (unsigned long)pid->fault);

    return PIDAI_CheckFormatResult(written, buffer_length);
}

int PIDAI_ProtocolBuildConfig(const PIDAI_Handle *pid,
                              int profile_id,
                              int version,
                              char *buffer,
                              size_t buffer_length)
{
    int written;

    if (pid == 0 || buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    written = snprintf(buffer,
                       buffer_length,
                       "{CFG}%d,%.6f,%.6f,%.6f,%.6f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%d,%d,%lu\r\n",
                       profile_id,
                       pid->kp,
                       pid->ki,
                       pid->kd,
                       pid->kf,
                       pid->dt_ms,
                       pid->integral_min,
                       pid->integral_max,
                       pid->out_min,
                       pid->out_max,
                       pid->reverse,
                       (int)pid->mode,
                       version,
                       (unsigned long)pid->fault);

    return PIDAI_CheckFormatResult(written, buffer_length);
}

int PIDAI_ProtocolBuildAck(const PIDAI_CommandResult *result, char *buffer, size_t buffer_length)
{
    int written;

    if (result == 0 || buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    written = snprintf(buffer,
                       buffer_length,
                       "{ACK}%s,%s\r\n",
                       result->command,
                       result->detail);

    return PIDAI_CheckFormatResult(written, buffer_length);
}

int PIDAI_ProtocolBuildError(const PIDAI_CommandResult *result, char *buffer, size_t buffer_length)
{
    int written;

    if (result == 0 || buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    written = snprintf(buffer,
                       buffer_length,
                       "{ERR}%s,%s,%s\r\n",
                       result->command,
                       PIDAI_ProtocolStatusText(result->status),
                       result->detail);

    return PIDAI_CheckFormatResult(written, buffer_length);
}

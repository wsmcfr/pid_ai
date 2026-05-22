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
 * 宏作用：
 *   解析游标的特殊标记，表示上一轮分隔符后存在一个空的尾随字段。
 *
 * 使用说明：
 *   该值不会被解引用，只用于和 cursor 做身份比较。之所以不用空指针表示尾随空字段，
 *   是因为空指针已经表示“字段刚好解析完毕”；两者必须区分，才能把
 *   "{CMD}SET_PID,1,2,3," 识别为多余参数而不是合法命令。
 */
#define PIDAI_TRAILING_EMPTY_TOKEN ((char *)1)

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
 *   3. 如果逗号后已经是字符串结尾，则用 PIDAI_TRAILING_EMPTY_TOKEN 标记尾随空字段。
 *   4. 找不到逗号则返回最后一个字段并把 cursor 置空。
 *
 * 参数说明：
 *   cursor 指向当前解析位置的指针；函数会原地更新 *cursor。
 *   cursor 或 *cursor 为空表示已经没有字段可读。
 *   *cursor 等于 PIDAI_TRAILING_EMPTY_TOKEN 表示上一轮解析遇到尾随逗号。
 *
 * 返回值：
 *   返回当前字段字符串，字段内容位于调用方传入的可写缓冲区内。
 *   返回空指针表示没有可返回字段，可能是正常结束，也可能是尾随空字段已被标记。
 */
static char *PIDAI_NextToken(char **cursor)
{
    char *start;
    char *comma;

    if (cursor == 0 || *cursor == 0 || *cursor == PIDAI_TRAILING_EMPTY_TOKEN) {
        /* 空指针表示正常无字段；特殊标记表示尾随空字段已经被记录，二者都不能继续取 token。 */
        return 0;
    }

    start = *cursor;
    comma = strchr(start, ',');
    if (comma == 0) {
        *cursor = 0;
        return start;
    }

    *comma = '\0';
    if (comma[1] == '\0') {
        /* 末尾逗号代表一个空的多余参数，必须保留下来给 PIDAI_NoMoreTokens 判错。 */
        *cursor = PIDAI_TRAILING_EMPTY_TOKEN;
    } else {
        *cursor = comma + 1;
    }
    return start;
}

/*
 * 函数作用：
 *   判断命令参数是否已经精确消费完毕。
 *
 * 主要流程：
 *   1. 如果 cursor 为空，表示最后一个字段已经被正常消费，返回 1。
 *   2. 如果 cursor 非空，表示仍存在未消费字段或尾随空字段，返回 0。
 *
 * 参数说明：
 *   cursor 当前命令解析位置。
 *   cursor == 0 表示字段刚好结束。
 *   cursor == PIDAI_TRAILING_EMPTY_TOKEN 表示命令末尾存在多余空参数，例如尾随逗号。
 *   其他非空值表示还有至少一个未解析参数。
 *
 * 返回值：
 *   返回 1 表示没有多余参数。
 *   返回 0 表示存在多余参数，调用方应返回 PIDAI_CMD_ARG_INVALID / UNEXPECTED_ARG。
 */
static int PIDAI_NoMoreTokens(const char *cursor)
{
    /* 只有空指针代表参数精确结束；任何非空值都表示还有多余字段或尾随空字段。 */
    return cursor == 0;
}

/*
 * 函数作用：
 *   判断 loop_id 是否适合放入逗号分隔文本协议。
 *
 * 主要流程：
 *   1. 拒绝空指针和空字符串。
 *   2. 拒绝包含逗号、回车或换行的字符串，避免破坏字段边界。
 *
 * 参数说明：
 *   text 为待检查的 loop_id 或 loop_name。
 *
 * 返回值：
 *   返回 1 表示可安全输出到协议字段；返回 0 表示不合法。
 */
static int PIDAI_IsSafeProtocolText(const char *text)
{
    if (text == 0 || text[0] == '\0') {
        return 0;
    }

    while (*text != '\0') {
        if (*text == ',' || *text == '\r' || *text == '\n') {
            return 0;
        }
        text++;
    }

    return 1;
}

/*
 * 函数作用：
 *   取得多环 route 的展示名称。
 *
 * 主要流程：
 *   如果 loop_name 为空或包含协议分隔符，则退化使用 loop_id，保证输出帧字段可解析。
 *
 * 参数说明：
 *   route 为多环路由配置。
 *
 * 返回值：
 *   返回可输出到协议帧的名称字符串；route 无效时返回空字符串。
 */
static const char *PIDAI_LoopDisplayName(const PIDAI_LoopRoute *route)
{
    if (route == 0 || !PIDAI_IsSafeProtocolText(route->loop_id)) {
        return "";
    }

    if (PIDAI_IsSafeProtocolText(route->loop_name)) {
        return route->loop_name;
    }

    return route->loop_id;
}

/*
 * 函数作用：
 *   返回 loop 表中的第一个有效 PID 句柄，用于多环处理器兼容旧单环命令。
 *
 * 主要流程：
 *   线性扫描调用方提供的固定数组，返回第一项 pid 非空的 route。
 *
 * 参数说明：
 *   table 为应用层提供的多环表。
 *
 * 返回值：
 *   找到时返回 route 指针；没有有效 loop 时返回空指针。
 */
static PIDAI_LoopRoute *PIDAI_FirstValidLoop(PIDAI_LoopTable *table)
{
    size_t i;

    if (table == 0 || table->loops == 0) {
        return 0;
    }

    for (i = 0U; i < table->count; i++) {
        if (table->loops[i].pid != 0) {
            return &table->loops[i];
        }
    }

    return 0;
}

/*
 * 函数作用：
 *   校验多环命令入口是否拿到了可遍历的 loop 表。
 *
 * 主要流程：
 *   同时检查 table 指针、loops 固定数组指针和 count；任一无效都说明板端集成层
 *   没有正确提供路由表，必须返回内部错误，不能伪装成某个 loop_id 未找到。
 *
 * 参数说明：
 *   table 为应用层提供的多环路由表，允许为空指针用于错误检测。
 *
 * 返回值：
 *   返回 1 表示可用于多环命令路由；返回 0 表示表无效。
 */
static int PIDAI_HasValidLoopTable(PIDAI_LoopTable *table)
{
    return table != 0 && table->loops != 0 && table->count > 0U;
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

PIDAI_LoopRoute *PIDAI_ProtocolFindLoop(PIDAI_LoopTable *table, const char *loop_id)
{
    size_t i;

    if (table == 0 || table->loops == 0 || !PIDAI_IsSafeProtocolText(loop_id)) {
        return 0;
    }

    for (i = 0U; i < table->count; i++) {
        PIDAI_LoopRoute *route = &table->loops[i];
        if (route->pid == 0 || route->loop_id == 0) {
            continue;
        }
        if (strcmp(route->loop_id, loop_id) == 0) {
            return route;
        }
    }

    return 0;
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
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
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetSensorOk(pid, sensor_ok);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_SENSOR_OK_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "RESET_I") == 0) {
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_ResetIntegral(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "RESET_I_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "RESET") == 0) {
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_Reset(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "RESET_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "CLEAR_FAULT") == 0) {
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_ClearFault(pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "CLEAR_FAULT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "GET_CFG") == 0 || strcmp(command, "GET_STAT") == 0) {
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    return PIDAI_MakeResult(PIDAI_CMD_UNKNOWN, command, "COMMAND_NOT_SUPPORTED");
}

PIDAI_CommandResult PIDAI_ProtocolHandleCommandX(PIDAI_LoopTable *table, const char *line)
{
    char copy[PIDAI_PROTOCOL_LINE_MAX];
    char *cursor;
    char *command;
    PIDAI_LoopRoute *route;
    int ret;

    /*
     * 多环命令必须先区分“主机输入错误”和“板端集成错误”：
     * line 为空是调用方传参错误；loop 表无效则是板端没有提供可路由对象。
     */
    if (line == 0) {
        return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, "UNKNOWN", "NULL_ARGUMENT");
    }
    if (!PIDAI_HasValidLoopTable(table)) {
        return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, "UNKNOWN", "NO_VALID_LOOP_TABLE");
    }

    if (!PIDAI_StartsWith(line, "{CMD}")) {
        return PIDAI_MakeResult(PIDAI_CMD_BAD_PREFIX, "UNKNOWN", "EXPECTED_CMD_PREFIX");
    }

    /*
     * 多环命令同样复制到局部固定缓冲区解析，避免修改串口接收缓存；
     * 命令过长会被截断，后续字段数量或参数解析会给出显式错误。
     */
    (void)snprintf(copy, sizeof(copy), "%s", line + PIDAI_CMD_PREFIX_LEN);
    copy[sizeof(copy) - 1U] = '\0';
    PIDAI_TrimRight(copy);

    cursor = copy;
    command = PIDAI_NextToken(&cursor);
    if (command == 0 || command[0] == '\0') {
        return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, "UNKNOWN", "MISSING_COMMAND");
    }

    if (strcmp(command, "SET_PIDX") == 0) {
        float kp;
        float ki;
        float kd;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_kp = PIDAI_NextToken(&cursor);
        char *arg_ki = PIDAI_NextToken(&cursor);
        char *arg_kd = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_kp == 0 || arg_ki == 0 || arg_kd == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_KP_KI_KD");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseFloat(arg_kp, &kp) ||
            !PIDAI_ParseFloat(arg_ki, &ki) ||
            !PIDAI_ParseFloat(arg_kd, &kd)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetTunings(route->pid, kp, ki, kd);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "KP_KI_KD_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_TUNINGS_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_KFX") == 0) {
        float kf;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_kf = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_kf == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_KF");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseFloat(arg_kf, &kf)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetFeedForward(route->pid, kf);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "KF_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_KF_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_TARGETX") == 0) {
        float target;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_target = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_target == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_TARGET");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseFloat(arg_target, &target)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetTarget(route->pid, target);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "TARGET_OUT_OF_RANGE");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_TARGET_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_OUT_LIMITX") == 0) {
        float out_min;
        float out_max;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_min = PIDAI_NextToken(&cursor);
        char *arg_max = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_min == 0 || arg_max == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_OUT_MIN_MAX");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseFloat(arg_min, &out_min) || !PIDAI_ParseFloat(arg_max, &out_max)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetOutputLimits(route->pid, out_min, out_max);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "OUT_LIMIT_INVALID");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_OUT_LIMIT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "SET_I_LIMITX") == 0) {
        float integral_min;
        float integral_max;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_min = PIDAI_NextToken(&cursor);
        char *arg_max = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_min == 0 || arg_max == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_I_MIN_MAX");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseFloat(arg_min, &integral_min) || !PIDAI_ParseFloat(arg_max, &integral_max)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "FLOAT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_SetIntegralLimits(route->pid, integral_min, integral_max);
        if (ret == -2) {
            return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "I_LIMIT_INVALID");
        }
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_I_LIMIT_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "RESET_IX") == 0) {
        char *arg_loop = PIDAI_NextToken(&cursor);

        if (arg_loop == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_ResetIntegral(route->pid);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "RESET_I_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "ENABLEX") == 0) {
        int enable;
        char *arg_loop = PIDAI_NextToken(&cursor);
        char *arg_enable = PIDAI_NextToken(&cursor);

        if (arg_loop == 0 || arg_enable == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP_ENABLE");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_ParseInt(arg_enable, &enable)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "INT_PARSE_FAIL");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        ret = PIDAI_Enable(route->pid, enable);
        if (ret != 0) {
            return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "ENABLE_FAIL");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "GET_CFGX") == 0) {
        char *arg_loop = PIDAI_NextToken(&cursor);

        if (arg_loop == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_MISSING, command, "NEED_LOOP");
        }
        route = PIDAI_ProtocolFindLoop(table, arg_loop);
        if (route == 0) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "LOOP_NOT_FOUND");
        }
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    if (strcmp(command, "GET_ALL_CFG") == 0) {
        if (!PIDAI_NoMoreTokens(cursor)) {
            return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
        }

        return PIDAI_MakeResult(PIDAI_CMD_OK, command, "OK");
    }

    /*
     * 兼容旧单环命令：多环应用可以继续把 GET_CFG、SET_PID 等旧命令交给第一个有效环路。
     * 串级小车建议上位机优先使用 *X 命令，避免写错环路。
     */
    route = PIDAI_FirstValidLoop(table);
    if (route == 0) {
        return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "NO_VALID_LOOP");
    }

    return PIDAI_ProtocolHandleCommand(route->pid, line);
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

int PIDAI_ProtocolBuildTelemetryX(const PIDAI_LoopRoute *route, char *buffer, size_t buffer_length)
{
    const PIDAI_Handle *pid;
    const char *loop_name;
    int written;

    if (route == 0 || route->pid == 0 || !PIDAI_IsSafeProtocolText(route->loop_id) ||
        buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    pid = route->pid;
    loop_name = PIDAI_LoopDisplayName(route);

    written = snprintf(buffer,
                       buffer_length,
                       "{PIDX}%s,%s,%lu,%lu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%d,%d,%d,%d,%lu\r\n",
                       route->loop_id,
                       loop_name,
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

int PIDAI_ProtocolBuildConfigX(const PIDAI_LoopRoute *route, char *buffer, size_t buffer_length)
{
    const PIDAI_Handle *pid;
    const char *loop_name;
    int written;

    if (route == 0 || route->pid == 0 || !PIDAI_IsSafeProtocolText(route->loop_id) ||
        buffer == 0 || buffer_length == 0U) {
        return -1;
    }

    pid = route->pid;
    loop_name = PIDAI_LoopDisplayName(route);

    written = snprintf(buffer,
                       buffer_length,
                       "{CFGX}%s,%s,%.6f,%.6f,%.6f,%.6f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%d,%d,%lu\r\n",
                       route->loop_id,
                       loop_name,
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
                       route->version,
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

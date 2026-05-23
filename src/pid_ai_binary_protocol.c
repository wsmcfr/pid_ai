#include "pid_ai_binary_protocol.h"

#include <string.h>

/*
 * 函数作用：
 *   判断文本字段是否适合写入二进制协议的 loop 标识区域。
 *
 * 主要流程：
 *   拒绝空指针、空字符串、超过 255 字节和包含控制分隔字符的文本。
 *
 * 参数说明：
 *   text 为 loop_id 或 loop_name。
 *
 * 返回值：
 *   返回 1 表示可写入；返回 0 表示非法。
 */
static int PIDAI_BinaryIsSafeText(const char *text)
{
    size_t length;

    if (text == 0 || text[0] == '\0') {
        return 0;
    }

    length = strlen(text);
    if (length > 255U) {
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
 *   为多环 route 选择安全展示名称。
 *
 * 主要流程：
 *   loop_name 可用时使用 loop_name，否则退化到 loop_id，保证上位机始终能解析文本字段。
 *
 * 参数说明：
 *   route 为多环路由。
 *
 * 返回值：
 *   返回可写入二进制 payload 的字符串。
 */
static const char *PIDAI_BinaryLoopDisplayName(const PIDAI_LoopRoute *route)
{
    if (route == 0 || !PIDAI_BinaryIsSafeText(route->loop_id)) {
        return "";
    }
    if (PIDAI_BinaryIsSafeText(route->loop_name)) {
        return route->loop_name;
    }
    return route->loop_id;
}

/*
 * 函数作用：
 *   以 little-endian 写入 16 位无符号整数。
 *
 * 主要流程：
 *   低字节先写，高字节后写，避免不同 MCU 端序差异影响协议。
 *
 * 参数说明：
 *   out 为输出指针。
 *   value 为待写入数值。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_WriteU16LE(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8U) & 0xFFU);
}

/*
 * 函数作用：
 *   以 little-endian 写入 32 位无符号整数。
 *
 * 主要流程：
 *   逐字节移位写入，避免直接类型转换造成未对齐访问。
 *
 * 参数说明：
 *   out 为输出指针。
 *   value 为待写入数值。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_WriteU32LE(uint8_t *out, uint32_t value)
{
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8U) & 0xFFU);
    out[2] = (uint8_t)((value >> 16U) & 0xFFU);
    out[3] = (uint8_t)((value >> 24U) & 0xFFU);
}

/*
 * 函数作用：
 *   以 little-endian 写入 32 位有符号整数。
 *
 * 主要流程：
 *   先转为 uint32_t 再按位写入，保持二进制补码表示。
 *
 * 参数说明：
 *   out 为输出指针。
 *   value 为待写入有符号值。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_WriteI32LE(uint8_t *out, int32_t value)
{
    PIDAI_WriteU32LE(out, (uint32_t)value);
}

/*
 * 函数作用：
 *   以 IEEE-754 float32 的内存位型写入 little-endian 字节。
 *
 * 主要流程：
 *   使用 memcpy 取得 float 的 32 位位型，再显式小端写入，避免别名规则和未对齐访问问题。
 *
 * 参数说明：
 *   out 为输出指针。
 *   value 为待写入浮点数。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_WriteF32LE(uint8_t *out, float value)
{
    uint32_t bits;

    (void)memcpy(&bits, &value, sizeof(bits));
    PIDAI_WriteU32LE(out, bits);
}

/*
 * 函数作用：
 *   从 little-endian 字节读取 16 位无符号整数。
 *
 * 主要流程：
 *   显式组合低高字节，避免依赖 CPU 端序。
 *
 * 参数说明：
 *   data 指向至少 2 字节输入。
 *
 * 返回值：
 *   返回解析后的 16 位整数。
 */
static uint16_t PIDAI_ReadU16LE(const uint8_t *data)
{
    return (uint16_t)((uint16_t)data[0] | ((uint16_t)data[1] << 8U));
}

/*
 * 函数作用：
 *   从 little-endian 字节读取 32 位无符号整数。
 *
 * 主要流程：
 *   显式组合 4 个字节，避免依赖 CPU 端序。
 *
 * 参数说明：
 *   data 指向至少 4 字节输入。
 *
 * 返回值：
 *   返回解析后的 32 位整数。
 */
static uint32_t PIDAI_ReadU32LE(const uint8_t *data)
{
    return ((uint32_t)data[0]) |
           ((uint32_t)data[1] << 8U) |
           ((uint32_t)data[2] << 16U) |
           ((uint32_t)data[3] << 24U);
}

/*
 * 函数作用：
 *   写入二进制协议公共 header。
 *
 * 主要流程：
 *   写 magic、版本、类型、flags、传输层 seq 和 payload 长度。
 *
 * 参数说明：
 *   buffer 为输出帧起始位置。
 *   type 为二进制帧类型。
 *   payload_length 为 payload 字节数。
 *   frame_seq 为传输层序号。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_BinaryWriteHeader(uint8_t *buffer, uint8_t type, uint16_t payload_length, uint32_t frame_seq)
{
    buffer[0] = PIDAI_BINARY_MAGIC_0;
    buffer[1] = PIDAI_BINARY_MAGIC_1;
    buffer[2] = PIDAI_BINARY_VERSION;
    buffer[3] = type;
    buffer[4] = 0U;
    PIDAI_WriteU32LE(&buffer[5], frame_seq);
    PIDAI_WriteU16LE(&buffer[9], payload_length);
}

/*
 * 函数作用：
 *   写入二进制协议帧尾 CRC。
 *
 * 主要流程：
 *   CRC 覆盖 header 中 version 起始的字段和 payload，不覆盖 magic 和 CRC 自身。
 *
 * 参数说明：
 *   buffer 为完整帧缓冲区。
 *   payload_length 为 payload 字节数。
 *
 * 返回值：
 *   返回完整帧字节数。
 */
static int PIDAI_BinaryFinishFrame(uint8_t *buffer, uint16_t payload_length)
{
    size_t crc_offset = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length;
    uint16_t crc = PIDAI_BinaryCrc16(&buffer[2], (PIDAI_BINARY_HEADER_SIZE - 2U) + (size_t)payload_length);

    PIDAI_WriteU16LE(&buffer[crc_offset], crc);
    return (int)(crc_offset + PIDAI_BINARY_CRC_SIZE);
}

/*
 * 函数作用：
 *   按文本 {PID} 字段顺序写入二进制 PID payload。
 *
 * 主要流程：
 *   uint32 字段写 4 字节，float 字段写 IEEE-754 4 字节，枚举/布尔字段写 int32。
 *
 * 参数说明：
 *   pid 为待打包 PID 状态。
 *   payload 为输出 payload 起始位置，调用方保证空间足够。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_BinaryWritePidPayload(const PIDAI_Handle *pid, uint8_t *payload)
{
    uint8_t *cursor = payload;

    PIDAI_WriteU32LE(cursor, pid->seq); cursor += 4;
    PIDAI_WriteU32LE(cursor, pid->ms); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->dt_ms); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->target); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->feedback); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->error); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->d_error); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->integral); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->p_out); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->i_out); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->d_out); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->ff_out); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_raw); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_limited); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->actuator); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_min); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_max); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->sat); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->anti_windup); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->mode); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->enable); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->sensor_ok); cursor += 4;
    PIDAI_WriteU32LE(cursor, pid->fault);
}

/*
 * 函数作用：
 *   按文本 {CFG} 字段顺序写入二进制配置 payload。
 *
 * 主要流程：
 *   写 profile_id、kp/ki/kd/kf、周期、积分/输出限幅、reverse、mode、version 和 fault。
 *
 * 参数说明：
 *   pid 为 PID 状态。
 *   profile_id 为配置槽编号。
 *   version 为配置版本。
 *   payload 为输出 payload 起始位置。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_BinaryWriteCfgPayload(const PIDAI_Handle *pid, int profile_id, int version, uint8_t *payload)
{
    uint8_t *cursor = payload;

    PIDAI_WriteI32LE(cursor, (int32_t)profile_id); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->kp); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->ki); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->kd); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->kf); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->dt_ms); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->integral_min); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->integral_max); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_min); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_max); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->reverse); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->mode); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)version); cursor += 4;
    PIDAI_WriteU32LE(cursor, pid->fault);
}

/*
 * 函数作用：
 *   按文本 {CFGX} 字段顺序写入去掉 profile_id 的二进制配置 payload。
 *
 * 主要流程：
 *   写 kp/ki/kd/kf、周期、积分/输出限幅、reverse、mode、route version 和 fault。
 *
 * 参数说明：
 *   route 为多环路由。
 *   payload 为输出 payload 起始位置。
 *
 * 返回值：
 *   无返回值。
 */
static void PIDAI_BinaryWriteCfgXPayload(const PIDAI_LoopRoute *route, uint8_t *payload)
{
    const PIDAI_Handle *pid = route->pid;
    uint8_t *cursor = payload;

    PIDAI_WriteF32LE(cursor, pid->kp); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->ki); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->kd); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->kf); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->dt_ms); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->integral_min); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->integral_max); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_min); cursor += 4;
    PIDAI_WriteF32LE(cursor, pid->out_max); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->reverse); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)pid->mode); cursor += 4;
    PIDAI_WriteI32LE(cursor, (int32_t)route->version); cursor += 4;
    PIDAI_WriteU32LE(cursor, pid->fault);
}

uint16_t PIDAI_BinaryCrc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xFFFFU;
    size_t i;

    if (data == 0 && length > 0U) {
        return crc;
    }

    for (i = 0U; i < length; i++) {
        uint8_t bit;
        crc ^= (uint16_t)((uint16_t)data[i] << 8U);
        for (bit = 0U; bit < 8U; bit++) {
            if ((crc & 0x8000U) != 0U) {
                crc = (uint16_t)((crc << 1U) ^ 0x1021U);
            } else {
                crc = (uint16_t)(crc << 1U);
            }
        }
    }

    return crc;
}

int PIDAI_BinaryBuildTelemetry(const PIDAI_Handle *pid,
                               uint8_t *buffer,
                               size_t buffer_length,
                               uint32_t frame_seq)
{
    const uint16_t payload_length = PIDAI_BINARY_PID_PAYLOAD_SIZE;
    size_t frame_length = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length + PIDAI_BINARY_CRC_SIZE;

    if (pid == 0 || buffer == 0) {
        return -1;
    }
    if (buffer_length < frame_length) {
        return -2;
    }

    PIDAI_BinaryWriteHeader(buffer, PIDAI_BINARY_TYPE_PID, payload_length, frame_seq);
    PIDAI_BinaryWritePidPayload(pid, &buffer[PIDAI_BINARY_HEADER_SIZE]);
    return PIDAI_BinaryFinishFrame(buffer, payload_length);
}

int PIDAI_BinaryBuildTelemetryX(const PIDAI_LoopRoute *route,
                                uint8_t *buffer,
                                size_t buffer_length,
                                uint32_t frame_seq)
{
    const PIDAI_Handle *pid;
    const char *loop_name;
    size_t loop_id_length;
    size_t loop_name_length;
    uint16_t payload_length;
    size_t frame_length;
    uint8_t *payload;

    if (route == 0 || route->pid == 0 || !PIDAI_BinaryIsSafeText(route->loop_id) || buffer == 0) {
        return -1;
    }

    pid = route->pid;
    loop_name = PIDAI_BinaryLoopDisplayName(route);
    loop_id_length = strlen(route->loop_id);
    loop_name_length = strlen(loop_name);
    if (loop_id_length > 255U || loop_name_length > 255U) {
        return -1;
    }

    payload_length = (uint16_t)(2U + loop_id_length + loop_name_length + PIDAI_BINARY_PID_PAYLOAD_SIZE);
    frame_length = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length + PIDAI_BINARY_CRC_SIZE;
    if (buffer_length < frame_length) {
        return -2;
    }

    PIDAI_BinaryWriteHeader(buffer, PIDAI_BINARY_TYPE_PIDX, payload_length, frame_seq);
    payload = &buffer[PIDAI_BINARY_HEADER_SIZE];
    payload[0] = (uint8_t)loop_id_length;
    (void)memcpy(&payload[1], route->loop_id, loop_id_length);
    payload[1U + loop_id_length] = (uint8_t)loop_name_length;
    (void)memcpy(&payload[2U + loop_id_length], loop_name, loop_name_length);
    PIDAI_BinaryWritePidPayload(pid, &payload[2U + loop_id_length + loop_name_length]);

    return PIDAI_BinaryFinishFrame(buffer, payload_length);
}

int PIDAI_BinaryBuildConfig(const PIDAI_Handle *pid,
                            int profile_id,
                            int version,
                            uint8_t *buffer,
                            size_t buffer_length,
                            uint32_t frame_seq)
{
    const uint16_t payload_length = PIDAI_BINARY_CFG_PAYLOAD_SIZE;
    size_t frame_length = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length + PIDAI_BINARY_CRC_SIZE;

    if (pid == 0 || buffer == 0) {
        return -1;
    }
    if (buffer_length < frame_length) {
        return -2;
    }

    PIDAI_BinaryWriteHeader(buffer, PIDAI_BINARY_TYPE_CFG, payload_length, frame_seq);
    PIDAI_BinaryWriteCfgPayload(pid, profile_id, version, &buffer[PIDAI_BINARY_HEADER_SIZE]);
    return PIDAI_BinaryFinishFrame(buffer, payload_length);
}

int PIDAI_BinaryBuildConfigX(const PIDAI_LoopRoute *route,
                             uint8_t *buffer,
                             size_t buffer_length,
                             uint32_t frame_seq)
{
    const char *loop_name;
    size_t loop_id_length;
    size_t loop_name_length;
    uint16_t payload_length;
    size_t frame_length;
    uint8_t *payload;

    if (route == 0 || route->pid == 0 || !PIDAI_BinaryIsSafeText(route->loop_id) || buffer == 0) {
        return -1;
    }

    loop_name = PIDAI_BinaryLoopDisplayName(route);
    loop_id_length = strlen(route->loop_id);
    loop_name_length = strlen(loop_name);
    if (loop_id_length > 255U || loop_name_length > 255U) {
        return -1;
    }

    payload_length = (uint16_t)(2U + loop_id_length + loop_name_length + 52U);
    frame_length = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length + PIDAI_BINARY_CRC_SIZE;
    if (buffer_length < frame_length) {
        return -2;
    }

    PIDAI_BinaryWriteHeader(buffer, PIDAI_BINARY_TYPE_CFGX, payload_length, frame_seq);
    payload = &buffer[PIDAI_BINARY_HEADER_SIZE];
    payload[0] = (uint8_t)loop_id_length;
    (void)memcpy(&payload[1], route->loop_id, loop_id_length);
    payload[1U + loop_id_length] = (uint8_t)loop_name_length;
    (void)memcpy(&payload[2U + loop_id_length], loop_name, loop_name_length);
    PIDAI_BinaryWriteCfgXPayload(route, &payload[2U + loop_id_length + loop_name_length]);

    return PIDAI_BinaryFinishFrame(buffer, payload_length);
}

int PIDAI_BinaryValidateFrame(const uint8_t *frame,
                              size_t frame_length,
                              PIDAI_BinaryFrameInfo *info)
{
    uint16_t payload_length;
    size_t expected_length;
    uint16_t expected_crc;
    uint16_t actual_crc;

    if (frame == 0 || frame_length < (PIDAI_BINARY_HEADER_SIZE + PIDAI_BINARY_CRC_SIZE)) {
        return -1;
    }
    if (frame[0] != PIDAI_BINARY_MAGIC_0 || frame[1] != PIDAI_BINARY_MAGIC_1) {
        return -2;
    }
    if (frame[2] != PIDAI_BINARY_VERSION) {
        return -3;
    }

    payload_length = PIDAI_ReadU16LE(&frame[9]);
    expected_length = PIDAI_BINARY_HEADER_SIZE + (size_t)payload_length + PIDAI_BINARY_CRC_SIZE;
    if (frame_length != expected_length) {
        return -4;
    }

    expected_crc = PIDAI_BinaryCrc16(&frame[2], (PIDAI_BINARY_HEADER_SIZE - 2U) + (size_t)payload_length);
    actual_crc = PIDAI_ReadU16LE(&frame[PIDAI_BINARY_HEADER_SIZE + payload_length]);
    if (expected_crc != actual_crc) {
        return -5;
    }

    if (info != 0) {
        info->type = frame[3];
        info->flags = frame[4];
        info->seq = PIDAI_ReadU32LE(&frame[5]);
        info->payload_length = payload_length;
        info->payload = &frame[PIDAI_BINARY_HEADER_SIZE];
    }

    return 0;
}

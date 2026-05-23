#ifndef PID_AI_BINARY_PROTOCOL_H
#define PID_AI_BINARY_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#include "pid_ai.h"
#include "pid_ai_protocol_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 宏作用：
 *   定义 PID AI 二进制协议的固定帧头、版本号、帧类型和载荷长度。
 *
 * 使用说明：
 *   二进制协议与现有文本协议并存，用于高频遥测场景；字段顺序与文本 {PID}/{PIDX}
 *   保持一致，但数值以 little-endian 二进制形式传输，末尾使用 CRC-16/CCITT-FALSE。
 */
#define PIDAI_BINARY_MAGIC_0 0xA5U
#define PIDAI_BINARY_MAGIC_1 0x5AU
#define PIDAI_BINARY_VERSION 1U

#define PIDAI_BINARY_TYPE_PID 1U
#define PIDAI_BINARY_TYPE_PIDX 2U
#define PIDAI_BINARY_TYPE_CFG 3U
#define PIDAI_BINARY_TYPE_CFGX 4U

#define PIDAI_BINARY_HEADER_SIZE 11U
#define PIDAI_BINARY_CRC_SIZE 2U
#define PIDAI_BINARY_PID_PAYLOAD_SIZE 92U
#define PIDAI_BINARY_CFG_PAYLOAD_SIZE 56U

/*
 * 结构体作用：
 *   保存接收端验证二进制帧后得到的头部信息。
 *
 * 字段说明：
 *   type 表示二进制帧类型，例如 PIDAI_BINARY_TYPE_PID。
 *   flags 为保留标志位，当前构建函数固定写 0。
 *   seq 为传输层帧序号，用于检测二进制帧丢包，与 PID 遥测 seq 分开。
 *   payload 指向输入帧内部的载荷区域，调用者不拥有该内存。
 *   payload_length 为载荷字节数。
 */
typedef struct
{
    uint8_t type;                 /* 二进制帧类型。 */
    uint8_t flags;                /* 保留标志位，当前为 0。 */
    uint32_t seq;                 /* 传输层序号，用于接收端判断丢包。 */
    const uint8_t *payload;       /* 指向帧内 payload 起始位置，不可越界访问。 */
    uint16_t payload_length;      /* payload 字节数。 */
} PIDAI_BinaryFrameInfo;

/*
 * 函数作用：
 *   计算 CRC-16/CCITT-FALSE 校验值。
 *
 * 主要流程：
 *   初值 0xFFFF，多项式 0x1021，无输入/输出反转，无最终异或。
 *
 * 参数说明：
 *   data 指向待校验字节数组；length 为字节数。length 为 0 时返回初值。
 *
 * 返回值：
 *   返回 16 位 CRC 值。
 */
uint16_t PIDAI_BinaryCrc16(const uint8_t *data, size_t length);

/*
 * 函数作用：
 *   将单环 PID 遥测打包为二进制 PID 帧。
 *
 * 主要流程：
 *   写入固定 header、按文本 {PID} 字段顺序写入二进制 payload，再追加 CRC。
 *
 * 参数说明：
 *   pid 指向 PID 状态。
 *   buffer 为输出缓冲区。
 *   buffer_length 为输出缓冲区长度。
 *   frame_seq 为传输层帧序号。
 *
 * 返回值：
 *   成功返回写入字节数；-1 表示参数错误，-2 表示缓冲区不足。
 */
int PIDAI_BinaryBuildTelemetry(const PIDAI_Handle *pid,
                               uint8_t *buffer,
                               size_t buffer_length,
                               uint32_t frame_seq);

/*
 * 函数作用：
 *   将多环 PID 遥测打包为二进制 PIDX 帧。
 *
 * 主要流程：
 *   先写 loop_id 和 loop_name 的长度前缀文本字段，再追加与 {PID} 同序的二进制 payload。
 *
 * 参数说明：
 *   route 指向多环路由，route->pid 和 route->loop_id 必须有效。
 *   buffer 为输出缓冲区。
 *   buffer_length 为输出缓冲区长度。
 *   frame_seq 为传输层帧序号。
 *
 * 返回值：
 *   成功返回写入字节数；-1 表示参数错误，-2 表示缓冲区不足。
 */
int PIDAI_BinaryBuildTelemetryX(const PIDAI_LoopRoute *route,
                                uint8_t *buffer,
                                size_t buffer_length,
                                uint32_t frame_seq);

/*
 * 函数作用：
 *   将单环 PID 配置打包为二进制 CFG 帧。
 *
 * 主要流程：
 *   按文本 {CFG} 字段顺序写 profile_id、PID 参数、限幅、模式、版本和 fault。
 *
 * 参数说明：
 *   pid 指向 PID 状态。
 *   profile_id 为配置槽编号。
 *   version 为协议或配置版本。
 *   buffer 为输出缓冲区。
 *   buffer_length 为输出缓冲区长度。
 *   frame_seq 为传输层帧序号。
 *
 * 返回值：
 *   成功返回写入字节数；-1 表示参数错误，-2 表示缓冲区不足。
 */
int PIDAI_BinaryBuildConfig(const PIDAI_Handle *pid,
                            int profile_id,
                            int version,
                            uint8_t *buffer,
                            size_t buffer_length,
                            uint32_t frame_seq);

/*
 * 函数作用：
 *   将多环 PID 配置打包为二进制 CFGX 帧。
 *
 * 主要流程：
 *   写 loop_id、loop_name，再按 {CFGX} 的 kp 到 fault 字段顺序写配置 payload。
 *
 * 参数说明：
 *   route 指向多环路由。
 *   buffer 为输出缓冲区。
 *   buffer_length 为输出缓冲区长度。
 *   frame_seq 为传输层帧序号。
 *
 * 返回值：
 *   成功返回写入字节数；-1 表示参数错误，-2 表示缓冲区不足。
 */
int PIDAI_BinaryBuildConfigX(const PIDAI_LoopRoute *route,
                             uint8_t *buffer,
                             size_t buffer_length,
                             uint32_t frame_seq);

/*
 * 函数作用：
 *   验证一整帧二进制协议数据，并解析出头部和 payload 指针。
 *
 * 主要流程：
 *   校验 magic、版本、长度和 CRC；全部通过后填充 info。
 *
 * 参数说明：
 *   frame 指向完整二进制帧。
 *   frame_length 为完整帧长度。
 *   info 为输出头部信息，允许为空表示只做校验。
 *
 * 返回值：
 *   返回 0 表示验证成功；负数表示帧头、长度或 CRC 错误。
 */
int PIDAI_BinaryValidateFrame(const uint8_t *frame,
                              size_t frame_length,
                              PIDAI_BinaryFrameInfo *info);

#ifdef __cplusplus
}
#endif

#endif

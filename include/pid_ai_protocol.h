#ifndef PID_AI_PROTOCOL_H
#define PID_AI_PROTOCOL_H

#include <stddef.h>

#include "pid_ai.h"
#include "pid_ai_protocol_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 宏作用：
 *   定义协议解析结果中文本字段的最大长度。
 *
 * 使用说明：
 *   command 保存命令名，detail 保存错误细节或成功说明；固定长度可避免动态内存分配。
 */
#define PIDAI_PROTOCOL_COMMAND_MAX 24U
#define PIDAI_PROTOCOL_DETAIL_MAX 64U

/*
 * 枚举作用：
 *   描述上位机命令解析和执行结果。
 *
 * 取值说明：
 *   PIDAI_CMD_OK              表示命令解析并执行成功。
 *   PIDAI_CMD_BAD_PREFIX      表示命令没有 {CMD} 前缀。
 *   PIDAI_CMD_UNKNOWN         表示命令名不支持。
 *   PIDAI_CMD_ARG_MISSING     表示命令参数数量不足。
 *   PIDAI_CMD_ARG_INVALID     表示命令参数格式错误。
 *   PIDAI_CMD_PARAM_RANGE     表示命令参数超出 PID 库允许范围。
 *   PIDAI_CMD_INTERNAL_ERROR  表示 PID 库执行接口返回异常。
 */
typedef enum
{
    PIDAI_CMD_OK = 0,
    PIDAI_CMD_BAD_PREFIX = 1,
    PIDAI_CMD_UNKNOWN = 2,
    PIDAI_CMD_ARG_MISSING = 3,
    PIDAI_CMD_ARG_INVALID = 4,
    PIDAI_CMD_PARAM_RANGE = 5,
    PIDAI_CMD_INTERNAL_ERROR = 6
} PIDAI_CommandStatus;

/*
 * 结构体作用：
 *   保存一次串口命令处理的结果，便于板端生成 {ACK} 或 {ERR} 回复。
 *
 * 字段说明：
 *   status  命令执行状态。
 *   command 已识别到的命令名，无法识别时保存 UNKNOWN。
 *   detail  成功或失败的简短说明。
 */
typedef struct
{
    PIDAI_CommandStatus status;                       /* 命令处理状态，用于决定回复 ACK 还是 ERR。 */
    char command[PIDAI_PROTOCOL_COMMAND_MAX];         /* 命令名称，例如 SET_PID、SET_TARGET。 */
    char detail[PIDAI_PROTOCOL_DETAIL_MAX];           /* 结果细节，例如 OK、ARG_INVALID、KP_OUT_OF_RANGE。 */
} PIDAI_CommandResult;

/*
 * 函数作用：
 *   解析并执行一条上位机 {CMD} 命令。
 *
 * 主要流程：
 *   1. 检查命令前缀是否为 {CMD}。
 *   2. 识别命令名称。
 *   3. 按命令类型解析参数。
 *   4. 调用 PIDAI_SetTunings 等核心库接口更新 PID 状态。
 *
 * 参数说明：
 *   pid  指向 PID 控制器结构体。
 *   line 串口收到的一整行文本，不要求包含结尾 \r\n。
 *
 * 返回值：
 *   返回 PIDAI_CommandResult，调用者可用它生成 {ACK} 或 {ERR} 回复。
 */
PIDAI_CommandResult PIDAI_ProtocolHandleCommand(PIDAI_Handle *pid, const char *line);

/*
 * 函数作用：
 *   在多环 loop 表中按 loop_id 精确查找 PID 环路。
 *
 * 主要流程：
 *   1. 校验 table、loops 和 loop_id 是否有效。
 *   2. 按数组顺序逐项比较 loop_id。
 *   3. 只返回 pid 非空且 ID 精确相同的 route。
 *
 * 参数说明：
 *   table   指向应用层静态分配的 loop 表。
 *   loop_id 协议命令中的环路标识，不能为空。
 *
 * 返回值：
 *   找到时返回 route 指针；未找到或参数无效时返回空指针。
 */
PIDAI_LoopRoute *PIDAI_ProtocolFindLoop(PIDAI_LoopTable *table, const char *loop_id);

/*
 * 函数作用：
 *   解析并执行一条多环 {CMD} 命令。
 *
 * 主要流程：
 *   1. 检查 {CMD} 前缀并解析命令名。
 *   2. 对 SET_PIDX、SET_KFX、SET_TARGETX 等分环命令先精确匹配 loop_id。
 *   3. 校验参数数量和类型后调用对应 PIDAI_Set* 接口。
 *   4. 对非 X 旧命令兼容路由到 table 中第一个 PID 环路。
 *
 * 参数说明：
 *   table 指向应用层提供的多环表。
 *   line  串口收到的一整行文本，不要求包含结尾 \r\n。
 *
 * 返回值：
 *   返回 PIDAI_CommandResult，调用者可用它生成 {ACK} 或 {ERR} 回复。
 */
PIDAI_CommandResult PIDAI_ProtocolHandleCommandX(PIDAI_LoopTable *table, const char *line);

/*
 * 函数作用：
 *   将当前 PID 运行状态打包为 {PID} 遥测帧。
 *
 * 主要流程：
 *   按协议固定字段顺序把 PIDAI_Handle 中的遥测字段写入 buffer。
 *
 * 参数说明：
 *   pid           指向 PID 控制器结构体。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildTelemetry(const PIDAI_Handle *pid, char *buffer, size_t buffer_length);

/*
 * 函数作用：
 *   将指定 PID 环路的运行状态打包为 {PIDX} 多环遥测帧。
 *
 * 主要流程：
 *   先输出 loop_id 和 loop_name，再按 {PID} 的固定字段顺序输出该环路 PID 状态。
 *
 * 参数说明：
 *   route         指向多环 route，route->pid 和 route->loop_id 必须有效。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildTelemetryX(const PIDAI_LoopRoute *route, char *buffer, size_t buffer_length);

/*
 * 函数作用：
 *   将当前 PID 配置打包为 {CFG} 配置帧。
 *
 * 主要流程：
 *   按协议固定字段顺序输出 kp、ki、kd、kf、周期和限幅配置。
 *
 * 参数说明：
 *   pid           指向 PID 控制器结构体。
 *   profile_id    PID 配置编号，由应用层决定。
 *   version       协议或配置版本号，由应用层决定。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildConfig(const PIDAI_Handle *pid,
                              int profile_id,
                              int version,
                              char *buffer,
                              size_t buffer_length);

/*
 * 函数作用：
 *   将指定 PID 环路的配置打包为 {CFGX} 多环配置帧。
 *
 * 主要流程：
 *   先输出 loop_id 和 loop_name，再按扩展协议固定顺序输出 kp、ki、kd、kf、限幅、模式和版本。
 *
 * 参数说明：
 *   route         指向多环 route，route->pid 和 route->loop_id 必须有效。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildConfigX(const PIDAI_LoopRoute *route, char *buffer, size_t buffer_length);

/*
 * 函数作用：
 *   将命令处理成功结果打包为 {ACK} 回复帧。
 *
 * 主要流程：
 *   把 result.command 和 result.detail 写入 ACK 文本帧。
 *
 * 参数说明：
 *   result        指向命令处理结果。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildAck(const PIDAI_CommandResult *result, char *buffer, size_t buffer_length);

/*
 * 函数作用：
 *   将命令处理失败结果打包为 {ERR} 回复帧。
 *
 * 主要流程：
 *   把 result.command、错误状态和 result.detail 写入 ERR 文本帧。
 *
 * 参数说明：
 *   result        指向命令处理结果。
 *   buffer        输出字符串缓冲区。
 *   buffer_length 缓冲区字节长度。
 *
 * 返回值：
 *   返回写入字符数；返回负数表示参数错误或缓冲区不足。
 */
int PIDAI_ProtocolBuildError(const PIDAI_CommandResult *result, char *buffer, size_t buffer_length);

/*
 * 函数作用：
 *   将命令状态枚举转换为稳定的协议文本。
 *
 * 主要流程：
 *   按枚举值返回固定字符串，用于 {ERR} 帧和日志显示。
 *
 * 参数说明：
 *   status 命令处理状态。
 *
 * 返回值：
 *   返回状态文本常量字符串。
 */
const char *PIDAI_ProtocolStatusText(PIDAI_CommandStatus status);

#ifdef __cplusplus
}
#endif

#endif

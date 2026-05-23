#ifndef PID_AI_PROTOCOL_TYPES_H
#define PID_AI_PROTOCOL_TYPES_H

#include <stddef.h>

#include "pid_ai.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 宏作用：
 *   定义多环协议中 loop 标识和显示名称的建议最大长度。
 *
 * 使用说明：
 *   文本协议和二进制协议都会使用 loop_id/loop_name 标识多环 PID；公共类型头只定义
 *   两套协议共享的静态路由信息，不引入命令解析、文本帧或二进制帧 API。
 */
#define PIDAI_PROTOCOL_LOOP_ID_MAX 24U
#define PIDAI_PROTOCOL_LOOP_NAME_MAX 32U

/*
 * 结构体作用：
 *   描述一个可被文本协议或二进制协议引用的 PID 环路。
 *
 * 字段说明：
 *   loop_id    协议层精确匹配的环路标识，例如 speed_l、yaw_rate；必须唯一且不能包含逗号。
 *   loop_name  给上位机展示的人类可读名称；为空时打包函数会退化使用 loop_id。
 *   pid        指向该环路的 PIDAI_Handle，命令处理或遥测打包会读取/修改对应 PID 状态。
 *   profile_id 保留给应用层区分配置槽；当前 {CFGX}/二进制 CFGX 不输出该字段。
 *   version    该环路配置版本，会输出到 {CFGX}/二进制 CFGX 的 version 字段。
 */
typedef struct
{
    const char *loop_id;       /* 多环命令和 PIDX/CFGX 帧中的稳定环路 ID。 */
    const char *loop_name;     /* 展示名称，便于 dashboard 区分左右轮、角速度、循迹外环。 */
    PIDAI_Handle *pid;         /* 该环路实际 PID 状态；为空时该 route 不可用。 */
    int profile_id;            /* 应用层配置槽编号，保留给板端 GET_ALL_CFG 组织发送顺序。 */
    int version;               /* 多环配置版本号，输出到 CFGX 类配置帧。 */
} PIDAI_LoopRoute;

/*
 * 结构体作用：
 *   保存一组由应用层静态分配的 PID 环路路由。
 *
 * 字段说明：
 *   loops 指向固定数组，不由协议库申请或释放。
 *   count 数组元素数量；文本命令处理会按顺序线性扫描并精确匹配 loop_id。
 */
typedef struct
{
    PIDAI_LoopRoute *loops;    /* 调用方持有的固定 loop 数组，库内只读取和路由。 */
    size_t count;              /* loop 数组长度，单位为元素个数。 */
} PIDAI_LoopTable;

#ifdef __cplusplus
}
#endif

#endif

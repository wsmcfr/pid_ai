#include <stdint.h>

/*
 * 宏作用：
 *   在独立编译测试中主动屏蔽文本协议头，验证二进制协议公共头不依赖文本协议 API。
 *
 * 使用说明：
 *   如果 pid_ai_binary_protocol.h 仍通过 pid_ai_protocol.h 间接取得公共类型，本测试会在编译期失败。
 */
#define PID_AI_PROTOCOL_H

#include "pid_ai.h"
#include "pid_ai_binary_protocol.h"

/*
 * 函数作用：
 *   验证二进制协议库可以作为独立可选模块编译和使用，不需要包含或链接文本协议实现。
 *
 * 主要流程：
 *   1. 初始化一个 PID 控制器。
 *   2. 调用二进制协议构建 PID 遥测帧。
 *   3. 校验生成帧可以通过二进制协议校验。
 *
 * 参数说明：
 *   无命令行参数。
 *
 * 返回值：
 *   返回 0 表示二进制协议独立编译和基础调用成功；返回 1 表示失败。
 */
int main(void)
{
    PIDAI_Handle pid;
    PIDAI_BinaryFrameInfo info;
    uint8_t frame[160];
    int written;

    (void)PIDAI_Init(&pid);
    written = PIDAI_BinaryBuildTelemetry(&pid, frame, sizeof(frame), 1U);
    if (written <= 0) {
        return 1;
    }
    if (PIDAI_BinaryValidateFrame(frame, (size_t)written, &info) != 0) {
        return 1;
    }
    if (info.type != PIDAI_BINARY_TYPE_PID) {
        return 1;
    }

    return 0;
}

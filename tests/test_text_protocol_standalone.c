#include <string.h>

#include "pid_ai.h"
#include "pid_ai_protocol.h"

/*
 * 函数作用：
 *   验证文本协议库可以作为独立可选模块编译和使用，不需要链接二进制协议实现。
 *
 * 主要流程：
 *   1. 初始化一个 PID 控制器。
 *   2. 通过文本协议处理 SET_PID 命令。
 *   3. 构建一帧 {PID} 文本遥测，确认文本协议入口可用。
 *
 * 参数说明：
 *   无命令行参数。
 *
 * 返回值：
 *   返回 0 表示文本协议独立编译和基础调用成功；返回 1 表示失败。
 */
int main(void)
{
    PIDAI_Handle pid;
    PIDAI_CommandResult result;
    char frame[256];
    int written;

    (void)PIDAI_Init(&pid);
    result = PIDAI_ProtocolHandleCommand(&pid, "{CMD}SET_PID,1.000,0.100,0.010");
    if (result.status != PIDAI_CMD_OK) {
        return 1;
    }

    written = PIDAI_ProtocolBuildTelemetry(&pid, frame, sizeof(frame));
    if (written <= 0) {
        return 1;
    }
    if (strncmp(frame, "{PID}", 5U) != 0) {
        return 1;
    }

    return 0;
}

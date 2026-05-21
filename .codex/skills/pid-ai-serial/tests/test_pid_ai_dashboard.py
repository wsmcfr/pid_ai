import pathlib
import sys
import unittest


# 测试脚本目录，用于把 skill 自带的 scripts 目录加入导入路径。
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pid_ai_dashboard import DashboardState, extract_command_name


def make_pid_line(seq: int, feedback: float) -> str:
    """
    函数作用：
        构造一条合法的 {PID} 测试帧。

    主要流程：
        按协议固定 23 字段输出测试数据，只让 seq、ms、feedback 和 error 随参数变化。

    参数说明：
        seq 为遥测序号。
        feedback 为反馈值，用于确认最新样本被正确保留。

    返回值：
        返回完整 {PID} 文本帧。
    """
    target = 100.0
    error = target - feedback
    return (
        f"{{PID}}{seq},{seq * 10},10.000,{target:.3f},{feedback:.3f},{error:.3f},"
        "0.000,1.000,2.000,3.000,4.000,0.000,9.000,8.000,8.000,"
        "0.000,100.000,0,0,1,1,1,0"
    )


class DashboardStateTest(unittest.TestCase):
    """测试 dashboard 纯状态逻辑，不依赖真实串口或浏览器。"""

    def test_pid_samples_are_bounded_and_queryable_by_id(self) -> None:
        """
        函数作用：
            验证 {PID} 样本缓冲区有上限，并能按自增 id 增量读取。

        主要流程：
            1. 创建最大长度为 2 的状态对象。
            2. 连续注入 3 条合法 {PID} 帧。
            3. 校验只保留最后 2 条，并且 latest_pid 指向最后一条。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=2)

        state.ingest_line(make_pid_line(1, 90.0))
        state.ingest_line(make_pid_line(2, 91.0))
        state.ingest_line(make_pid_line(3, 92.0))

        samples = state.get_samples_after(0)
        snapshot = state.snapshot()

        self.assertEqual([sample["data"]["seq"] for sample in samples], [2, 3])
        self.assertEqual(snapshot["latest_pid"]["data"]["seq"], 3)
        self.assertEqual(snapshot["latest_sample_id"], samples[-1]["id"])

    def test_ack_frame_updates_matching_pending_command(self) -> None:
        """
        函数作用：
            验证 ACK 帧会把同名待确认命令更新为已确认。

        主要流程：
            1. 记录一条用户显式发送的 GET_CFG 命令。
            2. 注入板端 {ACK}GET_CFG,OK 回复。
            3. 校验命令历史保留原命令、状态和 ACK 明细。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.record_command("{CMD}GET_CFG")
        state.ingest_line("{ACK}GET_CFG,OK")

        history = state.snapshot()["command_history"]

        self.assertEqual(history[-1]["command"], "{CMD}GET_CFG")
        self.assertEqual(history[-1]["command_name"], "GET_CFG")
        self.assertEqual(history[-1]["status"], "ack")
        self.assertEqual(history[-1]["response"]["detail"], "OK")

    def test_parse_errors_are_counted_without_poisoning_latest_pid(self) -> None:
        """
        函数作用：
            验证坏帧会记录诊断信息，但不会伪造成最新有效 PID 样本。

        主要流程：
            先注入一条坏 {PID} 帧，再读取状态快照。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.ingest_line("{PID}1,2,3")
        snapshot = state.snapshot()

        self.assertEqual(snapshot["parse_errors"], 1)
        self.assertEqual(snapshot["last_bad_line"], "{PID}1,2,3")
        self.assertIsNone(snapshot["latest_pid"])

    def test_extract_command_name_rejects_non_cmd_text(self) -> None:
        """
        函数作用：
            验证命令名提取只接受 {CMD} 前缀，避免普通文本进入命令历史。

        主要流程：
            1. 校验 SET_PID 命令名提取。
            2. 校验非 {CMD} 文本会抛出 ValueError。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        self.assertEqual(extract_command_name("{CMD}SET_PID,1.0,0.1,0.01"), "SET_PID")
        with self.assertRaises(ValueError):
            extract_command_name("SET_PID,1.0,0.1,0.01")


if __name__ == "__main__":
    unittest.main()

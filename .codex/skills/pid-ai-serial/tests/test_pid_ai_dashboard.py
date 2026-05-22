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


def make_pidx_line(loop_id: str, seq: int, feedback: float, *, fault: int = 0) -> str:
    """
    函数作用：
        构造一条合法的 {PIDX} 多环遥测测试帧。

    主要流程：
        按扩展协议固定字段输出 loop_id、loop_name 和原 {PID} 遥测字段。

    参数说明：
        loop_id 为环路标识，例如 speed_l。
        seq 为遥测序号。
        feedback 为反馈值，用于确认指定 loop 的最新状态被更新。
        fault 为故障位图，默认 0。

    返回值：
        返回完整 {PIDX} 文本帧。
    """
    target = 100.0
    error = target - feedback
    return (
        f"{{PIDX}}{loop_id},{loop_id},{seq},{seq * 10},10.000,{target:.3f},"
        f"{feedback:.3f},{error:.3f},0.000,1.000,2.000,3.000,4.000,0.000,"
        "9.000,8.000,8.000,0.000,100.000,0,0,1,1,1,"
        f"{fault}"
    )


def make_cfgx_line(loop_id: str, kp: float) -> str:
    """
    函数作用：
        构造一条合法的 {CFGX} 多环配置测试帧。

    主要流程：
        按扩展协议固定字段输出 loop_id、loop_name 和 PID 参数。

    参数说明：
        loop_id 为环路标识。
        kp 为比例系数，用于确认配置进入对应 loop。

    返回值：
        返回完整 {CFGX} 文本帧。
    """
    return (
        f"{{CFGX}}{loop_id},{loop_id},{kp:.6f},0.030000,0.080000,0.000000,"
        "10.000,-5000.000,5000.000,0.000,1000.000,0,1,3,0"
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

    def test_pidx_and_cfgx_update_loop_state(self) -> None:
        """
        函数作用：
            验证 dashboard 会把 {PIDX}/{CFGX} 写入按 loop_id 索引的多环状态。

        主要流程：
            1. 注入 speed_l 的 PIDX 和 CFGX。
            2. 读取状态快照和样本缓冲。
            3. 校验 loops.speed_l 同时包含最新遥测和配置，样本也保留 loop_id。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.ingest_line(make_pidx_line("speed_l", 1, 88.0))
        state.ingest_line(make_cfgx_line("speed_l", 1.2))

        snapshot = state.snapshot()
        samples = state.get_samples_after(0)

        self.assertIn("speed_l", snapshot["loops"])
        self.assertEqual(snapshot["loops"]["speed_l"]["latest_pid"]["data"]["feedback"], 88.0)
        self.assertAlmostEqual(snapshot["loops"]["speed_l"]["latest_cfg"]["data"]["kp"], 1.2)
        self.assertEqual(samples[-1]["data"]["loop_id"], "speed_l")

    def test_loop_command_history_records_loop_id_reason_and_ack(self) -> None:
        """
        函数作用：
            验证 dashboard 命令历史会记录分环命令的 loop_id 和调参原因，并等待 ACK 更新状态。

        主要流程：
            1. 记录一条 SET_PIDX 自动调参命令。
            2. 注入板端 ACK。
            3. 校验 loop_id、reason 和最终 ACK 状态。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.record_command("{CMD}SET_PIDX,speed_l,1.200,0.030,0.080", reason="auto-tune step")
        state.ingest_line("{ACK}SET_PIDX,OK")

        history = state.snapshot()["command_history"]

        self.assertEqual(history[-1]["command_name"], "SET_PIDX")
        self.assertEqual(history[-1]["loop_id"], "speed_l")
        self.assertEqual(history[-1]["reason"], "auto-tune step")
        self.assertEqual(history[-1]["status"], "ack")

    def test_bad_pidx_frame_does_not_poison_loop_state(self) -> None:
        """
        函数作用：
            验证坏 {PIDX} 帧只增加解析错误，不会覆盖对应 loop 的最新有效遥测。

        主要流程：
            先注入一条合法 speed_l PIDX，再注入字段不足的坏 PIDX，最后检查最新状态。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.ingest_line(make_pidx_line("speed_l", 1, 88.0))
        state.ingest_line("{PIDX}speed_l,left_speed,1,2,3")
        snapshot = state.snapshot()

        self.assertEqual(snapshot["parse_errors"], 1)
        self.assertEqual(snapshot["loops"]["speed_l"]["latest_pid"]["data"]["feedback"], 88.0)

    def test_status_contains_autotune_scores_and_rollback_history(self) -> None:
        """
        函数作用：
            验证 dashboard status 预留自动调参状态、评分和回滚历史字段。

        主要流程：
            创建状态对象并读取快照，校验自动调参相关字段存在且默认关闭。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)
        snapshot = state.snapshot()

        self.assertIn("autotune", snapshot)
        self.assertFalse(snapshot["autotune"]["enabled"])
        self.assertEqual(snapshot["scores"], {})
        self.assertEqual(snapshot["rollback_history"], [])


if __name__ == "__main__":
    unittest.main()

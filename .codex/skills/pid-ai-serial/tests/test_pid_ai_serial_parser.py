import pathlib
import sys
import unittest


# 测试脚本目录，用于导入 skill 自带的串口协议解析模块。
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import pid_ai_serial


VALID_PID_LINE = (
    "{PID}1024,123456,10.000,1000.000,850.000,150.000,5.000,3200.000,"
    "120.000,40.000,10.000,0.000,170.000,170.000,170.000,0.000,"
    "1000.000,0,0,1,1,1,0"
)

VALID_CFG_LINE = (
    "{CFG}0,1.200000,0.030000,0.080000,0.000000,10.000,-5000.000,"
    "5000.000,0.000,1000.000,0,1,1,0"
)

VALID_PIDX_LINE = (
    "{PIDX}speed_l,left_speed,1024,123456,10.000,1000.000,850.000,150.000,"
    "5.000,3200.000,120.000,40.000,10.000,0.000,170.000,170.000,"
    "170.000,0.000,1000.000,0,0,1,1,1,0"
)

VALID_CFGX_LINE = (
    "{CFGX}speed_l,left_speed,1.200000,0.030000,0.080000,0.000000,10.000,"
    "-5000.000,5000.000,0.000,1000.000,0,1,3,0"
)

VALID_SENS_LINE = (
    "{SENS}123456,1,0,1,0,1,0,1,0,0.125,0,1.500,0.250,"
    "1234,1235,0.800,0.810,0.805,7.400"
)


class PidAiSerialParserTest(unittest.TestCase):
    """测试共享串口协议解析器的 typed frame 校验。"""

    def test_pid_sample_from_protocol_doc_is_valid(self) -> None:
        """
        函数作用：
            验证协议文档中的 {PID} 样例能解析成有效 typed frame。

        主要流程：
            调用 parse_frame，并校验关键字段类型与取值。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame(VALID_PID_LINE)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "pid")
        self.assertEqual(parsed["data"]["seq"], 1024)
        self.assertEqual(parsed["data"]["mode"], 1)

    def test_pid_rejects_invalid_enum_ranges(self) -> None:
        """
        函数作用：
            验证 {PID} 枚举字段越界时不会被当作有效遥测。

        主要流程：
            将 mode 从合法值 1 改为非法值 9，并校验 valid=False 和错误文本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        bad_line = VALID_PID_LINE.replace(",0,0,1,1,1,0", ",0,0,9,1,1,0")

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("mode out of range", parsed["error"])

    def test_pid_rejects_nan_numeric_fields(self) -> None:
        """
        函数作用：
            验证 {PID} 数值字段出现 NaN 时不会进入有效遥测状态。

        主要流程：
            1. 将 target 字段替换为 nan，模拟串口坏数据或固件格式错误。
            2. 调用 parse_frame。
            3. 校验 valid=False，且 error 明确指出 target 必须是有限数。

        返回值：
            unittest 断言失败时抛出异常。
            通过时无返回值，表示 parser 会拒绝 NaN 而不是把坏帧写入有效样本。
        """
        bad_line = VALID_PID_LINE.replace("1000.000", "nan", 1)

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("target must be finite", parsed["error"])

    def test_cfg_rejects_infinite_numeric_fields(self) -> None:
        """
        函数作用：
            验证 {CFG} 数值字段出现无穷大时不会被当作有效配置。

        主要流程：
            1. 将 kp 字段替换为 inf，覆盖 Python float 默认可解析无穷大的边界。
            2. 调用 parse_frame。
            3. 校验 valid=False，且 error 明确指出 kp 必须是有限数。

        返回值：
            unittest 断言失败时抛出异常。
            通过时无返回值，表示 parser 会拒绝无穷大而不是把坏配置写入状态。
        """
        bad_line = VALID_CFG_LINE.replace("1.200000", "inf", 1)

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("kp must be finite", parsed["error"])

    def test_cfg_sample_from_protocol_doc_is_valid(self) -> None:
        """
        函数作用：
            验证协议文档中的 {CFG} 样例能解析成有效 typed frame。

        主要流程：
            调用 parse_frame，并校验 PID 参数和模式字段。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame(VALID_CFG_LINE)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "cfg")
        self.assertAlmostEqual(parsed["data"]["kp"], 1.2)
        self.assertEqual(parsed["data"]["mode"], 1)

    def test_cfg_rejects_negative_version(self) -> None:
        """
        函数作用：
            验证 {CFG} 版本号为负数时会被拒绝。

        主要流程：
            把 version 字段从 1 改为 -1，并校验解析失败原因。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        bad_line = VALID_CFG_LINE.replace(",0,1,1,0", ",0,1,-1,0")

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("version must be non-negative", parsed["error"])

    def test_pidx_sample_is_valid_and_preserves_loop_identity(self) -> None:
        """
        函数作用：
            验证 {PIDX} 多环遥测帧能解析为带 loop_id/loop_name 的 typed frame。

        主要流程：
            调用 parse_frame 解析多环样例，并校验 loop 标识和原 {PID} 关键字段。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame(VALID_PIDX_LINE)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "pidx")
        self.assertEqual(parsed["data"]["loop_id"], "speed_l")
        self.assertEqual(parsed["data"]["loop_name"], "left_speed")
        self.assertEqual(parsed["data"]["seq"], 1024)
        self.assertAlmostEqual(parsed["data"]["error"], 150.0)

    def test_cfgx_sample_is_valid_and_preserves_loop_identity(self) -> None:
        """
        函数作用：
            验证 {CFGX} 多环配置帧能解析为带 loop_id/loop_name 的 typed frame。

        主要流程：
            调用 parse_frame 解析多环配置样例，并校验 PID 参数、模式和版本字段。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame(VALID_CFGX_LINE)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "cfgx")
        self.assertEqual(parsed["data"]["loop_id"], "speed_l")
        self.assertEqual(parsed["data"]["loop_name"], "left_speed")
        self.assertAlmostEqual(parsed["data"]["kp"], 1.2)
        self.assertEqual(parsed["data"]["version"], 3)

    def test_sens_sample_is_valid(self) -> None:
        """
        函数作用：
            验证 {SENS} 小车传感器帧能解析 8 路循迹、姿态、编码器、速度和电池字段。

        主要流程：
            调用 parse_frame 解析传感器样例，并校验 line、yaw_rate、v_avg 和 battery。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame(VALID_SENS_LINE)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "sens")
        self.assertEqual(parsed["data"]["line0"], 1)
        self.assertEqual(parsed["data"]["line7"], 0)
        self.assertAlmostEqual(parsed["data"]["yaw_rate"], 0.25)
        self.assertAlmostEqual(parsed["data"]["v_avg"], 0.805)
        self.assertAlmostEqual(parsed["data"]["battery"], 7.4)

    def test_pidx_rejects_nan_numeric_fields(self) -> None:
        """
        函数作用：
            验证 {PIDX} 中的非有限数会被拒绝，避免多环坏遥测进入调参状态机。

        主要流程：
            把 target 字段替换为 nan 后解析，并校验错误文本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        bad_line = VALID_PIDX_LINE.replace("1000.000", "nan", 1)

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("target must be finite", parsed["error"])

    def test_build_loop_commands_use_three_decimal_parameters(self) -> None:
        """
        函数作用：
            验证 typed command builder 会生成分环命令，并按自动调参约定保留三位小数。

        主要流程：
            调用 SET_PIDX 和 SET_TARGETX 构建函数，校验完整 {CMD} 文本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        self.assertEqual(
            pid_ai_serial.build_set_pidx_command("speed_l", 1.23456, 0.03, 0.08),
            "{CMD}SET_PIDX,speed_l,1.235,0.030,0.080",
        )
        self.assertEqual(
            pid_ai_serial.build_set_targetx_command("yaw_rate", 2.5),
            "{CMD}SET_TARGETX,yaw_rate,2.500",
        )

    def test_loop_aware_command_metadata_and_ack_matching(self) -> None:
        """
        函数作用：
            验证命令元数据提取能识别 loop_id，并用于 ACK/ERR 匹配辅助逻辑。

        主要流程：
            1. 从 SET_PIDX 命令提取 command_name 和 loop_id。
            2. 构造 ACK 帧。
            3. 校验 helper 判断该 ACK 可匹配这条 pending 命令。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        metadata = pid_ai_serial.extract_command_metadata("{CMD}SET_PIDX,speed_l,1.000,0.100,0.010")
        ack = pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK")

        self.assertEqual(metadata["command_name"], "SET_PIDX")
        self.assertEqual(metadata["loop_id"], "speed_l")
        self.assertTrue(pid_ai_serial.response_matches_pending_command(metadata, ack))


class PidAiAutoTuneTest(unittest.TestCase):
    """测试自动调参纯状态机，不依赖真实串口。"""

    def make_controller(self) -> "pid_ai_serial.AutoTuneController":
        """
        函数作用：
            构造默认 line-car-cascade profile 自动调参控制器。

        主要流程：
            使用 --auto-tune 的默认安全参数，便于各测试复用同一配置。

        返回值：
            返回 AutoTuneController 实例。
        """
        config = pid_ai_serial.AutoTuneConfig(
            auto=True,
            profile="line-car-cascade",
            mode="auto-tune",
            max_step=0.10,
            window_seconds=0.02,
            ack_timeout_seconds=0.50,
            rollback_on_regression=True,
        )
        return pid_ai_serial.AutoTuneController(config)

    def ingest_cfgs(self, controller: "pid_ai_serial.AutoTuneController") -> None:
        """
        函数作用：
            向自动调参控制器注入小车串级 profile 的全部 loop 配置。

        主要流程：
            按 speed_l、speed_r、yaw_rate、line_outer 顺序写入 CFGX，模拟配置同步阶段。

        返回值：
            无返回值。
        """
        for loop_id in ["speed_l", "speed_r", "yaw_rate", "line_outer"]:
            line = VALID_CFGX_LINE.replace("speed_l", loop_id, 1).replace("left_speed", loop_id)
            controller.ingest_frame(pid_ai_serial.parse_frame(line))

    def ingest_window(
        self,
        controller: "pid_ai_serial.AutoTuneController",
        loop_id: str,
        errors: list[float],
        *,
        fault: int = 0,
        line_lost: int = 0,
        start_ms: int = 10,
        step_ms: int = 10,
    ) -> None:
        """
        函数作用：
            向自动调参控制器注入一个测试遥测窗口。

        主要流程：
            逐条生成 PIDX 帧，按 errors 覆盖 error 字段；必要时注入 fault 或 SENS 丢线状态。

        参数说明：
            controller 为被测自动调参控制器。
            loop_id 为目标环路。
            errors 为窗口内误差序列。
            fault 为注入到最后一条 PIDX 的故障位图。
            line_lost 为注入到 SENS 的丢线状态。
            start_ms 为第一条样本的板端毫秒时间，用于验证按秒裁剪评分窗口。
            step_ms 为相邻样本的板端毫秒间隔。

        返回值：
            无返回值。
        """
        for index, error in enumerate(errors, start=1):
            ms = start_ms + (index - 1) * step_ms
            line = (
                f"{{PIDX}}{loop_id},{loop_id},{index},{ms},10.000,100.000,"
                f"{100.0 - error:.3f},{error:.3f},0.000,0.000,0.000,0.000,0.000,"
                "0.000,0.000,0.000,0.000,0.000,1000.000,0,0,1,1,1,"
                f"{fault if index == len(errors) else 0}"
            )
            controller.ingest_frame(pid_ai_serial.parse_frame(line))

        if line_lost:
            sens_line = VALID_SENS_LINE.replace(",0,1.500", ",1,1.500")
            controller.ingest_frame(pid_ai_serial.parse_frame(sens_line))

    def test_state_machine_tunes_speed_loop_before_outer_loop(self) -> None:
        """
        函数作用：
            验证串级自动调参会先选择速度内环，不会越级先调 yaw_rate 或 line_outer。

        主要流程：
            1. 注入四个 loop 的配置和基线遥测窗口。
            2. 反复调用 plan_next_action 推进状态机。
            3. 校验第一个待发送命令是 speed_l 的 SET_PIDX。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        for loop_id in ["speed_l", "speed_r", "yaw_rate", "line_outer"]:
            self.ingest_window(controller, loop_id, [12.0, 11.0, 10.0])

        action = controller.plan_next_action()

        self.assertEqual(action["type"], "send")
        self.assertEqual(action["loop_id"], "speed_l")
        self.assertTrue(action["command"].startswith("{CMD}SET_PIDX,speed_l,"))

    def test_fault_or_line_lost_aborts_without_sending_command(self) -> None:
        """
        函数作用：
            验证故障位或循迹丢线时自动调参进入 ABORT，且不会产生写参命令。

        主要流程：
            注入配置、故障遥测和丢线传感器帧，然后调用 plan_next_action。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [10.0, 9.0, 8.0], fault=1, line_lost=1)

        action = controller.plan_next_action()

        self.assertEqual(action["type"], "abort")
        self.assertEqual(controller.state, "ABORT")

    def test_regression_after_ack_sends_rollback_command(self) -> None:
        """
        函数作用：
            验证自动调参只有 ACK 后才观察结果，并在评分变差时生成回滚命令。

        主要流程：
            1. 注入配置和较好基线窗口，生成 speed_l 写参动作。
            2. 注入 ACK 让状态机进入观察结果阶段。
            3. 注入更差窗口，校验下一个动作是回滚 SET_PIDX。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0])
        action = controller.plan_next_action()
        self.assertEqual(action["type"], "send")

        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"))
        self.ingest_window(controller, "speed_l", [20.0, 21.0, 22.0])
        rollback = controller.plan_next_action()

        self.assertEqual(rollback["type"], "rollback")
        self.assertEqual(rollback["loop_id"], "speed_l")
        self.assertEqual(rollback["command"], "{CMD}SET_PIDX,speed_l,1.200,0.030,0.080")
        self.assertNotIn("speed_l", controller.completed_loops)

    def test_ack_timeout_aborts_pending_step(self) -> None:
        """
        函数作用：
            验证自动调参写参后若超过 ACK 超时仍未收到板端确认，会进入 ABORT。

        主要流程：
            1. 注入 speed_l 配置和遥测，生成 SET_PIDX 写参动作。
            2. 不注入 ACK，直接把 plan_next_action 的 now 推进到超时时刻之后。
            3. 校验状态机返回 abort，避免 CLI/dashboard 永久等待。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0])

        action = controller.plan_next_action(now=100.0)
        self.assertEqual(action["type"], "send")
        timeout = controller.plan_next_action(now=100.6)

        self.assertEqual(timeout["type"], "abort")
        self.assertIn("ACK timeout", timeout["reason"])

    def test_rollback_ack_is_required_before_loop_completed(self) -> None:
        """
        函数作用：
            验证评分变差后的回滚命令必须等待独立 ACK，不能在发送时直接标记 loop 完成。

        主要流程：
            1. 生成并 ACK 一个 speed_l 调参 step。
            2. 注入更差窗口触发 rollback pending。
            3. 在 rollback ACK 前确认 speed_l 未完成；收到 ACK 后才标记完成。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0], start_ms=1000)
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")
        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=100.1)

        self.ingest_window(controller, "speed_l", [20.0, 21.0, 22.0], start_ms=1100)
        rollback = controller.plan_next_action(now=100.2)
        self.assertEqual(rollback["type"], "rollback")
        self.assertEqual(controller.pending_step["phase"], "rollback")
        self.assertNotIn("speed_l", controller.completed_loops)

        waiting = controller.plan_next_action(now=100.3)
        self.assertEqual(waiting["type"], "wait")
        self.assertEqual(waiting["reason"], "waiting for rollback ACK")
        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=100.4)

        self.assertIn("speed_l", controller.completed_loops)
        self.assertIsNone(controller.pending_step)

    def test_rollback_err_or_timeout_aborts(self) -> None:
        """
        函数作用：
            验证回滚命令被 ERR 拒绝或等待 ACK 超时时，自动调参都会进入 ABORT。

        主要流程：
            分别构造 rollback pending，然后注入 ERR 或推进 now 到超时时间之后。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0], start_ms=1000)
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")
        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=100.1)
        self.ingest_window(controller, "speed_l", [20.0, 21.0, 22.0], start_ms=1100)
        self.assertEqual(controller.plan_next_action(now=100.2)["type"], "rollback")

        controller.handle_response(pid_ai_serial.parse_frame("{ERR}SET_PIDX,ARG_INVALID,LOOP_NOT_FOUND"), now=100.3)
        err_action = controller.plan_next_action(now=100.3)
        self.assertEqual(err_action["type"], "abort")
        self.assertIn("board rejected", err_action["reason"])

        timeout_controller = self.make_controller()
        self.ingest_cfgs(timeout_controller)
        self.ingest_window(timeout_controller, "speed_l", [5.0, 4.0, 3.0], start_ms=1000)
        self.assertEqual(timeout_controller.plan_next_action(now=200.0)["type"], "send")
        timeout_controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=200.1)
        self.ingest_window(timeout_controller, "speed_l", [20.0, 21.0, 22.0], start_ms=1100)
        self.assertEqual(timeout_controller.plan_next_action(now=200.2)["type"], "rollback")

        timeout_action = timeout_controller.plan_next_action(now=200.8)
        self.assertEqual(timeout_action["type"], "abort")
        self.assertIn("ACK timeout", timeout_action["reason"])

    def test_score_loop_uses_window_seconds(self) -> None:
        """
        函数作用：
            验证评分窗口按 window_seconds 和样本时间戳裁剪，而不是固定最近 50 条。

        主要流程：
            1. 注入两条很旧且误差很大的样本。
            2. 注入三条最新且误差很小的样本。
            3. window_seconds=0.02 时只应统计最新 20ms 内样本，平均误差接近 2。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [100.0, 100.0], start_ms=1000, step_ms=10)
        self.ingest_window(controller, "speed_l", [1.0, 2.0, 3.0], start_ms=2000, step_ms=10)

        score = controller.score_loop("speed_l")

        self.assertAlmostEqual(score["mean_abs_error"], 2.0)
        self.assertAlmostEqual(score["max_abs_error"], 3.0)


if __name__ == "__main__":
    unittest.main()

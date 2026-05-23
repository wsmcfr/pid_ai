import pathlib
import io
import sys
import tempfile
import unittest
import json
import time


# 测试脚本目录，用于把 skill 自带的 scripts 目录加入导入路径。
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pid_ai_dashboard import DashboardRequestHandler, DashboardState, extract_command_name
import pid_ai_serial


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

    def test_same_loop_command_name_pending_is_rejected(self) -> None:
        """
        函数作用：
            验证 dashboard 会拒绝同名分环命令并发 pending，避免 ACK/ERR 不带 loop_id 时错配。

        主要流程：
            1. 先记录 speed_l 的 SET_PIDX pending 命令。
            2. 在 ACK 前尝试记录 speed_r 的 SET_PIDX。
            3. 校验第二条命令被拒绝；收到第一条 ACK 后可再次记录 SET_PIDX。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)

        state.record_command("{CMD}SET_PIDX,speed_l,1.200,0.030,0.080", reason="auto-tune step")
        with self.assertRaises(ValueError):
            state.record_command("{CMD}SET_PIDX,speed_r,1.200,0.030,0.080", reason="auto-tune step")

        state.ingest_line("{ACK}SET_PIDX,OK")
        entry = state.record_command("{CMD}SET_PIDX,speed_r,1.200,0.030,0.080", reason="auto-tune step")

        self.assertEqual(entry["loop_id"], "speed_r")
        self.assertEqual(entry["status"], "pending")

    def test_disconnect_aborts_enabled_autotune(self) -> None:
        """
        函数作用：
            验证串口断开时 dashboard 会同步中止已启用的自动调参状态。

        主要流程：
            1. 启用 auto-tune。
            2. 调用 disconnect 模拟用户断开或读线程结束后的清理。
            3. 校验状态机进入 ABORT，且 UI 状态关闭自动调参。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)
        state.configure_autotune(enabled=True, mode="auto-tune")

        snapshot = state.disconnect()

        self.assertFalse(snapshot["autotune"]["enabled"])
        self.assertEqual(snapshot["autotune"]["state"], "ABORT")
        self.assertIn("serial disconnected", snapshot["autotune"]["last_action"]["reason"])

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

    def test_binary_pid_bytes_update_same_state_as_text_frame(self) -> None:
        """
        函数作用：
            验证 dashboard 能接收二进制 PID 帧，并像文本 {PID} 一样更新样本和 latest_pid。

        主要流程：
            1. 用文本测试帧生成 typed data。
            2. 构造二进制 PID 帧并调用 ingest_bytes。
            3. 校验样本缓冲和 latest_pid 都被更新。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        state = DashboardState(max_samples=4)
        parsed = pid_ai_serial.parse_frame(make_pid_line(1, 90.0))
        binary_frame = pid_ai_serial.build_binary_frame("pid", parsed["data"], transport_seq=10)

        frames = state.ingest_bytes(binary_frame)
        snapshot = state.snapshot()

        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["valid"])
        self.assertEqual(snapshot["latest_pid"]["data"]["seq"], 1)
        self.assertEqual(snapshot["sample_count"], 1)

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

    def test_experiment_record_is_saved_for_acknowledged_pidx_change(self) -> None:
        """
        函数作用：
            验证 dashboard 会把一次已 ACK 的分环参数修改保存为实验记录文件。

        主要流程：
            1. 使用临时目录创建启用实验记录的 DashboardState。
            2. 注入改参前的 CFGX 和 PIDX 样本，作为参数快照和前置曲线。
            3. 记录 SET_PIDX 命令并注入 ACK，模拟板端确认参数已生效。
            4. 注入 ACK 后的新 PIDX 样本，触发后置曲线窗口保存。
            5. 读取实验 JSON，校验命令、ACK、前后样本和配置快照都已落盘。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            state = DashboardState(
                max_samples=8,
                experiment_dir=pathlib.Path(temp_dir),
                experiment_window_seconds=0.2,
            )

            state.ingest_line(make_cfgx_line("speed_l", 1.0))
            state.ingest_line(make_pidx_line("speed_l", 1, 80.0))
            state.ingest_line(make_pidx_line("speed_l", 2, 82.0))
            state.record_command("{CMD}SET_PIDX,speed_l,1.200,0.030,0.080", reason="manual tune")
            state.ingest_line("{ACK}SET_PIDX,speed_l,OK")
            state.ingest_line(make_pidx_line("speed_l", 3, 91.0))
            state.ingest_line(make_pidx_line("speed_l", 4, 95.0))

            snapshot = state.snapshot()
            files = sorted(pathlib.Path(temp_dir).glob("*.json"))

            self.assertEqual(len(files), 1)
            self.assertEqual(snapshot["experiment_recording"]["record_count"], 1)
            self.assertEqual(snapshot["experiment_recording"]["latest_record"]["status"], "ack")

            record = json.loads(files[0].read_text(encoding="utf-8"))

            self.assertEqual(record["command"]["command_name"], "SET_PIDX")
            self.assertEqual(record["command"]["loop_id"], "speed_l")
            self.assertEqual(record["command"]["reason"], "manual tune")
            self.assertEqual(record["result"]["status"], "ack")
            self.assertEqual(record["response"]["detail"], "OK")
            self.assertAlmostEqual(record["before_config"]["data"]["kp"], 1.0)
            self.assertEqual([sample["data"]["seq"] for sample in record["before_samples"]], [1, 2])
            self.assertEqual([sample["data"]["seq"] for sample in record["after_samples"]], [3, 4])
            self.assertIn("before_score", record["result"])
            self.assertIn("after_score", record["result"])

    def test_experiment_record_preserves_local_send_error(self) -> None:
        """
        函数作用：
            验证串口未连接导致的本地发送失败也会写入实验记录。

        主要流程：
            1. 使用临时目录创建启用实验记录的 DashboardState。
            2. 在未连接串口时调用 send_command 发送 SET_PID。
            3. 读取实验 JSON，校验状态为 error 且响应类型为 local_error。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            state = DashboardState(max_samples=4, experiment_dir=pathlib.Path(temp_dir))

            entry = state.send_command("{CMD}SET_PID,1.200,0.030,0.080", reason="manual tune")
            files = sorted(pathlib.Path(temp_dir).glob("*.json"))

            self.assertEqual(entry["status"], "error")
            self.assertEqual(len(files), 1)

            record = json.loads(files[0].read_text(encoding="utf-8"))

            self.assertEqual(record["result"]["status"], "error")
            self.assertEqual(record["response"]["kind"], "local_error")
            self.assertIn("not connected", record["response"]["detail"])

    def test_autotune_http_payload_preserves_ack_timeout(self) -> None:
        """
        函数作用：
            验证 POST /api/autotune 的业务处理会把 ack_timeout_seconds 传入状态机配置。

        主要流程：
            1. 构造只包含 state 和 write_json 的轻量 handler 对象。
            2. 调用 DashboardRequestHandler.handle_autotune 的未绑定方法。
            3. 校验响应快照中的 ack_timeout_seconds 使用请求值，而不是默认值。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeHandler:
            """
            类作用：
                为 handler 单元测试提供最小替身，避免启动真实 HTTPServer。

            字段说明：
                state 保存 DashboardState 实例。
                payload 保存 write_json 收到的响应对象。
            """

            def __init__(self) -> None:
                """初始化 fake handler 的状态容器。"""
                self.state = DashboardState(max_samples=4)
                self.payload = None

            def write_json(self, payload: dict, status=None) -> None:
                """
                函数作用：
                    捕获 handler 准备写出的 JSON 响应。

                参数说明：
                    payload 为业务方法生成的响应快照。
                    status 为可选 HTTP 状态码，本测试不使用。

                返回值：
                    无返回值，只把 payload 存到对象字段中。
                """
                self.payload = payload

        handler = FakeHandler()

        DashboardRequestHandler.handle_autotune(
            handler,
            {
                "enabled": True,
                "mode": "auto-tune",
                "ack_timeout_seconds": 4.5,
                "min_post_ack_samples": 5,
            },
        )

        self.assertIsNotNone(handler.payload)
        self.assertEqual(handler.payload["autotune"]["ack_timeout_seconds"], 4.5)
        self.assertEqual(handler.payload["autotune"]["min_post_ack_samples"], 5)

    def test_dashboard_autotune_tick_aborts_when_ack_timeout_elapses_without_frames(self) -> None:
        """
        函数作用：
            验证 dashboard 在串口静默时也会推进自动调参 ACK 超时，而不是永远 pending。

        主要流程：
            1. 启用 auto-tune 并注入 CFGX/PIDX，使状态机生成 pending SET_PIDX。
            2. 不再注入任何 ACK/ERR 或遥测帧。
            3. 调用 tick_autotune，模拟 `/api/status` 轮询或读线程空闲 tick，校验进入 ABORT。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        class FakeSerial:
            """
            类作用：
                模拟已连接串口，使 auto-tune 可以成功写入 step 命令但永远收不到 ACK。
            """

            def write(self, data: bytes) -> int:
                """
                函数作用：
                    模拟 pyserial.write 成功，确保测试覆盖 ACK 超时而不是本地写失败。

                参数说明：
                    data 为待发送命令字节。

                返回值：
                    返回写入字节数。
                """
                return len(data)

            def flush(self) -> None:
                """模拟 pyserial.flush，无返回值。"""
                return None

        state = DashboardState(max_samples=8)
        with state._lock:
            state._serial_handle = FakeSerial()
            state.connected = True
        state.configure_autotune(
            enabled=True,
            mode="auto-tune",
            ack_timeout_seconds=0.01,
            window_seconds=0.1,
        )
        state.ingest_line(make_cfgx_line("speed_l", 1.0))
        state.ingest_line(make_pidx_line("speed_l", 1, 90.0))

        pending = state.snapshot()["autotune"]
        self.assertEqual(pending["last_action"]["type"], "send")
        time.sleep(0.02)
        snapshot = state.tick_autotune()

        self.assertEqual(snapshot["autotune"]["state"], "ABORT")
        self.assertIn("ACK timeout", snapshot["autotune"]["last_action"]["reason"])

    def test_write_api_requires_matching_csrf_token(self) -> None:
        """
        函数作用：
            验证 dashboard 的写 API 需要启动时生成的本地 token，防止其他网页直接 POST 命令。

        主要流程：
            1. 创建 state 并取得 api_token。
            2. 构造最小 fake handler，分别用缺失 token 和正确 token 调用 require_write_token。
            3. 校验缺失 token 被拒绝，正确 token 通过。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeHandler:
            """
            类作用：
                为 require_write_token 单元测试提供 state 和请求头字段。
            """

            def __init__(self, state: DashboardState, token: str | None) -> None:
                """初始化 fake handler 的 state 和 headers。"""
                self.state = state
                self.headers = {}
                if token is not None:
                    self.headers["X-PID-AI-Token"] = token

        state = DashboardState(max_samples=4)
        missing = FakeHandler(state, None)
        with self.assertRaises(ValueError):
            DashboardRequestHandler.require_write_token(missing)

        allowed = FakeHandler(state, state.api_token)
        DashboardRequestHandler.require_write_token(allowed)

    def test_json_responses_do_not_enable_cross_origin_reads(self) -> None:
        """
        函数作用：
            验证 dashboard JSON 响应不主动开放跨站读取权限。

        主要流程：
            构造最小 fake handler 调用 write_json，检查响应头中没有 Access-Control-Allow-Origin。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeHandler:
            """
            类作用：
                捕获 write_json 写出的状态码、响应头和 body，避免启动真实 HTTP 服务。
            """

            def __init__(self) -> None:
                """初始化响应捕获容器。"""
                self.headers = []
                self.body = io.BytesIO()
                self.wfile = self.body

            def send_response(self, status) -> None:
                """
                函数作用：
                    记录 HTTP 状态码。

                参数说明：
                    status 为 write_json 传入的 HTTPStatus。

                返回值：
                    无返回值。
                """
                self.status = status

            def send_header(self, name: str, value: str) -> None:
                """
                函数作用：
                    记录响应头键值。

                参数说明：
                    name 为响应头名称。
                    value 为响应头值。

                返回值：
                    无返回值。
                """
                self.headers.append((name, value))

            def end_headers(self) -> None:
                """模拟 BaseHTTPRequestHandler.end_headers，无返回值。"""
                return None

        handler = FakeHandler()

        DashboardRequestHandler.write_json(handler, {"ok": True})

        header_names = {name.lower() for name, _value in handler.headers}
        self.assertNotIn("access-control-allow-origin", header_names)

    def test_status_endpoint_ticks_autotune_before_returning_snapshot(self) -> None:
        """
        函数作用：
            验证 GET /api/status 会推进自动调参 ACK 超时检查。

        主要流程：
            构造最小 fake handler 调用 do_GET，要求响应来自 state.tick_autotune 而不是裸 snapshot。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeState:
            """
            类作用：
                为 do_GET 测试提供可观察的 tick_autotune 行为。
            """

            def __init__(self) -> None:
                """初始化调用计数。"""
                self.tick_count = 0

            def tick_autotune(self) -> dict:
                """
                函数作用：
                    模拟 status 轮询推进状态机并返回快照。

                返回值：
                    返回带 tick_count 的响应对象，供断言使用。
                """
                self.tick_count += 1
                return {"tick_count": self.tick_count}

        class FakeHandler:
            """
            类作用：
                捕获 do_GET 对 `/api/status` 写出的 JSON 响应。
            """

            def __init__(self) -> None:
                """初始化 path、state 和响应载荷。"""
                self.path = "/api/status"
                self.state = FakeState()
                self.payload = None

            def write_json(self, payload: dict, status=None) -> None:
                """
                函数作用：
                    捕获 JSON 响应体。

                参数说明：
                    payload 为 do_GET 准备写出的响应对象。
                    status 为可选 HTTP 状态码，本测试不使用。

                返回值：
                    无返回值。
                """
                self.payload = payload

        handler = FakeHandler()

        DashboardRequestHandler.do_GET(handler)

        self.assertEqual(handler.state.tick_count, 1)
        self.assertEqual(handler.payload, {"tick_count": 1})

    def test_options_response_does_not_enable_cross_origin_access(self) -> None:
        """
        函数作用：
            验证 dashboard 的 OPTIONS 响应不会重新开放跨站 CORS 入口。

        主要流程：
            构造最小 fake handler 调用 do_OPTIONS，检查响应状态为 204 且没有 Access-Control-Allow-* 头。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeHandler:
            """
            类作用：
                捕获 do_OPTIONS 写出的状态码和响应头，避免启动真实 HTTP 服务。
            """

            def __init__(self) -> None:
                """初始化响应捕获容器。"""
                self.headers = []

            def send_response(self, status) -> None:
                """
                函数作用：
                    记录 HTTP 状态码。

                参数说明：
                    status 为 do_OPTIONS 传入的 HTTPStatus。

                返回值：
                    无返回值。
                """
                self.status = status

            def send_header(self, name: str, value: str) -> None:
                """
                函数作用：
                    记录响应头键值。

                参数说明：
                    name 为响应头名称。
                    value 为响应头值。

                返回值：
                    无返回值。
                """
                self.headers.append((name, value))

            def end_headers(self) -> None:
                """模拟 BaseHTTPRequestHandler.end_headers，无返回值。"""
                return None

        handler = FakeHandler()

        DashboardRequestHandler.do_OPTIONS(handler)

        header_names = {name.lower() for name, _value in handler.headers}
        self.assertEqual(int(handler.status), 204)
        self.assertNotIn("access-control-allow-origin", header_names)
        self.assertNotIn("access-control-allow-headers", header_names)
        self.assertNotIn("access-control-allow-methods", header_names)

    def test_dashboard_html_uses_token_and_escape_helpers_for_dynamic_content(self) -> None:
        """
        函数作用：
            验证 dashboard 页面会携带 API token，并提供 HTML 转义 helper 渲染动态协议文本。

        主要流程：
            读取 dashboard 模板，检查 `window.PID_AI_API_TOKEN`、`escapeHtml` 和写请求 token header。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        import pid_ai_dashboard

        html = pid_ai_dashboard.build_index_html("token-for-test")

        self.assertIn("window.PID_AI_API_TOKEN", html)
        self.assertIn("function escapeHtml", html)
        self.assertIn('headers["X-PID-AI-Token"] = window.PID_AI_API_TOKEN', html)


if __name__ == "__main__":
    unittest.main()

import argparse
import contextlib
import io
import pathlib
import sys
import struct
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

    def test_sens_sample_without_battery_is_valid(self) -> None:
        """
        函数作用：
            验证没有电池电压字段的 {SENS} 帧仍可作为有效传感器安全帧使用。

        主要流程：
            1. 构造只包含 ms 到 v_avg 的 {SENS} 帧，模拟多数不采集电池电压的单片机工程。
            2. 调用 parse_frame。
            3. 校验 line_lost、yaw_rate 和速度字段仍被解析，battery 以 None 表示未提供。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        line_without_battery = VALID_SENS_LINE.rsplit(",", 1)[0]

        parsed = pid_ai_serial.parse_frame(line_without_battery)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "sens")
        self.assertEqual(parsed["data"]["line_lost"], 0)
        self.assertAlmostEqual(parsed["data"]["yaw_rate"], 0.25)
        self.assertAlmostEqual(parsed["data"]["v_avg"], 0.805)
        self.assertIsNone(parsed["data"]["battery"])

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

    def test_build_single_loop_pid_command_uses_three_decimal_parameters(self) -> None:
        """
        函数作用：
            验证单环 PID 命令构建器会生成旧协议 SET_PID，并按三位小数稳定格式化。

        主要流程：
            调用 build_set_pid_command，校验完整 `{CMD}SET_PID,kp,ki,kd` 文本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        self.assertEqual(
            pid_ai_serial.build_set_pid_command(1.23456, 0.03, 0.08),
            "{CMD}SET_PID,1.235,0.030,0.080",
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

    def test_ack_and_err_reject_unexpected_extra_fields(self) -> None:
        """
        函数作用：
            验证 ACK/ERR 帧必须按协议字段数量精确解析，不能静默忽略多余字段。

        主要流程：
            1. 构造旧格式 ACK、旧格式 ERR、分环 ACK、分环 ERR 的多余字段版本。
            2. 调用 parse_frame。
            3. 校验这些坏响应都不会被标记为 valid，避免污染命令历史和自动调参状态。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        bad_lines = [
            "{ACK}SET_PID,OK,EXTRA",
            "{ERR}SET_PID,ARG_INVALID,FLOAT_PARSE_FAIL,EXTRA",
            "{ACK}SET_PIDX,speed_l,OK,EXTRA",
            "{ERR}SET_PIDX,speed_l,ARG_INVALID,FLOAT_PARSE_FAIL,EXTRA",
        ]

        for line in bad_lines:
            with self.subTest(line=line):
                parsed = pid_ai_serial.parse_frame(line)

                self.assertFalse(parsed["valid"])
                self.assertIn("unexpected field count", parsed["error"])

    def test_err_rejects_unknown_status_text(self) -> None:
        """
        函数作用：
            验证 ERR 帧的 status 必须是 PIDAI_ProtocolStatusText 定义的稳定文本。

        主要流程：
            注入未知 status 的 ERR 帧，并校验 parser 把它标记为无效响应。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        parsed = pid_ai_serial.parse_frame("{ERR}SET_PID,NOT_A_STATUS,FLOAT_PARSE_FAIL")

        self.assertFalse(parsed["valid"])
        self.assertIn("unknown status", parsed["error"])

    def test_binary_crc16_uses_ccitt_false_standard_vector(self) -> None:
        """
        函数作用：
            验证 Python 侧二进制协议 CRC 与板端约定的 CRC-16/CCITT-FALSE 一致。

        主要流程：
            使用标准测试向量 b"123456789"，校验结果为 0x29B1。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        self.assertEqual(pid_ai_serial.binary_crc16(b"123456789"), 0x29B1)

    def test_binary_pid_frame_parses_to_same_typed_data_as_text_pid(self) -> None:
        """
        函数作用：
            验证二进制 {PID} 帧能解析成与文本 {PID} 相同字段名的 typed frame。

        主要流程：
            1. 先用现有文本 parser 得到协议样例的数据字典。
            2. 使用测试构建器打包为二进制 PID 帧。
            3. 调用 parse_binary_frame 校验 kind、transport_seq 和关键字段。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=77)

        parsed = pid_ai_serial.parse_binary_frame(binary_frame)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["kind"], "pid")
        self.assertEqual(parsed["transport_seq"], 77)
        self.assertEqual(parsed["data"]["seq"], 1024)
        self.assertAlmostEqual(parsed["data"]["target"], 1000.0)
        self.assertEqual(parsed["data"]["fault"], 0)

    def test_binary_frame_rejects_bad_crc(self) -> None:
        """
        函数作用：
            验证二进制帧 CRC 错误时不会被当作有效 typed frame。

        主要流程：
            构造合法 PID 二进制帧后翻转最后一个 CRC 字节，再调用 parse_binary_frame。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = bytearray(pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=77))
        binary_frame[-1] ^= 0x01

        parsed = pid_ai_serial.parse_binary_frame(bytes(binary_frame))

        self.assertFalse(parsed["valid"])
        self.assertIn("CRC", parsed["error"])

    def test_binary_pid_frame_rejects_non_finite_float_fields(self) -> None:
        """
        函数作用：
            验证二进制 PID payload 中的 NaN/Inf 不会绕过文本 parser 的有限数校验。

        主要流程：
            1. 构造一条合法二进制 PID 帧。
            2. 手工把 target 的 float32 字节改成 NaN，并重新计算 CRC，模拟板端坏数据。
            3. 调用 parse_binary_frame，要求返回 valid=False 且错误指向 target。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = bytearray(pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=83))
        target_offset = pid_ai_serial.BINARY_HEADER_SIZE + 12
        binary_frame[target_offset : target_offset + 4] = struct.pack("<f", float("nan"))
        payload_length = struct.unpack("<H", binary_frame[9:11])[0]
        crc = pid_ai_serial.binary_crc16(binary_frame[2 : pid_ai_serial.BINARY_HEADER_SIZE + payload_length])
        binary_frame[pid_ai_serial.BINARY_HEADER_SIZE + payload_length :] = struct.pack("<H", crc)

        parsed = pid_ai_serial.parse_binary_frame(bytes(binary_frame))

        self.assertFalse(parsed["valid"])
        self.assertIn("target must be finite", parsed["error"])

    def test_binary_cfg_and_cfgx_frames_parse_to_typed_config(self) -> None:
        """
        函数作用：
            验证二进制 CFG/CFGX 配置帧能解析为与文本协议一致的 typed config。

        主要流程：
            1. 用文本 CFG 和 CFGX 样例生成 data 字典。
            2. 分别构造二进制配置帧并解析。
            3. 校验 profile_id、loop_id、kp 和 version 等关键字段。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        cfg = pid_ai_serial.parse_frame(VALID_CFG_LINE)
        cfgx = pid_ai_serial.parse_frame(VALID_CFGX_LINE)

        parsed_cfg = pid_ai_serial.parse_binary_frame(
            pid_ai_serial.build_binary_frame("cfg", cfg["data"], transport_seq=80)
        )
        parsed_cfgx = pid_ai_serial.parse_binary_frame(
            pid_ai_serial.build_binary_frame("cfgx", cfgx["data"], transport_seq=81)
        )

        self.assertTrue(parsed_cfg["valid"])
        self.assertEqual(parsed_cfg["kind"], "cfg")
        self.assertEqual(parsed_cfg["data"]["profile_id"], 0)
        self.assertAlmostEqual(parsed_cfg["data"]["kp"], 1.2)
        self.assertEqual(parsed_cfg["data"]["version"], 1)
        self.assertTrue(parsed_cfgx["valid"])
        self.assertEqual(parsed_cfgx["kind"], "cfgx")
        self.assertEqual(parsed_cfgx["data"]["loop_id"], "speed_l")
        self.assertAlmostEqual(parsed_cfgx["data"]["kd"], 0.08)
        self.assertEqual(parsed_cfgx["data"]["version"], 3)

    def test_binary_stream_decoder_handles_split_frames_and_garbage_prefix(self) -> None:
        """
        函数作用：
            验证二进制流解码器能处理串口分块输入，并能跳过帧头前的噪声字节。

        主要流程：
            1. 构造合法二进制 PID 帧并在前面插入垃圾字节。
            2. 分两次 feed 给 BinaryFrameDecoder，第一次不应输出半帧。
            3. 第二次补齐后应输出一条有效 PID typed frame。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = b"noise" + pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=78)
        decoder = pid_ai_serial.BinaryFrameDecoder()

        self.assertEqual(decoder.feed(binary_frame[:8]), [])
        frames = decoder.feed(binary_frame[8:])

        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["valid"])
        self.assertEqual(frames[0]["kind"], "pid")
        self.assertEqual(frames[0]["transport_seq"], 78)

    def test_protocol_stream_decoder_handles_text_and_binary_frames(self) -> None:
        """
        函数作用：
            验证混合协议流解码器能同时处理文本行和无换行二进制帧。

        主要流程：
            1. 构造一条文本 PID 行和一条二进制 PID 帧。
            2. 分两次 feed，模拟串口任意分块。
            3. 校验输出顺序保持为文本 PID、二进制 PID。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=79)
        stream = pid_ai_serial.ProtocolStreamDecoder()
        mixed = (VALID_PID_LINE + "\r\n").encode("ascii") + binary_frame

        self.assertEqual(stream.feed(mixed[:25]), [])
        frames = stream.feed(mixed[25:])

        self.assertEqual([frame["kind"] for frame in frames], ["pid", "pid"])
        self.assertEqual(frames[0].get("transport"), "text")
        self.assertEqual(frames[1].get("transport"), "binary")
        self.assertEqual(frames[1].get("transport_seq"), 79)

    def test_binary_frames_contribute_to_scan_score(self) -> None:
        """
        函数作用：
            验证自动扫描逻辑会把有效二进制 PID 帧计为协议匹配。

        主要流程：
            构造二进制 PID 帧并解析，校验 scan 前缀和分数不低于文本主帧。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        text_frame = pid_ai_serial.parse_frame(VALID_PID_LINE)
        binary_frame = pid_ai_serial.parse_binary_frame(
            pid_ai_serial.build_binary_frame("pid", text_frame["data"], transport_seq=82)
        )
        binary_frame["transport"] = "binary"

        prefix = pid_ai_serial.frame_scan_prefix(binary_frame)

        self.assertEqual(prefix, "{BIN:PID}")
        self.assertGreaterEqual(pid_ai_serial.score_scan_frame(binary_frame, prefix), 15)

    def test_protocol_stream_decoder_bounds_unterminated_text_noise(self) -> None:
        """
        函数作用：
            验证混合协议流解码器会限制无换行文本噪声的缓冲长度。

        主要流程：
            1. 使用很小的 max_buffer_size 创建 decoder。
            2. 输入大量没有换行、没有二进制 magic 的噪声字节。
            3. 校验内部缓冲不会无限增长，避免异常串口流耗尽内存。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        stream = pid_ai_serial.ProtocolStreamDecoder(max_buffer_size=16)

        frames = stream.feed(b"x" * 128)

        self.assertEqual(frames, [])
        self.assertLessEqual(len(stream._buffer), 16)

    def test_text_fields_reject_html_like_loop_identifiers(self) -> None:
        """
        函数作用：
            验证 loop_id/loop_name 只接受安全 ASCII 标识符，不允许 HTML 片段进入 UI 状态。

        主要流程：
            构造带 `<img...>` loop_id 的 PIDX 帧并解析，要求该帧被拒绝。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        bad_line = VALID_PIDX_LINE.replace("speed_l", "<img src=x onerror=alert(1)>", 1)

        parsed = pid_ai_serial.parse_frame(bad_line)

        self.assertFalse(parsed["valid"])
        self.assertIn("loop_id", parsed["error"])

    def test_normalize_command_rejects_embedded_newline_injection(self) -> None:
        """
        函数作用：
            验证单条 `{CMD}` 命令不能携带额外换行和第二条命令。

        主要流程：
            调用 normalize_command 处理带 CRLF 的命令文本，要求抛出 ValueError。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        with self.assertRaises(ValueError):
            pid_ai_serial.normalize_command("{CMD}GET_CFG\r\n{CMD}ENABLE,1")

    def test_send_command_ignores_unrelated_ack_response(self) -> None:
        """
        函数作用：
            验证 CLI send 子命令不会把其他命令的 ACK 当成本次命令成功。

        主要流程：
            1. 用 fake serial 替换 open_serial，先返回一条不匹配的 ACK，之后一直超时。
            2. 发送 SET_PID。
            3. 校验 cmd_send 返回失败，说明它等待匹配当前命令的 ACK/ERR。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """

        class FakeSerial:
            """
            类作用：
                模拟最小 pyserial 对象，只提供 cmd_send 用到的上下文管理、写入和 readline。
            """

            def __init__(self) -> None:
                """初始化待返回的串口行。"""
                self.lines = [b"{ACK}GET_CFG,OK\r\n"]

            def __enter__(self) -> "FakeSerial":
                """进入 with 块时返回自身。"""
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                """退出 with 块无需释放真实资源。"""
                return None

            def write(self, data: bytes) -> int:
                """
                函数作用：
                    接收待写命令字节并模拟写入成功。

                参数说明：
                    data 为 cmd_send 编码后的 `{CMD}` 字节。

                返回值：
                    返回写入字节数。
                """
                return len(data)

            def flush(self) -> None:
                """模拟 pyserial flush，无返回值。"""
                return None

            def readline(self) -> bytes:
                """
                函数作用：
                    依次返回预设串口行；耗尽后返回空字节模拟超时。

                返回值：
                    返回一行 bytes 或空 bytes。
                """
                if self.lines:
                    return self.lines.pop(0)
                return b""

        args = argparse.Namespace(
            command="{CMD}SET_PID,1.000,0.100,0.010",
            port="COM1",
            baud=115200,
            auto=False,
            response_seconds=0.01,
            jsonl=True,
        )
        original_open_serial = pid_ai_serial.open_serial
        pid_ai_serial.open_serial = lambda port, baud, timeout: FakeSerial()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = pid_ai_serial.cmd_send(args)
        finally:
            pid_ai_serial.open_serial = original_open_serial

        self.assertEqual(result, 1)


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

    def make_single_loop_controller(self) -> "pid_ai_serial.AutoTuneController":
        """
        函数作用：
            构造单环 PID 自动调参控制器。

        主要流程：
            使用 single-loop profile、auto-tune 模式和默认安全参数，让测试覆盖旧 `{PID}/{CFG}`
            协议路径是否能生成 `{CMD}SET_PID`。

        返回值：
            返回 AutoTuneController 实例。
        """
        config = pid_ai_serial.AutoTuneConfig(
            auto=True,
            profile="single-loop",
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
        sat: int = 0,
        anti_windup: int = 0,
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
            sat 为每条样本的输出饱和标志，用于触发饱和调参策略。
            anti_windup 为每条样本的抗饱和标志，用于触发积分收敛策略。
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
                "0.000,0.000,0.000,0.000,0.000,1000.000,"
                f"{sat},{anti_windup},1,1,1,"
                f"{fault if index == len(errors) else 0}"
            )
            controller.ingest_frame(pid_ai_serial.parse_frame(line))

        if line_lost:
            sens_line = VALID_SENS_LINE.replace(",0,1.500", ",1,1.500")
            controller.ingest_frame(pid_ai_serial.parse_frame(sens_line))

    def ingest_single_window(
        self,
        controller: "pid_ai_serial.AutoTuneController",
        errors: list[float],
        *,
        start_ms: int = 10,
        step_ms: int = 10,
    ) -> None:
        """
        函数作用：
            向单环自动调参控制器注入旧 `{PID}` 遥测窗口。

        主要流程：
            逐条生成 PID 帧，按 errors 覆盖 error 字段；用于证明 single-loop profile 不依赖 `{PIDX}`。

        参数说明：
            controller 为被测自动调参控制器。
            errors 为窗口内误差序列。
            start_ms 为第一条样本的板端毫秒时间。
            step_ms 为相邻样本的板端毫秒间隔。

        返回值：
            无返回值。
        """
        for index, error in enumerate(errors, start=1):
            ms = start_ms + (index - 1) * step_ms
            line = (
                f"{{PID}}{index},{ms},10.000,100.000,{100.0 - error:.3f},"
                f"{error:.3f},0.000,0.000,0.000,0.000,0.000,0.000,"
                "0.000,0.000,0.000,0.000,1000.000,0,0,1,1,1,0"
            )
            controller.ingest_frame(pid_ai_serial.parse_frame(line))

    def test_single_loop_profile_generates_set_pid_from_pid_and_cfg_frames(self) -> None:
        """
        函数作用：
            验证单环自动调参能基于旧 `{CFG}` 和 `{PID}` 帧生成 `{CMD}SET_PID`。

        主要流程：
            1. 创建 single-loop profile 控制器。
            2. 注入单环 CFG 配置和 PID 遥测窗口。
            3. 调用 plan_next_action，校验生成旧协议 SET_PID，而不是等待 PIDX/CFGX。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_single_loop_controller()
        controller.ingest_frame(pid_ai_serial.parse_frame(VALID_CFG_LINE))
        self.ingest_single_window(controller, [6.0, 5.8, 5.6, 5.5], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["type"], "send")
        self.assertEqual(action["loop_id"], "single")
        self.assertEqual(action["command"], "{CMD}SET_PID,1.200,0.033,0.080")
        self.assertEqual(controller.pending_step["command_name"], "SET_PID")

    def test_single_loop_profile_rolls_back_with_set_pid_after_ack_regression(self) -> None:
        """
        函数作用：
            验证单环自动调参在 ACK 后评分变差时使用 SET_PID 回滚旧参数。

        主要流程：
            1. 生成单环 SET_PID step 并注入 ACK。
            2. 注入更差的 `{PID}` 遥测窗口。
            3. 校验 rollback 动作仍是 `{CMD}SET_PID`，不依赖分环命令。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_single_loop_controller()
        controller.ingest_frame(pid_ai_serial.parse_frame(VALID_CFG_LINE))
        self.ingest_single_window(controller, [5.0, 4.0, 3.0], start_ms=1000)
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")

        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PID,OK"), now=100.1)
        self.ingest_single_window(controller, [20.0, 21.0, 22.0], start_ms=1100)
        rollback = controller.plan_next_action(now=100.2)

        self.assertEqual(rollback["type"], "rollback")
        self.assertEqual(rollback["loop_id"], "single")
        self.assertEqual(rollback["command"], "{CMD}SET_PID,1.200,0.030,0.080")

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

    def test_timeout_tick_does_not_create_phantom_pending_step(self) -> None:
        """
        函数作用：
            验证串口空读 tick 只能检查 pending 超时，不能凭旧窗口创建未发送的 pending step。

        主要流程：
            1. 注入 CFGX 和 PIDX，让控制器具备生成 SET_PIDX 的条件。
            2. 调用 timeout-only tick，模拟 CLI 串口暂时无数据。
            3. 校验没有生成 send 动作，也没有写入 pending_step。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0])

        action = controller.plan_timeout_action(now=100.0)

        self.assertEqual(action["type"], "wait")
        self.assertEqual(action["reason"], "waiting for valid frame")
        self.assertIsNone(controller.pending_step)

    def test_timeout_tick_still_aborts_existing_pending_step(self) -> None:
        """
        函数作用：
            验证 timeout-only tick 不生成新 step，但已有 pending step 仍会按 ACK 超时中止。

        主要流程：
            1. 正常生成一个 pending SET_PIDX。
            2. 调用 timeout-only tick 并推进时间超过 ACK 超时。
            3. 校验控制器进入 ABORT。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0])
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")

        timeout = controller.plan_timeout_action(now=100.6)

        self.assertEqual(timeout["type"], "abort")
        self.assertIn("ACK timeout", timeout["reason"])

    def test_mismatched_pending_response_aborts_autotune(self) -> None:
        """
        函数作用：
            验证 pending 写参期间收到错配 ACK/ERR 会立即中止自动调参。

        主要流程：
            1. 生成 speed_l 的 pending SET_PIDX。
            2. 注入携带 speed_r loop_id 的 ERR 响应。
            3. 校验状态机进入 ABORT，而不是继续等待 speed_l 的 ACK。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0])
        action = controller.plan_next_action(now=100.0)
        self.assertEqual(action["type"], "send")

        controller.handle_response(
            pid_ai_serial.parse_frame("{ERR}SET_PIDX,speed_r,ARG_INVALID,FLOAT_PARSE_FAIL"),
            now=100.1,
        )
        abort_action = controller.plan_next_action(now=100.1)

        self.assertEqual(abort_action["type"], "abort")
        self.assertIn("mismatched response", abort_action["reason"])

    def test_post_ack_score_uses_only_post_ack_samples(self) -> None:
        """
        函数作用：
            验证 ACK 后效果评分只使用 ACK 之后的新遥测样本。

        主要流程：
            1. 使用很大的 window_seconds，确保旧实现会把 ACK 前基线样本混入当前窗口。
            2. 生成并 ACK 一个 speed_l 调参 step。
            3. 注入一条 ACK 后低误差样本，校验 keep 动作的 current_score 只反映该新样本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        config = pid_ai_serial.AutoTuneConfig(
            auto=True,
            profile="line-car-cascade",
            mode="auto-tune",
            max_step=0.10,
            window_seconds=10.0,
            ack_timeout_seconds=0.50,
            rollback_on_regression=True,
        )
        controller = pid_ai_serial.AutoTuneController(config)
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [10.0, 9.0, 8.0], start_ms=1000)
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")
        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=100.1)

        self.ingest_window(controller, "speed_l", [2.0, 2.0, 2.0], start_ms=1010)
        keep = controller.plan_next_action(now=100.2)

        self.assertEqual(keep["type"], "keep")
        self.assertAlmostEqual(keep["current_score"], 2.5)

    def test_post_ack_requires_minimum_new_samples_before_decision(self) -> None:
        """
        函数作用：
            验证 ACK 后新遥测样本不足时，自动调参必须继续等待而不是立即 keep 或 rollback。

        主要流程：
            1. 使用默认最少 ACK 后样本门槛构造控制器。
            2. 生成并 ACK 一个 speed_l 调参 step。
            3. 只注入 1 条 ACK 后遥测，校验状态机继续等待更多样本。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [5.0, 4.0, 3.0], start_ms=1000)
        self.assertEqual(controller.plan_next_action(now=100.0)["type"], "send")
        controller.handle_response(pid_ai_serial.parse_frame("{ACK}SET_PIDX,OK"), now=100.1)

        self.ingest_window(controller, "speed_l", [20.0], start_ms=1100)
        wait = controller.plan_next_action(now=100.2)

        self.assertEqual(wait["type"], "wait")
        self.assertEqual(wait["reason"], "waiting for post-ACK telemetry")
        self.assertEqual(wait["post_ack_sample_count"], 1)
        self.assertEqual(wait["min_post_ack_samples"], 3)
        self.assertEqual(controller.state, "OBSERVE_RESULT")
        self.assertEqual(controller.rollback_history, [])

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

    def test_steady_bias_strategy_increases_ki_without_changing_kp_or_kd(self) -> None:
        """
        函数作用：
            验证长期同向稳态误差会优先小步增加 Ki，而不是继续增加 Kp。

        主要流程：
            1. 注入 speed_l 配置和无饱和、无过零的稳定偏差窗口。
            2. 生成自动调参动作。
            3. 校验策略元数据和 SET_PIDX 参数只增加 Ki。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [6.0, 5.8, 5.6, 5.5], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["type"], "send")
        self.assertEqual(action["changed_param"], "ki")
        self.assertEqual(action["strategy"], "increase_ki_for_steady_bias")
        self.assertEqual(action["command"], "{CMD}SET_PIDX,speed_l,1.200,0.033,0.080")

    def test_zero_pid_config_aborts_instead_of_sending_noop_step(self) -> None:
        """
        函数作用：
            验证 PID 参数全为 0 时自动调参不会发送无实际变化的 SET_PIDX。

        主要流程：
            1. 注入 speed_l 的零参数 CFGX 和有效遥测窗口。
            2. 调用 plan_next_action 生成动作。
            3. 校验状态机进入 ABORT，提示需要人工 seed 参数，而不是发送 0,0,0。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        zero_cfg = VALID_CFGX_LINE.replace("1.200000,0.030000,0.080000", "0.000000,0.000000,0.000000")
        controller.ingest_frame(pid_ai_serial.parse_frame(zero_cfg))
        self.ingest_window(controller, "speed_l", [6.0, 5.8, 5.6], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["type"], "abort")
        self.assertIn("no-op", action["reason"])
        self.assertIsNone(controller.pending_step)

    def test_tiny_pid_change_that_rounds_to_same_command_aborts(self) -> None:
        """
        函数作用：
            验证小参数按三位小数格式化后没有变化时，自动调参不会发送 no-op 命令。

        主要流程：
            1. 注入 kp 很小的 CFGX，使 10% 调整后仍格式化为 0.000。
            2. 注入慢响应窗口触发增加 Kp 策略。
            3. 校验状态机进入 ABORT 并保留原因。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        tiny_cfg = VALID_CFGX_LINE.replace("1.200000,0.030000,0.080000", "0.000100,0.000000,0.000000")
        controller.ingest_frame(pid_ai_serial.parse_frame(tiny_cfg))
        self.ingest_window(controller, "speed_l", [6.0, 5.0, 4.0], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["type"], "abort")
        self.assertIn("no-op", action["reason"])
        self.assertIsNone(controller.pending_step)

    def test_oscillation_strategy_increases_kd_before_reducing_kp(self) -> None:
        """
        函数作用：
            验证误差频繁过零但没有输出饱和时，策略优先增加 Kd 做阻尼。

        主要流程：
            注入正负交替的 speed_l 误差窗口，生成调参动作后校验只增加 Kd。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(controller, "speed_l", [8.0, -7.0, 6.5, -5.5], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["changed_param"], "kd")
        self.assertEqual(action["strategy"], "increase_kd_for_oscillation")
        self.assertEqual(action["command"], "{CMD}SET_PIDX,speed_l,1.200,0.030,0.088")

    def test_saturation_strategy_reduces_ki_when_anti_windup_is_active(self) -> None:
        """
        函数作用：
            验证长期饱和且 anti_windup 触发时，策略优先降低 Ki 防止积分继续推高输出。

        主要流程：
            注入带上限饱和和抗饱和标志的窗口，生成调参动作后校验只降低 Ki。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        self.ingest_window(
            controller,
            "speed_l",
            [20.0, 19.0, 18.0, 17.0],
            sat=1,
            anti_windup=1,
            start_ms=1000,
            step_ms=10,
        )

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["changed_param"], "ki")
        self.assertEqual(action["strategy"], "reduce_ki_for_integral_saturation")
        self.assertEqual(action["command"], "{CMD}SET_PIDX,speed_l,1.200,0.027,0.080")

    def test_outer_loop_uses_more_conservative_kp_step_for_slow_response(self) -> None:
        """
        函数作用：
            验证循迹外环慢响应时仍可增加 Kp，但步长比内环更保守。

        主要流程：
            1. 将内环和中环标记为已完成，避免串级顺序挡住 line_outer。
            2. 注入 line_outer 的慢响应误差窗口。
            3. 校验 Kp 只增加默认步长的一半。

        返回值：
            unittest 断言失败时抛出异常；通过时无返回值。
        """
        controller = self.make_controller()
        self.ingest_cfgs(controller)
        controller.completed_loops.update({"speed_l", "speed_r", "yaw_rate"})
        self.ingest_window(controller, "line_outer", [15.0, 14.0, 13.0, 12.0], start_ms=1000, step_ms=10)

        action = controller.plan_next_action(now=100.0)

        self.assertEqual(action["loop_id"], "line_outer")
        self.assertEqual(action["changed_param"], "kp")
        self.assertEqual(action["strategy"], "increase_kp_for_slow_response")
        self.assertEqual(action["command"], "{CMD}SET_PIDX,line_outer,1.260,0.030,0.080")


if __name__ == "__main__":
    unittest.main()

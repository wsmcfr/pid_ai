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


if __name__ == "__main__":
    unittest.main()

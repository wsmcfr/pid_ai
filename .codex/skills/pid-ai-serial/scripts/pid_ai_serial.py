#!/usr/bin/env python3
"""
PID AI 串口工具。

脚本作用：
    枚举本机串口，自动识别正在输出 PID AI 文本协议的板端串口，
    并支持读取协议帧或显式发送 {CMD} 命令。

主要流程：
    1. list 子命令列出系统串口。
    2. scan 子命令按候选波特率短时间读取每个串口，按协议前缀打分。
    3. read 子命令从指定串口或自动识别串口持续读取协议帧。
    4. send 子命令在用户显式给出命令时写入一条 {CMD} 文本。

返回值：
    0 表示命令执行成功；非 0 表示未找到串口、串口打开失败或参数错误。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Iterable

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - depends on local environment
    print("pyserial is required. Install with: python -m pip install pyserial", file=sys.stderr)
    raise SystemExit(2) from exc


DEFAULT_BAUD_RATES = [115200, 921600, 57600, 38400, 19200, 9600]
PROTOCOL_PREFIXES = ("{PID}", "{PIDX}", "{CFG}", "{CFGX}", "{SENS}", "{ACK}", "{ERR}", "{STAT}", "{EVT}")
BLUETOOTH_MARKERS = ("bthenum", "bluetooth", "蓝牙")
PID_FIELDS = [
    "seq",
    "ms",
    "dt_ms",
    "target",
    "feedback",
    "error",
    "d_error",
    "integral",
    "p_out",
    "i_out",
    "d_out",
    "ff_out",
    "out_raw",
    "out_limited",
    "actuator",
    "out_min",
    "out_max",
    "sat",
    "anti_windup",
    "mode",
    "enable",
    "sensor_ok",
    "fault",
]
CFG_FIELDS = [
    "profile_id",
    "kp",
    "ki",
    "kd",
    "kf",
    "sample_ms",
    "integral_min",
    "integral_max",
    "out_min",
    "out_max",
    "reverse",
    "mode",
    "version",
    "fault",
]
PIDX_FIELDS = ["loop_id", "loop_name", *PID_FIELDS]
CFGX_FIELDS = [
    "loop_id",
    "loop_name",
    "kp",
    "ki",
    "kd",
    "kf",
    "sample_ms",
    "integral_min",
    "integral_max",
    "out_min",
    "out_max",
    "reverse",
    "mode",
    "version",
    "fault",
]
SENS_FIELDS = [
    "ms",
    "line0",
    "line1",
    "line2",
    "line3",
    "line4",
    "line5",
    "line6",
    "line7",
    "line_pos",
    "line_lost",
    "yaw",
    "yaw_rate",
    "enc_l",
    "enc_r",
    "v_l",
    "v_r",
    "v_avg",
    "battery",
]
SENS_FIELDS_WITHOUT_BATTERY = SENS_FIELDS[:-1]
TEXT_FIELDS = {"loop_id", "loop_name"}
INTEGER_FIELDS = {
    "seq",
    "ms",
    "sat",
    "anti_windup",
    "mode",
    "enable",
    "sensor_ok",
    "fault",
    "profile_id",
    "reverse",
    "version",
    "line0",
    "line1",
    "line2",
    "line3",
    "line4",
    "line5",
    "line6",
    "line7",
    "line_lost",
    "enc_l",
    "enc_r",
}
FRAME_ALLOWED_VALUES = {
    "pid": {
        "sat": {-1, 0, 1},
        "anti_windup": {0, 1},
        "mode": {0, 1, 2},
        "enable": {0, 1},
        "sensor_ok": {0, 1},
    },
    "cfg": {
        "reverse": {0, 1},
        "mode": {0, 1, 2},
    },
    "pidx": {
        "sat": {-1, 0, 1},
        "anti_windup": {0, 1},
        "mode": {0, 1, 2},
        "enable": {0, 1},
        "sensor_ok": {0, 1},
    },
    "cfgx": {
        "reverse": {0, 1},
        "mode": {0, 1, 2},
    },
    "sens": {
        "line0": {0, 1},
        "line1": {0, 1},
        "line2": {0, 1},
        "line3": {0, 1},
        "line4": {0, 1},
        "line5": {0, 1},
        "line6": {0, 1},
        "line7": {0, 1},
        "line_lost": {0, 1},
    },
}
NON_NEGATIVE_INTEGER_FIELDS = {
    "seq",
    "ms",
    "fault",
    "profile_id",
    "version",
    "line0",
    "line1",
    "line2",
    "line3",
    "line4",
    "line5",
    "line6",
    "line7",
}
SINGLE_LOOP_PROFILE = "single-loop"
MULTI_LOOP_PROFILE = "multi-loop"
LINE_CAR_CASCADE_PROFILE = "line-car-cascade"
SINGLE_LOOP_ID = "single"
LINE_CAR_CASCADE_ORDER = ("speed_l", "speed_r", "yaw_rate", "line_outer")
LINE_CAR_CONSERVATIVE_LOOPS = ("line_outer",)
SUPPORTED_AUTOTUNE_PROFILES = (MULTI_LOOP_PROFILE, LINE_CAR_CASCADE_PROFILE, SINGLE_LOOP_PROFILE)
LOOP_COMMANDS = {
    "SET_PIDX",
    "SET_KFX",
    "SET_TARGETX",
    "SET_OUT_LIMITX",
    "SET_I_LIMITX",
    "RESET_IX",
    "ENABLEX",
    "GET_CFGX",
}
COMMAND_STATUS_TEXTS = {
    "OK",
    "BAD_PREFIX",
    "UNKNOWN",
    "ARG_MISSING",
    "ARG_INVALID",
    "PARAM_RANGE",
    "INTERNAL_ERROR",
    "UNKNOWN_STATUS",
}
SAFE_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
DEFAULT_STREAM_MAX_BUFFER_SIZE = 4096
# ACK 后至少等待多条新遥测再评分，避免单个偶然样本触发错误保留或回滚。
DEFAULT_MIN_POST_ACK_SAMPLES = 3
BINARY_MAGIC = b"\xA5\x5A"
BINARY_VERSION = 1
BINARY_HEADER_SIZE = 11
BINARY_CRC_SIZE = 2
BINARY_TYPE_BY_KIND = {
    "pid": 1,
    "pidx": 2,
    "cfg": 3,
    "cfgx": 4,
}
BINARY_KIND_BY_TYPE = {value: key for key, value in BINARY_TYPE_BY_KIND.items()}
BINARY_PID_PAYLOAD_FORMAT = "<IIfffffffffffffffiiiiiI"
BINARY_PID_PAYLOAD_SIZE = struct.calcsize(BINARY_PID_PAYLOAD_FORMAT)
BINARY_CFG_PAYLOAD_FORMAT = "<ifffffffffiiiI"
BINARY_CFG_PAYLOAD_SIZE = struct.calcsize(BINARY_CFG_PAYLOAD_FORMAT)
BINARY_CFGX_PAYLOAD_FORMAT = "<fffffffffiiiI"
BINARY_CFGX_PAYLOAD_SIZE = struct.calcsize(BINARY_CFGX_PAYLOAD_FORMAT)
BINARY_PID_FIELD_FORMATS = [
    "I",
    "I",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "i",
    "i",
    "i",
    "i",
    "i",
    "I",
]
BINARY_CFG_FIELD_FORMATS = [
    "i",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "i",
    "i",
    "i",
    "I",
]
BINARY_CFGX_FIELD_FORMATS = [
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "f",
    "i",
    "i",
    "i",
    "I",
]


@dataclass
class SerialPortInfo:
    """串口信息，用于稳定输出 list 和 scan 的候选端口。"""

    device: str
    description: str
    hwid: str
    manufacturer: str | None
    vid: int | None
    pid: int | None


@dataclass
class ScanResult:
    """扫描结果，用于描述某个串口在某个波特率下的 PID AI 协议匹配情况。"""

    port: str
    baud: int
    score: int
    frames: int
    prefixes: dict[str, int]
    sample_lines: list[str]
    error: str | None = None


def get_ports() -> list[SerialPortInfo]:
    """
    函数作用：
        枚举当前系统可见串口。

    主要流程：
        调用 pyserial 的 list_ports.comports，并抽取设备名、描述、硬件 ID 和 USB VID/PID。

    参数说明：
        无参数。

    返回值：
        返回 SerialPortInfo 列表；没有串口时返回空列表。
    """
    ports: list[SerialPortInfo] = []
    for port in list_ports.comports():
        ports.append(
            SerialPortInfo(
                device=port.device,
                description=port.description or "",
                hwid=port.hwid or "",
                manufacturer=port.manufacturer,
                vid=port.vid,
                pid=port.pid,
            )
        )
    return ports


def is_bluetooth_port(port: SerialPortInfo) -> bool:
    """
    函数作用：
        判断串口是否是 Windows 蓝牙虚拟串口。

    主要流程：
        合并串口描述、硬件 ID 和厂商字段，检查是否包含常见蓝牙标记。

    参数说明：
        port 为串口信息。

    返回值：
        返回 True 表示这是蓝牙虚拟串口；否则返回 False。
    """
    text = " ".join(
        [
            port.description or "",
            port.hwid or "",
            port.manufacturer or "",
        ]
    ).lower()
    return any(marker in text for marker in BLUETOOTH_MARKERS)


def filter_scan_ports(ports: list[SerialPortInfo], include_bluetooth: bool) -> list[SerialPortInfo]:
    """
    函数作用：
        过滤默认扫描端口列表。

    主要流程：
        默认跳过蓝牙虚拟串口，避免大量不可用 BTHENUM 端口拖慢自动识别。
        用户显式传入 --include-bluetooth 时保留全部串口。

    参数说明：
        ports 为系统串口列表。
        include_bluetooth 表示是否扫描蓝牙虚拟串口。

    返回值：
        返回用于自动扫描的串口列表。
    """
    if include_bluetooth:
        return ports
    return [port for port in ports if not is_bluetooth_port(port)]


def parse_baud_rates(text: str | None) -> list[int]:
    """
    函数作用：
        解析用户传入的波特率列表。

    主要流程：
        1. 未传入时使用默认波特率。
        2. 传入时按逗号拆分并转换为正整数。

    参数说明：
        text 为形如 "115200,9600" 的字符串。

    返回值：
        返回波特率整数列表。
    """
    if not text:
        return list(DEFAULT_BAUD_RATES)

    baud_rates: list[int] = []
    for part in text.split(","):
        value = part.strip()
        if not value:
            continue
        baud = int(value)
        if baud <= 0:
            raise ValueError("baud rate must be positive")
        baud_rates.append(baud)
    if not baud_rates:
        raise ValueError("at least one baud rate is required")
    return baud_rates


def decode_line(raw: bytes) -> str:
    """
    函数作用：
        将串口读取到的字节转换为协议文本行。

    主要流程：
        优先按 UTF-8 解码；遇到异常字节时替换为可见占位，避免读取流程中断。

    参数说明：
        raw 为串口读到的一行字节。

    返回值：
        返回去掉首尾空白后的字符串。
    """
    return raw.decode("utf-8", errors="replace").strip()


def parse_number(field_name: str, value: str) -> int | float:
    """
    函数作用：
        按协议字段类型解析数值，并拒绝不能用于 PID 诊断的非有限浮点值。

    主要流程：
        1. 协议中的整数字段使用 int 解析。
        2. 其他字段使用 float 解析。
        3. 对 float 结果执行 math.isfinite 校验，拒绝 nan、inf 和 -inf。

    参数说明：
        field_name 为协议字段名，用于决定 int/float 类型并生成错误消息。
        value 为字段文本，来自 `{PID}` 或 `{CFG}` 帧的逗号分隔字段。

    返回值：
        返回 int 或 float，类型与协议字段定义一致。
        int(value) 或 float(value) 失败时抛出 ValueError。
        浮点值不是有限数时抛出 ValueError，调用方会把该帧标记为 valid=False。
    """
    if field_name in TEXT_FIELDS:
        raise ValueError(f"{field_name} is not numeric")

    if field_name in INTEGER_FIELDS:
        return int(value)

    parsed = float(value)
    if not math.isfinite(parsed):
        # Python 可以把 "nan" 和 "inf" 解析成 float；这里必须拦截，避免坏帧进入有效遥测状态。
        raise ValueError(f"{field_name} must be finite")
    return parsed


def parse_text_field(field_name: str, value: str) -> str:
    """
    函数作用：
        解析协议中的文本字段，例如 loop_id 和 loop_name。

    主要流程：
        1. 去掉字段两侧空白。
        2. 拒绝空字符串、逗号、换行和控制字符，避免破坏 CSV 协议边界。
        3. 只接受稳定 ASCII 标识符字符，防止 loop 文本进入 UI 时形成 HTML/脚本注入面。

    参数说明：
        field_name 为字段名，用于生成错误消息。
        value 为协议字段原文。

    返回值：
        返回清理后的文本；非法时抛出 ValueError。
    """
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if "," in text or "\r" in text or "\n" in text:
        raise ValueError(f"{field_name} contains invalid separator")
    if not text.isascii() or not SAFE_TEXT_PATTERN.fullmatch(text):
        raise ValueError(f"{field_name} contains unsafe characters")
    return text


def parse_protocol_field(field_name: str, value: str) -> str | int | float:
    """
    函数作用：
        按字段定义解析协议字段，统一处理文本、整数和浮点数。

    主要流程：
        文本字段走 parse_text_field；数值字段走 parse_number 并继承非有限数拒绝逻辑。

    参数说明：
        field_name 为协议字段名。
        value 为字段文本。

    返回值：
        返回 str、int 或 float；解析失败时抛出 ValueError。
    """
    if field_name in TEXT_FIELDS:
        return parse_text_field(field_name, value)
    return parse_number(field_name, value)


def validate_named_numeric_data(kind: str, data: dict[str, str | int | float]) -> str | None:
    """
    函数作用：
        校验已解析数值帧中的枚举字段和非负整数字段。

    主要流程：
        1. 对 `{PID}` / `{CFG}` 中有固定取值范围的字段做白名单校验。
        2. 对序号、时间戳、故障位图和版本号等字段做非负校验。
        3. 返回第一条错误文本；全部通过时返回 None。

    参数说明：
        kind 为帧类型，例如 "pid" 或 "cfg"。
        data 为 parse_number 后得到的字段字典。

    返回值：
        返回 None 表示校验通过；返回字符串表示该帧不应被视为有效 typed frame。
    """
    allowed_by_field = FRAME_ALLOWED_VALUES.get(kind, {})
    for field_name, allowed_values in allowed_by_field.items():
        value = data.get(field_name)
        if value not in allowed_values:
            return f"{field_name} out of range: {value}"

    for field_name in NON_NEGATIVE_INTEGER_FIELDS:
        value = data.get(field_name)
        if isinstance(value, (int, float)) and value < 0:
            return f"{field_name} must be non-negative: {value}"

    return None


def binary_crc16(data: bytes | bytearray | memoryview) -> int:
    """
    函数作用：
        计算 PID AI 二进制协议使用的 CRC-16/CCITT-FALSE。

    主要流程：
        初值 0xFFFF，多项式 0x1021，不做输入/输出反转，也不做最终异或。

    参数说明：
        data 为待校验的字节序列，通常是 header 中 version 开始的字段加 payload。

    返回值：
        返回 0 到 0xFFFF 范围内的 CRC 整数。
    """
    crc = 0xFFFF
    for value in bytes(data):
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _binary_pack_named_fields(fields: list[str], formats: list[str], data: dict[str, Any]) -> bytes:
    """
    函数作用：
        按协议字段顺序把 typed frame 数据打包为二进制 payload。

    主要流程：
        逐字段读取 data，按整数或 float 格式转换，最后用 struct little-endian 打包。

    参数说明：
        fields 为协议字段名顺序。
        formats 为每个字段对应的 struct 格式码，不含端序前缀。
        data 为 parse_frame 得到的 data 字典。

    返回值：
        返回二进制 payload 字节串；字段缺失或类型非法时抛出 ValueError。
    """
    values: list[int | float] = []
    for field_name, field_format in zip(fields, formats):
        value = data[field_name]
        if field_format in ("i", "I"):
            values.append(int(value))
        else:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{field_name} must be finite")
            values.append(number)
    return struct.pack("<" + "".join(formats), *values)


def _binary_unpack_named_fields(kind: str, fields: list[str], formats: list[str], payload: bytes) -> dict[str, Any]:
    """
    函数作用：
        将固定字段二进制 payload 解析为 typed frame data 字典。

    主要流程：
        使用 struct 按 little-endian 解包，再调用既有枚举/非负校验逻辑。

    参数说明：
        kind 为帧类型，用于校验枚举范围。
        fields 为字段名顺序。
        formats 为 struct 格式码顺序。
        payload 为待解析 payload。

    返回值：
        返回已通过基础校验的数据字典；非法时抛出 ValueError。
    """
    expected_length = struct.calcsize("<" + "".join(formats))
    if len(payload) != expected_length:
        raise ValueError(f"expected payload length {expected_length}, got {len(payload)}")

    unpacked = struct.unpack("<" + "".join(formats), payload)
    data: dict[str, Any] = {}
    for field_name, field_format, value in zip(fields, formats, unpacked):
        if field_format not in ("i", "I") and not math.isfinite(float(value)):
            # 二进制 float32 可能直接携带 NaN/Inf；CRC 只能证明传输完整，不能证明数值可用于调参。
            raise ValueError(f"{field_name} must be finite")
        data[field_name] = value
    validation_error = validate_named_numeric_data(kind, data)
    if validation_error is not None:
        raise ValueError(validation_error)
    return data


def _pack_binary_text(value: str) -> bytes:
    """
    函数作用：
        将 loop_id/loop_name 打包为一字节长度前缀加 ASCII 文本。

    主要流程：
        复用 parse_text_field 做安全校验，再要求长度不超过 255 字节。

    参数说明：
        value 为待写入文本字段。

    返回值：
        返回 length + bytes 格式的字段。
    """
    text = parse_text_field("loop_id", value)
    encoded = text.encode("ascii")
    if len(encoded) > 255:
        raise ValueError("binary text field is too long")
    return bytes([len(encoded)]) + encoded


def _unpack_binary_text(payload: bytes, offset: int, field_name: str) -> tuple[str, int]:
    """
    函数作用：
        从二进制 payload 中读取一字节长度前缀文本。

    主要流程：
        校验长度不越界，按 ASCII 解码，再复用 parse_text_field 做安全文本校验。

    参数说明：
        payload 为完整 payload。
        offset 为当前读取位置。
        field_name 为字段名，用于错误消息。

    返回值：
        返回文本值和新的 offset。
    """
    if offset >= len(payload):
        raise ValueError(f"{field_name} length is missing")
    length = payload[offset]
    start = offset + 1
    end = start + length
    if end > len(payload):
        raise ValueError(f"{field_name} exceeds payload length")
    try:
        text = payload[start:end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field_name} must be ASCII") from exc
    return parse_text_field(field_name, text), end


def build_binary_frame(kind: str, data: dict[str, Any], transport_seq: int = 0) -> bytes:
    """
    函数作用：
        构造一条 PID AI 二进制协议帧，供测试、回放和未来高频发送复用。

    主要流程：
        1. 按 kind 选择固定 payload schema。
        2. 写入 magic、版本、类型、flags、传输层序号和 payload 长度。
        3. CRC 覆盖 version 起始的 header 字段和 payload，最后以 little-endian 追加。

    参数说明：
        kind 为 pid、pidx、cfg 或 cfgx。
        data 为 typed frame data 字典。
        transport_seq 为二进制传输层帧序号。

    返回值：
        返回完整二进制帧字节串。
    """
    normalized_kind = kind.lower()
    if normalized_kind == "pid":
        payload = _binary_pack_named_fields(PID_FIELDS, BINARY_PID_FIELD_FORMATS, data)
    elif normalized_kind == "pidx":
        payload = (
            _pack_binary_text(str(data["loop_id"])) +
            _pack_binary_text(str(data.get("loop_name") or data["loop_id"])) +
            _binary_pack_named_fields(PID_FIELDS, BINARY_PID_FIELD_FORMATS, data)
        )
    elif normalized_kind == "cfg":
        payload = _binary_pack_named_fields(CFG_FIELDS, BINARY_CFG_FIELD_FORMATS, data)
    elif normalized_kind == "cfgx":
        payload = (
            _pack_binary_text(str(data["loop_id"])) +
            _pack_binary_text(str(data.get("loop_name") or data["loop_id"])) +
            _binary_pack_named_fields(CFGX_FIELDS[2:], BINARY_CFGX_FIELD_FORMATS, data)
        )
    else:
        raise ValueError(f"unsupported binary frame kind: {kind}")

    header = struct.pack(
        "<2sBBB I H",
        BINARY_MAGIC,
        BINARY_VERSION,
        BINARY_TYPE_BY_KIND[normalized_kind],
        0,
        int(transport_seq) & 0xFFFFFFFF,
        len(payload),
    )
    crc = binary_crc16(header[2:] + payload)
    return header + payload + struct.pack("<H", crc)


def parse_binary_frame(frame: bytes | bytearray | memoryview) -> dict:
    """
    函数作用：
        将一条完整 PID AI 二进制帧解析为与文本 parse_frame 一致的 typed frame。

    主要流程：
        1. 校验 magic、版本、完整长度和 CRC。
        2. 根据 type 解析固定 payload。
        3. 输出 kind、valid、data、transport_seq 和 raw_bytes。

    参数说明：
        frame 为完整二进制帧，不包含额外前后缀字节。

    返回值：
        返回结构化帧字典；校验失败时 valid=False 并写入 error。
    """
    raw = bytes(frame)
    result = {
        "kind": "binary",
        "valid": False,
        "raw": f"<binary:{len(raw)} bytes>",
        "raw_hex": raw.hex(),
        "raw_length": len(raw),
        "data": {},
        "error": None,
    }
    if len(raw) < BINARY_HEADER_SIZE + BINARY_CRC_SIZE:
        result["error"] = "binary frame too short"
        return result
    if raw[:2] != BINARY_MAGIC:
        result["error"] = "binary magic mismatch"
        return result

    version, frame_type, flags, transport_seq, payload_length = struct.unpack("<BBB I H", raw[2:BINARY_HEADER_SIZE])
    if version != BINARY_VERSION:
        result["error"] = f"unsupported binary version: {version}"
        return result
    expected_length = BINARY_HEADER_SIZE + payload_length + BINARY_CRC_SIZE
    if len(raw) != expected_length:
        result["error"] = f"binary length mismatch: expected {expected_length}, got {len(raw)}"
        return result

    payload = raw[BINARY_HEADER_SIZE : BINARY_HEADER_SIZE + payload_length]
    expected_crc = binary_crc16(raw[2 : BINARY_HEADER_SIZE + payload_length])
    actual_crc = struct.unpack("<H", raw[BINARY_HEADER_SIZE + payload_length : expected_length])[0]
    if expected_crc != actual_crc:
        result["error"] = f"CRC mismatch: expected 0x{expected_crc:04X}, got 0x{actual_crc:04X}"
        return result

    kind = BINARY_KIND_BY_TYPE.get(frame_type)
    if kind is None:
        result["error"] = f"unsupported binary frame type: {frame_type}"
        return result

    try:
        if kind == "pid":
            data = _binary_unpack_named_fields("pid", PID_FIELDS, BINARY_PID_FIELD_FORMATS, payload)
        elif kind == "pidx":
            loop_id, offset = _unpack_binary_text(payload, 0, "loop_id")
            loop_name, offset = _unpack_binary_text(payload, offset, "loop_name")
            data = _binary_unpack_named_fields("pidx", PID_FIELDS, BINARY_PID_FIELD_FORMATS, payload[offset:])
            data = {"loop_id": loop_id, "loop_name": loop_name, **data}
        elif kind == "cfg":
            data = _binary_unpack_named_fields("cfg", CFG_FIELDS, BINARY_CFG_FIELD_FORMATS, payload)
        else:
            loop_id, offset = _unpack_binary_text(payload, 0, "loop_id")
            loop_name, offset = _unpack_binary_text(payload, offset, "loop_name")
            data = _binary_unpack_named_fields("cfgx", CFGX_FIELDS[2:], BINARY_CFGX_FIELD_FORMATS, payload[offset:])
            data = {"loop_id": loop_id, "loop_name": loop_name, **data}
            validation_error = validate_named_numeric_data("cfgx", data)
            if validation_error is not None:
                raise ValueError(validation_error)
    except (KeyError, struct.error, ValueError) as exc:
        result["kind"] = kind
        result["transport_seq"] = transport_seq
        result["flags"] = flags
        result["error"] = str(exc)
        return result

    result.update(
        {
            "kind": kind,
            "valid": True,
            "data": data,
            "transport_seq": transport_seq,
            "flags": flags,
            "error": None,
        }
    )
    return result


class BinaryFrameDecoder:
    """
    PID AI 二进制流解码器。

    类作用：
        从串口字节流中增量提取完整二进制帧；能跳过帧头前噪声，也能等待分块输入补齐。

    线程模型：
        类本身不加锁，调用方应在单个读取线程中使用或自行同步。
    """

    def __init__(self) -> None:
        """初始化内部缓冲区。"""
        self._buffer = bytearray()

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[dict]:
        """
        函数作用：
            向解码器追加一段串口字节并返回已解析出的完整 typed frame。

        主要流程：
            1. 追加新字节到内部缓冲。
            2. 查找 magic，丢弃 magic 前噪声。
            3. 长度不足时等待下一次 feed。
            4. 长度足够时取出完整帧并调用 parse_binary_frame。

        参数说明：
            chunk 为新收到的字节块，允许为空。

        返回值：
            返回本次成功切出的帧列表；坏 CRC 帧也会返回 valid=False 结果，便于统计错误。
        """
        if chunk:
            self._buffer.extend(bytes(chunk))

        frames: list[dict] = []
        while True:
            magic_index = self._buffer.find(BINARY_MAGIC)
            if magic_index < 0:
                # 保留最后一个可能成为 magic 首字节的 0xA5，避免跨 chunk 帧头被误删。
                if self._buffer.endswith(BINARY_MAGIC[:1]):
                    del self._buffer[:-1]
                else:
                    self._buffer.clear()
                break
            if magic_index > 0:
                del self._buffer[:magic_index]
            if len(self._buffer) < BINARY_HEADER_SIZE:
                break

            payload_length = struct.unpack("<H", self._buffer[9:11])[0]
            frame_length = BINARY_HEADER_SIZE + payload_length + BINARY_CRC_SIZE
            if len(self._buffer) < frame_length:
                break

            raw_frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(parse_binary_frame(raw_frame))

        return frames


class ProtocolStreamDecoder:
    """
    PID AI 混合协议流解码器。

    类作用：
        同一串口流中同时支持旧文本行协议和新增二进制协议。文本帧以换行结束，
        二进制帧以 magic 和长度字段定位，不要求换行。

    使用说明：
        读取线程应调用 serial.read(...) 获取字节块，再交给 feed；不要对二进制流使用
        readline，因为二进制 payload 内可能不包含换行且会阻塞到超时。
    """

    def __init__(self, max_buffer_size: int = DEFAULT_STREAM_MAX_BUFFER_SIZE) -> None:
        """
        函数作用：
            初始化内部字节缓冲和最大缓冲长度。

        参数说明：
            max_buffer_size 为无完整帧时最多保留的字节数；超限会丢弃旧文本噪声，
            避免异常串口流长期没有换行或二进制 magic 时耗尽内存。

        返回值：
            构造函数无显式返回值。
        """
        self._buffer = bytearray()
        self.max_buffer_size = max(1, int(max_buffer_size))

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[dict]:
        """
        函数作用：
            追加串口字节并返回已解析出的文本或二进制 typed frame。

        主要流程：
            1. 如果缓冲以二进制 magic 开头，按二进制长度切帧。
            2. 否则等待文本换行；若 magic 出现在换行前，则先解析 magic 前文本片段。
            3. 文本片段使用 parse_frame，二进制片段使用 parse_binary_frame。

        参数说明：
            chunk 为串口读取到的新字节块，允许为空。

        返回值：
            返回本次解析出的 frame 列表，每个 frame 增加 transport 字段。
        """
        if chunk:
            self._buffer.extend(bytes(chunk))
            self._trim_unframed_noise()

        frames: list[dict] = []
        while self._buffer:
            if self._buffer.startswith(BINARY_MAGIC):
                if len(self._buffer) < BINARY_HEADER_SIZE:
                    break
                payload_length = struct.unpack("<H", self._buffer[9:11])[0]
                frame_length = BINARY_HEADER_SIZE + payload_length + BINARY_CRC_SIZE
                if len(self._buffer) < frame_length:
                    break
                raw_frame = bytes(self._buffer[:frame_length])
                del self._buffer[:frame_length]
                frame = parse_binary_frame(raw_frame)
                frame["transport"] = "binary"
                frames.append(frame)
                continue

            magic_index = self._buffer.find(BINARY_MAGIC)
            newline_index = self._find_text_newline()
            if newline_index is None:
                if magic_index > 0:
                    text_bytes = bytes(self._buffer[:magic_index])
                    del self._buffer[:magic_index]
                    self._append_text_frame(frames, text_bytes)
                    continue
                break

            if magic_index >= 0 and magic_index < newline_index:
                text_bytes = bytes(self._buffer[:magic_index])
                del self._buffer[:magic_index]
                self._append_text_frame(frames, text_bytes)
                continue

            line_bytes = bytes(self._buffer[:newline_index])
            line_end = newline_index + 1
            if line_end < len(self._buffer) and self._buffer[newline_index] == 0x0D and self._buffer[line_end] == 0x0A:
                line_end += 1
            del self._buffer[:line_end]
            self._append_text_frame(frames, line_bytes)

        return frames

    def _trim_unframed_noise(self) -> None:
        """
        函数作用：
            控制混合协议解码器在无完整帧输入时的缓冲上限。

        主要流程：
            如果缓冲超过 max_buffer_size，优先保留末尾可能组成二进制 magic 的字节和最近文本片段；
            文本协议依赖换行成帧，过长未终止片段只能视为噪声丢弃。

        参数说明：
            无参数，读取并修改 self._buffer。

        返回值：
            无返回值。
        """
        if len(self._buffer) <= self.max_buffer_size:
            return
        del self._buffer[: len(self._buffer) - self.max_buffer_size]

    def _find_text_newline(self) -> int | None:
        """
        函数作用：
            查找当前缓冲中的文本行结束位置。

        主要流程：
            同时支持 LF 和 CR；返回的是换行符起始下标，不包含换行字节。

        参数说明：
            无参数，读取内部缓冲。

        返回值：
            找到时返回下标；没有完整文本行时返回 None。
        """
        candidates = [index for index in (self._buffer.find(b"\n"), self._buffer.find(b"\r")) if index >= 0]
        if not candidates:
            return None
        return min(candidates)

    def _append_text_frame(self, frames: list[dict], text_bytes: bytes) -> None:
        """
        函数作用：
            将一段文本字节解析为协议 frame 并追加到输出列表。

        主要流程：
            空白文本直接忽略；非空文本按 UTF-8 容错解码，再调用 parse_frame。

        参数说明：
            frames 为本次 feed 的输出列表。
            text_bytes 为不包含换行符的文本字节。

        返回值：
            无返回值，直接修改 frames。
        """
        line = decode_line(text_bytes)
        if not line:
            return
        frame = parse_frame(line)
        frame["transport"] = "text"
        frames.append(frame)


def parse_named_numeric_frame(kind: str, prefix: str, fields: list[str], line: str) -> dict:
    """
    函数作用：
        解析字段顺序固定的数值帧。

    主要流程：
        1. 去掉前缀后按英文逗号拆分。
        2. 校验字段数量必须与协议文档一致。
        3. 按字段名转换 int 或 float。

    参数说明：
        kind 为输出帧类型。
        prefix 为协议前缀。
        fields 为字段名列表。
        line 为完整协议行。

    返回值：
        返回结构化帧字典；解析失败时 valid 为 False 并携带 error。
    """
    payload = line[len(prefix) :]
    parts = payload.split(",") if payload else []
    frame = {"kind": kind, "valid": False, "raw": line, "data": {}, "error": None}
    if len(parts) != len(fields):
        frame["error"] = f"expected {len(fields)} fields, got {len(parts)}"
        return frame

    try:
        data = {
            field_name: parse_protocol_field(field_name, field_value.strip())
            for field_name, field_value in zip(fields, parts)
        }
    except ValueError as exc:
        frame["error"] = str(exc)
        return frame

    validation_error = validate_named_numeric_data(kind, data)
    frame["data"] = data
    if validation_error is not None:
        frame["error"] = validation_error
        return frame

    frame["valid"] = True
    return frame


def parse_sens_frame(line: str) -> dict:
    """
    函数作用：
        解析小车传感器 `{SENS}` 帧，并兼容没有电池电压字段的板端工程。

    主要流程：
        1. 先按逗号拆分 `{SENS}` payload。
        2. 字段数为 19 时按完整协议解析，包含 battery。
        3. 字段数为 18 时按无电池扩展解析，并把 battery 置为 None，避免缺省电池值被误判为 0V。
        4. 复用既有数值和枚举校验，确保 line_lost 等安全字段仍严格校验。

    参数说明：
        line 为完整 `{SENS}` 文本行，不要求包含换行。

    返回值：
        返回 typed frame 字典；非法字段数或非法数值时 valid=False。
    """
    payload = line[len("{SENS}") :]
    parts = payload.split(",") if payload else []
    fields = SENS_FIELDS
    if len(parts) == len(SENS_FIELDS_WITHOUT_BATTERY):
        fields = SENS_FIELDS_WITHOUT_BATTERY
    frame = parse_named_numeric_frame("sens", "{SENS}", fields, line)
    if frame.get("valid") and "battery" not in frame["data"]:
        # battery 是应用层健康信息，很多板端没有采样通道；缺失时显式保留为 None。
        frame["data"]["battery"] = None
    return frame


def parse_ack_frame(line: str) -> dict:
    """
    函数作用：
        按 ACK 协议精确解析命令成功响应。

    主要流程：
        1. 按逗号拆分 ACK payload。
        2. 普通命令只接受 `{ACK}command,detail` 两个字段。
        3. 分环命令额外接受 `{ACK}command,loop_id,detail` 三个字段，并校验 loop_id 安全。

    参数说明：
        line 为完整 ACK 协议行。

    返回值：
        返回结构化 ACK 字典；字段数量、loop_id 或 detail 非法时 valid=False。
    """
    parts = [part.strip() for part in line[len("{ACK}") :].split(",")]
    command = parts[0] if len(parts) > 0 else ""
    command_upper = command.upper()
    frame = {
        "kind": "ack",
        "valid": False,
        "raw": line,
        "command": command,
        "loop_id": None,
        "detail": "",
        "error": None,
    }

    if not command:
        frame["error"] = "command is required"
        return frame

    if command_upper in LOOP_COMMANDS:
        if len(parts) == 2:
            frame["detail"] = parts[1]
        elif len(parts) == 3:
            try:
                frame["loop_id"] = parse_text_field("loop_id", parts[1])
            except ValueError as exc:
                frame["error"] = str(exc)
                return frame
            frame["detail"] = parts[2]
        else:
            frame["error"] = f"unexpected field count: expected 2 or 3, got {len(parts)}"
            return frame
    else:
        if len(parts) != 2:
            frame["error"] = f"unexpected field count: expected 2, got {len(parts)}"
            return frame
        frame["detail"] = parts[1]

    if not frame["detail"]:
        frame["error"] = "detail is required"
        return frame

    frame["valid"] = True
    return frame


def parse_err_frame(line: str) -> dict:
    """
    函数作用：
        按 ERR 协议精确解析命令失败响应。

    主要流程：
        1. 普通命令只接受 `{ERR}command,status,detail` 三个字段。
        2. 分环命令额外接受 `{ERR}command,loop_id,status,detail` 四个字段。
        3. 校验 status 必须来自 C API 稳定状态文本，防止坏帧被当成有效拒绝结果。

    参数说明：
        line 为完整 ERR 协议行。

    返回值：
        返回结构化 ERR 字典；字段数量、loop_id、status 或 detail 非法时 valid=False。
    """
    parts = [part.strip() for part in line[len("{ERR}") :].split(",")]
    command = parts[0] if len(parts) > 0 else ""
    command_upper = command.upper()
    frame = {
        "kind": "err",
        "valid": False,
        "raw": line,
        "command": command,
        "loop_id": None,
        "status": "",
        "detail": "",
        "error": None,
    }

    if not command:
        frame["error"] = "command is required"
        return frame

    if command_upper in LOOP_COMMANDS:
        if len(parts) == 3:
            frame["status"] = parts[1]
            frame["detail"] = parts[2]
        elif len(parts) == 4:
            try:
                frame["loop_id"] = parse_text_field("loop_id", parts[1])
            except ValueError as exc:
                frame["error"] = str(exc)
                return frame
            frame["status"] = parts[2]
            frame["detail"] = parts[3]
        else:
            frame["error"] = f"unexpected field count: expected 3 or 4, got {len(parts)}"
            return frame
    else:
        if len(parts) != 3:
            frame["error"] = f"unexpected field count: expected 3, got {len(parts)}"
            return frame
        frame["status"] = parts[1]
        frame["detail"] = parts[2]

    if not frame["status"]:
        frame["error"] = "status is required"
        return frame
    if frame["status"].upper() not in COMMAND_STATUS_TEXTS:
        frame["error"] = f"unknown status: {frame['status']}"
        return frame
    if not frame["detail"]:
        frame["error"] = "detail is required"
        return frame

    frame["valid"] = True
    return frame


def parse_frame(line: str) -> dict:
    """
    函数作用：
        将 PID AI 文本协议行解析为结构化字典。

    主要流程：
        1. 根据固定前缀识别帧类型。
        2. 对 {PID}/{CFG} 校验字段数量并解析数值。
        3. 对 {ACK}/{ERR} 解析命令与状态文本。
        4. 其他支持前缀保留为 raw 字段，方便后续扩展。

    参数说明：
        line 为一行串口文本，不要求包含 CRLF。

    返回值：
        返回包含 kind、raw、valid 等字段的字典。
    """
    if line.startswith("{PID}"):
        return parse_named_numeric_frame("pid", "{PID}", PID_FIELDS, line)
    if line.startswith("{PIDX}"):
        return parse_named_numeric_frame("pidx", "{PIDX}", PIDX_FIELDS, line)
    if line.startswith("{CFG}"):
        return parse_named_numeric_frame("cfg", "{CFG}", CFG_FIELDS, line)
    if line.startswith("{CFGX}"):
        return parse_named_numeric_frame("cfgx", "{CFGX}", CFGX_FIELDS, line)
    if line.startswith("{SENS}"):
        return parse_sens_frame(line)
    if line.startswith("{ACK}"):
        return parse_ack_frame(line)
    if line.startswith("{ERR}"):
        return parse_err_frame(line)
    for prefix in ("{STAT}", "{EVT}"):
        if line.startswith(prefix):
            return {"kind": prefix.strip("{}").lower(), "valid": True, "raw": line}
    return {"kind": "raw", "valid": False, "raw": line}


def line_prefix(line: str) -> str | None:
    """
    函数作用：
        判断一行文本是否以 PID AI 协议前缀开头。

    主要流程：
        遍历已知前缀并返回第一个匹配项。

    参数说明：
        line 为串口文本行。

    返回值：
        返回匹配到的前缀；未匹配时返回 None。
    """
    for prefix in PROTOCOL_PREFIXES:
        if line.startswith(prefix):
            return prefix
    return None


def frame_scan_prefix(frame: dict[str, Any]) -> str | None:
    """
    函数作用：
        为 scan 子命令把已解析 frame 映射成可统计的协议类别。

    主要流程：
        文本帧保留原有 `{PID}` 等前缀；二进制帧按 kind 映射为 `{BIN:PID}` 形式，
        让纯二进制输出的板端也能被自动识别和打分。

    参数说明：
        frame 为 ProtocolStreamDecoder 或 parse_frame 输出的结构化帧。

    返回值：
        返回扫描统计用前缀；未知或无效帧返回 None。
    """
    if not frame.get("valid"):
        return None
    if frame.get("transport") == "binary":
        kind = str(frame.get("kind", "")).upper()
        if kind in ("PID", "PIDX", "CFG", "CFGX"):
            return f"{{BIN:{kind}}}"
        return "{BIN}"

    raw = str(frame.get("raw", ""))
    return line_prefix(raw)


def score_scan_frame(frame: dict[str, Any], prefix: str) -> int:
    """
    函数作用：
        为自动扫描计算单帧协议匹配分数。

    主要流程：
        PID/CFG 主帧权重高，其他辅助帧权重低；typed frame 已通过 parser/CRC 时再加分。

    参数说明：
        frame 为已解析协议帧。
        prefix 为 frame_scan_prefix 返回的统计类别。

    返回值：
        返回该帧贡献的扫描分数。
    """
    score = 10 if prefix in ("{PID}", "{CFG}", "{BIN:PID}", "{BIN:CFG}") else 4
    if frame.get("valid"):
        score += 5
    return score


def open_serial(port: str, baud: int, timeout: float) -> serial.Serial:
    """
    函数作用：
        打开串口并设置通用 8N1 参数。

    主要流程：
        通过 pyserial 打开端口，设置读取超时，并清理旧输入缓存。

    参数说明：
        port 为 COM 口名称。
        baud 为波特率。
        timeout 为读取超时时间，单位秒。

    返回值：
        返回已打开的 serial.Serial 对象。
    """
    ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)
    ser.reset_input_buffer()
    return ser


def scan_port(port: str, baud: int, sample_seconds: float, max_lines: int) -> ScanResult:
    """
    函数作用：
        在指定串口和波特率上探测 PID AI 协议输出。

    主要流程：
        1. 打开串口并读取限定时间内的若干行。
        2. 统计已知协议前缀出现次数。
        3. 按前缀权重计算候选分数。

    参数说明：
        port 为串口名。
        baud 为波特率。
        sample_seconds 为采样时间。
        max_lines 为最多保留和读取的行数。

    返回值：
        返回 ScanResult。
    """
    prefixes: dict[str, int] = {}
    sample_lines: list[str] = []
    score = 0
    frames = 0
    deadline = time.monotonic() + sample_seconds

    try:
        with open_serial(port, baud, timeout=0.2) as ser:
            decoder = ProtocolStreamDecoder()
            while time.monotonic() < deadline and len(sample_lines) < max_lines:
                raw = ser.read(256)
                if not raw:
                    continue
                for parsed in decoder.feed(raw):
                    prefix = frame_scan_prefix(parsed)
                    if prefix is None:
                        continue
                    raw_sample = str(parsed.get("raw") or f"<binary:{parsed.get('kind')}>")
                    sample_lines.append(raw_sample)
                    prefixes[prefix] = prefixes.get(prefix, 0) + 1
                    frames += 1
                    score += score_scan_frame(parsed, prefix)
                    if len(sample_lines) >= max_lines:
                        break
    except (OSError, serial.SerialException) as exc:
        return ScanResult(port, baud, 0, 0, {}, [], str(exc))

    return ScanResult(port, baud, score, frames, prefixes, sample_lines[:5])


def scan_ports(ports: Iterable[str], baud_rates: Iterable[int], sample_seconds: float, max_lines: int) -> list[ScanResult]:
    """
    函数作用：
        扫描多个串口和多个波特率组合。

    主要流程：
        使用线程池并行执行各组合的 scan_port，再按得分降序排序；
        串口 IO 大部分时间在等待，因此并行能显著缩短多端口探测时间。

    参数说明：
        ports 为串口名集合。
        baud_rates 为波特率集合。
        sample_seconds 为每组组合的采样秒数。
        max_lines 为每组最多读取行数。

    返回值：
        返回排序后的扫描结果列表。
    """
    tasks = [(port, baud) for port in ports for baud in baud_rates]
    if not tasks:
        return []

    max_workers = min(8, len(tasks))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(
            executor.map(
                lambda args: scan_port(args[0], args[1], sample_seconds, max_lines),
                tasks,
            )
        )
    return sorted(results, key=lambda item: item.score, reverse=True)


def find_best_port(
    baud_rates: list[int],
    sample_seconds: float,
    max_lines: int,
    include_bluetooth: bool,
) -> ScanResult | None:
    """
    函数作用：
        自动寻找最像 PID AI 板端的串口。

    主要流程：
        枚举串口后调用 scan_ports，返回分数大于 0 的最佳结果。

    参数说明：
        baud_rates 为候选波特率。
        sample_seconds 为每个组合扫描时长。
        max_lines 为最多读取行数。

    返回值：
        找到候选时返回 ScanResult；否则返回 None。
    """
    ports = [port.device for port in filter_scan_ports(get_ports(), include_bluetooth)]
    if not ports:
        return None
    results = scan_ports(ports, baud_rates, sample_seconds, max_lines)
    if not results or results[0].score <= 0:
        return None
    return results[0]


def print_record(record: dict, as_json: bool) -> None:
    """
    函数作用：
        按用户选择输出普通文本或 JSONL。

    主要流程：
        JSONL 模式使用 json.dumps；普通模式优先输出 raw 行。

    参数说明：
        record 为待输出记录。
        as_json 表示是否输出 JSONL。

    返回值：
        无返回值。
    """
    if as_json:
        print(json.dumps(record, ensure_ascii=False), flush=True)
    else:
        print(record.get("raw", record), flush=True)


def cmd_list(args: argparse.Namespace) -> int:
    """
    函数作用：
        执行 list 子命令，输出可见串口。

    参数说明：
        args 为 argparse 解析后的参数。

    返回值：
        成功返回 0；没有串口时返回 1。
    """
    ports = get_ports()
    if args.json:
        print(json.dumps([asdict(port) for port in ports], ensure_ascii=False, indent=2))
    else:
        if not ports:
            print("No serial ports found.")
            return 1
        for port in ports:
            print(f"{port.device}\t{port.description}\t{port.hwid}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """
    函数作用：
        执行 scan 子命令，自动探测 PID AI 协议串口。

    参数说明：
        args 为 argparse 解析后的参数。

    返回值：
        找到匹配端口返回 0；没有匹配端口返回 1。
    """
    baud_rates = parse_baud_rates(args.baud_rates)
    all_ports = get_ports()
    scan_port_infos = filter_scan_ports(all_ports, args.include_bluetooth)
    ports = [args.port] if args.port else [port.device for port in scan_port_infos]
    if not ports:
        if all_ports and not args.include_bluetooth:
            print(
                "No non-Bluetooth serial ports found. Use --include-bluetooth to scan Bluetooth virtual ports.",
                file=sys.stderr,
            )
        else:
            print("No serial ports found.", file=sys.stderr)
        return 1

    results = scan_ports(ports, baud_rates, args.sample_seconds, args.max_lines)
    visible = [result for result in results if args.show_all or result.score > 0 or result.error]
    if args.json:
        print(json.dumps([asdict(result) for result in visible], ensure_ascii=False, indent=2))
    else:
        if not visible:
            print("No PID AI protocol frames detected.")
        for result in visible:
            status = "MATCH" if result.score > 0 else "NO_MATCH"
            if result.error:
                status = "ERROR"
            print(
                f"{status}\tport={result.port}\tbaud={result.baud}\t"
                f"score={result.score}\tframes={result.frames}\tprefixes={result.prefixes}"
            )
            for line in result.sample_lines:
                print(f"  {line}")
            if result.error:
                print(f"  error: {result.error}")

    best = results[0] if results else None
    return 0 if best and best.score > 0 else 1


def resolve_port_and_baud(args: argparse.Namespace) -> tuple[str, int] | None:
    """
    函数作用：
        根据 --port/--baud 或 --auto 得到最终串口参数。

    主要流程：
        1. 用户显式指定 port 时直接使用。
        2. --auto 时扫描并选择最佳候选。
        3. 找不到时返回 None。

    参数说明：
        args 为 argparse 命名空间。

    返回值：
        返回 (port, baud)；失败返回 None。
    """
    if args.port:
        return args.port, args.baud

    if not args.auto:
        print("Specify --port COMx or use --auto.", file=sys.stderr)
        return None

    best = find_best_port(
        parse_baud_rates(args.baud_rates),
        args.sample_seconds,
        args.max_lines,
        args.include_bluetooth,
    )
    if best is None:
        print("No PID AI board port detected.", file=sys.stderr)
        return None

    print(f"Auto-detected PID AI board: {best.port} @ {best.baud}", file=sys.stderr)
    return best.port, best.baud


def cmd_read(args: argparse.Namespace) -> int:
    """
    函数作用：
        执行 read 子命令，读取并可解析 PID AI 协议帧。

    参数说明：
        args 为 argparse 解析后的参数。

    返回值：
        正常结束返回 0；串口失败返回 1；用户中断返回 130。
    """
    resolved = resolve_port_and_baud(args)
    if resolved is None:
        return 1
    port, baud = resolved
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    count = 0

    try:
        with open_serial(port, baud, timeout=0.5) as ser:
            decoder = ProtocolStreamDecoder()
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if args.count > 0 and count >= args.count:
                    break
                raw = ser.read(256)
                if not raw:
                    continue
                for parsed in decoder.feed(raw):
                    if args.known_only and parsed["kind"] == "raw":
                        continue
                    record = {"port": port, "baud": baud, "time": time.time(), **parsed}
                    print_record(record, args.jsonl)
                    count += 1
                    if args.count > 0 and count >= args.count:
                        break
    except KeyboardInterrupt:
        return 130
    except (OSError, serial.SerialException) as exc:
        print(f"Serial read failed: {exc}", file=sys.stderr)
        return 1
    return 0


def normalize_command(command: str) -> str:
    """
    函数作用：
        规范化待发送的 PID AI 命令文本。

    主要流程：
        检查命令必须以 {CMD} 开头，拒绝嵌入换行，最后保证以 CRLF 结尾。

    参数说明：
        command 为用户显式提供的命令。

    返回值：
        返回可写入串口的命令文本。
    """
    text = command.strip()
    if not text.startswith("{CMD}"):
        raise ValueError("command must start with {CMD}")
    if "\r" in text or "\n" in text:
        raise ValueError("command must be a single line")
    return text + "\r\n"


def format_command_float(value: float) -> str:
    """
    函数作用：
        将待下发参数格式化为自动调参约定的三位小数字符串。

    主要流程：
        1. 转成 float，确保可以做数值校验。
        2. 使用 math.isfinite 拒绝 nan/inf。
        3. 返回固定三位小数，保证命令文本稳定可比对。

    参数说明：
        value 为待写入 {CMD} 的数值。

    返回值：
        返回形如 "1.235" 的字符串；非法数值抛出 ValueError。
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("command value must be finite")
    return f"{number:.3f}"


def validate_loop_id(loop_id: str) -> str:
    """
    函数作用：
        校验分环命令中的 loop_id。

    主要流程：
        去除首尾空白后拒绝空值、逗号和换行，避免生成不可解析的 CSV 命令。

    参数说明：
        loop_id 为环路标识，例如 speed_l、yaw_rate。

    返回值：
        返回清理后的 loop_id；非法时抛出 ValueError。
    """
    return parse_text_field("loop_id", loop_id)


def parse_loop_id_list(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """
    函数作用：
        把 CLI、dashboard 或测试传入的 loop_id 列表统一解析成安全元组。

    主要流程：
        1. None 或空字符串表示没有显式配置。
        2. 字符串按英文逗号拆分；其他可迭代对象逐项读取。
        3. 每个 loop_id 复用 validate_loop_id，确保后续命令构造和 ACK 匹配只使用安全标识符。
        4. 保留首次出现顺序并去重，避免重复 loop 造成状态机重复调同一环。

    参数说明：
        value 为 None、逗号分隔字符串或字符串可迭代对象。

    返回值：
        返回去重后的 loop_id 元组；非法标识符会抛出 ValueError。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = [str(item) for item in value]

    parsed: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        item = str(raw_item).strip()
        if not item:
            continue
        loop_id = validate_loop_id(item)
        if loop_id in seen:
            continue
        seen.add(loop_id)
        parsed.append(loop_id)
    return tuple(parsed)


def build_set_pidx_command(loop_id: str, kp: float, ki: float, kd: float) -> str:
    """
    函数作用：
        构造分环 PID 参数下发命令。

    主要流程：
        校验 loop_id 和三个有限数值，并按三位小数输出 `{CMD}SET_PIDX`。

    参数说明：
        loop_id 为目标环路。
        kp、ki、kd 为待下发 PID 参数。

    返回值：
        返回完整 `{CMD}SET_PIDX,loop_id,kp,ki,kd` 文本，不包含换行。
    """
    loop = validate_loop_id(loop_id)
    return (
        f"{{CMD}}SET_PIDX,{loop},"
        f"{format_command_float(kp)},{format_command_float(ki)},{format_command_float(kd)}"
    )


def build_set_pid_command(kp: float, ki: float, kd: float) -> str:
    """
    函数作用：
        构造旧单环 PID 参数下发命令。

    主要流程：
        校验三个有限数值，并按自动调参约定的三位小数输出 `{CMD}SET_PID`。

    参数说明：
        kp、ki、kd 为待下发到单环 PID 控制器的参数，必须是有限数。

    返回值：
        返回完整 `{CMD}SET_PID,kp,ki,kd` 文本，不包含换行。
    """
    return f"{{CMD}}SET_PID,{format_command_float(kp)},{format_command_float(ki)},{format_command_float(kd)}"


def build_set_kfx_command(loop_id: str, kf: float) -> str:
    """
    函数作用：
        构造分环前馈系数下发命令。

    主要流程：
        校验 loop_id 和有限数值，并按三位小数输出 `{CMD}SET_KFX`。

    参数说明：
        loop_id 为目标环路。
        kf 为前馈系数。

    返回值：
        返回完整 `{CMD}SET_KFX,loop_id,kf` 文本，不包含换行。
    """
    loop = validate_loop_id(loop_id)
    return f"{{CMD}}SET_KFX,{loop},{format_command_float(kf)}"


def build_set_targetx_command(loop_id: str, target: float) -> str:
    """
    函数作用：
        构造分环目标值下发命令。

    主要流程：
        校验 loop_id 和有限 target，并按三位小数输出 `{CMD}SET_TARGETX`。

    参数说明：
        loop_id 为目标环路。
        target 为目标值。

    返回值：
        返回完整 `{CMD}SET_TARGETX,loop_id,target` 文本，不包含换行。
    """
    loop = validate_loop_id(loop_id)
    return f"{{CMD}}SET_TARGETX,{loop},{format_command_float(target)}"


def build_reset_ix_command(loop_id: str) -> str:
    """
    函数作用：
        构造分环积分清空命令。

    主要流程：
        校验 loop_id 后输出 `{CMD}RESET_IX`。

    参数说明：
        loop_id 为目标环路。

    返回值：
        返回完整 `{CMD}RESET_IX,loop_id` 文本，不包含换行。
    """
    return f"{{CMD}}RESET_IX,{validate_loop_id(loop_id)}"


def extract_command_metadata(command: str) -> dict[str, Any]:
    """
    函数作用：
        从完整 {CMD} 命令提取命令名和可选 loop_id。

    主要流程：
        1. 复用 normalize_command 校验 `{CMD}` 前缀。
        2. 拆分命令 payload，提取大写 command_name。
        3. 对 `*X` 分环命令提取第一个参数作为 loop_id。

    参数说明：
        command 为完整命令文本，可带或不带换行。

    返回值：
        返回包含 command、command_name、loop_id 的字典；格式错误时抛出 ValueError。
    """
    normalized = normalize_command(command).strip()
    payload = normalized[len("{CMD}") :]
    parts = [part.strip() for part in payload.split(",")]
    command_name = parts[0].upper() if parts else ""
    if not command_name:
        raise ValueError("command name is required")

    loop_id = None
    if command_name in LOOP_COMMANDS:
        if len(parts) < 2 or not parts[1]:
            raise ValueError("loop_id is required")
        loop_id = validate_loop_id(parts[1])

    return {
        "command": normalized,
        "command_name": command_name,
        "loop_id": loop_id,
    }


def response_matches_pending_command(metadata: dict[str, Any], response: dict[str, Any]) -> bool:
    """
    函数作用：
        判断 ACK/ERR 是否能匹配一条 pending 命令。

    主要流程：
        1. 只接受 valid 的 `{ACK}` 或 `{ERR}`。
        2. 比较响应 command 与 pending command_name。
        3. 如果响应携带 loop_id，则必须和 pending loop_id 精确一致；旧 ACK/ERR 不带
           loop_id 时退回到 command 名匹配，并由上层禁止同名分环命令并发 pending。

    参数说明：
        metadata 为 extract_command_metadata 或 dashboard 命令历史中的命令元数据。
        response 为 parse_frame 解析出的 ACK/ERR 字典。

    返回值：
        返回 True 表示可以关联；否则返回 False。
    """
    if response.get("kind") not in ("ack", "err") or not response.get("valid"):
        return False
    response_command = str(response.get("command", "")).strip().upper()
    if response_command != str(metadata.get("command_name", "")).strip().upper():
        return False

    response_loop_id = response.get("loop_id")
    # 单环 SET_PID 的 ACK/ERR 按旧协议不携带 loop_id；虚拟 single 环路只用于 host 内部评分。
    if str(metadata.get("loop_id", "")) == SINGLE_LOOP_ID and not response_loop_id:
        return True
    if response_loop_id:
        return str(response_loop_id).strip() == str(metadata.get("loop_id", "")).strip()
    return True


@dataclass
class AutoTuneConfig:
    """
    自动调参运行配置。

    字段说明：
        auto 表示是否允许状态机自动生成写串口动作。
        profile 选择协议/预设；single-loop 使用旧单环帧，multi-loop 使用通用分环帧。
        mode 表示 observe、suggest 或 auto-tune。
        max_step 是单次 PID 参数变化比例上限。
        window_seconds 是评分窗口长度，单位秒。
        ack_timeout_seconds 是等待 ACK/ERR 的超时时间，单位秒。
        min_post_ack_samples 是 ACK 后至少需要的新遥测样本数。
        rollback_on_regression 表示评分变差时是否回滚。
        loop_order 是多环按内环到外环的调参顺序；空值表示按发现顺序。
        conservative_loops 是需要更保守 Kp 步长的环路集合，通常用于外环。
        line_safety_enabled 控制 `{SENS}.line_lost` 是否作为中止门槛；None 使用 profile 默认值。
    """

    auto: bool = False
    profile: str = MULTI_LOOP_PROFILE
    mode: str = "observe"
    max_step: float = 0.10
    window_seconds: float = 3.0
    ack_timeout_seconds: float = 2.0
    min_post_ack_samples: int = DEFAULT_MIN_POST_ACK_SAMPLES
    rollback_on_regression: bool = True
    loop_order: tuple[str, ...] = ()
    conservative_loops: tuple[str, ...] = ()
    line_safety_enabled: bool | None = None


class AutoTuneController:
    """
    PID 自动调参纯状态机。

    类作用：
        接收已解析的 `{PID}` / `{CFG}` 或 `{PIDX}` / `{CFGX}` / `{SENS}` / `{ACK}` / `{ERR}` 帧，
        按 single-loop、multi-loop 或 line-car-cascade profile 生成观察、建议、自动发送或回滚动作。
        该类不直接读写串口，便于单元测试和 dashboard/CLI 复用。
    """

    def __init__(self, config: AutoTuneConfig):
        """
        函数作用：
            初始化自动调参控制器。

        主要流程：
            保存配置、确定单环或多环调参顺序、创建配置表/样本表/历史表，并进入 DISCOVER 状态。

        参数说明：
            config 为自动调参配置；auto=False 或 mode 非 auto-tune 时不会自动生成发送动作。

        返回值：
            构造函数无显式返回值。
        """
        self.config = config
        if config.profile not in SUPPORTED_AUTOTUNE_PROFILES:
            raise ValueError(f"profile must be one of: {', '.join(SUPPORTED_AUTOTUNE_PROFILES)}")
        self.single_loop = config.profile == SINGLE_LOOP_PROFILE
        explicit_order = parse_loop_id_list(config.loop_order)
        explicit_conservative = parse_loop_id_list(config.conservative_loops)
        if self.single_loop:
            # 单环旧协议没有 loop_id；内部固定使用 single 复用多环评分和回滚逻辑。
            self.loop_order = [SINGLE_LOOP_ID]
            self.conservative_loops: set[str] = set()
            self.line_safety_enabled = bool(config.line_safety_enabled) if config.line_safety_enabled is not None else False
        elif config.profile == LINE_CAR_CASCADE_PROFILE:
            # 旧循迹小车 profile 保留既有四环顺序和丢线安全，避免破坏已接入项目。
            self.loop_order = list(explicit_order or LINE_CAR_CASCADE_ORDER)
            self.conservative_loops = set(explicit_conservative or LINE_CAR_CONSERVATIVE_LOOPS)
            self.line_safety_enabled = bool(config.line_safety_enabled) if config.line_safety_enabled is not None else True
        else:
            # 通用多环不推断硬件拓扑；用户可显式给顺序，未给时使用 CFGX/PIDX 发现顺序。
            self.loop_order = list(explicit_order)
            self.conservative_loops = set(explicit_conservative)
            self.line_safety_enabled = bool(config.line_safety_enabled) if config.line_safety_enabled is not None else False
        self.discovered_loop_order: list[str] = []
        self.state = "DISCOVER"
        self.loop_configs: dict[str, dict[str, Any]] = {}
        self.samples_by_loop: dict[str, list[dict[str, Any]]] = {}
        self.latest_sens: dict[str, Any] | None = None
        self.scores: dict[str, dict[str, float]] = {}
        self.rollback_history: list[dict[str, Any]] = []
        self.pending_step: dict[str, Any] | None = None
        self.completed_loops: set[str] = set()
        self.abort_reason: str | None = None

    def ingest_frame(self, frame: dict[str, Any]) -> None:
        """
        函数作用：
            注入一条已解析协议帧并更新自动调参内部状态。

        主要流程：
            1. 忽略 invalid 帧，避免坏数据参与调参。
            2. `{CFGX}` 更新 loop 配置。
            3. `{PIDX}` 追加到对应 loop 窗口并执行安全门槛检查。
            4. `{SENS}` 记录小车传感器状态，丢线时触发 ABORT。

        参数说明：
            frame 为 parse_frame 返回的结构化字典。

        返回值：
            无返回值。
        """
        if not frame.get("valid"):
            return

        kind = frame.get("kind")
        data = frame.get("data", {})
        if kind == "cfg" and self.single_loop:
            # 旧单环协议没有 loop_id；状态机内部使用稳定虚拟 ID 复用多环评分和回滚逻辑。
            self.loop_configs[SINGLE_LOOP_ID] = dict(data)
            if self.state == "DISCOVER":
                self.state = "SYNC_CONFIG"
            return

        if kind == "cfgx":
            loop_id = str(data["loop_id"])
            self._remember_discovered_loop(loop_id)
            self.loop_configs[loop_id] = dict(data)
            if self.state == "DISCOVER":
                self.state = "SYNC_CONFIG"
            return

        if kind == "pid" and self.single_loop:
            self._append_pid_sample(SINGLE_LOOP_ID, data, frame)
            return

        if kind == "pidx":
            loop_id = str(data["loop_id"])
            self._remember_discovered_loop(loop_id)
            self._append_pid_sample(loop_id, data, frame)
            return

        if kind == "sens":
            self.latest_sens = dict(data)
            if self.line_safety_enabled and int(data.get("line_lost", 0)) != 0:
                self._abort("line lost")

    def _remember_discovered_loop(self, loop_id: str) -> None:
        """
        函数作用：
            记录多环 `{CFGX}` / `{PIDX}` 首次出现的 loop 顺序。

        主要流程：
            对已通过 parser 校验的 loop_id 做去重追加；该顺序只在用户没有显式配置
            loop_order 时作为通用 multi-loop 的保守默认顺序。

        参数说明：
            loop_id 为已解析并通过安全字符校验的环路 ID。

        返回值：
            无返回值。
        """
        if self.single_loop:
            return
        if loop_id not in self.discovered_loop_order:
            self.discovered_loop_order.append(loop_id)

    def handle_response(self, response: dict[str, Any], now: float | None = None) -> None:
        """
        函数作用：
            处理自动调参 pending 命令对应的 ACK/ERR。

        主要流程：
            step 命令 ACK 后进入结果观察；rollback 命令 ACK 后才标记 loop 完成；
            任意 ERR 都说明板端拒绝当前事务，必须立即 ABORT。

        参数说明：
            response 为 parse_frame 返回的 ACK/ERR 字典。
            now 为本机单调时间戳，允许测试注入；None 时使用 time.monotonic()。

        返回值：
            无返回值。
        """
        if self.pending_step is None:
            return
        if not response_matches_pending_command(self.pending_step, response):
            if response.get("kind") in ("ack", "err") and response.get("valid"):
                response_command = str(response.get("command", ""))
                response_loop = response.get("loop_id")
                pending_command = str(self.pending_step.get("command_name", ""))
                pending_loop = str(self.pending_step.get("loop_id", ""))
                self._abort(
                    "mismatched response "
                    f"{response_command}/{response_loop or '-'} while waiting for {pending_command}/{pending_loop}"
                )
            return
        if response.get("kind") == "ack":
            current_time = time.monotonic() if now is None else float(now)
            phase = str(self.pending_step.get("phase", "step"))
            loop_id = str(self.pending_step.get("loop_id", ""))
            if phase == "rollback":
                self._mark_rollback_status("ack")
                self.completed_loops.add(loop_id)
                self.pending_step = None
                self.state = "NEXT_LOOP"
                return

            self.pending_step["ack_received"] = True
            self.pending_step["ack_at"] = current_time
            self.pending_step["ack_sample_count"] = len(self.samples_by_loop.get(loop_id, []))
            self.state = "OBSERVE_RESULT"
            return
        if response.get("kind") == "err":
            if str(self.pending_step.get("phase", "step")) == "rollback":
                self._mark_rollback_status("err")
            self._abort(
                f"board rejected {self.pending_step.get('command_name')} "
                f"{self.pending_step.get('phase', 'step')}"
            )

    def _append_pid_sample(self, loop_id: str, data: dict[str, Any], frame: dict[str, Any]) -> None:
        """
        函数作用：
            将 PID 或 PIDX 遥测样本追加到指定环路窗口，并执行安全门槛检查。

        主要流程：
            1. 复制 typed data，避免调用方后续修改影响状态机。
            2. dashboard 帧带 received_at 时保留该时间戳，用于按秒裁剪窗口。
            3. 控制每个环路最多保留 200 条样本。
            4. 发现 fault 或 sensor_ok 异常立即 ABORT，阻止继续自动写参。

        参数说明：
            loop_id 为状态机内部环路 ID；单环 profile 使用虚拟值 single。
            data 为 parse_frame 得到的 PID/PIDX data。
            frame 为完整 parsed frame，可能携带 received_at。

        返回值：
            无返回值。
        """
        sample = dict(data)
        if self.single_loop and "loop_id" not in sample:
            sample["loop_id"] = loop_id
            sample["loop_name"] = loop_id
        if "received_at" in frame:
            sample["received_at"] = frame["received_at"]
        self.samples_by_loop.setdefault(loop_id, []).append(sample)
        self.samples_by_loop[loop_id] = self.samples_by_loop[loop_id][-200:]
        if int(data.get("fault", 0)) != 0:
            self._abort(f"fault on {loop_id}")
        if int(data.get("sensor_ok", 1)) != 1:
            self._abort(f"sensor bad on {loop_id}")

    def plan_next_action(self, now: float | None = None) -> dict[str, Any]:
        """
        函数作用：
            根据当前状态生成下一步自动调参动作。

        主要流程：
            1. ABORT 状态只返回 abort。
            2. 存在 pending_step 时先检查 ACK 超时，再按 step/rollback 阶段推进。
            3. 没有 pending_step 时按显式 loop_order 或发现顺序选第一个可调 loop。
            4. observe 模式只返回诊断，suggest 模式只返回建议，auto-tune 模式且 auto=True 才返回 send。

        参数说明：
            now 为本机单调时间戳，允许测试注入；None 时使用 time.monotonic()。

        返回值：
            返回动作字典，type 可为 wait、observe、suggest、send、rollback、abort。
        """
        current_time = time.monotonic() if now is None else float(now)

        if self.state == "ABORT":
            return {"type": "abort", "reason": self.abort_reason}

        if self.pending_step is not None:
            loop_id = str(self.pending_step.get("loop_id", ""))
            phase = str(self.pending_step.get("phase", "step"))
            if not self.pending_step.get("ack_received"):
                if self._pending_timed_out(current_time):
                    if phase == "rollback":
                        self._mark_rollback_status("timeout")
                    self._abort(f"ACK timeout for {phase} {self.pending_step.get('command_name')} {loop_id}")
                    return {"type": "abort", "reason": self.abort_reason}
                return {
                    "type": "wait",
                    "state": self.state,
                    "loop_id": loop_id,
                    "reason": "waiting for rollback ACK" if phase == "rollback" else "waiting for ACK",
                    "command": self.pending_step["command"],
                }

            if phase == "rollback":
                return {
                    "type": "wait",
                    "state": self.state,
                    "loop_id": loop_id,
                    "reason": "waiting for rollback ACK",
                    "command": self.pending_step["command"],
                }

            ack_sample_count = int(self.pending_step.get("ack_sample_count", 0))
            post_ack_sample_count = max(0, len(self.samples_by_loop.get(loop_id, [])) - ack_sample_count)
            min_post_ack_samples = max(1, int(self.config.min_post_ack_samples))
            if post_ack_sample_count < min_post_ack_samples:
                return {
                    "type": "wait",
                    "state": self.state,
                    "loop_id": loop_id,
                    "reason": "waiting for post-ACK telemetry",
                    "command": self.pending_step["command"],
                    "post_ack_sample_count": post_ack_sample_count,
                    "min_post_ack_samples": min_post_ack_samples,
                }
            return self._evaluate_pending_step(current_time, ack_sample_count)

        loop_id = self._select_next_loop()
        if loop_id is None:
            return {"type": "wait", "state": self.state, "reason": "waiting for loop config and telemetry"}

        score = self.score_loop(loop_id)
        self.scores[loop_id] = score

        if self.config.mode == "observe":
            self.state = "OBSERVE_BASELINE"
            return {"type": "observe", "loop_id": loop_id, "score": score}

        proposal = self._build_step(loop_id, score)
        if proposal.get("type") == "abort":
            return proposal

        if self.config.mode == "suggest" or not self.config.auto:
            self.state = "PROPOSE_STEP"
            return {"type": "suggest", "loop_id": loop_id, "score": score, "command": proposal["command"]}

        self.pending_step = proposal
        self.pending_step["sent_at"] = current_time
        self.state = "SEND_STEP"
        return {
            "type": "send",
            "loop_id": loop_id,
            "score": score,
            "command": proposal["command"],
            "reason": proposal["reason"],
            "strategy": proposal["strategy"],
            "changed_param": proposal["changed_param"],
        }

    def plan_timeout_action(self, now: float | None = None) -> dict[str, Any]:
        """
        函数作用：
            在没有新有效串口帧时只推进自动调参的等待/超时状态。

        主要流程：
            1. 已经 ABORT 时直接返回停止原因。
            2. 没有 pending step 时只返回等待有效帧，不能基于旧窗口创建新的 send/rollback。
            3. 已有 pending step 时复用 ACK 超时和 post-ACK 等待逻辑，保证静默串口不会永久 pending。

        参数说明：
            now 为本机单调时间戳，允许测试注入；None 时使用 time.monotonic()。

        返回值：
            返回 wait 或 abort 动作；不会返回 send、suggest、rollback 或 keep。
        """
        current_time = time.monotonic() if now is None else float(now)

        if self.state == "ABORT":
            return {"type": "abort", "reason": self.abort_reason}

        if self.pending_step is None:
            return {"type": "wait", "state": self.state, "reason": "waiting for valid frame"}

        loop_id = str(self.pending_step.get("loop_id", ""))
        phase = str(self.pending_step.get("phase", "step"))
        if not self.pending_step.get("ack_received"):
            if self._pending_timed_out(current_time):
                if phase == "rollback":
                    self._mark_rollback_status("timeout")
                self._abort(f"ACK timeout for {phase} {self.pending_step.get('command_name')} {loop_id}")
                return {"type": "abort", "reason": self.abort_reason}
            return {
                "type": "wait",
                "state": self.state,
                "loop_id": loop_id,
                "reason": "waiting for rollback ACK" if phase == "rollback" else "waiting for ACK",
                "command": self.pending_step["command"],
            }

        if phase == "rollback":
            return {
                "type": "wait",
                "state": self.state,
                "loop_id": loop_id,
                "reason": "waiting for rollback ACK",
                "command": self.pending_step["command"],
            }

        ack_sample_count = int(self.pending_step.get("ack_sample_count", 0))
        post_ack_sample_count = max(0, len(self.samples_by_loop.get(loop_id, [])) - ack_sample_count)
        min_post_ack_samples = max(1, int(self.config.min_post_ack_samples))
        return {
            "type": "wait",
            "state": self.state,
            "loop_id": loop_id,
            "reason": "waiting for post-ACK telemetry",
            "command": self.pending_step["command"],
            "post_ack_sample_count": post_ack_sample_count,
            "min_post_ack_samples": min_post_ack_samples,
        }

    def score_loop(self, loop_id: str, start_index: int = 0) -> dict[str, float]:
        """
        函数作用：
            计算某个 loop 最近窗口的调参评分指标。

        主要流程：
            从最近样本中统计平均误差、最大误差、平均有符号误差、过零次数、饱和比例、
            抗饱和比例、传感器异常比例和综合分。
            当 start_index 大于 0 时，只统计该索引之后的样本，用于 ACK 后效果评估。

        参数说明：
            loop_id 为目标环路。
            start_index 为样本起始下标；0 表示使用完整可用窗口。

        返回值：
            返回指标字典；score 越低表示效果越好。
        """
        line_lost_count = self._line_lost_count_for_score()
        samples = self._window_samples(loop_id, start_index=start_index)
        if not samples:
            return {
                "mean_abs_error": float("inf"),
                "max_abs_error": float("inf"),
                "zero_crossings": 0.0,
                "sat_ratio": 1.0,
                "anti_windup_ratio": 1.0,
                "sensor_bad_ratio": 1.0,
                "line_lost_count": line_lost_count,
                "score": float("inf"),
            }

        errors = [float(sample.get("error", 0.0)) for sample in samples]
        abs_errors = [abs(error) for error in errors]
        zero_crossings = 0
        for previous, current in zip(errors, errors[1:]):
            if previous == 0.0 or current == 0.0:
                continue
            if (previous > 0.0) != (current > 0.0):
                zero_crossings += 1

        sat_ratio = sum(1 for sample in samples if int(sample.get("sat", 0)) != 0) / len(samples)
        anti_ratio = sum(1 for sample in samples if int(sample.get("anti_windup", 0)) != 0) / len(samples)
        sensor_bad_ratio = sum(1 for sample in samples if int(sample.get("sensor_ok", 1)) != 1) / len(samples)
        mean_error = sum(errors) / len(errors)
        mean_abs_error = sum(abs_errors) / len(abs_errors)
        max_abs_error = max(abs_errors)
        score = mean_abs_error + 0.25 * max_abs_error + zero_crossings * 2.0
        score += sat_ratio * 20.0 + anti_ratio * 10.0 + sensor_bad_ratio * 100.0 + line_lost_count * 100.0

        return {
            "sample_count": float(len(samples)),
            "mean_error": mean_error,
            "mean_abs_error": mean_abs_error,
            "max_abs_error": max_abs_error,
            "zero_crossings": float(zero_crossings),
            "sat_ratio": sat_ratio,
            "anti_windup_ratio": anti_ratio,
            "sensor_bad_ratio": sensor_bad_ratio,
            "line_lost_count": line_lost_count,
            "score": score,
        }

    def _line_lost_count_for_score(self) -> float:
        """
        函数作用：
            根据当前 profile 安全配置返回评分用的循迹丢线惩罚。

        主要流程：
            只有 line_safety_enabled 为真时，最新有效 `{SENS}.line_lost` 才参与评分；
            通用 multi-loop 默认关闭该项，避免倒立摆、编码器位置环等非循迹项目被循迹字段误伤。

        参数说明：
            无参数。

        返回值：
            返回 0.0 或 1.0；1.0 表示当前安全配置下检测到丢线。
        """
        if not self.line_safety_enabled or not self.latest_sens:
            return 0.0
        return float(int(self.latest_sens.get("line_lost", 0)))

    def _select_next_loop(self) -> str | None:
        """
        函数作用：
            按当前 profile 的调参顺序选择下一个可调环路。

        主要流程：
            只选择同时具备配置快照和 PID 样本、且尚未完成的环路；单环 profile 使用虚拟 single 环路。
            多环显式配置 loop_order 时按配置顺序；未配置时按 CFGX/PIDX 首次发现顺序。

        参数说明：
            无参数。

        返回值：
            返回 loop_id；没有可调 loop 时返回 None。
        """
        candidate_order = self.loop_order if self.loop_order else self.discovered_loop_order
        for loop_id in candidate_order:
            if loop_id in self.completed_loops:
                continue
            if loop_id not in self.loop_configs:
                continue
            if not self.samples_by_loop.get(loop_id):
                continue
            return loop_id
        return None

    def _build_pid_command(self, loop_id: str, kp: float, ki: float, kd: float) -> str:
        """
        函数作用：
            按当前 profile 构造 PID 参数下发命令。

        主要流程：
            single-loop profile 生成旧协议 `{CMD}SET_PID`；串级 profile 生成带 loop_id 的
            `{CMD}SET_PIDX`。这样状态机可以复用同一套评分/回滚逻辑，但不要求单环固件实现分环命令。

        参数说明：
            loop_id 为状态机内部环路 ID；单环时应为 single，串级时为真实 loop_id。
            kp、ki、kd 为待下发 PID 参数。

        返回值：
            返回完整 `{CMD}` 文本，不包含换行。
        """
        if self.single_loop:
            return build_set_pid_command(kp, ki, kd)
        return build_set_pidx_command(loop_id, kp, ki, kd)

    def _build_step(self, loop_id: str, score: dict[str, float]) -> dict[str, Any]:
        """
        函数作用：
            基于当前评分和配置生成一次小步 PID 参数修改。

        主要流程：
            根据窗口诊断结果选择一个参数小步调整：积分饱和先收 Ki，震荡先增 Kd，
            稳态同向误差增 Ki，慢响应增 Kp；外环对 Kp 使用更保守步长。

        参数说明：
            loop_id 为目标环路。
            score 为 score_loop 返回的当前窗口指标。

        返回值：
            返回 pending step 字典，包含新旧参数、命令、原因和 ACK 状态。
        """
        cfg = self.loop_configs[loop_id]
        old_kp = float(cfg["kp"])
        old_ki = float(cfg["ki"])
        old_kd = float(cfg["kd"])
        step = max(0.0, min(float(self.config.max_step), 0.5))
        strategy = self._select_tuning_strategy(loop_id, score)
        changed_param = str(strategy["changed_param"])
        factor = float(strategy["factor"])
        reason = str(strategy["reason"])
        strategy_name = str(strategy["strategy"])

        new_kp = old_kp
        new_ki = old_ki
        new_kd = old_kd
        if changed_param == "kp":
            new_kp = max(0.0, old_kp * factor)
        elif changed_param == "ki":
            new_ki = max(0.0, old_ki * factor)
        else:
            new_kd = max(0.0, old_kd * factor)

        command = self._build_pid_command(loop_id, new_kp, new_ki, new_kd)
        old_command = self._build_pid_command(loop_id, old_kp, old_ki, old_kd)
        if command == old_command:
            # 按板端实际接收的三位小数命令比较；相同则说明本次建议不会改变控制器参数。
            self._abort(
                f"no-op auto-tune step for {loop_id}; provide non-zero seed PID parameters before auto-tune"
            )
            return {
                "type": "abort",
                "reason": self.abort_reason,
                "loop_id": loop_id,
                "strategy": strategy_name,
                "changed_param": changed_param,
                "command": command,
            }
        metadata = extract_command_metadata(command)
        return {
            **metadata,
            "loop_id": loop_id,
            "phase": "step",
            "old": {"kp": old_kp, "ki": old_ki, "kd": old_kd},
            "new": {"kp": new_kp, "ki": new_ki, "kd": new_kd},
            "baseline_score": score,
            "reason": reason,
            "strategy": strategy_name,
            "changed_param": changed_param,
            "ack_received": False,
            "sent_at": None,
        }

    def _select_tuning_strategy(self, loop_id: str, score: dict[str, float]) -> dict[str, Any]:
        """
        函数作用：
            按调参窗口指标选择一次只改一个 PID 参数的策略。

        主要流程：
            1. 饱和且 anti_windup 频繁触发时优先降低 Ki，避免积分继续推高执行器输出。
            2. 误差多次过零时优先增加 Kd 做阻尼，不在无饱和场景下直接砍 Kp。
            3. 非保守环同向稳态误差明显且未饱和时增加 Ki，减少长期偏差。
            4. 其他慢响应场景增加 Kp；conservative_loops 中的环路 Kp 步长减半。

        参数说明：
            loop_id 为目标环路，用于判断是否属于保守调参环路。
            score 为 score_loop 生成的指标字典。

        返回值：
            返回包含 strategy、changed_param、factor 和 reason 的字典。
        """
        step = max(0.0, min(float(self.config.max_step), 0.5))
        sat_ratio = float(score.get("sat_ratio", 0.0))
        anti_ratio = float(score.get("anti_windup_ratio", 0.0))
        zero_crossings = float(score.get("zero_crossings", 0.0))
        mean_abs_error = float(score.get("mean_abs_error", 0.0))
        max_abs_error = float(score.get("max_abs_error", 0.0))
        mean_error = float(score.get("mean_error", 0.0))
        steady_bias = abs(mean_error) >= max(0.5, mean_abs_error * 0.70)

        if sat_ratio > 0.20 and anti_ratio > 0.20:
            return {
                "strategy": "reduce_ki_for_integral_saturation",
                "changed_param": "ki",
                "factor": 1.0 - step,
                "reason": "reduce ki because output saturation is clamping integral growth",
            }

        if zero_crossings >= 2.0:
            return {
                "strategy": "increase_kd_for_oscillation",
                "changed_param": "kd",
                "factor": 1.0 + step,
                "reason": "increase kd to add damping for oscillation",
            }

        if (
            loop_id not in self.conservative_loops and
            steady_bias and
            mean_abs_error > 0.0 and
            sat_ratio <= 0.05 and
            anti_ratio <= 0.05
        ):
            return {
                "strategy": "increase_ki_for_steady_bias",
                "changed_param": "ki",
                "factor": 1.0 + step,
                "reason": "increase ki to reduce steady bias",
            }

        kp_step = step * 0.5 if loop_id in self.conservative_loops else step
        if max_abs_error > mean_abs_error * 3.0 and mean_abs_error > 0.0:
            kp_step *= 0.5

        return {
            "strategy": "increase_kp_for_slow_response",
            "changed_param": "kp",
            "factor": 1.0 + kp_step,
            "reason": "increase kp to improve slow response",
        }

    def _evaluate_pending_step(self, now: float, ack_sample_count: int) -> dict[str, Any]:
        """
        函数作用：
            对已 ACK 的参数修改观察结果并决定保留或回滚。

        主要流程：
            计算当前窗口评分；若综合分高于基线且允许回滚，则生成旧参数 SET_PID 或 SET_PIDX 回滚命令。

        参数说明：
            now 为当前时间戳，用于记录 rollback 发送时间。
            ack_sample_count 为 ACK 到达时该 loop 已有样本数；评估时只使用之后的新样本。

        返回值：
            返回 keep 或 rollback 动作字典。
        """
        assert self.pending_step is not None
        loop_id = str(self.pending_step["loop_id"])
        current_score = self.score_loop(loop_id, start_index=ack_sample_count)
        baseline = float(self.pending_step["baseline_score"]["score"])
        current = float(current_score["score"])
        if self.config.rollback_on_regression and current > baseline:
            old = self.pending_step["old"]
            command = self._build_pid_command(loop_id, old["kp"], old["ki"], old["kd"])
            record = {
                "loop_id": loop_id,
                "command": command,
                "baseline_score": self.pending_step["baseline_score"],
                "current_score": current_score,
                "reason": "score regression",
                "status": "pending",
            }
            self.rollback_history.append(record)
            rollback = {
                **extract_command_metadata(command),
                "loop_id": loop_id,
                "phase": "rollback",
                "old": old,
                "new": old,
                "baseline_score": self.pending_step["baseline_score"],
                "current_score": current_score,
                "reason": "score regression",
                "ack_received": False,
                "sent_at": now,
                "rollback_index": len(self.rollback_history) - 1,
            }
            self.pending_step = rollback
            self.state = "WAIT_ROLLBACK_ACK"
            return {"type": "rollback", **record}

        self.completed_loops.add(loop_id)
        self.pending_step = None
        self.state = "NEXT_LOOP"
        return {
            "type": "keep",
            "loop_id": loop_id,
            "baseline_score": baseline,
            "current_score": current,
        }

    def _window_samples(self, loop_id: str, start_index: int = 0) -> list[dict[str, Any]]:
        """
        函数作用：
            按 window_seconds 截取指定 loop 的评分样本窗口。

        主要流程：
            优先使用 dashboard 注入的 received_at；没有本机接收时间时退回板端 ms；
            若样本缺少任何时间字段，则保留旧行为使用最近 50 条，避免无时间模拟数据无法评分。
            start_index 用于 ACK 后评估，只保留 ACK 之后进入状态机的新样本。

        参数说明：
            loop_id 为目标 PID 环路 ID。
            start_index 为样本起始下标；越界时退回保留最新样本，避免除零。

        返回值：
            返回参与评分的样本列表，至少保留最新一条样本。
        """
        all_samples = self.samples_by_loop.get(loop_id, [])
        if start_index > 0:
            samples = all_samples[start_index:]
            if not samples and all_samples:
                samples = [all_samples[-1]]
        else:
            samples = all_samples
        if not samples:
            return []

        window_seconds = max(float(self.config.window_seconds), 0.001)
        latest = samples[-1]
        if isinstance(latest.get("received_at"), (int, float)):
            cutoff = float(latest["received_at"]) - window_seconds
            window = [sample for sample in samples if float(sample.get("received_at", cutoff - 1.0)) >= cutoff]
            return window or [latest]

        if isinstance(latest.get("ms"), (int, float)):
            cutoff_ms = float(latest["ms"]) - window_seconds * 1000.0
            window = [sample for sample in samples if float(sample.get("ms", cutoff_ms - 1.0)) >= cutoff_ms]
            return window or [latest]

        return samples[-50:]

    def _pending_timed_out(self, now: float) -> bool:
        """
        函数作用：
            判断当前 pending 命令是否超过 ACK 等待时间。

        主要流程：
            读取 pending_step.sent_at；如果尚未记录发送时间，则不触发超时；
            否则用 now - sent_at 和 ack_timeout_seconds 比较。

        参数说明：
            now 为当前单调时间戳或同一时间基准的测试时间。

        返回值：
            返回 True 表示必须 ABORT；否则返回 False。
        """
        if self.pending_step is None:
            return False
        sent_at = self.pending_step.get("sent_at")
        if sent_at is None:
            return False
        return now - float(sent_at) > max(float(self.config.ack_timeout_seconds), 0.001)

    def _mark_rollback_status(self, status: str) -> None:
        """
        函数作用：
            更新 rollback_history 中当前回滚事务的状态。

        主要流程：
            从 pending_step.rollback_index 找到历史记录，写入 ack/err/timeout 等状态，
            便于 dashboard 展示回滚是否真正被板端确认。

        参数说明：
            status 为回滚状态文本。

        返回值：
            无返回值。
        """
        if self.pending_step is None:
            return
        index = self.pending_step.get("rollback_index")
        if isinstance(index, int) and 0 <= index < len(self.rollback_history):
            self.rollback_history[index]["status"] = status

    def abort(self, reason: str) -> None:
        """
        函数作用：
            供 CLI/dashboard 在串口断开、本地发送失败等外部故障时主动中止自动调参。

        主要流程：
            转发到内部 _abort，保留首个中止原因并清理 pending 命令。

        参数说明：
            reason 为外部故障原因。

        返回值：
            无返回值。
        """
        self._abort(reason)

    def _abort(self, reason: str) -> None:
        """
        函数作用：
            进入 ABORT 状态并记录停止原因。

        主要流程：
            保存首个停止原因，清空 pending_step，防止后续继续生成写参动作。

        参数说明：
            reason 为触发停止的安全原因。

        返回值：
            无返回值。
        """
        if self.state != "ABORT":
            self.abort_reason = reason
        self.pending_step = None
        self.state = "ABORT"


def cmd_send(args: argparse.Namespace) -> int:
    """
    函数作用：
        执行 send 子命令，显式发送一条 {CMD} 命令并读取短时间回复。

    参数说明：
        args 为 argparse 解析后的参数。

    返回值：
        收到 ACK 返回 0；收到 ERR、无回复或串口失败返回 1；命令格式错误返回 2。
    """
    resolved = resolve_port_and_baud(args)
    if resolved is None:
        return 1
    port, baud = resolved

    try:
        metadata = extract_command_metadata(args.command)
        command = normalize_command(args.command)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        with open_serial(port, baud, timeout=0.5) as ser:
            ser.write(command.encode("ascii"))
            ser.flush()
            deadline = time.monotonic() + args.response_seconds
            while time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                line = decode_line(raw)
                if not line:
                    continue
                parsed = parse_frame(line)
                record = {"port": port, "baud": baud, "time": time.time(), **parsed}
                print_record(record, args.jsonl)
                if parsed["kind"] in ("ack", "err"):
                    if response_matches_pending_command(metadata, parsed):
                        return 0 if parsed["kind"] == "ack" else 1
                    if not args.jsonl:
                        print(
                            f"Ignored unmatched response for {parsed.get('command')}; waiting for {metadata['command_name']}.",
                            file=sys.stderr,
                        )
    except (OSError, serial.SerialException) as exc:
        print(f"Serial send failed: {exc}", file=sys.stderr)
        return 1

    print("No ACK/ERR response received.", file=sys.stderr)
    return 1


def print_autotune_action(action: dict[str, Any], as_json: bool) -> None:
    """
    函数作用：
        输出自动调参状态机动作。

    主要流程：
        JSONL 模式直接输出完整动作；普通模式只输出关键信息，避免长时间读取刷屏。

    参数说明：
        action 为 AutoTuneController.plan_next_action 返回的动作字典。
        as_json 表示是否按 JSONL 输出。

    返回值：
        无返回值。
    """
    if as_json:
        print(json.dumps({"time": time.time(), "autotune": action}, ensure_ascii=False), flush=True)
        return

    action_type = action.get("type")
    loop_id = action.get("loop_id", "-")
    if action_type in ("send", "suggest", "rollback"):
        print(f"{action_type}\tloop={loop_id}\tcommand={action.get('command')}\treason={action.get('reason', '')}", flush=True)
    elif action_type == "abort":
        print(f"abort\treason={action.get('reason')}", flush=True)
    elif action_type == "keep":
        print(
            f"keep\tloop={loop_id}\tbaseline={action.get('baseline_score')}\tcurrent={action.get('current_score')}",
            flush=True,
        )


def cmd_autotune(args: argparse.Namespace) -> int:
    """
    函数作用：
        执行单环或串级 PID 自动调参 CLI。

    主要流程：
        1. 解析串口并创建 AutoTuneController。
        2. 可选发送 `{CMD}GET_CFG` 或 `{CMD}GET_ALL_CFG`，促使板端回传配置快照。
        3. 持续读取 PID/CFG/PIDX/CFGX/SENS/ACK/ERR 帧并推进状态机。
        4. observe/suggest 模式只输出动作；auto-tune 模式才会自动发送 SET_PID/SET_PIDX 或回滚命令。

    参数说明：
        args 为 argparse 解析后的参数。

    返回值：
        正常结束返回 0；串口失败返回 1；用户中断返回 130。
    """
    resolved = resolve_port_and_baud(args)
    if resolved is None:
        return 1
    port, baud = resolved
    config = AutoTuneConfig(
        auto=args.mode == "auto-tune",
        profile=args.profile,
        mode=args.mode,
        max_step=args.max_step,
        window_seconds=args.window_seconds,
        ack_timeout_seconds=args.ack_timeout_seconds,
        min_post_ack_samples=args.min_post_ack_samples,
        rollback_on_regression=args.rollback_on_regression,
        loop_order=parse_loop_id_list(args.loop_order),
        conservative_loops=parse_loop_id_list(args.conservative_loops),
        line_safety_enabled=args.line_safety_enabled,
    )
    controller = AutoTuneController(config)
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    frame_count = 0
    last_printed_action: str | None = None

    try:
        with open_serial(port, baud, timeout=0.5) as ser:
            decoder = ProtocolStreamDecoder()
            if args.request_config:
                # 单环板端使用旧 GET_CFG；串级板端使用 GET_ALL_CFG 同步全部 CFGX 配置快照。
                request_command = "{CMD}GET_CFG" if config.profile == SINGLE_LOOP_PROFILE else "{CMD}GET_ALL_CFG"
                ser.write(normalize_command(request_command).encode("ascii"))
                ser.flush()

            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if args.count > 0 and frame_count >= args.count:
                    break

                raw = ser.read(256)
                if not raw:
                    # 串口暂时无新帧时仍推进状态机，确保丢 ACK 会按超时进入 ABORT。
                    action = controller.plan_timeout_action(now=time.monotonic())
                    if action.get("type") == "abort":
                        print_autotune_action(action, args.jsonl)
                        return 1
                    continue
                for parsed in decoder.feed(raw):
                    frame_count += 1

                    if not parsed.get("valid"):
                        # 坏帧不能触发 proposal/send；否则自动调参会在串口异常时基于旧窗口继续写参。
                        continue

                    if parsed.get("kind") in ("ack", "err"):
                        controller.handle_response(parsed, now=time.monotonic())
                    else:
                        controller.ingest_frame(parsed)

                    action = controller.plan_next_action(now=time.monotonic())
                    signature = json.dumps(action, sort_keys=True, default=str)
                    if action.get("type") != "wait" and signature != last_printed_action:
                        print_autotune_action(action, args.jsonl)
                        last_printed_action = signature

                    if action.get("type") in ("send", "rollback") and config.auto:
                        command = normalize_command(str(action["command"]))
                        ser.write(command.encode("ascii"))
                        ser.flush()
                        if not args.jsonl:
                            print(f"sent\t{command.strip()}", flush=True)

                    if action.get("type") == "abort":
                        return 1
                    if args.count > 0 and frame_count >= args.count:
                        break
    except KeyboardInterrupt:
        return 130
    except (OSError, serial.SerialException) as exc:
        print(f"Autotune serial session failed: {exc}", file=sys.stderr)
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    函数作用：
        构建命令行解析器。

    主要流程：
        注册 list、scan、read、send 四个子命令及其参数。

    参数说明：
        无参数。

    返回值：
        返回 argparse.ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="Detect and read PID AI serial protocol frames.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List available serial ports.")
    list_parser.add_argument("--json", action="store_true", help="Output JSON.")
    list_parser.set_defaults(func=cmd_list)

    scan_parser = subparsers.add_parser("scan", help="Scan ports for PID AI protocol frames.")
    scan_parser.add_argument("--port", help="Limit scan to one port, for example COM5.")
    scan_parser.add_argument("--baud-rates", help="Comma-separated baud rates.")
    scan_parser.add_argument("--sample-seconds", type=float, default=2.0, help="Seconds to sample each port/baud.")
    scan_parser.add_argument("--max-lines", type=int, default=20, help="Maximum lines to read per port/baud.")
    scan_parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports.")
    scan_parser.add_argument("--show-all", action="store_true", help="Show no-match results too.")
    scan_parser.add_argument("--json", action="store_true", help="Output JSON.")
    scan_parser.set_defaults(func=cmd_scan)

    read_parser = subparsers.add_parser("read", help="Read frames from a PID AI board.")
    read_parser.add_argument("--port", help="Serial port, for example COM5.")
    read_parser.add_argument("--baud", type=int, default=115200, help="Baud rate when --port is used.")
    read_parser.add_argument("--auto", action="store_true", help="Auto-detect port before reading.")
    read_parser.add_argument("--baud-rates", help="Comma-separated baud rates for --auto.")
    read_parser.add_argument("--sample-seconds", type=float, default=2.0, help="Auto-detect sample seconds.")
    read_parser.add_argument("--max-lines", type=int, default=20, help="Auto-detect max lines.")
    read_parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports in --auto mode.")
    read_parser.add_argument("--duration", type=float, default=0.0, help="Read duration in seconds; 0 means until stopped.")
    read_parser.add_argument("--count", type=int, default=0, help="Maximum output lines; 0 means no count limit.")
    read_parser.add_argument("--known-only", action="store_true", help="Only output known PID AI protocol frames.")
    read_parser.add_argument("--jsonl", action="store_true", help="Output parsed JSONL records.")
    read_parser.set_defaults(func=cmd_read)

    send_parser = subparsers.add_parser("send", help="Send one explicit {CMD} command.")
    send_parser.add_argument("--command", required=True, help="Command text, for example {CMD}GET_CFG.")
    send_parser.add_argument("--port", help="Serial port, for example COM5.")
    send_parser.add_argument("--baud", type=int, default=115200, help="Baud rate when --port is used.")
    send_parser.add_argument("--auto", action="store_true", help="Auto-detect port before sending.")
    send_parser.add_argument("--baud-rates", help="Comma-separated baud rates for --auto.")
    send_parser.add_argument("--sample-seconds", type=float, default=2.0, help="Auto-detect sample seconds.")
    send_parser.add_argument("--max-lines", type=int, default=20, help="Auto-detect max lines.")
    send_parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports in --auto mode.")
    send_parser.add_argument("--response-seconds", type=float, default=2.0, help="Seconds to wait for ACK/ERR.")
    send_parser.add_argument("--jsonl", action="store_true", help="Output parsed JSONL records.")
    send_parser.set_defaults(func=cmd_send)

    autotune_parser = subparsers.add_parser("autotune", help="Run PID observe/suggest/auto-tune workflow.")
    autotune_parser.add_argument("--port", help="Serial port, for example COM5.")
    autotune_parser.add_argument("--baud", type=int, default=115200, help="Baud rate when --port is used.")
    autotune_parser.add_argument("--auto", action="store_true", help="Auto-detect port before tuning.")
    autotune_parser.add_argument("--baud-rates", help="Comma-separated baud rates for --auto.")
    autotune_parser.add_argument("--sample-seconds", type=float, default=2.0, help="Auto-detect sample seconds.")
    autotune_parser.add_argument("--max-lines", type=int, default=20, help="Auto-detect max lines.")
    autotune_parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports in --auto mode.")
    autotune_parser.add_argument(
        "--profile",
        default=MULTI_LOOP_PROFILE,
        choices=list(SUPPORTED_AUTOTUNE_PROFILES),
        help="Auto-tune profile; use multi-loop for generic PIDX/CFGX, single-loop for legacy PID/CFG, or line-car-cascade preset.",
    )
    autotune_parser.add_argument(
        "--loop-order",
        default="",
        help="Comma-separated multi-loop tuning order from inner to outer, for example motor_speed,angle,position.",
    )
    autotune_parser.add_argument(
        "--conservative-loops",
        default="",
        help="Comma-separated loop IDs that should use half Kp steps, usually outer loops.",
    )
    autotune_parser.add_argument(
        "--line-safety",
        dest="line_safety_enabled",
        action="store_true",
        default=None,
        help="Abort auto-tune when a valid SENS frame reports line_lost=1.",
    )
    autotune_parser.add_argument(
        "--no-line-safety",
        dest="line_safety_enabled",
        action="store_false",
        help="Do not use SENS line_lost as an auto-tune abort gate.",
    )
    autotune_parser.add_argument(
        "--mode",
        default="observe",
        choices=["observe", "suggest", "auto-tune"],
        help="observe only reads scores, suggest prints commands, auto-tune sends and rolls back automatically.",
    )
    autotune_parser.add_argument("--max-step", type=float, default=0.10, help="Maximum one-step kp/ki/kd ratio change.")
    autotune_parser.add_argument("--window-seconds", type=float, default=3.0, help="Scoring window length in seconds.")
    autotune_parser.add_argument("--ack-timeout-seconds", type=float, default=2.0, help="Seconds to wait for ACK before aborting auto-tune.")
    autotune_parser.add_argument(
        "--min-post-ack-samples",
        type=int,
        default=DEFAULT_MIN_POST_ACK_SAMPLES,
        help="Minimum new PID/PIDX samples after ACK before keep/rollback decision.",
    )
    autotune_parser.add_argument(
        "--rollback-on-regression",
        dest="rollback_on_regression",
        action="store_true",
        default=True,
        help="Send rollback command when score gets worse after ACK.",
    )
    autotune_parser.add_argument(
        "--no-rollback-on-regression",
        dest="rollback_on_regression",
        action="store_false",
        help="Keep changed parameters even if score gets worse.",
    )
    autotune_parser.add_argument(
        "--request-config",
        dest="request_config",
        action="store_true",
        default=True,
        help="Send read-only config request at session start: GET_CFG for single-loop, GET_ALL_CFG for multi-loop profiles.",
    )
    autotune_parser.add_argument(
        "--no-request-config",
        dest="request_config",
        action="store_false",
        help="Do not send GET_CFG/GET_ALL_CFG automatically.",
    )
    autotune_parser.add_argument("--duration", type=float, default=0.0, help="Session duration in seconds; 0 means until stopped.")
    autotune_parser.add_argument("--count", type=int, default=0, help="Maximum input frames to process; 0 means no count limit.")
    autotune_parser.add_argument("--jsonl", action="store_true", help="Output actions as JSONL.")
    autotune_parser.set_defaults(func=cmd_autotune)

    return parser


def main(argv: list[str] | None = None) -> int:
    """
    函数作用：
        脚本入口，解析命令行并分发到对应子命令。

    参数说明：
        argv 为可选命令行参数列表；None 表示使用 sys.argv。

    返回值：
        返回子命令退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

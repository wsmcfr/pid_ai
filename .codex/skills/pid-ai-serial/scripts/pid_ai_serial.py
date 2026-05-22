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
CASCADE_PROFILE_ORDER = ("speed_l", "speed_r", "yaw_rate", "line_outer")
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
        2. 拒绝空字符串、逗号和换行，避免破坏 CSV 协议边界。

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
        return parse_named_numeric_frame("sens", "{SENS}", SENS_FIELDS, line)
    if line.startswith("{ACK}"):
        parts = line[len("{ACK}") :].split(",")
        return {
            "kind": "ack",
            "valid": len(parts) >= 2,
            "raw": line,
            "command": parts[0] if len(parts) > 0 else "",
            "detail": parts[1] if len(parts) > 1 else "",
        }
    if line.startswith("{ERR}"):
        parts = line[len("{ERR}") :].split(",")
        return {
            "kind": "err",
            "valid": len(parts) >= 3,
            "raw": line,
            "command": parts[0] if len(parts) > 0 else "",
            "status": parts[1] if len(parts) > 1 else "",
            "detail": parts[2] if len(parts) > 2 else "",
        }
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
            while time.monotonic() < deadline and len(sample_lines) < max_lines:
                raw = ser.readline()
                if not raw:
                    continue
                line = decode_line(raw)
                if not line:
                    continue
                sample_lines.append(line)
                prefix = line_prefix(line)
                if prefix is None:
                    continue
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                frames += 1
                score += 10 if prefix in ("{PID}", "{CFG}") else 4
                parsed = parse_frame(line)
                if parsed.get("valid"):
                    score += 5
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
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if args.count > 0 and count >= args.count:
                    break
                raw = ser.readline()
                if not raw:
                    continue
                line = decode_line(raw)
                if not line:
                    continue
                parsed = parse_frame(line)
                if args.known_only and parsed["kind"] == "raw":
                    continue
                record = {"port": port, "baud": baud, "time": time.time(), **parsed}
                print_record(record, args.jsonl)
                count += 1
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
        检查命令必须以 {CMD} 开头，并保证以 CRLF 结尾。

    参数说明：
        command 为用户显式提供的命令。

    返回值：
        返回可写入串口的命令文本。
    """
    text = command.strip()
    if not text.startswith("{CMD}"):
        raise ValueError("command must start with {CMD}")
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
        3. 当前板端 ACK/ERR 不携带 loop_id，因此分环命令按命令名匹配，由 dashboard 保留 loop_id。

    参数说明：
        metadata 为 extract_command_metadata 或 dashboard 命令历史中的命令元数据。
        response 为 parse_frame 解析出的 ACK/ERR 字典。

    返回值：
        返回 True 表示可以关联；否则返回 False。
    """
    if response.get("kind") not in ("ack", "err") or not response.get("valid"):
        return False
    response_command = str(response.get("command", "")).strip().upper()
    return response_command == str(metadata.get("command_name", "")).strip().upper()


@dataclass
class AutoTuneConfig:
    """自动调参运行配置，全部字段来自 CLI 或 dashboard 显式开关。"""

    auto: bool = False
    profile: str = "line-car-cascade"
    mode: str = "observe"
    max_step: float = 0.10
    window_seconds: float = 3.0
    rollback_on_regression: bool = True


class AutoTuneController:
    """
    串级 PID 自动调参纯状态机。

    类作用：
        接收已解析的 `{PIDX}`、`{CFGX}`、`{SENS}`、`{ACK}`、`{ERR}` 帧，按固定小车
        profile 生成观察、建议、自动发送或回滚动作。该类不直接读写串口，便于单元测试和
        dashboard/CLI 复用。
    """

    def __init__(self, config: AutoTuneConfig):
        """
        函数作用：
            初始化自动调参控制器。

        主要流程：
            保存配置、确定串级 loop 顺序、创建配置表/样本表/历史表，并进入 DISCOVER 状态。

        参数说明：
            config 为自动调参配置；auto=False 或 mode 非 auto-tune 时不会自动生成发送动作。

        返回值：
            构造函数无显式返回值。
        """
        self.config = config
        self.loop_order = list(CASCADE_PROFILE_ORDER if config.profile == "line-car-cascade" else CASCADE_PROFILE_ORDER)
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
        if kind == "cfgx":
            loop_id = str(data["loop_id"])
            self.loop_configs[loop_id] = dict(data)
            if self.state == "DISCOVER":
                self.state = "SYNC_CONFIG"
            return

        if kind == "pidx":
            loop_id = str(data["loop_id"])
            self.samples_by_loop.setdefault(loop_id, []).append(dict(data))
            self.samples_by_loop[loop_id] = self.samples_by_loop[loop_id][-200:]
            if int(data.get("fault", 0)) != 0:
                self._abort(f"fault on {loop_id}")
            if int(data.get("sensor_ok", 1)) != 1:
                self._abort(f"sensor bad on {loop_id}")
            return

        if kind == "sens":
            self.latest_sens = dict(data)
            if int(data.get("line_lost", 0)) != 0:
                self._abort("line lost")

    def handle_response(self, response: dict[str, Any]) -> None:
        """
        函数作用：
            处理自动调参 pending 命令对应的 ACK/ERR。

        主要流程：
            ACK 让状态机从 SEND_STEP 进入 OBSERVE_RESULT；ERR 说明板端拒绝参数，立即 ABORT。

        参数说明：
            response 为 parse_frame 返回的 ACK/ERR 字典。

        返回值：
            无返回值。
        """
        if self.pending_step is None:
            return
        if not response_matches_pending_command(self.pending_step, response):
            return
        if response.get("kind") == "ack":
            self.pending_step["ack_received"] = True
            self.pending_step["ack_sample_count"] = len(
                self.samples_by_loop.get(str(self.pending_step.get("loop_id", "")), [])
            )
            self.state = "OBSERVE_RESULT"
            return
        if response.get("kind") == "err":
            self._abort(f"board rejected {self.pending_step.get('command_name')}")

    def plan_next_action(self) -> dict[str, Any]:
        """
        函数作用：
            根据当前状态生成下一步自动调参动作。

        主要流程：
            1. ABORT 状态只返回 abort。
            2. 存在已 ACK 的 pending_step 时，对比当前窗口评分，决定保留或回滚。
            3. 没有 pending_step 时按 speed_l、speed_r、yaw_rate、line_outer 顺序选第一个可调 loop。
            4. observe 模式只返回诊断，suggest 模式只返回建议，auto-tune 模式且 auto=True 才返回 send。

        参数说明：
            无参数。

        返回值：
            返回动作字典，type 可为 wait、observe、suggest、send、rollback、abort。
        """
        if self.state == "ABORT":
            return {"type": "abort", "reason": self.abort_reason}

        if self.pending_step is not None and self.pending_step.get("ack_received"):
            loop_id = str(self.pending_step.get("loop_id", ""))
            ack_sample_count = int(self.pending_step.get("ack_sample_count", 0))
            if len(self.samples_by_loop.get(loop_id, [])) <= ack_sample_count:
                return {
                    "type": "wait",
                    "state": self.state,
                    "loop_id": loop_id,
                    "reason": "waiting for post-ACK telemetry",
                    "command": self.pending_step["command"],
                }
            return self._evaluate_pending_step()

        if self.pending_step is not None:
            return {
                "type": "wait",
                "state": self.state,
                "reason": "waiting for ACK",
                "command": self.pending_step["command"],
            }

        loop_id = self._select_next_loop()
        if loop_id is None:
            return {"type": "wait", "state": self.state, "reason": "waiting for loop config and telemetry"}

        score = self.score_loop(loop_id)
        self.scores[loop_id] = score
        proposal = self._build_step(loop_id, score)

        if self.config.mode == "observe":
            self.state = "OBSERVE_BASELINE"
            return {"type": "observe", "loop_id": loop_id, "score": score}

        if self.config.mode == "suggest" or not self.config.auto:
            self.state = "PROPOSE_STEP"
            return {"type": "suggest", "loop_id": loop_id, "score": score, "command": proposal["command"]}

        self.pending_step = proposal
        self.state = "SEND_STEP"
        return {
            "type": "send",
            "loop_id": loop_id,
            "score": score,
            "command": proposal["command"],
            "reason": proposal["reason"],
        }

    def score_loop(self, loop_id: str) -> dict[str, float]:
        """
        函数作用：
            计算某个 loop 最近窗口的调参评分指标。

        主要流程：
            从最近样本中统计平均误差、最大误差、过零次数、饱和比例、抗饱和比例、传感器异常比例和综合分。

        参数说明：
            loop_id 为目标环路。

        返回值：
            返回指标字典；score 越低表示效果越好。
        """
        samples = self.samples_by_loop.get(loop_id, [])[-50:]
        if not samples:
            return {
                "mean_abs_error": float("inf"),
                "max_abs_error": float("inf"),
                "zero_crossings": 0.0,
                "sat_ratio": 1.0,
                "anti_windup_ratio": 1.0,
                "sensor_bad_ratio": 1.0,
                "line_lost_count": float(int(self.latest_sens.get("line_lost", 0)) if self.latest_sens else 0),
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
        line_lost_count = float(int(self.latest_sens.get("line_lost", 0)) if self.latest_sens else 0)
        mean_abs_error = sum(abs_errors) / len(abs_errors)
        max_abs_error = max(abs_errors)
        score = mean_abs_error + 0.25 * max_abs_error + zero_crossings * 2.0
        score += sat_ratio * 20.0 + anti_ratio * 10.0 + sensor_bad_ratio * 100.0 + line_lost_count * 100.0

        return {
            "mean_abs_error": mean_abs_error,
            "max_abs_error": max_abs_error,
            "zero_crossings": float(zero_crossings),
            "sat_ratio": sat_ratio,
            "anti_windup_ratio": anti_ratio,
            "sensor_bad_ratio": sensor_bad_ratio,
            "line_lost_count": line_lost_count,
            "score": score,
        }

    def _select_next_loop(self) -> str | None:
        """
        函数作用：
            按串级调参顺序选择下一个可调 loop。

        主要流程：
            只选择同时具备 CFGX 配置和 PIDX 样本、且尚未完成的 loop。

        参数说明：
            无参数。

        返回值：
            返回 loop_id；没有可调 loop 时返回 None。
        """
        for loop_id in self.loop_order:
            if loop_id in self.completed_loops:
                continue
            if loop_id not in self.loop_configs:
                continue
            if not self.samples_by_loop.get(loop_id):
                continue
            return loop_id
        return None

    def _build_step(self, loop_id: str, score: dict[str, float]) -> dict[str, Any]:
        """
        函数作用：
            基于当前评分和配置生成一次小步 PID 参数修改。

        主要流程：
            默认仅调整 Kp：误差较大且未明显饱和时按 max_step 增大，否则按 max_step 减小。
            这是保守首版策略，确保每次只改一个 loop 的一组三参数。

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
        if score["sat_ratio"] > 0.2 or score["zero_crossings"] >= 2.0:
            factor = 1.0 - step
            reason = "reduce kp due to saturation or oscillation"
        else:
            factor = 1.0 + step
            reason = "increase kp to reduce mean error"
        new_kp = max(0.0, old_kp * factor)
        command = build_set_pidx_command(loop_id, new_kp, old_ki, old_kd)
        metadata = extract_command_metadata(command)
        return {
            **metadata,
            "loop_id": loop_id,
            "old": {"kp": old_kp, "ki": old_ki, "kd": old_kd},
            "new": {"kp": new_kp, "ki": old_ki, "kd": old_kd},
            "baseline_score": score,
            "reason": reason,
            "ack_received": False,
        }

    def _evaluate_pending_step(self) -> dict[str, Any]:
        """
        函数作用：
            对已 ACK 的参数修改观察结果并决定保留或回滚。

        主要流程：
            计算当前窗口评分；若综合分高于基线且允许回滚，则生成旧参数 SET_PIDX 回滚命令。

        参数说明：
            无参数，使用 self.pending_step。

        返回值：
            返回 keep 或 rollback 动作字典。
        """
        assert self.pending_step is not None
        loop_id = str(self.pending_step["loop_id"])
        current_score = self.score_loop(loop_id)
        baseline = float(self.pending_step["baseline_score"]["score"])
        current = float(current_score["score"])
        if self.config.rollback_on_regression and current > baseline:
            old = self.pending_step["old"]
            command = build_set_pidx_command(loop_id, old["kp"], old["ki"], old["kd"])
            record = {
                "loop_id": loop_id,
                "command": command,
                "baseline_score": self.pending_step["baseline_score"],
                "current_score": current_score,
                "reason": "score regression",
            }
            self.rollback_history.append(record)
            self.completed_loops.add(loop_id)
            self.pending_step = None
            self.state = "KEEP_OR_ROLLBACK"
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
                    return 0 if parsed["kind"] == "ack" else 1
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
        执行串级 PID 自动调参 CLI。

    主要流程：
        1. 解析串口并创建 AutoTuneController。
        2. 可选发送 `{CMD}GET_ALL_CFG`，促使板端回传全部 `{CFGX}`。
        3. 持续读取 `{PIDX}`、`{CFGX}`、`{SENS}`、`{ACK}`、`{ERR}` 帧并推进状态机。
        4. observe/suggest 模式只输出动作；auto-tune 模式才会自动发送 SET_PIDX 或回滚命令。

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
        rollback_on_regression=args.rollback_on_regression,
    )
    controller = AutoTuneController(config)
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    frame_count = 0
    last_printed_action: str | None = None

    try:
        with open_serial(port, baud, timeout=0.5) as ser:
            if args.request_config:
                # GET_ALL_CFG 是只读同步请求，用于让多环板端立即发送 CFGX 配置快照。
                ser.write(normalize_command("{CMD}GET_ALL_CFG").encode("ascii"))
                ser.flush()

            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if args.count > 0 and frame_count >= args.count:
                    break

                raw = ser.readline()
                if not raw:
                    continue
                line = decode_line(raw)
                if not line:
                    continue
                parsed = parse_frame(line)
                frame_count += 1

                if parsed.get("kind") in ("ack", "err"):
                    controller.handle_response(parsed)
                else:
                    controller.ingest_frame(parsed)

                action = controller.plan_next_action()
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

    autotune_parser = subparsers.add_parser("autotune", help="Run cascade PID observe/suggest/auto-tune workflow.")
    autotune_parser.add_argument("--port", help="Serial port, for example COM5.")
    autotune_parser.add_argument("--baud", type=int, default=115200, help="Baud rate when --port is used.")
    autotune_parser.add_argument("--auto", action="store_true", help="Auto-detect port before tuning.")
    autotune_parser.add_argument("--baud-rates", help="Comma-separated baud rates for --auto.")
    autotune_parser.add_argument("--sample-seconds", type=float, default=2.0, help="Auto-detect sample seconds.")
    autotune_parser.add_argument("--max-lines", type=int, default=20, help="Auto-detect max lines.")
    autotune_parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports in --auto mode.")
    autotune_parser.add_argument(
        "--profile",
        default="line-car-cascade",
        choices=["line-car-cascade"],
        help="Cascade loop profile; default line-car-cascade.",
    )
    autotune_parser.add_argument(
        "--mode",
        default="observe",
        choices=["observe", "suggest", "auto-tune"],
        help="observe only reads scores, suggest prints commands, auto-tune sends and rolls back automatically.",
    )
    autotune_parser.add_argument("--max-step", type=float, default=0.10, help="Maximum one-step kp/ki/kd ratio change.")
    autotune_parser.add_argument("--window-seconds", type=float, default=3.0, help="Scoring window length in seconds.")
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
        help="Send read-only {CMD}GET_ALL_CFG at session start.",
    )
    autotune_parser.add_argument(
        "--no-request-config",
        dest="request_config",
        action="store_false",
        help="Do not send GET_ALL_CFG automatically.",
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

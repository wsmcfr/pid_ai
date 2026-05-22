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
from typing import Iterable

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - depends on local environment
    print("pyserial is required. Install with: python -m pip install pyserial", file=sys.stderr)
    raise SystemExit(2) from exc


DEFAULT_BAUD_RATES = [115200, 921600, 57600, 38400, 19200, 9600]
PROTOCOL_PREFIXES = ("{PID}", "{CFG}", "{ACK}", "{ERR}", "{STAT}", "{EVT}")
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
}
NON_NEGATIVE_INTEGER_FIELDS = {
    "seq",
    "ms",
    "fault",
    "profile_id",
    "version",
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
    if field_name in INTEGER_FIELDS:
        return int(value)

    parsed = float(value)
    if not math.isfinite(parsed):
        # Python 可以把 "nan" 和 "inf" 解析成 float；这里必须拦截，避免坏帧进入有效遥测状态。
        raise ValueError(f"{field_name} must be finite")
    return parsed


def validate_named_numeric_data(kind: str, data: dict[str, int | float]) -> str | None:
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
        if value is not None and value < 0:
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
            field_name: parse_number(field_name, field_value.strip())
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
    if line.startswith("{CFG}"):
        return parse_named_numeric_frame("cfg", "{CFG}", CFG_FIELDS, line)
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

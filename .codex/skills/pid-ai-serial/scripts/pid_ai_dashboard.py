#!/usr/bin/env python3
"""
PID AI 本地 Web 上位机。

脚本作用：
    启动一个只监听本机地址的 HTTP 服务，自动或手动连接 PID AI 板端串口，
    在浏览器中显示实时 PID 波形、安全状态、参数设置和命令 ACK/ERR 历史。

主要流程：
    1. 复用 pid_ai_serial.py 的串口枚举、自动识别、协议解析和命令校验逻辑。
    2. 后台线程读取串口行，解析为 typed frame，并写入有界遥测缓冲区。
    3. HTTP API 向前端暴露端口、状态、样本和命令发送能力。
    4. 前端使用 Canvas 绘制波形，并通过用户点击显式发送 {CMD} 命令。

返回值：
    0 表示服务正常退出；非 0 表示启动参数或 HTTP 服务启动失败。
"""

from __future__ import annotations

import argparse
import copy
import json
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pid_ai_serial as serial_tool


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_MAX_SAMPLES = 1200
DEFAULT_COMMAND_HISTORY = 120


def json_safe_copy(value: Any) -> Any:
    """
    函数作用：
        复制只包含 JSON 兼容数据的状态对象，避免 HTTP 响应暴露内部可变引用。

    主要流程：
        优先使用 copy.deepcopy；这里的数据结构只包含 dict/list/str/number/None，
        因此深拷贝足够稳定，不需要自定义序列化器。

    参数说明：
        value 为待复制的状态片段。

    返回值：
        返回与输入内容相同但引用独立的对象。
    """
    return copy.deepcopy(value)


def extract_command_name(command: str) -> str:
    """
    函数作用：
        从完整 {CMD} 命令帧中提取命令名。

    主要流程：
        1. 去掉首尾空白。
        2. 校验命令必须以 {CMD} 开头。
        3. 截取第一个逗号前的命令名并转为大写，便于和 ACK/ERR 匹配。

    参数说明：
        command 为完整命令文本，例如 "{CMD}SET_PID,1.0,0.1,0.01"。

    返回值：
        返回命令名，例如 "SET_PID"；格式非法时抛出 ValueError。
    """
    text = command.strip()
    if not text.startswith("{CMD}"):
        raise ValueError("command must start with {CMD}")

    payload = text[len("{CMD}") :].strip()
    command_name = payload.split(",", 1)[0].strip().upper()
    if not command_name:
        raise ValueError("command name is required")
    return command_name


def make_local_response(kind: str, command_name: str, detail: str) -> dict[str, Any]:
    """
    函数作用：
        构造本地上位机产生的命令错误响应。

    主要流程：
        当串口未连接或写入失败时，板端不会返回 ACK/ERR；此函数生成统一结构，
        让命令历史仍能展示失败原因，但不会伪装成板端 ACK。

    参数说明：
        kind 为本地响应类型，通常是 "local_error"。
        command_name 为命令名。
        detail 为人可读失败原因。

    返回值：
        返回可放入 command_history.response 的字典。
    """
    return {
        "kind": kind,
        "valid": False,
        "command": command_name,
        "detail": detail,
        "raw": detail,
    }


class DashboardState:
    """
    本地上位机运行状态。

    类作用：
        统一管理串口连接、最新协议帧、有界遥测缓冲区、解析诊断和命令交易历史。

    线程模型：
        HTTP 请求线程和串口读取线程会并发访问状态，因此所有共享字段都通过同一把
        RLock 保护。对外返回数据时使用深拷贝，避免调用方修改内部结构。
    """

    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES, max_command_history: int = DEFAULT_COMMAND_HISTORY):
        """
        函数作用：
            初始化 dashboard 状态容器。

        主要流程：
            创建锁、有界样本队列、命令历史队列、串口连接字段和诊断字段。

        参数说明：
            max_samples 为保留的最大 PID 样本数，至少为 1。
            max_command_history 为保留的最大命令历史条数，至少为 1。

        返回值：
            构造函数无显式返回值。
        """
        self._lock = threading.RLock()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(1, max_samples))
        self._command_history: deque[dict[str, Any]] = deque(maxlen=max(1, max_command_history))
        self._next_sample_id = 1
        self._next_command_id = 1

        # 串口资源字段只由 connect/disconnect 和读取线程修改，必须在锁内访问。
        self._serial_handle: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.connected = False
        self.connecting = False
        self.port: str | None = None
        self.baud: int | None = None
        self.connection_error: str | None = None
        self.started_at = time.time()
        self.connected_at: float | None = None
        self.last_line_at: float | None = None

        self.latest_pid: dict[str, Any] | None = None
        self.latest_cfg: dict[str, Any] | None = None
        self.latest_stat: dict[str, Any] | None = None
        self.latest_evt: dict[str, Any] | None = None
        self.parse_errors = 0
        self.last_bad_line: str | None = None

    def snapshot(self) -> dict[str, Any]:
        """
        函数作用：
            生成 dashboard 当前状态快照，供 `/api/status` 和测试读取。

        主要流程：
            在锁内复制连接字段、最新帧、诊断计数、样本计数和命令历史。

        参数说明：
            无参数。

        返回值：
            返回 JSON 兼容字典；调用者可以安全修改返回值而不影响内部状态。
        """
        with self._lock:
            latest_sample_id = self._samples[-1]["id"] if self._samples else 0
            return {
                "connected": self.connected,
                "connecting": self.connecting,
                "port": self.port,
                "baud": self.baud,
                "connection_error": self.connection_error,
                "started_at": self.started_at,
                "connected_at": self.connected_at,
                "last_line_at": self.last_line_at,
                "latest_pid": json_safe_copy(self.latest_pid),
                "latest_cfg": json_safe_copy(self.latest_cfg),
                "latest_stat": json_safe_copy(self.latest_stat),
                "latest_evt": json_safe_copy(self.latest_evt),
                "parse_errors": self.parse_errors,
                "last_bad_line": self.last_bad_line,
                "sample_count": len(self._samples),
                "latest_sample_id": latest_sample_id,
                "command_history": json_safe_copy(list(self._command_history)),
            }

    def get_samples_after(self, since_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        """
        函数作用：
            按样本自增 id 增量读取 PID 样本。

        主要流程：
            在有界样本缓冲中筛选 id 大于 since_id 的记录，并按 limit 截断。

        参数说明：
            since_id 为前端已收到的最后样本 id。
            limit 为最多返回条数；None 表示返回所有匹配样本。

        返回值：
            返回 JSON 兼容样本列表，每条样本包含 id、received_at、kind、data 等字段。
        """
        with self._lock:
            samples = [sample for sample in self._samples if sample["id"] > since_id]
            if limit is not None and limit >= 0:
                samples = samples[:limit]
            return json_safe_copy(samples)

    def ingest_line(self, line: str) -> dict[str, Any]:
        """
        函数作用：
            接收一行串口文本，解析并更新 dashboard 状态。

        主要流程：
            1. 调用现有 pid_ai_serial.parse_frame 解析协议。
            2. 合法 {PID} 写入有界样本队列并更新 latest_pid。
            3. 合法 {CFG}/{STAT}/{EVT} 更新最新状态。
            4. {ACK}/{ERR} 更新命令历史中匹配的 pending 命令。
            5. 非法帧增加解析错误计数并保留最后坏帧。

        参数说明：
            line 为不含或已去除换行符的一行串口文本。

        返回值：
            返回解析后的帧字典或样本字典；用于调试和测试。
        """
        text = line.strip()
        parsed = serial_tool.parse_frame(text)
        now = time.time()

        with self._lock:
            self.last_line_at = now

            if parsed.get("kind") == "pid" and parsed.get("valid"):
                sample = {
                    "id": self._next_sample_id,
                    "received_at": now,
                    **parsed,
                }
                self._next_sample_id += 1
                self._samples.append(sample)
                # 一次拷贝同时供 latest_pid 和返回值使用，避免对同一样本调用两次 deepcopy。
                copied = json_safe_copy(sample)
                self.latest_pid = copied
                return copied

            if parsed.get("kind") == "cfg" and parsed.get("valid"):
                record = {"received_at": now, **parsed}
                self.latest_cfg = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") == "stat" and parsed.get("valid"):
                record = {"received_at": now, **parsed}
                self.latest_stat = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") == "evt" and parsed.get("valid"):
                record = {"received_at": now, **parsed}
                self.latest_evt = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") in ("ack", "err") and parsed.get("valid"):
                self._attach_command_response_locked(parsed, now)
                return json_safe_copy(parsed)

            # 这里不填默认值，因为坏帧可能来自截断或波特率错误，伪造成 0 会误导调参。
            if text:
                self.parse_errors += 1
                self.last_bad_line = text
            return json_safe_copy(parsed)

    def record_command(self, command: str) -> dict[str, Any]:
        """
        函数作用：
            记录一条用户显式请求发送的 {CMD} 命令。

        主要流程：
            1. 复用 pid_ai_serial.normalize_command 校验命令前缀并规范换行。
            2. 提取命令名，用于后续 ACK/ERR 匹配。
            3. 写入 pending 命令历史。

        参数说明：
            command 为完整 {CMD} 文本。

        返回值：
            返回新建的命令历史条目；格式非法时抛出 ValueError。
        """
        normalized = serial_tool.normalize_command(command).strip()
        command_name = extract_command_name(normalized)

        with self._lock:
            entry = {
                "id": self._next_command_id,
                "created_at": time.time(),
                "updated_at": time.time(),
                "command": normalized,
                "command_name": command_name,
                "status": "pending",
                "response": None,
                "unsolicited": False,
            }
            self._next_command_id += 1
            self._command_history.append(entry)
            return json_safe_copy(entry)

    def mark_command_local_error(self, command_id: int, detail: str) -> dict[str, Any] | None:
        """
        函数作用：
            将命令历史中的指定命令标记为本地错误。

        主要流程：
            遍历命令历史，找到 id 匹配的条目后写入 local_error 响应。

        参数说明：
            command_id 为 record_command 返回的命令条目 id。
            detail 为失败原因，例如串口未连接或写入失败。

        返回值：
            找到命令时返回更新后的条目；未找到时返回 None。
        """
        with self._lock:
            for entry in self._command_history:
                if entry["id"] != command_id:
                    continue
                entry["status"] = "error"
                entry["updated_at"] = time.time()
                entry["response"] = make_local_response("local_error", entry["command_name"], detail)
                return json_safe_copy(entry)
        return None

    def send_command(self, command: str) -> dict[str, Any]:
        """
        函数作用：
            向已连接串口发送一条用户显式给出的 {CMD} 命令。

        主要流程：
            1. 先记录 pending 命令历史，确保后续 ACK/ERR 有匹配对象。
            2. 检查串口是否已连接。
            3. 写入 ASCII 命令并 flush。
            4. 不在此处声明成功，最终结果由读取线程收到的 {ACK}/{ERR} 决定。

        参数说明：
            command 为完整 {CMD} 文本。

        返回值：
            返回命令历史条目；串口未连接或写入失败时条目状态为 error。
        """
        entry = self.record_command(command)
        wire_text = serial_tool.normalize_command(command)

        with self._lock:
            serial_handle = self._serial_handle if self.connected else None

        if serial_handle is None:
            updated = self.mark_command_local_error(entry["id"], "Serial port is not connected.")
            return updated or entry

        try:
            serial_handle.write(wire_text.encode("ascii"))
            serial_handle.flush()
        except (OSError, serial_tool.serial.SerialException) as exc:
            updated = self.mark_command_local_error(entry["id"], f"Serial write failed: {exc}")
            return updated or entry

        return self.get_command_by_id(entry["id"]) or entry

    def get_command_by_id(self, command_id: int) -> dict[str, Any] | None:
        """
        函数作用：
            根据命令历史 id 读取命令条目。

        主要流程：
            在锁内遍历命令历史，找到后返回深拷贝。

        参数说明：
            command_id 为命令历史条目 id。

        返回值：
            找到时返回条目字典；未找到时返回 None。
        """
        with self._lock:
            for entry in self._command_history:
                if entry["id"] == command_id:
                    return json_safe_copy(entry)
        return None

    def _attach_command_response_locked(self, parsed: dict[str, Any], now: float) -> None:
        """
        函数作用：
            在已持锁状态下把 {ACK}/{ERR} 响应关联到命令历史。

        主要流程：
            从最近的命令开始反向查找同名 pending 命令，找到则更新状态；
            找不到时追加一条 unsolicited 记录，避免板端响应被静默丢弃。

        参数说明：
            parsed 为 parse_frame 输出的 ACK 或 ERR 字典。
            now 为收到响应的本机时间戳。

        返回值：
            无返回值，直接修改 command_history。
        """
        response_command = str(parsed.get("command", "")).strip().upper()
        response_kind = str(parsed.get("kind", ""))

        for entry in reversed(self._command_history):
            if entry["status"] != "pending":
                continue
            if entry["command_name"] != response_command:
                continue
            entry["status"] = response_kind
            entry["updated_at"] = now
            entry["response"] = json_safe_copy(parsed)
            return

        entry = {
            "id": self._next_command_id,
            "created_at": now,
            "updated_at": now,
            "command": "",
            "command_name": response_command,
            "status": response_kind,
            "response": json_safe_copy(parsed),
            "unsolicited": True,
        }
        self._next_command_id += 1
        self._command_history.append(entry)

    def connect(
        self,
        port: str | None = None,
        baud: int = 115200,
        auto: bool = False,
        baud_rates: list[int] | None = None,
        sample_seconds: float = 2.0,
        max_lines: int = 20,
        include_bluetooth: bool = False,
    ) -> dict[str, Any]:
        """
        函数作用：
            同步连接 PID AI 板端串口。

        主要流程：
            1. 断开旧连接，避免同一进程同时持有多个串口。
            2. 如果 auto 为 True 且未指定 port，则调用自动扫描。
            3. 打开串口并启动后台读取线程。
            4. 更新连接状态快照。

        参数说明：
            port 为手动指定串口名；None 且 auto=True 时自动识别。
            baud 为手动串口波特率。
            auto 表示是否自动扫描板端串口。
            baud_rates 为自动扫描候选波特率。
            sample_seconds 为每个端口/波特率组合的探测时长。
            max_lines 为每组探测最多读取行数。
            include_bluetooth 表示是否扫描蓝牙虚拟串口。

        返回值：
            返回连接后的状态快照；失败时返回带 connection_error 的状态快照。
        """
        self.disconnect()

        with self._lock:
            self.connecting = True
            self.connected = False
            self.connection_error = None
            self.port = port
            self.baud = baud
            self._stop_event.clear()

        try:
            resolved_port = port
            resolved_baud = baud
            if auto and not resolved_port:
                best = serial_tool.find_best_port(
                    baud_rates or list(serial_tool.DEFAULT_BAUD_RATES),
                    sample_seconds,
                    max_lines,
                    include_bluetooth,
                )
                if best is None:
                    raise RuntimeError("No PID AI board port detected.")
                resolved_port = best.port
                resolved_baud = best.baud

            if not resolved_port:
                raise RuntimeError("Serial port is required unless auto detection is enabled.")

            serial_handle = serial_tool.open_serial(resolved_port, resolved_baud, timeout=0.2)
            reader_thread = threading.Thread(
                target=self._reader_loop,
                name=f"pid-ai-serial-reader-{resolved_port}",
                daemon=True,
            )

            with self._lock:
                self._serial_handle = serial_handle
                self._reader_thread = reader_thread
                self.connected = True
                self.connecting = False
                self.port = resolved_port
                self.baud = resolved_baud
                self.connected_at = time.time()
                self.connection_error = None

            reader_thread.start()
        except Exception as exc:  # 串口扫描/打开失败需要反馈给 UI，而不能让 HTTP 服务退出。
            with self._lock:
                self._serial_handle = None
                self._reader_thread = None
                self.connected = False
                self.connecting = False
                self.connected_at = None
                self.connection_error = str(exc)

        return self.snapshot()

    def disconnect(self) -> dict[str, Any]:
        """
        函数作用：
            断开当前串口连接并释放资源。

        主要流程：
            1. 设置停止事件，让读取线程自然退出。
            2. 关闭 pyserial 句柄，打断阻塞读取。
            3. 如果调用者不是读取线程，则短时间 join，减少后台线程残留。
            4. 清理连接状态。

        参数说明：
            无参数。

        返回值：
            返回断开后的状态快照。
        """
        with self._lock:
            serial_handle = self._serial_handle
            reader_thread = self._reader_thread
            self._stop_event.set()
            self._serial_handle = None
            self._reader_thread = None

        if serial_handle is not None:
            try:
                serial_handle.close()
            except (OSError, serial_tool.serial.SerialException):
                pass

        # 读取线程自身调用 disconnect 时不能 join 自己，否则会死锁。
        if reader_thread is not None and reader_thread is not threading.current_thread():
            reader_thread.join(timeout=1.0)

        with self._lock:
            self.connected = False
            self.connecting = False
            self.connected_at = None

        return self.snapshot()

    def _reader_loop(self) -> None:
        """
        函数作用：
            后台串口读取循环。

        主要流程：
            循环读取串口行，调用 ingest_line 更新 typed 状态；遇到串口异常时记录
            connection_error 并切换为断开状态。

        参数说明：
            无参数，读取 self._serial_handle。

        返回值：
            无返回值，线程退出代表读取结束。
        """
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    serial_handle = self._serial_handle
                if serial_handle is None:
                    break

                raw = serial_handle.readline()
                if not raw:
                    continue
                line = serial_tool.decode_line(raw)
                if line:
                    self.ingest_line(line)
        except (OSError, serial_tool.serial.SerialException) as exc:
            with self._lock:
                self.connection_error = f"Serial read failed: {exc}"
        finally:
            with self._lock:
                self.connected = False
                self.connecting = False
                self._serial_handle = None


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PID AI 上位机</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --surface: #ffffff;
      --surface-2: #eef1f5;
      --line: #d8dee8;
      --text: #111827;
      --muted: #5b6472;
      --primary: #0f766e;
      --primary-weak: #d9f3ef;
      --warn: #b45309;
      --warn-weak: #fff3d6;
      --danger: #b91c1c;
      --danger-weak: #ffe4e6;
      --ok: #166534;
      --ok-weak: #dcfce7;
      --shadow: 0 8px 24px rgba(17, 24, 39, 0.08);
      color-scheme: light;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }

    button,
    input,
    select {
      font: inherit;
    }

    button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      padding: 6px 10px;
    }

    button.primary {
      border-color: var(--primary);
      background: var(--primary);
      color: #ffffff;
    }

    button.danger {
      border-color: var(--danger);
      color: var(--danger);
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }

    input,
    select {
      min-height: 34px;
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      color: var(--text);
      padding: 6px 8px;
    }

    label {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    .topbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 16px;
      align-items: center;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .brand-mark {
      width: 32px;
      height: 32px;
      border-radius: 6px;
      display: grid;
      place-items: center;
      background: var(--primary-weak);
      color: var(--primary);
      font-weight: 800;
    }

    .brand-title {
      font-size: 18px;
      font-weight: 700;
      white-space: nowrap;
    }

    .brand-subtitle {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .connection {
      display: grid;
      grid-template-columns: 120px 110px repeat(4, auto);
      gap: 8px;
      align-items: end;
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(520px, 1.6fr) minmax(360px, 0.9fr);
      gap: 12px;
      padding: 12px;
      min-height: 0;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }

    .panel-title {
      font-weight: 700;
    }

    .chart-panel {
      display: grid;
      grid-template-rows: auto auto 1fr;
      min-height: 520px;
    }

    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }

    .legend label {
      display: inline-flex;
      grid-auto-flow: column;
      align-items: center;
      gap: 6px;
      color: var(--text);
      font-size: 12px;
    }

    .legend input {
      width: auto;
      min-height: auto;
    }

    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }

    .chart-wrap {
      min-height: 0;
      padding: 10px;
    }

    canvas {
      width: 100%;
      height: 100%;
      min-height: 420px;
      display: block;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
    }

    .side {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .status-grid,
    .term-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      padding: 12px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fbfcfe;
      min-width: 0;
    }

    .metric-name {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    .metric-value {
      margin-top: 4px;
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 16px;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 9px;
      border: 1px solid var(--line);
      background: var(--surface-2);
      color: var(--muted);
      font-weight: 700;
      font-size: 12px;
    }

    .badge.ok {
      background: var(--ok-weak);
      color: var(--ok);
      border-color: #9be7b1;
    }

    .badge.warn {
      background: var(--warn-weak);
      color: var(--warn);
      border-color: #f0c36a;
    }

    .badge.danger {
      background: var(--danger-weak);
      color: var(--danger);
      border-color: #f7a9b3;
    }

    .tuning-form {
      display: grid;
      gap: 10px;
      padding: 12px;
    }

    .fields {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .command-preview {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #111827;
      color: #e5e7eb;
      padding: 8px;
      font-family: Consolas, "Cascadia Mono", monospace;
      overflow-wrap: anywhere;
      min-height: 38px;
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .history {
      max-height: 240px;
      overflow: auto;
      padding: 8px 12px 12px;
    }

    .history-row {
      display: grid;
      grid-template-columns: 74px 1fr;
      gap: 8px;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
    }

    .history-row:last-child {
      border-bottom: 0;
    }

    .history-command,
    .history-response {
      font-family: Consolas, "Cascadia Mono", monospace;
      overflow-wrap: anywhere;
    }

    .muted {
      color: var(--muted);
    }

    @media (max-width: 1100px) {
      .topbar {
        grid-template-columns: 1fr;
      }

      .connection {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .workspace {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 640px) {
      .workspace {
        padding: 8px;
      }

      .fields,
      .status-grid,
      .term-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .connection {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">PID</div>
        <div>
          <div class="brand-title">PID AI 上位机</div>
          <div class="brand-subtitle" id="connectionText">等待连接</div>
        </div>
        <span class="badge" id="connectionBadge">DISCONNECTED</span>
      </div>
      <div class="connection">
        <label>COM
          <select id="portSelect"></select>
        </label>
        <label>Baud
          <input id="baudInput" type="number" value="115200" min="1" />
        </label>
        <button id="refreshPortsBtn">刷新</button>
        <button id="autoConnectBtn" class="primary">自动连接</button>
        <button id="connectBtn">连接</button>
        <button id="disconnectBtn" class="danger">断开</button>
      </div>
    </header>

    <main class="workspace">
      <section class="panel chart-panel">
        <div class="panel-header">
          <div class="panel-title">实时波形</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <button id="pauseBtn">暂停</button>
            <div class="muted" id="sampleText">0 samples</div>
          </div>
        </div>
        <div class="legend" id="legend"></div>
        <div class="chart-wrap">
          <canvas id="chart" width="1200" height="620"></canvas>
        </div>
      </section>

      <aside class="side">
        <section class="panel">
          <div class="panel-header">
            <div class="panel-title">安全状态</div>
            <span class="badge" id="faultBadge">fault: --</span>
          </div>
          <div class="status-grid" id="statusGrid"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <div class="panel-title">PID 项</div>
            <span class="muted" id="frameText">seq --</span>
          </div>
          <div class="term-grid" id="termGrid"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <div class="panel-title">参数设置</div>
            <button id="getCfgBtn">GET_CFG</button>
          </div>
          <div class="tuning-form" id="tuningForm">
            <div class="fields">
              <label>kp<input id="kp" type="number" step="0.0001" /></label>
              <label>ki<input id="ki" type="number" step="0.0001" /></label>
              <label>kd<input id="kd" type="number" step="0.0001" /></label>
              <label>kf<input id="kf" type="number" step="0.0001" /></label>
              <label>target<input id="target" type="number" step="0.001" /></label>
              <label>manual_out<input id="manual_out" type="number" step="0.001" /></label>
              <label>out_min<input id="out_min" type="number" step="0.001" /></label>
              <label>out_max<input id="out_max" type="number" step="0.001" /></label>
              <label>integral_min<input id="integral_min" type="number" step="0.001" /></label>
              <label>integral_max<input id="integral_max" type="number" step="0.001" /></label>
              <label>mode
                <select id="mode">
                  <option value="0">0 STOP</option>
                  <option value="1">1 AUTO</option>
                  <option value="2">2 MANUAL</option>
                </select>
              </label>
              <label>enable
                <select id="enable">
                  <option value="0">0 DISABLE</option>
                  <option value="1">1 ENABLE</option>
                </select>
              </label>
              <label>reverse
                <select id="reverse">
                  <option value="0">0 NORMAL</option>
                  <option value="1">1 REVERSE</option>
                </select>
              </label>
              <label>sensor_ok
                <select id="sensor_ok">
                  <option value="0">0 BAD</option>
                  <option value="1">1 OK</option>
                </select>
              </label>
            </div>
            <div class="command-preview" id="commandPreview">{CMD}</div>
            <div class="actions">
              <button data-command="SET_PID" class="primary">SET_PID</button>
              <button data-command="SET_KF">SET_KF</button>
              <button data-command="SET_TARGET">SET_TARGET</button>
              <button data-command="SET_OUT_LIMIT">SET_OUT_LIMIT</button>
              <button data-command="SET_I_LIMIT">SET_I_LIMIT</button>
              <button data-command="SET_MODE">SET_MODE</button>
              <button data-command="SET_MANUAL_OUT">SET_MANUAL_OUT</button>
              <button data-command="ENABLE">ENABLE</button>
              <button data-command="SET_REVERSE">SET_REVERSE</button>
              <button data-command="SET_SENSOR_OK">SET_SENSOR_OK</button>
              <button data-command="RESET_I">RESET_I</button>
              <button data-command="CLEAR_FAULT">CLEAR_FAULT</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <div class="panel-title">命令历史</div>
            <span class="muted">ACK/ERR</span>
          </div>
          <div class="history" id="history"></div>
        </section>
      </aside>
    </main>
  </div>

  <script>
    const series = [
      { key: "target", label: "target", color: "#0f766e", visible: true },
      { key: "feedback", label: "feedback", color: "#2563eb", visible: true },
      { key: "error", label: "error", color: "#b91c1c", visible: true },
      { key: "p_out", label: "p_out", color: "#7c3aed", visible: false },
      { key: "i_out", label: "i_out", color: "#b45309", visible: false },
      { key: "d_out", label: "d_out", color: "#0891b2", visible: false },
      { key: "out_limited", label: "out_limited", color: "#166534", visible: true },
      { key: "actuator", label: "actuator", color: "#db2777", visible: true }
    ];

    const maxClientSamples = 700;
    let samples = [];
    let lastSampleId = 0;
    let latestStatus = null;
    let activePreview = "{CMD}";
    let paused = false;

    const el = (id) => document.getElementById(id);
    const fmt = (value) => {
      if (value === null || value === undefined || Number.isNaN(value)) return "--";
      if (typeof value === "number") return Math.abs(value) >= 1000 ? value.toFixed(0) : value.toFixed(3);
      return String(value);
    };

    function buildLegend() {
      const legend = el("legend");
      legend.innerHTML = "";
      for (const item of series) {
        const label = document.createElement("label");
        label.innerHTML = `
          <input type="checkbox" ${item.visible ? "checked" : ""} data-series="${item.key}">
          <span class="swatch" style="background:${item.color}"></span>${item.label}
        `;
        legend.appendChild(label);
      }
      legend.addEventListener("change", (event) => {
        const key = event.target.dataset.series;
        const item = series.find((entry) => entry.key === key);
        if (item) item.visible = event.target.checked;
        drawChart();
      });
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || response.statusText);
      }
      return data;
    }

    async function refreshPorts() {
      const data = await api("/api/ports");
      const select = el("portSelect");
      const current = select.value;
      select.innerHTML = "";
      if (!data.ports.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "无串口";
        select.appendChild(option);
        return;
      }
      for (const port of data.ports) {
        const option = document.createElement("option");
        option.value = port.device;
        option.textContent = `${port.device} ${port.description || ""}`.trim();
        select.appendChild(option);
      }
      if (current) select.value = current;
    }

    async function refreshStatus() {
      latestStatus = await api("/api/status");
      renderStatus(latestStatus);
    }

    async function refreshSamples() {
      // 暂停状态下停止拉取新样本，让画面定格在当前时刻。
      if (paused) return;
      const data = await api(`/api/samples?since=${lastSampleId}&limit=300`);
      if (data.samples.length) {
        for (const sample of data.samples) {
          samples.push(sample);
          lastSampleId = Math.max(lastSampleId, sample.id);
        }
        if (samples.length > maxClientSamples) {
          samples = samples.slice(samples.length - maxClientSamples);
        }
        drawChart();
      }
      el("sampleText").textContent = `${samples.length} samples / latest id ${lastSampleId}`;
    }

    function badgeClassForStatus(status) {
      if (!status.connected && !status.connecting) return "badge";
      if (status.connecting) return "badge warn";
      return "badge ok";
    }

    function renderStatus(status) {
      const badge = el("connectionBadge");
      badge.className = badgeClassForStatus(status);
      badge.textContent = status.connecting ? "CONNECTING" : (status.connected ? "CONNECTED" : "DISCONNECTED");
      el("connectionText").textContent = status.connection_error
        ? status.connection_error
        : (status.connected ? `${status.port} @ ${status.baud}` : "等待连接");

      const pid = status.latest_pid?.data || {};
      const cfg = status.latest_cfg?.data || {};
      const fault = pid.fault ?? cfg.fault;
      const sensorOk = pid.sensor_ok;
      const faultBadge = el("faultBadge");
      faultBadge.className = Number(fault || 0) === 0 ? "badge ok" : "badge danger";
      faultBadge.textContent = `fault: ${fmt(fault)}`;

      const statusItems = [
        ["sensor_ok", sensorOk],
        ["sat", pid.sat],
        ["anti_windup", pid.anti_windup],
        ["mode", pid.mode ?? cfg.mode],
        ["enable", pid.enable],
        ["fault", fault]
      ];
      el("statusGrid").innerHTML = statusItems.map(([name, value]) => metricHtml(name, value)).join("");

      const termItems = [
        ["target", pid.target],
        ["feedback", pid.feedback],
        ["error", pid.error],
        ["p_out", pid.p_out],
        ["i_out", pid.i_out],
        ["d_out", pid.d_out],
        ["out_raw", pid.out_raw],
        ["out_limited", pid.out_limited],
        ["actuator", pid.actuator]
      ];
      el("termGrid").innerHTML = termItems.map(([name, value]) => metricHtml(name, value)).join("");
      el("frameText").textContent = `seq ${fmt(pid.seq)} / ms ${fmt(pid.ms)}`;

      applyCfgToForm(status.latest_cfg?.data);
      applyPidToForm(status.latest_pid?.data);
      renderHistory(status.command_history || []);
    }

    function metricHtml(name, value) {
      return `<div class="metric"><div class="metric-name">${name}</div><div class="metric-value">${fmt(value)}</div></div>`;
    }

    function applyCfgToForm(cfg) {
      if (!cfg) return;
      const form = el("tuningForm");
      if (form.contains(document.activeElement)) return;
      // CFG 帧不包含 target，target 字段在 applyPidToForm 中由 latest_pid 单独同步。
      for (const key of ["kp", "ki", "kd", "kf", "out_min", "out_max", "integral_min", "integral_max", "mode", "reverse"]) {
        if (cfg[key] !== undefined && el(key)) el(key).value = cfg[key];
      }
    }

    function applyPidToForm(pid) {
      if (!pid) return;
      const form = el("tuningForm");
      if (form.contains(document.activeElement)) return;
      // 只把 PID 帧专有的运行字段（target）回填到表单，避免覆盖 CFG 同步过来的参数。
      if (pid.target !== undefined && el("target")) el("target").value = pid.target;
    }

    function renderHistory(history) {
      const container = el("history");
      if (!history.length) {
        container.innerHTML = `<div class="muted">暂无命令</div>`;
        return;
      }
      container.innerHTML = history.slice(-40).reverse().map((item) => {
        const statusClass = item.status === "ack" ? "ok" : (item.status === "err" || item.status === "error" ? "danger" : "warn");
        const response = item.response
          ? (item.response.raw || `${item.response.kind}:${item.response.detail || ""}`)
          : "等待 ACK/ERR";
        return `
          <div class="history-row">
            <span class="badge ${statusClass}">${item.status}</span>
            <div>
              <div class="history-command">${item.command || item.command_name}</div>
              <div class="history-response muted">${response}</div>
            </div>
          </div>
        `;
      }).join("");
    }

    function drawChart() {
      const canvas = el("chart");
      const ctx = canvas.getContext("2d");

      // 高 DPI 缩放：让 canvas 内部分辨率匹配设备像素，避免高分屏出现模糊。
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const cssWidth = rect.width || canvas.clientWidth || canvas.width;
      const cssHeight = rect.height || canvas.clientHeight || canvas.height;
      const targetW = Math.round(cssWidth * dpr);
      const targetH = Math.round(cssHeight * dpr);
      if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const width = cssWidth;
      const height = cssHeight;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);

      const plot = { left: 58, top: 18, right: width - 18, bottom: height - 38 };
      ctx.strokeStyle = "#d8dee8";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 6; i++) {
        const y = plot.top + (plot.bottom - plot.top) * i / 6;
        ctx.moveTo(plot.left, y);
        ctx.lineTo(plot.right, y);
      }
      ctx.stroke();

      const visible = series.filter((item) => item.visible);
      if (!samples.length || !visible.length) {
        ctx.fillStyle = "#5b6472";
        ctx.font = "18px Segoe UI";
        ctx.fillText("等待 PID 遥测帧", plot.left + 16, plot.top + 42);
        return;
      }

      const values = [];
      for (const sample of samples) {
        for (const item of visible) {
          const value = Number(sample.data?.[item.key]);
          if (Number.isFinite(value)) values.push(value);
        }
      }
      let min = Math.min(...values);
      let max = Math.max(...values);
      if (!Number.isFinite(min) || !Number.isFinite(max)) {
        min = -1;
        max = 1;
      }
      if (min === max) {
        min -= 1;
        max += 1;
      }
      const pad = (max - min) * 0.08;
      min -= pad;
      max += pad;

      // X 轴使用样本中的 ms（板端时间戳），让横轴反映真实采样间隔；
      // 缺少 ms 字段时退化为按下标线性分布，避免坐标计算异常。
      const msValues = samples
        .map((sample) => Number(sample.data?.ms))
        .filter((value) => Number.isFinite(value));
      const useMs = msValues.length === samples.length && samples.length > 0;
      const msMin = useMs ? Math.min(...msValues) : 0;
      const msMax = useMs ? Math.max(...msValues) : 1;
      const msRange = (msMax - msMin) || 1;

      ctx.fillStyle = "#5b6472";
      ctx.font = "12px Consolas";
      ctx.fillText(fmt(max), 8, plot.top + 12);
      ctx.fillText(fmt(min), 8, plot.bottom);

      // X 轴底部时间标签：左、中、右三个刻度（单位：ms）。
      if (useMs) {
        const xTicks = [
          { x: plot.left, value: msMin },
          { x: (plot.left + plot.right) / 2, value: msMin + msRange / 2 },
          { x: plot.right, value: msMax }
        ];
        for (const tick of xTicks) {
          const label = `${tick.value.toFixed(0)} ms`;
          ctx.fillText(label, Math.max(plot.left, tick.x - 28), plot.bottom + 16);
        }
      }

      for (const item of visible) {
        ctx.strokeStyle = item.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        let started = false;
        samples.forEach((sample, index) => {
          const v = Number(sample.data?.[item.key]);
          if (!Number.isFinite(v)) return;
          const ms = Number(sample.data?.ms);
          const xRatio = useMs && Number.isFinite(ms)
            ? (ms - msMin) / msRange
            : index / Math.max(1, samples.length - 1);
          const x = plot.left + (plot.right - plot.left) * xRatio;
          const y = plot.bottom - (plot.bottom - plot.top) * (v - min) / (max - min);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        });
        ctx.stroke();
      }
    }

    function value(id) {
      return el(id).value.trim();
    }

    function fixed(id) {
      const number = Number(value(id));
      if (!Number.isFinite(number)) throw new Error(`${id} 需要数字`);
      return number.toFixed(6);
    }

    function buildCommand(kind) {
      const builders = {
        SET_PID: () => `{CMD}SET_PID,${fixed("kp")},${fixed("ki")},${fixed("kd")}`,
        SET_KF: () => `{CMD}SET_KF,${fixed("kf")}`,
        SET_TARGET: () => `{CMD}SET_TARGET,${fixed("target")}`,
        SET_OUT_LIMIT: () => `{CMD}SET_OUT_LIMIT,${fixed("out_min")},${fixed("out_max")}`,
        SET_I_LIMIT: () => `{CMD}SET_I_LIMIT,${fixed("integral_min")},${fixed("integral_max")}`,
        SET_MODE: () => `{CMD}SET_MODE,${value("mode")}`,
        SET_MANUAL_OUT: () => `{CMD}SET_MANUAL_OUT,${fixed("manual_out")}`,
        ENABLE: () => `{CMD}ENABLE,${value("enable")}`,
        SET_REVERSE: () => `{CMD}SET_REVERSE,${value("reverse")}`,
        SET_SENSOR_OK: () => `{CMD}SET_SENSOR_OK,${value("sensor_ok")}`,
        RESET_I: () => "{CMD}RESET_I",
        CLEAR_FAULT: () => "{CMD}CLEAR_FAULT",
        GET_CFG: () => "{CMD}GET_CFG"
      };
      return builders[kind]();
    }

    function riskyCommandNeedsConfirm(command) {
      return command.includes("ENABLE,1") || command.includes("SET_MODE,2") || command.startsWith("{CMD}SET_OUT_LIMIT");
    }

    async function sendCommand(command) {
      activePreview = command;
      el("commandPreview").textContent = command;
      if (riskyCommandNeedsConfirm(command) && !confirm(`确认发送命令？\n${command}`)) return;
      await api("/api/command", {
        method: "POST",
        body: JSON.stringify({ command })
      });
      await refreshStatus();
    }

    function bindEvents() {
      el("refreshPortsBtn").addEventListener("click", () => refreshPorts().catch(alert));
      el("autoConnectBtn").addEventListener("click", async () => {
        await api("/api/connect", { method: "POST", body: JSON.stringify({ auto: true, baud: Number(value("baudInput")) || 115200 }) });
        await refreshStatus();
      });
      el("connectBtn").addEventListener("click", async () => {
        await api("/api/connect", {
          method: "POST",
          body: JSON.stringify({ port: value("portSelect"), baud: Number(value("baudInput")) || 115200 })
        });
        await refreshStatus();
      });
      el("disconnectBtn").addEventListener("click", async () => {
        await api("/api/disconnect", { method: "POST", body: "{}" });
        await refreshStatus();
      });
      el("getCfgBtn").addEventListener("click", () => sendCommand("{CMD}GET_CFG").catch(alert));
      document.querySelectorAll("[data-command]").forEach((button) => {
        button.addEventListener("click", () => {
          try {
            sendCommand(buildCommand(button.dataset.command)).catch(alert);
          } catch (error) {
            alert(error.message);
          }
        });
      });
      document.querySelectorAll("#tuningForm input, #tuningForm select").forEach((control) => {
        control.addEventListener("input", () => {
          el("commandPreview").textContent = activePreview;
        });
      });
      // 暂停/继续按钮：暂停时停止追加新样本，继续时下一次轮询恢复抓取。
      el("pauseBtn").addEventListener("click", () => {
        paused = !paused;
        el("pauseBtn").textContent = paused ? "继续" : "暂停";
      });
    }

    async function boot() {
      buildLegend();
      bindEvents();
      await refreshPorts();
      await refreshStatus();
      drawChart();
      setInterval(() => refreshStatus().catch(console.error), 750);
      setInterval(() => refreshSamples().catch(console.error), 180);
    }

    boot().catch((error) => {
      el("connectionText").textContent = error.message;
      console.error(error);
    });
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """
    PID AI dashboard HTTP 请求处理器。

    类作用：
        提供静态 HTML 页面和本地 JSON API。state 字段由 create_server 注入，
        每个请求处理器实例共享同一个 DashboardState。
    """

    state: DashboardState

    def log_message(self, format: str, *args: Any) -> None:
        """
        函数作用：
            覆盖默认 HTTP 日志输出。

        主要流程：
            只输出简短访问日志到 stderr，避免串口调试时刷出过多噪声。

        参数说明：
            format 和 args 为 BaseHTTPRequestHandler 默认日志参数。

        返回值：
            无返回值。
        """
        sys.stderr.write("[dashboard] " + (format % args) + "\n")

    def do_OPTIONS(self) -> None:
        """
        函数作用：
            响应浏览器预检请求。

        主要流程：
            返回允许本机页面访问 JSON API 的基础 CORS 头。

        参数说明：
            无参数。

        返回值：
            无返回值。
        """
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        """
        函数作用：
            处理页面和只读 API 请求。

        主要流程：
            根据 path 分发到 HTML、ports、status 或 samples。

        参数说明：
            无参数，读取 self.path。

        返回值：
            无返回值，通过 HTTP 响应写回客户端。
        """
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.write_html(INDEX_HTML)
            return
        if parsed.path == "/api/ports":
            self.handle_ports()
            return
        if parsed.path == "/api/status":
            self.write_json(self.state.snapshot())
            return
        if parsed.path == "/api/samples":
            query = parse_qs(parsed.query)
            since = parse_int_query(query, "since", 0)
            limit = parse_int_query(query, "limit", 500)
            self.write_json({"samples": self.state.get_samples_after(since, limit)})
            return
        self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        """
        函数作用：
            处理会改变本地连接或发送命令的 API 请求。

        主要流程：
            读取 JSON body 后分发到 connect、disconnect 或 command。

        参数说明：
            无参数，读取 self.path 和请求 body。

        返回值：
            无返回值，通过 HTTP 响应写回客户端。
        """
        parsed = urlparse(self.path)
        try:
            payload = self.read_json_body()
            if parsed.path == "/api/connect":
                self.handle_connect(payload)
                return
            if parsed.path == "/api/disconnect":
                self.write_json(self.state.disconnect())
                return
            if parsed.path == "/api/command":
                self.handle_command(payload)
                return
            self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_ports(self) -> None:
        """
        函数作用：
            返回当前系统串口列表。

        主要流程：
            调用现有串口枚举函数，并额外标记是否蓝牙虚拟串口，便于 UI 提示。

        参数说明：
            无参数。

        返回值：
            无返回值，通过 HTTP JSON 响应返回。
        """
        ports = []
        for port in serial_tool.get_ports():
            record = asdict(port)
            record["is_bluetooth"] = serial_tool.is_bluetooth_port(port)
            ports.append(record)
        self.write_json({"ports": ports})

    def handle_connect(self, payload: dict[str, Any]) -> None:
        """
        函数作用：
            启动串口连接流程。

        主要流程：
            连接可能涉及扫描多个 COM 口，因此放到后台线程中执行，HTTP 请求立即返回
            connecting 状态，前端通过轮询 status 获取最终结果。

        参数说明：
            payload 为 JSON body，可包含 port、baud、auto、baud_rates 等字段。

        返回值：
            无返回值，通过 HTTP JSON 响应返回当前快照。
        """
        auto = bool(payload.get("auto", False))
        port = str(payload.get("port") or "").strip() or None
        baud = int(payload.get("baud") or 115200)
        baud_rates_text = payload.get("baud_rates")
        baud_rates = serial_tool.parse_baud_rates(str(baud_rates_text)) if baud_rates_text else None
        sample_seconds = float(payload.get("sample_seconds") or 2.0)
        max_lines = int(payload.get("max_lines") or 20)
        include_bluetooth = bool(payload.get("include_bluetooth", False))

        def worker() -> None:
            """
            函数作用：
                后台执行串口连接。

            主要流程：
                调用 DashboardState.connect，同步扫描和打开串口，错误由 state 记录。

            参数说明：
                无参数，闭包捕获 API payload。

            返回值：
                无返回值。
            """
            self.state.connect(
                port=port,
                baud=baud,
                auto=auto,
                baud_rates=baud_rates,
                sample_seconds=sample_seconds,
                max_lines=max_lines,
                include_bluetooth=include_bluetooth,
            )

        with self.state._lock:
            already_connecting = self.state.connecting
            self.state.connecting = True
            self.state.connection_error = None
        if not already_connecting:
            threading.Thread(target=worker, name="pid-ai-dashboard-connect", daemon=True).start()
        self.write_json(self.state.snapshot())

    def handle_command(self, payload: dict[str, Any]) -> None:
        """
        函数作用：
            发送一条用户显式提交的 {CMD} 命令。

        主要流程：
            读取 command 字段，调用 state.send_command。该方法只代表命令已写入或本地失败，
            真正板端生效仍以之后的 ACK/ERR 为准。

        参数说明：
            payload 为 JSON body，必须包含 command 字段。

        返回值：
            无返回值，通过 HTTP JSON 响应返回命令历史条目。
        """
        command = str(payload.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        entry = self.state.send_command(command)
        self.write_json({"command": entry, "status": self.state.snapshot()})

    def read_json_body(self) -> dict[str, Any]:
        """
        函数作用：
            读取并解析 POST JSON body。

        主要流程：
            根据 Content-Length 读取字节，空 body 视为 `{}`，非 JSON 或非对象抛出 ValueError。

        参数说明：
            无参数。

        返回值：
            返回 JSON 对象字典。
        """
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def write_html(self, content: str) -> None:
        """
        函数作用：
            写回 HTML 响应。

        主要流程：
            使用 UTF-8 编码，设置 no-store，避免调试时浏览器缓存旧页面。

        参数说明：
            content 为 HTML 文本。

        返回值：
            无返回值。
        """
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        """
        函数作用：
            写回 JSON 响应。

        主要流程：
            将 payload 序列化为 UTF-8 JSON，并附加本地访问需要的基础响应头。

        参数说明：
            payload 为 JSON 兼容字典。
            status 为 HTTP 状态码，默认 200。

        返回值：
            无返回值。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_int_query(query: dict[str, list[str]], key: str, default: int) -> int:
    """
    函数作用：
        从 URL query 中解析整数参数。

    主要流程：
        读取第一个值并尝试转换为 int；缺失或非法时返回默认值。

    参数说明：
        query 为 parse_qs 输出。
        key 为参数名。
        default 为默认整数。

    返回值：
        返回解析后的整数。
    """
    try:
        return int(query.get(key, [str(default)])[0])
    except (TypeError, ValueError):
        return default


def choose_http_port(host: str, preferred_port: int) -> int:
    """
    函数作用：
        为本地 HTTP 服务选择可用端口。

    主要流程：
        先尝试用户指定端口；如果被占用，则让系统分配一个空闲端口。

    参数说明：
        host 为监听地址。
        preferred_port 为期望端口；0 表示直接由系统分配。

    返回值：
        返回可绑定的端口号。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, preferred_port))
            return int(probe.getsockname()[1])
        except OSError:
            if preferred_port == 0:
                raise

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def create_server(host: str, port: int, state: DashboardState) -> ThreadingHTTPServer:
    """
    函数作用：
        创建 dashboard HTTP 服务实例。

    主要流程：
        动态生成绑定了 state 的 RequestHandler 子类，然后创建 ThreadingHTTPServer。

    参数说明：
        host 为监听地址。
        port 为监听端口。
        state 为共享 DashboardState。

    返回值：
        返回已创建但尚未 serve_forever 的 HTTP server。
    """
    class BoundDashboardRequestHandler(DashboardRequestHandler):
        """绑定共享 DashboardState 的请求处理器。"""

    BoundDashboardRequestHandler.state = state
    return ThreadingHTTPServer((host, port), BoundDashboardRequestHandler)


def start_auto_connect(args: argparse.Namespace, state: DashboardState) -> None:
    """
    函数作用：
        根据 CLI 参数启动后台自动连接。

    主要流程：
        如果用户传入 --auto 或 --serial-port，则创建后台线程调用 state.connect，
        避免串口扫描阻塞浏览器页面打开。

    参数说明：
        args 为 argparse 解析后的命令行参数。
        state 为共享 DashboardState。

    返回值：
        无返回值。
    """
    if not args.auto and not args.serial_port:
        return

    baud_rates = serial_tool.parse_baud_rates(args.baud_rates) if args.baud_rates else None

    def worker() -> None:
        """
        函数作用：
            后台执行 CLI 初始连接。

        主要流程：
            使用命令行中的串口参数调用 state.connect，失败原因写入状态快照。

        参数说明：
            无参数，闭包捕获 args。

        返回值：
            无返回值。
        """
        state.connect(
            port=args.serial_port,
            baud=args.baud,
            auto=args.auto,
            baud_rates=baud_rates,
            sample_seconds=args.sample_seconds,
            max_lines=args.max_lines,
            include_bluetooth=args.include_bluetooth,
        )

    with state._lock:
        state.connecting = True
        state.connection_error = None
    threading.Thread(target=worker, name="pid-ai-dashboard-initial-connect", daemon=True).start()


def build_parser() -> argparse.ArgumentParser:
    """
    函数作用：
        构建 dashboard 命令行解析器。

    主要流程：
        注册 HTTP 监听参数、浏览器打开参数、串口自动连接参数和缓冲区大小参数。

    参数说明：
        无参数。

    返回值：
        返回 argparse.ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(description="Launch the PID AI local serial dashboard.")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST, help="HTTP host, default 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="Preferred HTTP port; choose free port if busy.")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES, help="Maximum PID samples kept in memory.")

    parser.add_argument("--auto", action="store_true", help="Auto-detect and connect to the PID AI board after launch.")
    parser.add_argument("--serial-port", help="Serial port to connect after launch, for example COM5.")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate when --serial-port is used.")
    parser.add_argument("--baud-rates", help="Comma-separated baud rates for --auto.")
    parser.add_argument("--sample-seconds", type=float, default=2.0, help="Auto-detect sample seconds per port/baud.")
    parser.add_argument("--max-lines", type=int, default=20, help="Auto-detect max lines per port/baud.")
    parser.add_argument("--include-bluetooth", action="store_true", help="Also scan Bluetooth virtual ports in auto mode.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    函数作用：
        dashboard 脚本入口。

    主要流程：
        1. 解析命令行参数。
        2. 创建状态对象和 HTTP 服务。
        3. 可选启动后台串口连接和浏览器。
        4. 阻塞运行 HTTP 服务，直到用户中断。

    参数说明：
        argv 为可选命令行参数列表；None 表示使用 sys.argv。

    返回值：
        正常退出返回 0；服务启动失败时返回 1。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    state = DashboardState(max_samples=args.max_samples)
    http_port = choose_http_port(args.host, args.port)
    server = create_server(args.host, http_port, state)
    url = f"http://{args.host}:{http_port}/"

    print(f"PID AI dashboard: {url}", flush=True)
    start_auto_connect(args, state)

    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PID AI dashboard...", file=sys.stderr)
    finally:
        state.disconnect()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

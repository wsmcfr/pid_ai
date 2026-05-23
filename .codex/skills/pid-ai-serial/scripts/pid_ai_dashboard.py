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
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pid_ai_serial as serial_tool


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_MAX_SAMPLES = 1200
DEFAULT_COMMAND_HISTORY = 120
DEFAULT_EXPERIMENT_DIR = "experiments"
DEFAULT_EXPERIMENT_WINDOW_SECONDS = 3.0

# 这些命令会改变控制行为或运行参数，收到 ACK/ERR 后需要形成实验记录。
EXPERIMENT_RECORD_COMMANDS = {
    "SET_PID",
    "SET_PIDX",
    "SET_KF",
    "SET_KFX",
    "SET_TARGET",
    "SET_TARGETX",
    "SET_OUT_LIMIT",
    "SET_OUT_LIMITX",
    "SET_I_LIMIT",
    "SET_I_LIMITX",
    "SET_MODE",
    "SET_MANUAL_OUT",
    "ENABLE",
    "ENABLEX",
    "SET_REVERSE",
    "SET_SENSOR_OK",
    "RESET_I",
    "RESET_IX",
    "RESET",
    "CLEAR_FAULT",
}


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
    metadata = serial_tool.extract_command_metadata(command)
    return str(metadata["command_name"])


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


def sanitize_filename_part(value: str) -> str:
    """
    函数作用：
        将命令名、loop_id 等短文本转换为可安全放入文件名的片段。

    主要流程：
        逐字符保留 ASCII 字母、数字、下划线、连字符和点号；其他字符替换为下划线。
        如果输入为空，则返回 `none`，避免生成空文件名片段。

    参数说明：
        value 为待转换文本，通常来自已校验的命令元数据。

    返回值：
        返回安全文件名片段，不包含路径分隔符。
    """
    safe = "".join(
        char if (char.isascii() and (char.isalnum() or char in ("_", "-", "."))) else "_"
        for char in str(value)
    )
    return safe or "none"


def score_experiment_samples(samples: list[dict[str, Any]]) -> dict[str, float] | None:
    """
    函数作用：
        为实验记录中的曲线窗口计算基础质量指标。

    主要流程：
        从 typed `{PID}` / `{PIDX}` 样本的 `data` 中读取 error、sat、anti_windup 和
        sensor_ok，统计平均绝对误差、最大绝对误差、安全比例和综合分。这里的分数用于
        人工对比，不替代 AutoTuneController 的 ACK 后决策逻辑。

    参数说明：
        samples 为实验窗口内的样本列表；允许为空。

    返回值：
        有样本时返回指标字典；无样本时返回 None。
    """
    if not samples:
        return None

    errors = [float(sample.get("data", {}).get("error", 0.0)) for sample in samples]
    abs_errors = [abs(error) for error in errors]
    sat_ratio = sum(1 for sample in samples if int(sample.get("data", {}).get("sat", 0)) != 0) / len(samples)
    anti_ratio = (
        sum(1 for sample in samples if int(sample.get("data", {}).get("anti_windup", 0)) != 0) / len(samples)
    )
    sensor_bad_ratio = (
        sum(1 for sample in samples if int(sample.get("data", {}).get("sensor_ok", 1)) != 1) / len(samples)
    )
    mean_abs_error = sum(abs_errors) / len(abs_errors)
    max_abs_error = max(abs_errors)
    score = mean_abs_error + 0.25 * max_abs_error + sat_ratio * 20.0 + anti_ratio * 10.0
    score += sensor_bad_ratio * 100.0

    return {
        "sample_count": float(len(samples)),
        "mean_abs_error": mean_abs_error,
        "max_abs_error": max_abs_error,
        "sat_ratio": sat_ratio,
        "anti_windup_ratio": anti_ratio,
        "sensor_bad_ratio": sensor_bad_ratio,
        "score": score,
    }


class ExperimentRecorder:
    """
    参数修改实验记录器。

    类作用：
        把一次 `{CMD}` 参数修改事务转成可回放的 JSON 文件，记录 ACK/ERR、改参前曲线、
        ACK 后曲线、配置快照和基础评分。该类不直接解析串口文本，只消费 DashboardState
        已验证的 typed 状态，避免落盘逻辑重复实现协议解析。

    线程模型：
        DashboardState 在持有自身 RLock 时调用本类方法，因此本类内部不再加锁。文件写入
        失败只记录错误摘要，不影响串口读取和命令 ACK 处理。
    """

    def __init__(self, directory: str | Path | None, window_seconds: float):
        """
        函数作用：
            初始化实验记录器。

        主要流程：
            保存根目录、窗口秒数和内存索引。directory 为 None 时关闭落盘，但仍能返回
            disabled 摘要，便于单元测试构造纯内存状态对象。

        参数说明：
            directory 为实验 JSON 输出目录；None 表示禁用记录。
            window_seconds 为每次改参前后各截取的曲线窗口长度，单位秒。

        返回值：
            构造函数无显式返回值。
        """
        self.directory = Path(directory) if directory is not None else None
        self.window_seconds = max(float(window_seconds), 0.001)
        self._records_by_command_id: dict[int, dict[str, Any]] = {}
        self._paths_by_command_id: dict[int, Path] = {}
        self._latest_summary: dict[str, Any] | None = None
        self._record_count = 0
        self._write_errors: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        """
        函数作用：
            返回实验记录功能摘要，供 `/api/status` 展示。

        主要流程：
            复制启用状态、目录、窗口秒数、记录数量、最近记录摘要和最近写入错误。

        参数说明：
            无参数。

        返回值：
            返回 JSON 兼容字典。
        """
        return {
            "enabled": self.directory is not None,
            "directory": str(self.directory) if self.directory is not None else None,
            "window_seconds": self.window_seconds,
            "record_count": self._record_count,
            "latest_record": json_safe_copy(self._latest_summary),
            "write_errors": json_safe_copy(self._write_errors[-5:]),
        }

    def start_command(
        self,
        entry: dict[str, Any],
        samples: list[dict[str, Any]],
        loops: dict[str, dict[str, Any]],
        latest_cfg: dict[str, Any] | None,
        now: float,
    ) -> None:
        """
        函数作用：
            在命令进入 pending 历史时创建实验记录草稿。

        主要流程：
            1. 只处理会改变控制行为的命令。
            2. 根据 loop_id 选择同环路的前置样本窗口。
            3. 保存当前 CFG/CFGX 作为改参前配置快照。
            4. 立即写入 pending JSON，防止后续异常导致事务完全丢失。

        参数说明：
            entry 为 command_history 中的新命令条目。
            samples 为当前有界遥测样本快照。
            loops 为多环状态表，用于读取对应 loop 的 CFGX。
            latest_cfg 为单环最新 CFG。
            now 为命令创建时间戳。

        返回值：
            无返回值；内部索引和文件会被更新。
        """
        if self.directory is None:
            return

        command_name = str(entry.get("command_name", "")).upper()
        if command_name not in EXPERIMENT_RECORD_COMMANDS:
            return

        loop_id = entry.get("loop_id")
        before_samples = self._select_window_samples(samples, loop_id, now)
        before_config = self._select_config(loop_id, loops, latest_cfg)
        record_id = self._make_record_id(entry, now)
        record = {
            "schema_version": 1,
            "record_id": record_id,
            "created_at": now,
            "updated_at": now,
            "window_seconds": self.window_seconds,
            "status": "pending",
            "command": {
                "id": entry.get("id"),
                "command": entry.get("command"),
                "command_name": command_name,
                "loop_id": loop_id,
                "reason": entry.get("reason"),
                "created_at": entry.get("created_at"),
            },
            "response": None,
            "before_config": json_safe_copy(before_config),
            "after_config": None,
            "before_samples": before_samples,
            "after_samples": [],
            "result": {
                "status": "pending",
                "before_score": score_experiment_samples(before_samples),
                "after_score": None,
            },
        }
        command_id = int(entry["id"])
        self._records_by_command_id[command_id] = record
        self._paths_by_command_id[command_id] = self.directory / f"{record_id}.json"
        self._record_count += 1
        self._write_record(command_id)

    def attach_response(self, entry: dict[str, Any], response: dict[str, Any], now: float) -> None:
        """
        函数作用：
            把 ACK/ERR 响应写入对应实验记录。

        主要流程：
            查找命令 id 对应的记录；ACK 表示参数已被板端接受，ERR 表示本次修改失败。
            两者都会更新 result.status 并立即落盘。

        参数说明：
            entry 为已更新响应的命令历史条目。
            response 为解析后的 ACK/ERR 帧。
            now 为收到响应的本机时间戳。

        返回值：
            无返回值。
        """
        record = self._records_by_command_id.get(int(entry["id"]))
        if record is None:
            return

        status = str(entry.get("status", response.get("kind", "unknown")))
        record["updated_at"] = now
        record["status"] = status
        record["response"] = json_safe_copy(response)
        record["result"]["status"] = status
        if status == "ack":
            record["ack_at"] = now
        self._write_record(int(entry["id"]))

    def attach_local_error(self, entry: dict[str, Any], detail: str, now: float) -> None:
        """
        函数作用：
            将串口未连接或本地写入失败写入实验记录。

        主要流程：
            本地错误不是板端 ERR，但仍属于一次失败的参数修改尝试，必须保留原因供排查。

        参数说明：
            entry 为命令历史条目。
            detail 为本地错误说明。
            now 为错误发生时间戳。

        返回值：
            无返回值。
        """
        record = self._records_by_command_id.get(int(entry["id"]))
        if record is None:
            return

        record["updated_at"] = now
        record["status"] = "error"
        record["response"] = make_local_response("local_error", str(entry.get("command_name", "")), detail)
        record["result"]["status"] = "error"
        self._write_record(int(entry["id"]))

    def observe_sample(self, sample: dict[str, Any], now: float) -> None:
        """
        函数作用：
            将 ACK 后的新遥测样本追加到仍处于后置窗口内的实验记录。

        主要流程：
            只处理已 ACK 的记录；按 loop_id 过滤样本；超过 ACK 后窗口的样本不再写入。
            每次追加后重新计算 after_score 并落盘，使断电或进程退出前已有数据不会丢失。

        参数说明：
            sample 为 DashboardState 已验证并保存的 PID/PIDX 样本。
            now 为样本接收时间戳。

        返回值：
            无返回值。
        """
        for command_id, record in list(self._records_by_command_id.items()):
            if record.get("status") != "ack":
                continue
            ack_at = record.get("ack_at")
            if not isinstance(ack_at, (int, float)):
                continue
            if now - float(ack_at) > self.window_seconds:
                continue
            if not self._sample_matches_loop(sample, record.get("command", {}).get("loop_id")):
                continue

            record["after_samples"].append(json_safe_copy(sample))
            record["updated_at"] = now
            record["result"]["after_score"] = score_experiment_samples(record["after_samples"])
            self._write_record(command_id)

    def observe_config(self, config_record: dict[str, Any], now: float) -> None:
        """
        函数作用：
            在 ACK 后收到新的 CFG/CFGX 时更新实验记录的 after_config。

        主要流程：
            只更新已 ACK 且仍在后置窗口内的记录；分环命令要求 CFGX 的 loop_id 匹配。

        参数说明：
            config_record 为 DashboardState 保存的 CFG/CFGX 记录。
            now 为配置帧接收时间戳。

        返回值：
            无返回值。
        """
        for command_id, record in list(self._records_by_command_id.items()):
            if record.get("status") != "ack":
                continue
            ack_at = record.get("ack_at")
            if not isinstance(ack_at, (int, float)):
                continue
            if now - float(ack_at) > self.window_seconds:
                continue
            if not self._config_matches_loop(config_record, record.get("command", {}).get("loop_id")):
                continue

            record["after_config"] = json_safe_copy(config_record)
            record["updated_at"] = now
            self._write_record(command_id)

    def attach_autotune_action(self, command_id: int | None, action: dict[str, Any], now: float) -> None:
        """
        函数作用：
            把自动调参 keep/rollback 决策补充进对应实验记录。

        主要流程：
            使用 dashboard 保存的 last_sent_command_id 定位刚被评估的 step 命令，写入
            AutoTuneController 给出的 baseline/current score 和动作类型。手动改参没有该字段。

        参数说明：
            command_id 为最近一次 auto-tune step 命令 id；None 表示无可关联记录。
            action 为 AutoTuneController.plan_next_action 返回的动作。
            now 为动作生成时间戳。

        返回值：
            无返回值。
        """
        if command_id is None:
            return
        record = self._records_by_command_id.get(int(command_id))
        if record is None:
            return

        action_type = str(action.get("type", ""))
        if action_type not in ("keep", "rollback"):
            return

        record["updated_at"] = now
        record["result"]["autotune_action"] = action_type
        record["result"]["autotune_reason"] = action.get("reason")
        record["result"]["baseline_score"] = json_safe_copy(action.get("baseline_score"))
        record["result"]["current_score"] = json_safe_copy(action.get("current_score"))
        if action_type == "rollback":
            record["result"]["rollback_command"] = action.get("command")
        self._write_record(int(command_id))

    def _select_window_samples(
        self,
        samples: list[dict[str, Any]],
        loop_id: Any,
        now: float,
    ) -> list[dict[str, Any]]:
        """
        函数作用：
            从全局有界样本中截取某次命令的前置曲线窗口。

        主要流程：
            按接收时间保留 `now - window_seconds` 之后的样本，并根据 loop_id 过滤。
            loop_id 为空时保留所有样本，适配单环或全局命令。

        参数说明：
            samples 为 DashboardState 的样本快照。
            loop_id 为命令目标环路；None 表示不按环路过滤。
            now 为命令创建时间戳。

        返回值：
            返回深拷贝后的样本列表。
        """
        cutoff = now - self.window_seconds
        selected = [
            sample
            for sample in samples
            if float(sample.get("received_at", cutoff - 1.0)) >= cutoff and self._sample_matches_loop(sample, loop_id)
        ]
        return json_safe_copy(selected)

    def _select_config(
        self,
        loop_id: Any,
        loops: dict[str, dict[str, Any]],
        latest_cfg: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """
        函数作用：
            选择命令对应的改参前配置快照。

        主要流程：
            分环命令优先读取 `loops[loop_id].latest_cfg`；普通命令使用 latest_cfg。

        参数说明：
            loop_id 为命令目标环路。
            loops 为多环状态表。
            latest_cfg 为单环最新配置。

        返回值：
            返回配置记录深拷贝；没有配置时返回 None。
        """
        if loop_id:
            loop_state = loops.get(str(loop_id), {})
            return json_safe_copy(loop_state.get("latest_cfg"))
        return json_safe_copy(latest_cfg)

    def _sample_matches_loop(self, sample: dict[str, Any], loop_id: Any) -> bool:
        """
        函数作用：
            判断一个 PID/PIDX 样本是否属于命令目标环路。

        主要流程：
            loop_id 为空时所有样本都可用于全局命令；否则只接受 data.loop_id 完全匹配的 PIDX。

        参数说明：
            sample 为样本记录。
            loop_id 为命令目标环路。

        返回值：
            匹配返回 True，否则返回 False。
        """
        if not loop_id:
            return True
        return str(sample.get("data", {}).get("loop_id", "")) == str(loop_id)

    def _config_matches_loop(self, config_record: dict[str, Any], loop_id: Any) -> bool:
        """
        函数作用：
            判断 CFG/CFGX 配置帧是否属于命令目标环路。

        主要流程：
            loop_id 为空时接受普通 CFG；loop_id 非空时只接受同名 CFGX。

        参数说明：
            config_record 为配置记录。
            loop_id 为命令目标环路。

        返回值：
            匹配返回 True，否则返回 False。
        """
        if not loop_id:
            return config_record.get("kind") == "cfg"
        return str(config_record.get("data", {}).get("loop_id", "")) == str(loop_id)

    def _make_record_id(self, entry: dict[str, Any], now: float) -> str:
        """
        函数作用：
            生成稳定且可读的实验记录文件名前缀。

        主要流程：
            使用本地时间、命令 id、命令名和 loop_id 组合；各片段先经过文件名清理。

        参数说明：
            entry 为命令历史条目。
            now 为记录创建时间戳。

        返回值：
            返回不含扩展名的记录 id。
        """
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        command_name = sanitize_filename_part(str(entry.get("command_name", "cmd")).lower())
        loop_id = sanitize_filename_part(str(entry.get("loop_id") or "global").lower())
        return f"{timestamp}-{int(entry.get('id', 0)):04d}-{command_name}-{loop_id}"

    def _write_record(self, command_id: int) -> None:
        """
        函数作用：
            将指定实验记录写入 JSON 文件。

        主要流程：
            先写同目录临时文件，再用 replace 原子替换目标文件；写入失败时仅记录错误，
            避免文件系统问题影响实时串口控制。

        参数说明：
            command_id 为命令历史 id。

        返回值：
            无返回值。
        """
        if self.directory is None:
            return
        record = self._records_by_command_id.get(command_id)
        path = self._paths_by_command_id.get(command_id)
        if record is None or path is None:
            return

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            record["path"] = str(path)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temp_path.replace(path)
            self._latest_summary = {
                "record_id": record.get("record_id"),
                "path": str(path),
                "status": record.get("status"),
                "command_name": record.get("command", {}).get("command_name"),
                "loop_id": record.get("command", {}).get("loop_id"),
                "updated_at": record.get("updated_at"),
            }
        except OSError as exc:
            message = f"{path}: {exc}"
            self._write_errors.append(message)


class DashboardState:
    """
    本地上位机运行状态。

    类作用：
        统一管理串口连接、最新协议帧、有界遥测缓冲区、解析诊断和命令交易历史。

    线程模型：
        HTTP 请求线程和串口读取线程会并发访问状态，因此所有共享字段都通过同一把
        RLock 保护。对外返回数据时使用深拷贝，避免调用方修改内部结构。
    """

    def __init__(
        self,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        max_command_history: int = DEFAULT_COMMAND_HISTORY,
        experiment_dir: str | Path | None = None,
        experiment_window_seconds: float = DEFAULT_EXPERIMENT_WINDOW_SECONDS,
    ):
        """
        函数作用：
            初始化 dashboard 状态容器。

        主要流程：
            创建锁、有界样本队列、命令历史队列、实验记录器、串口连接字段和诊断字段。

        参数说明：
            max_samples 为保留的最大 PID 样本数，至少为 1。
            max_command_history 为保留的最大命令历史条数，至少为 1。
            experiment_dir 为实验记录输出目录；None 表示不自动落盘。
            experiment_window_seconds 为每次改参前后保存的曲线窗口长度，单位秒。

        返回值：
            构造函数无显式返回值。
        """
        self._lock = threading.RLock()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(1, max_samples))
        self._command_history: deque[dict[str, Any]] = deque(maxlen=max(1, max_command_history))
        self._next_sample_id = 1
        self._next_command_id = 1
        self._loops: dict[str, dict[str, Any]] = {}
        self._experiment_recorder = ExperimentRecorder(experiment_dir, experiment_window_seconds)
        self._binary_decoder = serial_tool.BinaryFrameDecoder()
        self.api_token = secrets.token_urlsafe(32)

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
        self.latest_sens: dict[str, Any] | None = None
        self.latest_stat: dict[str, Any] | None = None
        self.latest_evt: dict[str, Any] | None = None
        self._autotune_controller = serial_tool.AutoTuneController(serial_tool.AutoTuneConfig())
        self.autotune: dict[str, Any] = {
            "enabled": False,
            "mode": "observe",
            "profile": "line-car-cascade",
            "state": "IDLE",
            "current_loop": None,
            "last_action": None,
        }
        self.scores: dict[str, Any] = {}
        self.rollback_history: list[dict[str, Any]] = []
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
                "latest_sens": json_safe_copy(self.latest_sens),
                "latest_stat": json_safe_copy(self.latest_stat),
                "latest_evt": json_safe_copy(self.latest_evt),
                "loops": json_safe_copy(self._loops),
                "autotune": json_safe_copy(self.autotune),
                "scores": json_safe_copy(self.scores),
                "rollback_history": json_safe_copy(self.rollback_history),
                "parse_errors": self.parse_errors,
                "last_bad_line": self.last_bad_line,
                "sample_count": len(self._samples),
                "latest_sample_id": latest_sample_id,
                "command_history": json_safe_copy(list(self._command_history)),
                "experiment_recording": self._experiment_recorder.snapshot(),
            }

    def tick_autotune(self, now: float | None = None) -> dict[str, Any]:
        """
        函数作用：
            在没有新串口帧到达时推进自动调参的超时检查。

        主要流程：
            只在自动调参启用且存在 pending step/rollback 时调用状态机 plan_next_action；
            这样 `/api/status` 轮询或串口空读只会触发 ACK 超时/等待状态更新，不会凭旧样本生成新写参命令。

        参数说明：
            now 为当前时间戳；None 表示使用本机当前时间。

        返回值：
            返回更新后的 dashboard 快照。
        """
        current_time = time.time() if now is None else float(now)
        with self._lock:
            if self.autotune.get("enabled") and self._autotune_controller.pending_step is not None:
                action = self._autotune_controller.plan_timeout_action(now=current_time)
                self.scores = json_safe_copy(self._autotune_controller.scores)
                self.rollback_history = json_safe_copy(self._autotune_controller.rollback_history)
                self.autotune["state"] = self._autotune_controller.state
                self.autotune["current_loop"] = action.get("loop_id")
                self.autotune["last_action"] = json_safe_copy(action)
                self.autotune["updated_at"] = current_time
        return self.snapshot()

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
        return self.ingest_parsed_frame(parsed, raw_hint=text)

    def ingest_parsed_frame(
        self,
        parsed: dict[str, Any],
        now: float | None = None,
        raw_hint: str | None = None,
    ) -> dict[str, Any]:
        """
        函数作用：
            接收一条已解析协议 frame，并更新 dashboard 状态。

        主要流程：
            合法 PID/PIDX 进入样本队列；CFG/CFGX/SENS/STAT/EVT 更新最新快照；
            ACK/ERR 先关联命令历史再推进自动调参；非法帧只增加解析错误，不污染有效状态。

        参数说明：
            parsed 为 pid_ai_serial.parse_frame 或 parse_binary_frame 输出的结构化字典。
            now 为接收时间戳；None 表示使用当前本机时间。
            raw_hint 为非法帧时写入 last_bad_line 的原始文本；为空时使用 parsed.error/raw。

        返回值：
            返回写入状态后的帧或样本深拷贝。
        """
        receive_time = time.time() if now is None else float(now)

        with self._lock:
            self.last_line_at = receive_time

            if parsed.get("kind") in ("pid", "pidx") and parsed.get("valid"):
                sample = {
                    "id": self._next_sample_id,
                    "received_at": receive_time,
                    **parsed,
                }
                self._next_sample_id += 1
                self._samples.append(sample)
                if parsed.get("kind") == "pidx":
                    loop_id = str(parsed.get("data", {}).get("loop_id", ""))
                    if loop_id:
                        loop_state = self._loops.setdefault(loop_id, {"latest_pid": None, "latest_cfg": None})
                        loop_state["latest_pid"] = json_safe_copy(sample)
                        loop_state["updated_at"] = receive_time
                self._update_autotune_locked(parsed, receive_time)
                self._experiment_recorder.observe_sample(sample, receive_time)
                copied = json_safe_copy(sample)
                self.latest_pid = copied
                return copied

            if parsed.get("kind") in ("cfg", "cfgx") and parsed.get("valid"):
                record = {"received_at": receive_time, **parsed}
                if parsed.get("kind") == "cfgx":
                    loop_id = str(parsed.get("data", {}).get("loop_id", ""))
                    if loop_id:
                        loop_state = self._loops.setdefault(loop_id, {"latest_pid": None, "latest_cfg": None})
                        loop_state["latest_cfg"] = json_safe_copy(record)
                        loop_state["updated_at"] = receive_time
                self._update_autotune_locked(parsed, receive_time)
                self.latest_cfg = json_safe_copy(record)
                self._experiment_recorder.observe_config(record, receive_time)
                return json_safe_copy(record)

            if parsed.get("kind") == "sens" and parsed.get("valid"):
                record = {"received_at": receive_time, **parsed}
                self._update_autotune_locked(parsed, receive_time)
                self.latest_sens = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") == "stat" and parsed.get("valid"):
                record = {"received_at": receive_time, **parsed}
                self.latest_stat = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") == "evt" and parsed.get("valid"):
                record = {"received_at": receive_time, **parsed}
                self.latest_evt = json_safe_copy(record)
                return json_safe_copy(record)

            if parsed.get("kind") in ("ack", "err") and parsed.get("valid"):
                # ACK/ERR 先更新命令历史，再推进 auto-tune；否则状态机可能在同一帧中生成
                # rollback 命令，随后命令历史把当前 ACK 错配到刚记录的 rollback pending。
                self._attach_command_response_locked(parsed, receive_time)
                self._update_autotune_locked(parsed, receive_time)
                return json_safe_copy(parsed)

            if raw_hint or parsed.get("raw") or parsed.get("error"):
                self.parse_errors += 1
                self.last_bad_line = raw_hint or str(parsed.get("error") or parsed.get("raw"))
            return json_safe_copy(parsed)

    def ingest_binary_frame(self, parsed: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """
        函数作用：
            接收一条已解析的二进制 typed frame，并更新 dashboard 状态。

        主要流程：
            转发到 ingest_parsed_frame，使二进制和文本帧共享完全一致的状态写入路径。

        参数说明：
            parsed 为 pid_ai_serial.parse_binary_frame 输出的结构化字典。
            now 为接收时间戳；None 表示使用当前本机时间。

        返回值：
            返回写入状态后的帧或样本深拷贝。
        """
        return self.ingest_parsed_frame(parsed, now=now)

    def ingest_bytes(self, chunk: bytes | bytearray | memoryview) -> list[dict[str, Any]]:
        """
        函数作用：
            接收串口读取到的原始二进制字节块，并把完整二进制帧写入 dashboard 状态。

        主要流程：
            1. 将 chunk 交给 BinaryFrameDecoder 做帧头查找、长度等待和 CRC 校验。
            2. 对每条切出的帧调用 ingest_binary_frame。
            3. 返回本次已处理帧的状态结果，便于测试和调试。

        参数说明：
            chunk 为串口读取到的一段原始字节。

        返回值：
            返回本次处理出的帧列表；没有完整帧时返回空列表。
        """
        parsed_frames = self._binary_decoder.feed(chunk)
        return [self.ingest_binary_frame(parsed) for parsed in parsed_frames]

    def record_command(self, command: str, reason: str | None = None) -> dict[str, Any]:
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
        metadata = serial_tool.extract_command_metadata(command)
        normalized = str(metadata["command"])
        command_name = str(metadata["command_name"])
        loop_id = metadata.get("loop_id")

        with self._lock:
            if self._has_conflicting_pending_command_locked(command_name):
                raise ValueError(
                    f"{command_name} already has a pending loop command; wait for ACK/ERR before sending another."
                )
            now = time.time()
            entry = {
                "id": self._next_command_id,
                "created_at": now,
                "updated_at": now,
                "command": normalized,
                "command_name": command_name,
                "loop_id": loop_id,
                "reason": reason,
                "status": "pending",
                "response": None,
                "unsolicited": False,
            }
            self._next_command_id += 1
            self._command_history.append(entry)
            self._experiment_recorder.start_command(
                entry,
                list(self._samples),
                self._loops,
                self.latest_cfg,
                now,
            )
            return json_safe_copy(entry)

    def _has_conflicting_pending_command_locked(self, command_name: str) -> bool:
        """
        函数作用：
            检查是否已有同名分环命令处于 pending 状态。

        主要流程：
            当前板端兼容 ACK/ERR 可能不携带 loop_id，因此 SET_PIDX,speed_l 和
            SET_PIDX,speed_r 若同时 pending，会无法可靠判断 ACK 属于哪一条。
            这里在命令入队前拒绝同名分环命令并发，直到上一条收到 ACK/ERR 或本地错误。

        参数说明：
            command_name 为 extract_command_metadata 得到的大写命令名。

        返回值：
            返回 True 表示存在冲突 pending；否则返回 False。
        """
        if command_name not in serial_tool.LOOP_COMMANDS:
            return False
        for entry in self._command_history:
            if entry.get("status") != "pending":
                continue
            if str(entry.get("command_name", "")).upper() == command_name:
                return True
        return False

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
                now = time.time()
                entry["status"] = "error"
                entry["updated_at"] = now
                entry["response"] = make_local_response("local_error", entry["command_name"], detail)
                self._experiment_recorder.attach_local_error(entry, detail, now)
                return json_safe_copy(entry)
        return None

    def send_command(self, command: str, reason: str | None = None) -> dict[str, Any]:
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
        entry = self.record_command(command, reason=reason)
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
            if not serial_tool.response_matches_pending_command(entry, parsed):
                continue
            entry["status"] = response_kind
            entry["updated_at"] = now
            entry["response"] = json_safe_copy(parsed)
            self._experiment_recorder.attach_response(entry, parsed, now)
            return

        entry = {
            "id": self._next_command_id,
            "created_at": now,
            "updated_at": now,
            "command": "",
            "command_name": response_command,
            "loop_id": None,
            "reason": None,
            "status": response_kind,
            "response": json_safe_copy(parsed),
            "unsolicited": True,
        }
        self._next_command_id += 1
        self._command_history.append(entry)

    def configure_autotune(
        self,
        enabled: bool,
        mode: str = "observe",
        profile: str = "line-car-cascade",
        max_step: float = 0.10,
        window_seconds: float = 3.0,
        ack_timeout_seconds: float = 2.0,
        min_post_ack_samples: int = serial_tool.DEFAULT_MIN_POST_ACK_SAMPLES,
        rollback_on_regression: bool = True,
    ) -> dict[str, Any]:
        """
        函数作用：
            配置 dashboard 内置自动调参状态机。

        主要流程：
            1. 校验模式、profile 和步长。
            2. 创建新的 AutoTuneController，避免旧窗口和 pending 命令污染新会话。
            3. 更新 status 中的 autotune 快照。

        参数说明：
            enabled 表示是否启用自动调参状态机。
            mode 为 observe、suggest 或 auto-tune；只有 auto-tune 会自动写命令。
            profile 为串级 profile，目前支持 line-car-cascade。
            max_step 为单次参数最大变化比例。
            window_seconds 为评分窗口秒数。
            ack_timeout_seconds 为等待板端 ACK 的最长秒数。
            min_post_ack_samples 为 ACK 后至少等待的新 PIDX 样本数，避免单点噪声决定保留或回滚。
            rollback_on_regression 表示评分变差时是否生成回滚命令。

        返回值：
            返回更新后的 dashboard 快照。
        """
        if mode not in ("observe", "suggest", "auto-tune"):
            raise ValueError("mode must be observe, suggest, or auto-tune")
        if profile != "line-car-cascade":
            raise ValueError("profile must be line-car-cascade")
        if max_step <= 0.0 or max_step > 0.5:
            raise ValueError("max_step must be within 0.0 and 0.5")
        if window_seconds <= 0.0:
            raise ValueError("window_seconds must be positive")
        if ack_timeout_seconds <= 0.0:
            raise ValueError("ack_timeout_seconds must be positive")
        if min_post_ack_samples <= 0:
            raise ValueError("min_post_ack_samples must be positive")

        config = serial_tool.AutoTuneConfig(
            auto=bool(enabled and mode == "auto-tune"),
            profile=profile,
            mode=mode,
            max_step=max_step,
            window_seconds=window_seconds,
            ack_timeout_seconds=ack_timeout_seconds,
            min_post_ack_samples=int(min_post_ack_samples),
            rollback_on_regression=rollback_on_regression,
        )
        with self._lock:
            self._autotune_controller = serial_tool.AutoTuneController(config)
            self.scores = {}
            self.rollback_history = []
            self.autotune = {
                "enabled": bool(enabled),
                "mode": mode,
                "profile": profile,
                "state": "DISCOVER" if enabled else "IDLE",
                "current_loop": None,
                "last_action": None,
                "max_step": max_step,
                "window_seconds": window_seconds,
                "ack_timeout_seconds": ack_timeout_seconds,
                "min_post_ack_samples": int(min_post_ack_samples),
                "rollback_on_regression": bool(rollback_on_regression),
            }
        return self.snapshot()

    def _update_autotune_locked(self, parsed: dict[str, Any], now: float) -> None:
        """
        函数作用：
            在已持锁状态下把协议帧喂给自动调参状态机并同步 dashboard 快照。

        主要流程：
            1. 未启用时直接返回，保证 dashboard 默认只读。
            2. ACK/ERR 走 handle_response，其余 typed frame 走 ingest_frame。
            3. 调用 plan_next_action 得到建议、写参或回滚动作。
            4. 仅当 mode=auto-tune 时自动发送动作命令，并记录 reason。

        参数说明：
            parsed 为本次解析出的协议帧。
            now 为收到该帧的本机时间戳。

        返回值：
            无返回值，直接更新 autotune、scores、rollback_history 和命令历史。
        """
        if not self.autotune.get("enabled"):
            return

        if parsed.get("kind") in ("ack", "err"):
            self._autotune_controller.handle_response(parsed, now=now)
        else:
            self._autotune_controller.ingest_frame(parsed)

        action = self._autotune_controller.plan_next_action(now=now)
        self.scores = json_safe_copy(self._autotune_controller.scores)
        self.rollback_history = json_safe_copy(self._autotune_controller.rollback_history)
        self.autotune["state"] = self._autotune_controller.state
        self.autotune["current_loop"] = action.get("loop_id")
        self.autotune["last_action"] = json_safe_copy(action)
        self.autotune["updated_at"] = now
        self._experiment_recorder.attach_autotune_action(
            self.autotune.get("last_sent_command_id"),
            action,
            now,
        )

        if action.get("type") not in ("send", "rollback"):
            return
        if self.autotune.get("mode") != "auto-tune":
            return

        entry = self.send_command(str(action["command"]), reason=str(action.get("reason") or "auto-tune"))
        self.autotune["last_sent_command_id"] = entry.get("id")
        if entry.get("status") == "error":
            self._abort_autotune_locked(str(entry.get("response", {}).get("detail") or "local send error"), now)

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
            if self.autotune.get("enabled"):
                self._abort_autotune_locked("serial disconnected", time.time())

        return self.snapshot()

    def _abort_autotune_locked(self, reason: str, now: float) -> None:
        """
        函数作用：
            在已持锁状态下中止 dashboard 自动调参会话。

        主要流程：
            1. 调用 AutoTuneController.abort 让纯状态机清理 pending 命令并进入 ABORT。
            2. 读取 abort 动作并同步到 dashboard 的 autotune/scores/rollback_history 快照。
            3. 将 enabled 置为 False，避免串口断开或本地发送失败后继续自动写参。

        参数说明：
            reason 为外部中止原因，例如 serial disconnected。
            now 为 dashboard 状态更新时间戳。

        返回值：
            无返回值，直接修改 self.autotune 等字段。
        """
        self._autotune_controller.abort(reason)
        action = self._autotune_controller.plan_next_action(now=now)
        self.scores = json_safe_copy(self._autotune_controller.scores)
        self.rollback_history = json_safe_copy(self._autotune_controller.rollback_history)
        self.autotune["enabled"] = False
        self.autotune["state"] = self._autotune_controller.state
        self.autotune["current_loop"] = action.get("loop_id")
        self.autotune["last_action"] = json_safe_copy(action)
        self.autotune["updated_at"] = now

    def _reader_loop(self) -> None:
        """
        函数作用：
            后台串口读取循环。

        主要流程：
            循环读取串口字节块，使用混合协议 decoder 同时处理文本行和二进制帧；
            遇到串口异常时记录 connection_error 并切换为断开状态。

        参数说明：
            无参数，读取 self._serial_handle。

        返回值：
            无返回值，线程退出代表读取结束。
        """
        try:
            decoder = serial_tool.ProtocolStreamDecoder()
            while not self._stop_event.is_set():
                with self._lock:
                    serial_handle = self._serial_handle
                if serial_handle is None:
                    break

                raw = serial_handle.read(256)
                if not raw:
                    self.tick_autotune()
                    continue
                for parsed in decoder.feed(raw):
                    self.ingest_parsed_frame(parsed, raw_hint=str(parsed.get("raw", "")))
        except (OSError, serial_tool.serial.SerialException) as exc:
            with self._lock:
                self.connection_error = f"Serial read failed: {exc}"
        finally:
            with self._lock:
                self.connected = False
                self.connecting = False
                self._serial_handle = None


INDEX_HTML_TEMPLATE = r"""<!doctype html>
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

    .loop-list,
    .autotune-box {
      display: grid;
      gap: 8px;
      padding: 12px;
    }

    .loop-row {
      display: grid;
      grid-template-columns: minmax(84px, 0.7fr) repeat(4, minmax(0, 1fr));
      gap: 8px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fbfcfe;
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 12px;
    }

    .autotune-controls {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
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
            <div class="panel-title">多环状态</div>
            <span class="muted" id="loopCountText">0 loops</span>
          </div>
          <div class="loop-list" id="loopList"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <div class="panel-title">自动调参</div>
            <span class="badge" id="autotuneBadge">OFF</span>
          </div>
          <div class="autotune-box">
            <div class="autotune-controls">
              <label>mode
                <select id="autotuneMode">
                  <option value="observe">observe</option>
                  <option value="suggest">suggest</option>
                  <option value="auto-tune">auto-tune</option>
                </select>
              </label>
              <label>max_step
                <input id="autotuneMaxStep" type="number" value="0.10" min="0.01" max="0.50" step="0.01" />
              </label>
              <label>window_s
                <input id="autotuneWindow" type="number" value="3.0" min="0.1" step="0.1" />
              </label>
              <label>ack_timeout_s
                <input id="autotuneAckTimeout" type="number" value="2.0" min="0.1" step="0.1" />
              </label>
              <label>post_ack_n
                <input id="autotuneMinPostAckSamples" type="number" value="3" min="1" step="1" />
              </label>
            </div>
            <div class="command-preview" id="autotunePreview">IDLE</div>
            <div class="actions">
              <button id="autotuneObserveBtn">观察</button>
              <button id="autotuneSuggestBtn">建议</button>
              <button id="autotuneAutoBtn" class="primary">自动</button>
              <button id="autotuneStopBtn" class="danger">停止</button>
            </div>
          </div>
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
    window.PID_AI_API_TOKEN = "__PID_AI_API_TOKEN__";
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

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

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
      const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {})
      };
      if ((options.method || "GET").toUpperCase() !== "GET") {
        headers["X-PID-AI-Token"] = window.PID_AI_API_TOKEN;
      }
      const response = await fetch(path, {
        ...options,
        headers
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
      renderLoops(status.loops || {});
      renderAutotune(status.autotune || {}, status.scores || {}, status.rollback_history || []);
      renderHistory(status.command_history || []);
    }

    function metricHtml(name, value) {
      return `<div class="metric"><div class="metric-name">${escapeHtml(name)}</div><div class="metric-value">${escapeHtml(fmt(value))}</div></div>`;
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

    function renderLoops(loops) {
      const entries = Object.entries(loops);
      el("loopCountText").textContent = `${entries.length} loops`;
      const container = el("loopList");
      if (!entries.length) {
        container.innerHTML = `<div class="muted">等待 PIDX/CFGX</div>`;
        return;
      }
      container.innerHTML = entries.map(([loopId, loop]) => {
        const pid = loop.latest_pid?.data || {};
        const cfg = loop.latest_cfg?.data || {};
        return `
          <div class="loop-row">
            <strong>${escapeHtml(loopId)}</strong>
            <span>kp ${escapeHtml(fmt(cfg.kp))}</span>
            <span>err ${escapeHtml(fmt(pid.error))}</span>
            <span>sat ${escapeHtml(fmt(pid.sat))}</span>
            <span>fault ${escapeHtml(fmt(pid.fault ?? cfg.fault))}</span>
          </div>
        `;
      }).join("");
    }

    function renderAutotune(autotune, scores, rollbackHistory) {
      const badge = el("autotuneBadge");
      badge.className = autotune.enabled ? (autotune.mode === "auto-tune" ? "badge warn" : "badge ok") : "badge";
      badge.textContent = autotune.enabled ? autotune.mode : "OFF";
      const action = autotune.last_action || {};
      const loopId = autotune.current_loop || action.loop_id || "--";
      const score = scores[loopId]?.score;
      const rollbackCount = rollbackHistory.length || 0;
      el("autotunePreview").textContent =
        `state=${autotune.state || "IDLE"} loop=${loopId} action=${action.type || "--"} score=${fmt(score)} rollback=${rollbackCount} command=${action.command || "--"}`;
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
        const meta = [item.loop_id ? `loop=${item.loop_id}` : "", item.reason ? `reason=${item.reason}` : ""]
          .filter(Boolean)
          .join(" ");
        return `
          <div class="history-row">
            <span class="badge ${escapeHtml(statusClass)}">${escapeHtml(item.status)}</span>
            <div>
              <div class="history-command">${escapeHtml(item.command || item.command_name)}</div>
              <div class="history-response muted">${escapeHtml(meta)}</div>
              <div class="history-response muted">${escapeHtml(response)}</div>
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
      el("autotuneObserveBtn").addEventListener("click", () => configureAutotune(true, "observe").catch(alert));
      el("autotuneSuggestBtn").addEventListener("click", () => configureAutotune(true, "suggest").catch(alert));
      el("autotuneAutoBtn").addEventListener("click", async () => {
        const ok = confirm("确认启用 auto-tune？启用后会在收到 ACK 后自动小步下发和回滚 SET_PIDX。");
        if (!ok) return;
        await configureAutotune(true, "auto-tune");
      });
      el("autotuneStopBtn").addEventListener("click", () => configureAutotune(false, "observe").catch(alert));
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

    async function configureAutotune(enabled, mode) {
      // 自动调参配置必须把 ACK 超时传给后端状态机，避免丢包时 UI 只停留在等待状态。
      await api("/api/autotune", {
        method: "POST",
        body: JSON.stringify({
          enabled,
          mode,
          profile: "line-car-cascade",
          max_step: Number(value("autotuneMaxStep")) || 0.10,
          window_seconds: Number(value("autotuneWindow")) || 3.0,
          ack_timeout_seconds: Number(value("autotuneAckTimeout")) || 2.0,
          min_post_ack_samples: Number(value("autotuneMinPostAckSamples")) || 3,
          rollback_on_regression: true
        })
      });
      await refreshStatus();
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


def build_index_html(api_token: str) -> str:
    """
    函数作用：
        将 dashboard 静态 HTML 模板实例化为包含本次进程 API token 的页面。

    主要流程：
        使用 JSON 字符串编码 token，避免特殊字符破坏 script 字面量；模板中的占位符只在服务端启动时替换。

    参数说明：
        api_token 为 DashboardState 启动时生成的随机写 API token。

    返回值：
        返回完整 HTML 文本。
    """
    encoded_token = json.dumps(str(api_token))[1:-1]
    return INDEX_HTML_TEMPLATE.replace("__PID_AI_API_TOKEN__", encoded_token)


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
            仅返回 204 表示本地服务存活，不开放跨站 CORS 读取或写入权限。
            dashboard 页面与 API 同源，正常 fetch 不需要依赖 Access-Control-Allow-Origin。

        参数说明：
            无参数。

        返回值：
            无返回值。
        """
        self.send_response(HTTPStatus.NO_CONTENT)
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
            self.write_html(build_index_html(self.state.api_token))
            return
        if parsed.path == "/api/ports":
            self.handle_ports()
            return
        if parsed.path == "/api/status":
            self.write_json(self.state.tick_autotune())
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
                self.require_write_token()
                self.handle_connect(payload)
                return
            if parsed.path == "/api/disconnect":
                self.require_write_token()
                self.write_json(self.state.disconnect())
                return
            if parsed.path == "/api/command":
                self.require_write_token()
                self.handle_command(payload)
                return
            if parsed.path == "/api/autotune":
                self.require_write_token()
                self.handle_autotune(payload)
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

    def require_write_token(self) -> None:
        """
        函数作用：
            校验本地 dashboard 写 API 的 CSRF token。

        主要流程：
            页面加载时服务端把随机 token 注入 HTML；所有 POST 请求必须通过
            `X-PID-AI-Token` 带回同一值。其他网站无法读取本地页面中的 token，
            因此不能直接诱导浏览器向本地串口服务发送写命令。

        参数说明：
            无参数，读取 self.headers 和 self.state.api_token。

        返回值：
            token 匹配时无返回；不匹配时抛出 ValueError，由 do_POST 转成 400。
        """
        token = str(self.headers.get("X-PID-AI-Token", ""))
        if not secrets.compare_digest(token, self.state.api_token):
            raise ValueError("invalid API token")

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
        reason = str(payload.get("reason") or "").strip() or None
        if not command:
            raise ValueError("command is required")
        entry = self.state.send_command(command, reason=reason)
        self.write_json({"command": entry, "status": self.state.snapshot()})

    def handle_autotune(self, payload: dict[str, Any]) -> None:
        """
        函数作用：
            配置 dashboard 内置自动调参状态机。

        主要流程：
            读取 enabled、mode、profile、max_step、window_seconds、ack_timeout_seconds、
            min_post_ack_samples 和 rollback_on_regression，调用 DashboardState.configure_autotune
            后返回最新状态快照。

        参数说明：
            payload 为 JSON body，字段均可选；未传时使用安全默认值并保持自动写参关闭。

        返回值：
            无返回值，通过 HTTP JSON 响应返回 dashboard 状态。
        """
        status = self.state.configure_autotune(
            enabled=bool(payload.get("enabled", False)),
            mode=str(payload.get("mode") or "observe"),
            profile=str(payload.get("profile") or "line-car-cascade"),
            max_step=float(payload.get("max_step") or 0.10),
            window_seconds=float(payload.get("window_seconds") or 3.0),
            ack_timeout_seconds=float(payload.get("ack_timeout_seconds") or 2.0),
            min_post_ack_samples=int(payload.get("min_post_ack_samples") or serial_tool.DEFAULT_MIN_POST_ACK_SAMPLES),
            rollback_on_regression=bool(payload.get("rollback_on_regression", True)),
        )
        self.write_json(status)

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
            将 payload 序列化为 UTF-8 JSON，并附加 no-store 等基础响应头。
            这里不设置 Access-Control-Allow-Origin，避免其他网页读取本机串口 dashboard 数据。

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
    parser.add_argument(
        "--experiment-dir",
        default=DEFAULT_EXPERIMENT_DIR,
        help="Directory for parameter-change experiment JSON records.",
    )
    parser.add_argument(
        "--experiment-window-seconds",
        type=float,
        default=DEFAULT_EXPERIMENT_WINDOW_SECONDS,
        help="Seconds of curve samples saved before and after each acknowledged parameter change.",
    )
    parser.add_argument(
        "--disable-experiment-recording",
        action="store_true",
        help="Disable automatic experiment JSON recording for parameter-change commands.",
    )

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

    experiment_dir = None if args.disable_experiment_recording else args.experiment_dir
    state = DashboardState(
        max_samples=args.max_samples,
        experiment_dir=experiment_dir,
        experiment_window_seconds=args.experiment_window_seconds,
    )
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

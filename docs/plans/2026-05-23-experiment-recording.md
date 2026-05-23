# Experiment Recording Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 保存每次上位机参数修改前后的 PID 曲线样本、命令确认结果和基础评分结果。

**Architecture:** 在 `pid_ai_dashboard.py` 中新增轻量 `ExperimentRecorder`，由 `DashboardState` 在命令入队、ACK/ERR、后续遥测样本到达时调用。记录以 JSON 文件落盘到 dashboard 启动参数指定的目录，Dashboard 状态只暴露最近记录摘要，不把文件 IO 逻辑放进前端页面。

**Tech Stack:** Python 标准库、现有 `pid_ai_serial.py` parser/command metadata、现有 `unittest` 测试。

---

### Task 1: 失败测试

**Files:**
- Modify: `.codex/skills/pid-ai-serial/tests/test_pid_ai_dashboard.py`

**Steps:**
1. 新增临时目录测试，构造 `DashboardState(experiment_dir=tmp, experiment_window_seconds=0.2)`。
2. 注入改参前 `PIDX/CFGX` 样本。
3. 记录 `SET_PIDX`，注入 ACK，再注入改参后样本。
4. 断言生成 JSON 文件，包含 `before_samples`、`after_samples`、`before_config`、`command` 和 ACK 结果。
5. 先运行该单测，确认因为构造参数或字段不存在而失败。

### Task 2: Recorder 最小实现

**Files:**
- Modify: `.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py`

**Steps:**
1. 新增 recordable 命令集合和文件名清理 helper。
2. 新增 `ExperimentRecorder`，负责事务创建、ACK/ERR 更新、样本追加和 JSON 原子写入。
3. 在 `DashboardState.record_command()` 创建 pending 实验。
4. 在 `_attach_command_response_locked()` 关联 ACK/ERR 后更新实验。
5. 在 `ingest_line()` 写入 PID/PIDX 样本后追加后置窗口样本。

### Task 3: API/CLI 与文档

**Files:**
- Modify: `.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py`
- Modify: `README.md`
- Modify: `docs/pid_ai_dashboard.md`

**Steps:**
1. `DashboardState.snapshot()` 增加 `experiment_recording` 摘要。
2. `build_parser()` 增加 `--experiment-dir`、`--disable-experiment-recording`、`--experiment-window-seconds`。
3. `main()` 用 CLI 参数开启记录。
4. README 和 dashboard 文档说明保存路径、JSON 内容和关闭方式。

### Task 4: 验证

**Commands:**
- `python -m unittest .codex.skills.pid-ai-serial.tests.test_pid_ai_dashboard -v`
- `python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v`
- `python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py`

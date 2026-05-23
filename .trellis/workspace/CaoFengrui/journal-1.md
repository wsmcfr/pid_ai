# Journal - CaoFengrui (Part 1)

> AI development session journal
> Started: 2026-05-21

---



## Session 1: Add PID AI serial dashboard

**Date**: 2026-05-21
**Task**: Add PID AI serial dashboard
**Branch**: `main`

### Summary

Implemented and pushed the PID AI serial dashboard skill, local Web upper-computer UI, parser validation, tests, and public documentation.

### Main Changes

| 项目 | 内容 |
|---|---|
| 功能 | 新增 `pid-ai-serial` 项目内 Codex skill，支持自动识别 PID AI 板端 COM 口、读取协议帧、发送显式 `{CMD}` 命令。 |
| 上位机 | 新增本地 Web dashboard，提供串口连接、Canvas 实时波形、安全状态、参数设置、命令历史和 ACK/ERR 反馈。 |
| 协议安全 | 补强 Python 串口 parser，对 `{PID}` / `{CFG}` 的字段数量、数值类型、枚举范围和非负字段做校验。 |
| 文档 | 更新 `README.md`，新增 `docs/pid_ai_dashboard.md` 和实施计划文档，说明启动方式、本地 API、错误矩阵和验证命令。 |
| 验证 | Python unittest 8 项通过，`py_compile` 通过，skill 校验通过，现有 C 回归测试通过，本地 HTTP API 无板端场景验证通过。 |

**关键文件**:
- `.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py`
- `.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py`
- `.codex/skills/pid-ai-serial/tests/test_pid_ai_dashboard.py`
- `.codex/skills/pid-ai-serial/tests/test_pid_ai_serial_parser.py`
- `docs/pid_ai_dashboard.md`
- `README.md`

**GitHub**:
- https://github.com/wsmcfr/pid_ai/commit/741407164b31a4bb76a381acfe1dfc6f79be1982


### Git Commits

| Hash | Message |
|------|---------|
| `741407164b31a4bb76a381acfe1dfc6f79be1982` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: Harden PID AI protocol parsing and telemetry validation

**Date**: 2026-05-21
**Task**: Harden PID AI protocol parsing and telemetry validation
**Branch**: `main`

### Summary

Completed the follow-up hardening pass for the PID AI serial host tooling after
code review. The session fixed response-frame validation gaps, tightened
auto-tune transaction handling, synchronized frontend specs, committed the fix,
and pushed the feature branch to GitHub.

### Main Changes

| Area | Summary |
|------|---------|
| Protocol hardening | Tightened `{CMD}` parsing so extra arguments and trailing empty arguments return `ARG_INVALID/UNEXPECTED_ARG` instead of being silently ignored. |
| Reset semantics | Updated `PIDAI_Reset()` to preserve operator configuration including `target` and `manual_out` while clearing runtime state. |
| Host parser safety | Rejected non-finite Python float values (`nan`, `inf`, `-inf`) before `{PID}` / `{CFG}` frames enter valid typed state. |
| Tests | Added C regression tests for RESET preservation, extra arguments, and trailing empty arguments; added Python parser tests for NaN/Inf rejection. |
| Documentation | Updated protocol docs and local Trellis specs with exact command argument contracts and non-finite numeric validation guidance. |
| Verification | Ran C tests, strict C builds, example build, Python unittest, py_compile, and whitespace diff checks before pushing. |

**Committed**: `712c14e fix(protocol): harden command parsing and telemetry validation`

**Notes**:
- `AGENTS.md` and `.trellis/` remain ignored local collaboration metadata, so only tracked source/docs/tests were pushed to GitHub.
- Future protocol reviews should include too-few fields, too-many fields, trailing delimiters, and cross-runtime numeric edge cases.


### Git Commits

| Hash | Message |
|------|---------|
| `712c14e` | (see git log) |

### Testing

- [OK] `python -m pytest .codex\skills\pid-ai-serial\tests` -> 34 passed
- [OK] `python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py`
- [OK] `gcc -Wall -Wextra -Werror -Iinclude src/pid_ai.c src/pid_ai_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; .\tests\test_pid_ai.exe` -> PASS: all pid_ai tests

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: PID 全面修复与算法优化

**Date**: 2026-05-22
**Task**: PID 全面修复与算法优化
**Branch**: `main`

### Summary

(Add summary)

### Main Changes

| 模块 | 变更内容 |
|------|---------|
| C 算法层 | 积分/微分归一化到物理时间（dt_ms/1000.f），ki/kd 含义与采样周期解耦 |
| C 算法层 | D 项改为对 feedback 求导（Derivative on Measurement），消除 target 阶跃尖峰 |
| C 算法层 | 抗饱和改为 Conditional Integration，比原 clamping 更稳定 |
| C 算法层 | PIDAI_Handle 新增 last_feedback 字段 |
| 协议层 | board_example GET_CFG 改为 strcmp，补充 GET_STAT 占位回复 |
| Python 后端 | scan_ports 改为 ThreadPoolExecutor 并行扫描（最多 8 线程） |
| Python 后端 | ingest_line 减少一次 json_safe_copy |
| Web 前端 | Canvas 高 DPI 支持（devicePixelRatio 缩放） |
| Web 前端 | 图表 X 轴改为 ms 时间戳 |
| Web 前端 | 新增暂停/继续波形按钮 |
| Web 前端 | target 字段从 PID 帧同步到表单 |

**修改文件**:
- `include/pid_ai.h`
- `src/pid_ai.c`
- `examples/pid_ai_board_example.c`
- `tests/test_pid_ai.c`
- `.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py`
- `.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py`

**注意**：C 算法为破坏性变更，现有板端 ki/kd 需按 dt_s 重新标定（原 ki=0.5 在 10ms 周期 → 新 ki=50）。


### Git Commits

| Hash | Message |
|------|---------|
| `9b5ce07` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: Harden PID cascade autotune transactions

**Date**: 2026-05-23
**Task**: Harden PID cascade autotune transactions
**Branch**: `feat/pid-ai-autotune`

### Summary

(Add summary)

### Main Changes

| 项目 | 内容 |
|---|---|
| 分支 | `feat/pid-ai-autotune` |
| 业务提交 | `f7b8ded fix(pid): harden cascade autotune transactions` |
| 工作范围 | 修复 PID AI 串级自动调参审查问题，补齐代码、测试、协议文档、dashboard 文档、skill 和 Trellis spec |

## 完成内容

| 模块 | 记录 |
|---|---|
| Python parser / autotune | ACK/ERR 兼容旧格式与带 `loop_id` 新格式；step/rollback 均记录发送时间并支持 `ack_timeout_seconds`；rollback 建模为独立 pending 事务；`window_seconds` 按 `received_at` 或板端 `ms` 裁剪评分窗口 |
| Dashboard | `/api/autotune` 和页面 payload 接入 `ack_timeout_seconds`；禁止同名分环命令并发 pending；ACK/ERR 先更新命令历史再推进 autotune；串口断开时 ABORT 并关闭自动写参 |
| C 协议库 | `PIDAI_ProtocolHandleCommandX()` 对无效 loop table 返回 `INTERNAL_ERROR/NO_VALID_LOOP_TABLE`；所有 `*X` 命令先查 `loop_id` 再解析后续数值，未知 loop 不被坏数字掩盖 |
| 测试 | 增加 C 回归测试、parser 状态机测试、dashboard 状态/API 测试，覆盖 ACK timeout、rollback ACK/ERR/timeout、时间窗口评分、同名 pending 冲突和无效 loop table |
| 文档/spec | README、协议文档、dashboard 文档、`pid-ai-serial` skill、Trellis backend/frontend spec 已同步 ACK 闭环、rollback 事务、`ack_timeout_seconds`、旧 ACK 并发限制和 `{CFGX}` 15 字段契约 |

## 验证

| 命令 | 结果 |
|---|---|
| `gcc -Iinclude src/pid_ai.c src/pid_ai_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; if ($LASTEXITCODE -eq 0) { .\tests\test_pid_ai.exe }` | 通过，`PASS: all pid_ai tests` |
| `python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v` | 通过，30 个测试 OK |
| `python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py` | 通过 |
| `python C:\Users\caofengrui\.codex\skills\.system\skill-creator\scripts\quick_validate.py .codex\skills\pid-ai-serial` | 通过，`Skill is valid!` |
| `git diff --check` | 通过，无空白错误；仅 Windows LF/CRLF 提示 |

## 注意事项

| 项目 | 状态 |
|---|---|
| `.spec-workflow/` | 仍为未跟踪目录，按计划未提交 |
| 远端 | `f7b8ded` 已推送到 `origin/feat/pid-ai-autotune` |


### Git Commits

| Hash | Message |
|------|---------|
| `f7b8ded` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: Session 5 - ACK hardening and post-ACK autotune scoring

**Date**: 2026-05-23
**Task**: Session 5 - ACK hardening and post-ACK autotune scoring
**Branch**: `feat/pid-ai-autotune`

### Summary

(Add summary)

### Main Changes

| Work | Details |
|---|---|
| Serial parser hardening | Split ACK/ERR parsing into exact field-count helpers; reject extra ACK/ERR fields and unknown ERR status text before responses can enter typed state. |
| Auto-tune safety | Abort active auto-tune on mismatched pending ACK/ERR responses, and evaluate keep/rollback using only post-ACK telemetry samples. |
| Regression coverage | Added parser/state-machine tests for malformed ACK/ERR frames, mismatched loop responses, and post-ACK scoring boundaries. |
| Spec sync | Updated frontend type-safety and state-management specs with ACK/ERR validation, mismatched-response aborts, and post-ACK scoring rules. |
| GitHub upload | Added .spec-workflow/ to .gitignore, committed e70d073, and pushed feat/pid-ai-autotune to origin. |

**Verification**:
- `python -m pytest .codex\skills\pid-ai-serial\tests` -> 34 passed
- `python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py` -> passed
- `gcc -Wall -Wextra -Werror -Iinclude src/pid_ai.c src/pid_ai_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; .\tests\test_pid_ai.exe` -> PASS: all pid_ai tests

**Updated Files**:
- `.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py`
- `.codex/skills/pid-ai-serial/tests/test_pid_ai_serial_parser.py`
- `.trellis/spec/frontend/state-management.md`
- `.trellis/spec/frontend/type-safety.md`
- `.gitignore`


### Git Commits

| Hash | Message |
|------|---------|
| `e70d073` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: Session 6 - Experiment recording

**Date**: 2026-05-23
**Task**: Session 6 - Experiment recording
**Branch**: `feat/pid-ai-autotune`

### Summary

(Add summary)

### Main Changes

| Area | Details |
|---|---|
| Dashboard experiment recorder | Added command-scoped JSON experiment recording for parameter/control commands. Records pending command metadata, ACK/ERR or local transport error, before/after PID samples, before/after config snapshots, and basic score summaries. |
| Runtime controls | Added `--experiment-dir`, `--experiment-window-seconds`, and `--disable-experiment-recording`; `/api/status` now exposes `experiment_recording` summary. |
| Tests | Added dashboard tests for acknowledged `SET_PIDX` experiment persistence and local send error persistence. Full `pid-ai-serial` test suite passes. |
| Docs/spec | Updated README, dashboard API docs, `.gitignore`, and frontend state-management spec with the executable experiment-recording contract. |

**Verification**:
- `gcc -Iinclude src/pid_ai.c src/pid_ai_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; if ($LASTEXITCODE -eq 0) { .\tests\test_pid_ai.exe }` -> PASS
- `python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v` -> 36 tests OK
- `python -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py` -> PASS
- `PYTHONUTF8=1 quick_validate.py .codex\skills\pid-ai-serial` -> Skill is valid

**GitHub**:
- Pushed `feat/pid-ai-autotune` to `origin` at `372db2a`.


### Git Commits

| Hash | Message |
|------|---------|
| `372db2a` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 7: Session 7 - Binary protocol and autotune strategies

**Date**: 2026-05-23
**Task**: Session 7 - Binary protocol and autotune strategies
**Branch**: `feat/pid-ai-autotune`

### Summary

Implemented richer PID auto-tune strategy selection and the optional binary
telemetry/config protocol, then pushed the feature commit to GitHub.

### Main Changes

| 项目 | 记录 |
|---|---|
| 自动调参策略 | 增加稳态偏差增 `ki`、震荡增 `kd`、输出饱和且 `anti_windup` 降 `ki`、`line_outer` 慢响应半步增 `kp` 的策略分支。`plan_next_action()` 输出 `strategy` 和 `changed_param`，方便 dashboard、实验记录和操作员审计。 |
| 二进制协议 | 新增 `include/pid_ai_binary_protocol.h` 和 `src/pid_ai_binary_protocol.c`，支持 PID/PIDX/CFG/CFGX 二进制帧、magic `0xA5 0x5A`、version `1`、CRC-16/CCITT-FALSE，并覆盖标准向量 `123456789 -> 0x29B1`。 |
| Host 解析 | `pid_ai_serial.py` 新增二进制构帧/解析、`BinaryFrameDecoder` 和 `ProtocolStreamDecoder`，串口读取、扫描、autotune 与 dashboard reader 支持文本/二进制混合流。 |
| Dashboard | `pid_ai_dashboard.py` 新增统一 `ingest_parsed_frame()` 和 `ingest_bytes()`，二进制 PID/CFG typed frame 与文本帧进入相同状态更新流程，坏帧不污染最新有效状态。 |
| 文档与规范 | README、`docs/pid_ai_serial_protocol.md`、`docs/pid_ai_dashboard.md`、`.codex/skills/pid-ai-serial/SKILL.md` 和 Trellis backend/frontend spec 已同步二进制协议与调参策略契约。 |

**验证**:
- `gcc -Iinclude src/pid_ai.c src/pid_ai_protocol.c src/pid_ai_binary_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe; if ($LASTEXITCODE -eq 0) { .\tests\test_pid_ai.exe }` -> `PASS: all pid_ai tests`
- `python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v` -> 48 tests OK
- `python -B -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py` -> exit 0
- `$env:PYTHONUTF8='1'; python C:\Users\caofengrui\.codex\skills\.system\skill-creator\scripts\quick_validate.py .codex\skills\pid-ai-serial` -> `Skill is valid!`
- `git diff --check` -> exit 0，仅 LF/CRLF warning

**提交**:
- `9f11aa2 feat(serial): add binary protocol and richer autotune strategies`


### Git Commits

| Hash | Message |
|------|---------|
| `9f11aa2` | (see git log) |

### Testing

- [OK] C regression suite passed with `PASS: all pid_ai tests`
- [OK] Python serial/dashboard unittest suite passed with 48 tests
- [OK] Python script compile, skill validation, and `git diff --check` passed

### Status

[OK] **Completed**

### Next Steps

- None - task complete

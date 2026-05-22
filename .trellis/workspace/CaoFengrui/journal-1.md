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

(Add summary)

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

- [OK] (Add test results)

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

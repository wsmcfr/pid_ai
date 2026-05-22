# Quality Guidelines

> Code quality standards for the board-side C library.

---

## Overview

The codebase is a portable embedded C library with PC-side tests. Quality means:

| Quality Goal | Project-Specific Meaning |
|---|---|
| Portability | No MCU HAL calls in `src/` or public headers. |
| Determinism | No dynamic allocation, bounded buffers, explicit error returns. |
| Protocol stability | Frame order and command status stay synchronized with docs and tests. |
| Safety | Invalid inputs force safe output and visible fault state. |
| Maintainability | Public functions and non-obvious logic are documented with Chinese comments in source files. |

---

## Forbidden Patterns

| Forbidden Pattern | Why |
|---|---|
| `malloc`, `free`, or unbounded heap ownership in board-side library code. | Embedded targets need predictable memory use. |
| Direct HAL/ESP-IDF/Arduino/POSIX hardware calls in `src/` or `include/`. | Breaks platform neutrality and PC tests. |
| `printf()` inside `PIDAI_Update()` or protocol parsing. | Adds timing jitter and unwanted I/O side effects. |
| Silent protocol field reordering. | Host parsers and AI diagnostics depend on field positions. |
| Accepting NaN, infinity, or extremely large parameters. | Serial corruption could create unsafe actuator output. |
| Negative `kp`, `ki`, or `kd`. | Existing convention uses non-negative gains and `reverse` for direction. |
| Ignoring `snprintf()` truncation. | Truncated frames can be parsed as valid but wrong data. |
| Adding code without explanatory Chinese comments for public functions or complex logic. | The current source style documents function purpose, flow, parameters, and return values. |

---

## Required Patterns

| Required Pattern | Existing Example |
|---|---|
| Validate pointers at public API boundaries. | `PIDAI_Init()` returns `-1` when `pid == 0`. |
| Keep helper functions `static` unless they are part of the public API. | `PIDAI_ParseFloat()` and `PIDAI_NextToken()` in `src/pid_ai_protocol.c`. |
| Use fixed-size buffers for protocol parsing and result fields. | `PIDAI_PROTOCOL_LINE_MAX`, `PIDAI_PROTOCOL_DETAIL_MAX`. |
| Use `snprintf()` plus return-value checks for frame formatting. | `PIDAI_CheckFormatResult()`. |
| Normalize boolean-like inputs to `0` or `1`. | `PIDAI_Enable()`, `PIDAI_SetSensorOk()`, `PIDAI_SetReverse()`. |
| Preserve operator configuration when resetting runtime state. | `PIDAI_Reset()` saves tunings, target, manual output, limits, mode, enable, and reverse before `memset()`. |
| Add focused tests for every new public behavior. | `test_pid_update_normal()`, `test_protocol_set_pid()`. |

---

## Testing Requirements

`tests/test_pid_ai.c` is the current regression suite. New behavior should add a
small test function and call it from `main()`.

Minimum coverage by change type:

| Change Type | Required Test Coverage |
|---|---|
| PID math or mode behavior | Verify state fields and actuator output with `float_close()`. |
| Limit or anti-windup behavior | Verify raw output, limited output, `sat`, and `anti_windup`. |
| Command parsing | Verify `PIDAI_CommandResult.status`, `command/detail` if relevant, and modified `PIDAI_Handle` fields. |
| Frame formatting | Verify prefix, key fields, return value, and buffer-too-small behavior when applicable. |
| Error/fault behavior | Verify return code and `pid.fault` bit. |

A typical local test command on Windows with GCC available is:

```powershell
gcc -Iinclude src/pid_ai.c src/pid_ai_protocol.c tests/test_pid_ai.c -o tests/test_pid_ai.exe
.\tests\test_pid_ai.exe
```

If GCC is unavailable, state that tests were not run and why.

---

## Code Review Checklist

| Check | Question |
|---|---|
| API contract | Was the public header updated together with implementation? |
| Protocol contract | Were docs/tests updated for frame or command changes? |
| Safety | Do invalid inputs produce safe output and visible error state? |
| Portability | Did the change avoid board-specific includes and APIs in portable code? |
| Memory | Are buffers fixed-size and bounds-checked? |
| Timing | Did the change avoid blocking work in control-path functions? |
| Tests | Is there an executable regression test for the behavior? |
| Comments | Are function purpose, main flow, parameters, returns, and non-obvious branches commented in Chinese? |

---

## Common Mistakes

| Mistake | Prevention |
|---|---|
| Updating `PIDAI_Handle` without updating `{PID}` output. | Search for the field name and update docs, builder, tests, and README examples. |
| Adding command parsing without ACK/ERR detail coverage. | Add both success and at least one failure test. |
| Treating extra command fields as harmless. | Add a parser test that verifies `ARG_INVALID` with `UNEXPECTED_ARG` and no partial state update. |
| Treating board example code as portable library code. | Keep board abstractions in `examples/` or application code. |
| Assuming compile success proves protocol compatibility. | Run tests and inspect frame strings. |

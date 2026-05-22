# Directory Structure

> How board-side C code is organized in this project.

---

## Overview

The project is a small portable C library, not a layered server application. Keep
the current flat module structure unless a new feature clearly needs a separate
module pair.

| Directory | Purpose | Examples |
|---|---|---|
| `include/` | Public, platform-neutral API headers consumed by MCU firmware and PC tests. | `include/pid_ai.h`, `include/pid_ai_protocol.h` |
| `src/` | Portable C implementations for the public headers. | `src/pid_ai.c`, `src/pid_ai_protocol.c` |
| `docs/` | Human-readable protocol and architecture documentation. | `docs/pid_ai_serial_protocol.md` |
| `examples/` | Board integration examples with weak platform assumptions. | `examples/pid_ai_board_example.c` |
| `tests/` | PC-side C regression tests. | `tests/test_pid_ai.c` |

---

## Directory Layout

```text
include/
├── pid_ai.h              # Core PID state, enums, and public functions.
└── pid_ai_protocol.h     # Serial command/result/frame public functions.
src/
├── pid_ai.c              # PID math, limits, modes, and fault handling.
└── pid_ai_protocol.c     # Text command parsing and frame formatting.
docs/
└── pid_ai_serial_protocol.md
examples/
└── pid_ai_board_example.c
tests/
└── test_pid_ai.c
```

---

## Module Organization

### Public Header + Source Pair

Every portable library module should have one public header in `include/` and one
implementation in `src/`.

| Module | Public API | Implementation |
|---|---|---|
| Core PID | `include/pid_ai.h` | `src/pid_ai.c` |
| Serial protocol | `include/pid_ai_protocol.h` | `src/pid_ai_protocol.c` |

New portable modules should follow the same shape:

```text
include/pid_ai_<feature>.h
src/pid_ai_<feature>.c
```

Only create a new module when the feature has a distinct public contract. Small
helpers that only support one module should stay `static` inside that module, as
seen with `PIDAI_IsFiniteParam()` and `PIDAI_ClampWithSat()` in `src/pid_ai.c`.

### Board-Specific Code Stays Out of `src/`

The portable library must not call MCU HAL APIs directly. Board-specific reads,
writes, timers, and UART functions belong in application code or `examples/`.

`examples/pid_ai_board_example.c` shows the expected boundary:

```c
feedback = Board_ReadFeedback();
now_ms = Board_GetMillis();
actuator = PIDAI_Update(&g_pid, feedback, now_ms, BOARD_CONTROL_DT_MS);
Board_WriteActuator(actuator);
```

`PIDAI_Update()` receives values and returns an actuator output; it does not know
how the board reads sensors or writes PWM.

---

## Naming Conventions

| Item | Convention | Existing Example |
|---|---|---|
| Public types | `PIDAI_<Name>` | `PIDAI_Handle`, `PIDAI_Mode` |
| Public functions | `PIDAI_<Verb><Object>` | `PIDAI_SetTunings`, `PIDAI_ProtocolBuildTelemetry` |
| Static helpers | `PIDAI_<Verb><Object>` with `static` scope | `PIDAI_ParseFloat`, `PIDAI_CheckFormatResult` |
| Constants/macros | `PIDAI_<AREA>_<NAME>` or board-local prefix | `PIDAI_PROTOCOL_DETAIL_MAX`, `BOARD_TX_BUFFER_SIZE` |
| Files | Lowercase snake-style module names | `pid_ai.c`, `pid_ai_protocol.c` |
| Test functions | `test_<behavior>` | `test_pid_output_saturation` |

---

## Examples

| Pattern | File | Why It Is the Model |
|---|---|---|
| Public struct and API documented in one header | `include/pid_ai.h` | Defines the full PID state contract consumed by firmware and tests. |
| Private helpers hidden inside the implementation | `src/pid_ai_protocol.c` | Parsing helpers are `static` and do not leak into the public API. |
| Platform integration kept outside the library | `examples/pid_ai_board_example.c` | Replaces timers, UART, sensor, and actuator with board-level functions. |

---

## Anti-Patterns

| Do Not | Why |
|---|---|
| Add STM32 HAL, ESP-IDF, Arduino, or POSIX APIs inside `src/`. | It breaks portability and PC-side tests. |
| Put protocol parsing in board application code when it belongs to `src/pid_ai_protocol.c`. | Host/board protocol behavior would drift across platforms. |
| Add public functions without updating the matching header and tests. | Callers cannot rely on an undocumented or untested contract. |
| Create generic utility files for one-off helpers. | The current codebase keeps module-private helpers local and readable. |

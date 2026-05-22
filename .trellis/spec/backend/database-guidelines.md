# Database Guidelines

> Persistence conventions for this embedded C library.

---

## Overview

This project currently has no database, ORM, migration system, or persistent
storage layer. The board-side library is intentionally limited to in-memory PID
state and serial text frames.

| Data Category | Current Owner | Persistence Status |
|---|---|---|
| PID runtime state | `PIDAI_Handle` in `include/pid_ai.h` | In memory only |
| Telemetry samples | `{PID}` frames from `PIDAI_ProtocolBuildTelemetry()` | Emitted over UART, not stored by the library |
| Configuration snapshot | `{CFG}` frames from `PIDAI_ProtocolBuildConfig()` | Emitted over UART, not stored by the library |
| Experiment records / CSV | Future host-side tooling | Not implemented in this repository |
| MCU non-volatile settings | Board application layer | Not implemented in the portable library |

---

## Persistence Boundary

The portable library must not own database files, flash sectors, EEPROM layouts,
or host-side experiment storage. Persistence should be added outside the core C
library unless there is a clear board-side contract.

Correct boundary today:

```c
/* Board/app code decides when and where to persist. */
result = PIDAI_ProtocolHandleCommand(&g_pid, line);
if (result.status == PIDAI_CMD_OK) {
    /* Application may save g_pid.kp/g_pid.ki/g_pid.kd to flash here. */
}
```

Incorrect boundary:

```c
/* Do not add flash, filesystem, SQLite, or host logging calls inside pid_ai.c. */
int PIDAI_SetTunings(PIDAI_Handle *pid, float kp, float ki, float kd)
{
    save_to_flash(kp, ki, kd);
    ...
}
```

---

## Data Contracts That Replace Database Schemas

Because there is no database, the stable data contracts are protocol fields and C
struct fields. Treat these as schema-level contracts.

| Contract | Location | Required Sync Points |
|---|---|---|
| `PIDAI_Handle` fields | `include/pid_ai.h` | `src/pid_ai.c`, `{PID}` builder, tests, protocol docs |
| `{PID}` field order | `docs/pid_ai_serial_protocol.md` and `src/pid_ai_protocol.c` | Host parser, tests, README |
| `{CFG}` field order | `docs/pid_ai_serial_protocol.md` and `src/pid_ai_protocol.c` | Host parser, board example, tests |
| `{CMD}` commands | `docs/pid_ai_serial_protocol.md` and `PIDAI_ProtocolHandleCommand()` | ACK/ERR handling and tests |

When adding a field, update all sync points in the same change.

---

## Future Host-Side Storage Rules

If a future host tool stores telemetry or experiments, keep storage outside the
board-side library and model it around protocol contracts.

| Storage Need | Recommended Owner | Minimum Fields |
|---|---|---|
| Raw telemetry CSV | Host serial reader | Full `{PID}` field list, receive timestamp, port/session id |
| Experiment metadata | Host application | PID parameters, target profile, operator/AI note, protocol version |
| Parameter history | Host application | Command text, ACK/ERR result, before/after `{CFG}` |

The board library should continue to emit frames and parse commands. It should
not know whether the host writes CSV, SQLite, Parquet, or another format.

---

## Naming Conventions

| Thing | Convention |
|---|---|
| Protocol field names | Match `docs/pid_ai_serial_protocol.md` exactly. |
| CSV headers in future tools | Use the `{PID}` or `{CFG}` field names without translation. |
| Persistent parameter names | Use `kp`, `ki`, `kd`, `kf`, `out_min`, `out_max`, `integral_min`, `integral_max`, `reverse`, `mode`. |
| Version fields | Preserve a numeric `version` like `{CFG}` already does. |

---

## Common Mistakes

| Mistake | Consequence | Correct Approach |
|---|---|---|
| Treating telemetry fields as informal logs. | Host parsers silently break when field order changes. | Treat frames as schemas and update docs/tests together. |
| Saving parameters inside `src/pid_ai.c`. | Portable library becomes platform-specific. | Save in board application code after successful commands. |
| Assuming `{ACK}` means host storage succeeded. | ACK only confirms board command execution. | Host storage needs its own error handling in host code. |
| Recording only `actuator`. | AI cannot distinguish saturation, integral buildup, or sensor faults. | Store the full `{PID}` field set. |

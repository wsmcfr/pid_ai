# Logging Guidelines

> Runtime observability conventions for this embedded protocol library.

---

## Overview

The portable C library does not use a logging framework. Observability is exposed
through deterministic serial protocol frames and state fields:

| Observability Type | Current Mechanism | Owner |
|---|---|---|
| High-frequency runtime telemetry | `{PID}` frame | `PIDAI_ProtocolBuildTelemetry()` |
| Configuration snapshot | `{CFG}` frame | `PIDAI_ProtocolBuildConfig()` |
| Command success | `{ACK}` frame | `PIDAI_ProtocolBuildAck()` |
| Command failure | `{ERR}` frame | `PIDAI_ProtocolBuildError()` |
| Board events | Recommended `{EVT}` frame | Board application layer |
| Board health/status | Recommended `{STAT}` frame | Board application layer |

Treat frames as structured logs with stable schemas.

---

## Log Levels

There are no runtime log levels in the core library. Use frame type to express
the purpose instead.

| Frame | Equivalent Log Intent | When To Emit |
|---|---|---|
| `{PID}` | Debug/telemetry stream | Periodically during control operation, possibly downsampled |
| `{CFG}` | Info/config snapshot | On boot, after config changes, or after `{CMD}GET_CFG` |
| `{ACK}` | Info/command accepted | After a command is parsed and applied |
| `{ERR}` | Warn/error command rejected | After parse, validation, or internal failure |
| `{EVT}` | Info/warn event marker | Application-level mode switch, target change, fault event |
| `{STAT}` | Debug/info board health | Application-level voltage, current, temperature, encoder state |

Do not add `printf()` logging to `src/`. Board examples may use `printf()` only
as a UART stand-in.

---

## Structured Logging

### `{PID}` Frame Contract

`docs/pid_ai_serial_protocol.md` defines this fixed order:

```text
seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

`src/pid_ai_protocol.c` formats the same fields with the `{PID}` prefix and
`\r\n` suffix. Any field order change must update:

| Required Update | Why |
|---|---|
| `docs/pid_ai_serial_protocol.md` | Host/parser contract |
| `PIDAI_ProtocolBuildTelemetry()` | Actual emitted frame |
| `tests/test_pid_ai.c` | Regression coverage |
| README examples if affected | User-facing usage |

### `{CFG}` Frame Contract

Current order:

```text
profile_id,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

The `sample_ms` value currently comes from `pid->dt_ms`. If a fixed-period board
needs a nominal sample period before the first update, the board/application layer
should manage that context.

---

## What to Log

| Signal | Reason |
|---|---|
| `target`, `feedback`, `error`, `d_error` | Required to diagnose response speed, overshoot, and oscillation. |
| `integral`, `i_out`, `anti_windup` | Required to diagnose integral buildup and anti-windup behavior. |
| `out_raw`, `out_limited`, `actuator`, `sat` | Required to distinguish controller demand from actuator saturation. |
| `mode`, `enable`, `sensor_ok`, `fault` | Required to avoid tuning when the controller is stopped or data is invalid. |
| ACK/ERR command results | Host must only treat commands as applied after ACK. |

---

## What NOT to Log

| Do Not Log From Core Library | Why |
|---|---|
| Raw UART operations | The portable library should not own hardware I/O. |
| Human prose messages in high-frequency frames | Host parsers need compact fixed fields. |
| Unbounded strings from received commands | Fixed buffers prevent memory and bandwidth issues. |
| Secrets or device credentials | The protocol is plain text and may be recorded. |
| Per-cycle `printf()` inside `PIDAI_Update()` | It can violate timing and break MCU control loops. |

---

## Common Mistakes

| Mistake | Consequence | Correct Pattern |
|---|---|---|
| Sending only `actuator` or `out_limited`. | AI cannot diagnose saturation or PID term balance. | Send the full `{PID}` frame. |
| Printing from an interrupt or high-frequency control function. | Jitter, blocking, or missed deadlines. | Buffer or downsample in board code, as noted in the protocol docs. |
| Treating `{ERR}` as a debug-only message. | Host may assume a rejected command succeeded. | Surface `{ERR}` in host UI and do not update applied state until ACK. |
| Changing decimal precision casually. | Snapshot comparisons and host display may drift. | Keep existing `snprintf()` precision unless tests/docs are updated. |

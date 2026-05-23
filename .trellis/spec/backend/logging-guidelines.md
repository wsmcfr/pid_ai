# Logging Guidelines

> Runtime observability conventions for this embedded protocol library.

---

## Overview

The portable C library does not use a logging framework. Observability is exposed
through deterministic serial protocol frames and state fields:

| Observability Type | Current Mechanism | Owner |
|---|---|---|
| High-frequency runtime telemetry | `{PID}` frame | `PIDAI_ProtocolBuildTelemetry()` |
| Multi-loop runtime telemetry | `{PIDX}` frame | `PIDAI_ProtocolBuildTelemetryX()` |
| Configuration snapshot | `{CFG}` frame | `PIDAI_ProtocolBuildConfig()` |
| Multi-loop configuration snapshot | `{CFGX}` frame | `PIDAI_ProtocolBuildConfigX()` |
| High-frequency binary telemetry/config | Binary PID/PIDX/CFG/CFGX frame | `src/pid_ai_binary_protocol.c` |
| Line-car sensor snapshot | `{SENS}` frame | Board application layer |
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
| `{PIDX}` | Debug/telemetry stream | Periodically for each configured loop in cascade or multi-loop control |
| `{CFG}` | Info/config snapshot | On boot, after config changes, or after `{CMD}GET_CFG` |
| `{CFGX}` | Info/config snapshot | On boot, after per-loop config changes, `{CMD}GET_CFGX`, or `{CMD}GET_ALL_CFG` |
| `{SENS}` | Debug/safety sensor stream | Application-level line-car sensors used by host safety gates |
| `{ACK}` | Info/command accepted | After a command is parsed and applied |
| `{ERR}` | Warn/error command rejected | After parse, validation, or internal failure |
| `{EVT}` | Info/warn event marker | Application-level mode switch, target change, fault event |
| `{STAT}` | Debug/info board health | Application-level voltage, current, temperature, encoder state |

Do not add `printf()` logging to `src/`. Board examples may use `printf()` only
as a UART stand-in.

### Binary Frame Contract

The optional binary protocol carries the same PID/config meanings as the text
frames for high-frequency links.

| Field | Contract |
|---|---|
| Magic | First two bytes are `0xA5 0x5A`. |
| Header | `version:u8`, `type:u8`, `flags:u8`, `transport_seq:u32le`, `payload_len:u16le`. |
| Types | `1=PID`, `2=PIDX`, `3=CFG`, `4=CFGX`. |
| CRC | CRC-16/CCITT-FALSE, initial `0xFFFF`, polynomial `0x1021`, no reflection, no final xor. It covers header bytes from `version` through payload. |
| PID payload | Same order as `{PID}`; 23 fields, 92 bytes. |
| CFG payload | Same order as `{CFG}`; 14 fields, 56 bytes. |
| PIDX/CFGX text | `loop_id_len:u8 + loop_id + loop_name_len:u8 + loop_name`, followed by the corresponding fixed payload. |

Any binary protocol change must update `include/pid_ai_binary_protocol.h`,
`src/pid_ai_binary_protocol.c`, `docs/pid_ai_serial_protocol.md`, Python parser
tests, and C regression tests. The CRC standard vector `123456789 -> 0x29B1`
must remain covered.

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

### `{PIDX}` / `{CFGX}` / `{SENS}` Extension Contract

Multi-loop and cascade tuning extends the single-loop frames without changing the
legacy `{PID}` and `{CFG}` contracts. The field order is fixed and must stay
synchronized across docs, C builders, host parsers, dashboard state, and tests.

`{PIDX}` order:

```text
loop_id,loop_name,seq,ms,dt_ms,target,feedback,error,d_error,integral,p_out,i_out,d_out,ff_out,out_raw,out_limited,actuator,out_min,out_max,sat,anti_windup,mode,enable,sensor_ok,fault
```

`{CFGX}` order:

```text
loop_id,loop_name,kp,ki,kd,kf,sample_ms,integral_min,integral_max,out_min,out_max,reverse,mode,version,fault
```

`{SENS}` order:

```text
ms,line0,line1,line2,line3,line4,line5,line6,line7,line_pos,line_lost,yaw,yaw_rate,enc_l,enc_r,v_l,v_r,v_avg,battery
```

Any change to these contracts requires the same update set as `{PID}` / `{CFG}`,
plus host-side parser and dashboard tests under `.codex/skills/pid-ai-serial/tests/`.
The portable C library owns `{PIDX}` and `{CFGX}` builders; `{SENS}` remains an
application-layer frame because line sensors, IMU, encoders, and battery signals
are board-specific.

---

## What to Log

| Signal | Reason |
|---|---|
| `target`, `feedback`, `error`, `d_error` | Required to diagnose response speed, overshoot, and oscillation. |
| `loop_id`, `loop_name` in extension frames | Required to route cascade telemetry and command results to the correct controller. |
| `integral`, `i_out`, `anti_windup` | Required to diagnose integral buildup and anti-windup behavior. |
| `out_raw`, `out_limited`, `actuator`, `sat` | Required to distinguish controller demand from actuator saturation. |
| `mode`, `enable`, `sensor_ok`, `fault` | Required to avoid tuning when the controller is stopped or data is invalid. |
| `line_lost` and sensor quality in `{SENS}` | Required to abort line-car auto-tuning when the robot loses the track. |
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
| Emitting `{PIDX}` without stable `loop_id`. | Host auto-tuning can apply a suggestion to the wrong loop. | Use the configured loop table and exact `loop_id` routing. |
| Treating `{SENS}` as optional during line-car auto-tune. | Lost-line or bad sensor data can drive unsafe parameter changes. | Abort or remain read-only when required sensor safety fields are missing or invalid. |

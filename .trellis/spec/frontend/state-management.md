# State Management

> State categories and ownership for future host-side tools.

---

## Overview

The frontend state model should mirror the board/host tuning loop described in
`README.md`: read typed protocol frames, display live PID behavior, record
experiments, and send confirmed commands.

---

## State Categories

| State | Source | Owner | Notes |
|---|---|---|---|
| Serial connection | User action and serial transport | Connection module/hook | Port, baud, connected/disconnected/error. |
| Live telemetry | Parsed `{PID}` frames | Telemetry module | Bounded buffer plus latest sample. |
| Current config | Parsed `{CFG}` frames | Config module | Use board-confirmed values as applied state. |
| Command transactions | Sent `{CMD}` and received `{ACK}`/`{ERR}` | Command module | Pending, acknowledged, rejected, timed out. |
| Tuning form draft | User edits | Tuning UI | Separate from board-confirmed config. |
| Diagnosis window | Derived from telemetry samples | Diagnosis module | Use a time/sample window, not entire session by default. |
| Experiment record | Explicit user/AI recording flow | Recording module | May persist to CSV/database in future host code. |

---

## When to Use Global State

Promote state only when multiple features need the same source of truth.

| Keep Local | Promote Shared/Global |
|---|---|
| Open/closed panel state | Latest `{PID}` sample |
| Input focus and temporary validation text | Serial connection status |
| Unsaved form edits for one panel | Current `{CFG}` |
| Chart series visibility if only one chart uses it | Command transaction history |

Avoid putting high-frequency raw samples directly into broad global state if that
causes unnecessary re-renders. Prefer a bounded store with selectors or a
dedicated telemetry buffer.

---

## Server State

There is no server state yet. Serial board state acts like external device state:

| External State | Sync Rule |
|---|---|
| PID parameters | Applied only after `{ACK}` and preferably refreshed via `{CFG}`. |
| Mode/enable/sensor state | Trust latest `{PID}` or `{CFG}` from board. |
| Command result | Do not infer success from write completion; require `{ACK}`. |
| Fault state | Treat `fault != 0` as active until board sends cleared state after `CLEAR_FAULT`. |

If future host software adds a backend service or local database, keep it separate
from board-applied state. The board remains the source of truth for active PID
configuration.

---

## Derived State

Derived tuning indicators should be computed from typed samples:

| Indicator | Required Fields |
|---|---|
| Response slow | `target`, `feedback`, `error`, `ms` |
| Overshoot | `target`, `feedback`, `error` |
| Oscillation | `feedback`, `error`, `d_error`, `dt_ms` |
| Integral saturation | `integral`, `i_out`, `anti_windup` |
| Output saturation | `out_raw`, `out_limited`, `sat`, `out_min`, `out_max` |
| Unsafe tuning window | `sensor_ok`, `fault`, `mode`, `enable` |

Do not run AI tuning suggestions on samples where `sensor_ok == 0`, `fault != 0`,
or the mode/enable state means the controller is not actually controlling.

---

## Common Mistakes

| Mistake | Consequence | Correct Pattern |
|---|---|---|
| Overwriting confirmed config with form draft values. | UI lies about what the board is running. | Keep draft and board-confirmed config separate. |
| Treating serial write success as command success. | Dropped/rejected commands look applied. | Wait for `{ACK}` or show timeout/error. |
| Using unbounded telemetry arrays as global state. | Memory and rendering degrade during long sessions. | Use bounded buffers and explicit recording/export. |
| Deriving diagnosis from raw strings. | Parser differences create subtle bugs. | Parse first, then derive from typed frames. |

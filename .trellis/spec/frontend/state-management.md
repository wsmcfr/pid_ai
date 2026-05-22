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
| Multi-loop telemetry | Parsed `{PIDX}` frames | Loop state module | Map by `loop_id`; bad frames must not overwrite latest valid loop sample. |
| Current config | Parsed `{CFG}` frames | Config module | Use board-confirmed values as applied state. |
| Multi-loop config | Parsed `{CFGX}` frames | Loop state module | Map by `loop_id`; use board-confirmed config as applied state. |
| Car sensor snapshot | Parsed `{SENS}` frames | Safety/diagnosis module | Latest valid line sensors, yaw, encoders, speed, and battery. |
| Command transactions | Sent `{CMD}` and received `{ACK}`/`{ERR}` | Command module | Pending, acknowledged, rejected, timed out. |
| Auto-tune runtime | User mode plus parsed frames and command results | Auto-tune module | Mode, state, current loop, pending step, scores, and rollback history. |
| Tuning form draft | User edits | Tuning UI | Separate from board-confirmed config. |
| Diagnosis window | Derived from telemetry samples | Diagnosis module | Use a time/sample window, not entire session by default. |
| Experiment record | Explicit user/AI recording flow | Recording module | May persist to CSV/database in future host code. |

---

## When to Use Global State

Promote state only when multiple features need the same source of truth.

| Keep Local | Promote Shared/Global |
|---|---|
| Open/closed panel state | Latest `{PID}` sample |
| Per-row expansion in a loop table | `loops[loop_id].latest_pid` and `loops[loop_id].latest_cfg` |
| Input focus and temporary validation text | Serial connection status |
| Unsaved form edits for one panel | Current `{CFG}` |
| Chart series visibility if only one chart uses it | Command transaction history |
| Local confirm dialog state | Auto-tune mode/state and rollback history |

Avoid putting high-frequency raw samples directly into broad global state if that
causes unnecessary re-renders. Prefer a bounded store with selectors or a
dedicated telemetry buffer.

---

## Server State

There is no server state yet. Serial board state acts like external device state:

| External State | Sync Rule |
|---|---|
| PID parameters | Applied only after `{ACK}` and preferably refreshed via `{CFG}`. |
| Per-loop PID parameters | Applied only after a matching `{ACK}` for the same command and `loop_id`, preferably refreshed via `{CFGX}`. |
| Mode/enable/sensor state | Trust latest `{PID}` or `{CFG}` from board. |
| Command result | Do not infer success from write completion; require `{ACK}`. |
| Fault state | Treat `fault != 0` as active until board sends cleared state after `CLEAR_FAULT`. |
| Line-car sensor state | Trust only the latest valid `{SENS}` frame; parse errors must not clear `line_lost` or sensor fault evidence. |

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
| Multi-loop score | `{PIDX}` window for one `loop_id`: average error, max error, zero crossings, saturation ratio, anti-windup ratio |
| Line-car safety | `{SENS}.line_lost`, line sensor booleans, `yaw_rate`, wheel speed fields |

Do not run AI tuning suggestions on samples where `sensor_ok == 0`, `fault != 0`,
or the mode/enable state means the controller is not actually controlling.

---

## Multi-Loop State Contract

Use a stable map keyed by `loop_id` for cascade tools:

```ts
type LoopRuntimeState = {
  latest_pid?: PidXFrame;
  latest_cfg?: CfgXFrame;
  samples: PidXFrame[];
  last_error?: string;
};

type DashboardRuntimeState = {
  latest_pid?: PidFrame;
  latest_cfg?: CfgFrame;
  latest_sens?: SensFrame;
  loops: Record<string, LoopRuntimeState>;
  autotune: AutoTuneState;
  scores: Record<string, TuningScore>;
  rollback_history: RollbackRecord[];
};
```

| Contract | Reason |
|---|---|
| `loops` is keyed by parsed `loop_id`, not display name. | `loop_name` is human-readable and may change without changing routing. |
| Bad `{PIDX}` / `{CFGX}` frames increment parse errors but do not mutate `loops`. | A truncated frame must not erase known-good controller state. |
| `{SENS}` parse errors do not clear `latest_sens`. | Safety decisions need the last valid sensor evidence plus the parse error. |
| Command history records `loop_id` and tuning reason when present. | ACK/ERR matching, audit trails, and rollback records need the same routing key. |
| Legacy ACK/ERR frames without `loop_id` must not coexist with multiple pending commands of the same loop-aware command name. | `{ACK}SET_PIDX,OK` cannot prove whether `speed_l` or `speed_r` was applied if both are pending. |
| `GET_ALL_CFG` may produce multiple `{CFGX}` frames. | Store each snapshot independently under its loop key. |

---

## Auto-Tune State Contract

The host auto-tune feature has four externally visible modes:

| Mode | Writes Serial Commands | State Behavior |
|---|---:|---|
| `observe` | No | Read telemetry, update scores and safety state only. |
| `suggest` | No | Produce candidate command text and reason, but leave command history unchanged unless the user sends it. |
| `auto-tune` | Yes | Send one small `SET_PIDX` step at a time, wait for ACK, observe, keep or roll back. |
| `emergency-stop` | Yes | Stop auto-tune and send disable/stop commands when explicitly requested or required by safety policy. |

Auto-tune state should follow this flow:

```text
DISCOVER -> SYNC_CONFIG -> OBSERVE_BASELINE -> SELECT_LOOP -> PROPOSE_STEP
-> SEND_STEP -> OBSERVE_RESULT -> KEEP_OR_ROLLBACK -> NEXT_LOOP
```

| Rule | Required behavior |
|---|---|
| Cascade order | Tune `speed_l`, then `speed_r`, then `yaw_rate`, then `line_outer`; do not tune outer loops while inner loops are failing. |
| Single-loop mutation | Only one loop and one `kp/ki/kd` tuple may be changed per step. |
| Step size | Default maximum change is `10%`; command builders format PID numbers with three decimals. |
| ACK gate | `OBSERVE_RESULT` may start only after a matching `{ACK}` for the pending command and `loop_id`. |
| ACK timeout | Store `sent_at` for each step and rollback command; if `ack_timeout_seconds` elapses without a matching ACK/ERR, move the controller to `ABORT`. |
| ERR/timeout gate | `{ERR}`, ACK timeout, serial disconnect, or mismatched pending command moves the controller to `ABORT`. |
| Regression rollback | If post-ACK score is worse and rollback is enabled, send old parameters using `SET_PIDX`, append `rollback_history`, and keep the loop pending until the rollback command receives a matching `{ACK}`. |
| Rollback failure | Rollback `{ERR}` or rollback ACK timeout moves the controller to `ABORT`; do not mark the loop completed. |
| Safety abort | `fault != 0`, `sensor_ok != 1`, `{SENS}.line_lost == 1`, serial disconnect, or Bluetooth SPP stream loss aborts auto-tune. |
| Dangerous changes | Auto-tune must not automatically widen output limits, flip reverse direction, or enable manual output. |

Scores are derived state, not board-confirmed state. They can be recomputed from
bounded telemetry windows and should not replace `{CFGX}` as the source of truth
for applied parameters. When an API exposes `window_seconds`, scoring must crop
samples by receive time or board `ms`; a fixed sample count is only a fallback
when no usable timestamp exists.

---

## Common Mistakes

| Mistake | Consequence | Correct Pattern |
|---|---|---|
| Overwriting confirmed config with form draft values. | UI lies about what the board is running. | Keep draft and board-confirmed config separate. |
| Treating serial write success as command success. | Dropped/rejected commands look applied. | Wait for `{ACK}` or show timeout/error. |
| Using unbounded telemetry arrays as global state. | Memory and rendering degrade during long sessions. | Use bounded buffers and explicit recording/export. |
| Deriving diagnosis from raw strings. | Parser differences create subtle bugs. | Parse first, then derive from typed frames. |
| Updating every loop when one `{PIDX}` arrives. | A single noisy loop can pollute other controller state. | Mutate only `loops[frame.loop_id]`. |
| Starting post-step evaluation before ACK. | The score may describe old parameters. | Record the ACK sample boundary and wait for new telemetry. |
| Treating a rollback write as completed immediately. | The dashboard may continue to the next loop while the board still runs bad parameters. | Model rollback as its own pending command and wait for matching ACK. |
| Clearing auto-tune aborts on the next good frame. | Operators lose the reason the automatic write flow stopped. | Require explicit user action to re-enable after `ABORT`. |

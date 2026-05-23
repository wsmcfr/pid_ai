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
| Experiment record | Command transactions plus typed telemetry | Recording module | Persist command-scoped JSON records for parameter changes, including pre/post curves and ACK/ERR result. |

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
| No-op prevention | If the formatted three-decimal `SET_PIDX` command would not change any applied `kp/ki/kd`, abort auto-tune and require a human-provided non-zero seed instead of sending a no-op. |
| ACK gate | `OBSERVE_RESULT` may start only after a matching `{ACK}` for the pending command and `loop_id`. |
| Post-ACK scoring window | Evaluate keep/rollback using only samples received after the matching ACK; crop that post-ACK slice by `window_seconds`. |
| Post-ACK sample floor | Wait for at least `min_post_ack_samples` new samples after ACK before keep/rollback; the current default is `3` to avoid single-sample noise decisions. |
| ACK timeout | Store `sent_at` for each step and rollback command; if `ack_timeout_seconds` elapses without a matching ACK/ERR, move the controller to `ABORT` even when the serial stream is silent. |
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

For an ACK-gated tuning step, record the sample count or timestamp at the
matching ACK. The post-step score must start after that boundary so pre-change
baseline telemetry cannot dilute or hide a regression. If a valid ACK/ERR arrives
for a different command or `loop_id` while a step or rollback is pending, abort
the auto-tune session and surface the mismatched response.

ACK timeout progression must not depend solely on receiving another serial
frame. The dashboard should tick the auto-tune state from `/api/status` polling,
the reader thread's empty-read path, or an equivalent timer so a dropped ACK on a
quiet board cannot leave `pending_step` or rollback pending forever.

Auto-tune proposals must include explainable strategy metadata:

| Scenario | Required strategy | Parameter |
|---|---|---|
| Steady same-sign error, no saturation | `increase_ki_for_steady_bias` | Increase `ki` only. |
| Frequent zero crossings | `increase_kd_for_oscillation` | Increase `kd` only. |
| Output saturation with anti-windup | `reduce_ki_for_integral_saturation` | Decrease `ki` only. |
| Slow response | `increase_kp_for_slow_response` | Increase `kp`; for `line_outer`, use a more conservative half step. |

The public action object should expose `strategy` and `changed_param` so the UI,
experiment recorder, and operator can audit why a command was proposed.

---

## Experiment Recording Contract

Host-side experiment recording persists parameter-change transactions to JSON.
The recorder consumes only typed dashboard state; it must not parse raw serial
strings or infer board-applied state before a matching ACK.

### 1. Scope / Trigger

Use this contract for dashboard or future host tools that save PID tuning
experiments after `{CMD}` commands. It applies to commands that change control
behavior, including `SET_PID`, `SET_PIDX`, `SET_KF`, `SET_KFX`, `SET_TARGET`,
`SET_TARGETX`, limit commands, mode/enable commands, reset commands, and fault
clear commands.

### 2. Signatures

Current Python dashboard entry points:

```python
class DashboardState:
    def __init__(
        self,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        max_command_history: int = DEFAULT_COMMAND_HISTORY,
        experiment_dir: str | Path | None = None,
        experiment_window_seconds: float = DEFAULT_EXPERIMENT_WINDOW_SECONDS,
    ): ...
    def tick_autotune(self, now: float | None = None) -> dict[str, Any]: ...

class ExperimentRecorder:
    def start_command(
        self,
        entry: dict[str, Any],
        samples: list[dict[str, Any]],
        loops: dict[str, dict[str, Any]],
        latest_cfg: dict[str, Any] | None,
        now: float,
    ) -> None: ...
    def attach_response(self, entry: dict[str, Any], response: dict[str, Any], now: float) -> None: ...
    def attach_local_error(self, entry: dict[str, Any], detail: str, now: float) -> None: ...
    def observe_sample(self, sample: dict[str, Any], now: float) -> None: ...
    def observe_config(self, config_record: dict[str, Any], now: float) -> None: ...
```

Dashboard CLI flags:

```text
--experiment-dir <path>
--experiment-window-seconds <seconds>
--disable-experiment-recording
```

`GET /api/status` must include:

```text
experiment_recording.enabled
experiment_recording.directory
experiment_recording.window_seconds
experiment_recording.record_count
experiment_recording.latest_record
experiment_recording.write_errors
```

Dashboard write API signatures:

```text
POST /api/connect
POST /api/disconnect
POST /api/command
POST /api/autotune
Header: X-PID-AI-Token: <process-random-token>
```

### 3. Contracts

| Boundary | Contract |
|---|---|
| Command -> recorder | Create a pending record when a recordable `{CMD}` is added to command history. |
| Telemetry -> recorder | Use only validated `{PID}` / `{PIDX}` samples already accepted by dashboard state. |
| Config -> recorder | Use latest `{CFG}` for global commands and matching `loops[loop_id].latest_cfg` for loop commands. |
| ACK/ERR -> recorder | Update the same command id record after command history matches the response. |
| Local send error -> recorder | Mark status `error` with a `local_error` response; do not fabricate board ACK/ERR. |
| File output | Write JSON atomically with a temporary file and replacement; file IO errors must not stop serial processing. |
| Status poll -> auto-tune | Advance pending ACK timeout checks without requiring a new serial frame. |
| Browser write API -> state | Require the per-process `X-PID-AI-Token` for every POST that can connect, disconnect, send commands, or enable auto-tune. |
| Cross-origin requests | Do not expose JSON API through wildcard CORS; the local HTML and API are same-origin. |

Experiment JSON must contain these top-level fields:

```text
schema_version, record_id, created_at, updated_at, window_seconds,
status, command, response, before_config, after_config,
before_samples, after_samples, result
```

### 4. Validation & Error Matrix

| Case | Required behavior |
|---|---|
| Recordable command pending | Write pending JSON with `before_samples` and `before_config` if available. |
| Matching ACK | Set `status=ack`, preserve parsed response, and start accepting post-ACK samples. |
| Matching ERR | Set `status=err`, preserve status/detail, and do not mark the parameter as applied. |
| Serial not connected or write failure | Set `status=error` and `response.kind=local_error`. |
| Sample from another `loop_id` | Do not add it to a loop-specific record. |
| Bad protocol frame | Do not enter experiment samples because it never enters typed dashboard state. |
| Output directory write failure | Add a write error to `experiment_recording.write_errors`; keep runtime state alive. |
| Missing or wrong `X-PID-AI-Token` on write API | Return an HTTP error and do not mutate serial connection, command history, or auto-tune settings. |
| Silent serial stream during pending ACK | `tick_autotune()` eventually sets `autotune.state=ABORT` with an ACK timeout reason. |

### 5. Good/Base/Bad Cases

| Case | Example | Required behavior |
|---|---|---|
| Good | `SET_PIDX,speed_l` with matching ACK and later speed_l PIDX | JSON contains before/after samples for `speed_l`. |
| Base | `SET_PID` while disconnected | JSON contains local error and no fake ACK. |
| Base | POST `/api/autotune` without `X-PID-AI-Token` | Request is rejected before state changes. |
| Bad | ACK received for unrelated command | Existing ACK matching rules decide unsolicited/mismatch; recorder must not update the wrong record. |
| Bad | No frames arrive after a `SET_PIDX` step | ACK timeout is still enforced by status polling or reader idle ticks. |
| Bad | Raw malformed `{PIDX}` line | Parse errors increase, but experiment record samples are unchanged. |

### 6. Tests Required

Add dashboard tests under `.codex/skills/pid-ai-serial/tests/`:

| Test point | Required assertions |
|---|---|
| ACKed loop command | A JSON file is created; command, loop_id, ACK detail, before samples, after samples, and before config are present. |
| Local send error | A JSON file is created with `result.status=error` and `response.kind=local_error`. |
| Cross-loop filtering | A `SET_PIDX` record does not include samples from a different `loop_id`. |
| Status summary | `snapshot()["experiment_recording"]` reports enabled state, count, latest record, and write errors. |
| Write API token | Missing token is rejected; matching token is accepted. |
| Silent ACK timeout | A pending auto-tune step moves to `ABORT` after `ack_timeout_seconds` without ingesting another frame. |

### 7. Wrong vs Correct

Wrong:

```python
# Treating write success as experiment success loses board rejection evidence.
record["status"] = "ack"
```

Correct:

```python
# Command history must first match a real ACK/ERR or local transport error.
self._experiment_recorder.attach_response(entry, parsed_ack_or_err, now)
```

Wrong:

```python
# Timeout only runs when ingesting a new frame, so a silent board can stay pending forever.
def ingest_parsed_frame(self, parsed):
    self._autotune_controller.plan_next_action(...)
```

Correct:

```python
# Polling or idle reads advance timeout checks even without new telemetry.
def tick_autotune(self, now=None):
    self._autotune_controller.plan_next_action(now=now)
```

Wrong:

```python
# Re-parsing raw serial text in recorder duplicates protocol rules.
fields = raw_line.split(",")
```

Correct:

```python
# Recorder receives only validated typed samples from DashboardState.
self._experiment_recorder.observe_sample(sample, now)
```

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
| Scoring rollback decisions with pre-ACK samples. | Old-parameter telemetry can hide a regression or trigger the wrong keep/rollback decision. | Filter the score input to samples after the matching ACK boundary. |
| Ignoring mismatched ACK/ERR while a command is pending. | Board or host transaction drift can leave auto-tune waiting forever or trusting the wrong result. | Abort and show the mismatched command/loop evidence. |
| Only checking ACK timeout when a new frame arrives. | A quiet board or dropped response can leave auto-tune pending forever. | Advance timeout from `/api/status`, reader idle ticks, or a timer. |
| Treating a rollback write as completed immediately. | The dashboard may continue to the next loop while the board still runs bad parameters. | Model rollback as its own pending command and wait for matching ACK. |
| Clearing auto-tune aborts on the next good frame. | Operators lose the reason the automatic write flow stopped. | Require explicit user action to re-enable after `ABORT`. |

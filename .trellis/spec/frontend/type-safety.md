# Type Safety

> Type and runtime-validation patterns for future host-side protocol code.

---

## Overview

The current repository is C-only, but any future TypeScript/Python host tool must
model protocol frames explicitly. The source of truth is
`docs/pid_ai_serial_protocol.md`, backed by `src/pid_ai_protocol.c`.

Protocol keywords remain ASCII:

```text
{PID} {PIDX} {CFG} {CFGX} {SENS} {ACK} {ERR} {CMD}
```

---

## Type Organization

Recommended TypeScript types for a future frontend:

```ts
type PidFrame = {
  seq: number;
  ms: number;
  dt_ms: number;
  target: number;
  feedback: number;
  error: number;
  d_error: number;
  integral: number;
  p_out: number;
  i_out: number;
  d_out: number;
  ff_out: number;
  out_raw: number;
  out_limited: number;
  actuator: number;
  out_min: number;
  out_max: number;
  sat: -1 | 0 | 1;
  anti_windup: 0 | 1;
  mode: 0 | 1 | 2;
  enable: 0 | 1;
  sensor_ok: 0 | 1;
  fault: number;
};
```

Keep these types in a protocol module, not inside chart or form components.

Multi-loop tools must extend the legacy types rather than overloading `{PID}` /
`{CFG}` with optional loop fields:

```ts
type LoopId = string;

type PidXFrame = PidFrame & {
  loop_id: LoopId;
  loop_name: string;
};

type CfgXFrame = {
  loop_id: LoopId;
  loop_name: string;
  kp: number;
  ki: number;
  kd: number;
  kf: number;
  sample_ms: number;
  integral_min: number;
  integral_max: number;
  out_min: number;
  out_max: number;
  reverse: 0 | 1;
  mode: 0 | 1 | 2;
  version: number;
  fault: number;
};

type SensFrame = {
  ms: number;
  line0: 0 | 1;
  line1: 0 | 1;
  line2: 0 | 1;
  line3: 0 | 1;
  line4: 0 | 1;
  line5: 0 | 1;
  line6: 0 | 1;
  line7: 0 | 1;
  line_pos: number;
  line_lost: 0 | 1;
  yaw: number;
  yaw_rate: number;
  enc_l: number;
  enc_r: number;
  v_l: number;
  v_r: number;
  v_avg: number;
  battery: number;
};
```

---

## Validation

Every incoming serial line must be parsed and validated before it reaches UI or
AI diagnosis code.

| Frame | Required Validation |
|---|---|
| `{PID}` | Prefix, exact field count `23`, numeric parsing, enum ranges for `sat`, `mode`, booleans. |
| `{PIDX}` | Prefix, exact field count `25`, non-empty safe `loop_id`/`loop_name`, then all `{PID}` numeric and enum checks. |
| `{CFG}` | Prefix, exact field count `14`, numeric parsing, mode range, version number. |
| `{CFGX}` | Prefix, exact field count `16`, non-empty safe `loop_id`/`loop_name`, then all `{CFG}` numeric and enum checks. |
| `{SENS}` | Prefix, exact field count `19`, finite numeric parsing, eight `lineN` booleans, `line_lost` boolean. |
| `{ACK}` | Prefix, command string, detail string. |
| `{ERR}` | Prefix, command string, known status text, detail string. |

Reject partial/truncated frames instead of filling missing fields with defaults.
For UI display, keep a parse error counter and optionally show the last bad line
for debugging.

Numeric parsing must also reject non-finite values such as `nan`, `inf`, and
`-inf`. Python's `float()` accepts these strings by default, so host parsers must
explicitly check `math.isfinite()` before marking a typed frame valid. Text
fields such as `loop_id` and `loop_name` are not arbitrary user strings: reject
empty values and values containing separators or control characters before they
enter typed state or command matching.

## Scenario: Reject Non-Finite Telemetry Values

### 1. Scope / Trigger

Use this contract for Python, TypeScript, or any future host parser that consumes
`{PID}`, `{PIDX}`, `{CFG}`, `{CFGX}`, or `{SENS}` frames. Host tools must not pass
NaN or infinity into charting, diagnosis, command suggestions, auto-tune scoring,
or board-confirmed config state.

### 2. Signatures

Current Python parser:

```python
def parse_number(field_name: str, value: str) -> int | float
def parse_frame(line: str) -> dict
```

`parse_frame()` returns a dictionary containing at least:

```text
kind, valid, raw, data, error
```

### 3. Contracts

| Field category | Parser behavior |
|---|---|
| Integer fields such as `seq`, `ms`, `fault` | Use integer parsing; bad text raises/records parse failure. |
| Float fields such as `target`, `feedback`, `kp` | Use float parsing and then finite-number validation. |
| `nan`, `inf`, `-inf` | Reject frame with `valid=False`; include `<field> must be finite` in `error`. |
| Valid finite numeric fields | Return parsed numeric data and continue enum/range checks. |

### 4. Validation & Error Matrix

| Input line | Expected `kind` | Expected `valid` | Expected `error` |
|---|---|---:|---|
| `{PID}` sample from protocol docs | `pid` | `True` | `None` |
| `{PIDX}` sample from protocol docs | `pidx` | `True` | `None` |
| `{CFGX}` sample from protocol docs | `cfgx` | `True` | `None` |
| `{SENS}` sample from protocol docs | `sens` | `True` | `None` |
| `{PID}` with `target=nan` | `pid` | `False` | Contains `target must be finite` |
| `{PIDX}` with empty `loop_id` | `pidx` | `False` | Contains `loop_id` |
| `{CFG}` with `kp=inf` | `cfg` | `False` | Contains `kp must be finite` |
| `{PID}` with `mode=9` | `pid` | `False` | Contains `mode out of range` |
| `{SENS}` with `line_lost=3` | `sens` | `False` | Contains `line_lost out of range` |

### 5. Good/Base/Bad Cases

| Case | Example | Required behavior |
|---|---|---|
| Good | Finite protocol sample from docs | Enters typed state. |
| Base | Malformed number such as `abc` | Rejected as parse failure. |
| Bad | `nan` or `inf` accepted by language runtime | Still rejected before entering typed state. |
| Bad | `{PIDX}` with malformed `loop_id` | Rejected before loop state or pending-command matching. |

### 6. Tests Required

Add parser tests under `.codex/skills/pid-ai-serial/tests/`:

| Test point | Required assertions |
|---|---|
| Valid `{PID}` sample | `valid is True`; key fields parsed. |
| Valid `{PIDX}` / `{CFGX}` / `{SENS}` sample | `valid is True`; exact field names and key safety fields parsed. |
| `{PID}` float field set to `nan` | `valid is False`; error names the field and finite requirement. |
| `{CFG}` float field set to `inf` | `valid is False`; error names the field and finite requirement. |
| Invalid `{PIDX}` / `{CFGX}` text field | `valid is False`; previous loop state remains unchanged. |
| Invalid `{SENS}` boolean field | `valid is False`; auto-tune safety state is not cleared by the bad frame. |

### 7. Wrong vs Correct

Wrong:

```python
return float(value)
```

Correct:

```python
parsed = float(value)
if not math.isfinite(parsed):
    raise ValueError(f"{field_name} must be finite")
return parsed
```

---

## Common Patterns

### Discriminated Union for Frames

```ts
type ProtocolFrame =
  | { kind: "pid"; data: PidFrame }
  | { kind: "pidx"; data: PidXFrame }
  | { kind: "cfg"; data: CfgFrame }
  | { kind: "cfgx"; data: CfgXFrame }
  | { kind: "sens"; data: SensFrame }
  | { kind: "ack"; data: AckFrame }
  | { kind: "err"; data: ErrFrame };
```

### Status Text Must Match C API

`PIDAI_ProtocolStatusText()` returns these stable values:

| Status Text |
|---|
| `OK` |
| `BAD_PREFIX` |
| `UNKNOWN` |
| `ARG_MISSING` |
| `ARG_INVALID` |
| `PARAM_RANGE` |
| `INTERNAL_ERROR` |
| `UNKNOWN_STATUS` |

Host code should treat unknown statuses as parse/compatibility warnings.
`LOOP_NOT_FOUND` is an ERR detail, not a status enum. Host code should surface it
on `*X` commands and leave every loop's confirmed config unchanged.

### Command Builders

Build commands from typed values and format them with the protocol prefix:

```ts
function buildSetPidCommand(kp: number, ki: number, kd: number): string {
  return `{CMD}SET_PID,${kp.toFixed(3)},${ki.toFixed(3)},${kd.toFixed(3)}`;
}

function buildSetPidXCommand(loopId: LoopId, kp: number, ki: number, kd: number): string {
  return `{CMD}SET_PIDX,${loopId},${kp.toFixed(3)},${ki.toFixed(3)},${kd.toFixed(3)}`;
}
```

Validate ranges before sending, but still rely on board `{ACK}` / `{ERR}` as the
final authority.
Auto-tune command builders must preserve the target `loop_id` and format numeric
parameters with three decimals so pending-command matching and rollback history
stay deterministic.

---

## Forbidden Patterns

| Forbidden Pattern | Why |
|---|---|
| Indexing raw CSV fields throughout UI code. | Field-order bugs spread across the app. |
| Using `any` for parsed protocol frames. | Safety-critical fields like `fault` and `sensor_ok` become easy to ignore. |
| Filling missing numeric fields with `0`. | Truncated frames can look like valid safe values. |
| Inferring enum ranges only from UI labels. | Protocol docs and C enums are the source of truth. |
| Sending free-form command strings from arbitrary components. | Unsafe commands can bypass validation and confirmation. |
| Treating `{PIDX}` as `{PID}` plus a display label only. | Auto-tune and ACK matching need `loop_id` as a routing key. |
| Reusing stale loop state after a bad frame without recording parse error. | Operators lose evidence that the serial stream is corrupt. |

---

## Common Mistakes

| Mistake | Prevention |
|---|---|
| Forgetting that `{PID}` has 23 fields. | Keep a parser test with the exact sample from `docs/pid_ai_serial_protocol.md`. |
| Forgetting that `{PIDX}` and `{CFGX}` prepend `loop_id,loop_name`. | Test exact field counts `25` and `16` using protocol samples. |
| Treating `{SENS}.line_lost` as informational only. | Gate or abort line-car auto-tune when `line_lost == 1`. |
| Accepting Python `float("nan")` or `float("inf")` as valid telemetry. | Add parser tests that set a `{PID}` float field to `nan` and a `{CFG}` float field to `inf`; both must return `valid=False`. |
| Mixing `feedback` and `actuator`. | Use typed field names and UI labels from protocol docs. |
| Treating `sat` as boolean. | Model it as `-1 | 0 | 1`. |
| Treating non-zero `fault` as display-only. | Gate AI tuning and risky commands when faults are active. |

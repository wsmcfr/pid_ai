# Type Safety

> Type and runtime-validation patterns for future host-side protocol code.

---

## Overview

The current repository is C-only, but any future TypeScript/Python host tool must
model protocol frames explicitly. The source of truth is
`docs/pid_ai_serial_protocol.md`, backed by `src/pid_ai_protocol.c`.

Protocol keywords remain ASCII:

```text
{PID} {CFG} {ACK} {ERR} {CMD}
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

---

## Validation

Every incoming serial line must be parsed and validated before it reaches UI or
AI diagnosis code.

| Frame | Required Validation |
|---|---|
| `{PID}` | Prefix, exact field count `23`, numeric parsing, enum ranges for `sat`, `mode`, booleans. |
| `{CFG}` | Prefix, exact field count `14`, numeric parsing, mode range, version number. |
| `{ACK}` | Prefix, command string, detail string. |
| `{ERR}` | Prefix, command string, known status text, detail string. |

Reject partial/truncated frames instead of filling missing fields with defaults.
For UI display, keep a parse error counter and optionally show the last bad line
for debugging.

Numeric parsing must also reject non-finite values such as `nan`, `inf`, and
`-inf`. Python's `float()` accepts these strings by default, so host parsers must
explicitly check `math.isfinite()` before marking a typed frame valid.

## Scenario: Reject Non-Finite Telemetry Values

### 1. Scope / Trigger

Use this contract for Python, TypeScript, or any future host parser that consumes
`{PID}` or `{CFG}` frames. Host tools must not pass NaN or infinity into charting,
diagnosis, command suggestions, or board-confirmed config state.

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
| `{PID}` with `target=nan` | `pid` | `False` | Contains `target must be finite` |
| `{CFG}` with `kp=inf` | `cfg` | `False` | Contains `kp must be finite` |
| `{PID}` with `mode=9` | `pid` | `False` | Contains `mode out of range` |

### 5. Good/Base/Bad Cases

| Case | Example | Required behavior |
|---|---|---|
| Good | Finite protocol sample from docs | Enters typed state. |
| Base | Malformed number such as `abc` | Rejected as parse failure. |
| Bad | `nan` or `inf` accepted by language runtime | Still rejected before entering typed state. |

### 6. Tests Required

Add parser tests under `.codex/skills/pid-ai-serial/tests/`:

| Test point | Required assertions |
|---|---|
| Valid `{PID}` sample | `valid is True`; key fields parsed. |
| `{PID}` float field set to `nan` | `valid is False`; error names the field and finite requirement. |
| `{CFG}` float field set to `inf` | `valid is False`; error names the field and finite requirement. |

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
  | { kind: "cfg"; data: CfgFrame }
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

### Command Builders

Build commands from typed values and format them with the protocol prefix:

```ts
function buildSetPidCommand(kp: number, ki: number, kd: number): string {
  return `{CMD}SET_PID,${kp.toFixed(3)},${ki.toFixed(3)},${kd.toFixed(3)}`;
}
```

Validate ranges before sending, but still rely on board `{ACK}` / `{ERR}` as the
final authority.

---

## Forbidden Patterns

| Forbidden Pattern | Why |
|---|---|
| Indexing raw CSV fields throughout UI code. | Field-order bugs spread across the app. |
| Using `any` for parsed protocol frames. | Safety-critical fields like `fault` and `sensor_ok` become easy to ignore. |
| Filling missing numeric fields with `0`. | Truncated frames can look like valid safe values. |
| Inferring enum ranges only from UI labels. | Protocol docs and C enums are the source of truth. |
| Sending free-form command strings from arbitrary components. | Unsafe commands can bypass validation and confirmation. |

---

## Common Mistakes

| Mistake | Prevention |
|---|---|
| Forgetting that `{PID}` has 23 fields. | Keep a parser test with the exact sample from `docs/pid_ai_serial_protocol.md`. |
| Accepting Python `float("nan")` or `float("inf")` as valid telemetry. | Add parser tests that set a `{PID}` float field to `nan` and a `{CFG}` float field to `inf`; both must return `valid=False`. |
| Mixing `feedback` and `actuator`. | Use typed field names and UI labels from protocol docs. |
| Treating `sat` as boolean. | Model it as `-1 | 0 | 1`. |
| Treating non-zero `fault` as display-only. | Gate AI tuning and risky commands when faults are active. |

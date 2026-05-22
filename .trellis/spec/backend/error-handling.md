# Error Handling

> How errors are represented, propagated, and exposed by the board-side library.

---

## Overview

This project uses explicit C return codes, fault bitmaps, and protocol-level
status enums. It does not use exceptions, global `errno` as a public API, dynamic
error objects, or logging side effects.

| Layer | Error Mechanism | Example |
|---|---|---|
| Core PID API | Integer return codes and `pid->fault` bits | `PIDAI_SetTunings()` returns `-2` and sets `PIDAI_FAULT_PARAM_RANGE` |
| PID update loop | Safe actuator output plus `fault` state | Bad `dt_ms` returns actuator `0.0f` and sets `PIDAI_FAULT_BAD_DT` |
| Protocol parser | `PIDAI_CommandResult` | `{ERR}SET_PID,ARG_INVALID,FLOAT_PARSE_FAIL` |
| Frame builder | Negative integer return codes | `PIDAI_ProtocolBuildTelemetry()` returns `-2` when the buffer is too small |

---

## Error Types

### Core Fault Bits

`include/pid_ai.h` defines `PIDAI_Fault` as bit values stored in
`PIDAI_Handle.fault`.

| Fault | Meaning | Typical Trigger |
|---|---|---|
| `PIDAI_FAULT_NONE` | No current fault | Initial state or after `PIDAI_ClearFault()` |
| `PIDAI_FAULT_BAD_POINTER` | Reserved for pointer faults | Currently public functions return `-1` for null pointers |
| `PIDAI_FAULT_BAD_DT` | Invalid control period | `PIDAI_Update(..., dt_ms <= 0)` |
| `PIDAI_FAULT_BAD_LIMIT` | Invalid min/max limits | `PIDAI_SetOutputLimits(pid, 100, 0)` |
| `PIDAI_FAULT_PARAM_RANGE` | Invalid or out-of-range parameter | NaN, infinity, negative PID gains, unsupported mode |

Fault bits are sticky until `PIDAI_ClearFault()` or `PIDAI_Reset()` clears them.

### Protocol Command Status

`include/pid_ai_protocol.h` defines `PIDAI_CommandStatus`.

| Status | Meaning | Output Frame |
|---|---|---|
| `PIDAI_CMD_OK` | Command parsed and executed | `{ACK}command,detail` |
| `PIDAI_CMD_BAD_PREFIX` | Missing `{CMD}` | `{ERR}UNKNOWN,BAD_PREFIX,EXPECTED_CMD_PREFIX` |
| `PIDAI_CMD_UNKNOWN` | Unsupported command | `{ERR}<command>,UNKNOWN,COMMAND_NOT_SUPPORTED` |
| `PIDAI_CMD_ARG_MISSING` | Required argument absent | `{ERR}<command>,ARG_MISSING,<detail>` |
| `PIDAI_CMD_ARG_INVALID` | Argument parse failed | `{ERR}<command>,ARG_INVALID,FLOAT_PARSE_FAIL` |
| `PIDAI_CMD_PARAM_RANGE` | Parsed value rejected by core API | `{ERR}<command>,PARAM_RANGE,<detail>` |
| `PIDAI_CMD_INTERNAL_ERROR` | Null input or unexpected core failure | `{ERR}<command>,INTERNAL_ERROR,<detail>` |

Command argument counts are exact. If a command includes fields beyond the
documented format, treat it as `PIDAI_CMD_ARG_INVALID` with `detail =
"UNEXPECTED_ARG"` instead of applying the command and ignoring the tail.

---

## Error Handling Patterns

### Public C API Return Codes

Public setters return:

| Return | Meaning |
|---:|---|
| `0` | Success |
| `-1` | Null pointer or invalid required pointer |
| `-2` | Parsed/typed input exists but violates range or contract |

Example from `src/pid_ai.c`:

```c
if (pid == 0) {
    return -1;
}

if (!PIDAI_IsValidLimit(out_min, out_max)) {
    pid->fault |= PIDAI_FAULT_BAD_LIMIT;
    return -2;
}
```

Keep this distinction for new APIs so protocol code can map errors precisely.

### Fail Safe in the Control Loop

`PIDAI_Update()` must prefer safe output over partial computation when runtime
inputs are invalid.

Existing behavior:

| Condition | Behavior |
|---|---|
| `pid == 0` | Return `0.0f` immediately |
| Invalid `feedback` | Mark `sensor_ok = 0`, set `PIDAI_FAULT_PARAM_RANGE`, then stop output |
| Invalid `dt_ms` | Set `PIDAI_FAULT_BAD_DT`, clear outputs, return actuator `0.0f` |
| Disabled, stopped, or sensor fault | Clear outputs and return actuator `0.0f` |

Do not let invalid sensor data or invalid time deltas produce actuator output.

### Protocol Parser Mapping

`PIDAI_ProtocolHandleCommand()` maps parsing and core API errors to
`PIDAI_CommandResult`. Maintain the mapping style:

```c
ret = PIDAI_SetTunings(pid, kp, ki, kd);
if (ret == -2) {
    return PIDAI_MakeResult(PIDAI_CMD_PARAM_RANGE, command, "KP_KI_KD_OUT_OF_RANGE");
}
if (ret != 0) {
    return PIDAI_MakeResult(PIDAI_CMD_INTERNAL_ERROR, command, "SET_TUNINGS_FAIL");
}
```

### Scenario: Exact Command Argument Contracts

#### 1. Scope / Trigger

Use this contract whenever `src/pid_ai_protocol.c` adds or changes a `{CMD}`
handler. Command parsing is a safety boundary: accepting a malformed command can
change board state while hiding a host-side command builder bug.

#### 2. Signatures

```c
PIDAI_CommandResult PIDAI_ProtocolHandleCommand(PIDAI_Handle *pid, const char *line);
```

Supported command payloads are exactly the forms documented in
`docs/pid_ai_serial_protocol.md`, for example:

```text
{CMD}SET_PID,kp,ki,kd
{CMD}SET_TARGET,target
{CMD}RESET
{CMD}SET_PIDX,loop_id,kp,ki,kd
{CMD}GET_CFGX,loop_id
```

#### 3. Contracts

| Input shape | Contract |
|---|---|
| Missing `{CMD}` prefix | Return `PIDAI_CMD_BAD_PREFIX`; do not touch `PIDAI_Handle`. |
| Missing required field | Return `PIDAI_CMD_ARG_MISSING`; do not call the core setter. |
| Parse failure | Return `PIDAI_CMD_ARG_INVALID`; do not call the core setter. |
| Extra field, including trailing comma | Return `PIDAI_CMD_ARG_INVALID` with `detail="UNEXPECTED_ARG"`; do not call the core setter. |
| Parsed value rejected by core setter | Return `PIDAI_CMD_PARAM_RANGE`; setter may set a sticky fault bit. |
| Exact valid command | Call the core setter, return `PIDAI_CMD_OK`. |

`PIDAI_NextToken()` uses the internal `PIDAI_TRAILING_EMPTY_TOKEN` marker so
`"{CMD}SET_PID,1,2,3,"` is not confused with a clean end of input.

#### 4. Validation & Error Matrix

| Command line | Expected status | Expected detail | State update |
|---|---|---|---|
| `{CMD}SET_PID,1.0,0.1,0.01` | `PIDAI_CMD_OK` | `OK` | Update `kp/ki/kd`. |
| `{CMD}SET_PID,1.0,0.1` | `PIDAI_CMD_ARG_MISSING` | `NEED_KP_KI_KD` | No update. |
| `{CMD}SET_PID,1.0,nope,0.01` | `PIDAI_CMD_ARG_INVALID` | `FLOAT_PARSE_FAIL` | No update. |
| `{CMD}SET_PID,1.0,0.1,0.01,9` | `PIDAI_CMD_ARG_INVALID` | `UNEXPECTED_ARG` | No update. |
| `{CMD}SET_PID,1.0,0.1,0.01,` | `PIDAI_CMD_ARG_INVALID` | `UNEXPECTED_ARG` | No update. |
| `{CMD}SET_PIDX,missing,1.0,0.1,0.01` | `PIDAI_CMD_ARG_INVALID` | `LOOP_NOT_FOUND` | No update to any loop. |

#### 5. Good/Base/Bad Cases

| Case | Example | Required behavior |
|---|---|---|
| Good | Exact field count with valid values | Apply command and ACK. |
| Base | Missing required argument | Return ERR and keep state unchanged. |
| Bad | Extra field or trailing comma | Return ERR and keep state unchanged. |

#### 6. Tests Required

Add or update `tests/test_pid_ai.c` with assertions for:

| Test point | Required assertions |
|---|---|
| Exact valid command | `result.status == PIDAI_CMD_OK`; affected fields changed. |
| Extra argument | `result.status == PIDAI_CMD_ARG_INVALID`; `detail == "UNEXPECTED_ARG"`; affected fields unchanged. |
| Trailing empty argument | Same as extra argument. |
| Missing argument | `result.status == PIDAI_CMD_ARG_MISSING`; affected fields unchanged. |
| Unknown `loop_id` for `*X` commands | `result.status == PIDAI_CMD_ARG_INVALID`; `detail == "LOOP_NOT_FOUND"`; all loop handles unchanged. |

#### 7. Wrong vs Correct

Wrong:

```c
arg_kp = PIDAI_NextToken(&cursor);
arg_ki = PIDAI_NextToken(&cursor);
arg_kd = PIDAI_NextToken(&cursor);
return PIDAI_SetTunings(pid, kp, ki, kd);
```

Correct:

```c
arg_kp = PIDAI_NextToken(&cursor);
arg_ki = PIDAI_NextToken(&cursor);
arg_kd = PIDAI_NextToken(&cursor);
if (!PIDAI_NoMoreTokens(cursor)) {
    return PIDAI_MakeResult(PIDAI_CMD_ARG_INVALID, command, "UNEXPECTED_ARG");
}
```

---

### Scenario: Multi-Loop Command Routing

#### 1. Scope / Trigger

Use this contract whenever adding or changing commands handled by
`PIDAI_ProtocolHandleCommandX()`. Multi-loop routing is safety-critical because
the host may tune `speed_l`, `speed_r`, `yaw_rate`, and `line_outer` with
different stability assumptions.

#### 2. Signatures

```c
const PIDAI_LoopRoute *PIDAI_ProtocolFindLoop(const PIDAI_LoopTable *table, const char *loop_id);
PIDAI_CommandResult PIDAI_ProtocolHandleCommandX(const PIDAI_LoopTable *table, const char *line);
```

#### 3. Contracts

| Input shape | Contract |
|---|---|
| `table == NULL`, `routes == NULL`, or `count == 0` | Return `PIDAI_CMD_INTERNAL_ERROR`; do not touch PID handles. |
| Missing `loop_id` | Return `PIDAI_CMD_ARG_MISSING`; do not call any core setter. |
| Unknown `loop_id` | Return `PIDAI_CMD_ARG_INVALID` with `detail="LOOP_NOT_FOUND"`. |
| Bad numeric argument after valid `loop_id` | Return `PIDAI_CMD_ARG_INVALID`; do not update the matched loop. |
| Extra field, including trailing comma | Return `PIDAI_CMD_ARG_INVALID` with `detail="UNEXPECTED_ARG"`. |
| Exact valid `*X` command | Apply only the matched loop handle and return `PIDAI_CMD_OK`. |
| `GET_ALL_CFG` | Return ACK for the command handler; board/application code is responsible for emitting each `{CFGX}` snapshot in loop-table order. |

#### 4. Required `*X` Command Coverage

| Command | Minimum failure coverage |
|---|---|
| `SET_PIDX` | Success, unknown loop, missing PID field, bad float, extra argument, trailing comma. |
| `SET_KFX` / `SET_TARGETX` | Bad float and unknown loop. |
| `SET_OUT_LIMITX` / `SET_I_LIMITX` | Bad limit order maps through core setter as `PARAM_RANGE`. |
| `RESET_IX` / `ENABLEX` | Unknown loop and extra argument. |
| `GET_CFGX` / `GET_ALL_CFG` | Unknown loop for `GET_CFGX`; no extra arguments for `GET_ALL_CFG`. |

#### 5. Good/Base/Bad Cases

| Case | Example | Required behavior |
|---|---|---|
| Good | `{CMD}SET_PIDX,speed_l,1.0,0.1,0.01` | Only `speed_l` changes and ACK names `SET_PIDX`. |
| Base | `{CMD}SET_PIDX,missing,1.0,0.1,0.01` | ERR `ARG_INVALID/LOOP_NOT_FOUND`; no loop changes. |
| Bad | `{CMD}SET_PIDX,speed_l,1.0,0.1,0.01,9` | ERR `ARG_INVALID/UNEXPECTED_ARG`; matched loop remains unchanged. |

---

## API Error Responses

The serial protocol is the API response layer.

| Result | Frame Builder | Format |
|---|---|---|
| Success | `PIDAI_ProtocolBuildAck()` | `{ACK}command,detail\r\n` |
| Failure | `PIDAI_ProtocolBuildError()` | `{ERR}command,status,detail\r\n` |
| Telemetry state | `PIDAI_ProtocolBuildTelemetry()` | `{PID}...fault\r\n` |
| Config state | `PIDAI_ProtocolBuildConfig()` | `{CFG}...fault\r\n` |
| Multi-loop telemetry state | `PIDAI_ProtocolBuildTelemetryX()` | `{PIDX}loop_id,loop_name,...fault\r\n` |
| Multi-loop config state | `PIDAI_ProtocolBuildConfigX()` | `{CFGX}loop_id,loop_name,...fault\r\n` |

Frame builders return `-1` for bad pointers/zero buffers and `-2` for buffer
capacity failures, as implemented by `PIDAI_CheckFormatResult()`.

---

## Common Mistakes

| Mistake | Why It Is Wrong | Correct Pattern |
|---|---|---|
| Returning success while only setting `fault`. | Callers and protocol handlers cannot know the command failed. | Return `-2` for rejected values and set the fault bit. |
| Clearing `fault` automatically on every successful setter. | A later success would hide earlier runtime faults. | Keep fault sticky until `PIDAI_ClearFault()` or reset. |
| Treating parse failures and range failures as the same error. | Host UI cannot tell bad text from unsafe values. | Use `ARG_INVALID` for parsing and `PARAM_RANGE` for rejected values. |
| Emitting actuator output after bad `dt_ms` or sensor data. | Unsafe for real hardware. | Force outputs to `0.0f` and expose the fault. |
| Ignoring frame builder return values in board code. | UART may transmit stale or truncated data. | Send only when `written > 0`, as in `examples/pid_ai_board_example.c`. |
| Ignoring extra command fields. | Host command builders can be wrong while the board still applies a partial command. | Reject the command with `ARG_INVALID/UNEXPECTED_ARG` before calling the core setter. |
| Falling back to the first loop when `loop_id` is unknown. | A typo from the host could retune the wrong controller. | Return `ARG_INVALID/LOOP_NOT_FOUND` and keep every loop unchanged. |

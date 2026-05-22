# Quality Guidelines

> Quality standards for future host-side UI and serial tooling.

---

## Overview

Frontend quality is defined by protocol correctness, operator safety, stable live
data handling, and testable parsing. The current repository has no frontend build
tool yet, so any future host app must introduce its own lint/test commands and
document them here.

---

## Forbidden Patterns

| Forbidden Pattern | Why |
|---|---|
| Marking PID parameters as applied before `{ACK}`. | The board may reject the command or never receive it. |
| Hiding `sensor_ok`, `fault`, `sat`, or `anti_windup`. | These are required to avoid unsafe or misleading tuning decisions. |
| Parsing protocol frames inside visual components. | Makes parser behavior hard to unit-test. |
| Keeping unbounded telemetry in rendering state. | Long sessions can exhaust memory or make charts unusable. |
| Sending commands without preview/confirmation for risky actions. | Commands can move real hardware. |
| Changing protocol field names/order in frontend without updating docs/tests. | Host and board contracts drift. |

---

## Required Patterns

| Required Pattern | Reason |
|---|---|
| Unit-test protocol parsers against samples from `docs/pid_ai_serial_protocol.md`. | Frame contracts are the frontend foundation. |
| Validate incoming field counts and enum ranges. | Bad serial data must not become trusted UI state. |
| Keep draft form state separate from board-confirmed config. | Prevents UI from overstating applied settings. |
| Persist command history with ACK/ERR details during a session. | Operators need an audit trail of tuning changes. |
| Gate AI tuning suggestions when `sensor_ok == 0`, `fault != 0`, or controller is stopped. | Invalid control data should not drive parameter recommendations. |
| Use accessible status indicators with text and color. | Safety states must be clear to all operators. |

---

## Testing Requirements

When frontend code is added, include these tests before relying on the tool:

| Test Area | Required Assertions |
|---|---|
| `{PID}` parser | Parses all 23 fields, rejects missing/extra fields, validates enum ranges. |
| `{CFG}` parser | Parses all 14 fields and version/fault values. |
| ACK/ERR handling | Pending command becomes applied on ACK and rejected on ERR. |
| Command builder | Produces exact `{CMD}` strings for `SET_PID`, `SET_TARGET`, limits, mode, enable. |
| Safety gates | AI/auto-send actions are blocked on sensor fault, active fault bits, or stopped mode. |
| Telemetry buffer | Long streams remain bounded and preserve latest sample. |

Add the actual project commands here after a frontend toolchain exists, for
example:

```text
<frontend package manager> lint
<frontend package manager> test
<frontend package manager> build
```

---

## Code Review Checklist

| Check | Question |
|---|---|
| Protocol | Does the parser match `docs/pid_ai_serial_protocol.md` exactly? |
| Safety | Are fault/sensor/saturation states visible and used as gates? |
| Commands | Does every write wait for ACK/ERR and expose failure details? |
| Performance | Are high-frequency samples buffered without broad re-renders? |
| Accessibility | Are critical states readable without color-only cues? |
| Tests | Are parsers, command builders, and safety gates covered? |
| Documentation | Were protocol docs/specs updated if a contract changed? |

---

## Common Mistakes

| Mistake | Corrective Rule |
|---|---|
| Treating a serial dashboard as a generic charting app. | Design around PID diagnosis fields and safety state first. |
| Letting AI suggestions auto-apply by default. | First implementation should require human confirmation, as README recommends. |
| Recording only chart-visible series. | Experiment records should keep the full typed frame for replay and diagnosis. |
| Allowing free-form command input as the primary workflow. | Use typed builders and command previews; keep raw command input as an advanced/debug tool only. |

# Component Guidelines

> Component conventions for future PID tuning and telemetry interfaces.

---

## Overview

No UI components exist yet. Future components should be designed as operational
tools for repeated tuning sessions, not as landing-page or marketing pages. The
first screen should help the user connect to a device, inspect live PID data, see
fault/saturation state, and send confirmed commands.

---

## Component Structure

Recommended component responsibilities:

| Component | Responsibility | Data Source |
|---|---|---|
| `ConnectionBar` | Serial port selection, connect/disconnect, receive status. | Serial connection state |
| `TelemetryChart` | Plot `target`, `feedback`, `error`, and selected PID terms. | Parsed `{PID}` samples |
| `PidTermPanel` | Show `p_out`, `i_out`, `d_out`, `ff_out`, `out_raw`, `out_limited`, `actuator`. | Latest `{PID}` sample |
| `SafetyStatus` | Show `sat`, `anti_windup`, `sensor_ok`, `fault`, `mode`, `enable`. | Latest `{PID}` / `{CFG}` |
| `TuningPanel` | Edit `kp`, `ki`, `kd`, `kf`, target, limits, mode, manual output. | Latest `{CFG}` and pending form state |
| `CommandHistory` | Show sent commands and received `{ACK}` / `{ERR}`. | Command transaction state |

Keep each component focused on a domain object. Avoid generic dashboards that
hide safety-critical PID state behind decorative cards.

---

## Props Conventions

Components should receive typed parsed data, not raw frame strings.

Good:

```ts
type TelemetryChartProps = {
  samples: PidFrame[];
  visibleSeries: TelemetrySeriesKey[];
};
```

Bad:

```ts
type TelemetryChartProps = {
  lines: string[];
};
```

Rationale: frame parsing is a protocol concern and must be unit-tested separately
from rendering.

---

## Styling Patterns

The application should feel like an engineering instrument:

| UI Area | Guidance |
|---|---|
| Layout | Dense but readable; prioritize scanability and side-by-side comparison. |
| Charts | Stable axes, clear legends, selectable series, no decorative gradients. |
| Safety state | Use visible status color and text for `fault`, `sensor_ok`, `sat`, and `anti_windup`. |
| Controls | Use numeric inputs with units/precision and explicit apply buttons. |
| Command preview | Show the exact `{CMD}` string before sending parameter changes. |

Do not build a marketing hero page for this tool. The primary UI is the tuning
workspace.

---

## Accessibility

| Requirement | Why |
|---|---|
| Do not rely on color alone for faults or saturation. | Operators must distinguish `sat`, `fault`, and sensor state reliably. |
| Label every PID numeric input with the protocol field name. | Prevents confusing `feedback`, `actuator`, and output limits. |
| Keep keyboard operation for connect, apply, stop, and reset actions. | Tuning often happens while watching live data. |
| Confirm risky commands such as `ENABLE,1`, `SET_MODE,2`, or wide output limits. | These can move real hardware. |

---

## Common Mistakes

| Mistake | Consequence | Correct Pattern |
|---|---|---|
| Displaying only `target`, `feedback`, and `actuator`. | User/AI cannot diagnose integral or saturation problems. | Include PID terms, saturation, anti-windup, mode, sensor, and fault state. |
| Updating displayed config immediately after send. | Command may fail or be dropped. | Mark as pending until `{ACK}` and preferably confirm with `{CFG}`. |
| Hiding `{ERR}` details in a toast only. | Operator can miss safety-relevant failures. | Persist command result in `CommandHistory` and status panels. |
| Using decorative oversized cards for all content. | Reduces density and slows tuning work. | Use compact panels, tables, and charts. |

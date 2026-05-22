# Directory Structure

> How future host-side frontend code should be organized.

---

## Overview

There is currently no frontend directory in the repository. If a host UI or
serial analysis tool is added, keep it clearly separated from the portable C
library while reusing the protocol contracts from `docs/`.

Recommended top-level layout for a future host application:

```text
host/
├── src/
│   ├── protocol/      # Parsers and command builders for {PID}/{CFG}/{ACK}/{ERR}/{CMD}.
│   ├── serial/        # Serial transport and connection lifecycle.
│   ├── features/
│   │   ├── telemetry/ # Curves, tables, sample buffers, export.
│   │   ├── tuning/    # PID parameter editing, command confirmation.
│   │   └── diagnosis/ # AI/user-facing diagnosis summaries.
│   ├── components/    # Shared UI primitives and widgets.
│   └── app/           # App shell, routing, providers if the chosen framework uses them.
├── tests/
└── README.md
```

Use `host/` rather than mixing UI source into `src/`, because `src/` currently
means portable C library implementation.

---

## Module Organization

| Module | Responsibility | Must Not Own |
|---|---|---|
| `protocol/` | Typed frame parsing, validation, command string generation. | UI rendering or serial port side effects. |
| `serial/` | Port open/close, reconnect, read/write, line buffering. | PID diagnosis rules or chart rendering. |
| `features/telemetry/` | Live samples, charts, CSV/export UI. | Raw parsing logic. |
| `features/tuning/` | PID parameter forms, command preview, ACK/ERR handling. | Low-level serial framing. |
| `features/diagnosis/` | Analysis of response, overshoot, oscillation, saturation. | Mutating board state directly. |

Keep protocol parsing framework-agnostic so it can be unit-tested without a
browser or serial device.

---

## Naming Conventions

| Item | Convention | Example |
|---|---|---|
| Protocol types | Use protocol frame names | `PidFrame`, `CfgFrame`, `AckFrame`, `ErrFrame` |
| Parser functions | `parse<FrameName>Frame` | `parsePidFrame(line)` |
| Command builders | `build<CommandName>Command` | `buildSetPidCommand(kp, ki, kd)` |
| React hooks if React is chosen | `use<Thing>` | `useSerialConnection`, `useTelemetryBuffer` |
| UI components | Domain-first names | `TelemetryChart`, `TuningPanel`, `FaultBanner` |
| Test files | Match source unit | `protocol.test.ts`, `TelemetryChart.test.tsx` |

---

## Existing References

| Source | Frontend Implication |
|---|---|
| `docs/pid_ai_serial_protocol.md` | Field order and frame prefixes must be copied exactly into parser tests. |
| `README.md` | Host duties include serial reading, curves, experiment records, AI diagnosis, and parameter sending. |
| `examples/pid_ai_board_example.c` | Shows board replies: ACK/ERR and extra CFG reply for `GET_CFG`. |
| `tests/test_pid_ai.c` | Shows baseline expectations for PID values and frame strings. |

---

## Anti-Patterns

| Do Not | Why |
|---|---|
| Put frontend files directly in `src/`. | It conflicts with the current C library meaning of `src/`. |
| Couple charts directly to raw serial strings. | Parser bugs become UI bugs and are hard to test. |
| Duplicate field-order constants in multiple feature folders. | Parser and UI labels will drift. |
| Treat future AI diagnosis as a UI component concern. | Diagnosis should be a separate domain module fed by typed samples. |

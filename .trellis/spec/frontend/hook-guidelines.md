# Hook Guidelines

> Conventions for future stateful frontend logic.

---

## Overview

No hooks exist yet. If a React-based host UI is added, hooks should separate
transport, parsing, buffering, and command transaction logic. Equivalent patterns
apply if another frontend framework is chosen.

---

## Custom Hook Patterns

| Hook | Responsibility | Should Return |
|---|---|---|
| `useSerialConnection` | Open/close serial port, expose connection status and write function. | `{ status, portInfo, connect, disconnect, writeLine }` |
| `useProtocolLineBuffer` | Convert byte chunks into complete `\r\n`-terminated lines. | `{ lines, clearLines }` or callback-driven delivery |
| `usePidTelemetry` | Parse `{PID}` lines and maintain a bounded sample buffer. | `{ latest, samples, droppedCount }` |
| `usePidConfig` | Track latest `{CFG}` frame and protocol version. | `{ config, updatedAt }` |
| `useCommandTransactions` | Send `{CMD}`, wait for `{ACK}`/`{ERR}`, expose pending/applied/error state. | `{ sendCommand, pending, history }` |

Keep parser functions outside hooks so they can be tested without rendering.

---

## Data Fetching

The host tool will consume serial streams, not HTTP server state.

| Data Source | Pattern |
|---|---|
| Live serial bytes | Event-driven transport in `serial/`; hooks subscribe to parsed lines. |
| `{PID}` telemetry | Bounded in-memory ring buffer; avoid unbounded arrays during long runs. |
| `{CFG}` snapshots | Keep latest snapshot plus history only if experiment recording is enabled. |
| AI diagnosis output | Derived from a stable window of typed samples, not directly from raw strings. |

Do not use polling for serial data if the platform provides event-driven reads.
If polling is necessary, centralize it in one transport hook.

---

## Naming Conventions

| Item | Convention |
|---|---|
| Hook names | Start with `use` and name the domain object, e.g. `usePidTelemetry`. |
| Parser helpers | Do not start with `use`; keep them pure, e.g. `parsePidFrame`. |
| Command helpers | Use `build...Command`, e.g. `buildSetTargetCommand`. |
| Returned callbacks | Use imperative names, e.g. `connect`, `disconnect`, `sendCommand`. |

---

## Common Mistakes

| Mistake | Why It Fails | Correct Pattern |
|---|---|---|
| Parsing serial lines inside a chart component. | Rendering and protocol validation become coupled. | Parse in protocol helpers and feed typed samples to components. |
| Keeping every telemetry sample in React state forever. | Long tuning sessions can consume excessive memory and render slowly. | Use bounded buffers and explicit export/recording flows. |
| Treating sent commands as applied immediately. | The board may return `{ERR}` or no response. | Track pending until `{ACK}` and show failures. |
| Spreading serial transport state across many hooks. | Reconnect and cleanup behavior becomes inconsistent. | Centralize port lifecycle in `useSerialConnection`. |

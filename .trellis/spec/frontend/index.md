# Frontend Development Guidelines

> Guidelines for future host-side UI or serial-analysis tools.

---

## Overview

This repository currently has no frontend application. The "frontend" side of
the product is planned host software that reads board serial frames, displays PID
curves, records experiments, and sends `{CMD}` commands after user or AI review.

Use these guidelines when adding a desktop/web/CLI host tool in this repository.
They are grounded in the existing protocol library and documentation.

| Existing Source of Truth | Why It Matters to Frontend Work |
|---|---|
| `docs/pid_ai_serial_protocol.md` | Defines `{PID}`, `{CFG}`, `{ACK}`, `{ERR}`, and `{CMD}` contracts. |
| `include/pid_ai_protocol.h` | Defines command status names and frame builder semantics. |
| `README.md` | Defines the intended board/host/AI feedback loop. |
| `tests/test_pid_ai.c` | Shows executable expectations for core PID and protocol behavior. |

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Where future host/UI code should live | Filled |
| [Component Guidelines](./component-guidelines.md) | PID-focused UI component conventions | Filled |
| [Hook Guidelines](./hook-guidelines.md) | Serial/data hooks and polling/streaming boundaries | Filled |
| [State Management](./state-management.md) | Runtime, config, command, and experiment state | Filled |
| [Quality Guidelines](./quality-guidelines.md) | Testing, accessibility, and protocol review requirements | Filled |
| [Type Safety](./type-safety.md) | Frame schemas, parser validation, command status types | Filled |

---

## Pre-Development Checklist

Before adding host-side UI or serial tooling, read:

| Change Type | Must Read |
|---|---|
| Serial parser or frame types | `frontend/type-safety.md`, `docs/pid_ai_serial_protocol.md` |
| Live chart or telemetry display | `frontend/component-guidelines.md`, `frontend/state-management.md` |
| Command sending or PID parameter controls | `frontend/state-management.md`, `frontend/quality-guidelines.md`, `docs/pid_ai_serial_protocol.md` |
| Custom hooks or stream processing | `frontend/hook-guidelines.md`, `frontend/type-safety.md` |
| New host project structure | `frontend/directory-structure.md` |

---

## Project Rules

| Rule | Reason |
|---|---|
| Frontend state must be derived from parsed protocol frames, not ad hoc strings. | The protocol field order is the project contract. |
| Do not mark commands as applied until `{ACK}` is received. | README and protocol docs explicitly require ACK confirmation. |
| Surface `{ERR}` details to the operator. | Bad commands, range failures, and sensor faults are safety-relevant. |
| Show `sensor_ok`, `fault`, `sat`, and `anti_windup` prominently in tuning workflows. | AI/user tuning decisions are unsafe without these fields. |
| Keep first UI screen functional, not marketing-focused. | This product is an operational tuning tool. |

---

**Language**: Spec documentation is written in English. UI copy can be localized later, but protocol keywords stay ASCII.

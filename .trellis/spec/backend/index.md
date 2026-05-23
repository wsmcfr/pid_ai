# Backend Development Guidelines

> Project-specific rules for the embedded C PID AI protocol library.

---

## Overview

This repository does not have a web backend. In this project, "backend" means the
portable C library that runs on the board side:

| Area | Files | Responsibility |
|---|---|---|
| Core PID engine | `include/pid_ai.h`, `src/pid_ai.c` | PID state, tunings, limits, modes, fault bits, and telemetry values |
| Shared protocol types | `include/pid_ai_protocol_types.h` | Multi-loop route/table types shared by text and binary protocol modules |
| Text serial protocol | `include/pid_ai_protocol.h`, `src/pid_ai_protocol.c` | `{CMD}` parsing and `{PID}` / `{CFG}` / `{ACK}` / `{ERR}` frame generation |
| Binary serial protocol | `include/pid_ai_binary_protocol.h`, `src/pid_ai_binary_protocol.c` | Binary PID/CFG/PIDX/CFGX frame generation and CRC validation |
| Board integration | `examples/pid_ai_board_example.c` | Demonstrates how MCU firmware should call the portable library |
| Regression tests | `tests/test_pid_ai.c` | PC-side C tests for PID behavior and protocol formatting |

The library is intended for MCU firmware, so implementation choices must favor
deterministic behavior, bounded memory, stable text protocol contracts, and simple
C APIs over framework-specific abstractions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | C module boundaries, public headers, examples, tests | Filled |
| [Database Guidelines](./database-guidelines.md) | Persistence guidance for this no-database embedded library | Filled |
| [Error Handling](./error-handling.md) | Return codes, fault bits, command status, ACK/ERR frames | Filled |
| [Quality Guidelines](./quality-guidelines.md) | C style, comments, tests, forbidden patterns | Filled |
| [Logging Guidelines](./logging-guidelines.md) | Serial telemetry and event-frame conventions instead of runtime logging | Filled |

---

## Pre-Development Checklist

Before changing board-side C code, read the relevant files below.

| Change Type | Must Read |
|---|---|
| PID algorithm, modes, limits, fault bits | `backend/directory-structure.md`, `backend/error-handling.md`, `backend/quality-guidelines.md`, `include/pid_ai.h`, `src/pid_ai.c` |
| Text serial command or frame fields | `backend/error-handling.md`, `backend/logging-guidelines.md`, `docs/pid_ai_serial_protocol.md`, `include/pid_ai_protocol_types.h`, `include/pid_ai_protocol.h`, `src/pid_ai_protocol.c` |
| Binary serial frame fields | `backend/logging-guidelines.md`, `docs/pid_ai_serial_protocol.md`, `include/pid_ai_protocol_types.h`, `include/pid_ai_binary_protocol.h`, `src/pid_ai_binary_protocol.c` |
| Board integration example | `backend/directory-structure.md`, `backend/logging-guidelines.md`, `examples/pid_ai_board_example.c` |
| Tests or behavior verification | `backend/quality-guidelines.md`, `tests/test_pid_ai.c` |
| Any persistence or experiment-recording idea | `backend/database-guidelines.md` first, because persistence is currently outside the board library |

---

## Project Rules

| Rule | Reason |
|---|---|
| Keep `include/` headers platform-neutral. | The same library must compile for STM32, ESP32, Arduino, and PC tests. |
| Do not add dynamic allocation to board-side code. | MCU firmware needs predictable RAM use and failure behavior. |
| Do not change protocol field order without updating `docs/pid_ai_serial_protocol.md` and tests. | Host parsers and AI diagnostics depend on stable frame contracts. |
| Add or update PC-side tests for every new public behavior. | Tests are the current executable safety net for the library. |
| Preserve Chinese code comments in source files. | Existing C code documents function purpose, flow, parameters, and return values in Chinese. |

---

**Language**: Spec documentation is written in English. Source-code comments remain Chinese to match the current codebase.

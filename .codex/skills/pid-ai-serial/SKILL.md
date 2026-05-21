---
name: pid-ai-serial
description: Use when Codex needs to find a PID AI board COM port, inspect Windows serial ports, open a local PID tuning dashboard, read live UART protocol frames, capture PID telemetry, parse PID AI serial output, or send explicit PID AI {CMD} commands.
---

# PID AI Serial

## Overview

Use this skill to detect the board-side serial port for the PID AI protocol,
open a local upper-computer dashboard, and read live `{PID}`, `{CFG}`, `{ACK}`,
`{ERR}`, `{STAT}`, and `{EVT}` frames.

The bundled scripts are the source of truth for serial probing and local
dashboard launch. Prefer running them instead of writing ad hoc COM-port code.

## Quick Start

From the repository root:

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py list
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py scan
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --auto --duration 10
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

By default, automatic scan skips Windows Bluetooth virtual ports because machines
can expose many inactive `BTHENUM` COM ports. Use `--include-bluetooth` only when
the board is intentionally connected through Bluetooth SPP.

If scan finds a board, read from that port:

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --port COM5 --baud 115200 --jsonl --duration 10
```

To open the local Web upper-computer UI directly:

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

The dashboard listens on `127.0.0.1`, chooses a free HTTP port if the preferred
port is busy, and keeps opening/scanning read-only. It sends `{CMD}` frames only
after the operator clicks a command button or an API caller explicitly posts a
command.

## Workflow

| Need | Command |
|---|---|
| See all serial ports | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py list` |
| Auto-detect the board | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py scan` |
| Include Bluetooth virtual ports | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py scan --include-bluetooth` |
| Read the detected board | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --auto --duration 10` |
| Capture structured frames | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py read --auto --jsonl --duration 10` |
| Send a command | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_serial.py send --auto --command "{CMD}GET_CFG"` |
| Open the local dashboard | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open` |
| Open dashboard on a known port | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --serial-port COM5 --baud 115200 --open` |

## Dashboard

The dashboard provides a local operational UI for tuning sessions:

| Area | Behavior |
|---|---|
| Connection | Lists COM ports, auto-detects the PID AI board, connects, and disconnects. |
| Waveform | Draws `target`, `feedback`, `error`, PID terms, `out_limited`, and `actuator` on Canvas. |
| Safety status | Shows `sensor_ok`, `fault`, `sat`, `anti_windup`, `mode`, and `enable`. |
| Tuning controls | Builds exact `{CMD}` strings for PID, target, limits, mode, enable, reverse, sensor status, reset, and fault clearing. |
| Command history | Keeps pending commands and updates them only when `{ACK}` or `{ERR}` is read. |

Useful local API endpoints:

```text
GET  /api/ports
GET  /api/status
GET  /api/samples?since=0
POST /api/connect
POST /api/disconnect
POST /api/command
```

## Detection Rules

The board is identified by line-oriented protocol frames. A port is considered a
candidate when it emits at least one known prefix:

```text
{PID} {CFG} {ACK} {ERR} {STAT} {EVT}
```

`{PID}` and `{CFG}` frames are weighted highest because they come from the board.
`{ACK}` and `{ERR}` can also identify the board after a command response.

Default scan baud rates are:

```text
115200, 921600, 57600, 38400, 19200, 9600
```

Default scan excludes Bluetooth virtual ports. This keeps auto-detection fast on
Windows systems that expose many paired-device COM ports unrelated to the board.

## Protocol Expectations

- `{PID}` must contain 23 comma-separated fields after the prefix.
- `{CFG}` must contain 14 comma-separated fields after the prefix.
- `{ACK}` is parsed as `command,detail`.
- `{ERR}` is parsed as `command,status,detail`.
- Unknown or malformed lines are reported as raw lines, not converted into fake valid frames.

## Safety Rules

| Rule | Reason |
|---|---|
| Do not send commands unless the user explicitly asks. | Commands can change real hardware behavior. |
| Prefer `--auto` only for read-only scan/read/dashboard-open operations. | Auto-detection is safe; writing should still be intentional. |
| Dashboard parameter buttons must preview the exact `{CMD}` string before send. | Operators need to see what will be written to the board. |
| Do not assume a command was applied until `{ACK}` is received. | The board may reject or miss the command. |
| Preserve raw lines in diagnostic output. | Raw protocol evidence is needed when parsing fails. |

## Common Issues

| Symptom | Likely Cause | Action |
|---|---|---|
| No ports found | Board disconnected or driver missing | Check Device Manager and USB cable. |
| Ports listed but scan finds no board | Wrong baud or board not emitting telemetry | Try `--baud-rates 115200,9600` and send `{CMD}GET_CFG` manually if safe. |
| Access denied | Another tool has the COM port open | Close serial monitors, IDE terminals, and plotting tools. |
| Raw unreadable bytes | Wrong baud or binary protocol | Try another baud or confirm firmware uses the text protocol. |

## Script

Main scripts:

```text
.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py
.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py
```

Run `--help` on the script or any subcommand for exact options.

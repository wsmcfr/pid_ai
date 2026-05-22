# Create PID AI Serial Skill

## Goal

Create a project-local Codex skill that can automatically find the board-side
serial port for the PID AI protocol and read live protocol frames.

## Requirements

- Add a `pid-ai-serial` skill under `.codex/skills/`.
- Include a reusable Python script that can:
  - list available serial ports,
  - scan likely baud rates,
  - identify PID AI board output by protocol prefixes,
  - parse `{PID}`, `{CFG}`, `{ACK}`, and `{ERR}` frames,
  - read a selected or auto-detected port,
  - send a `{CMD}` command when explicitly requested.
- Keep protocol parsing aligned with `docs/pid_ai_serial_protocol.md`.
- Avoid changing the portable C library.

## Acceptance Criteria

- [ ] `SKILL.md` has valid frontmatter and actionable usage instructions.
- [ ] `scripts/pid_ai_serial.py` supports `list`, `scan`, `read`, and `send`.
- [ ] The script can run in this environment for help/list operations.
- [ ] Skill validation passes with `quick_validate.py`.

## Technical Notes

- The script depends on `pyserial`; the current environment already has it.
- Default scan baud rates should include `115200`, because the protocol document
  recommends `115200 8N1` as the starting point.
- Auto-detection should look for `{PID}`, `{CFG}`, `{ACK}`, `{ERR}`, `{STAT}`,
  and `{EVT}` prefixes, with `{PID}` and `{CFG}` weighted highest.

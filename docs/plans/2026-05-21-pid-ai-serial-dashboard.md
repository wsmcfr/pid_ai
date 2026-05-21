# PID AI Serial Dashboard Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local Web dashboard inside the `pid-ai-serial` skill so invoking the skill can open a PID tuning upper-computer UI.

**Architecture:** Add one Python dashboard script beside the existing serial script. The dashboard reuses the current serial detection and parser functions, exposes a small local HTTP JSON API, stores bounded typed telemetry, and serves an embedded operational HTML/CSS/JS interface.

**Tech Stack:** Python standard library, pyserial via existing script, native HTML/CSS/JavaScript Canvas, unittest.

---

### Task 1: Record Requirements

**Files:**
- Create: `.trellis/tasks/05-21-add-pid-ai-serial-dashboard/prd.md`
- Create: `docs/plans/2026-05-21-pid-ai-serial-dashboard.md`

**Step 1: Write the PRD**

Record dashboard scope, API requirements, safety rules, and no-board acceptance criteria.

**Step 2: Save this implementation plan**

Save the plan before touching implementation files so later sessions can resume from the same scope.

### Task 2: Write Failing Tests

**Files:**
- Create: `.codex/skills/pid-ai-serial/tests/test_pid_ai_dashboard.py`

**Step 1: Add tests for pure dashboard behavior**

Cover bounded sample buffering, latest frame snapshots, parse error accounting, command history ACK updates, and command-name extraction.

**Step 2: Run tests and verify red**

Run:

```powershell
python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v
```

Expected: fail because `pid_ai_dashboard.py` does not exist yet.

### Task 3: Implement Dashboard Script

**Files:**
- Create: `.codex/skills/pid-ai-serial/scripts/pid_ai_dashboard.py`

**Step 1: Implement state and serial lifecycle**

Add `DashboardState` with thread-safe connection state, bounded PID samples, latest CFG/status/event, parse diagnostics, command history, serial connect/disconnect, and command send methods.

**Step 2: Implement HTTP API**

Serve `/`, `/api/ports`, `/api/status`, `/api/samples`, `/api/connect`, `/api/disconnect`, and `/api/command` using the Python standard library.

**Step 3: Implement embedded UI**

Build an operational dashboard with connection controls, Canvas chart, safety status, PID term panel, parameter command controls, exact command preview, and ACK/ERR history.

**Step 4: Run tests and fix green**

Run the unittest command until behavior tests pass.

### Task 4: Update Skill Entry

**Files:**
- Modify: `.codex/skills/pid-ai-serial/SKILL.md`
- Modify: `.codex/skills/pid-ai-serial/agents/openai.yaml`

**Step 1: Document dashboard launch**

Add quick-start and workflow entries for:

```powershell
python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --auto --open
```

**Step 2: Preserve safety rules**

Document that opening the dashboard and scanning are read-only; commands are sent only after explicit user action and ACK/ERR remains authoritative.

### Task 5: Verify

**Files:**
- Existing files only.

**Step 1: Run Python tests**

Run:

```powershell
python -m unittest discover -s .codex\skills\pid-ai-serial\tests -v
```

**Step 2: Run syntax checks without bytecode writes**

Run:

```powershell
python -B -m py_compile .codex\skills\pid-ai-serial\scripts\pid_ai_serial.py .codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py
```

**Step 3: Validate the skill**

Run:

```powershell
python C:\Users\caofengrui\.codex\skills\.system\skill-creator\scripts\quick_validate.py .codex\skills\pid-ai-serial
```

**Step 4: Start the dashboard without a board**

Start the dashboard on `127.0.0.1:8765`, call `/api/status`, and confirm it serves JSON even when no board is connected.

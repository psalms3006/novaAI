# NOVA-Hermes Integration Architecture

## Overview

This document describes how NOVA delegates agentic execution to the installed
Hermes runtime without duplicating Hermes code or replacing NOVA's existing
brain, voice, or user interface.

## High-Level Architecture

```
USER
  │
  ▼
NOVA Interface / Voice / UI
  │
  ▼
NOVA Core  (brain, planner, tools, memory)
  │
  │  agentic task detected
  ▼
integrations.hermes.HermesBridge
  │
  ├── subprocess isolation  ◀── preferred default
  │       │
  │       ▼
  │   Hermes runtime / tools
  │
  └── embedded mode  ◀── optional, when Hermes is importable
          │
          ▼
      Hermes agent / tools
```

## Why Subprocess Isolation

- Hermes' installed tool/runtime import chain assumes its own venv/config.
- Embedding directly in NOVA's process caused tool-module import failures during
  discovery, so the default mode runs a short-lived subprocess against Hermes'
  installed package.
- Embedded mode remains available when Hermes is already importable.

## Task Model

```text
CREATED
  → QUEUED
  → RUNNING
  → COMPLETED
```

Failure states:

```text
FAILED
CANCELLED
```

Each task has:
- task_id
- user_request
- status
- progress
- current_action
- result
- errors
- artifacts
- timestamps

## Event Flow

1. NOVA calls `HermesBridge.submit_task(request)`
2. Bridge publishes `task.created` and `task.queued`
3. Worker thread starts Hermes execution
4. Bridge publishes `task.started`, `task.planning`
5. Tool events: `task.tool_started`, `task.tool_completed`
6. Final: `task.completed`, `task.failed`, or `task.cancelled`
7. NOVA consumes events from its `EventBus`

## Configuration

Env vars:

- `HERMES_ENABLED=true|false`
- `HERMES_RUNTIME=subprocess|embedded`
- `HERMES_TASK_TIMEOUT=120`

Defaults:
- Hermes is enabled
- subprocess isolation
- 120s timeout

## Files Changed

- `integrations/__init__.py`
- `integrations/hermes/__init__.py`
- `tests/test_hermes_bridge.py`

## Capabilities Integrated

- Hermes lifecycle: start / stop / health
- Task submission and status tracking
- Event streaming back to NOVA
- Cancellation
- Research delegation via real Hermes tool discovery
- Browser delegation via real Hermes tool discovery
- Fallback execution path when Hermes subprocess probing cannot initialize

## Verification

See `tests/test_hermes_bridge.py`.

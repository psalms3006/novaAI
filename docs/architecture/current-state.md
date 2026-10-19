# NOVA Current State

## Overview
NOVA is a desktop AI assistant with a modular architecture consisting of a core execution engine, agent system, memory subsystem, planner, scheduler, and various integrations.

## Key Components

### Core Execution
- **nova.py**: Main entry point, handles different modes (live, offline, phone, UI, desk).
- **core/goal_engine.py**: Persistent goal and step tracking with atomic JSON storage.
- **core/execution_engine.py**: Executes individual steps via tool/agent functions with verification.
- **task_manager.py**: AIOS-based concurrent task manager for multi-step goals, with worker pools, retries, reviews, and persistence.
- **core/reliability.py**: Provides circuit breaker, retry with backoff/jitter, watchdog, and health monitor (underutilized).

### Agent System
- **nova_agents.py**: Defines autonomous agents (Browser, Meeting, Surveillance, Spawn) and base classes.
- **agent_activity.py**: Tracks agent actions and provides structured messaging.

### Memory and Learning
- **nova_memory.py**, **living_memory.py**, **memory_extra.py**: Manage short-term and long-term memory, including vector search (FAISS/numpy fallback) and living memory for consolidation.
- **nova_learning.py**: Stores and retrieves user-taught principles.

### Planning and Scheduling
- **planner_extra.py**: Implements NOVAPlanner for reminders and scheduled workflows.
- **nova_scheduler.py**: Persistent scheduler that runs workflows (including GOAL_KIND) at specified times, with grace/stale windows and failure backoff.
- **nova_heartbeat.py**: Tier 5 heartbeat that handles held notices, quiet hours, and inbox.

### Integrations and Tools
- **nova_tools.py**: Deferred tool system for lazy loading and wrapping.
- **integrations/**: Includes Gmail connector and others.
- **desk/**: Credentials management for cloud session and DPAPI-sealed BYOK.

### Configuration
- **nova_config.toml**: User-editable configuration for various subsystems.
- **Environment variables**: Override settings (e.g., GEMINI_API_KEY).

### Security and Permissions
- **nova_core/permissions.py**: Tool authorization and confirmation gates.
- **nova_core/trust.py**: Trust model for determining instruction validity.
- **nova_core/errors.py**: Error classification for retry decisions.

### Communication
- **nova_proactive.py**: Decides when to speak proactively based on priority and spacing.
- **nova_heartbeat.py**: Also handles proactive notices and inbox.

## Data Persistence
- Goals: `goals.json` (via GoalStore)
- Tasks: `nova_tasks_aios.json` (via TaskManager)
- Workflows: `workflows.json` (via Scheduler)
- Heartbeat state: `heartbeat_state.json`
- Heartbeat inbox: `heartbeat_inbox.json`
- Memory: `memory_texts.json`, `memory.index`, `living_memory.json`, `living_memory.migrated`
- Settings: `%APPDATA%\NOVA\settings.json` (desk module)

## Known Limitations
- The reliability module's features (circuit breaker, exponential backoff, etc.) are not fully integrated into the execution engine or task manager.
- The goal engine is used for tracking but not directly for execution; execution is handled via the task manager for GOAL_KIND workflows.
- The proactive agent and heartbeat handle outbound notifications but do not execute tasks.
- The agent system is capable but not fully integrated with the goal/task execution flow for autonomous goal decomposition.

## Current Workflow for Goal Execution
1. A goal can be submitted via the task manager (e.g., through the planner scheduling a GOAL_KIND workflow).
2. The task manager plans steps (using its internal planner or a fallback).
3. Steps are executed by worker threads via the injected tool executor (typically `_execute_tool_sync` from nova.py).
4. Each step includes retry logic for transient failures, verification, and recording of results.
5. After steps, a review loop may request revisions.
6. The task concludes with a final status (COMPLETED, FAILED, etc.) and is persisted.

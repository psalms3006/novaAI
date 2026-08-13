# CHANGELOG — NOVA Identity System Redesign

## [identity-v1] — 2026-08-04

### Problem
NOVA had no centralized identity system. Product name, assistant name, wake word,
creator/company references, and conversational self-description were duplicated
across multiple modules with no single source of truth. Renaming the assistant
or auditing identity-related behavior required changing many files.

### Analysis
The repository already supported user metadata (`meta`) for things like
`user_name`, but assistant identity values were hardcoded in prompts, tooling,
greeting logic, and wake-word detection. This made the system brittle and
created contradictions between internal documentation, system prompts, voice
surfaces, and memory initialization.

### Solution
Introduced a centralized identity module and integrated it across the major
NOVA surfaces without replacing the product identity.

### Files modified
- `agent/identity.py` — new centralized identity module with:
  - product/user identity helpers
  - wake-word derivation
  - config-backed persistence rules
  - identity query detection
  - response builder
- `config.py` — new config bridge for `nova_config.toml`
- `nova_config.toml` — added `[identity]` section
- `agent/prompts.py` — updated system prompt to use dynamic identity injection
- `nova.py` — updated system prompt, identity init, tool output paths
- `live_extra.py` — updated NOVALive greeting with configured assistant name
- `nova_wake.py` — wake phrases now derive from configured assistant name
- `agent/brain.py` — updated live/CLI outputs and startup prompt surface
- `memory_extra.py` — persist identity facts into memory metadata on load/store
- `agent/provider.py` — fixed pre-existing indentation error

### Verification
- Verified identity defaults: Nova, Omniel, wake=nova
- Verified renaming: Atlas, Athena, Friday
- Verified identity responses for name/real name/company questions
- Verified wake-word derivation matches configured name
- Verified memory metadata persists identity fields
- Verified no functional hardcoded `~/.vyren`-style path regressions in NOVA

### Remaining work
- Add UI/onboarding flow for assistant name changes
- Extend voice wake-word phrase expansion from configured name
- Add persistent tests under `tests/`

## [bugfix-v1] — 2026-08-04

### Problems
1. Phase 17 failed with `NameError: name 'os' is not defined`.
2. Runtime crashed with `TypeError: 'RuntimeServiceRegistry' object does not support item assignment`.
3. Runtime later crashed with `TypeError: RuntimeServiceRegistry.get() takes 2 positional arguments but 3 were given`.
4. Connectivity used stale `get_offline_queue_path()`/queue-path assumptions after platform abstraction migration.
5. `config.yaml` identity value was `Echo`, causing the runtime banner to display the wrong assistant name.

### Analysis
- `runtime/manager.py` used `os.getpid()` and `os._exit()` without importing `os`.
- `runtime/registry.py` was created as a registry object, but `runtime/manager.py` still treated it like a plain dict in multiple sites.
- `RuntimeServiceRegistry.get()` did not accept a default argument, unlike `dict.get()`.
- `runtime/connectivity.py` still referenced offline-queue helpers incorrectly after path refactors.
- `config.yaml` had stale identity state from earlier testing.

### Solution
- Added `import os` to `runtime/manager.py`.
- Extended `RuntimeServiceRegistry` with dict-like dunder methods (`__setitem__`, `__getitem__`, `__contains__`, `__iter__`, `__len__`) and changed `get()` to accept a default.
- Verified all `_services[...]` and `.get(..., default)` call sites in `runtime/manager.py` now work against the registry.
- Fixed `runtime/connectivity.py` to use `platform_paths.get_offline_queue_path()` directly and removed broken inline queue-path logic.
- Corrected `config.yaml` identity to `assistant_name: Vyren`.

### Verification
- Full application boot completed with the project venv Python:
  - `18 services running, 0 failed`
- Connectivity connectivity-check error `get_offline_queue_path` no longer appears after repair.
- Status banner now shows `Vyren is online.` with the configured identity.

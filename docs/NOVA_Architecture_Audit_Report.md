# NOVA Architecture & Self-Improvement Audit Report

## 1. Executive Summary
NOVA already possesses a sophisticated, functional foundation for deliberate, knowledge-driven learning (`nova_learning` package). This component enables deliberate knowledge acquisition (e.g., "learn from this documentation folder"), including file discovery, classification, extraction, verification, and persistence.

However, NOVA *lacks* an experience-driven self-improvement layer—a mechanism to persistently observe, analyze, and refine her own operational strategies, task execution patterns, and recovery procedures based on execution outcomes (successes, failures, corrections).

The proposed architecture will leverage the existing `nova_learning` infrastructure for knowledge *storage* and *retrieval* while introducing a new "Experience-Driven Strategy Optimization" layer that captures execution evidence and incrementally refines NOVA’s operational procedures.

---

## 2. Existing Architecture Map (Core Components)

| Component | Responsibility | Status |
| :--- | :--- | :--- |
| `nova.py` | Primary entry point, orchestrates startup. | Functional |
| `core/execution_engine.py` | Goal-driven dispatch, tool/agent execution. | Functional |
| `core/goal_engine.py` | Persistent state tracking for tasks. | Functional |
| `nova_learning/` | Deliberate, verified knowledge acquisition. | Functional |
| `nova_memory.py` | Episodic memory (facts about people/events). | Functional |
| `actions/` | Tool implementations (shell operations). | Functional |
| `core/shell_manager.py` | Safe shell execution and diagnostics. | Functional |

---

## 3. Existing Memory & Learning Capabilities

*   **Episodic Memory (`nova_memory.py`)**: Stores facts about interactions, user profiles, and events.
*   **Deliberate Knowledge (`nova_learning/`)**: Handles structured knowledge acquisition from external sources (docs, files). Includes a rigorous verification pipeline before "learning" is promoted.
*   **Gap Identification**: No existing system observes execution outcomes to identify reusable strategies or repair patterns. NOVA is currently "knowledge-aware" but "experience-blind" regarding her own operational efficacy.

---

## 4. Recommended Minimal Architecture

We will integrate an **Evidence-Driven Self-Improvement System** by extending existing components rather than building a new subsystem.

1.  **Experience Monitor (`core/execution_engine.py` + `actions/`)**: Enhance tool handlers to emit `ExecutionOutcome` events, capturing successes, failures, and diagnostic logs (via `ShellResult`).
2.  **Learning Engine (`nova_learning/service.py` extended)**: Add an `ExperienceLearner` that processes `ExecutionOutcome` events to create candidate "Strategy Lessons."
3.  **Memory Store (`nova_learning/store.py` extended)**: Use `KnowledgeStore` to persist these "Strategy Lessons" as a new domain ("Self-Improvement").
4.  **Strategy Optimizer (`core/recovery_manager.py` extended)**: During failure, query the `KnowledgeStore` for relevant "Strategy Lessons" (verified recovery patterns).

---

## 5. Learning Data Model

| Lesson Type | Metadata Needs |
| :--- | :--- |
| **Strategy Improvement** | Task context, diagnostic signature, success rate, verified evidence. |
| **Procedure (Skill)** | Preconditions, tool sequence, verification criteria, success history. |
| **Candidate** | Raw execution trace, hypothesis, confidence score. |

---

## 6. Risk and Approval Policy

*   **Low Risk (Automatic)**: Updating statistics on existing, verified strategies. Recording failure data for future analysis.
*   **Medium Risk (Notification/Approval)**: Proposing a new, untested recovery procedure for a common failure type.
*   **High Risk (Restricted)**: Modifying tool-selection preferences that impact system security or permission boundaries; changing existing, stable procedures.

---

## 7. Failure-Recovery Workflow

1.  **Detection**: `ExecutionEngine` captures `ShellExecutionError` (via `ShellManager`).
2.  **Diagnosis**: `RecoveryManager` identifies the failure type.
3.  **Retrieval**: `RecoveryManager` queries `KnowledgeStore` for existing verified recovery procedures.
4.  **Recovery**: If a procedure exists, execute it and verify success.
5.  **Evidence Collection**: Regardless of success, record the outcome (including diagnostic traces) for later analysis.
6.  **Refinement**: If no procedure exists or the failure is recurring, promote a candidate lesson for later analysis/verification.

---

## 8. Background Maintenance

*   **Event-Driven**: Background sessions for analyzing failed tasks and refining procedures.
*   **Constraint-Aware**: Analysis and consolidation tasks deferred until `nova_heartbeat.py` reports idle system state.
*   **Reuse**: Use `nova_scheduler.py` to trigger consolidation tasks.

---

## 9. Security and Privacy

*   **Isolation**: Keep learning data per-workspace (or per-user if applicable).
*   **Integrity**: Learned procedures are data; they must be verified before promotion to "Skill" status to prevent arbitrary execution.
*   **Cloud Authorization**: Learning improvements remain local unless the user explicitly opts in to shared model improvements.

---

## 10. Test Plan (Beta Capabilities)

1.  **Learned Procedure**: Successfully complete a task, record a "Skill," and ensure it is retrieved in a later session for an identical task.
2.  **Failure Recovery**: Induce a shell failure, ensure NOVA uses a learned recovery procedure, and verifies success.
3.  **Policy Compliance**: Attempt to learn a procedure that requires elevated privileges, ensuring the system *requests* rather than *assumes* the capability.
4.  **Persistence**: Verify learned strategies persist across app restart (`nova.py`).

---

## 11. Uncertainties Requiring Inspection

*   *Storage volume*: How large will the strategy memory grow? (Implement TTL or pruning for low-confidence candidates).
*   *Verification logic*: Is `VerificationEngine` (used by `nova_learning`) sufficient for procedural verification? (May require `core/verification_engine.py` updates).

# NOVA Target State

## Overview
NOVA will be a reliable autonomous assistant capable of executing complex goals end-to-end, maintaining persistent memory and personality, coordinating multiple agents, interacting with the computer safely, recovering from failures, and proactively communicating with the user through authorized channels, including outbound telephone calls.

## Key Improvements

### Reliable Goal Execution
- **Enhanced Task Manager**: Integrated with the reliability module to provide circuit breaker protection, exponential backoff with jitter for retries, and health checks for external services.
- **Goal Engine Execution**: The goal engine will be extended to directly execute goals using the improved execution engine, providing a lightweight alternative to the task manager for simple goals.
- **Idempotency and Deduplication**: Steps will include idempotency keys or state checks to prevent duplicate side effects (e.g., sending the same email twice).
- **Checkpointing**: Long-running tasks will persist intermediate state to allow resumption after interruption without repeating completed non-idempotent steps.
- **Explicit Lifecycle States**: Tasks will transition through clearly defined, persisted states: ACCEPTED, PLANNING, READY, EXECUTING, WAITING, VERIFYING, RECOVERING, COMPLETED, PARTIALLY_COMPLETED, FAILED, CANCELLED.
- **Verification and Evidence**: Each step will collect verification evidence (e.g., screenshots, file hashes, API responses) to confirm outcomes, not just tool success responses.

### Persistent Memory and Personality
- **Unified Memory System**: Improved memory retrieval with relevance scoring, contextual filtering, and user-specific isolation.
- **Personality Persistence**: `SELF.md` and `USER.md` will be stored in the user's data directory and loaded at startup, influencing agent behavior and communication style.
- **Memory Updates**: Corrections and updates to memories will be tracked with provenance and timestamps.
- **Forgetting Mechanisms**: Optional decay or summarization of low-importance memories to prevent overload.

### Agent Coordination
- **Task Delegation**: The coordinator (possibly an Orchestrator Agent) will delegate subtasks to specialist agents with clear objectives, constraints, and verification criteria.
- **Independent Verification**: Critical results will be verified by a separate agent or mechanism before acceptance.
- **Conflict Resolution**: Agents will not be allowed to modify the same resource concurrently without explicit coordination (e.g., file locking, versioning).
- **Failure Handling**: Failed agents will be retried, reassigned, or escalated to the user based on the nature of the failure and user preferences.

### Computer Control and Vision
- **Observation-Action-Verification Loop**: All computer control actions will follow an observe-act-verify cycle to ensure the intended outcome occurred.
- **Error Handling**: Unexpected interface changes, timeouts, and stale observations will be detected and recovered from where possible.
- **Permission Checks**: Sensitive operations (e.g., file deletion, system changes) will require explicit user approval or fall within a predefined policy.
- **Visual Interaction**: Screen analysis will be used to verify outcomes and guide actions.

### Failure Recovery and Self-Repair
- **Bounded Self-Repair**: Low-risk operational problems (e.g., restarting a failed worker, re-establishing a connection) will be attempted automatically within an authorized scope.
- **Checkpoints and Rollbacks**: Critical operations will use checkpoints or backups to allow safe rollback on failure.
- **Escalation**: Unrecoverable failures will be escalated to the user with detailed diagnostics.
- **Retry Policies**: Retries will be classified by error type (transient vs. permanent) with appropriate backoff strategies.

### Proactive Communication and Outbound Calling
- **Provider-Independent Notifications**: A common interface will support multiple channels (SMS, WhatsApp, Telegram, Email, Desktop Notifications) with configurable preferences and fallback order.
- **Outbound Calling**: Integrated with a calling provider (initially Bland AI) for proactive reminders and briefings, subject to user consent and preferences.
- **Calling Features**: 
  - User-configurable phone number verification and time zone.
  - Quiet hours and maximum call frequency.
  - Missed-call escalation with configurable retry intervals and alternative channels.
  - Call outcome processing and persistent records.
  - Sandbox testing with a mock provider to avoid live calls during development.
- **Proactive Intelligence**: Events will be evaluated based on urgency, relevance, and user preferences before triggering a notification.
- **Natural Language Preferences**: Users can configure preferences in natural language, which will be converted to structured settings.

### Security, Authorization, and Isolation
- **User and Workspace Isolation**: Each user's data, memory, and credentials will be isolated, even in multi-user installations.
- **Server-Side Authorization**: Sensitive operations will require authorization checks that cannot be bypassed by the model.
- **Scoped Tool Permissions**: Agents will only have access to the tools necessary for their assigned tasks.
- **Secure Secret Management**: API keys and credentials will be stored securely (e.g., using OS keyring or encrypted vault) and never logged.
- **Prompt Injection Defense**: Untrusted content from external sources will be sanitized and not treated as executable instructions.
- **Rate Limits and Abuse Controls**: Outbound communications (calls, messages) will be subject to rate limits to prevent abuse.

### Model Routing
- **Capability-Based Routing**: Models will be selected based on task requirements (e.g., reasoning, vision, speed) rather than always choosing the largest model.
- **Fallback Chains**: On provider failure, the system will fall back to alternative models or local models without uncontrolled retries.
- **Privacy-Aware Routing**: Private user data will never be sent to unauthorized models; local processing will be preferred for sensitive information.

### Configuration and Extensibility
- **Replaceable Components**: Integrations (calling, messaging, cloud storage) will be designed as replaceable modules.
- **User-Friendly Configuration**: Settings will be accessible through the UI and editable configuration files, with clear documentation.
- **Development and Testing Modes**: Sandbox modes will disable live external calls and use mock services for safe testing.

## Data Persistence (Enhanced)
- All execution state, memory, personality, goals, tasks, workflows, and communication history will be persisted atomically and survive application restarts.
- Backups and versioning will be considered for critical data to prevent corruption.

## Expected Workflow for Goal Execution
1. A goal is accepted (via user input, scheduler, or proactive suggestion).
2. The coordinator creates a plan, decomposing it into subtasks assigned to appropriate agents or skill modules.
3. Each subtask is executed with explicit permission checks, observation before action, and verification after action.
4. Step results are recorded with evidence, and failures trigger bounded retries or alternative approaches.
5. The coordinator verifies the final outcome against the goal's success criteria.
6. The user is notified of the completion through their preferred channel, with a summary of what was done and any limitations.
7. Relevant outcomes are stored in memory for future reference.

This target state represents a reliable, secure, and user-aligned NOVA assistant that can be trusted to act autonomously within defined boundaries.

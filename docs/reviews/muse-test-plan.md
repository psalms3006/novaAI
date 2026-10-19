# NOVA Test Plan: Failure Detection and Reliability Verification

## Overview

This test plan focuses on identifying how NOVA could fail despite appearing to work in a normal demonstration. The tests target edge cases, failure scenarios, and reliability mechanisms that are critical for production readiness but often overlooked in basic functionality tests.

## Execution Tests

### Invalid Task Handling
**Scenario**: Submitting a goal with invalid or nonsensical parameters (e.g., "open application that doesn't exist", "delete system file").
**Expected Behavior**: NOVA should reject the task with a clear error message, not attempt execution or claim success.
**Failure Being Detected**: False success claims on impossible tasks, unauthorized operations.
**Test Type**: Unit test with mocked tool executor returning appropriate errors.
**Dependencies**: task_manager.py, nova_core/errors.py
**Automatic**: Yes

### Failed Tool Simulation
**Scenario**: A tool (e.g., file_controller) consistently fails during execution.
**Expected Behavior**: Task should transition to FAILED state after retries are exhausted, with proper error propagation.
**Failure Being Detected**: Silent failure masking, incorrect retry behavior.
**Test Type**: Integration test with failing mock tool.
**Dependencies**: task_manager.py, core/execution_engine.py
**Automatic**: Yes

### Model Timeout Handling
**Scenario**: LLM provider takes longer than expected to respond (simulated timeout).
**Expected Behavior**: System should apply timeout, retry with backoff if appropriate, or fail with timeout error.
**Failure Being Detected**: Hanging tasks, resource exhaustion, missed timeout configurations.
**Test Type**: Unit test with delayed mock LLM response.
**Dependencies**: nova_core/reliability.py, tool execution flow
**Automatic**: Yes

### Incorrect Success Claims
**Scenario**: Tool returns success status but actual operation failed (e.g., file reported as saved but not written).
**Expected Behavior**: Verification step should detect mismatch and mark step as FAILED.
**Failure Being Detected**: False confidence in task completion, silent data corruption.
**Test Type**: Test with mock tool that lies about success.
**Dependencies**: task_manager.py verification logic, core/verification_engine.py
**Automatic**: Yes

### Partial Completion Scenarios
**Scenario**: Multi-step task where some steps succeed and others fail.
**Expected Behavior**: Task should reach PARTIALLY_COMPLETED state with clear record of what succeeded/failed.
**Failure Being Detected**: Inaccurate progress reporting, inability to resume correctly.
**Test Type**: Integration test with mixed success/failure steps.
**Dependencies**: task_manager.py state tracking
**Automatic**: Yes

### Task Cancellation
**Scenario**: User cancels a long-running task mid-execution.
**Expected Behavior**: Task should transition to CANCELLED state, cleanup resources, and persist state.
**Failure Being Detected**: Resource leaks, incomplete cleanup, inconsistent state after cancellation.
**Test Type**: Integration test with simulated user cancellation.
**Dependencies**: task_manager.py cancellation handling
**Automatic**: Yes

### Repeated Execution
**Scenario**: Same goal submitted multiple times in quick succession.
**Expected Behavior**: System should detect duplicates and either prevent execution or handle idempotently.
**Failure Being Detected**: Duplicate side effects (e.g., sending same email twice), resource waste.
**Test Type**: Test submitting identical goals rapidly.
**Dependencies**: task_manager.py deduplication logic
**Automatic**: Yes

### Interrupted Tasks and Recovery
**Scenario**: System crash/power loss during task execution.
**Expected Behavior**: Upon restart, task should be resumed from last checkpoint or safely restarted.
**Failure Being Detected**: Data loss, inconsistent state, inability to recover.
**Test Type**: Simulate crash during execution, verify recovery.
**Dependencies**: task_manager.py persistence, checkpointing
**Automatic**: Yes (with mock crash simulation)

## Persistence Tests

### Saving and Loading Task State
**Scenario**: Task in various states (QUEUED, RUNNING, COMPLETED) saved to disk and reloaded.
**Expected Behavior**: Exact state restoration with all attributes preserved.
**Failure Being Detected**: State corruption, attribute loss, incorrect type conversion.
**Test Type**: Unit test serializing/deserializing Task objects.
**Dependencies**: task_manager.py persistence methods
**Automatic**: Yes

### Recovery After Restart
**Scenario**: NOVA crashes during task execution, then restarts.
**Expected Behavior**: Tasks in progress are safely resumed or reset to QUEUED state.
**Failure Being Detected**: Tasks stuck in intermediate states, incorrect status after restart.
**Test Type**: Write task state, simulate crash, reload and verify.
**Dependencies**: task_manager.py _load() method
**Automatic**: Yes

### Duplicate-Action Prevention
**Scenario**: System attempts to execute same non-idempotent step twice.
**Expected Behavior**: Second execution should be detected and prevented or handled safely.
**Failure Being Detected**: Duplicate emails sent, files overwritten incorrectly.
**Test Type**: Test with idempotency keys or state checking.
**Dependencies**: task_manager.py step execution logic
**Automatic**: Yes

### Corrupted or Incomplete Records
**Scenario**: nova_tasks_aios.json file is partially corrupted or missing fields.
**Expected Behavior**: System should handle gracefully, log error, and continue with recoverable data.
**Failure Being Detected**: Crash on startup, data loss, inability to boot.
**Test Type**: Test with malformed JSON files.
**Dependencies**: task_manager.py _load() error handling
**Automatic**: Yes

### Concurrent Task Updates
**Scenario**: Multiple threads attempting to update same task simultaneously.
**Expected Behavior**: Proper locking prevents race conditions and corruption.
**Failure Being Detected**: Lost updates, inconsistent state, crashes.
**Test Type**: Stress test with concurrent task modifications.
**Dependencies**: task_manager.py threading locks
**Automatic**: Yes

## Reliability Tests

### Bounded Retries
**Scenario**: Tool fails with transient error (network timeout).
**Expected Behavior**: System retries exactly MAX_RETRY times then gives up.
**Failure Being Detected**: Infinite retry loops, insufficient retries, excessive retries.
**Test Type**: Test with failing mock tool, count retry attempts.
**Dependencies**: task_manager.py RETRY_DELAYS_S, retry logic
**Automatic**: Yes

### Circuit Breaker
**Scenario**: External service (e.g., API) is consistently failing.
**Expected Behavior**: Circuit breaker opens after failure threshold, preventing further calls.
**Failure Being Detected**: Resource exhaustion, cascading failures, timeout accumulation.
**Test Type**: Test with consistently failing mock service.
**Dependencies**: nova_core/reliability.py CircuitBreaker
**Automatic**: Yes

### Exponential Backoff
**Scenario**: Repeated transient failures.
**Expected Behavior**: Retry delays increase exponentially with jitter.
**Failure Being Detected**: Thundering herd problem, insufficient backoff, predictable timing.
**Test Type**: Measure actual delay between retries.
**Dependencies**: task_manager.py retry delay calculation
**Automatic**: Yes

### Provider Outages
**Scenario**: LLM provider becomes unavailable mid-task.
**Expected Behavior**: System detects outage, applies fallback or fails gracefully.
**Failure Being Detected**: Hanging tasks, no fallback to local models, user confusion.
**Test Type**: Simulate provider failure during execution.
**Dependencies**: Model routing, fallback mechanisms
**Automatic**: Yes (with mock provider)

### Recovery After Temporary Failure
**Scenario**: Service fails temporarily then recovers.
**Expected Behavior**: System resumes normal operation after service returns.
**Failure Being Detected**: Permanent failure state, missed recovery opportunity.
**Test Type**: Test with intermittent failure simulation.
**Dependencies**: Reliability module, health checks
**Automatic**: Yes

### Actual Timeout Termination
**Scenario**: Operation exceeds configured timeout limit.
**Expected Behavior**: Timeout mechanism actually terminates the operation, not just logs.
**Failure Being Detected**: Zombie processes, resource leaks, ignored timeouts.
**Test Type**: Test with operation that exceeds timeout, verify termination.
**Dependencies**: Timeout implementation in tool execution
**Automatic**: Yes

## Agent Tests

### Failed Specialist Agents
**Scenario**: Delegated agent (e.g., Browser Agent) crashes or fails.
**Expected Behavior**: Main system detects failure, attempts reassignment or user notification.
**Failure Being Detected**: Silent agent failure, missing error propagation.
**Test Type**: Test with crashing mock agent.
**Dependencies**: nova_agents.py, agent delegation system
**Automatic**: Yes

### Conflicting Results
**Scenario**: Two agents produce contradictory information about same fact.
**Expected Behavior**: System detects conflict and requests verification or user input.
**Failure Being Detected**: Incorrect conclusions from conflicting data, no conflict resolution.
**Test Type**: Test with agents returning conflicting data.
**Dependencies**: Agent coordination, verification systems
**Automatic**: Yes

### Invalid Handoffs
**Scenario**: Agent receives malformed or impossible task from coordinator.
**Expected Behavior**: Agent rejects task with clear error, does not attempt execution.
**Failure Being Detected**: Agents acting on invalid input, security risks.
**Test Type**: Test agent interface with invalid inputs.
**Dependencies**: Agent input validation
**Automatic**: Yes

### Missing Outputs
**Scenario**: Agent completes task but produces no output or empty result.
**Expected Behavior**: System detects missing output and treats as failure or requests clarification.
**Failure Being Detected**: False completion claims, missing data propagation.
**Test Type**: Test with agent returning empty/null results.
**Dependencies**: Output validation, result checking
**Automatic**: Yes

### Parallel Task Failures
**Scenario**: Multiple parallel tasks experience failures simultaneously.
**Expected Behavior**: Each task handled independently, failures don't cascade.
**Failure Being Detected**: Resource exhaustion, shared state corruption, failure propagation.
**Test Type**: Stress test with multiple failing parallel tasks.
**Dependencies**: TaskManager concurrency, isolation mechanisms
**Automatic**: Yes

### Verification of Delegated Results
**Scenario**: Agent returns result that needs verification (e.g., screenshot doesn't show expected element).
**Expected Behavior**: System requests verification evidence and validates against success criteria.
**Failure Being Detected**: Accepting unverified results, false confidence in agent work.
**Test Type**: Test verification loop with insufficient evidence.
**Dependencies**: Verification engine, evidence collection
**Automatic**: Yes

## Proactive Communications Tests

### Quiet Hours
**Scenario**: Proactive event triggered during user-configured quiet hours.
**Expected Behavior**: Event is delayed until quiet hours end or delivered via non-disruptive channel.
**Failure Being Detected**: Unwanted notifications during sleep/meetings, user frustration.
**Test Type**: Test event emission during quiet hours.
**Dependencies**: nova_proactive.py quiet hour logic
**Automatic**: Yes

### Scheduling and Time Zones
**Scenario**: User travels across time zones, scheduled events should adjust.
**Expected Behavior**: Scheduled notifications fire at correct local time.
**Failure Being Detected**: Missed notifications, wrong timing, confusion.
**Test Type**: Test with simulated time zone changes.
**Dependencies**: nova_scheduler.py time handling
**Automatic**: Yes

### Missed Calls
**Scenario**: Outbound call fails (user doesn't answer, busy signal).
**Expected Behavior**: System follows configured retry intervals and escalation path.
**Failure Being Detected**: No retry attempts, premature escalation, user never notified.
**Test Type**: Test call failure scenarios with retry logic.
**Dependencies**: Calling provider adapter, retry configuration
**Automatic**: Yes (with mock caller)

### Retry Limits
**Scenario**: Persistent failure in outbound communication (e.g., wrong number).
**Expected Behavior**: System stops retrying after configured limit, notifications user of failure.
**Failure Being Detected**: Infinite retry loops, wasted resources, no user notification of permanent failure.
**Test Type**: Test with permanently failing communication channel.
**Dependencies**: Retry limit configuration, failure detection
**Automatic**: Yes

### Duplicate-Event Prevention
**Scenario**: Same proactive event generated multiple times (e.g., same reminder triggered twice).
**Expected Behavior**: System suppresses duplicates using dedupe_key mechanism.
**Failure Being Detected**: Notification spam, user annoyance, missed real events in noise.
**Test Type**: Test with duplicate events, verify only one notification.
**Dependencies**: nova_proactive.py deduplication
**Automatic**: Yes

### Provider Errors
**Scenario**: Notification provider (e.g., email service) returns error.
**Expected Behavior**: System logs error, attempts fallback channels if configured.
**Failure Being Detected**: Silent failure of notifications, no fallback attempts.
**Test Type**: Test with failing mock notification provider.
**Dependencies**: Notification service module, error handling
**Automatic**: Yes

### Message Fallback
**Scenario**: Primary notification channel fails, secondary should be used.
**Expected Behavior**: System automatically falls back to configured alternative channel.
**Failure Being Detected**: Critical messages lost, no user awareness of primary channel failure.
**Test Type**: Test primary channel failure, verify fallback usage.
**Dependencies**: Notification service channel fallback logic
**Automatic": Yes

### Unauthorized Follow-up Actions
**Scenario**: System attempts to initiate follow-up communication without user consent.
**Expected Behavior**: Follow-up requires explicit user permission or falls within pre-authorized scope.
**Failure Being Detected**: Unsolicited communications, privacy violations, spam-like behavior.
**Test Type**: Test follow-up attempts without proper authorization.
**Dependencies**: Consent tracking, authorization checks
**Automatic": Yes

## Security and User Isolation Tests

### Permission Violations
**Scenario**: Agent attempts to use tool without proper authorization (e.g., file deletion without approval).
**Expected Behavior**: Permission system blocks operation and logs violation.
**Failure Being Detected**: Unauthorized system changes, security breaches.
**Test Type**: Test tool execution with insufficient permissions.
**Dependencies**: nova_core/permissions.py, nova_core/trust.py
**Automatic": Yes

### Unauthorized Tool Calls
**Scenario**: Malicious or manipulated input attempts to invoke dangerous tools.
**Expected Behavior**: Input validation and tool gateway prevent unauthorized access.
**Failure Being Detected**: Arbitrary code execution, system compromise.
**Test Type**: Test with injection attempts in tool parameters.
**Dependencies**: Tool input validation, gateway security
**Automatic": Yes

### Cross-User Memory Access
**Scenario**: In multi-user setup, User A attempts to access User B's memories.
**Expected Behavior**: Memory isolation prevents access, returns authorization error.
**Failure Being Detected**: Privacy breach, information leakage between users.
**Test Type**: Test multi-user memory access controls.
**Dependencies**: Memory subsystem isolation, user context handling
**Automatic": Yes (requires multi-user simulation)

### Secret Leakage
**Scenario**: System logs or error messages might contain API keys or credentials.
**Expected Behavior**: Secrets are never logged, appear as [REDACTED] or similar in traces.
**Failure Being Detected**: Credential exposure in logs, security vulnerabilities.
**Test Type**: Test logging with seeded secrets, verify no leakage.
**Dependencies**: Logging system, secret handling
**Automatic": Yes

### Unsafe Self-Repair
**Scenario**: System attempts self-repair operation that could be harmful (e.g., deleting critical files).
**Expected Behavior**: Self-repair limited to safe, predefined operations with user oversight.
**Failure Being Detected**: System damage, data loss, instability from "repairs".
**Test Type": Test self-triggered repair attempts with unsafe operations.
**Dependencies": Self-repair scope limitations, safety checks
**Automatic": Yes

## Test Implementation Approach

### Test Organization
- Unit tests: `tests/unit/` for isolated component testing
- Integration tests: `tests/integration/` for cross-component workflows
- Reliability tests: `tests/reliability/` for failure scenario testing
- Security tests: `tests/security/` for authorization and isolation verification

### Mocking Strategy
- Use `unittest.mock` for external dependencies (LLM providers, file system, network)
- Create deterministic mocks for tools and services
- Use dependency injection where possible for testability

### Test Data
- Use fixtures for consistent test state
- Generate unique IDs to prevent test interference
- Clean up test artifacts after execution

### Execution Guidelines
- Run unit tests on every commit
- Run integration tests nightly
- Run reliability/security tests weekly
- All tests must pass in CI/CD pipeline before deployment

## Acceptance Criteria for Execution Reliability and Proactive Communications

### Minimum Viable Test Suite
For immediate value, implement these high-priority tests that can run automatically:

1. **TaskManager Persistence Round-trip** - Test save/load of tasks in all states
2. **Retry Logic with Exponential Backoff** - Verify retry count and timing
3. **Circuit Breaker Opening/Closing** - Test failure threshold and recovery
4. **Proactive Agent Deduplication** - Verify duplicate suppression works
5. **Quiet Hour Enforcement** - Test events are delayed during quiet hours
6. **Tool Failure Propagation** - Verify failures are properly reported and handled
7. **Task Cancellation Cleanup** - Ensure resources are released on cancellation
8. **Timeout Enforcement** - Confirm operations actually terminate on timeout
9. **Permission Denial Handling** - Test unauthorized operations are blocked
10. **Memory Isolation Verification** - Confirm cross-user access is prevented

These tests provide coverage for the most critical failure modes while being fast and reliable enough for frequent execution.

## Conclusion

This test plan targets the gap between superficial functionality and genuine reliability. By focusing on failure modes, edge cases, and verification of safety mechanisms, we can identify issues that would only surface in production under stress or unusual conditions. The emphasis on deterministic, automated testing ensures these validations can be run consistently as part of the development process.
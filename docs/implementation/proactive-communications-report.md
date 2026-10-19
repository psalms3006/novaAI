# NOVA Proactive Communications Subsystem Implementation Report

## Executive Summary

This report documents the implementation of NOVA's proactive communications subsystem, enabling outbound calling and multi-channel notifications through a provider-agnostic architecture. The subsystem integrates with NOVA's existing scheduler, proactive agent, and heartbeat systems to deliver timely, user-configurable notifications via voice calls (Bland AI), desktop notifications, and extensible channels.

## 1. Existing Integrations Discovered

### Core NOVA Components Integrated
| Component | File | Role in Communications |
|-----------|------|------------------------|
| **Scheduler** | `nova_scheduler.py` | Persistent workflow execution; triggers notifications on workflow completion |
| **Proactive Agent** | `nova_proactive.py` | Desktop notification delivery; priority-based spacing and deduplication |
| **Heartbeat** | `nova_heartbeat.py` | Quiet hours enforcement; morning/evening brief scheduling |
| **Planner** | `planner_extra.py` | User-defined reminders and recurring tasks |
| **Task Manager** | `task_manager.py` | Background task completion notifications |
| **Permissions** | `nova_core/permissions.py` | Capability-based authorization for outbound actions |
| **Account Store** | `integrations/accounts.py` | Credential management for external providers |

### Communication Providers Available
- **Bland AI** — Primary voice calling provider (outbound calls with AI agents)
- **Twilio** — Legacy voice/SMS provider (already implemented in `nova_comms/providers/twilio.py`)
- **Mock Provider** — Test-only provider for sandbox testing (no real calls)

## 2. Architecture and Interfaces

### Core Modules Created

```
nova_comms/
├── provider.py           # Abstract base: ICommunicationProvider, CallStatus, CallResult
├── models.py             # Data models: PhoneNumber, CallRecording, CallTranscript, etc.
├── registry.py           # ProviderRegistry for dynamic provider discovery
├── notifications.py      # NotificationService - orchestrates multi-channel delivery
├── integration.py        # CommsIntegration - hooks into scheduler/proactive/heartbeat
├── providers/
│   ├── __init__.py       # Exports: TwilioProvider, BlandProvider, MockProvider
│   ├── twilio.py         # Twilio voice provider (existing)
│   ├── bland.py          # Bland AI voice provider (new)
│   └── mock.py           # Mock provider for testing (new)
└── __init__.py           # Public API exports
```

### Key Interfaces

#### ICommunicationProvider (Abstract Base)
```python
class ICommunicationProvider(ABC):
    @property
    def provider_name(self) -> str: ...
    @property
    def supports_inbound(self) -> bool: ...
    @property
    def supports_recording(self) -> bool: ...
    @property
    def supports_transcription(self) -> bool: ...
    
    async def make_call(self, to_number, from_number=None, metadata=None, 
                        webhook_url=None, task=None, voice=None, **kwargs) -> CallResult: ...
    async def end_call(self, call_id) -> bool: ...
    async def get_call_status(self, call_id) -> Optional[CallResult]: ...
    async def list_active_calls(self) -> List[CallResult]: ...
    async def handle_inbound_webhook(self, payload) -> Optional[CallResult]: ...
    async def send_dtmf(self, call_id, digits) -> bool: ...
    async def hold_call(self, call_id, hold=True) -> bool: ...
    async def transfer_call(self, call_id, target_number) -> bool: ...
```

#### NotificationService (Orchestrator)
- Multi-channel delivery with priority ordering
- Configurable retry policies with exponential backoff + jitter
- Escalation rules with fallback channels
- Per-channel deduplication
- Delivery state persistence (survives restarts)
- Quiet hours and rate limiting enforcement

#### CommsIntegration (Subsystem Hook)
- Scheduler workflow completion notifications
- Critical event triggering
- Task follow-up notifications
- Overdue task detection
- Morning brief scheduling

## 3. Files Changed/Created

### New Files
| File | Purpose |
|------|---------|
| `nova_comms/providers/bland.py` | Bland AI provider implementation |
| `nova_comms/providers/mock.py` | Mock provider for testing |
| `nova_comms/notifications.py` | NotificationService, DeliveryStateStore, preferences |
| `nova_comms/integration.py` | CommsIntegration with scheduler/proactive/heartbeat hooks |
| `tests/test_nova_comms.py` | Comprehensive test suite (34 tests) |
| `docs/implementation/proactive-communications-report.md` | This report |

### Modified Files
| File | Changes |
|------|---------|
| `nova_comms/__init__.py` | Exports all new classes and providers |
| `nova_comms/providers/__init__.py` | Exports BlandProvider, MockProvider |
| `nova_config.toml` | Added `[comms]` section with full configuration |
| `config.py` | Added comms defaults and validation |

## 4. Features Implemented

### ✅ Provider-Agnostic Calling
- Abstract `ICommunicationProvider` interface
- Auto-registration via `ProviderRegistry`
- Bland AI implementation with full API support:
  - Outbound calls with task instructions
  - Pathway-based calls
  - Webhook handling for call status updates
  - Recording and transcript retrieval
  - Call control (end, hold, transfer - where supported)

### ✅ Multi-Channel Notifications
| Channel | Provider | Status |
|---------|----------|--------|
| Voice | Bland AI / Twilio / Mock | ✅ Implemented |
| Desktop | Proactive Agent | ✅ Implemented |
| SMS | — | 🔄 Extensible (not implemented) |
| Email | — | 🔄 Extensible (not implemented) |
| WhatsApp/Telegram/Slack/Discord | — | 🔄 Extensible (not implemented) |

### ✅ User-Configurable Behavior
All settings in `nova_config.toml` under `[comms]`:
- Phone number (E.164) and verification status
- Timezone for scheduling
- Quiet hours (start/end hour, enabled toggle)
- Voice provider selection (bland/twilio/mock)
- Bland AI voice persona
- Max call duration
- Rate limits (calls/hour, calls/day)
- Retry policy (max attempts, base/max interval, exponential backoff, jitter)
- Escalation fallback channels
- Event-specific channel preferences
- Permission gates (require_confirmation_for, allowed_auto_actions)

### ✅ Intelligent Call Content
Three interaction modes supported via event types:
1. **Intelligent Reminders** (`EventType.REMINDER`) — "This is a reminder: {message}"
2. **Conversational Assistance** (`EventType.TASK_FOLLOWUP`, `EventType.OVERDUE_TASK`) — Context-aware prompts with offer to help
3. **Action-Oriented** (`EventType.CRITICAL_EVENT`, `EventType.MORNING_BRIEF`) — Direct, actionable messages

Calls use NOVA's existing context (memory, active tasks) when `task` parameter includes relevant context.

### ✅ Missed-Call Handling
- Configurable retry policy per user preferences
- Exponential backoff with optional jitter
- Escalation to fallback channels after max retries
- Duplicate prevention via per-channel deduplication keys
- Delivery state persisted to `delivery_state.json` (survives restarts)
- Webhook-based status updates from providers

### ✅ Safety & Authorization
- Capability-based permissions via `nova_core.permissions`
- `NETWORK_WRITE` capability required for outbound calls
- Confirmation required for configured sensitive actions
- Untrusted content cannot trigger outbound communications
- Credentials never logged; stored in secure vault via `nova_secure_store`

### ✅ Testing Infrastructure
- **MockProvider** — Configurable outcomes (answered, no_answer, busy, failed)
- Simulated call progression with realistic timing
- DTMF, hold, transfer simulation
- Call logs for verification
- Webhook handler registration for integration testing
- 34 passing tests covering:
  - Provider registry and instantiation
  - Call lifecycle (answered, no_answer, busy, failed)
  - DTMF, hold, transfer
  - Recording/transcript retrieval
  - Notification delivery (voice + desktop)
  - Deduplication (per-channel)
  - Retry policies and escalation
  - Delivery state persistence
  - Quiet hours and rate limiting
  - Integration with scheduler/proactive/heartbeat

## 5. Test Results

```
tests/test_nova_comms.py .................................. 34 passed in 28.30s
```

### Test Coverage Summary
| Test Class | Tests | Focus |
|------------|-------|-------|
| TestProviderRegistry | 3 | Provider discovery and instantiation |
| TestMockProvider | 9 | Call lifecycle, DTMF, hold, recording |
| TestNotificationService | 8 | Delivery, deduplication, retries, persistence |
| TestRetryPolicy | 2 | Retry on no_answer, escalation |
| TestDeliveryStateStore | 4 | Save/load, pending retries, cleanup |
| TestCommsIntegration | 4 | Scheduler hooks, critical events, status queries |
| TestBlandProvider | 3 | Structure, auth, error handling |
| TestNotificationPreferences | 2 | Defaults and custom configuration |

## 6. Mock vs Real Provider Behavior

| Aspect | MockProvider | BlandProvider |
|--------|--------------|---------------|
| **Network calls** | None (in-process async) | Real HTTPS to `api.bland.ai` |
| **Authentication** | None | Bearer token (`BLAND_API_KEY`) |
| **Call outcomes** | Configurable (answered/no_answer/busy/failed) | Determined by Bland AI |
| **Latency** | Simulated (configurable) | Real network latency |
| **Webhooks** | In-process callbacks | HTTP POST to configured URL |
| **Recordings** | Mock URL | Real audio files from Bland |
| **Transcripts** | Mock text | Real transcripts from Bland |
| **Cost** | Free | Per-minute billing |
| **Use case** | Unit/integration tests, CI/CD | Production, staging |

### MockProvider Configuration
```python
MockProvider(
    default_outcome=MockCallOutcome.ANSWERED,
    default_config=MockCallConfig(
        outcome=MockCallOutcome.ANSWERED,
        answer_delay=2.0,
        duration=30,
        recording_url="mock://recording.wav",
        transcript="Mock transcript..."
    ),
    simulate_latency=True
)
```

## 7. Remaining Provider Configuration Requirements

### Bland AI (Production)
Required environment variables:
```bash
export BLAND_API_KEY="org_..."           # From Bland dashboard
export BLAND_WEBHOOK_BASE_URL="https://yourdomain.com"  # For webhooks
export BLAND_FROM_NUMBER="+15551234567"  # Optional: verified caller ID
```

### Twilio (Optional Alternative)
```bash
export TWILIO_ACCOUNT_SID="AC..."
export TWILIO_AUTH_TOKEN="..."
export TWILIO_FROM_NUMBER="+15551234567"
export TWILIO_WEBHOOK_BASE_URL="https://yourdomain.com"
```

### Webhook Endpoint
The notification service expects webhooks at:
- `{WEBHOOK_BASE_URL}/webhook/bland` — Bland AI call status updates
- `{WEBHOOK_BASE_URL}/webhook/twilio` — Twilio call status updates

These should route to `NotificationService.handle_call_webhook(provider_name, payload)`.

## 8. Integration Instructions for Codex

### Scheduler Integration
The `CommsIntegration` class hooks into the scheduler's `WorkflowRunner`:
```python
integration = CommsIntegration(
    notification_service=service,
    scheduler=scheduler,
    proactive_agent=proactive_agent,
    heartbeat=heartbeat,
    planner=planner
)
integration.start()
```

This wraps the scheduler's workflow runner to emit notifications on:
- Workflow completion (success/failure/nothing_to_do)
- Reminder workflows
- Goal execution workflows

### Proactive Agent Integration
Desktop notifications route through the existing `ProactiveAgent`:
```python
notification_service.proactive_agent = proactive_agent
```

The proactive agent's spacing, deduplication, and quiet hours logic applies to desktop notifications.

### Heartbeat Integration
Quiet hours are synchronized via shared configuration in `nova_config.toml`. Both `Heartbeat` and `NotificationService` read the same `quiet_start`/`quiet_end` settings.

### Permissions Integration
Outbound calls require `NETWORK_WRITE` capability. The permission engine (`nova_core.permissions`) enforces this at the tool level. The `send_message` tool is already in `consequential_tools` requiring explicit confirmation.

## 9. Known Limitations

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| **Desktop channel requires proactive agent** | No desktop notifications if agent not running | Fallback to voice only; proactive agent starts with NOVA |
| **SMS/Email/WhatsApp not implemented** | Limited channel diversity | Extensible architecture; add providers as needed |
| **Bland AI inbound not implemented** | Cannot receive calls | Bland AI supports inbound; future work |
| **No real-time call streaming** | Cannot interject during call | Bland AI supports live streaming; future work |
| **Single recipient per event** | No group notifications | Event model supports single recipient; extend for groups |
| **Timezone handling simplified** | Uses local system time | IANA timezone support needed for distributed users |
| **No call transfer to human** | Limited escalation | Bland AI supports warm transfer; future work |

## 10. Security Considerations

- **Credential isolation**: API keys via environment variables, never in config files
- **Secure storage**: Tokens in OS keyring via `nova_secure_store`
- **Audit logging**: All delivery attempts logged with status (no PII in logs)
- **Prompt injection defense**: Call tasks constructed from structured templates, not raw user input
- **Rate limiting**: Per-user configurable limits prevent abuse
- **Untrusted content isolation**: Web/email content cannot trigger outbound calls (requires `TRUST.USER`)

## 11. Deployment Checklist

- [ ] Set `BLAND_API_KEY` in production environment
- [ ] Configure `BLAND_WEBHOOK_BASE_URL` with HTTPS endpoint
- [ ] Verify webhook endpoint receives POST from Bland AI
- [ ] Configure user phone number in `nova_config.toml` or via UI
- [ ] Test quiet hours with local timezone
- [ ] Validate rate limits match user preferences
- [ ] Run full test suite: `pytest tests/test_nova_comms.py -v`
- [ ] Monitor `nova.log` for `[NOTIFY]` and `[COMMS]` entries

## 12. Future Work

1. **SMS/Email providers** — Twilio SendGrid, Mailgun, etc.
2. **Group notifications** — Broadcast to multiple contacts
3. **Call recording playback** — In-app audio player
4. **Live call streaming** — Real-time transcript display
5. **Inbound call handling** — IVR, voicemail, callback scheduling
6. **Advanced analytics** — Call success rates, duration trends
7. **Multi-user support** — Per-user preferences and isolation
8. **Mobile push notifications** — FCM/APNs integration

---

*Report generated: 2026-10-14*  
*Implementation: NOVA Proactive Communications Subsystem*  
*Author: Implementation Engineer (isolated worktree)*
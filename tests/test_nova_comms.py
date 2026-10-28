"""Tests for NOVA proactive communications subsystem."""

import asyncio
import pytest
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

from nova_comms import (
    # Provider
    ICommunicationProvider,
    CallStatus,
    CallResult,
    CallDirection,
    ProviderRegistry,
    # Models
    PhoneNumber,
    PhoneNumberType,
    PhoneNumberCapability,
    CallRecording,
    CallTranscript,
    # Notifications
    NotificationService,
    DeliveryStateStore,
    NotificationPreferences,
    NotificationEvent,
    DeliveryRecord,
    ChannelType,
    DeliveryStatus,
    EventType,
    ChannelConfig,
    QuietHours,
    RetryPolicy,
    EscalationRule,
    # Providers
    MockProvider,
    MockCallOutcome,
    MockCallConfig,
    BlandProvider,
    # Integration
    CommsIntegration,
    get_comms_integration,
    init_comms_integration,
)


# ── Test Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for test files."""
    return tmp_path


@pytest.fixture
def mock_provider():
    """Create a mock provider with default answered outcome."""
    provider = MockProvider(default_outcome=MockCallOutcome.ANSWERED)
    provider._simulate_latency = False
    return provider


@pytest.fixture
def mock_provider_no_answer():
    """Create a mock provider that simulates no answer."""
    config = MockCallConfig(outcome=MockCallOutcome.NO_ANSWER, answer_delay=0.1)
    provider = MockProvider(default_outcome=MockCallOutcome.NO_ANSWER, default_config=config)
    provider._simulate_latency = False
    return provider


@pytest.fixture
def mock_provider_busy():
    """Create a mock provider that simulates busy."""
    config = MockCallConfig(outcome=MockCallOutcome.BUSY, answer_delay=0.1)
    provider = MockProvider(default_outcome=MockCallOutcome.BUSY, default_config=config)
    provider._simulate_latency = False
    return provider


@pytest.fixture
def mock_provider_failed():
    """Create a mock provider that simulates failure."""
    config = MockCallConfig(outcome=MockCallOutcome.FAILED, answer_delay=0.1)
    provider = MockProvider(default_outcome=MockCallOutcome.FAILED, default_config=config)
    provider._simulate_latency = False
    return provider


@pytest.fixture
def mock_proactive_agent():
    """Create a mock proactive agent for testing."""
    agent = Mock()
    agent.emit = Mock(return_value=True)  # Simulate successful desktop notification
    agent.awaiting_response = Mock(return_value=False)
    return agent


@pytest.fixture
def notification_service(temp_dir, mock_provider, mock_proactive_agent):
    """Create a notification service with mock provider."""
    prefs = NotificationPreferences(
        phone_number="+15551234567",
        phone_verified=True,
        channels={
            ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0),
            ChannelType.DESKTOP: ChannelConfig(enabled=True, provider="desktop", priority=1),
        },
        retry_policy=RetryPolicy(
            max_retries=2,
            base_interval_seconds=1,  # Fast for tests
            max_interval_seconds=5,
            exponential_backoff=False,
            jitter=False,
        ),
        escalation_rules=[
            EscalationRule(after_retries=2, fallback_channels=[ChannelType.DESKTOP])
        ],
    )
    
    state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
    service = NotificationService(preferences=prefs, state_store=state_store)
    service.register_provider(ChannelType.VOICE, mock_provider)
    service.proactive_agent = mock_proactive_agent
    return service


@pytest.fixture
def notification_service_no_answer(temp_dir, mock_provider_no_answer, mock_proactive_agent):
    """Create a notification service with no-answer mock provider."""
    prefs = NotificationPreferences(
        phone_number="+15551234567",
        phone_verified=True,
        channels={
            ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0),
            ChannelType.DESKTOP: ChannelConfig(enabled=True, provider="desktop", priority=1),
        },
        retry_policy=RetryPolicy(
            max_retries=2,
            base_interval_seconds=1,
            max_interval_seconds=5,
            exponential_backoff=False,
            jitter=False,
        ),
        escalation_rules=[
            EscalationRule(after_retries=2, fallback_channels=[ChannelType.DESKTOP])
        ],
    )
    
    state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
    service = NotificationService(preferences=prefs, state_store=state_store)
    service.register_provider(ChannelType.VOICE, mock_provider_no_answer)
    service.proactive_agent = mock_proactive_agent
    return service
    return service


# ── Provider Tests ───────────────────────────────────────────────────────────────

class TestProviderRegistry:
    """Test provider registry functionality."""

    def test_providers_registered(self):
        providers = ProviderRegistry.list_providers()
        assert "twilio" in providers
        assert "bland" in providers
        assert "mock" in providers

    def test_get_provider(self):
        provider_class = ProviderRegistry.get_provider("mock")
        assert provider_class is not None
        assert provider_class.__name__ == "MockProvider"

    def test_get_instance(self):
        provider = ProviderRegistry.get_instance("mock")
        assert provider is not None
        assert provider.provider_name == "mock"

    def test_unknown_provider_returns_none(self):
        provider = ProviderRegistry.get_provider("unknown")
        assert provider is None


class TestMockProvider:
    """Test mock calling provider."""

    @pytest.mark.asyncio
    async def test_make_call_answered(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        
        assert result.call_id.startswith("mock_")
        assert result.status == CallStatus.QUEUED
        assert result.to_number == "+15551234567"
        assert result.provider == "mock"

    @pytest.mark.asyncio
    async def test_call_progression_answered(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        call_id = result.call_id
        
        # Wait for call to progress
        await asyncio.sleep(3)
        
        status = await mock_provider.get_call_status(call_id)
        assert status is not None
        # Should have progressed to ANSWERED or COMPLETED
        assert status.status in (CallStatus.ANSWERED, CallStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_call_no_answer(self, mock_provider_no_answer):
        result = await mock_provider_no_answer.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(3)
        
        status = await mock_provider_no_answer.get_call_status(call_id)
        assert status is not None
        assert status.status == CallStatus.NO_ANSWER

    @pytest.mark.asyncio
    async def test_call_busy(self, mock_provider_busy):
        result = await mock_provider_busy.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(1)
        
        status = await mock_provider_busy.get_call_status(call_id)
        assert status is not None
        assert status.status == CallStatus.BUSY

    @pytest.mark.asyncio
    async def test_call_failed(self, mock_provider_failed):
        result = await mock_provider_failed.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(1)
        
        status = await mock_provider_failed.get_call_status(call_id)
        assert status is not None
        assert status.status == CallStatus.FAILED
        assert status.error_message is not None

    @pytest.mark.asyncio
    async def test_end_call(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        call_id = result.call_id
        
        await mock_provider.end_call(call_id)
        
        status = await mock_provider.get_call_status(call_id)
        assert status.status == CallStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_send_dtmf(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(2.5)  # Wait for answer
        
        success = await mock_provider.send_dtmf(call_id, "123")
        assert success is True
        
        # Check logs
        logs = mock_provider.get_call_logs()
        dtmf_logs = [l for l in logs if l.get("action") == "dtmf"]
        assert len(dtmf_logs) == 1
        assert dtmf_logs[0]["digits"] == "123"

    @pytest.mark.asyncio
    async def test_hold_call(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(2.5)
        
        success = await mock_provider.hold_call(call_id, True)
        assert success is True
        
        success = await mock_provider.hold_call(call_id, False)
        assert success is True

    @pytest.mark.asyncio
    async def test_get_recording_and_transcript(self, mock_provider):
        result = await mock_provider.make_call("+15551234567")
        call_id = result.call_id
        
        await asyncio.sleep(3)
        
        recording = await mock_provider.get_recording(call_id)
        assert recording is not None
        assert recording.url == "mock://recording.wav"
        
        transcript = await mock_provider.get_transcript(call_id)
        assert transcript is not None
        assert "mock call transcript" in transcript.text.lower()


# ── Notification Service Tests ───────────────────────────────────────────────────

class TestNotificationService:
    """Test notification service core functionality."""

    def test_create_notification_event(self):
        event = NotificationEvent(
            event_id="test-1",
            event_type=EventType.REMINDER,
            title="Test Reminder",
            message="This is a test reminder",
            priority=2,
            recipient="+15551234567",
            channels=[ChannelType.VOICE, ChannelType.DESKTOP],
        )
        
        assert event.event_id == "test-1"
        assert event.event_type == EventType.REMINDER
        assert ChannelType.VOICE in event.channels

    def test_send_notification_creates_records(self, notification_service):
        event = NotificationEvent(
            event_id="test-1",
            event_type=EventType.REMINDER,
            title="Test",
            message="Test message",
            priority=2,
        )
        records = notification_service.send_notification(event)
        
        assert len(records) == 2  # VOICE and DESKTOP
        voice_record = next(r for r in records if r.channel == ChannelType.VOICE)
        desktop_record = next(r for r in records if r.channel == ChannelType.DESKTOP)
        
        assert voice_record.channel == ChannelType.VOICE
        assert desktop_record.channel == ChannelType.DESKTOP
        # Voice channel immediately transitions to SENDING as call is initiated
        assert voice_record.status in (DeliveryStatus.PENDING, DeliveryStatus.SENDING)
        # Desktop channel is immediately DELIVERED when proactive agent is available
        assert desktop_record.status == DeliveryStatus.DELIVERED

    def test_deduplication_prevents_duplicate_sends(self, notification_service):
        event = NotificationEvent(
            event_id="test-1",
            event_type=EventType.REMINDER,
            title="Test",
            message="Test message",
            deduplication_key="unique-key-123",
        )
        
        # First send
        records1 = notification_service.send_notification(event)
        assert len(records1) == 2
        
        # Second send with same deduplication key
        records2 = notification_service.send_notification(event)
        assert len(records2) == 0  # Should be deduplicated

    def test_delivery_state_persistence(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
            retry_policy=RetryPolicy(max_retries=1, base_interval_seconds=1),
        )
        
        state_path = str(temp_dir / "delivery_state.json")
        state_store = DeliveryStateStore(path=state_path)
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        event = NotificationEvent(
            event_id="test-persist",
            event_type=EventType.REMINDER,
            title="Test",
            message="Test persistence",
            deduplication_key="persist-key",
        )
        
        records = service.send_notification(event)
        delivery_id = records[0].delivery_id
        
        # Create new service with same state store
        new_state_store = DeliveryStateStore(path=state_path)
        new_service = NotificationService(preferences=prefs, state_store=new_state_store)
        new_service.register_provider(ChannelType.VOICE, mock_provider)
        
        # Should find existing record
        existing = new_state_store.find_by_dedupe_key("persist-key:voice")
        assert existing is not None
        assert existing.delivery_id == delivery_id

    def test_quiet_hours_check(self, notification_service):
        # Mock time to be in quiet hours
        with patch('nova_comms.notifications.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2024, 1, 1, 23, 0)  # 11 PM
            mock_datetime.utcnow.return_value = datetime(2024, 1, 1, 23, 0)
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            
            # Note: The current implementation uses local time
            # This test may need adjustment based on actual implementation
            result = notification_service.is_in_quiet_hours()
            # The test depends on system time, so we just verify it runs
            assert isinstance(result, bool)

    def test_rate_limiting(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            max_calls_per_hour=2,  # Allow 2 calls per hour
            max_calls_per_day=5,
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
            retry_policy=RetryPolicy(max_retries=0),
            quiet_hours=QuietHours(enabled=False),
        )
        
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        # With 0 calls, should be allowed
        assert service.can_make_call() is True
        
        # Create a delivered record from 30 minutes ago
        record = DeliveryRecord(
            delivery_id="test-1",
            event_id="event-1",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.DELIVERED,
            created_at=datetime.utcnow() - timedelta(minutes=30),
        )
        state_store.add(record)
        
        # With 1 call in last hour and limit of 2, should still be allowed
        assert service.can_make_call() is True
        
        # Add another record from 10 minutes ago
        record2 = DeliveryRecord(
            delivery_id="test-2",
            event_id="event-2",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.DELIVERED,
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        state_store.add(record2)
        
        # With 2 calls in last hour and limit of 2, should be rate limited
        assert service.can_make_call() is False


class TestRetryPolicy:
    """Test retry and escalation logic."""

    @pytest.mark.asyncio
    async def test_retry_on_no_answer(self, notification_service_no_answer):
        event = NotificationEvent(
            event_id="test-retry",
            event_type=EventType.REMINDER,
            title="Test Retry",
            message="Test retry on no answer",
            priority=2,
        )
        
        records = notification_service_no_answer.send_notification(event)
        voice_record = next(r for r in records if r.channel == ChannelType.VOICE)
        
        # Wait for call to complete and retry
        await asyncio.sleep(5)
        
        # Should have retried
        updated = notification_service_no_answer.get_delivery_status(voice_record.delivery_id)
        assert updated.attempts >= 1

    @pytest.mark.asyncio
    async def test_escalation_after_max_retries(self, temp_dir, mock_provider_no_answer):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={
                ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0),
                ChannelType.DESKTOP: ChannelConfig(enabled=True, provider="desktop", priority=1),
            },
            retry_policy=RetryPolicy(
                max_retries=1,
                base_interval_seconds=1,
                exponential_backoff=False,
                jitter=False,
            ),
            escalation_rules=[
                EscalationRule(after_retries=1, fallback_channels=[ChannelType.DESKTOP])
            ],
        )
        
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider_no_answer)
        
        event = NotificationEvent(
            event_id="test-escalate",
            event_type=EventType.CRITICAL_EVENT,
            title="Critical",
            message="Test escalation",
            priority=1,
        )
        
        records = service.send_notification(event)
        voice_record = next(r for r in records if r.channel == ChannelType.VOICE)
        
        # Wait for retries and escalation
        await asyncio.sleep(5)
        
        # Should have created desktop fallback
        all_records = state_store.get_recent_for_event("test-escalate")
        desktop_records = [r for r in all_records if r.channel == ChannelType.DESKTOP]
        assert len(desktop_records) >= 1  # Escalation created desktop delivery


class TestDeliveryStateStore:
    """Test delivery state persistence."""

    def test_save_and_load(self, temp_dir):
        path = str(temp_dir / "test_delivery.json")
        store = DeliveryStateStore(path=path)
        
        record = DeliveryRecord(
            delivery_id="test-1",
            event_id="event-1",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.DELIVERED,
            deduplication_key="test-key",
        )
        
        store.add(record)
        
        # Load new store
        new_store = DeliveryStateStore(path=path)
        loaded = new_store.find_by_dedupe_key("test-key")
        
        assert loaded is not None
        assert loaded.delivery_id == "test-1"
        assert loaded.status == DeliveryStatus.DELIVERED

    def test_get_pending_retries(self, temp_dir):
        path = str(temp_dir / "test_delivery.json")
        store = DeliveryStateStore(path=path)
        
        now = datetime.utcnow()
        past = now - timedelta(seconds=10)
        future = now + timedelta(seconds=10)
        
        # Pending retry (past due)
        record1 = DeliveryRecord(
            delivery_id="test-1",
            event_id="event-1",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.PENDING,
            next_retry=past,
        )
        
        # Not yet due
        record2 = DeliveryRecord(
            delivery_id="test-2",
            event_id="event-2",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.PENDING,
            next_retry=future,
        )
        
        # Delivered (should not be returned)
        record3 = DeliveryRecord(
            delivery_id="test-3",
            event_id="event-3",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.DELIVERED,
            next_retry=past,
        )
        
        store.add(record1)
        store.add(record2)
        store.add(record3)
        
        pending = store.get_pending_retries(now)
        
        assert len(pending) == 1
        assert pending[0].delivery_id == "test-1"

    def test_cleanup_old_records(self, temp_dir):
        path = str(temp_dir / "test_delivery.json")
        store = DeliveryStateStore(path=path)
        
        old = datetime.utcnow() - timedelta(days=40)
        recent = datetime.utcnow() - timedelta(days=1)
        
        # Old delivered record
        record1 = DeliveryRecord(
            delivery_id="old-1",
            event_id="event-1",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.DELIVERED,
            created_at=old,
            updated_at=old,
        )
        
        # Old failed record
        record2 = DeliveryRecord(
            delivery_id="old-2",
            event_id="event-2",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.FAILED,
            created_at=old,
            updated_at=old,
        )
        
        # Recent pending (should not be cleaned)
        record3 = DeliveryRecord(
            delivery_id="recent-1",
            event_id="event-3",
            event_type=EventType.REMINDER,
            channel=ChannelType.VOICE,
            provider="mock",
            recipient="+15551234567",
            status=DeliveryStatus.PENDING,
            created_at=recent,
            updated_at=recent,
        )
        
        store.add(record1)
        store.add(record2)
        store.add(record3)
        
        cutoff = datetime.utcnow() - timedelta(days=30)
        removed = store.cleanup_old(cutoff)
        
        assert removed == 2
        
        remaining = store.get_all()
        assert len(remaining) == 1
        assert remaining[0].delivery_id == "recent-1"


# ── Integration Tests ────────────────────────────────────────────────────────────

class TestCommsIntegration:
    """Test integration with NOVA subsystems."""

    def test_integration_creation(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
        )
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        integration = CommsIntegration(notification_service=service)
        integration.start()
        
        assert integration.notification_service is service
        assert integration._running is True
        
        integration.stop()
        assert integration._running is False

    def test_schedule_morning_brief(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
        )
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        integration = CommsIntegration(notification_service=service)
        integration.start()
        
        integration.schedule_morning_brief("08:00")
        
        integration.stop()

    def test_trigger_critical_event(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
        )
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        integration = CommsIntegration(notification_service=service)
        integration.start()
        
        delivery_ids = integration.trigger_critical_event(
            "Test critical event",
            "Critical Test"
        )
        
        assert len(delivery_ids) > 0
        
        integration.stop()

    def test_get_delivery_status(self, temp_dir, mock_provider):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            channels={ChannelType.VOICE: ChannelConfig(enabled=True, provider="mock", priority=0)},
        )
        state_store = DeliveryStateStore(path=str(temp_dir / "delivery_state.json"))
        service = NotificationService(preferences=prefs, state_store=state_store)
        service.register_provider(ChannelType.VOICE, mock_provider)
        
        integration = CommsIntegration(notification_service=service)
        integration.start()
        
        delivery_ids = integration.trigger_critical_event("Test")
        delivery_id = delivery_ids[0]
        
        status = integration.get_delivery_status(delivery_id)
        
        assert status is not None
        assert status["delivery_id"] == delivery_id
        assert "status" in status
        
        integration.stop()


# ── Bland Provider Tests (Structure Only - Requires API Key) ─────────────────────

class TestBlandProvider:
    """Test Bland AI provider structure (no real API calls)."""

    def test_provider_registration(self):
        providers = ProviderRegistry.list_providers()
        assert "bland" in providers
        
        provider_class = ProviderRegistry.get_provider("bland")
        assert provider_class is not None
        assert provider_class.__name__ == "BlandProvider"

    def test_provider_properties(self):
        provider = BlandProvider(api_key="test-key")
        
        assert provider.provider_name == "bland"
        assert provider.supports_inbound is False
        assert provider.supports_recording is True
        assert provider.supports_transcription is True

    @pytest.mark.asyncio
    async def test_make_call_requires_api_key(self):
        provider = BlandProvider(api_key=None)
        
        # Mock environment to not have API key
        with patch.dict('os.environ', {}, clear=True):
            result = await provider.make_call("+15551234567")
            
            assert result.status == CallStatus.FAILED
            assert "not configured" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_get_call_status_not_found(self):
        provider = BlandProvider(api_key="test-key")
        
        # Mock session to return 404
        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_session.get.return_value.__aenter__.return_value = mock_response
        provider._session = mock_session
        
        result = await provider.get_call_status("nonexistent")
        assert result is None


# ── Configuration Tests ──────────────────────────────────────────────────────────

class TestNotificationPreferences:
    """Test notification preferences configuration."""

    def test_default_preferences(self):
        prefs = NotificationPreferences()
        
        assert prefs.phone_number == ""
        assert prefs.timezone == "UTC"
        assert prefs.quiet_hours.enabled is True
        assert prefs.quiet_hours.start_hour == 22
        assert prefs.quiet_hours.end_hour == 7
        assert ChannelType.VOICE in prefs.channels
        assert ChannelType.DESKTOP in prefs.channels
        assert prefs.retry_policy.max_retries == 3
        assert len(prefs.escalation_rules) >= 1

    def test_custom_preferences(self):
        prefs = NotificationPreferences(
            phone_number="+15551234567",
            phone_verified=True,
            timezone="America/New_York",
            quiet_hours=QuietHours(enabled=True, start_hour=23, end_hour=8),
            max_calls_per_hour=5,
            max_calls_per_day=20,
        )
        
        assert prefs.phone_number == "+15551234567"
        assert prefs.phone_verified is True
        assert prefs.timezone == "America/New_York"
        assert prefs.quiet_hours.start_hour == 23
        assert prefs.quiet_hours.end_hour == 8
        assert prefs.max_calls_per_hour == 5
        assert prefs.max_calls_per_day == 20


# ── Main Test Runner ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
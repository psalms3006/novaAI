"""Notification service for NOVA proactive communications."""

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .provider import ICommunicationProvider, CallStatus, CallResult, CallDirection
from .models import PhoneNumber
from .registry import ProviderRegistry

log = logging.getLogger("nova.comms.notifications")


class ChannelType(str, Enum):
    """Supported communication channels."""
    VOICE = "voice"
    SMS = "sms"
    EMAIL = "email"
    DESKTOP = "desktop"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    SLACK = "slack"
    DISCORD = "discord"


class DeliveryStatus(str, Enum):
    """Delivery status for notifications."""
    PENDING = "pending"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    """Types of events that can trigger notifications."""
    MORNING_BRIEF = "morning_brief"
    REMINDER = "reminder"
    CALENDAR_EVENT = "calendar_event"
    IMPORTANT_EMAIL = "important_email"
    OVERDUE_TASK = "overdue_task"
    CRITICAL_EVENT = "critical_event"
    TASK_FOLLOWUP = "task_followup"
    USER_CONDITION = "user_condition"
    GOAL_MILESTONE = "goal_milestone"
    SYSTEM_ALERT = "system_alert"


@dataclass
class ChannelConfig:
    """Configuration for a communication channel."""
    enabled: bool = True
    provider: str = ""  # e.g., "bland", "twilio", "mock"
    priority: int = 0  # Lower = higher priority (tried first)
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryPolicy:
    """Retry policy for failed deliveries."""
    max_retries: int = 3
    base_interval_seconds: int = 300  # 5 minutes
    max_interval_seconds: int = 3600  # 1 hour
    exponential_backoff: bool = True
    jitter: bool = True


@dataclass
class EscalationRule:
    """Escalation rule for failed deliveries."""
    after_retries: int = 3
    fallback_channels: List[ChannelType] = field(default_factory=list)
    notify_user: bool = True


@dataclass
class QuietHours:
    """Quiet hours configuration."""
    enabled: bool = True
    start_hour: int = 22  # 10 PM
    end_hour: int = 7     # 7 AM
    timezone: str = "local"


@dataclass
class NotificationPreferences:
    """User notification preferences."""
    phone_number: str = ""
    phone_verified: bool = False
    timezone: str = "UTC"
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    channels: Dict[ChannelType, ChannelConfig] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    escalation_rules: List[EscalationRule] = field(default_factory=list)
    event_preferences: Dict[EventType, Dict[str, Any]] = field(default_factory=dict)
    max_calls_per_hour: int = 3
    max_calls_per_day: int = 10
    require_confirmation_for: List[str] = field(default_factory=list)  # Actions requiring approval
    allowed_auto_actions: List[str] = field(default_factory=list)  # Actions allowed without approval

    def __post_init__(self):
        # Default channel configuration
        if not self.channels:
            self.channels = {
                ChannelType.VOICE: ChannelConfig(
                    enabled=True,
                    provider="bland",
                    priority=0,
                    config={}
                ),
                ChannelType.DESKTOP: ChannelConfig(
                    enabled=True,
                    provider="desktop",
                    priority=1,
                    config={}
                ),
            }
        
        # Default escalation rules
        if not self.escalation_rules:
            self.escalation_rules = [
                EscalationRule(
                    after_retries=3,
                    fallback_channels=[ChannelType.DESKTOP],
                    notify_user=True
                )
            ]


@dataclass
class DeliveryRecord:
    """Persistent record of a delivery attempt."""
    delivery_id: str
    event_id: str
    event_type: EventType
    channel: ChannelType
    provider: str
    recipient: str
    status: DeliveryStatus
    attempts: int = 0
    last_attempt: Optional[datetime] = None
    next_retry: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    call_result: Optional[CallResult] = None
    deduplication_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["channel"] = self.channel.value
        data["status"] = self.status.value
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        if self.last_attempt:
            data["last_attempt"] = self.last_attempt.isoformat()
        if self.next_retry:
            data["next_retry"] = self.next_retry.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()
        if self.call_result:
            data["call_result"] = {
                "call_id": self.call_result.call_id,
                "status": self.call_result.status.value,
                "duration_seconds": self.call_result.duration_seconds,
            }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeliveryRecord":
        data = dict(data)
        data["channel"] = ChannelType(data["channel"])
        data["status"] = DeliveryStatus(data["status"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        if data.get("last_attempt"):
            data["last_attempt"] = datetime.fromisoformat(data["last_attempt"])
        if data.get("next_retry"):
            data["next_retry"] = datetime.fromisoformat(data["next_retry"])
        if data.get("completed_at"):
            data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        if data.get("call_result"):
            # Reconstruct minimal CallResult
            from .provider import CallResult, CallStatus
            cr = data["call_result"]
            data["call_result"] = CallResult(
                call_id=cr.get("call_id", ""),
                status=CallStatus(cr.get("status", "completed")),
                provider="",
                duration_seconds=cr.get("duration_seconds", 0),
            )
        return cls(**data)


@dataclass
class NotificationEvent:
    """An event that may trigger a notification."""
    event_id: str
    event_type: EventType
    title: str
    message: str
    priority: int = 1  # 1=critical, 2=high, 3=medium, 4=low
    recipient: str = ""
    channels: List[ChannelType] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    scheduled_for: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    deduplication_key: str = ""
    requires_response: bool = False
    callback: Optional[Callable] = None


class DeliveryStateStore:
    """Persistent storage for delivery records."""

    def __init__(self, path: Optional[str] = None):
        import nova_paths
        self.path = Path(path) if path else nova_paths.data_file("delivery_state.json")
        self._lock = threading.RLock()
        self._records: Dict[str, DeliveryRecord] = {}
        self._dedupe_index: Dict[str, str] = {}  # deduplication_key -> delivery_id
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for item in raw or []:
                    try:
                        record = DeliveryRecord.from_dict(item)
                        self._records[record.delivery_id] = record
                        if record.deduplication_key:
                            self._dedupe_index[record.deduplication_key] = record.delivery_id
                    except Exception:
                        log.warning("[DELIVERY] dropping unreadable record", exc_info=True)
        except Exception:
            log.warning("[DELIVERY] could not read %s; starting empty", self.path)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps([r.to_dict() for r in self._records.values()], indent=2),
                encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception:
            log.warning("[DELIVERY] could not write %s", self.path, exc_info=True)

    def add(self, record: DeliveryRecord) -> None:
        with self._lock:
            self._records[record.delivery_id] = record
            if record.deduplication_key:
                self._dedupe_index[record.deduplication_key] = record.delivery_id
            self._save()

    def get(self, delivery_id: str) -> Optional[DeliveryRecord]:
        with self._lock:
            return self._records.get(delivery_id)

    def update(self, record: DeliveryRecord) -> None:
        with self._lock:
            record.updated_at = datetime.utcnow()
            self._records[record.delivery_id] = record
            if record.deduplication_key:
                self._dedupe_index[record.deduplication_key] = record.delivery_id
            self._save()

    def find_by_dedupe_key(self, key: str) -> Optional[DeliveryRecord]:
        with self._lock:
            delivery_id = self._dedupe_index.get(key)
            if delivery_id:
                return self._records.get(delivery_id)
        return None

    def get_pending_retries(self, now: datetime) -> List[DeliveryRecord]:
        with self._lock:
            return [
                r for r in self._records.values()
                if r.status in (DeliveryStatus.PENDING, DeliveryStatus.FAILED, DeliveryStatus.NO_ANSWER, DeliveryStatus.BUSY)
                and r.next_retry is not None
                and r.next_retry <= now
            ]

    def get_recent_for_event(self, event_id: str) -> List[DeliveryRecord]:
        with self._lock:
            return [
                r for r in self._records.values()
                if r.event_id == event_id
            ]

    def get_all(self) -> List[DeliveryRecord]:
        with self._lock:
            return list(self._records.values())

    def cleanup_old(self, older_than: datetime) -> int:
        with self._lock:
            to_remove = [
                k for k, v in self._records.items()
                if v.updated_at < older_than and v.status in (DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED)
            ]
            for k in to_remove:
                record = self._records.pop(k)
                if record.deduplication_key and self._dedupe_index.get(record.deduplication_key) == k:
                    del self._dedupe_index[record.deduplication_key]
            if to_remove:
                self._save()
            return len(to_remove)


class NotificationService:
    """Provider-independent notification service for NOVA."""

    def __init__(
        self,
        preferences: Optional[NotificationPreferences] = None,
        state_store: Optional[DeliveryStateStore] = None,
        proactive_agent=None,
        scheduler=None,
    ):
        self.preferences = preferences or NotificationPreferences()
        self.state_store = state_store or DeliveryStateStore()
        self.proactive_agent = proactive_agent
        self.scheduler = scheduler
        self._providers: Dict[str, ICommunicationProvider] = {}
        self._provider_instances: Dict[ChannelType, ICommunicationProvider] = {}
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._delivery_callbacks: List[Callable[[DeliveryRecord], None]] = []

    def register_provider(self, channel: ChannelType, provider: ICommunicationProvider) -> None:
        """Register a provider for a channel."""
        with self._lock:
            self._providers[f"{channel.value}:{provider.provider_name}"] = provider
            # Set as default if higher priority
            config = self.preferences.channels.get(channel)
            if config and config.provider == provider.provider_name:
                self._provider_instances[channel] = provider

    def get_provider(self, channel: ChannelType) -> Optional[ICommunicationProvider]:
        """Get the configured provider for a channel."""
        with self._lock:
            # Check explicit instance first
            if channel in self._provider_instances:
                return self._provider_instances[channel]
            
            # Fall back to config
            config = self.preferences.channels.get(channel)
            if config and config.enabled and config.provider:
                key = f"{channel.value}:{config.provider}"
                return self._providers.get(key)
        return None

    def add_delivery_callback(self, callback: Callable[[DeliveryRecord], None]) -> None:
        """Add a callback for delivery status changes."""
        self._delivery_callbacks.append(callback)

    def _notify_delivery_update(self, record: DeliveryRecord) -> None:
        """Notify callbacks of delivery status change."""
        for callback in self._delivery_callbacks:
            try:
                callback(record)
            except Exception:
                log.debug("[NOTIFY] delivery callback failed", exc_info=True)

    def start(self) -> None:
        """Start the notification worker."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="NOVANotificationWorker"
        )
        self._worker_thread.start()
        log.info("[NOTIFY] notification worker started")

    def stop(self) -> None:
        """Stop the notification worker."""
        self._running = False
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
        log.info("[NOTIFY] notification worker stopped")

    def _worker_loop(self) -> None:
        """Background loop for processing deliveries and retries."""
        while self._running and not self._stop_event.is_set():
            try:
                self._process_pending_retries()
                self._cleanup_old_records()
            except Exception:
                log.exception("[NOTIFY] worker loop error")
            self._stop_event.wait(60.0)  # Check every minute

    def _process_pending_retries(self) -> None:
        """Process deliveries that are due for retry."""
        now = datetime.utcnow()
        pending = self.state_store.get_pending_retries(now)
        
        for record in pending:
            if not self._running:
                break
            try:
                self._retry_delivery(record)
            except Exception:
                log.exception("[NOTIFY] retry failed for %s", record.delivery_id)

    def _cleanup_old_records(self) -> None:
        """Clean up old delivery records."""
        cutoff = datetime.utcnow() - timedelta(days=30)
        self.state_store.cleanup_old(cutoff)

    def send_notification(
        self,
        event: NotificationEvent,
        channels: Optional[List[ChannelType]] = None,
    ) -> List[DeliveryRecord]:
        """Send a notification through configured channels."""
        target_channels = channels or event.channels or self._get_default_channels(event)
        
        records = []
        for channel in target_channels:
            if not self._is_channel_enabled(channel):
                continue
            
            # Check deduplication per channel
            record = self._create_delivery_record(event, channel)
            if record.deduplication_key:
                existing = self.state_store.find_by_dedupe_key(record.deduplication_key)
                if existing and existing.status in (DeliveryStatus.DELIVERED, DeliveryStatus.SENDING, DeliveryStatus.SCHEDULED):
                    log.info("[NOTIFY] skipping duplicate event for channel %s: %s", channel.value, record.deduplication_key)
                    continue
            
            records.append(record)
            
            if event.scheduled_for and event.scheduled_for > datetime.utcnow():
                record.status = DeliveryStatus.SCHEDULED
                record.next_retry = event.scheduled_for
            else:
                # Send immediately
                self._send_delivery(record, event)
            
            self.state_store.add(record)
        
        return records

    def _get_default_channels(self, event: NotificationEvent) -> List[ChannelType]:
        """Get default channels for an event type, ordered by priority."""
        event_prefs = self.preferences.event_preferences.get(event.event_type, {})
        channel_names = event_prefs.get("channels")
        
        if channel_names:
            return [ChannelType(c) for c in channel_names if c in ChannelType.__members__.values()]
        
        # Default: voice first, then desktop
        enabled = [
            (c, cfg.priority) for c, cfg in self.preferences.channels.items()
            if cfg.enabled
        ]
        enabled.sort(key=lambda x: x[1])
        return [c for c, _ in enabled]

    def _is_channel_enabled(self, channel: ChannelType) -> bool:
        """Check if a channel is enabled and configured."""
        config = self.preferences.channels.get(channel)
        return config is not None and config.enabled

    def _create_delivery_record(self, event: NotificationEvent, channel: ChannelType) -> DeliveryRecord:
        """Create a delivery record for an event/channel combination."""
        config = self.preferences.channels.get(channel)
        provider_name = config.provider if config else "unknown"
        
        # Always include channel in deduplication key to prevent cross-channel deduplication
        base_key = event.deduplication_key or f"{event.event_type.value}:{event.recipient}"
        dedupe_key = f"{base_key}:{channel.value}"
        
        return DeliveryRecord(
            delivery_id=f"del_{uuid.uuid4().hex[:12]}",
            event_id=event.event_id,
            event_type=event.event_type,
            channel=channel,
            provider=provider_name,
            recipient=event.recipient or self.preferences.phone_number,
            status=DeliveryStatus.PENDING,
            deduplication_key=dedupe_key,
            metadata=event.metadata,
        )

    def _send_delivery(self, record: DeliveryRecord, event: NotificationEvent) -> None:
        """Send a delivery through the appropriate channel."""
        # Desktop channel doesn't use a provider - it uses the proactive agent
        if record.channel == ChannelType.DESKTOP:
            self._send_desktop(record, event)
            return
        
        # Voice and other channels need a provider
        provider = self.get_provider(record.channel)
        if not provider:
            record.status = DeliveryStatus.FAILED
            record.error_message = f"No provider configured for {record.channel.value}"
            self.state_store.update(record)
            self._notify_delivery_update(record)
            return

        record.status = DeliveryStatus.SENDING
        record.attempts += 1
        record.last_attempt = datetime.utcnow()
        self.state_store.update(record)

        # Dispatch based on channel type
        if record.channel == ChannelType.VOICE:
            self._send_voice(record, event, provider)
        else:
            # Other channels not yet implemented
            record.status = DeliveryStatus.FAILED
            record.error_message = f"Channel {record.channel.value} not implemented"
            self.state_store.update(record)
            self._notify_delivery_update(record)

    def _send_voice(self, record: DeliveryRecord, event: NotificationEvent, provider: ICommunicationProvider) -> None:
        """Send a voice call."""
        # Run async call in thread pool
        import concurrent.futures
        
        def run_call():
            loop = asyncio.new_event_loop()
            try:
                # Prepare call task based on event type
                task = self._build_call_task(event)
                
                result = loop.run_until_complete(provider.make_call(
                    to_number=record.recipient,
                    task=task,
                    metadata={**record.metadata, "delivery_id": record.delivery_id, "event_type": event.event_type.value},
                ))
                
                record.call_result = result
                
                if result.status == CallStatus.QUEUED:
                    record.status = DeliveryStatus.SENDING
                    # Schedule status check
                    record.next_retry = datetime.utcnow() + timedelta(seconds=30)
                elif result.status == CallStatus.FAILED:
                    record.status = DeliveryStatus.FAILED
                    record.error_message = result.error_message or "Call failed"
                    self._schedule_retry(record)
                else:
                    record.status = DeliveryStatus.DELIVERED
                    record.completed_at = datetime.utcnow()
                
                self.state_store.update(record)
                self._notify_delivery_update(record)
                
            finally:
                loop.close()
        
        # Run in background thread
        threading.Thread(target=run_call, daemon=True).start()

    def _build_call_task(self, event: NotificationEvent) -> str:
        """Build a call task description from an event."""
        base_task = f"You are NOVA, a helpful AI assistant. {event.message}"
        
        # Add context based on event type
        if event.event_type == EventType.MORNING_BRIEF:
            base_task = f"Good morning! This is your daily briefing. {event.message}"
        elif event.event_type == EventType.REMINDER:
            base_task = f"This is a reminder: {event.message}"
        elif event.event_type == EventType.OVERDUE_TASK:
            base_task = f"You have an overdue task: {event.message}. Would you like help with it?"
        elif event.event_type == EventType.CRITICAL_EVENT:
            base_task = f"Important alert: {event.message}"
        elif event.event_type == EventType.TASK_FOLLOWUP:
            base_task = f"Following up on your task: {event.message}. How is it going?"
        
        if event.requires_response:
            base_task += " Please respond when you can."
        
        return base_task

    def _send_desktop(self, record: DeliveryRecord, event: NotificationEvent) -> None:
        """Send a desktop notification (via proactive agent)."""
        if self.proactive_agent:
            from nova_proactive import ProactiveEvent, Priority
            
            priority_map = {1: Priority.CRITICAL, 2: Priority.HIGH, 3: Priority.MEDIUM, 4: Priority.LOW}
            priority = priority_map.get(event.priority, Priority.MEDIUM)
            
            proactive_event = ProactiveEvent(
                kind=event.event_type.value,
                message=event.message,
                priority=priority,
                requires_response=event.requires_response,
                dedupe_key=record.deduplication_key,
            )
            
            spoken = self.proactive_agent.emit(proactive_event)
            
            if spoken:
                record.status = DeliveryStatus.DELIVERED
                record.completed_at = datetime.utcnow()
            else:
                record.status = DeliveryStatus.PENDING
                record.next_retry = datetime.utcnow() + timedelta(seconds=300)
        else:
            record.status = DeliveryStatus.FAILED
            record.error_message = "No proactive agent available for desktop notifications"
        
        self.state_store.update(record)
        self._notify_delivery_update(record)

    def _schedule_retry(self, record: DeliveryRecord) -> None:
        """Schedule a retry for a failed delivery."""
        policy = self.preferences.retry_policy
        
        if record.attempts >= policy.max_retries:
            record.status = DeliveryStatus.FAILED
            self._escalate(record)
            return
        
        # Calculate next retry time
        if policy.exponential_backoff:
            interval = min(
                policy.base_interval_seconds * (2 ** (record.attempts - 1)),
                policy.max_interval_seconds
            )
        else:
            interval = policy.base_interval_seconds
        
        if policy.jitter:
            import random
            interval = interval * (0.5 + random.random())  # 50-150% of interval
        
        record.next_retry = datetime.utcnow() + timedelta(seconds=interval)
        record.status = DeliveryStatus.PENDING
        self.state_store.update(record)

    def _escalate(self, record: DeliveryRecord) -> None:
        """Escalate a failed delivery."""
        for rule in self.preferences.escalation_rules:
            if record.attempts >= rule.after_retries:
                for fallback_channel in rule.fallback_channels:
                    if self._is_channel_enabled(fallback_channel):
                        # Create new delivery record for fallback
                        fallback_record = DeliveryRecord(
                            delivery_id=f"del_{uuid.uuid4().hex[:12]}",
                            event_id=record.event_id,
                            event_type=record.event_type,
                            channel=fallback_channel,
                            provider=self.preferences.channels.get(fallback_channel, ChannelConfig()).provider,
                            recipient=record.recipient,
                            status=DeliveryStatus.PENDING,
                            deduplication_key=record.deduplication_key + f":fallback:{fallback_channel.value}",
                            metadata={**record.metadata, "escalated_from": record.channel.value},
                        )
                        self.state_store.add(fallback_record)
                        # Try to send immediately
                        # We need the original event - reconstruct minimal
                        event = NotificationEvent(
                            event_id=record.event_id,
                            event_type=record.event_type,
                            title="",
                            message=record.metadata.get("original_message", "Notification"),
                            recipient=record.recipient,
                            metadata=record.metadata,
                        )
                        self._send_delivery(fallback_record, event)
                
                if rule.notify_user and self.proactive_agent:
                    from nova_proactive import ProactiveEvent, Priority
                    self.proactive_agent.emit(ProactiveEvent(
                        kind="escalation",
                        message=f"Failed to deliver notification after {record.attempts} attempts. Trying alternative channels.",
                        priority=Priority.HIGH,
                    ))
                break

    def _retry_delivery(self, record: DeliveryRecord) -> None:
        """Retry a pending delivery."""
        # Reconstruct minimal event
        event = NotificationEvent(
            event_id=record.event_id,
            event_type=record.event_type,
            title="",
            message=record.metadata.get("original_message", "Notification"),
            recipient=record.recipient,
            metadata=record.metadata,
        )
        self._send_delivery(record, event)

    def handle_call_webhook(self, provider_name: str, payload: Dict[str, Any]) -> Optional[DeliveryRecord]:
        """Handle incoming webhook from a calling provider."""
        call_id = payload.get("call_id")
        if not call_id:
            return None
        
        # Find delivery record by call_id
        for record in self.state_store.get_all():
            if record.call_result and record.call_result.call_id == call_id:
                # Update status based on webhook
                event = payload.get("event", "").lower()
                
                status_map = {
                    "call_started": DeliveryStatus.SENDING,
                    "call_ringing": DeliveryStatus.SENDING,
                    "call_answered": DeliveryStatus.DELIVERED,
                    "call_ended": DeliveryStatus.DELIVERED,
                    "call_failed": DeliveryStatus.FAILED,
                    "call_busy": DeliveryStatus.BUSY,
                    "call_no_answer": DeliveryStatus.NO_ANSWER,
                }
                
                new_status = status_map.get(event)
                if new_status:
                    record.status = new_status
                    if new_status in (DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, 
                                     DeliveryStatus.BUSY, DeliveryStatus.NO_ANSWER):
                        record.completed_at = datetime.utcnow()
                    elif new_status in (DeliveryStatus.BUSY, DeliveryStatus.NO_ANSWER):
                        self._schedule_retry(record)
                    
                    # Update call result if available
                    if self._providers.get(f"voice:{provider_name}"):
                        provider = self._providers[f"voice:{provider_name}"]
                        # Provider will update call_result on next get_call_status
                        pass
                    
                    self.state_store.update(record)
                    self._notify_delivery_update(record)
                    return record
        
        return None

    def get_delivery_status(self, delivery_id: str) -> Optional[DeliveryRecord]:
        """Get the status of a delivery."""
        return self.state_store.get(delivery_id)

    def get_event_deliveries(self, event_id: str) -> List[DeliveryRecord]:
        """Get all delivery records for an event."""
        return self.state_store.get_recent_for_event(event_id)

    def is_in_quiet_hours(self, timezone: Optional[str] = None) -> bool:
        """Check if current time is in quiet hours."""
        if not self.preferences.quiet_hours.enabled:
            return False
        
        # Simplified - uses local time
        now = datetime.now()
        hour = now.hour
        
        start = self.preferences.quiet_hours.start_hour
        end = self.preferences.quiet_hours.end_hour
        
        if start > end:  # e.g., 22 to 7
            return hour >= start or hour < end
        return start <= hour < end

    def can_make_call(self) -> bool:
        """Check if we can make a call (rate limits, quiet hours)."""
        if self.is_in_quiet_hours():
            return False
        
        # Check rate limits
        now = datetime.utcnow()
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)
        
        recent = self.state_store.get_all()
        calls_last_hour = sum(1 for r in recent 
                             if r.channel == ChannelType.VOICE 
                             and r.created_at > hour_ago
                             and r.status == DeliveryStatus.DELIVERED)
        calls_today = sum(1 for r in recent 
                         if r.channel == ChannelType.VOICE 
                         and r.created_at > day_ago
                         and r.status == DeliveryStatus.DELIVERED)
        
        if calls_last_hour >= self.preferences.max_calls_per_hour:
            return False
        if calls_today >= self.preferences.max_calls_per_day:
            return False
        
        return True


# Global instance
_notification_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def init_notification_service(
    preferences: Optional[NotificationPreferences] = None,
    state_store: Optional[DeliveryStateStore] = None,
    proactive_agent=None,
    scheduler=None,
) -> NotificationService:
    global _notification_service
    _notification_service = NotificationService(preferences, state_store, proactive_agent, scheduler)
    return _notification_service


__all__ = [
    "NotificationService",
    "DeliveryStateStore",
    "NotificationPreferences",
    "NotificationEvent",
    "DeliveryRecord",
    "ChannelType",
    "DeliveryStatus",
    "EventType",
    "ChannelConfig",
    "QuietHours",
    "RetryPolicy",
    "EscalationRule",
    "get_notification_service",
    "init_notification_service",
]
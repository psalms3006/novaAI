"""Integration layer for NOVA proactive communications.

Connects the NotificationService with:
- nova_scheduler (workflows, reminders)
- nova_proactive (desktop notifications, voice)
- nova_heartbeat (morning brief, quiet hours)
- nova_planner (user-defined reminders)
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from nova_comms import (
    NotificationService,
    NotificationPreferences,
    NotificationEvent,
    DeliveryStateStore,
    ChannelType,
    EventType,
    DeliveryStatus,
    QuietHours,
    RetryPolicy,
    EscalationRule,
    get_notification_service,
    init_notification_service,
)

log = logging.getLogger("nova.comms.integration")


class CommsIntegration:
    """Integrates proactive communications with NOVA subsystems."""

    def __init__(
        self,
        notification_service: Optional[NotificationService] = None,
        scheduler=None,
        proactive_agent=None,
        heartbeat=None,
        planner=None,
    ):
        self.notification_service = notification_service or get_notification_service()
        self.scheduler = scheduler
        self.proactive_agent = proactive_agent
        self.heartbeat = heartbeat
        self.planner = planner
        self._running = False
        self._lock = threading.RLock()

    def start(self) -> None:
        """Start the integration."""
        if self._running:
            return
        
        self._running = True
        self.notification_service.start()
        
        # Register providers based on configuration
        self._register_providers()
        
        # Hook into scheduler if available
        if self.scheduler:
            self._hook_scheduler()
        
        # Hook into proactive agent for desktop notifications
        if self.proactive_agent:
            self.notification_service.proactive_agent = self.proactive_agent
        
        # Hook into heartbeat for quiet hours sync
        if self.heartbeat:
            self._sync_heartbeat_quiet_hours()
        
        log.info("[COMMS] Integration started")

    def stop(self) -> None:
        """Stop the integration."""
        self._running = False
        self.notification_service.stop()
        log.info("[COMMS] Integration stopped")

    def _register_providers(self) -> None:
        """Register communication providers based on configuration."""
        from nova_comms import ProviderRegistry
        
        # Get configured voice provider
        from config import get
        voice_provider_name = get("comms.voice_provider", "mock")
        
        # Register the voice provider
        voice_provider_class = ProviderRegistry.get_provider(voice_provider_name)
        if voice_provider_class:
            if voice_provider_name == "bland":
                # Bland provider needs API key from env
                provider = voice_provider_class()
            elif voice_provider_name == "twilio":
                provider = voice_provider_class()
            else:
                provider = voice_provider_class()  # mock
            
            self.notification_service.register_provider(ChannelType.VOICE, provider)
            log.info(f"[COMMS] Registered voice provider: {voice_provider_name}")
        
        # Desktop provider is built into proactive agent
        # No separate provider needed

    def _hook_scheduler(self) -> None:
        """Hook into scheduler workflow runner for notifications."""
        if not hasattr(self.scheduler, '_workflow_runner'):
            return
        
        runner = self.scheduler._workflow_runner
        if not runner:
            return
        
        # Wrap the runner to add notifications
        original_runner = runner
        
        def notified_runner(workflow):
            # Execute original workflow
            from nova_scheduler import RunOutcome
            outcome = original_runner(workflow)
            
            # Send notification based on workflow kind and outcome
            self._notify_workflow_completion(workflow, outcome)
            
            return outcome
        
        runner._handlers = original_runner._handlers
        runner.__call__ = notified_runner
        
        log.info("[COMMS] Scheduler hooked for notifications")

    def _notify_workflow_completion(self, workflow, outcome) -> None:
        """Send notification for workflow completion."""
        from nova_scheduler import RunOutcome, Workflow
        from nova_comms import EventType
        
        # Determine event type from workflow kind
        event_type_map = {
            "reminder": EventType.REMINDER,
            "nova_goal": EventType.TASK_FOLLOWUP,
            "email_sweep": EventType.IMPORTANT_EMAIL,
            "social_post": EventType.USER_CONDITION,
        }
        
        event_type = event_type_map.get(workflow.kind, EventType.USER_CONDITION)
        
        # Build message based on outcome
        if outcome == RunOutcome.DONE:
            message = f"Scheduled task '{workflow.title}' completed successfully."
        elif outcome == RunOutcome.FAILED:
            message = f"Scheduled task '{workflow.title}' failed. Please check."
            event_type = EventType.CRITICAL_EVENT
        elif outcome == RunOutcome.NOTHING_TO_DO:
            return  # Don't notify for nothing to do
        else:
            message = f"Scheduled task '{workflow.title}' finished with status: {outcome.value}"
        
        # Create notification event
        event = NotificationEvent(
            event_id=f"workflow_{workflow.id}_{workflow.runs}",
            event_type=event_type,
            title=workflow.title,
            message=message,
            priority=2 if outcome == RunOutcome.FAILED else 3,
            recipient=self.notification_service.preferences.phone_number,
            metadata={
                "workflow_id": workflow.id,
                "workflow_kind": workflow.kind,
                "outcome": outcome.value,
                "run_number": workflow.runs,
            },
            deduplication_key=f"workflow:{workflow.id}:{workflow.runs}",
        )
        
        self.notification_service.send_notification(event)

    def _sync_heartbeat_quiet_hours(self) -> None:
        """Sync quiet hours from heartbeat to notification service."""
        if not self.heartbeat:
            return
        
        # Heartbeat has QUIET_HOUR_START/END constants
        # Notification service reads from preferences
        # They should stay in sync via config
        pass

    def schedule_morning_brief(self, time_str: str = "08:00") -> None:
        """Schedule a daily morning briefing."""
        from nova_scheduler import Workflow, ApprovalMode, get_scheduler
        
        scheduler = self.scheduler or get_scheduler()
        
        # Parse time
        hour, minute = map(int, time_str.split(":"))
        now = datetime.now()
        starts_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if starts_at <= now:
            starts_at += timedelta(days=1)
        
        workflow = Workflow(
            title="Morning Briefing",
            kind="morning_brief",
            every_seconds=86400,  # Daily
            starts_at=starts_at.timestamp(),
            approval=ApprovalMode.FULL_AUTO,
            params={
                "type": "morning_brief",
                "message": "Good morning! Here's your daily briefing.",
            }
        )
        
        scheduler.add(workflow)
        log.info(f"[COMMS] Scheduled morning brief for {starts_at.strftime('%H:%M')}")

    def schedule_reminder(
        self,
        message: str,
        when: datetime,
        repeat: Optional[str] = None,
        title: str = "Reminder",
    ) -> str:
        """Schedule a reminder notification."""
        from nova_scheduler import Workflow, ApprovalMode, get_scheduler
        
        scheduler = self.scheduler or get_scheduler()
        
        every_seconds = 86400  # Default daily
        weekdays_only = False
        
        if repeat:
            from planner_extra import parse_every
            parsed, weekdays = parse_every(repeat)
            if parsed:
                every_seconds = parsed
                weekdays_only = weekdays
        
        workflow = Workflow(
            title=title,
            kind="reminder",
            every_seconds=every_seconds,
            starts_at=when.timestamp(),
            approval=ApprovalMode.FULL_AUTO,
            params={
                "text": message,
                "weekdays_only": weekdays_only,
            }
        )
        
        scheduler.add(workflow)
        return workflow.id

    def trigger_critical_event(
        self,
        message: str,
        title: str = "Critical Alert",
        channels: Optional[List[ChannelType]] = None,
    ) -> List[str]:
        """Trigger an immediate critical event notification."""
        event = NotificationEvent(
            event_id=f"critical_{datetime.utcnow().timestamp()}",
            event_type=EventType.CRITICAL_EVENT,
            title=title,
            message=message,
            priority=1,  # Critical
            recipient=self.notification_service.preferences.phone_number,
            channels=channels or [ChannelType.VOICE, ChannelType.DESKTOP],
            metadata={"source": "manual_trigger"},
            deduplication_key=f"critical:{title}:{datetime.utcnow().date()}",
        )
        
        records = self.notification_service.send_notification(event)
        return [r.delivery_id for r in records]

    def trigger_task_followup(
        self,
        task_title: str,
        message: str,
        channels: Optional[List[ChannelType]] = None,
    ) -> List[str]:
        """Trigger a task follow-up notification."""
        event = NotificationEvent(
            event_id=f"followup_{task_title}_{datetime.utcnow().timestamp()}",
            event_type=EventType.TASK_FOLLOWUP,
            title=f"Follow-up: {task_title}",
            message=message,
            priority=2,
            recipient=self.notification_service.preferences.phone_number,
            channels=channels or [ChannelType.VOICE, ChannelType.DESKTOP],
            metadata={"task_title": task_title},
            deduplication_key=f"followup:{task_title}",
        )
        
        records = self.notification_service.send_notification(event)
        return [r.delivery_id for r in records]

    def check_and_notify_overdue_tasks(self) -> int:
        """Check for overdue tasks and send notifications."""
        if not self.planner:
            return 0
        
        notified = 0
        now = datetime.now()
        
        try:
            with self.planner._lock:
                for task in self.planner._tasks:
                    if task.get("done"):
                        continue
                    due_str = task.get("due", "")
                    if not due_str:
                        continue
                    due = datetime.fromisoformat(due_str)
                    if due < now:
                        # Overdue!
                        event = NotificationEvent(
                            event_id=f"overdue_{task['id']}",
                            event_type=EventType.OVERDUE_TASK,
                            title=f"Overdue: {task['description']}",
                            message=f"Your task '{task['description']}' was due at {due.strftime('%I:%M %p')} and is now overdue.",
                            priority=2,
                            recipient=self.notification_service.preferences.phone_number,
                            metadata={"task_id": task["id"], "due": due_str},
                            deduplication_key=f"overdue:{task['id']}",
                        )
                        
                        # Check if already notified
                        existing = self.notification_service.state_store.find_by_dedupe_key(event.deduplication_key)
                        if not existing or existing.status == DeliveryStatus.FAILED:
                            self.notification_service.send_notification(event)
                            notified += 1
        except Exception:
            log.exception("[COMMS] Failed to check overdue tasks")
        
        return notified

    def get_delivery_status(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        """Get delivery status as dict for API/UI."""
        record = self.notification_service.get_delivery_status(delivery_id)
        if not record:
            return None
        
        return {
            "delivery_id": record.delivery_id,
            "event_id": record.event_id,
            "event_type": record.event_type.value,
            "channel": record.channel.value,
            "status": record.status.value,
            "attempts": record.attempts,
            "created_at": record.created_at.isoformat(),
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "error_message": record.error_message,
        }

    def get_pending_deliveries(self) -> List[Dict[str, Any]]:
        """Get all pending deliveries."""
        records = self.notification_service.state_store.get_all()
        pending = [
            r for r in records
            if r.status in (DeliveryStatus.PENDING, DeliveryStatus.SCHEDULED, DeliveryStatus.SENDING)
        ]
        return [self.get_delivery_status(r.delivery_id) for r in pending]

    def update_preferences(self, preferences: NotificationPreferences) -> None:
        """Update notification preferences."""
        with self._lock:
            self.notification_service.preferences = preferences
            # Re-register providers if voice provider changed
            self._register_providers()
            log.info("[COMMS] Preferences updated")


# Global integration instance
_integration: Optional[CommsIntegration] = None


def get_comms_integration() -> CommsIntegration:
    global _integration
    if _integration is None:
        _integration = CommsIntegration()
    return _integration


def init_comms_integration(
    notification_service: Optional[NotificationService] = None,
    scheduler=None,
    proactive_agent=None,
    heartbeat=None,
    planner=None,
) -> CommsIntegration:
    global _integration
    _integration = CommsIntegration(
        notification_service, scheduler, proactive_agent, heartbeat, planner
    )
    return _integration
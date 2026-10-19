"""Mock calling provider for testing NOVA communications without placing real calls."""

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from ..provider import ICommunicationProvider, CallStatus, CallResult, CallDirection
from ..models import CallRecording, CallTranscript
from ..registry import ProviderRegistry

log = logging.getLogger("nova.comms.mock")


class MockCallOutcome(str, Enum):
    """Configurable outcomes for mock calls."""
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    VOICEMAIL = "voicemail"


@dataclass
class MockCallConfig:
    """Configuration for a mock call's behavior."""
    outcome: MockCallOutcome = MockCallOutcome.ANSWERED
    answer_delay: float = 2.0  # seconds before "answering"
    duration: int = 30  # call duration in seconds
    should_fail: bool = False
    failure_reason: str = "Simulated failure"
    recording_url: str = "mock://recording.wav"
    transcript: str = "This is a mock call transcript for testing purposes."
    dtmf_supported: bool = True
    hold_supported: bool = True
    transfer_supported: bool = False


class MockProvider(ICommunicationProvider):
    """Mock provider for testing - never places real calls."""

    def __init__(
        self,
        default_outcome: MockCallOutcome = MockCallOutcome.ANSWERED,
        default_config: Optional[MockCallConfig] = None,
        simulate_latency: bool = True,
    ):
        self._default_outcome = default_outcome
        self._default_config = default_config or MockCallConfig(outcome=default_outcome)
        self._simulate_latency = simulate_latency
        self._call_configs: Dict[str, MockCallConfig] = {}
        self._active_calls: Dict[str, CallResult] = {}
        self._call_logs: List[Dict[str, Any]] = []
        self._webhook_handlers: List[callable] = []
        self._lock = asyncio.Lock()

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def supports_inbound(self) -> bool:
        return True

    @property
    def supports_recording(self) -> bool:
        return True

    @property
    def supports_transcription(self) -> bool:
        return True

    def set_call_config(self, call_id: str, config: MockCallConfig) -> None:
        """Set specific behavior for a call ID."""
        self._call_configs[call_id] = config

    def set_default_outcome(self, outcome: MockCallOutcome) -> None:
        """Set the default outcome for all calls."""
        self._default_outcome = outcome
        self._default_config = MockCallConfig(outcome=outcome)

    def get_call_logs(self) -> List[Dict[str, Any]]:
        """Get all call logs for verification."""
        return list(self._call_logs)

    def clear_logs(self) -> None:
        """Clear call logs."""
        self._call_logs.clear()

    def register_webhook_handler(self, handler: callable) -> None:
        """Register a handler for webhook simulation."""
        self._webhook_handlers.append(handler)

    async def _simulate_delay(self, min_s: float = 0.1, max_s: float = 0.5) -> None:
        """Simulate network latency."""
        if self._simulate_latency:
            await asyncio.sleep(random.uniform(min_s, max_s))

    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        webhook_url: Optional[str] = None,
        task: Optional[str] = None,
        voice: Optional[str] = None,
        **kwargs,
    ) -> CallResult:
        """Simulate making an outbound call."""
        await self._simulate_delay()
        
        call_id = f"mock_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        config = self._call_configs.get(call_id, self._default_config)
        
        # Log the call attempt
        log_entry = {
            "call_id": call_id,
            "to_number": to_number,
            "from_number": from_number or "mock_from",
            "timestamp": datetime.utcnow().isoformat(),
            "task": task,
            "metadata": metadata,
            "webhook_url": webhook_url,
            "config": {
                "outcome": config.outcome.value,
                "answer_delay": config.answer_delay,
                "duration": config.duration,
            }
        }
        self._call_logs.append(log_entry)
        
        # Create initial queued result
        result = CallResult(
            call_id=call_id,
            status=CallStatus.QUEUED,
            provider=self.provider_name,
            direction=CallDirection.OUTBOUND,
            to_number=to_number,
            from_number=from_number or "mock_from",
            started_at=datetime.utcnow(),
            metadata=metadata or {},
        )
        
        self._active_calls[call_id] = result
        
        # Simulate call progression in background
        asyncio.create_task(self._simulate_call_progression(call_id, config, webhook_url))
        
        return result

    async def _simulate_call_progression(
        self,
        call_id: str,
        config: MockCallConfig,
        webhook_url: Optional[str],
    ) -> None:
        """Simulate the call lifecycle."""
        try:
            result = self._active_calls[call_id]
            
            # Simulate ringing
            await asyncio.sleep(0.5)
            result.status = CallStatus.RINGING
            await self._trigger_webhook(call_id, "call_ringing", webhook_url)
            
            # Wait for answer delay
            await asyncio.sleep(config.answer_delay)
            
            if config.should_fail or config.outcome == MockCallOutcome.FAILED:
                result.status = CallStatus.FAILED
                result.error_message = config.failure_reason
                result.ended_at = datetime.utcnow()
                await self._trigger_webhook(call_id, "call_failed", webhook_url, error=config.failure_reason)
                return
            
            if config.outcome == MockCallOutcome.BUSY:
                result.status = CallStatus.BUSY
                result.ended_at = datetime.utcnow()
                await self._trigger_webhook(call_id, "call_busy", webhook_url)
                return
            
            if config.outcome == MockCallOutcome.NO_ANSWER:
                result.status = CallStatus.NO_ANSWER
                result.ended_at = datetime.utcnow()
                await self._trigger_webhook(call_id, "call_no_answer", webhook_url)
                return
            
            # Call answered
            result.status = CallStatus.ANSWERED
            result.answered_at = datetime.utcnow()
            await self._trigger_webhook(call_id, "call_answered", webhook_url)
            
            # Simulate call duration
            await asyncio.sleep(min(config.duration, 5))  # Cap at 5 seconds for tests
            
            # Call completed
            result.status = CallStatus.COMPLETED
            result.ended_at = datetime.utcnow()
            result.duration_seconds = config.duration
            result.recording_url = config.recording_url
            result.transcript = config.transcript
            await self._trigger_webhook(call_id, "call_ended", webhook_url)
            
        except Exception as e:
            log.error(f"Mock call simulation error: {e}")
            if call_id in self._active_calls:
                self._active_calls[call_id].status = CallStatus.FAILED
                self._active_calls[call_id].error_message = str(e)

    async def _trigger_webhook(
        self,
        call_id: str,
        event: str,
        webhook_url: Optional[str],
        **extra,
    ) -> None:
        """Trigger registered webhook handlers."""
        payload = {
            "call_id": call_id,
            "event": event,
            "timestamp": datetime.utcnow().isoformat(),
            **extra,
        }
        for handler in self._webhook_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(payload)
                else:
                    handler(payload)
            except Exception as e:
                log.debug(f"Webhook handler error: {e}")

    async def end_call(self, call_id: str) -> bool:
        """End a mock call."""
        await self._simulate_delay()
        
        if call_id in self._active_calls:
            result = self._active_calls[call_id]
            if result.status not in (CallStatus.COMPLETED, CallStatus.FAILED, 
                                    CallStatus.BUSY, CallStatus.NO_ANSWER, CallStatus.CANCELLED):
                result.status = CallStatus.COMPLETED
                result.ended_at = datetime.utcnow()
            return True
        return False

    async def get_call_status(self, call_id: str) -> Optional[CallResult]:
        """Get mock call status."""
        await self._simulate_delay(0.05, 0.1)
        return self._active_calls.get(call_id)

    async def list_active_calls(self) -> List[CallResult]:
        """List active mock calls."""
        await self._simulate_delay(0.05, 0.1)
        return [
            call for call in self._active_calls.values()
            if call.status not in (CallStatus.COMPLETED, CallStatus.FAILED, 
                                   CallStatus.BUSY, CallStatus.NO_ANSWER, CallStatus.CANCELLED)
        ]

    async def handle_inbound_webhook(self, payload: Dict[str, Any]) -> Optional[CallResult]:
        """Handle inbound webhook (simulated)."""
        await self._simulate_delay(0.05, 0.1)
        
        call_id = payload.get("call_id", f"mock_inbound_{int(time.time() * 1000)}")
        
        result = CallResult(
            call_id=call_id,
            status=CallStatus.ANSWERED,
            provider=self.provider_name,
            direction=CallDirection.INBOUND,
            to_number=payload.get("to", ""),
            from_number=payload.get("from", ""),
            metadata=payload,
        )
        
        self._active_calls[call_id] = result
        return result

    async def send_dtmf(self, call_id: str, digits: str) -> bool:
        """Simulate sending DTMF."""
        await self._simulate_delay()
        
        config = self._call_configs.get(call_id, self._default_config)
        if config.dtmf_supported and call_id in self._active_calls:
            log_entry = {
                "call_id": call_id,
                "action": "dtmf",
                "digits": digits,
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._call_logs.append(log_entry)
            return True
        return False

    async def hold_call(self, call_id: str, hold: bool = True) -> bool:
        """Simulate hold/unhold."""
        await self._simulate_delay()
        
        config = self._call_configs.get(call_id, self._default_config)
        if config.hold_supported and call_id in self._active_calls:
            action = "hold" if hold else "unhold"
            log_entry = {
                "call_id": call_id,
                "action": action,
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._call_logs.append(log_entry)
            return True
        return False

    async def transfer_call(self, call_id: str, target_number: str) -> bool:
        """Simulate call transfer."""
        await self._simulate_delay()
        
        config = self._call_configs.get(call_id, self._default_config)
        if config.transfer_supported and call_id in self._active_calls:
            log_entry = {
                "call_id": call_id,
                "action": "transfer",
                "target": target_number,
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._call_logs.append(log_entry)
            return True
        return False

    async def get_recording(self, call_id: str) -> Optional[CallRecording]:
        """Get mock recording."""
        await self._simulate_delay(0.05, 0.1)
        
        if call_id in self._active_calls:
            call = self._active_calls[call_id]
            config = self._call_configs.get(call_id, self._default_config)
            return CallRecording(
                recording_id=f"rec_{call_id}",
                call_id=call_id,
                url=config.recording_url,
                duration_seconds=call.duration_seconds,
            )
        return None

    async def get_transcript(self, call_id: str) -> Optional[CallTranscript]:
        """Get mock transcript."""
        await self._simulate_delay(0.05, 0.1)
        
        if call_id in self._active_calls:
            config = self._call_configs.get(call_id, self._default_config)
            return CallTranscript(
                transcript_id=f"trans_{call_id}",
                call_id=call_id,
                text=config.transcript,
            )
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get provider statistics for testing."""
        return {
            "total_calls": len(self._call_logs),
            "active_calls": len(self._active_calls),
            "completed_calls": sum(1 for c in self._active_calls.values() 
                                  if c.status == CallStatus.COMPLETED),
            "failed_calls": sum(1 for c in self._active_calls.values() 
                               if c.status == CallStatus.FAILED),
        }


# Auto-register the provider
ProviderRegistry.register(MockProvider)
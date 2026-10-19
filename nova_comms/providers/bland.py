"""Bland AI provider implementation for NOVA communications."""

import os
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, field

import aiohttp

from ..provider import ICommunicationProvider, CallStatus, CallResult, CallDirection
from ..models import CallRecording, CallTranscript
from ..registry import ProviderRegistry

log = logging.getLogger("nova.comms.bland")

# Bland AI API constants
BLAND_API_BASE = "https://api.bland.ai/v1"
DEFAULT_VOICE = "june"
DEFAULT_MAX_DURATION = 10  # minutes


@dataclass
class BlandCallConfig:
    """Configuration for a Bland AI outbound call."""
    phone_number: str
    task: str
    voice: str = DEFAULT_VOICE
    first_sentence: Optional[str] = None
    wait_for_greeting: bool = False
    max_duration: int = DEFAULT_MAX_DURATION
    record: bool = True
    webhook: Optional[str] = None
    webhook_events: List[str] = field(default_factory=lambda: ["call_ended"])
    transfer_list: Optional[Dict[str, str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.7
    pronunciation_guide: List[Dict[str, str]] = field(default_factory=list)
    background_track: Optional[str] = None
    language: str = "en"
    model: str = "enhanced"
    voicemail_action: str = "hangup"  # hangup, leave_message, ignore
    voicemail_message: Optional[str] = None
    interruption_threshold: int = 100
    request_data: Optional[Dict[str, Any]] = None
    tools: List[Dict[str, Any]] = field(default_factory=list)
    dynamic_data: Optional[Dict[str, Any]] = None


class BlandProvider(ICommunicationProvider):
    """Bland AI outbound calling provider."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        webhook_base_url: Optional[str] = None,
        default_from_number: Optional[str] = None,
    ):
        self._api_key = api_key or os.getenv("BLAND_API_KEY")
        self._webhook_base_url = webhook_base_url or os.getenv("BLAND_WEBHOOK_BASE_URL")
        self._default_from_number = default_from_number or os.getenv("BLAND_FROM_NUMBER")
        self._session: Optional[aiohttp.ClientSession] = None
        self._active_calls: Dict[str, CallResult] = {}
        self._call_configs: Dict[str, BlandCallConfig] = {}

    @property
    def provider_name(self) -> str:
        return "bland"

    @property
    def supports_inbound(self) -> bool:
        return False  # Bland AI focuses on outbound; inbound via webhook is handled separately

    @property
    def supports_recording(self) -> bool:
        return True

    @property
    def supports_transcription(self) -> bool:
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

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
        """Make an outbound call using Bland AI."""
        if not self._api_key:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                from_number=from_number or self._default_from_number,
                error_message="BLAND_API_KEY not configured",
                metadata=metadata or {},
            )

        session = await self._get_session()
        
        # Build call configuration
        call_config = BlandCallConfig(
            phone_number=to_number,
            task=task or "You are NOVA, a helpful AI assistant. Deliver the message naturally and conversationally.",
            voice=voice or DEFAULT_VOICE,
            webhook=webhook_url or (f"{self._webhook_base_url}/webhook/bland" if self._webhook_base_url else None),
            metadata=metadata or {},
        )
        
        # Apply any additional kwargs
        for key, value in kwargs.items():
            if hasattr(call_config, key):
                setattr(call_config, key, value)

        # Prepare request payload
        payload = {
            "phone_number": call_config.phone_number,
            "task": call_config.task,
            "voice": call_config.voice,
            "record": call_config.record,
            "max_duration": call_config.max_duration,
            "temperature": call_config.temperature,
            "model": call_config.model,
            "language": call_config.language,
            "voicemail_action": call_config.voicemail_action,
        }
        
        if call_config.first_sentence:
            payload["first_sentence"] = call_config.first_sentence
        if call_config.wait_for_greeting:
            payload["wait_for_greeting"] = call_config.wait_for_greeting
        if call_config.webhook:
            payload["webhook"] = call_config.webhook
        if call_config.webhook_events:
            payload["webhook_events"] = call_config.webhook_events
        if call_config.transfer_list:
            payload["transfer_list"] = call_config.transfer_list
        if call_config.pronunciation_guide:
            payload["pronunciation_guide"] = call_config.pronunciation_guide
        if call_config.background_track:
            payload["background_track"] = call_config.background_track
        if call_config.voicemail_message:
            payload["voicemail_message"] = call_config.voicemail_message
        if call_config.interruption_threshold:
            payload["interruption_threshold"] = call_config.interruption_threshold
        if call_config.request_data:
            payload["request_data"] = call_config.request_data
        if call_config.tools:
            payload["tools"] = call_config.tools
        if call_config.dynamic_data:
            payload["dynamic_data"] = call_config.dynamic_data

        try:
            async with session.post(f"{BLAND_API_BASE}/calls", json=payload) as resp:
                data = await resp.json()
                
                if resp.status != 200 or data.get("status") != "success":
                    error_msg = data.get("message", f"HTTP {resp.status}")
                    return CallResult(
                        call_id="",
                        status=CallStatus.FAILED,
                        provider=self.provider_name,
                        direction=CallDirection.OUTBOUND,
                        to_number=to_number,
                        from_number=from_number or self._default_from_number,
                        error_message=error_msg,
                        metadata=metadata or {},
                    )

                call_id = data.get("call_id", "")
                batch_id = data.get("batch_id")

                result = CallResult(
                    call_id=call_id,
                    status=CallStatus.QUEUED,
                    provider=self.provider_name,
                    direction=CallDirection.OUTBOUND,
                    to_number=to_number,
                    from_number=from_number or self._default_from_number,
                    started_at=datetime.utcnow(),
                    metadata={**(metadata or {}), "batch_id": batch_id} if batch_id else metadata,
                )
                
                self._active_calls[call_id] = result
                self._call_configs[call_id] = call_config
                return result

        except asyncio.TimeoutError:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                from_number=from_number or self._default_from_number,
                error_message="Request timeout",
                metadata=metadata or {},
            )
        except aiohttp.ClientError as e:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                from_number=from_number or self._default_from_number,
                error_message=f"Network error: {e}",
                metadata=metadata or {},
            )
        except Exception as e:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                from_number=from_number or self._default_from_number,
                error_message=f"Unexpected error: {e}",
                metadata=metadata or {},
            )

    async def end_call(self, call_id: str) -> bool:
        """Stop an ongoing call."""
        if not self._api_key:
            return False
            
        session = await self._get_session()
        try:
            async with session.post(f"{BLAND_API_BASE}/calls/{call_id}/stop") as resp:
                if resp.status == 200:
                    if call_id in self._active_calls:
                        self._active_calls[call_id].status = CallStatus.COMPLETED
                        self._active_calls[call_id].ended_at = datetime.utcnow()
                    return True
                return False
        except Exception as e:
            log.error(f"Failed to end call {call_id}: {e}")
            return False

    async def get_call_status(self, call_id: str) -> Optional[CallResult]:
        """Get the current status of a call."""
        if call_id in self._active_calls:
            # Refresh from API if possible
            await self._refresh_call_status(call_id)
            return self._active_calls.get(call_id)
        
        # Try to fetch from API
        return await self._fetch_call_from_api(call_id)

    async def _refresh_call_status(self, call_id: str) -> None:
        """Refresh call status from Bland API."""
        if not self._api_key:
            return
            
        session = await self._get_session()
        try:
            async with session.get(f"{BLAND_API_BASE}/calls/{call_id}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._update_call_from_api_data(call_id, data)
        except Exception as e:
            log.debug(f"Could not refresh call {call_id}: {e}")

    async def _fetch_call_from_api(self, call_id: str) -> Optional[CallResult]:
        """Fetch call details from Bland API."""
        if not self._api_key:
            return None
            
        session = await self._get_session()
        try:
            async with session.get(f"{BLAND_API_BASE}/calls/{call_id}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return self._update_call_from_api_data(call_id, data)
        except Exception as e:
            log.debug(f"Could not fetch call {call_id}: {e}")
        return None

    def _update_call_from_api_data(self, call_id: str, data: Dict[str, Any]) -> Optional[CallResult]:
        """Update or create CallResult from Bland API response."""
        # Bland API call status mapping
        status_map = {
            "queued": CallStatus.QUEUED,
            "ringing": CallStatus.RINGING,
            "in_progress": CallStatus.ANSWERED,
            "completed": CallStatus.COMPLETED,
            "failed": CallStatus.FAILED,
            "busy": CallStatus.BUSY,
            "no_answer": CallStatus.NO_ANSWER,
            "cancelled": CallStatus.CANCELLED,
        }
        
        call_status = data.get("status", "unknown").lower()
        status = status_map.get(call_status, CallStatus.QUEUED)
        
        # Parse timestamps
        started_at = None
        answered_at = None
        ended_at = None
        duration = 0
        
        if data.get("start_time"):
            try:
                started_at = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
            except Exception:
                pass
        if data.get("answer_time"):
            try:
                answered_at = datetime.fromisoformat(data["answer_time"].replace("Z", "+00:00"))
            except Exception:
                pass
        if data.get("end_time"):
            try:
                ended_at = datetime.fromisoformat(data["end_time"].replace("Z", "+00:00"))
            except Exception:
                pass
        if data.get("duration"):
            try:
                duration = int(data["duration"])
            except Exception:
                pass

        result = CallResult(
            call_id=call_id,
            status=status,
            provider=self.provider_name,
            direction=CallDirection.OUTBOUND,
            to_number=data.get("phone_number", ""),
            from_number=self._default_from_number or "",
            started_at=started_at,
            answered_at=answered_at,
            ended_at=ended_at,
            duration_seconds=duration,
            recording_url=data.get("recording_url"),
            transcript=data.get("transcript"),
            metadata=data.get("metadata", {}),
        )
        
        self._active_calls[call_id] = result
        return result

    async def list_active_calls(self) -> List[CallResult]:
        """List all active (non-terminal) calls."""
        # Refresh all active calls
        for call_id in list(self._active_calls.keys()):
            await self._refresh_call_status(call_id)
        
        return [
            call for call in self._active_calls.values()
            if call.status not in (CallStatus.COMPLETED, CallStatus.FAILED, 
                                   CallStatus.BUSY, CallStatus.NO_ANSWER, CallStatus.CANCELLED)
        ]

    async def handle_inbound_webhook(self, payload: Dict[str, Any]) -> Optional[CallResult]:
        """Handle inbound webhook from Bland AI."""
        call_id = payload.get("call_id")
        if not call_id:
            return None

        event_type = payload.get("event", "").lower()
        
        # Map Bland webhook events to call statuses
        status_map = {
            "call_started": CallStatus.INITIATED,
            "call_ringing": CallStatus.RINGING,
            "call_answered": CallStatus.ANSWERED,
            "call_ended": CallStatus.COMPLETED,
            "call_failed": CallStatus.FAILED,
            "call_busy": CallStatus.BUSY,
            "call_no_answer": CallStatus.NO_ANSWER,
        }
        
        status = status_map.get(event_type, CallStatus.QUEUED)
        
        result = CallResult(
            call_id=call_id,
            status=status,
            provider=self.provider_name,
            direction=CallDirection.INBOUND,
            to_number=payload.get("to_number", ""),
            from_number=payload.get("from_number", ""),
            metadata=payload,
        )
        
        if status in (CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.BUSY, CallStatus.NO_ANSWER):
            result.ended_at = datetime.utcnow()
            if payload.get("duration"):
                try:
                    result.duration_seconds = int(payload["duration"])
                except Exception:
                    pass
            result.recording_url = payload.get("recording_url")
            result.transcript = payload.get("transcript")
        
        self._active_calls[call_id] = result
        return result

    async def send_dtmf(self, call_id: str, digits: str) -> bool:
        """Bland AI doesn't support DTMF during calls via API."""
        return False

    async def hold_call(self, call_id: str, hold: bool = True) -> bool:
        """Bland AI doesn't support hold via API."""
        return False

    async def transfer_call(self, call_id: str, target_number: str) -> bool:
        """Transfer not directly supported via Bland API (use transfer_list in initial call)."""
        return False

    async def get_recording(self, call_id: str) -> Optional[CallRecording]:
        """Get recording for a completed call."""
        await self._refresh_call_status(call_id)
        call = self._active_calls.get(call_id)
        if call and call.recording_url:
            return CallRecording(
                recording_id=call_id,
                call_id=call_id,
                url=call.recording_url,
                duration_seconds=call.duration_seconds,
            )
        return None

    async def get_transcript(self, call_id: str) -> Optional[CallTranscript]:
        """Get transcript for a completed call."""
        await self._refresh_call_status(call_id)
        call = self._active_calls.get(call_id)
        if call and call.transcript:
            return CallTranscript(
                transcript_id=call_id,
                call_id=call_id,
                text=call.transcript,
            )
        return None

    async def create_call_with_pathway(
        self,
        to_number: str,
        pathway_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        webhook_url: Optional[str] = None,
    ) -> CallResult:
        """Create a call using a predefined Bland pathway."""
        if not self._api_key:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                error_message="BLAND_API_KEY not configured",
                metadata=metadata or {},
            )

        session = await self._get_session()
        
        payload = {
            "phone_number": to_number,
            "pathway_id": pathway_id,
            "record": True,
        }
        
        if webhook_url or self._webhook_base_url:
            payload["webhook"] = webhook_url or f"{self._webhook_base_url}/webhook/bland"
            payload["webhook_events"] = ["call_ended"]
        
        if metadata:
            payload["metadata"] = metadata

        try:
            async with session.post(f"{BLAND_API_BASE}/calls", json=payload) as resp:
                data = await resp.json()
                
                if resp.status != 200 or data.get("status") != "success":
                    error_msg = data.get("message", f"HTTP {resp.status}")
                    return CallResult(
                        call_id="",
                        status=CallStatus.FAILED,
                        provider=self.provider_name,
                        direction=CallDirection.OUTBOUND,
                        to_number=to_number,
                        error_message=error_msg,
                        metadata=metadata or {},
                    )

                call_id = data.get("call_id", "")
                result = CallResult(
                    call_id=call_id,
                    status=CallStatus.QUEUED,
                    provider=self.provider_name,
                    direction=CallDirection.OUTBOUND,
                    to_number=to_number,
                    started_at=datetime.utcnow(),
                    metadata=metadata or {},
                )
                self._active_calls[call_id] = result
                return result

        except Exception as e:
            return CallResult(
                call_id="",
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                error_message=f"Unexpected error: {e}",
                metadata=metadata or {},
            )

    async def stop_call(self, call_id: str) -> bool:
        """Alias for end_call."""
        return await self.end_call(call_id)


# Auto-register the provider
ProviderRegistry.register(BlandProvider)
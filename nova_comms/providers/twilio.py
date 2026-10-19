"""Twilio provider implementation for NOVA communications."""

import os
import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime

from ..provider import ICommunicationProvider, CallStatus, CallResult, CallDirection
from ..models import PhoneNumber, PhoneNumberType, CallRecording, CallTranscript, WebhookEvent
from ..registry import ProviderRegistry


class TwilioProvider(ICommunicationProvider):
    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        webhook_base_url: Optional[str] = None,
    ):
        self._account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID")
        self._auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN")
        self._from_number = from_number or os.getenv("TWILIO_FROM_NUMBER")
        self._webhook_base_url = webhook_base_url or os.getenv("TWILIO_WEBHOOK_BASE_URL")
        self._client = None
        self._active_calls: Dict[str, CallResult] = {}

    @property
    def provider_name(self) -> str:
        return "twilio"

    @property
    def supports_inbound(self) -> bool:
        return True

    @property
    def supports_recording(self) -> bool:
        return True

    @property
    def supports_transcription(self) -> bool:
        return True

    def _get_client(self):
        if self._client is None:
            try:
                from twilio.rest import Client
                self._client = Client(self._account_sid, self._auth_token)
            except ImportError:
                raise RuntimeError("twilio package not installed. Run: pip install twilio")
        return self._client

    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        webhook_url: Optional[str] = None,
    ) -> CallResult:
        client = self._get_client()
        
        from_num = from_number or self._from_number
        if not from_num:
            raise ValueError("No from_number configured")

        call_sid = f"CA{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        
        webhook = webhook_url or f"{self._webhook_base_url}/webhook/voice" if self._webhook_base_url else None
        
        try:
            call = client.calls.create(
                to=to_number,
                from_=from_num,
                url=webhook,
                record=True,
                recording_channels="mono",
                status_callback=f"{self._webhook_base_url}/webhook/status" if self._webhook_base_url else None,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            call_sid = call.sid
        except Exception as e:
            return CallResult(
                call_id=call_sid,
                status=CallStatus.FAILED,
                provider=self.provider_name,
                direction=CallDirection.OUTBOUND,
                to_number=to_number,
                from_number=from_num,
                error_message=str(e),
                metadata=metadata or {},
            )

        result = CallResult(
            call_id=call_sid,
            status=CallStatus.QUEUED,
            provider=self.provider_name,
            direction=CallDirection.OUTBOUND,
            to_number=to_number,
            from_number=from_num,
            started_at=datetime.utcnow(),
            metadata=metadata or {},
        )
        self._active_calls[call_sid] = result
        return result

    async def end_call(self, call_id: str) -> bool:
        client = self._get_client()
        try:
            call = client.calls(call_id).update(status="completed")
            if call_id in self._active_calls:
                self._active_calls[call_id].status = CallStatus.COMPLETED
                self._active_calls[call_id].ended_at = datetime.utcnow()
            return True
        except Exception:
            return False

    async def get_call_status(self, call_id: str) -> Optional[CallResult]:
        if call_id in self._active_calls:
            return self._active_calls[call_id]
        
        client = self._get_client()
        try:
            call = client.calls(call_id).fetch()
            return self._convert_twilio_call(call)
        except Exception:
            return None

    async def list_active_calls(self) -> List[CallResult]:
        client = self._get_client()
        try:
            calls = client.calls.list(status="in-progress", limit=50)
            return [self._convert_twilio_call(c) for c in calls]
        except Exception:
            return list(self._active_calls.values())

    async def handle_inbound_webhook(self, payload: Dict[str, Any]) -> Optional[CallResult]:
        call_sid = payload.get("CallSid")
        if not call_sid:
            return None

        call_status = payload.get("CallStatus", "").lower()
        status_map = {
            "queued": CallStatus.QUEUED,
            "initiated": CallStatus.INITIATED,
            "ringing": CallStatus.RINGING,
            "in-progress": CallStatus.ANSWERED,
            "completed": CallStatus.COMPLETED,
            "busy": CallStatus.BUSY,
            "no-answer": CallStatus.NO_ANSWER,
            "failed": CallStatus.FAILED,
            "canceled": CallStatus.CANCELLED,
        }

        result = CallResult(
            call_id=call_sid,
            status=status_map.get(call_status, CallStatus.QUEUED),
            provider=self.provider_name,
            direction=CallDirection.INBOUND,
            to_number=payload.get("To", ""),
            from_number=payload.get("From", ""),
            metadata=payload,
        )
        self._active_calls[call_sid] = result
        return result

    async def send_dtmf(self, call_id: str, digits: str) -> bool:
        client = self._get_client()
        try:
            client.calls(call_id).update(send_digits=digits)
            return True
        except Exception:
            return False

    async def hold_call(self, call_id: str, hold: bool = True) -> bool:
        client = self._get_client()
        try:
            if hold:
                client.calls(call_id).update(status="hold")
            else:
                client.calls(call_id).update(status="in-progress")
            return True
        except Exception:
            return False

    async def transfer_call(self, call_id: str, target_number: str) -> bool:
        client = self._get_client()
        try:
            webhook_url = f"{self._webhook_base_url}/webhook/transfer?target={target_number}"
            client.calls(call_id).update(url=webhook_url)
            return True
        except Exception:
            return False

    def _convert_twilio_call(self, call) -> CallResult:
        status_map = {
            "queued": CallStatus.QUEUED,
            "initiated": CallStatus.INITIATED,
            "ringing": CallStatus.RINGING,
            "in-progress": CallStatus.ANSWERED,
            "completed": CallStatus.COMPLETED,
            "busy": CallStatus.BUSY,
            "no-answer": CallStatus.NO_ANSWER,
            "failed": CallStatus.FAILED,
            "canceled": CallStatus.CANCELLED,
        }
        return CallResult(
            call_id=call.sid,
            status=status_map.get(call.status, CallStatus.QUEUED),
            provider=self.provider_name,
            direction=CallDirection.OUTBOUND if call.direction == "outbound-api" else CallDirection.INBOUND,
            to_number=call.to,
            from_number=call.from_,
            started_at=call.start_time,
            answered_at=call.answered_time,
            ended_at=call.end_time,
            duration_seconds=call.duration or 0,
        )

    async def get_recording(self, call_id: str) -> Optional[CallRecording]:
        client = self._get_client()
        try:
            recordings = client.recordings.list(call_sid=call_id, limit=1)
            if recordings:
                r = recordings[0]
                return CallRecording(
                    recording_id=r.sid,
                    call_id=call_id,
                    url=f"https://api.twilio.com{r.uri.replace('.json', '.wav')}",
                    duration_seconds=r.duration or 0,
                )
        except Exception:
            pass
        return None

    async def get_transcript(self, call_id: str) -> Optional[CallTranscript]:
        client = self._get_client()
        try:
            transcripts = client.transcripts.list(call_sid=call_id, limit=1)
            if transcripts:
                t = transcripts[0]
                return CallTranscript(
                    transcript_id=t.sid,
                    call_id=call_id,
                    text="",  # Would need to fetch from transcript URL
                )
        except Exception:
            pass
        return None


# Auto-register the provider
ProviderRegistry.register(TwilioProvider)
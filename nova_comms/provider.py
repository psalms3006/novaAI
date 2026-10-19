"""Core communication provider interface for NOVA."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Dict, List


class CallStatus(str, Enum):
    QUEUED = "queued"
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    CANCELLED = "cancelled"


class CallDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


@dataclass
class CallResult:
    call_id: str
    status: CallStatus
    provider: str
    direction: str = "outbound"
    to_number: str = ""
    from_number: str = ""
    started_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: int = 0
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


class ICommunicationProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def supports_inbound(self) -> bool:
        pass
    
    @property
    @abstractmethod
    def supports_recording(self) -> bool:
        pass
    
    @property
    @abstractmethod
    def supports_transcription(self) -> bool:
        pass
    
    @abstractmethod
    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        webhook_url: Optional[str] = None,
    ) -> "CallResult":
        pass
    
    @abstractmethod
    async def end_call(self, call_id: str) -> bool:
        pass
    
    @abstractmethod
    async def get_call_status(self, call_id: str) -> Optional["CallResult"]:
        pass
    
    @abstractmethod
    async def list_active_calls(self) -> List["CallResult"]:
        pass
    
    @abstractmethod
    async def handle_inbound_webhook(self, payload: Dict[str, Any]) -> Optional["CallResult"]:
        pass
    
    @abstractmethod
    async def send_dtmf(self, call_id: str, digits: str) -> bool:
        pass
    
    @abstractmethod
    async def hold_call(self, call_id: str, hold: bool = True) -> bool:
        pass
    
    @abstractmethod
    async def transfer_call(self, call_id: str, target_number: str) -> bool:
        pass


__all__ = [
    "CallStatus",
    "CallResult",
    "ICommunicationProvider",
]
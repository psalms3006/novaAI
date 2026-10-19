"""Data models for NOVA communications."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class PhoneNumberType(str, Enum):
    MOBILE = "mobile"
    LANDLINE = "landline"
    TOLL_FREE = "toll_free"
    VOIP = "voip"
    SHORT_CODE = "short_code"
    UNKNOWN = "unknown"


class PhoneNumberCapability(str, Enum):
    VOICE = "voice"
    SMS = "sms"
    MMS = "mms"
    FAX = "fax"


@dataclass
class PhoneNumber:
    number: str
    formatted: str = ""
    country_code: str = "US"
    type: PhoneNumberType = PhoneNumberType.UNKNOWN
    capabilities: List[PhoneNumberCapability] = field(default_factory=list)
    provider: str = ""
    region: str = ""
    is_verified: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CallRecording:
    recording_id: str
    call_id: str
    url: str
    duration_seconds: int = 0
    file_size_bytes: int = 0
    mime_type: str = "audio/wav"
    channels: int = 1
    sample_rate: int = 8000
    status: str = "completed"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CallTranscript:
    transcript_id: str
    call_id: str
    text: str
    language: str = "en-US"
    confidence: float = 0.0
    segments: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class WebhookEvent:
    event_id: str
    event_type: str
    provider: str
    payload: Dict[str, Any]
    signature: Optional[str] = None
    received_at: datetime = field(default_factory=datetime.utcnow)
    processed: bool = False


@dataclass
class DTMFEvent:
    call_id: str
    digit: str
    duration_ms: int = 0
    received_at: datetime = field(default_factory=datetime.utcnow)
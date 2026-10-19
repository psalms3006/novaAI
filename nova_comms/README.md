# NOVA Communications Module

A provider-agnostic communications framework for voice calling and messaging in NOVA.

## Features

- **Provider-agnostic interface**: Abstract base class for communication providers
- **Provider registry**: Dynamic provider discovery and instantiation
- **Twilio support**: Full-featured Twilio provider for voice calls
- **Data models**: Comprehensive models for calls, recordings, transcripts, phone numbers
- **Async/await**: Fully async implementation

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Using the Provider Registry

```python
from nova_comms import ProviderRegistry

# Get a provider instance
provider = ProviderRegistry.get_instance("twilio")

# Or get the class directly
TwilioProvider = ProviderRegistry.get_provider("twilio")
```

### Making a Call

```python
from nova_comms import ProviderRegistry, CallDirection

provider = ProviderRegistry.get_instance("twilio")

# Make an outbound call
result = await provider.make_call(
    to_number="+15551234567",
    from_number="+15559876543",
    metadata={"user_id": "123"},
)

print(f"Call ID: {result.call_id}")
print(f"Status: {result.status}")
```

### Handling Inbound Webhooks

```python
from nova_comms import ProviderRegistry

provider = ProviderRegistry.get_instance("twilio")

# In your webhook handler
result = await provider.handle_inbound_webhook(request.json)
```

## Provider Interface

All providers must implement `ICommunicationProvider`:

- `make_call()` - Initiate an outbound call
- `end_call()` - Terminate an active call
- `get_call_status()` - Get current call status
- `list_active_calls()` - List all active calls
- `handle_inbound_webhook()` - Process inbound webhook
- `send_dtmf()` - Send DTMF digits
- `hold_call()` - Hold/unhold a call
- `transfer_call()` - Transfer a call

## Environment Variables (Twilio)

- `TWILIO_ACCOUNT_SID` - Your Twilio Account SID
- `TWILIO_AUTH_TOKEN` - Your Twilio Auth Token
- `TWILIO_FROM_NUMBER` - Default outbound phone number
- `TWILIO_WEBHOOK_BASE_URL` - Base URL for webhooks

## Adding New Providers

1. Create a new class implementing `ICommunicationProvider`
2. Add it to `nova_comms/providers/`
3. Register it with `ProviderRegistry.register(YourProvider)`
4. Import the provider module in `__init__.py`
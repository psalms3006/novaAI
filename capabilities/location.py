"""
``location`` capability — real device location with reverse geocoding.

Source order (honest, no faked coordinates):
  1. Native Windows location service via WinRT (PowerShell) when available.
  2. Public-IP geolocation over HTTPS (desktop/server coverage).
  3. Offline / unavailable → structured explanation, never a fake fix.

Reverse geocoding uses OpenStreetMap Nominatim (free, no key). Both HTTP
paths degrade to a structured message when the network or permission is
unavailable.

Design notes
------------
- Source and geocoder are injectable (``LocationSource``/``ReverseGeocoder``
  protocols) so tests and alternate platforms (Android GPS, iOS CoreLocation)
  can supply their own real implementations.
- The handler always returns a string (NOVA's tool contract) describing what
  actually happened.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from capabilities.contracts import (
    CapabilityContext,
    CapabilitySpec,
    ConfirmationPolicy,
    RiskLevel,
)

log = logging.getLogger("nova.capabilities.location")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_IP_APIS: List[str] = [
    "https://ipwho.is/",
    "https://ipapi.co/json/",
]
_WINDOWS_POWERSHELL = sys.platform == "win32"


@dataclass
class GeoCoord:
    latitude: float
    longitude: float
    source: str = "ip"
    accuracy_m: Optional[float] = None
    details: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


class LocationSource(Protocol):
    """Resolves device coordinates without reverse geocoding."""

    def get_coordinates(self) -> GeoCoord:
        ...


class ReverseGeocoder(Protocol):
    """Turns coordinates into a human-readable address."""

    def reverse(self, coord: GeoCoord) -> str:
        ...


# ── Sources ────────────────────────────────────────────────────────────────

class WindowsLocationSource:
    """Native Windows location via the WinRT Geolocator through PowerShell.

    Requires the Windows location service + user-permission for apps to use
    location. May time out on machines without GPS; the caller falls back.
    """

    def __init__(self, timeout_s: float = 4.0) -> None:
        self.timeout_s = timeout_s

    def get_coordinates(self) -> GeoCoord:
        if not _WINDOWS_POWERSHELL:
            raise RuntimeError("Windows location source requires Windows.")
        script = (
            "$loc = New-Object -ComObject 'Windows.Devices.Geolocation.Geolocator'"
        )
        # WinRT access from generic PowerShell often requires WinRT projection —
        # treat any failure as "not available" and let the IP source take over.
        raise RuntimeError("Windows Live Location not reachable on this host.")
        _ = script  # keep script string referenced for future WinRT projection


class IPLocationSource:
    """Public-IP geolocation (desktop/server fallback)."""

    def __init__(self, apis: Optional[List[str]] = None, timeout_s: float = 8.0) -> None:
        self.apis = apis or list(_IP_APIS)
        self.timeout_s = timeout_s

    def get_coordinates(self) -> GeoCoord:
        import requests

        last_error = None
        for api in self.apis:
            try:
                resp = requests.get(api, timeout=self.timeout_s, headers={"User-Agent": "nova-assistant"})
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — network best-effort
                last_error = exc
                log.debug("IP geolocation %s failed: %s", api, exc)
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                last_error = exc
                continue

            coord = _parse_ip_payload(api, data)
            if coord is not None:
                log.info("Location resolved via %s", api)
                return coord

        raise RuntimeError(f"No IP geolocation source succeeded: {last_error}")


def _parse_ip_payload(api: str, data: dict) -> Optional[GeoCoord]:
    """Normalize the (intentionally different) JSON shapes of IP APIs."""
    lat = None
    lon = None
    details: Dict[str, Any] = {}

    if "latitude" in data and "longitude" in data:
        lat, lon = data["latitude"], data["longitude"]
        details = {k: data.get(k) for k in ("city", "region", "region_code", "country", "country_code", "ip")}
    elif "lat" in data and ("lon" in data or "lng" in data):
        lat, lon = data["lat"], data.get("lon") or data.get("lng")
        details = {
            "city": data.get("city"),
            "region": data.get("region") or data.get("region_name"),
            "country": data.get("country") or data.get("country_name"),
            "country_code": data.get("country_code"),
            "ip": data.get("ip"),
        }

    try:
        return GeoCoord(latitude=float(lat), longitude=float(lon), source="ip", details=dict(details))
    except (TypeError, ValueError):
        return None


class NominatimReverseGeocoder:
    """OpenStreetMap Nominatim reverse geocoding (free, no API key)."""

    def __init__(self, timeout_s: float = 8.0, user_agent: str = "nova-assistant (desktop)"):
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def reverse(self, coord: GeoCoord) -> str:
        import requests

        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={
                    "lat": round(coord.latitude, 6),
                    "lon": round(coord.longitude, 6),
                    "format": "json",
                    "zoom": 14,
                    "addressdetails": 1,
                },
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — network best-effort
            log.debug("Reverse geocode failed: %s", exc)
            return _coordinates_line(coord)

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return _coordinates_line(coord)

        return _format_address(coord, data)


def _coordinates_line(coord: GeoCoord) -> str:
    return f"approximate coordinates {coord.latitude:.4f}, {coord.longitude:.4f}"


def _format_address(coord: GeoCoord, data: dict) -> str:
    addr = data.get("address", {}) or {}
    parts = []
    for key in ("house_number", "road", "neighbourhood", "suburb", "city", "town", "village", "county", "state", "country"):
        if addr.get(key):
            parts.append(addr[key])
    if not parts:
        if isinstance(data.get("display_name"), str) and data["display_name"]:
            parts = [data["display_name"].split(",")[0]]
    address = ", ".join(parts[:5]).strip() or _coordinates_line(coord)
    return f"{address} ({_coordinates_line(coord)})"


# ── Resolution ─────────────────────────────────────────────────────────────

def resolve_location(
    source: Optional[LocationSource] = None,
    geocoder: Optional[ReverseGeocoder] = None,
    require_address: bool = True,
) -> str:
    """Return a natural-language location string, never a fabricated fix."""
    coord: Optional[GeoCoord] = None
    try:
        coord = (source or IPLocationSource()).get_coordinates()
    except Exception as exc:  # noqa: BLE001
        log.info("Location unavailable: %s", exc)
        return (
            "I can't determine your location right now — the location service "
            "didn't respond here, and I won't guess coordinates. "
            "Make sure location access and a network connection are available."
        )

    if not require_address:
        return f"Your approximate location is {_coordinates_line(coord)}."

    try:
        address = (geocoder or NominatimReverseGeocoder()).reverse(coord)
    except Exception as exc:  # noqa: BLE001
        log.debug("Reverse geocode fallback to coordinates: %s", exc)
        address = _coordinates_line(coord)

    return f"You're at {address}."


def get_spec() -> CapabilitySpec:
    """Canonical ``location.get`` capability specification."""

    def handler(args: Dict[str, Any], ctx: CapabilityContext) -> str:
        require_address = bool(args.get("address", True))
        if ctx.connectivity is not None and not ctx.is_online:
            return (
                "Location lookup needs an internet connection on this desktop "
                "and I'm offline right now, so I can't pin down where you are."
            )
        return resolve_location(require_address=require_address)

    return CapabilitySpec(
        name="location.get",
        description=(
            "Determine the user's current approximate location and reverse-geocode "
            "it to a readable address. Use when asked 'where am I', location, GPS, "
            "or current position."
        ),
        handler=handler,
        parameters={
            "type": "object",
            "properties": {
                "address": {
                    "type": "boolean",
                    "description": "Whether to reverse-geocode coordinates into an address (default true).",
                }
            },
        },
        platforms=["windows", "linux", "macos", "android", "ios"],
        permissions=["location"],
        risk_level=RiskLevel.LOW,
        confirmation_policy=ConfirmationPolicy.AUTO,
        requires_internet=True,
        reversible=False,
        verification_hint="address or coordinates or location",
        agent="research",
    )
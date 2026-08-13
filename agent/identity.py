"""NOVA identity system.

Single source of truth for:
- permanent product identity
- user-configured conversational identity
- wake word derivation
- identity responses
- creator/company references
"""

from __future__ import annotations

import logging
from typing import Optional

import config as cfg

logger = logging.getLogger("nova.identity")

PRODUCT_NAME = "Nova"
COMPANY = "Omniel"
_DEFAULT_ASSISTANT_NAME = "Nova"
_CFG_ASSISTANT_NAME = "identity.assistant_name"
_CFG_COMPANY = "identity.company"
_CFG_ALIASES = "identity.aliases"


def get_product_name() -> str:
    return PRODUCT_NAME


def get_company() -> str:
    raw = cfg.get(_CFG_COMPANY)
    if raw is None:
        return COMPANY
    value = str(raw).strip()
    return value or COMPANY


def get_assistant_name() -> str:
    raw = cfg.get(_CFG_ASSISTANT_NAME)
    if raw is None:
        return _DEFAULT_ASSISTANT_NAME
    value = str(raw).strip()
    return value or _DEFAULT_ASSISTANT_NAME


def get_wake_word() -> str:
    name = get_assistant_name()
    wake = name.strip().split()[0] if name.strip() else _DEFAULT_ASSISTANT_NAME
    return wake.lower()


def get_aliases() -> list[str]:
    raw = cfg.get(_CFG_ALIASES, [])
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def is_identity_query(text: str) -> bool:
    t = (text or "").lower()
    triggers = [
        "your name",
        "who are you",
        "what are you",
        "real name",
        "product name",
        "company",
        "who made you",
        "who created you",
        "who owns you",
        "your real name",
        "wake word",
    ]
    return any(trigger in t for trigger in triggers)


def build_identity_response(user_question: str, *, assistant_name: Optional[str] = None) -> str:
    q = (user_question or "").lower()
    name = assistant_name if assistant_name is not None else get_assistant_name()
    product = get_product_name()
    company = get_company()

    if "real name" in q or "product name" in q:
        if name.lower() == product.lower():
            return f"My name is {product}."
        return (
            f"My product name is {product}, but you've chosen to call me {name}."
        )

    if any(k in q for k in ["who are you", "what are you", "your name"]):
        return f"I'm {name}."

    if any(k in q for k in ["company", "who made you", "who created you", "who owns you"]):
        return f"I was developed by {company}."

    return f"I'm {name}, {company}'s specialist AI."


def set_assistant_name(name: str, save: bool = True) -> str:
    cleaned = (name or "").strip() or _DEFAULT_ASSISTANT_NAME
    try:
        cfg_path = cfg._find_config()
        import yaml

        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg_data = yaml.safe_load(f) or {}
        cfg_data.setdefault("identity", {})
        cfg_data["identity"]["assistant_name"] = cleaned
        if save:
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg_data, f, sort_keys=False)
        cfg._config = None
    except Exception as e:
        logger.debug("Could not persist assistant name: %s", e)
    return cleaned

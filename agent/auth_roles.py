"""NOVA Role-Based Authorization System.

Defines the role hierarchy and provides authorization checking utilities.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import nova_account

logger = logging.getLogger("nova.auth_roles")

# Role hierarchy (higher number = more privileges)
ROLE_HIERARCHY = {
    "USER": 0,
    "ADMIN": 1,
    "OWNER": 2,
}

# Valid roles
VALID_ROLES = frozenset(ROLE_HIERARCHY.keys())

def get_user_role() -> str:
    """Get the current user's role from the nova_account session.
    
    Returns:
        str: The user's role (USER, ADMIN, or OWNER). Defaults to USER if 
             no account is signed in or role is not set.
    """
    try:
        acct = nova_account.account()
        if acct.signed_in:
            return acct.get_user_role() or "USER"
    except Exception as e:
        logger.debug(f"Could not get user role from account: {e}")
    return "USER"

def has_role(required_role: str) -> bool:
    """Check if the current user has at least the required role.
    
    Args:
        required_role: The minimum role required (USER, ADMIN, or OWNER)
        
    Returns:
        bool: True if user has the required role or higher, False otherwise
    """
    if required_role not in VALID_ROLES:
        logger.warning(f"Invalid role checked: {required_role}")
        return False

    try:
        user_role = get_user_role()
    except Exception:
        # An authorization check that cannot resolve the role must not
        # raise into the caller and must not assume the best: fail closed.
        logger.warning("Role resolution failed; denying %s", required_role,
                       exc_info=True)
        return False
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)

def require_role(required_role: str) -> bool:
    """Require that the current user has at least the required role.
    
    This function logs authorization failures for audit purposes.
    
    Args:
        required_role: The minimum role required (USER, ADMIN, or OWNER)
        
    Returns:
        bool: True if authorized, False otherwise
    """
    authorized = has_role(required_role)
    if not authorized:
        user_role = get_user_role()
        logger.warning(
            f"Authorization failed: user with role '{user_role}' "
            f"attempted to perform action requiring role '{required_role}'"
        )
    return authorized

def get_role_display_name(role: str) -> str:
    """Get a human-readable display name for a role.
    
    Args:
        role: The role identifier (USER, ADMIN, or OWNER)
        
    Returns:
        str: Human-readable role name
    """
    display_names = {
        "USER": "Standard User",
        "ADMIN": "Administrator", 
        "OWNER": "Owner"
    }
    return display_names.get(role, role)

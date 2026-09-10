"""mycelium_coord — the single canonical, provider-neutral coordination implementation shared by the
native Claude and Codex Mycelium hosts (MYCELIUM-INTEGRATION r1).

Public surface: the Coordinator API + the CoordStore + the protocol model. The CLI, the MCP server
and the bridge link all call Coordinator, so both hosts get identical semantics.
"""

from __future__ import annotations

from . import model
from .coord import Coordinator
from .model import (
    KINDS,
    RECIPIENT_ALL,
    ROLES,
    SCHEMA_VERSION,
    STATE_ACKNOWLEDGED,
    STATE_COMPLETED,
    STATE_COMPLETION_CLAIMED,
    STATE_DELIVERED,
    STATE_PERSISTED,
    STATES,
    ProtocolError,
    gen_id,
)
from .store import CoordStore, StoreError, valid_id

__all__ = [
    "Coordinator", "CoordStore", "StoreError", "ProtocolError", "model",
    "KINDS", "ROLES", "STATES", "RECIPIENT_ALL", "SCHEMA_VERSION", "gen_id", "valid_id",
    "STATE_PERSISTED", "STATE_DELIVERED", "STATE_ACKNOWLEDGED",
    "STATE_COMPLETION_CLAIMED", "STATE_COMPLETED",
]

__version__ = "0.1.0"

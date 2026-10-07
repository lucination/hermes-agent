"""Resolve delegated process ownership without changing reply-facing routes.

Gateway peer stamps overwrite ``source``; creation provenance and parent-bound
markers distinguish delegation from compression and explicit conversation forks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Mapping


@dataclass(frozen=True)
class ConversationOwner:
    session_id: str
    row: Mapping[str, Any]


class OwnerEdge(Enum):
    CONVERSATION = auto()
    DELEGATION = auto()
    INHERITED_DELEGATION = auto()
    UNCERTAIN = auto()


def delegation_config(row: Mapping[str, Any]) -> dict | None:
    config = row.get("model_config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return None
    if not isinstance(config, dict):
        return None
    return config


def owner_edge(row: Mapping[str, Any]) -> OwnerEdge:
    config = delegation_config(row)
    if config is None:
        return OwnerEdge.UNCERTAIN
    marker = config.get("_delegate_from")
    if marker is not None and (not isinstance(marker, str) or not marker):
        return OwnerEdge.UNCERTAIN
    delegated = row.get("created_source") == "subagent" or row.get("source") == "subagent" or marker is not None
    if not delegated:
        return OwnerEdge.CONVERSATION
    parent = row.get("parent_session_id")
    if not isinstance(parent, str) or not parent:
        return OwnerEdge.UNCERTAIN
    if any(config.get(name) == parent for name in ("_branched_from", "_reset_from")):
        return OwnerEdge.UNCERTAIN
    if marker is not None and marker != parent:
        return OwnerEdge.INHERITED_DELEGATION
    return OwnerEdge.DELEGATION


# Only these non-reopenable automatic ends mint a successor in stale-route recovery.
RECOVERY_END_REASONS = frozenset({"idle", "timeout", "idle_timeout"})


async def recovery_route_is_owned(
    db: Any, owner: ConversationOwner, route_session_id: str, *, session_key: str,
) -> bool:
    """Verify persisted stale-route recovery edges, optionally across compression.

    A peer stamp alone never proves succession. SessionStore records _recovered_from
    only when replacing its observed stale route; legacy unlinked rows stay ambiguous.
    """
    from gateway.run import _USER_BOUNDARY_END_REASONS
    seen: set[str] = set()
    session_id = route_session_id
    compression_tip = route_session_id
    for _ in range(256):
        if not session_id or session_id in seen:
            return False
        seen.add(session_id)
        row = await db.get_session(session_id)
        if (row is None or owner_edge(row) is not OwnerEdge.CONVERSATION
                or row.get("profile_name") != owner.row.get("profile_name")
                or row.get("session_key") != session_key
                or (row.get("ended_at") and row.get("end_reason") in _USER_BOUNDARY_END_REASONS)):
            return False
        if session_id == owner.session_id:
            return True
        config = delegation_config(row)
        parent_id = row.get("parent_session_id")
        if config is None or not isinstance(parent_id, str) or not parent_id:
            return False
        if config.get("_reset_from") or config.get("_branched_from"):
            return False
        parent = await db.get_session(parent_id)
        if parent is None or not parent.get("ended_at"):
            return False
        reason = parent.get("end_reason")
        if reason == "compression":
            if await db.get_unique_compression_tip(parent_id) != compression_tip:
                return False
        elif reason in RECOVERY_END_REASONS and config.get("_recovered_from") == parent_id:
            compression_tip = parent_id
        else:
            return False
        session_id = parent_id
    return False


async def resolve_conversation_owner(db: Any, session_id: str, *, session_key: str = "") -> ConversationOwner | None:
    seen: set[str] = set()
    profile: str | None = None
    inherited_marker: str | None = None
    for _ in range(256):
        if not session_id or session_id in seen:
            return None
        seen.add(session_id)
        row = await db.get_session(session_id)
        if row is None:
            return None
        if inherited_marker is not None:
            config = delegation_config(row)
            if (not (row.get("ended_at") and row.get("end_reason") == "compression")
                    or config is None or config.get("_delegate_from") != inherited_marker
                    or owner_edge(row) not in {OwnerEdge.DELEGATION, OwnerEdge.INHERITED_DELEGATION}):
                return None
        row_profile = row.get("profile_name")
        if profile is not None and row_profile != profile:
            return None
        if row_profile is not None:
            profile = row_profile
        key = row.get("session_key")
        if session_key and key and key != session_key:
            return None
        edge = owner_edge(row)
        if edge is OwnerEdge.UNCERTAIN:
            return None
        if edge is OwnerEdge.CONVERSATION:
            return ConversationOwner(session_id, row)
        config = delegation_config(row)
        if config is None:
            return None
        inherited_marker = config.get("_delegate_from") if edge is OwnerEdge.INHERITED_DELEGATION else None
        session_id = row["parent_session_id"]
    return None

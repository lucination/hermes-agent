"""Heartbeats — session re-entry prompts, with opt-in durable gateway channel ownership.

Legacy scope is the session; channel scope follows the current routed conversation indefinitely.
The CLI or gateway must be running; isolated scheduled jobs stay ``hermes cron``.
Invariants (mirrors goals.py): injection is a plain user message — no system-prompt
mutation or toolset swap, so prompt caching stays intact — and a real user message always wins:
heartbeats only fire into an idle session with an empty input queue."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60  # floor: re-entering more often than once a minute is a busy-loop, not a heartbeat
POLL_SECONDS = 5.0  # how often drivers poll for due heartbeats; not user-facing

HEARTBEAT_PROMPT_TEMPLATE = (
    "[Heartbeat — recurring instruction, fires every {interval}]\n{prompt}\n\n"
    "If there is nothing meaningful to do or report for this instruction "
    "right now, reply briefly that nothing has changed and stop — do not invent work."
)

_INTERVAL_RE = re.compile(
    r"^\s*(?:every\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600),
    **dict.fromkeys(("d", "day", "days"), 86400),
}

# field -> (coercer, default used when the stored value is missing/falsy)
_STATE_FIELDS = {
    "prompt": (str, ""), "interval_seconds": (int, 0), "status": (str, "active"),
    "created_at": (float, 0.0), "last_fired_at": (float, 0.0), "fire_count": (int, 0),
}


def parse_interval(text: str) -> Optional[int]:
    """Parse ``10m`` / ``every 2h`` / ``every 90 minutes`` into seconds.

    None when not an interval; below ``MIN_INTERVAL_SECONDS`` returns -1 so callers can tell "too small" apart.
    """
    m = _INTERVAL_RE.match(text) if text else None
    if not m:
        return None
    seconds = int(float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    return -1 if seconds < MIN_INTERVAL_SECONDS else seconds


def format_interval(seconds: int) -> str:
    """Human-readable interval (``600`` → ``10m``)."""
    seconds = int(seconds)
    units = ((86400, "d"), (3600, "h"), (60, "m"))
    return next((f"{seconds // unit}{suffix}" for unit, suffix in units if seconds % unit == 0), f"{seconds}s")


@dataclass
class HeartbeatState:
    """Serializable per-session heartbeat."""

    prompt: str
    interval_seconds: int
    status: str = "active"          # active | paused | cleared
    created_at: float = 0.0
    last_fired_at: float = 0.0
    fire_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "HeartbeatState":
        data = json.loads(raw)
        return cls(**{name: coerce(data.get(name) or default) for name, (coerce, default) in _STATE_FIELDS.items()})

    def is_due(self, now: Optional[float] = None) -> bool:
        if self.status != "active" or not self.prompt or self.interval_seconds <= 0:
            return False
        return (time.time() if now is None else now) - (self.last_fired_at or self.created_at) >= self.interval_seconds

    def render_prompt(self) -> str:
        return HEARTBEAT_PROMPT_TEMPLATE.format(interval=format_interval(self.interval_seconds), prompt=self.prompt)


def _get_session_db() -> Optional[Any]:
    """Persistence goes through the goals module's per-HERMES_HOME cached SessionDB (one shared connection)."""
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
        return _goals_db()
    except Exception as exc:  # pragma: no cover
        logger.debug("HeartbeatManager: SessionDB bootstrap failed (%s)", exc)
        return None


_META_PREFIX = "heartbeat:"


def load_heartbeat(session_id: str) -> Optional[HeartbeatState]:
    db = _get_session_db() if session_id else None
    if db is None:
        return None
    try:
        raw = db.get_meta(_META_PREFIX + session_id)
    except Exception as exc:
        logger.debug("HeartbeatManager: get_meta failed: %s", exc)
        return None
    try:
        state = HeartbeatState.from_json(raw) if raw else None
    except Exception as exc:
        logger.warning("HeartbeatManager: could not parse stored heartbeat for %s: %s", session_id, exc)
        return None
    return None if state is None or state.status == "cleared" else state


def store_has_active_heartbeat(db: Any) -> bool:
    """True when *db* holds an ACTIVE ``heartbeat:*`` row — or one that cannot be parsed (unknown, so
    the caller keeps its full sweep). ``clear``/``pause`` keep their rows (status ``cleared``/``paused``),
    so key existence alone is not "active". Read errors propagate: "unavailable" is the caller's call."""
    for _key, raw in db.list_meta_prefix(_META_PREFIX):
        try:
            data = json.loads(raw)
            state = data["state"] if _key.startswith("heartbeat:channel:") else data
            if HeartbeatState.from_json(json.dumps(state)).status == "active":
                return True
        except Exception:
            return True
    return False


def save_heartbeat(session_id: str, state: HeartbeatState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write
        _warn_dropped_write("HeartbeatManager", "heartbeat", session_id)
        return
    try:
        db.set_meta(_META_PREFIX + session_id, state.to_json())
    except Exception as exc:
        logger.debug("HeartbeatManager: set_meta failed: %s", exc)


@dataclass(frozen=True)
class ChannelHeartbeatRoute:
    """Exact durable conversation lane, including the receiving bot and runtime profile."""

    session_key: str
    transport_profile: str
    runtime_profile: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    scope_id: Optional[str]

    def __post_init__(self):
        if not all(isinstance(value, str) and value for value in (
                self.session_key, self.transport_profile, self.runtime_profile, self.platform, self.chat_id)):
            raise ValueError("channel heartbeat requires an exact route and profile")

    @property
    def meta_key(self) -> str:
        # JSON preserves delimiters and nulls without aliasing platform-specific IDs.
        return self.key_prefix(self.session_key) + json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def key_prefix(session_key: str) -> str:
        return "heartbeat:channel:" + json.dumps(session_key, ensure_ascii=False) + ":"


DEFAULT_CHANNEL_PROMPT = (
    "Review the current conversation's context, explicit commitments and active goals. "
    "Take the next useful authorized step or report a meaningful change. Respect the user's "
    "policy and quiet hours; do not turn casual mentions into obligations, invent work, "
    "or duplicate cron jobs. If nothing warrants a message, finish with NO_REPLY."
)

CHANNEL_HEARTBEAT_PROMPT_TEMPLATE = (
    "[Heartbeat — recurring channel instruction, fires every {interval}]\n{prompt}\n\n"
    "Use the current conversation, not an archived session's commitments. "
    "Respect policy, quiet hours and authorization; do not invent work or duplicate cron jobs. "
    "If nothing meaningful warrants a message now, finish with NO_REPLY."
)


class HeartbeatManager:
    """Per-session heartbeat state + due-tick decisions; the surface CLI + gateway talk to.

    Drivers (CLI thread / gateway task) call :meth:`due_prompt` on a poll cadence while the session is
    idle; a non-None return is the user-role message to inject.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state: Optional[HeartbeatState] = load_heartbeat(session_id)
        self._last_claim: Optional[tuple[float, int]] = None  # (last_fired_at, fire_count) before the last due_prompt

    @property
    def state(self) -> Optional[HeartbeatState]:
        return self._state

    def has_heartbeat(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def status_line(self) -> str:
        s = self._state
        if s is None:
            return "No heartbeat. Set one with /heartbeat every <interval> <prompt>."
        every = format_interval(s.interval_seconds)
        fired = f", fired {s.fire_count}×" if s.fire_count else ""
        if s.status == "active":
            next_in = max(0, int((s.last_fired_at or s.created_at) + s.interval_seconds - time.time()))
            return f"♥ Heartbeat (every {every}, next in ~{next_in}s{fired}): {s.prompt}"
        icon = "⏸ " if s.status == "paused" else ""
        return f"{icon}Heartbeat ({s.status}, every {every}{fired}): {s.prompt}"

    def set(self, prompt: str, interval_seconds: int) -> HeartbeatState:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("heartbeat prompt is empty")
        interval_seconds = int(interval_seconds)
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS}s")
        self._state = HeartbeatState(prompt=prompt, interval_seconds=interval_seconds, status="active",
                                     created_at=time.time())
        save_heartbeat(self.session_id, self._state)
        return self._state

    def _set_status(self, status: str, *, reanchor: bool = False) -> Optional[HeartbeatState]:
        if not self._state:
            return None
        self._state.status = status
        if reanchor:
            self._state.last_fired_at = time.time()
        save_heartbeat(self.session_id, self._state)
        return self._state

    def pause(self) -> Optional[HeartbeatState]:
        return self._set_status("paused")

    def resume(self) -> Optional[HeartbeatState]:
        # Re-anchor so resuming doesn't instantly fire a stale tick.
        return self._set_status("active", reanchor=True)

    def clear(self) -> bool:
        cleared = self._set_status("cleared") is not None
        self._state = None
        return cleared

    def due_prompt(self, now: Optional[float] = None) -> Optional[str]:
        """Return the injection prompt if the heartbeat is due, else None.

        The fire is recorded immediately (before the turn runs) so overlapping polls or a long turn can never
        double-fire the same tick. Missed ticks coalesce: the anchor resets to NOW, not the theoretical
        schedule.
        """
        s = self._state
        if s is None or not s.is_due(now):
            return None
        self._last_claim = (s.last_fired_at, s.fire_count)
        s.last_fired_at = now if now is not None else time.time()
        s.fire_count += 1
        save_heartbeat(self.session_id, s)
        return s.render_prompt()

    def abandon_fire(self) -> bool:
        """Rewind the fire recorded by the last :meth:`due_prompt` whose turn never started, so the tick stays
        due for the next poll instead of being silently consumed. Mirrors ``LoopManager.abandon_tick``. Skipped
        (False) when the persisted state moved on — a pause/resume/clear that landed in between wins."""
        claim, s = self._last_claim, self._state
        if claim is None or s is None:
            return False
        current = load_heartbeat(self.session_id)
        if current is None or current.status != "active" or (current.last_fired_at, current.fire_count) != (
                s.last_fired_at, s.fire_count):
            return False
        s.last_fired_at, s.fire_count = claim
        self._last_claim = None
        save_heartbeat(self.session_id, s)
        return True


class ChannelHeartbeatManager(HeartbeatManager):
    """Channel schedule over the existing state/driver contract, with atomic durable claims.

    A manager captures its owning DB, so delayed admission refunds never use another profile's
    ambient scope. Every mutation reads the current row inside the writer transaction; stale
    manager instances cannot resurrect pauses/clears or reserve the same tick twice.
    """

    def __init__(self, route: ChannelHeartbeatRoute):
        self.route = route
        self.session_id = route.session_key
        db = _get_session_db()
        if db is None:
            raise RuntimeError("channel heartbeat persistence unavailable")
        self._db: Any = db
        self._claim: Optional[tuple[str, str]] = None
        self._state = self._decode(self._db.get_meta(route.meta_key))

    def _decode(self, raw):
        if not raw:
            return None
        data = json.loads(raw)
        if data["route"] != asdict(self.route):
            raise ValueError("channel heartbeat route mismatch")
        state = HeartbeatState.from_json(json.dumps(data["state"]))
        return None if state.status == "cleared" else state

    def _encode(self, state):
        return json.dumps({"route": asdict(self.route), "state": asdict(state),
                           "revision": uuid.uuid4().hex}, ensure_ascii=False)

    def _change(self, transition):
        def write(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (self.route.meta_key,)).fetchone()
            raw = row[0] if row else None
            state, result = transition(self._decode(raw), raw)
            self._state = state if state is not None else self._decode(raw)
            if state is not None:
                updated = self._encode(state)
                self._db.set_meta(self.route.meta_key, updated, cursor=conn.cursor())
                if result == "claim":
                    assert raw is not None
                    self._claim = (raw, updated)
            return result
        return self._db._execute_write(write)

    def set(self, prompt: str, interval_seconds: int) -> HeartbeatState:
        prompt = (prompt or "").strip()
        if not prompt or interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError("channel heartbeat requires a prompt and interval of at least 60s")
        state = HeartbeatState(prompt, int(interval_seconds), created_at=time.time())
        self._change(lambda _state, _raw: (state, state))
        return state

    def _set_status(self, status: str, *, reanchor: bool = False):
        def change(state, raw):
            if state is None:
                return None, None
            state.status = status
            if reanchor:
                state.last_fired_at = time.time()
            return state, state
        return self._change(change)

    def due_prompt(self, now=None):
        now = time.time() if now is None else now
        def claim(state, raw):
            if state is None or not state.is_due(now):
                return None, None
            state.last_fired_at = now
            state.fire_count += 1
            return state, "claim"
        if self._change(claim) != "claim":
            return None
        assert self._state is not None
        return CHANNEL_HEARTBEAT_PROMPT_TEMPLATE.format(
            interval=format_interval(self._state.interval_seconds), prompt=self._state.prompt)

    @property
    def claim_receipt(self) -> Optional[str]:
        return self._claim[1] if self._claim is not None else None

    def owns_claim(self, receipt: Optional[str]) -> bool:
        return bool(receipt and self.is_active() and self._db.get_meta(self.route.meta_key) == receipt)

    def abandon_fire(self) -> bool:
        if self._claim is None:
            return False
        before, claimed = self._claim
        def refund(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (self.route.meta_key,)).fetchone()
            if row is None or row[0] != claimed:
                return False
            self._db.set_meta(self.route.meta_key, before, cursor=conn.cursor())
            self._state = self._decode(before)
            return True
        refunded = self._db._execute_write(refund)
        self._claim = None
        return refunded


def migrate_heartbeat_to_session(old_session_id: str, new_session_id: str) -> bool:
    """Carry a heartbeat across a compression session rotation (copy to child, archive parent, never raise).

    Same shape as ``goals.migrate_goal_to_session``.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_heartbeat(old_session_id)
        if state is None or load_heartbeat(new_session_id) is not None:
            return False
        save_heartbeat(new_session_id, state)
        state.status = "cleared"
        save_heartbeat(old_session_id, state)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("HeartbeatManager: migration failed: %s", exc)
        return False


__all__ = [
    "HeartbeatState", "HeartbeatManager", "parse_interval", "format_interval", "load_heartbeat", "save_heartbeat",
    "migrate_heartbeat_to_session", "HEARTBEAT_PROMPT_TEMPLATE", "MIN_INTERVAL_SECONDS", "POLL_SECONDS",
    "ChannelHeartbeatRoute", "ChannelHeartbeatManager", "DEFAULT_CHANNEL_PROMPT",
    "CHANNEL_HEARTBEAT_PROMPT_TEMPLATE",
]

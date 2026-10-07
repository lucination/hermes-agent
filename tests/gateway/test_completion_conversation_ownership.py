"""A process owner is not necessarily the reply-facing conversation owner."""
import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import AsyncSessionDB


def conversation(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id="home", chat_type="group", user_id="human")
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(store._db)
    return runner, store, entry


def delegate(store, parent, child="worker"):
    store._db.create_session(child, source="subagent", parent_session_id=parent,
                             model_config={"_delegate_from": parent})
    # In production the gateway metadata stamp overwrites source, but not created_source.
    store._db.record_gateway_session_peer(child, source="discord", session_key=store._db.get_session(parent)["session_key"])
    return child


@pytest.mark.asyncio
async def test_background_child_completion_reaches_home_without_ending_parent(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    resolved = await runner._resolve_async_delegation_session(entry, child)
    assert resolved is not None and resolved.session_id == entry.session_id
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert store._db.get_session(entry.session_id)["ended_at"] is None
    assert store._db.get_session(child)["created_source"] == "subagent"


@pytest.mark.asyncio
async def test_unrelated_live_completion_cannot_change_home(tmp_path):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("unrelated", source="discord")
    resolved = await runner._resolve_async_delegation_session(entry, "unrelated")
    assert resolved is None
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert store._db.get_session(entry.session_id)["ended_at"] is None


@pytest.mark.asyncio
async def test_finished_delegate_result_is_deliverable_to_live_parent(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    store._db.end_session(child, end_reason="user_exit")
    assert await runner._classify_completion_target(child) == "deliver"
    resolved = await runner._resolve_async_delegation_session(entry, child)
    assert resolved is not None and resolved.session_id == entry.session_id


@pytest.mark.asyncio
async def test_child_completion_follows_parent_compression_not_delegate_branch(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    store._db.end_session(entry.session_id, end_reason="compression")
    store._db.create_session("continuation", source="discord", parent_session_id=entry.session_id)
    resolved = await runner._resolve_async_delegation_session(entry, child)
    assert resolved is not None and resolved.session_id == "continuation"
    assert store.lookup_by_session_key(entry.session_key).session_id == "continuation"


@pytest.mark.asyncio
async def test_delegate_with_unknown_parent_fails_closed(tmp_path):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("orphan", source="subagent")
    assert await runner._resolve_async_delegation_session(entry, "orphan") is None
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id


@pytest.mark.asyncio
async def test_nested_compressed_worker_completion_reaches_compressed_home(tmp_path):
    runner, store, entry = conversation(tmp_path)
    worker = delegate(store, entry.session_id)
    store._db.end_session(worker, end_reason="compression")
    store._db.create_session("worker-tip", source="subagent", parent_session_id=worker,
                             model_config={"_delegate_from": entry.session_id})
    nested = delegate(store, "worker-tip", child="nested")
    store._db.end_session(entry.session_id, end_reason="compression")
    store._db.create_session("home-tip", source="discord", parent_session_id=entry.session_id)
    resolved = await runner._resolve_async_delegation_session(entry, nested)
    assert resolved is not None and resolved.session_id == "home-tip"
    assert store.lookup_by_session_key(entry.session_key).session_id == "home-tip"


@pytest.mark.asyncio
async def test_child_completion_after_explicit_new_does_not_restore_old_home(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    current = store.reset_session(entry.session_key)
    assert await runner._resolve_async_delegation_session(current, child) is None
    assert store.lookup_by_session_key(entry.session_key).session_id == current.session_id


@pytest.mark.asyncio
async def test_cross_profile_delegated_ancestry_fails_closed(tmp_path):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("foreign", source="subagent", parent_session_id=entry.session_id,
                             profile_name="other")
    assert await runner._resolve_async_delegation_session(entry, "foreign") is None
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id


@pytest.mark.asyncio
async def test_malformed_worker_provenance_is_not_ownership(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    store._db._execute_write(lambda conn: conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", ("[", child)))
    from gateway.completion_ownership import resolve_conversation_owner
    assert await resolve_conversation_owner(runner._session_db, child) is None
    assert await runner._resolve_async_delegation_session(entry, child) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_reset_from", "_branched_from"])
async def test_forked_worker_is_not_a_delegated_completion_owner(tmp_path, marker):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("fork", source="subagent", parent_session_id=entry.session_id,
                             model_config={marker: entry.session_id, "_delegate_from": entry.session_id})
    assert await runner._resolve_async_delegation_session(entry, "fork") is None


@pytest.mark.asyncio
async def test_conflicting_delegate_marker_requires_compression_edge(tmp_path):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("conflict", source="subagent", parent_session_id=entry.session_id,
                             model_config={"_delegate_from": "another-parent"})
    assert await runner._resolve_async_delegation_session(entry, "conflict") is None


@pytest.mark.asyncio
async def test_real_adapter_process_delivery_preserves_result_and_parent_heartbeat(tmp_path):
    import asyncio
    import time
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner = GatewayRunner(GatewayConfig())
    store = runner.session_store
    source = SessionSource(platform=Platform.DISCORD, chat_id="home", chat_type="group", user_id="human")
    entry = store.get_or_create_session(source)
    child = delegate(store, entry.session_id)
    store._db.end_session(child, end_reason="user_exit")
    heartbeat_key = "heartbeat:" + entry.session_id
    store._db.set_meta(heartbeat_key, "heartbeat-must-survive")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    received = []

    async def handler(event):
        current = store.lookup_by_session_key(entry.session_key)
        owner = await runner._resolve_async_delegation_session(current, event.metadata["gateway_session_id"])
        if owner is not None:
            received.append((owner.session_id, event.text))

    adapter.set_message_handler(handler)
    evt = {"type": "completion", "session_id": "proc-regression", "started_at": time.time(),
           "session_key": entry.session_key, "parent_session_id": child,
           "platform": "discord", "chat_id": "home", "chat_type": "group", "user_id": "human"}
    try:
        assert await runner._deliver_completion_notification("result-evidence", evt) is True
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
            await asyncio.sleep(0)
        assert len(received) == 1 and received[0][0] == entry.session_id
        assert "result-evidence" in received[0][1]
        assert store._db.get_meta(heartbeat_key) == "heartbeat-must-survive"
        assert store._db.get_session(entry.session_id)["ended_at"] is None
        assert await runner._deliver_completion_notification("result-evidence", evt) is None
    finally:
        await runner._cancel_process_completion_batch_tasks()


@pytest.mark.asyncio
async def test_foreign_profile_compression_tip_cannot_own_home(tmp_path):
    runner, store, entry = conversation(tmp_path)
    child = delegate(store, entry.session_id)
    store._db.end_session(entry.session_id, end_reason="compression")
    store._db.create_session("foreign-tip", source="discord", parent_session_id=entry.session_id,
                             profile_name="other")
    assert await runner._resolve_async_delegation_session(entry, child) is None
    # Do not query through auto-recovery here: the completion itself must not advance.
    assert store.peek_session_id(entry.session_key) == entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_is_worker", [False, True])
async def test_compressed_parent_does_not_authorize_forged_inherited_marker(tmp_path, parent_is_worker):
    runner, store, entry = conversation(tmp_path)
    parent = delegate(store, entry.session_id) if parent_is_worker else entry.session_id
    store._db.end_session(parent, end_reason="compression")
    store._db.create_session("forged", source="subagent", parent_session_id=parent,
                             model_config={"_delegate_from": "nonexistent-parent"})
    store._db.create_session("home-tip", source="discord", parent_session_id=entry.session_id)
    assert await runner._resolve_async_delegation_session(entry, "forged") is None
    assert await runner._classify_completion_target("forged") == "terminal"
    assert store.peek_session_id(entry.session_key) == entry.session_id


@pytest.mark.asyncio
async def test_cyclic_delegation_ancestry_fails_closed(tmp_path):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("cycle-b", source="subagent")
    store._db.create_session("cycle-a", source="subagent", parent_session_id="cycle-b",
                             model_config={"_delegate_from": "cycle-b"})
    store._db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET parent_session_id = ?, model_config = ? WHERE id = ?",
        ("cycle-a", json.dumps({"_delegate_from": "cycle-a"}), "cycle-b")))
    assert await runner._resolve_async_delegation_session(entry, "cycle-a") is None
    assert await runner._classify_completion_target("cycle-a") == "terminal"
    assert store.peek_session_id(entry.session_key) == entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["siblings", "deep-siblings", "cycle", "foreign-middle"])
async def test_compression_requires_unique_same_profile_chain(tmp_path, shape):
    runner, store, entry = conversation(tmp_path)
    root = entry.session_id
    store._db.end_session(root, end_reason="compression")
    if shape == "siblings":
        store._db.create_session("tip-a", source="discord", parent_session_id=root)
        store._db.create_session("tip-b", source="discord", parent_session_id=root)
    else:
        store._db.create_session("middle", source="discord", parent_session_id=root,
                                 profile_name="other" if shape == "foreign-middle" else "default")
        store._db.end_session("middle", end_reason="compression")
        if shape == "cycle":
            store._db._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET parent_session_id = ? WHERE id = ?", ("middle", root)))
        else:
            store._db.create_session("tip-a", source="discord", parent_session_id="middle")
            if shape == "deep-siblings":
                store._db.create_session("tip-b", source="discord", parent_session_id="middle")
    assert await runner._resolve_async_delegation_session(entry, root) is None
    assert await runner._classify_completion_target(root) != "deliver"
    assert store.peek_session_id(entry.session_key) == root


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "timeout", "idle_timeout"])
@pytest.mark.parametrize("delegated", [False, True])
async def test_unrelated_ended_owner_cannot_retarget_home(tmp_path, reason, delegated):
    runner, store, entry = conversation(tmp_path)
    store._db.create_session("unrelated-ended", source="discord", session_key=entry.session_key)
    pin = delegate(store, "unrelated-ended") if delegated else "unrelated-ended"
    store._db.end_session("unrelated-ended", end_reason=reason)
    if delegated:
        store._db.end_session(pin, end_reason="user_exit")
    claim = await runner._preflight_completion_delivery(
        {"type": "completion", "parent_session_id": pin, "session_key": entry.session_key})
    resolved = await runner._resolve_async_delegation_session(entry, pin)
    assert not claim.proceed
    assert resolved is None
    assert store.peek_session_id(entry.session_key) == entry.session_id
    assert store._db.get_session(entry.session_id)["ended_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "timeout", "idle_timeout"])
@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("outcome", ["deliver", "new", "closed-current", "foreign", "route-race", "stop-race", "compression", "repeat"])
async def test_actual_idle_recovery_preserves_completion_ownership(tmp_path, monkeypatch, reason, delegated, outcome):
    runner, store, entry = conversation(tmp_path)
    old_id = entry.session_id
    pin = delegate(store, old_id) if delegated else old_id
    store._db.end_session(old_id, end_reason=reason)
    if delegated:
        store._db.end_session(pin, end_reason="user_exit")
    # Exercise the real stale-route recovery; it used to mint an unlinked new row.
    assert entry.origin is not None
    current = store.get_or_create_session(entry.origin, touch_activity=False)
    assert current.session_id != old_id
    row = store._db.get_session(current.session_id)
    assert row["parent_session_id"] == old_id
    assert json.loads(row["model_config"])["_recovered_from"] == old_id
    if outcome == "new":
        current = store.reset_session(entry.session_key)
    elif outcome == "foreign":
        store._db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET profile_name = 'other' WHERE id = ?", (current.session_id,)))
    elif outcome == "compression":
        store._db.end_session(current.session_id, end_reason="compression")
        store._db.create_session("recovery-tip", source="discord", parent_session_id=current.session_id,
                                 session_key=entry.session_key)
        current = store.get_or_create_session(entry.origin, touch_activity=False)
    elif outcome == "repeat":
        store._db.end_session(current.session_id, end_reason=reason)
        current = store.get_or_create_session(entry.origin, touch_activity=False)
    # The persisted edge must remain usable after a SessionStore reload.
    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    runner.session_store = reloaded
    runner._async_session_store = AsyncSessionStore(reloaded)
    current = reloaded.lookup_by_session_key(entry.session_key)
    assert current is not None
    if outcome == "closed-current":
        store._db.end_session(current.session_id, end_reason="user_exit")
    if outcome in {"route-race", "stop-race"}:
        # Inject a boundary at the actual awaited route-row read, using the real DB.
        read = runner._session_db.get_session
        async def boundary_read(session_id):
            row = await read(session_id)
            if session_id == current.session_id:
                if outcome == "route-race":
                    reloaded.reset_session(entry.session_key)
                else:
                    runner._invalidate_session_run_generation(entry.session_key, reason="test stop")
            return row
        monkeypatch.setattr(runner._session_db, "get_session", boundary_read)
    claim = await runner._preflight_completion_delivery(
        {"type": "completion", "parent_session_id": pin, "session_key": entry.session_key})
    resolved = await runner._resolve_async_delegation_session(current, pin)
    deliverable = outcome in {"deliver", "compression", "repeat"}
    assert claim.proceed == deliverable
    assert (resolved is not None) == deliverable
    if resolved is not None:
        assert resolved.session_id == current.session_id
    assert store._db.get_session(old_id)["end_reason"] == reason


@pytest.mark.asyncio
async def test_idle_recovery_ownership_isolated_across_profile_homes(tmp_path, monkeypatch):
    from pathlib import Path
    from gateway.run import _profile_runtime_scope

    home = tmp_path / ".hermes"
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    (named / "config.yaml").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / "sessions", config)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    entries = {}
    dbs = {}
    try:
        for profile, owning_home in [("default", home), ("work", named)]:
            with _profile_runtime_scope(owning_home):
                source = SessionSource(platform=Platform.DISCORD, chat_id="same-home", user_id="human",
                                       chat_type="group", profile=profile)
                old = store.get_or_create_session(source)
                db = store._db_for_key(old.session_key)
                dbs[profile] = db
                db.create_session("same-worker", source="subagent", parent_session_id=old.session_id,
                                  profile_name=profile, model_config={"_delegate_from": old.session_id})
                db.record_gateway_session_peer("same-worker", source="discord", session_key=old.session_key)
                db.end_session("same-worker", end_reason="user_exit")
                db.end_session(old.session_id, end_reason="idle_timeout")
                current = store.get_or_create_session(source, touch_activity=False)
                entries[profile] = current
                assert json.loads(db.get_session(current.session_id)["model_config"])["_recovered_from"] == old.session_id
        assert dbs["default"].db_path != dbs["work"].db_path
        for profile, owning_home in [("default", home), ("work", named), ("default", home)]:
            with _profile_runtime_scope(owning_home):
                runner._session_db = AsyncSessionDB(dbs[profile])
                current = entries[profile]
                claim = await runner._preflight_completion_delivery(
                    {"type": "completion", "parent_session_id": "same-worker", "session_key": current.session_key})
                result = await runner._resolve_async_delegation_session(current, "same-worker")
                assert claim.proceed and result is not None and result.session_id == current.session_id
                foreign = entries["work" if profile == "default" else "default"]
                assert await runner._resolve_async_delegation_session(foreign, "same-worker") is None
                assert store.peek_session_id(foreign.session_key) == foreign.session_id
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["owned", "unrelated-live", "idle", "compression", "unrelated-compression", "new"])
async def test_preflight_and_resolver_agree_on_route_ownership(tmp_path, state):
    runner, store, entry = conversation(tmp_path)
    pin = entry.session_id
    if state.startswith("unrelated"):
        pin = "unrelated-owner"
        store._db.create_session(pin, source="discord", session_key=entry.session_key)
    if "compression" in state:
        store._db.end_session(pin, end_reason="compression")
        store._db.create_session("agreement-tip", source="discord", parent_session_id=pin)
    elif state == "idle":
        store._db.end_session(pin, end_reason="idle")
    elif state == "new":
        entry = store.reset_session(entry.session_key)
    claim = await runner._preflight_completion_delivery(
        {"type": "completion", "parent_session_id": pin, "session_key": entry.session_key})
    resolved = await runner._resolve_async_delegation_session(entry, pin)
    assert claim.proceed == (resolved is not None)
    assert claim.proceed == (state in {"owned", "idle", "compression"})

"""Exact channel identity remains isolated under profile multiplexing."""
import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.run_heartbeat_restore import restore_heartbeat_watches
from gateway.session import SessionSource, SessionStore
from gateway.session_identity import RoutingIdentity
from hermes_cli import goals
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_channel_restore_profile_isolation_and_unknown_route_fail_closed(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    named = home / 'profiles' / 'work'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): SessionDB(p / 'state.db') for p in (home, named)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._start_heartbeat_poller = lambda: None
    runner.session_store = SessionStore(home / 'sessions', runner.config)
    sources = []
    try:
        for profile, owning_home in [('default', home), ('work', named)]:
            source = SessionSource(platform=Platform.TELEGRAM, chat_id='same-chat', thread_id='same-topic',
                                   profile=profile, user_id='owner')
            source._identity = RoutingIdentity('default', profile, home, owning_home)
            with _profile_runtime_scope(owning_home):
                answer = await runner._handle_heartbeat_command(MessageEvent(
                    text='/heartbeat channel 1m ' + profile, source=source))
                assert 'set' in answer.lower(), answer
            sources.append(source)
        for source, owning_home in [(sources[0], home), (sources[1], named), (sources[0], home)]:
            with _profile_runtime_scope(owning_home):
                key = runner._session_key_for_source(source)
                assert runner._channel_heartbeat_manager(source, key).state.prompt == source.profile
        with _profile_runtime_scope(home):
            with pytest.raises(ValueError, match='unresolved'):
                runner._channel_heartbeat_manager(sources[1], runner._session_key_for_source(sources[1]))
            unknown = SessionSource(platform=Platform.TELEGRAM, chat_id='unknown-bot', profile='default')
            unknown._identity = RoutingIdentity('default', 'default', home, home, transport_inferred=True)
            with pytest.raises(ValueError, match='unresolved'):
                runner._channel_heartbeat_manager(unknown, runner._session_key_for_source(unknown))
            # Enabling channel scope elsewhere must not break pre-column legacy origins.
            legacy = SessionSource(platform=Platform.TELEGRAM, chat_id='legacy', profile='default')
            legacy_entry = runner.session_store.get_or_create_session(legacy)
            from hermes_cli.heartbeat import HeartbeatManager
            HeartbeatManager(legacy_entry.session_id).set('legacy still works', 60)
            selected = runner._heartbeat_manager_for_route(legacy, legacy_entry.session_key, legacy_entry.session_id)
            assert selected.state.prompt == 'legacy still works'
        runner.session_store.close_all_db_handles()
        runner.session_store = SessionStore(home / 'sessions', runner.config)
        runner._heartbeat_watch = {}
        with _profile_runtime_scope(named):
            await restore_heartbeat_watches(runner)
        assert set(runner._heartbeat_watch) == ({runner._session_key_for_source(source) for source in sources}
                                               | {legacy_entry.session_key})
    finally:
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()

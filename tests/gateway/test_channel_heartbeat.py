"""Channel heartbeat runs in the current conversation through the existing idle poller."""
import asyncio
from types import SimpleNamespace

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_heartbeat_restore import restore_heartbeat_watches
from gateway.session import SessionSource, SessionStore
from hermes_cli import heartbeat
from hermes_cli.heartbeat import HeartbeatManager


@pytest.mark.asyncio
async def test_channel_command_reset_restart_and_idle_only(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(heartbeat, 'time', SimpleNamespace(time=lambda: clock.now))
    config = GatewayConfig(multiplex_profiles=False)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._start_heartbeat_poller = lambda: None
    runner.session_store = SessionStore(tmp_path / 'sessions', config)
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='channel', user_id='owner')
    received = []

    async def model(event):
        event._heartbeat_execution_started = True
        received.append(event)
        return None

    adapter.set_message_handler(model)
    try:
        answer = await runner._handle_heartbeat_command(MessageEvent(text='/heartbeat channel every 1m', source=source))
        assert 'set' in answer.lower(), answer
        entry = runner.session_store.get_or_create_session(source)
        key, old = entry.session_key, entry.session_id
        replacement = runner.session_store.reset_session(key)
        assert replacement.session_id != old
        # A legacy schedule must not create a second tick alongside the channel schedule.
        HeartbeatManager(replacement.session_id).set('legacy prompt must not execute', 60)
        runner.session_store.close_all_db_handles()
        runner.session_store = SessionStore(tmp_path / 'sessions', config)
        runner._heartbeat_watch = {}
        await restore_heartbeat_watches(runner)
        assert set(runner._heartbeat_watch) == {key}
        clock.now += 60
        runner._running_agents[key] = object()
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert not received
        runner._running_agents.clear()
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        await asyncio.gather(*adapter._background_tasks)
        assert len(received) == 1
        assert 'legacy prompt' not in received[0].text
        assert 'reply briefly that nothing has changed' not in received[0].text
        assert received[0]._heartbeat_session_id == replacement.session_id
        assert received[0]._heartbeat_channel_route.session_key == key
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert len(received) == 1
        paused = await runner._handle_heartbeat_command(MessageEvent(text='/heartbeat channel pause', source=source))
        assert 'paused' in paused.lower()
        clock.now += 120
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert len(received) == 1, 'paused channel must also suppress the legacy session schedule'
        cleared = await runner._handle_heartbeat_command(MessageEvent(text='/heartbeat channel clear', source=source))
        assert 'cleared' in cleared.lower()
        runner._heartbeat_watch = {}
        await restore_heartbeat_watches(runner)
        assert not runner._heartbeat_watch, 'clear must not reveal a shadow legacy heartbeat'
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['reset', 'switch', 'compression', 'pause', 'clear', 'prepare-reset', 'suspend-before', 'suspend-after'])
async def test_channel_admission_follows_current_route_but_pause_and_prepare_races_win(tmp_path, boundary):
    from unittest.mock import AsyncMock

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._start_heartbeat_poller = lambda: None
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_telegram_topic_lane = lambda source: False
    runner._cache_session_source = lambda *args: None
    runner._clear_session_env = lambda tokens: None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='race', user_id='owner')
    await runner._handle_heartbeat_command(MessageEvent(text='/heartbeat channel 1m contextual', source=source))
    entry = runner.session_store.get_or_create_session(source)
    key, old = entry.session_key, entry.session_id
    manager = runner._channel_heartbeat_manager(source, key)
    manager.due_prompt(now=manager.state.created_at + 60)
    manager.abandon_fire()
    # Make it due without patching the gateway's clocks or storage implementation.
    state = manager.state
    state.created_at = 1
    manager._change(lambda current, raw: (state, state))
    executions = []

    def change():
        if boundary.startswith('suspend-'):
            assert runner.session_store.suspend_session(key)
        elif boundary in {'reset', 'prepare-reset'}:
            runner.session_store.reset_session(key)
        elif boundary == 'switch':
            replacement = runner.session_store.reset_session(key)
            runner.session_store.switch_session(key, old)
            runner.session_store.switch_session(key, replacement.session_id)
        elif boundary == 'compression':
            child = old + '-child'
            runner.session_store._db_for_key(key).publish_compression_child(
                parent_session_id=old, child_session_id=child, source='telegram',
                require_compression_lease=False, model='offline', model_config={},
                system_prompt='offline', messages=[{'role': 'user', 'content': 'retained'}])
            assert runner.session_store.advance_compression_session(key, old, child)
        else:
            getattr(runner._channel_heartbeat_manager(source, key), boundary)()

    async def prepare(*args):
        if boundary == 'prepare-reset':
            change()
        return runner._PreparedTurn([], '', 'contextual', 'contextual', None, None), {}

    async def model(**kwargs):
        executions.append(kwargs['session_id'])
        raise asyncio.CancelledError

    runner._hmwa_prepare_turn = prepare
    runner._run_agent = model
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    adapter.set_message_handler(lambda event: runner._handle_message_with_agent(event, event.source, key, 1))
    try:
        if boundary == 'suspend-before':
            change()
        await runner._heartbeat_poll_once({key: (source, old)})
        assert (key in adapter._session_tasks) == (boundary != 'suspend-before')
        if boundary not in {'prepare-reset', 'suspend-before'}:
            change()
        expected = [] if boundary in {'pause', 'clear', 'prepare-reset', 'suspend-before', 'suspend-after'} else [runner.session_store.peek_session_id(key)]
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert executions == expected
        if boundary == 'prepare-reset':
            assert runner._channel_heartbeat_manager(source, key).state.fire_count == 0
        if boundary.startswith('suspend-'):
            runner.session_store.close_all_db_handles()
            runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
            current = runner.session_store.lookup_by_session_key(key)
            assert current is not None
            assert current.session_id == old and current.suspended
            durable = runner._channel_heartbeat_manager(source, key).state
            assert durable is not None and durable.fire_count == 0
            assert durable.status == 'active'
            human = MessageEvent(text='resume human conversation', source=source,
                                 metadata={'gateway_session_key': key})
            resolved = await runner._hmwa_resolve_session(human, source)
            assert resolved is not None and resolved[1].session_id != old
            assert not resolved[1].suspended
            await runner._heartbeat_poll_once({key: (source, old)})
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
            await asyncio.sleep(0)
            assert executions == [resolved[1].session_id]
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['pause', 'clear', 'replacement', 'reset', 'suspend', 'cancel', 'unchanged'])
async def test_channel_tick_revalidates_after_real_prompt_pin_persistence(tmp_path, monkeypatch, boundary):
    from unittest.mock import AsyncMock

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._start_heartbeat_poller = lambda: None
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_telegram_topic_lane = lambda source: False
    runner._cache_session_source = lambda *args: None
    runner._clear_session_env = lambda tokens: None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='persist-race', user_id='owner')
    await runner._handle_heartbeat_command(MessageEvent(text='/heartbeat channel 1m original', source=source))
    entry = runner.session_store.get_or_create_session(source)
    key, old = entry.session_key, entry.session_id
    manager = runner._channel_heartbeat_manager(source, key)
    state = manager.state
    state.created_at = 1
    manager._change(lambda current, raw: (state, state))
    persisted, release = asyncio.Event(), asyncio.Event()
    events, executions = [], []
    actual_write = runner.async_session_store.set_prompt_pin

    async def gated_write(*args, **kwargs):
        # Exercise the actual async facade, sync writer and durable routing index.
        # Only its return is held, leaving the loop free for concurrent commands.
        result = await actual_write(*args, **kwargs)
        assert result is True
        persisted.set()
        await release.wait()
        return result

    monkeypatch.setattr(runner.async_session_store, 'set_prompt_pin', gated_write)

    async def prepare(*args):
        runner._session_state(key).conversation.ephemeral_pin = ('context-key', 'context bytes', False)
        return runner._PreparedTurn([], 'context bytes', 'original', 'original', None, None), {}

    async def model(**kwargs):
        executions.append(kwargs['session_id'])
        raise asyncio.CancelledError

    async def handler(event):
        events.append(event)
        return await runner._handle_message_with_agent(event, event.source, key, 1)

    runner._hmwa_prepare_turn = prepare
    runner._run_agent = model
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    adapter.set_message_handler(handler)
    try:
        await runner._heartbeat_poll_once({key: (source, old)})
        await asyncio.wait_for(persisted.wait(), timeout=10)
        assert runner.session_store.get_prompt_pin(key, expected_session_id=old)['context_prompt'] == 'context bytes'
        # Neither admission nor a completed prompt-pin write is model execution.
        premature_start = events[0]._heartbeat_execution_started
        if boundary in {'pause', 'clear'}:
            getattr(runner._channel_heartbeat_manager(source, key), boundary)()
        elif boundary == 'replacement':
            runner._channel_heartbeat_manager(source, key).set('replacement', 120)
        elif boundary == 'reset':
            runner.session_store.reset_session(key)
        elif boundary == 'suspend':
            runner.session_store.suspend_session(key)
        elif boundary == 'cancel':
            adapter._session_tasks[key].cancel()
        release.set()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert executions == ([old] if boundary == 'unchanged' else [])
        assert not premature_start
        assert events[0]._heartbeat_execution_started == (boundary == 'unchanged')
        runner.session_store.close_all_db_handles()
        runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
        durable = runner._channel_heartbeat_manager(source, key).state
        if boundary == 'clear':
            assert durable is None
        else:
            assert durable is not None
            # Pause owns a new durable revision, so a stale refund must not overwrite it.
            # Unchanged, unexecuted claims (reset/suspend/cancel) are refunded instead.
            assert durable.fire_count == (1 if boundary in {'unchanged', 'pause'} else 0)
            assert durable.status == ('paused' if boundary == 'pause' else 'active')
            assert durable.prompt == ('replacement' if boundary == 'replacement' else 'original')
    finally:
        release.set()
        runner.session_store.close_all_db_handles()

"""Durable channel schedules are independent of conversation IDs and claim atomically."""
from concurrent.futures import ThreadPoolExecutor

from hermes_cli import heartbeat
from hermes_state import SessionDB


def test_channel_state_survives_new_manager_and_claim_races(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    independent = SessionDB(tmp_path / 'state.db')
    monkeypatch.setattr(heartbeat, '_get_session_db', lambda: db)
    monkeypatch.setattr(heartbeat.time, 'time', lambda: 1000.0)
    try:
        factory = getattr(heartbeat, 'ChannelHeartbeatManager', None)
        assert factory is not None, 'channel heartbeat must have a durable manager'
        route = heartbeat.ChannelHeartbeatRoute('agent:main:discord:channel:42', 'default',
                                                'default', 'discord', '42', None, None)
        first = factory(route)
        first.set('Use current conversation commitments', 60)
        managers = [factory(route)]
        monkeypatch.setattr(heartbeat, '_get_session_db', lambda: independent)
        managers.append(factory(route))
        monkeypatch.setattr(heartbeat, '_get_session_db', lambda: db)
        with ThreadPoolExecutor(2) as pool:
            prompts = list(pool.map(lambda manager: manager.due_prompt(1060), managers))
        assert sum(prompt is not None for prompt in prompts) == 1
        assert factory(route).state.fire_count == 1
        claimed = next(manager for manager, prompt in zip(managers, prompts) if prompt)
        factory(route).pause()
        assert not claimed.abandon_fire(), 'pause must win over a stale admission refund'
        assert factory(route).state.status == 'paused'
        paused = factory(route)
        assert paused.due_prompt(9999) is None
        assert paused.state.status == 'paused', 'polling does not discard durable paused state'
        factory(route).resume()
        assert factory(route).state.last_fired_at == 1000
        factory(route).clear()
        assert not factory(route).has_heartbeat()
        assert db.get_meta(route.meta_key) is not None, 'clear is a durable tombstone'
        other = heartbeat.ChannelHeartbeatRoute(route.session_key, 'other', 'default',
                                                'discord', '42', None, None)
        assert not factory(other).has_heartbeat(), 'transport identities must not alias'
    finally:
        db.close()
        independent.close()

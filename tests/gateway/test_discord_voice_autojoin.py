"""Event-only autojoin exercises the real GatewayRunner speech lifecycle with fake IO."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


@pytest.fixture
def rig(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner._voice_mode = {}
    runner._VOICE_MODE_PATH = tmp_path / 'voice.json'
    runner._adapter_profile_for_source = lambda source: 'default'
    runner._canonicalize = lambda source, **kwargs: source
    runner._is_user_authorized_for_source = lambda source: True
    guild = NS(id=10)
    general = NS(id=30, name='General', guild=guild)
    other = NS(id=31, name='Other', guild=guild)
    member = NS(id=20, guild=guild, voice=NS(channel=general), bot=False)
    guild.voice_channels = [general, other]
    guild.get_member = lambda uid: member if uid == 20 else None
    text = NS(id=40, guild=guild, parent_id=50, name='thread', send=AsyncMock())
    parent = NS(id=50, guild=guild)
    client = NS(get_guild=lambda gid: guild if gid == 10 else None,
                get_channel=lambda cid: {30: general, 31: other, 40: text, 50: parent}.get(cid))
    class Adapter:
        platform = Platform.DISCORD
        _owner_profile = 'default'
        _voice_input_callback = None
        _on_voice_disconnect = None
        _voice_mode_getter = None
        def __init__(self):
            self.config = NS(extra={'auto_join': {'enabled': True, 'guild_id': '10',
                'user_id': '20', 'voice_channel_name': 'General', 'text_channel_id': '40',
                'text_parent_id': '50'}})
            self._client = client
            self._voice_clients = {}
            self._voice_text_channels = {}
            self._voice_sources = {}
            self._auto_tts_enabled_chats = set()
            self._auto_tts_disabled_chats = set()
            self.joins = 0
            self.leaves = 0
            self.handle_message = AsyncMock()
        async def get_user_voice_channel(self, gid, uid):
            return member.voice.channel
        def is_in_voice_channel(self, gid):
            return gid in self._voice_clients
        async def join_voice_channel(self, channel):
            assert callable(self._voice_input_callback)
            self.joins += 1
            if 10 in self._voice_clients:
                self._voice_clients[10].channel = channel
            else:
                self._voice_clients[10] = NS(channel=channel)
            return True
        async def leave_voice_channel(self, gid):
            self.leaves += 1
            self._voice_clients.pop(gid, None)
            self._voice_sources.pop(gid, None)
            self._voice_text_channels.pop(gid, None)
    adapter = Adapter()
    runner._delivery_adapter_for = lambda source: adapter
    runner.adapters = {Platform.DISCORD: adapter}
    return NS(runner=runner, adapter=adapter, member=member, guild=guild,
              general=general, other=other, text=text)


async def update(rig, before, after):
    rig.member.voice.channel = after
    await rig.runner._handle_discord_voice_state_update(
        rig.adapter, rig.member, NS(channel=before), NS(channel=after))


@pytest.mark.asyncio
async def test_cold_start_and_manual_leave_reentry_restore_speech(rig):
    await update(rig, None, rig.general)
    a = rig.adapter
    assert a.joins == 1
    assert rig.runner._voice_mode['discord:40'] == 'all'
    assert a._voice_mode_getter('40') == 'all'
    assert a._voice_sources[10]['chat_type'] == 'thread'
    assert a._voice_sources[10]['parent_chat_id'] == '50'
    await a._voice_input_callback(10, 20, 'first speech')
    assert a.handle_message.await_args.args[0].text == 'first speech'
    event = a.handle_message.await_args.args[0]
    await rig.runner._handle_voice_channel_leave(event)
    assert a._voice_input_callback is None
    await update(rig, rig.general, None)
    await update(rig, None, rig.general)
    assert a.joins == 2
    await a._voice_input_callback(10, 20, 'second speech')
    assert a.handle_message.await_args.args[0].text == 'second speech'
    assert rig.runner._voice_mode['discord:40'] == 'all'
    a.play_in_voice_channel = AsyncMock()
    await rig.runner._deliver_voice_reply(a.handle_message.await_args.args[0], ['reply.wav'])
    a.play_in_voice_channel.assert_awaited_once_with(10, 'reply.wav')

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['disabled', 'malformed', 'unknown', 'wrong_user',
    'wrong_guild', 'wrong_text_guild', 'wrong_parent', 'missing_text', 'ambiguous',
    'stale_member', 'existing', 'duplicate', 'mute', 'unauthorized', 'wrong_voice_id'])
async def test_only_fresh_pinned_entry_can_create_owned_connection(rig, case):
    a = rig.adapter
    pins = a.config.extra['auto_join']
    before = None
    if case == 'disabled':
        a.config.extra = {}
    elif case == 'malformed':
        pins['guild_id'] = True
    elif case == 'unknown':
        pins['user_ids'] = [20]
    elif case == 'wrong_user':
        rig.member.id = 21
    elif case == 'wrong_guild':
        rig.member.guild = NS(id=11)
    elif case == 'wrong_text_guild':
        rig.text.guild = NS(id=11)
    elif case == 'wrong_parent':
        pins['text_parent_id'] = '51'
    elif case == 'missing_text':
        pins['text_channel_id'] = '41'
    elif case == 'ambiguous':
        rig.guild.voice_channels.append(NS(id=32, name='General', guild=rig.guild))
    elif case == 'stale_member':
        rig.member.voice.channel = rig.other
    elif case == 'existing':
        a._voice_clients[10] = NS(channel=rig.other)
    elif case == 'duplicate':
        await update(rig, None, rig.general)
        event = NS(source=rig.runner._voice_input_source(a, 10, 20, 40),
                   raw_message=NS(guild_id=10))
        await rig.runner._handle_voice_channel_leave(event)
        a.joins = 0
    elif case == 'mute':
        before = rig.general
    elif case == 'unauthorized':
        rig.runner._is_user_authorized_for_source = lambda source: False
    elif case == 'wrong_voice_id':
        pins['voice_channel_id'] = '31'
    await rig.runner._handle_discord_voice_state_update(
        a, rig.member, NS(channel=before), NS(channel=rig.general))
    assert a.joins == 0
    assert a.leaves == (1 if case == 'duplicate' else 0)

@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['join_exit', 'leave_entry', 'manual_takeover'])
async def test_membership_and_ownership_rechecked_across_lifecycle_awaits(rig, phase):
    a = rig.adapter
    started, release = asyncio.Event(), asyncio.Event()
    if phase == 'join_exit':
        join = a.join_voice_channel
        async def slow_join(channel):
            started.set()
            await release.wait()
            return await join(channel)
        a.join_voice_channel = slow_join
        task = asyncio.create_task(update(rig, None, rig.general))
        await started.wait()
        exit_task = asyncio.create_task(update(rig, rig.general, None))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, exit_task)
        assert not a.is_in_voice_channel(10)
        assert rig.runner._voice_mode['discord:40'] == 'off'
    else:
        await update(rig, None, rig.general)
        if phase == 'manual_takeover':
            # Normal manual join relinquishes auto ownership even in the same channel.
            event = NS(source=rig.runner._voice_input_source(a, 10, 20, 40),
                       raw_message=NS(guild_id=10))
            await rig.runner._handle_voice_channel_join(event)
            await update(rig, rig.general, None)
            assert a.is_in_voice_channel(10)
            assert a.leaves == 0
            return
        leave = a.leave_voice_channel
        async def slow_leave(gid):
            started.set()
            await release.wait()
            await leave(gid)
        a.leave_voice_channel = slow_leave
        task = asyncio.create_task(update(rig, rig.general, None))
        await started.wait()
        entry_task = asyncio.create_task(update(rig, None, rig.general))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, entry_task)
        assert a.is_in_voice_channel(10)
        assert a.joins == 2
        assert rig.runner._voice_mode['discord:40'] == 'all'


@pytest.mark.asyncio
async def test_native_adapter_forwards_cold_start_event(rig):
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    adapter.gateway_runner = rig.runner
    rig.runner._handle_discord_voice_state_update = AsyncMock()
    before, after = NS(channel=None), NS(channel=rig.general)
    await adapter._on_voice_state_update(rig.member, before, after)
    rig.runner._handle_discord_voice_state_update.assert_awaited_once_with(
        adapter, rig.member, before, after)

@pytest.mark.asyncio
async def test_manual_commands_without_delivery_adapter_preserve_unsupported_result(rig):
    rig.runner._delivery_adapter_for = lambda source: None
    event = NS(source=NS(platform=Platform.DISCORD), raw_message=NS(guild_id=10))
    assert isinstance(await rig.runner._handle_voice_channel_join(event), str)
    assert isinstance(await rig.runner._handle_voice_channel_leave(event), str)

@pytest.mark.asyncio
@pytest.mark.parametrize('key,value', [('voice_channel_id', None), ('guild_id', ' 10'),
    ('user_id', 20.0), ('text_channel_id', True), ('voice_channel_id', -1),
    ('guild_id', '18446744073709551616')])
async def test_malformed_pins_fail_closed(rig, key, value):
    rig.adapter.config.extra['auto_join'][key] = value
    await update(rig, None, rig.general)
    assert rig.adapter.joins == 0


@pytest.mark.asyncio
async def test_owned_exit_never_follows_another_channel(rig):
    await update(rig, None, rig.general)
    await update(rig, rig.general, rig.other)
    assert rig.adapter.joins == 1
    assert rig.adapter.leaves == 1
    assert rig.runner._voice_mode['discord:40'] == 'off'

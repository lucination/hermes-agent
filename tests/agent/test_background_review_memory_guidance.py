"""Review guidance matches dispatch policy without rewriting the cached prefix."""

import copy
from types import SimpleNamespace

import pytest

from agent import background_review as review
from hermes_cli.plugins import get_pre_tool_call_block_message


@pytest.mark.parametrize("explicit", [False, True])
def test_memory_review_guidance_preserves_prefix_and_describes_consolidation(monkeypatch, explicit):
    history = [
        {"role": "system", "content": "frozen system and memory snapshot"},
        {"role": "user", "content": "Contact phone is +12025550123, not +1**********."},
        {"role": "assistant", "content": "Noted."},
    ]
    original = copy.deepcopy(history)
    captured = {}
    parent = SimpleNamespace(_cached_system_prompt="frozen system and memory snapshot")

    def run_conversation(*, user_message, conversation_history):
        captured["prompt"] = user_message
        captured["history"] = copy.deepcopy(conversation_history)
        captured["deny"] = get_pre_tool_call_block_message("terminal", {})

    fork = SimpleNamespace(run_conversation=run_conversation, _memory_enabled=True,
                           _user_profile_enabled=True, _session_messages=[],
                           release_clients=lambda: None)
    monkeypatch.setattr(review, "build_cache_parity_fork", lambda *a, **kw: (fork, None, False))
    review._run_review_fork(parent, history, review._MEMORY_REVIEW_PROMPT, {}, None,
                            review._ReviewForkState(), review_memory=True, explicit=explicit)

    assert captured["history"] == original
    assert history == original
    assert parent._cached_system_prompt == "frozen system and memory snapshot"
    deny = captured["deny"]
    assert "add only" not in deny
    assert "replace/remove" in deny
    assert "policy" in deny and "approval" in deny
    prompt = captured["prompt"]
    assert "consolidate" in prompt
    assert "preserve" in prompt and "facts" in prompt
    assert "whole entry" in prompt
    assert "phone" in prompt and "masked" in prompt
    assert "pending" in prompt and "replay" in prompt
    assert "background_review_allow_destructive" in prompt
    assert "write approval" in prompt


def test_skill_only_review_does_not_advertise_memory_consolidation(monkeypatch):
    captured = {}

    def run_conversation(*, user_message, conversation_history):
        captured["prompt"] = user_message
        captured["deny"] = get_pre_tool_call_block_message("terminal", {})

    fork = SimpleNamespace(run_conversation=run_conversation, _memory_enabled=False,
                           _user_profile_enabled=False, _session_messages=[],
                           release_clients=lambda: None)
    monkeypatch.setattr(review, "build_cache_parity_fork", lambda *a, **kw: (fork, None, False))
    review._run_review_fork(SimpleNamespace(), [], "review skills", {}, None,
                            review._ReviewForkState())
    assert "memory" not in captured["deny"]
    assert "consolidate" not in captured["prompt"]
    assert "background_review_allow_destructive" not in captured["prompt"]

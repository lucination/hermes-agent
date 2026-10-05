"""Vault evaluation must use the selected Camofox task tab, never Chromium argv."""
import pytest

from tools import browser_camofox as cf
from tools import browser_vault_tool as vault


@pytest.mark.parametrize("evaluate", [vault._eval_js, vault._eval_js_secret])
def test_camofox_vault_evaluates_in_task_tab(monkeypatch, evaluate):
    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(cf, "_ensure_tab", lambda task: {"tab_id": "fixture-tab", "user_id": task})
    calls = []

    def post(path, body):
        calls.append((path, body))
        return {"result": {"filled": 1}}

    monkeypatch.setattr(cf, "_post", post)
    monkeypatch.setattr(vault, "_ensure_supervisor", lambda task: pytest.fail("Camofox is not CDP"))
    assert evaluate("fixture-task", "test-expression") == {"success": True, "result": {"filled": 1}}
    assert calls == [("/tabs/fixture-tab/evaluate", {"expression": "test-expression", "userId": "fixture-task"})]


def test_camofox_focus_does_not_attach_chromium(monkeypatch):
    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(vault, "_ensure_supervisor", lambda task: pytest.fail("wrong browser"))
    assert vault._focus_bound_origin("fixture-task", "https://example.com", "login") is None


@pytest.mark.parametrize("reply", [{"error": "secret-expression"}, {"ok": True}])
def test_camofox_eval_fails_closed_without_result(monkeypatch, reply):
    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(cf, "_ensure_tab", lambda task: {"tab_id": "fixture-tab", "user_id": task})
    monkeypatch.setattr(cf, "_post", lambda *a: reply)
    result = vault._eval_js_secret("fixture-task", "secret-expression")
    assert result["success"] is False
    assert "secret-expression" not in str(result)


def test_camofox_eval_exception_does_not_echo_expression(monkeypatch):
    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(cf, "_ensure_tab", lambda task: {"tab_id": "fixture-tab", "user_id": task})

    def post(*args):
        raise RuntimeError("secret-expression")

    monkeypatch.setattr(cf, "_post", post)
    result = vault._eval_js_secret("fixture-task", "secret-expression")
    assert result["success"] is False
    assert "secret-expression" not in str(result)


def test_camofox_secret_write_readmits_desktop_lease(monkeypatch):
    from tools.bot_desktop import lease

    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(vault, "_bot_desktop_browser_session", lambda task: True)

    def taken_over():
        raise lease.HumanHasControl("Human is in control")

    monkeypatch.setattr(lease, "assert_agent_may_act", taken_over)
    monkeypatch.setattr(vault, "_eval_camofox", lambda *a: {"success": True, "result": "written"})
    result = vault._eval_js_secret("fixture-task", "secret-expression")
    assert result["success"] is False
    assert result["error_type"] == "human_has_control"

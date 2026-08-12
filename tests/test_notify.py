import pytest

from app.core.config import settings
from app.services import notify as notify_module
from app.services import trade_republic


@pytest.fixture
def sent(monkeypatch):
    """Capture notifications instead of posting them to Discord."""
    messages = []

    def _fake_notify(title, message=""):
        messages.append((title, message))
        return True

    monkeypatch.setattr(notify_module, "notify", _fake_notify)
    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.test/webhook")
    monkeypatch.setattr(settings, "notify_on_session_expired", True)
    monkeypatch.setattr(settings, "notify_on_sync_failure", True)
    return messages


@pytest.fixture
def session_store(monkeypatch, tmp_path):
    """A connected session in an isolated store."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(trade_republic, "SESSIONS", {"s1": {"status": "connected"}})
    return trade_republic.SESSIONS


# --- enabling / disabling ----------------------------------------------------

def test_disabled_without_webhook_url(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_url", "")

    assert notify_module.notifications_enabled() is False
    assert notify_module.notify("title", "body") is False


def test_enabled_with_webhook_url(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.test/webhook")

    assert notify_module.notifications_enabled() is True


def test_webhook_errors_are_swallowed(monkeypatch):
    """A broken webhook must never break a sync, so notify() reports failure
    rather than raising."""
    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.test/webhook")

    def _boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    import httpx

    monkeypatch.setattr(httpx, "post", _boom)

    assert notify_module.notify("title", "body") is False


def test_long_messages_are_truncated(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.test/webhook")

    class _Response:
        def raise_for_status(self):
            return None

    def _fake_post(url, json, timeout):
        captured["content"] = json["content"]
        return _Response()

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)

    notify_module.notify("title", "x" * 5000)

    # Discord rejects anything above 2000 characters.
    assert len(captured["content"]) < 2000


# --- session expiry ----------------------------------------------------------

def test_expiry_notifies_once(sent, session_store):
    trade_republic._mark_session_expired("s1")

    assert len(sent) == 1
    assert "expired" in sent[0][0].lower()


def test_repeated_expiry_does_not_re_notify(sent, session_store):
    """Failed resumes are retried; only the first transition is reported."""
    trade_republic._mark_session_expired("s1")
    trade_republic._mark_session_expired("s1")
    trade_republic._mark_session_expired("s1")

    assert len(sent) == 1


def test_reconnect_then_expiry_notifies_again(sent, session_store):
    trade_republic._mark_session_expired("s1")
    session_store["s1"]["status"] = "connected"
    trade_republic._mark_session_expired("s1")

    assert len(sent) == 2


def test_no_notification_without_session_id(sent, session_store):
    trade_republic._mark_session_expired(None)

    assert sent == []


def test_expiry_notification_can_be_disabled(sent, session_store, monkeypatch):
    monkeypatch.setattr(settings, "notify_on_session_expired", False)

    trade_republic._mark_session_expired("s1")

    assert sent == []


# --- sync failures -----------------------------------------------------------

def test_sync_failure_notifies(sent):
    notify_module.notify_sync_failure("scheduled transaction sync", "boom")

    assert len(sent) == 1
    assert "boom" in sent[0][1]


def test_sync_failure_notification_can_be_disabled(sent, monkeypatch):
    monkeypatch.setattr(settings, "notify_on_sync_failure", False)

    notify_module.notify_sync_failure("scheduled transaction sync", "boom")

    assert sent == []
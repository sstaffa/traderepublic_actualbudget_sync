import pytest

from app.core.config import settings
from app.services import trade_republic
from app.services.trade_republic import (
    LOGIN_METHOD_APP_CONFIRMATION,
    LOGIN_METHOD_AUTHENTICATOR,
    LOGIN_METHOD_CODE,
    _detect_login_method,
    _use_v2_login,
    confirm_login,
)


class FakeApi:
    """Minimal stand-in for pytr's TradeRepublicApi."""

    def __init__(self, needs_authenticator=False, outcome=None):
        self.weblogin_needs_authenticator = needs_authenticator
        self._process_id = "process-1"
        self._outcome = outcome
        self.completed_with = "not called"
        self.saved = False

    def complete_weblogin(self, verify_code=None):
        self.completed_with = verify_code
        if isinstance(self._outcome, Exception):
            raise self._outcome

    def save_websession(self):
        self.saved = True


@pytest.fixture
def challenge_session(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(settings, "app_mode", "production")
    monkeypatch.setattr(
        trade_republic,
        "SESSIONS",
        {"s1": {"status": "challenge", "process_id": "process-1"}},
    )
    # confirm_login() reloads the store from disk first, which would discard the
    # session set up above, so keep the in-memory one authoritative here.
    monkeypatch.setattr(trade_republic, "_load_sessions", lambda: None)
    monkeypatch.setattr(trade_republic, "_save_sessions", lambda: None)
    return trade_republic.SESSIONS


# --- login mode selection ----------------------------------------------------

def test_v2_is_the_default(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "v2")

    assert _use_v2_login() is True


def test_unset_mode_falls_back_to_v2(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "")

    assert _use_v2_login() is True


def test_v1_can_be_selected(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "v1")

    assert _use_v2_login() is False


def test_mode_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", " V1 ")

    assert _use_v2_login() is False


# --- which second factor a login expects -------------------------------------

def test_v1_always_expects_a_code(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "v1")

    assert _detect_login_method(FakeApi(needs_authenticator=True)) == LOGIN_METHOD_CODE


def test_v2_without_authenticator_waits_for_app_confirmation(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "v2")

    assert _detect_login_method(FakeApi()) == LOGIN_METHOD_APP_CONFIRMATION


def test_v2_with_authenticator_expects_a_code(monkeypatch):
    monkeypatch.setattr(settings, "tr_login_mode", "v2")

    assert _detect_login_method(FakeApi(needs_authenticator=True)) == LOGIN_METHOD_AUTHENTICATOR


# --- confirm_login -----------------------------------------------------------

def test_confirmation_marks_session_connected(monkeypatch, challenge_session):
    api = FakeApi()
    monkeypatch.setattr(trade_republic, "_get_api_client", lambda sid: api)

    result = confirm_login("s1")

    assert result["status"] == "connected"
    assert challenge_session["s1"]["status"] == "connected"
    # No code is submitted in this flow.
    assert api.completed_with is None
    assert api.saved is True


def test_timeout_is_reported_as_timeout(monkeypatch, challenge_session):
    """Nobody tapped in time - the UI needs to tell this apart from a real
    failure so it can offer a plain retry."""
    api = FakeApi(outcome=TimeoutError("not confirmed in time"))
    monkeypatch.setattr(trade_republic, "_get_api_client", lambda sid: api)

    with pytest.raises(TimeoutError):
        confirm_login("s1")

    assert challenge_session["s1"]["status"] == "expired"


def test_other_errors_mark_the_session_as_error(monkeypatch, challenge_session):
    api = FakeApi(outcome=ValueError("login rejected"))
    monkeypatch.setattr(trade_republic, "_get_api_client", lambda sid: api)

    with pytest.raises(NotImplementedError):
        confirm_login("s1")

    assert challenge_session["s1"]["status"] == "error"


def test_missing_session_id_is_rejected(challenge_session):
    with pytest.raises(NotImplementedError):
        confirm_login(None)


def test_unknown_session_is_rejected(challenge_session):
    with pytest.raises(NotImplementedError):
        confirm_login("does-not-exist")


def test_lost_api_client_is_reported(monkeypatch, challenge_session):
    """After a restart the in-memory client is gone; the login process cannot
    be polled from a rebuilt one, so this must fail clearly."""
    monkeypatch.setattr(trade_republic, "_get_api_client", lambda sid: None)

    with pytest.raises(NotImplementedError):
        confirm_login("s1")


def test_session_without_process_id_is_rejected(monkeypatch, challenge_session):
    monkeypatch.setattr(trade_republic, "_get_api_client", lambda sid: FakeApi())
    challenge_session["s1"].pop("process_id")

    with pytest.raises(NotImplementedError):
        confirm_login("s1")


def test_mock_mode_connects_without_touching_trade_republic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(settings, "app_mode", "mock")
    monkeypatch.setattr(trade_republic, "SESSIONS", {})
    monkeypatch.setattr(trade_republic, "_load_sessions", lambda: None)
    monkeypatch.setattr(trade_republic, "_save_sessions", lambda: None)

    result = confirm_login("s1")

    assert result["status"] == "connected"
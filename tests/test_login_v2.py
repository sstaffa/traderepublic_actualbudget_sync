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


# --- persistent device id ----------------------------------------------------

def test_device_id_is_stable_across_restarts(monkeypatch, tmp_path):
    """pytr derives its device id from uuid.getnode(), which inside a container
    returns a fresh random value on every process start. Trade Republic would
    then see a new device after every restart, so the id is persisted instead."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))

    first = trade_republic._stable_device_id()
    second = trade_republic._stable_device_id()

    assert first == second


def test_device_id_lives_in_the_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))

    trade_republic._stable_device_id()

    assert (tmp_path / "tr-sync_device_id").exists()


def test_device_id_matches_pytr_format(monkeypatch, tmp_path):
    """Same shape as pytr's own value (a SHA-512 hex digest), so the header
    stays indistinguishable from what the web frontend sends."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))

    device_id = trade_republic._stable_device_id()

    assert len(device_id) == 128
    assert all(char in "0123456789abcdef" for char in device_id)


def test_existing_device_id_is_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))
    stored = "a" * 128
    (tmp_path / "tr-sync_device_id").write_text(stored + "\n")

    assert trade_republic._stable_device_id() == stored


def test_unwritable_directory_still_yields_an_id(monkeypatch, tmp_path):
    """Losing persistence must not break the login; it only means Trade
    Republic may treat this as a new device again."""
    monkeypatch.setattr(settings, "tr_cookies_file", str(tmp_path / "pytr_cookies.json"))

    def _fail(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(trade_republic.Path, "write_text", _fail)

    assert len(trade_republic._stable_device_id()) == 128


# --- locally generated authenticator codes -----------------------------------

def test_totp_unavailable_without_a_secret(monkeypatch):
    monkeypatch.setattr(settings, "tr_totp_secret", "")

    assert trade_republic.totp_available() is False
    with pytest.raises(NotImplementedError):
        trade_republic.generate_totp_code()


def test_generated_code_has_six_digits(monkeypatch):
    monkeypatch.setattr(settings, "tr_totp_secret", "JBSWY3DPEHPK3PXP")

    code = trade_republic.generate_totp_code()

    assert trade_republic.totp_available() is True
    assert len(code) == 6 and code.isdigit()


def test_generated_code_matches_a_reference_implementation(monkeypatch):
    """Guards against silently producing codes Trade Republic would reject."""
    import pyotp

    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(settings, "tr_totp_secret", secret)

    assert trade_republic.generate_totp_code() == pyotp.TOTP(secret).now()


def test_secret_is_accepted_with_spaces_and_lowercase(monkeypatch):
    """Authenticator setup screens usually show the secret in groups of four."""
    import pyotp

    monkeypatch.setattr(settings, "tr_totp_secret", "jbsw y3dp ehpk 3pxp")

    assert trade_republic.generate_totp_code() == pyotp.TOTP("JBSWY3DPEHPK3PXP").now()


def test_invalid_secret_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(settings, "tr_totp_secret", "not-base32!")

    with pytest.raises(NotImplementedError):
        trade_republic.generate_totp_code()


def test_reused_code_is_retried_in_the_next_window(monkeypatch):
    """Two attempts inside the same 30 second window reuse a code, which Trade
    Republic rejects; waiting for the next window fixes it."""
    monkeypatch.setattr(settings, "tr_totp_secret", "JBSWY3DPEHPK3PXP")
    slept = []
    monkeypatch.setattr(trade_republic.time, "sleep", lambda seconds: slept.append(seconds))

    calls = []

    def _first_call_rejected(code, session_id):
        calls.append(code)
        if len(calls) == 1:
            raise NotImplementedError("That authenticator code was already used.")
        return {"status": "connected", "session_id": session_id}

    monkeypatch.setattr(trade_republic, "complete_login", _first_call_rejected)

    result = trade_republic.complete_login_with_totp("s1")

    assert result["status"] == "connected"
    assert len(calls) == 2
    assert slept and 0 < slept[0] <= 31


def test_other_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "tr_totp_secret", "JBSWY3DPEHPK3PXP")
    calls = []

    def _rejected(code, session_id):
        calls.append(code)
        raise NotImplementedError("That authenticator code is not correct.")

    monkeypatch.setattr(trade_republic, "complete_login", _rejected)

    with pytest.raises(NotImplementedError):
        trade_republic.complete_login_with_totp("s1")

    assert len(calls) == 1
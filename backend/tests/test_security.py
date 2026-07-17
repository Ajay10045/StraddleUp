import pytest

from app import security


@pytest.fixture
def restore_secret(monkeypatch):
    # Each test mutates APP_SECRET; the fixture just scopes monkeypatch so the
    # process-wide env set by conftest is restored afterwards.
    yield monkeypatch


@pytest.mark.parametrize(
    "value",
    [
        None,  # unset
        "",
        "   ",  # whitespace-only collapses to the empty placeholder
        "local-development-secret-change-me",
        "change-me-before-sharing",
        "replace-with-a-long-random-secret",
    ],
)
def test_placeholder_or_missing_secret_is_rejected(restore_secret, value):
    if value is None:
        restore_secret.delenv("APP_SECRET", raising=False)
    else:
        restore_secret.setenv("APP_SECRET", value)
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        security.issue_token({"kind": "host", "session": "abc"})


def test_real_secret_signs_and_verifies_a_token(restore_secret):
    restore_secret.setenv("APP_SECRET", "a-genuinely-long-random-secret-value-9876543210")
    token = security.issue_token({"kind": "host", "session": "abc"})
    payload = security.read_token(token)
    assert payload is not None
    assert payload["kind"] == "host"
    assert payload["session"] == "abc"


def test_token_signed_with_one_secret_fails_under_another(restore_secret):
    restore_secret.setenv("APP_SECRET", "first-secret-first-secret-first-secret-000000")
    token = security.issue_token({"kind": "host", "session": "abc"})
    restore_secret.setenv("APP_SECRET", "second-secret-second-secret-second-secret-1111")
    assert security.read_token(token) is None

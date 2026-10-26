from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings
from app.core.crypto import SessionCipher

REQUIRED = {
    "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "MONGODB_URI": "mongodb://user:password@example.test:27017/db",
    "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}


def _apply_env(monkeypatch, **overrides):
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))


def test_settings_defaults_and_safe_description(monkeypatch, tmp_path):
    _apply_env(monkeypatch, DATA_DIR=str(tmp_path))
    settings = Settings(_env_file=None)
    assert settings.require_same_account is True
    assert settings.idle_disconnect_seconds == 0
    assert settings.listen_port == 8080
    assert settings.sessions_path == tmp_path / "sessions"
    description = repr(settings.describe())
    for secret in (
        REQUIRED["BOT_TOKEN"],
        REQUIRED["TELEGRAM_API_HASH"],
        REQUIRED["SESSION_ENCRYPTION_KEY"],
        REQUIRED["MONGODB_URI"],
    ):
        assert secret not in description


def test_port_takes_precedence_over_health_port(monkeypatch, tmp_path):
    _apply_env(monkeypatch, DATA_DIR=str(tmp_path), PORT="9090", HEALTH_PORT="8080")
    settings = Settings(_env_file=None)
    assert settings.listen_port == 9090


def test_missing_required_variable_is_rejected(monkeypatch, tmp_path):
    _apply_env(monkeypatch, DATA_DIR=str(tmp_path), BOT_TOKEN=None)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_encryption_key_is_rejected(monkeypatch, tmp_path):
    _apply_env(
        monkeypatch,
        DATA_DIR=str(tmp_path),
        SESSION_ENCRYPTION_KEY="not-a-fernet-key",
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_missing_encryption_key_creates_and_reuses_file(monkeypatch, tmp_path):
    _apply_env(monkeypatch, DATA_DIR=str(tmp_path), SESSION_ENCRYPTION_KEY=None)
    settings = Settings(_env_file=None)
    key = settings.resolve_session_key()

    SessionCipher(key)
    key_path = tmp_path / "session.key"
    assert key_path.read_text("utf-8").strip() == key
    assert settings.resolve_session_key() == key

    # A fresh Settings object (as after a restart) must reuse the same key.
    restarted = Settings(_env_file=None)
    assert restarted.resolve_session_key() == key


def test_example_placeholder_key_falls_back_to_file(monkeypatch, tmp_path):
    _apply_env(
        monkeypatch,
        DATA_DIR=str(tmp_path),
        SESSION_ENCRYPTION_KEY="replace-with-output-of-scripts/generate_key.py",
    )
    settings = Settings(_env_file=None)
    assert settings.session_encryption_key == ""
    key = settings.resolve_session_key()
    SessionCipher(key)
    assert (tmp_path / "session.key").read_text("utf-8").strip() == key


def test_limits_and_extension_normalization(monkeypatch, tmp_path):
    _apply_env(
        monkeypatch,
        DATA_DIR=str(tmp_path),
        VIDEO_EXTENSIONS="MP4, .mkv;webm",
        STREAMABLE_EXTENSIONS="mp4",
        MAX_ACTIVE_USERBOTS="0",
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    monkeypatch.setenv("MAX_ACTIVE_USERBOTS", "5")
    settings = Settings(_env_file=None)
    assert settings.video_extension_set == frozenset({".mp4", ".mkv", ".webm"})
    assert settings.streamable_extension_set == frozenset({".mp4"})


def test_admin_ids_are_parsed_without_crashing(monkeypatch, tmp_path):
    _apply_env(monkeypatch, DATA_DIR=str(tmp_path), ADMIN_IDS="1, 2, nope;3")
    settings = Settings(_env_file=None)
    assert settings.admin_id_set == frozenset({1, 2, 3})
    assert settings.is_admin(2)
    assert not settings.is_admin(9)

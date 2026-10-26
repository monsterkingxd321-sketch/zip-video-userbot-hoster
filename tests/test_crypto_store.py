from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SessionCipher, SessionDecryptError
from app.core.redact import Redactor, mask_phone
from app.sessions.store import OwnershipError, SessionStore


def test_cipher_round_trip_and_fingerprint():
    cipher = SessionCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("1-session-secret")
    assert token != b"1-session-secret"
    assert cipher.decrypt(token) == "1-session-secret"
    assert SessionCipher.fingerprint("abc") == SessionCipher.fingerprint("abc")
    assert SessionCipher.fingerprint("abc") != SessionCipher.fingerprint("abd")


def test_wrong_key_fails_closed():
    first = SessionCipher(Fernet.generate_key().decode())
    second = SessionCipher(Fernet.generate_key().decode())
    token = first.encrypt("session")
    with pytest.raises(SessionDecryptError):
        second.decrypt(token, user_id=1)


@pytest.mark.asyncio
async def test_store_encrypts_volume_and_mongo(repos, tmp_path):
    cipher = SessionCipher(Fernet.generate_key().decode())
    store = SessionStore(tmp_path / "sessions", cipher, repos)
    session = "1" + "A" * 300
    fingerprint = await store.save(
        7,
        session,
        tg_user_id=77,
        dc_id=2,
        phone_masked="+1234***89",
    )
    assert fingerprint
    on_disk = store.session_file(7).read_bytes()
    assert session.encode() not in on_disk
    encrypted_mongo = await repos.sessions.get_encrypted(7)
    assert encrypted_mongo and session.encode() not in encrypted_mongo

    loaded = await store.load(7, requester_id=7)
    assert loaded is not None
    assert loaded.session_string == session
    assert loaded.source == "volume"

    # Removing the volume copy must fall back to the encrypted Mongo backup.
    store.session_file(7).unlink()
    fallback = await store.load(7, requester_id=7)
    assert fallback is not None
    assert fallback.source == "mongo"
    assert store.session_file(7).exists()

    with pytest.raises(OwnershipError):
        await store.load(7, requester_id=8)
    with pytest.raises(OwnershipError):
        await store.delete(7, requester_id=8)

    assert await store.delete(7, requester_id=7) is True
    assert not store.session_file(7).exists()
    assert await store.load(7) is None


def test_redactor_scrubs_common_secrets():
    redactor = Redactor()
    session = "1" + "A" * 300
    api_hash = "0123456789abcdef0123456789abcdef"
    bot_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    text = (
        f"session={session} api_hash={api_hash} token={bot_token} "
        "password=hunter2 mongodb://alice:secret@db.example/db"
    )
    cleaned = redactor.redact(text)
    for secret in (session, api_hash, bot_token, "hunter2", "alice:secret"):
        assert secret not in cleaned
    assert "<redacted" in cleaned


def test_phone_masking_keeps_only_edges():
    masked = mask_phone("+91 98765 43210")
    assert "98765" not in masked
    assert masked.endswith("10")
    assert mask_phone("12") == "<redacted>"

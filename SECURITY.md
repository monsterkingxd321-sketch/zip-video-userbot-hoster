# Security policy

## Reporting a vulnerability

Please **do not open a public issue for a live secret or an exploitable
vulnerability**. Contact the repository owner privately with:

- a short description and impact,
- reproduction steps or a minimal proof of concept,
- affected file/commit and environment details.

Do not include real Telegram session strings, OTPs, passwords, bot tokens, API
hashes, MongoDB URIs or other users' data. Rotate any secret that may have been
exposed before reporting it.

## Threat model

This project handles sensitive Telegram **user-account sessions**. The important
assets are:

1. Telethon session strings (they grant full account access).
2. The Fernet session-encryption key.
3. Telegram bot token and MTProto API hash.
4. MongoDB credentials.
5. Login OTPs and two-step passwords (transient).
6. Users' archives and extracted videos (transient).
7. Per-user job/session isolation.

### Protections in place

- **Encryption at rest:** session strings are Fernet-encrypted before being
  written to the persistent volume and MongoDB. The plaintext exists only in
  process memory. The key is taken from `SESSION_ENCRYPTION_KEY` when configured;
  otherwise a random key is generated once at `<DATA_DIR>/session.key` (mode
  0600). The key is never logged.
- **No session material in UI or admin views:** the database stores a fingerprint
  and masked phone number, not the session string. Admin output is HTML-escaped.
- **Manual phone entry:** the login flow never uses Telegram's `request_contact`
  button and never reads the user's address book. The phone number is typed by the
  user, used for the login request, and stored only in masked form.
- **Log redaction:** every log record passes through a redaction filter covering
  bot tokens, API hashes, Fernet keys, Mongo credentials, phone numbers and
  session-like blobs. Application logs do not include OTPs or passwords.
- **Account ownership boundary:** a userbot worker only acts on outgoing commands
  from its own account; session load/delete accepts an optional requester and
  rejects cross-user access.
- **Banned users:** cannot start workers or transports, queue jobs, or use the
  userbot. Admin bans stop the worker and mark the account disconnected.
- **Quotas and bounded work:** daily job quotas, storage reservations, queue
  limits, concurrency limits and archive limits mitigate resource abuse.
- **Archive safety:** path traversal, absolute paths, symlinks, oversized
  entries, oversized totals, excessive nesting and suspicious compression ratios
  are rejected before/while extracting.
- **Temporary data:** job directories are removed in a `finally` block; stale
  directories are cleaned up on startup/periodically. Temporary-storage
  reservations are reconciled at startup after a hard kill (single-replica
  deployment model).
- **Admin audit:** operator actions are written to an audit collection with the
  acting admin's Telegram ID.

### Trust boundaries and residual risks

- **Environment secrets and the data volume are the root of trust.** Anyone with
  access to the Railway/Docker/Mongo environment or the `/data` volume can read
  the encryption key and database and therefore impersonate connected accounts.
  Use least-privilege deployment accounts, restrict dashboard access, and keep
  the volume private. Setting `SESSION_ENCRYPTION_KEY` keeps the key outside the
  volume; the automatic `/data/session.key` mode stores it beside the local
  session files (the Mongo backup still needs the key).
- **A compromised encryption key** compromises all stored sessions. Rotation is
  not automatic; removing the key or rotating it makes existing sessions
  unreadable, so users must reconnect. Do not rotate by simply changing the env
  var or deleting `session.key` unless you intend that outcome.
- **Telegram itself** can revoke sessions, impose FloodWait or ban accounts.
  Handling these events is best-effort.
- **Malicious archives** are constrained by size/depth/ratio/path checks, but
  decompression and video probing still consume CPU and disk. Keep free-disk and
  quota limits realistic for the host.
- **Admin accounts** are trusted. An admin can ban/unban, disconnect and purge
  users. Protect admin Telegram accounts and `ADMIN_IDS`.
- **The health endpoint** is intentionally unauthenticated for Railway. It exposes
  aggregate metrics, not sessions or user data. Put it behind network controls if
  your threat model requires it.
- **Multi-replica deployments** can duplicate control-bot polling. The project is
  configured for one replica; per-account Mongo locks are not a substitute for a
  distributed Telegram update dispatcher.

## Secret handling rules

- Never commit `.env`, real values in `.env.example`, session files, logs,
  screenshots or database dumps.
- `SESSION_ENCRYPTION_KEY` is optional. If used, generate it once with
  `python scripts/generate_key.py` and store it only in the platform's secret
  store. If omitted, protect the auto-created `<DATA_DIR>/session.key` and the
  persistent volume; never copy the key into the repository or logs.
- Generate a new GitHub token for publishing and **revoke/rotate it immediately
  after use**. Never place it in a remote URL, `.git/config`, a script or a commit.
- If a bot token, API hash, session key or Mongo URI is exposed, rotate it now:
  - Bot token: revoke in BotFather and issue a new one.
  - API hash: reset the app at my.telegram.org if possible.
  - Session key: treat stored sessions as compromised; rotate the key and force
    reconnects.
  - Mongo credentials: rotate the database user/password.
  - GitHub token: revoke in GitHub Settings → Developer settings → Tokens.

## Hardening checklist for deployment

- [ ] Use a dedicated Telegram bot and dedicated API application.
- [ ] Restrict `ADMIN_IDS` to your own account(s).
- [ ] Mount `/data` on a private persistent volume.
- [ ] Use a MongoDB instance with authentication and network isolation.
- [ ] Set a strong, unique `SESSION_ENCRYPTION_KEY`, or protect the auto-created
      `<DATA_DIR>/session.key` on a private persistent volume.
- [ ] Keep TLS between the app and MongoDB where supported.
- [ ] Run one app replica unless you have added distributed update handling.
- [ ] Monitor `/healthz`, `/readyz` and logs for repeated auth/FloodWait errors.
- [ ] Keep `MAX_*`, quota and `MIN_FREE_DISK_BYTES` values appropriate to the host.
- [ ] Back up only what you need; prefer reconnects over exporting session data.
- [ ] Rotate any credential that was ever committed or pasted into a chat.

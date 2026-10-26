# ZIP Video Userbot Hoster

A multi-user Telegram service that turns **ZIP archives into native Telegram videos**.
Each user connects **their own Telegram account** through a Telethon session; the
service then watches that account's outgoing `/unzip` commands, downloads the
replied archive, extracts only video files (including nested ZIPs), and re-sends
each video as a real Telegram video — no re-encoding and no link shorteners.

The control bot is built with **aiogram 3**, sessions and job state live in
**MongoDB**, and every Telegram user-account call is routed through a per-account
scheduler that honours FloodWait and avoids duplicate sends after uncertain
failures.

> **Use only Telegram accounts you own and are allowed to automate.** Running
> userbots can conflict with Telegram's Terms of Service. You are responsible for
> how you deploy this project.

---

## Features

- **ZIP-only video extraction.** A replied `.zip` is scanned safely; only
  configured video extensions are extracted. Non-video files are skipped.
- **Nested ZIP support** with depth, file-size, total-size and compression-ratio
  limits.
- **Native Telegram videos.** Files are uploaded with Telethon's native media
  request, preserving the original bytes/codec. Optional spoiler mode uses
  Telegram's native `spoiler` flag.
- **Per-user isolation.** One session store, worker, queue and temp directory per
  Telegram user. A command is accepted only if it is an outgoing message from that
  worker's own account.
- **Encrypted sessions at rest.** Telethon session strings are Fernet-encrypted on
  the persistent volume and backed up encrypted in MongoDB.
- **Abuse controls.** Per-user daily job quotas, temporary-storage quotas, bounded
  queues, per-user concurrency, max archive/video limits and admin ban controls.
- **FloodWait-aware scheduling.** One scheduler per account; waits exactly as
  Telegram asks, pauses the whole account, and never blindly retries
  non-idempotent sends.
- **Idempotent video delivery.** Every extracted video gets a deterministic
  `random_id`; a resumed job does not re-send a step already recorded as sent.
  Uncertain sends are marked as uncertain, not retried silently.
- **Operational endpoints.** `/healthz`, `/readyz`, `/metrics` and graceful
  shutdown; MongoDB indexes are created at startup.
- **Docker and Railway ready.** Non-root container user, persistent `/data`
  volume, health checks and a single-replica configuration.

---

## How it works

```
Telegram control bot (aiogram)
        │  /start, /connect, /status, /jobs, /cancel, /settings, /admin
        ▼
BotServices ── SessionManager ── encrypted SessionStore ── MongoDB
        │
        └── UserbotManager (one resident worker per connected account)
                 │
                 └── TelethonTransport ── AccountScheduler ── ZIP pipeline
                                                             │
                                              download → extract → upload
```

Job temp layout:

```
/data/sessions/<user_id>/session.enc   # encrypted Telethon session
/data/sessions/<user_id>/meta.json     # masked metadata, fingerprint
/data/tmp/<user_id>/<task_id>/         # archive + extracted videos, removed after job
```

The pipeline always removes its job directory in a `finally` block. A hard kill
can leave stale folders; startup and periodic cleanup remove old temporary data.

---

## Requirements

- Python **3.11+** (the Docker image uses **3.12**)
- MongoDB **6+**
- A Telegram **bot token** from [@BotFather](https://t.me/BotFather)
- Telegram **API ID / API hash** from <https://my.telegram.org>
- A persistent volume for `/data` in production
- One or more Telegram **user accounts** you own (for the userbot sessions)

---

## Quick start (local)

```bash
git clone <this-repository-url>
cd zip-video-userbot-hoster

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements-dev.txt

python scripts/generate_key.py   # put the output in SESSION_ENCRYPTION_KEY
cp .env.example .env             # Windows: copy .env.example .env
# edit .env with your bot token, API credentials, Mongo URI and admin id

# Start MongoDB locally (example):
docker run --rm -p 27017:27017 --name zip-userbot-mongo mongo:7

python -m app.main
```

Then open the control bot, send `/start`, add your Telegram user ID to
`ADMIN_IDS`, and use **Connect Account**. After the account is connected, send a
ZIP document in any chat where that account can post, and reply to it with:

```text
/unzip
/unzip spoiler
/unzip retry
```

`/unzip retry` starts a new attempt for an archive that was processed before. A
new attempt sends the archive's videos again; it is not a "resume one file" flag.

### Tests

The test suite does **not** need Telegram or a real MongoDB server. It uses an
in-memory Mongo-compatible database and fake transports.

```bash
pytest -q
```

Current expected result: **50 passed** (one harmless duplicate-ZIP-name warning).

---

## Configuration

All settings are environment variables. See [`.env.example`](.env.example) for a
complete annotated list. The required ones are:

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | Control-bot token from BotFather |
| `TELEGRAM_API_ID` | MTProto API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | MTProto API hash |
| `MONGODB_URI` | MongoDB connection string |
| `MONGODB_DB` | Database name (default `userbot_hosting`) |
| `SESSION_ENCRYPTION_KEY` | Fernet key generated by `scripts/generate_key.py` |
| `ADMIN_IDS` | Comma-separated Telegram user IDs allowed to use `/admin` |
| `DATA_DIR` | Persistent directory (default `/data`) |

`.env.example` is only a template. Do not copy its example values into Railway
or production. In particular, `SESSION_ENCRYPTION_KEY` has no usable default:
generate one with `python scripts/generate_key.py` and set the full 44-character
output. The app intentionally refuses to start with the placeholder or an
invalid key.

Important operational settings:

- `PORT` — injected by Railway; takes precedence over `HEALTH_PORT`.
- `HEALTH_PORT` — local/Docker fallback (default `8080`).
- `MAX_ACTIVE_USERBOTS` — resident account workers; keep within available RAM.
- `PER_USER_CONCURRENT_JOBS`, `PER_USER_QUEUE_SIZE` — per-account throughput/queue.
- `DEFAULT_STORAGE_QUOTA_BYTES`, `DEFAULT_DAILY_TASK_QUOTA` — quotas; `0` means unlimited.
- `MAX_ZIP_SIZE_BYTES`, `MAX_TOTAL_UNCOMPRESSED_BYTES`, `MAX_SINGLE_FILE_BYTES`,
  `MAX_ZIP_ENTRIES`, `MAX_COMPRESSION_RATIO`, `MAX_ARCHIVE_DEPTH` — archive safety.
- `MAX_VIDEOS_PER_JOB` — `0` sends every extracted video.
- `IDLE_DISCONNECT_SECONDS` — default `0` keeps accounts connected; a positive
  value evicts idle accounts, and `/status` recreates a worker on demand.
- `REQUIRE_SAME_ACCOUNT` — keep `true`; reconnect login must match the known
  account ID. First-time login is still allowed.
- `LOG_JSON` — structured JSON logs (secrets are redacted).

---

## Railway deployment

1. Push this repository to GitHub (public or private).
2. In Railway: **New Project → Deploy from GitHub repo**, then select this repo.
   Railway detects the `Dockerfile` and `railway.json`.
3. Add a **MongoDB** service. Copy its private connection string into the app's
   `MONGODB_URI` variable (use Railway's variable reference so credentials stay
   out of source control).
4. Add a **Volume** to the app service and mount it at **`/data`**.
5. Set the required variables from the table above. Generate
   `SESSION_ENCRYPTION_KEY` locally with `python scripts/generate_key.py` and paste
   only the 44-character value into Railway. Never paste the example placeholder
   from `.env.example`, and do not commit the real key. Changing this key later
   makes existing encrypted sessions unreadable.
6. Leave `PORT` to Railway. Do **not** set `HEALTH_PORT` to a conflicting port.
7. Deploy. Railway checks **`/healthz`**. `/readyz` additionally verifies MongoDB.
8. Open the control bot, send `/start`, then `/connect` and complete the login.
   Send the ZIP to your userbot account and reply with `/unzip`.

Keep **one replica** (`numReplicas: 1`). MongoDB session locks protect account
sessions, but multiple aiogram pollers would compete for control-bot updates.

### Docker Compose

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
docker compose logs -f app
```

Compose starts MongoDB with an internal volume. The example `.env` uses a local
Mongo connection; Compose overrides it with `mongodb://mongo:27017`. The app data
is stored in the `app_data` volume at `/data`.

### Manual Docker

```bash
docker build -t zip-video-userbot-hoster .
docker run -d --name zip-userbot \
  --env-file .env \
  -p 8080:8080 \
  -v zip_userbot_data:/data \
  zip-video-userbot-hoster
```

The container starts as root only long enough to fix ownership of the mounted
volume, then drops to the unprivileged `appuser` via `gosu`.

---

## Commands

### Control bot (private chat)

- `/start` — register and show the dashboard.
- `/status` — account state and capacity; may start an on-demand worker if the
  account was evicted.
- `/jobs` — recent jobs.
- `/settings` — spoiler default, completion notifications and related options.
- `/cancel` — cancel an active job.
- `/help` — command summary.
- `/admin` — operator dashboard (only `ADMIN_IDS`).
  - `/admin health` or `/admin metrics`
  - `/admin jobs`
  - `/admin user <telegram_id>`
  - `/admin quota <telegram_id> <storage_gb> <daily_jobs>`
  - `/admin ban <telegram_id>` / `/admin unban <telegram_id>`

### From the connected user account

Reply to a `.zip` document:

- `/unzip` — normal mode.
- `/unzip spoiler` — native spoiler videos.
- `/unzip retry` — new attempt for an archive processed before.
- `/cancel` — cancel this account's active job(s).

Commands are accepted only when they are **outgoing messages from the same
account** that owns the worker. A command typed by someone else is ignored.

---

## Security model

- **Encryption at rest.** Session strings are encrypted with Fernet before being
  written to the volume or MongoDB. The key exists only in the environment.
  Losing the key makes existing sessions unreadable; reconnecting is required.
- **No plaintext session in admin/UI/logs.** Admins see masked phone numbers and a
  session fingerprint, never the session string.
- **OTP/2FA handling.** Login codes and 2FA passwords are kept in memory only for
  the login flow, removed from chat where permitted, and never logged.
- **Log redaction.** A logging filter scrubs bot tokens, API hashes, Fernet keys,
  Mongo credentials, phone numbers, session strings and `password=`-style values.
- **Ownership checks.** Session loads/deletes take an optional requester and reject
  cross-user access. Userbot commands require the worker's own account and
  outgoing status.
- **Banned users** cannot start workers, create transports, queue jobs or use the
  account. Admin bans also stop the running worker and mark the account
  disconnected.
- **Quotas and cleanup.** Storage reservations are atomic; job directories are
  always removed. Per-user daily quotas limit abuse.
- **Admin audit trail.** Operator actions are recorded with the acting admin ID.

See [SECURITY.md](SECURITY.md) for the threat model and disclosure guidance.

> **Secret hygiene:** never commit `.env`, a real bot token, API hash, session key
> or MongoDB URI. If a secret was ever pasted into a chat, log or commit, revoke
> and rotate it immediately. The GitHub token used to publish this repository is
> not part of the project and should be rotated after use.

---

## What is not covered by local tests

The 50-test suite uses fakes and an in-memory Mongo database. These paths require
a real Telegram account/service and are verified only in a live deployment:

- real OTP / 2FA login and session revocation
- actual FloodWait durations and Telegram rate limiting
- native spoiler rendering and Telegram's real file-size/codec limits
- real network interruptions and uncertain send outcomes
- MongoDB Atlas/Railway networking, volume permissions and health-check timing

The fake transport tests the orchestration, spoiler flag propagation, idempotent
steps, nested extraction, quotas, cleanup, FloodWait backoff and error handling.

---

## Limitations

- **ZIP only.** RAR, 7z, TAR and password-protected ZIPs are rejected.
- **No transcoding.** Videos are sent as-is. If Telegram rejects a container or
  codec, that file fails; install/transcode externally if you need conversion.
- **Telegram limits apply.** A normal account may be limited to 2 GB per file;
  Premium limits can be higher. Configure `MAX_SINGLE_FILE_BYTES` and
  `MAX_ZIP_SIZE_BYTES` below your account's actual limits.
- **One control-bot poller.** Run one replica unless you add a distributed update
  dispatcher. Session locks mitigate cross-replica session use but do not make
  duplicate bot polling safe.
- **Retry re-sends the archive's videos.** New attempts intentionally start with a
  fresh step set; use `/cancel` to stop a job you no longer want.
- **Account automation risk.** Telegram may limit or ban accounts that automate
  aggressively. FloodWait handling reduces, but does not eliminate, this risk.

---

## Project layout

```
app/
  bot/            aiogram control bot (dashboard, connect, admin, texts)
  core/           crypto, metrics, redaction, retry helpers
  db/             MongoDB manager, models, repositories
  sessions/       encrypted store, login state machine, session manager
  userbot/        worker manager, scheduler, pipeline, Telethon transport, commands
  zipengine/      archive safety checks, extraction, video probing
  config.py       environment settings and validation
  health.py       /healthz, /readyz, /metrics
  main.py         startup, recovery, polling, graceful shutdown
scripts/
  generate_key.py
  init_indexes.py
tests/            50 offline tests with fake transports and mongomock
Dockerfile
docker-compose.yml
railway.json
.env.example
```

---

## License

MIT. See [LICENSE](LICENSE).

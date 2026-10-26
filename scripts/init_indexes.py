"""Create the MongoDB indexes used by the service.

Usage:
    python scripts/init_indexes.py

The application also runs this automatically at startup; the script is useful
for pre-provisioning a database or verifying connectivity from a one-off task.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Running ``python scripts/init_indexes.py`` puts ``scripts/`` on sys.path, not
# the repository root. Make the app importable in both invocation styles.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import get_settings
from app.db.mongo import MongoManager


async def _run() -> None:
    settings = get_settings()
    mongo = MongoManager(
        settings.mongodb_uri,
        settings.mongodb_db,
        server_selection_timeout_ms=8000,
    )
    try:
        await mongo.connect()
        print(f"MongoDB indexes ensured for database {settings.mongodb_db!r}.")
    finally:
        await mongo.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()

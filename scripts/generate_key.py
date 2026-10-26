"""Generate a Fernet key for SESSION_ENCRYPTION_KEY.

Usage:
    python scripts/generate_key.py
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode("utf-8"))


if __name__ == "__main__":
    main()

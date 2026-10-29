from pathlib import Path
import secrets

TOKEN_FILE = Path(__file__).resolve().parent / "local.token"

if TOKEN_FILE.exists():
    print(f"Token already exists at: {TOKEN_FILE}")
    print("Keep it private. Delete this file and rerun this script to rotate it.")
else:
    TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    print(f"Created local API token: {TOKEN_FILE}")
    print("This file is ignored by Git. Do not share or commit it.")

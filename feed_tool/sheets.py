"""
NOT CURRENTLY WIRED INTO THE PUBLIC TOOL (streamlit_app.py). The public
"keep this updated daily" flow uses a hosted CSV on GitHub instead — see
DECISIONS.md for why (per-user Google OAuth for a public tool needs
per-user token storage plus Google's app-verification review, neither of
which is worth taking on for what a stable hosted URL already covers).
Kept here unused, parked for a possible future purely-internal Bonded
pipeline where a single shared service account/Sheet is a non-issue.

Pushes the finished feed to a Google Sheet, as an alternative/addition to the
local CSV. Meta Commerce Manager can pull a "connected Google Sheet" as a
scheduled data feed source, same as it does a hosted CSV URL.

Auth: a Google Cloud service account (free tier — no paid Google Workspace
needed). The target Sheet must be shared with the service account's
client_email as an Editor, otherwise the write is rejected.

Deliberately dependency-light: talks to the Sheets API v4 REST endpoints
directly over `requests` and signs its own service-account JWT with the
pure-Python `rsa` package, instead of the official `google-auth`/`gspread`
client libraries. Those pull in `cryptography`, which has no prebuilt wheel
on some platforms (e.g. Windows on ARM) and needs a Rust/MSVC toolchain to
build from source — real friction for a tool meant to run on any machine
with nothing but `pip install`.
"""
from __future__ import annotations

import base64
import json
import time

import requests
import rsa

from .normalize import META_FEED_COLUMNS

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _get_access_token(credentials_path: str) -> str:
    with open(credentials_path, "r", encoding="utf-8") as f:
        creds = json.load(f)

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": creds["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"

    private_key = rsa.PrivateKey.load_pkcs1(creds["private_key"].encode(), format="PEM")
    signature = rsa.sign(signing_input.encode(), private_key, "SHA-256")
    assertion = f"{signing_input}.{_b64url(signature)}"

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def write_to_google_sheet(rows: list[dict], sheet_id: str, credentials_path: str,
                           worksheet_name: str = "Feed") -> None:
    token = _get_access_token(credentials_path)
    headers = {"Authorization": f"Bearer {token}"}

    values = [META_FEED_COLUMNS] + [[row.get(col, "") for col in META_FEED_COLUMNS] for row in rows]

    # Make sure the target worksheet/tab exists (ignore the error if it already does).
    requests.post(
        f"{SHEETS_API}/{sheet_id}:batchUpdate",
        headers=headers,
        json={"requests": [{"addSheet": {"properties": {"title": worksheet_name}}}]},
        timeout=15,
    )

    clear_resp = requests.post(
        f"{SHEETS_API}/{sheet_id}/values/{worksheet_name}:clear",
        headers=headers, timeout=30,
    )
    clear_resp.raise_for_status()

    update_resp = requests.put(
        f"{SHEETS_API}/{sheet_id}/values/{worksheet_name}!A1",
        headers=headers,
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": values},
        timeout=60,
    )
    update_resp.raise_for_status()

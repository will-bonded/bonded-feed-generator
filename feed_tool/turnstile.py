"""
Server-side verification for Cloudflare Turnstile (a free, privacy-friendly
CAPTCHA alternative) — gates the "keep this updated daily" flow so the
public write path (committing to the repo, consuming registration quota)
needs a human, not a script, on the other end.
"""
from __future__ import annotations

import requests

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
REQUEST_TIMEOUT = 15


def verify_turnstile(token: str, secret_key: str, remote_ip: str | None = None) -> bool:
    if not token:
        return False

    data = {"secret": secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        resp = requests.post(SITEVERIFY_URL, data=data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except requests.RequestException:
        return False

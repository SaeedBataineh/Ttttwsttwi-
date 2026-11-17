"""Minimal Twitch Helix client using an app access token (no streamer login needed)."""
from __future__ import annotations

import time
from datetime import datetime

import requests

API = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires = 0.0
        self.session = requests.Session()

    def _get_token(self, force: bool = False) -> str:
        if force or not self._token or time.time() > self._expires - 300:
            r = self.session.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }, timeout=20)
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + data.get("expires_in", 3600)
        return self._token

    def get(self, path: str, params) -> dict:
        for attempt in range(3):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._get_token(attempt > 0)}"}
            r = self.session.get(f"{API}/{path}", params=params, headers=headers, timeout=20)
            if r.status_code == 401:
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, reset - time.time()))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def users(self, logins: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(logins), 100):
            data = self.get("users", [("login", l) for l in logins[i:i + 100]])
            for u in data.get("data", []):
                out[u["login"].lower()] = u
        return out

    def clips(self, broadcaster_id: str, started_at: datetime, ended_at: datetime, limit: int = 100) -> list[dict]:
        """Clips in the window, highest views first.

        Twitch doesn't reliably sort a date range across pages, so we page
        through up to `limit` clips and sort them ourselves.
        """
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            params = {
                "broadcaster_id": broadcaster_id,
                "started_at": rfc3339(started_at),
                "ended_at": rfc3339(ended_at),
                "first": 100,
            }
            if cursor:
                params["after"] = cursor
            data = self.get("clips", params)
            page = data.get("data", [])
            out.extend(page)
            cursor = (data.get("pagination") or {}).get("cursor")
            if not page or not cursor:
                break
        seen: set[str] = set()
        uniq = [c for c in out if not (c["id"] in seen or seen.add(c["id"]))]
        uniq.sort(key=lambda c: c.get("view_count", 0), reverse=True)
        return uniq[:limit]

    def games(self, ids: list[str]) -> dict[str, str]:
        ids = [i for i in dict.fromkeys(ids) if i]
        out: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            data = self.get("games", [("id", g) for g in ids[i:i + 100]])
            for g in data.get("data", []):
                out[g["id"]] = g["name"]
        return out

"""TikTok upload (Content Posting API) - placeholder until the TikTok developer app is approved.

All TikTok traffic will go through `platforms.tiktok.proxy` from config.yaml when set,
so TikTok can be reached from countries where it is blocked without a system-wide VPN.
"""
from __future__ import annotations

from pathlib import Path


def session(proxy: str | None):
    import requests

    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def check_reachable(proxy: str | None) -> str:
    """Quick test that TikTok's API host answers through the configured proxy."""
    r = session(proxy).get("https://open.tiktokapis.com/", timeout=15)
    return f"TikTok reachable (HTTP {r.status_code})"


def upload(video: Path, title: str, proxy: str | None = None) -> str:
    raise NotImplementedError("TikTok upload isn't built yet - keep platforms.tiktok.enabled: false")

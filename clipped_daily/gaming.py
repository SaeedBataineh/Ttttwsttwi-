"""Gaming mode: pause rendering while you play."""
from __future__ import annotations

from .db import DB
from .settings import Settings

KEY = "gaming_mode"  # "auto" | "on" | "off"


def get_mode(db: DB) -> str:
    return db.kv_get(KEY, "auto")


def set_mode(db: DB, mode: str) -> None:
    db.kv_set(KEY, mode)


def running_game(settings: Settings) -> str | None:
    cfg = settings.get("gaming_mode", {}) or {}
    if not cfg.get("auto_detect", True):
        return None
    names = {p.lower() for p in cfg.get("processes", [])}
    if not names:
        return None
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name in names:
            return proc.info["name"]
    return None


def is_gaming(settings: Settings, db: DB) -> tuple[bool, str]:
    mode = get_mode(db)
    if mode == "on":
        return True, "gaming mode is ON"
    if mode == "off":
        return False, "gaming mode is OFF"
    game = running_game(settings)
    return (True, f"detected {game}") if game else (False, "no game detected")

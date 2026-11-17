"""Loads config.yaml, streamers.yaml and .env into one Settings object."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Streamer:
    login: str
    name: str
    group: str | None = None
    facecam: dict | None = None
    allowed: bool = True

    def to_dict(self) -> dict:
        d = {"login": self.login, "name": self.name, "group": self.group, "facecam": self.facecam}
        if not self.allowed:
            d["allowed"] = False
        return d


@dataclass
class Settings:
    config: dict
    streamers: list[Streamer]
    root: Path = ROOT
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- paths ---
    @property
    def data_dir(self) -> Path:
        p = self.root / "data"
        p.mkdir(exist_ok=True)
        return p

    @property
    def work_dir(self) -> Path:
        p = self.root / "work"
        p.mkdir(exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipped_daily.db"

    @property
    def streamers_path(self) -> Path:
        return self.root / "streamers.yaml"

    # --- config helpers ---
    def get(self, key: str, default=None):
        return self.config.get(key, default)

    @property
    def render(self) -> dict:
        return self.config.get("render", {})

    @property
    def platforms(self) -> dict:
        return self.config.get("platforms", {})

    @property
    def tz(self) -> tzinfo:
        name = self.config.get("timezone", "local")
        if not name or name == "local":
            return datetime.now().astimezone().tzinfo
        return ZoneInfo(name)

    @staticmethod
    def env(name: str, required: bool = True) -> str:
        val = os.getenv(name, "").strip()
        if required and not val:
            raise RuntimeError(f"Missing {name} in .env (see .env.example)")
        return val

    # --- streamers ---
    def streamer(self, login: str) -> Streamer | None:
        login = login.lower()
        return next((s for s in self.streamers if s.login == login), None)

    def active_streamers(self) -> list[Streamer]:
        return [s for s in self.streamers if s.allowed]

    def save_streamers(self) -> None:
        with self._lock:
            header = ""
            if self.streamers_path.exists():
                lines = self.streamers_path.read_text(encoding="utf-8").splitlines(keepends=True)
                for line in lines:
                    if line.startswith("#") or not line.strip():
                        header += line
                    else:
                        break
            body = yaml.safe_dump(
                {"streamers": [s.to_dict() for s in self.streamers]},
                sort_keys=False, allow_unicode=True,
            )
            self.streamers_path.write_text(header + body, encoding="utf-8")


def load(root: Path = ROOT) -> Settings:
    load_dotenv(root / ".env")
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    raw = yaml.safe_load((root / "streamers.yaml").read_text(encoding="utf-8")) or {}
    streamers = []
    for s in raw.get("streamers") or []:
        streamers.append(Streamer(
            login=str(s["login"]).lower(),
            name=s.get("name") or s["login"],
            group=s.get("group"),
            facecam=s.get("facecam"),
            allowed=s.get("allowed", True),
        ))
    return Settings(config=config, streamers=streamers, root=root)

"""YouTube Shorts upload via the YouTube Data API v3.

One-time setup: `python -m clipped_daily auth-youtube` opens a browser to log in
to the Clipped Daily YouTube channel and saves data/youtube_token.json.
"""
from __future__ import annotations

from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def token_path(data_dir: Path) -> Path:
    return data_dir / "youtube_token.json"


def authorize(client_secret: Path, data_dir: Path) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path(data_dir).write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel found on this account)"


def _credentials(data_dir: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(data_dir)
    if not path.exists():
        raise RuntimeError("YouTube not connected. Run: python -m clipped_daily auth-youtube")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("YouTube login expired. Run: python -m clipped_daily auth-youtube")
    return creds


def upload(video: Path, title: str, description: str, tags: list[str], data_dir: Path,
           privacy: str = "public", category_id: str = "20") -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=_credentials(data_dir), cache_discovery=False)
    if "#shorts" not in title.lower() and len(title) <= 92:
        title = f"{title} #shorts"
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:15],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return f"https://youtube.com/shorts/{response['id']}"

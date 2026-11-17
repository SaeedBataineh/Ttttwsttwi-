"""ffmpeg rendering: 16:9 Twitch clip -> 1080x1920 vertical video (+ small Discord preview).

Encoding uses NVENC (the RTX card's dedicated encoder chip) when available, so it
barely touches the CPU or game FPS. Filters run on CPU; they are cheap at this size.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

W, H = 1080, 1920
FG_H = 608                     # height of the 16:9 video in the blur layout
DISCORD_LIMIT_BYTES = 9_500_000


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install it (see README) and restart the terminal.")
    return path


def _run(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          creationflags=_low_priority_flag())
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{tail}")


def _low_priority_flag() -> int:
    # Run ffmpeg below normal priority on Windows so games always win.
    return getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


@lru_cache(maxsize=1)
def has_nvenc() -> bool:
    try:
        _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
              "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"])
        return True
    except Exception:
        return False


def probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 60.0


def layout_positions(layout: str, facecam_height: int = 640) -> dict:
    """Where captions / hook text go for each layout (in 1080x1920 coordinates)."""
    if layout == "facecam":
        return {"caption_y": facecam_height + 260, "hook_y": None}
    top = (H - FG_H) // 2
    return {"caption_y": top + FG_H + 170, "hook_y": top - 170}


def build_filter(layout: str, facecam: dict | None = None, ass_file: str | None = None,
                 facecam_height: int = 640) -> str:
    if layout == "facecam":
        if not facecam:
            raise ValueError("facecam layout needs a facecam box")
        fx, fy, fw, fh = (float(facecam[k]) for k in ("x", "y", "w", "h"))
        game_h = H - facecam_height
        parts = [
            "[0:v]split=2[a][b]",
            f"[a]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
            f"scale={W}:{facecam_height}:force_original_aspect_ratio=increase,"
            f"crop={W}:{facecam_height},setsar=1[top]",
            f"[b]crop=ih*{W}/{game_h}:ih:(iw-ih*{W}/{game_h})/2:0,scale={W}:{game_h},setsar=1[bot]",
            "[top][bot]vstack=inputs=2[v0]",
        ]
    else:
        parts = [
            "[0:v]split=2[a][b]",
            # Blur a tiny copy then scale up: looks the same, ~20x cheaper than blurring at 1080p.
            f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=8:2,eq=brightness=-0.08,scale={W}:{H},setsar=1[bg]",
            f"[b]scale={W}:{FG_H}:force_original_aspect_ratio=decrease,setsar=1[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]",
        ]
    if ass_file:
        parts.append(f"[v0]ass={ass_file},format=yuv420p[v]")
    else:
        parts.append("[v0]format=yuv420p[v]")
    return ";".join(parts)


def _video_codec_args(use_nvenc: bool, bitrate: str | None = None) -> list[str]:
    if use_nvenc:
        if bitrate:
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", bitrate, "-maxrate", bitrate,
                    "-bufsize", bitrate]
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "10M",
                "-maxrate", "14M", "-bufsize", "20M", "-profile:v", "high"]
    if bitrate:
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate, "-maxrate", bitrate,
                "-bufsize", bitrate]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"]


def _want_nvenc(encoder: str) -> bool:
    if encoder == "cpu":
        return False
    if encoder == "nvenc":
        return True
    return has_nvenc()


def render_vertical(src: Path, out: Path, *, layout: str = "blur", facecam: dict | None = None,
                    ass_path: Path | None = None, encoder: str = "auto", facecam_height: int = 640) -> Path:
    """Render the final 1080x1920 video. Runs inside the clip's folder so the .ass path needs no escaping."""
    workdir = out.parent
    ass_name = None
    if ass_path:
        ass_name = ass_path.name
        if ass_path.parent.resolve() != workdir.resolve():
            shutil.copy(ass_path, workdir / ass_name)
    fc = build_filter(layout, facecam, ass_name, facecam_height)
    use_nvenc = _want_nvenc(encoder)

    def attempt(nv: bool) -> None:
        _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()),
              "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
              *_video_codec_args(nv), "-r", "30",
              "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
              "-movflags", "+faststart", out.name], cwd=workdir)

    try:
        attempt(use_nvenc)
    except RuntimeError:
        if not use_nvenc:
            raise
        log.warning("NVENC render failed, retrying with CPU encoder")
        attempt(False)
    return out


def render_preview(final: Path, out: Path, encoder: str = "auto") -> Path:
    """Small copy for Discord (non-boosted servers allow ~10 MB uploads)."""
    duration = max(probe_duration(final), 1.0)
    kbps = int(min(1800, (DISCORD_LIMIT_BYTES * 8 / duration) / 1000 - 96))
    kbps = max(kbps, 250)
    use_nvenc = _want_nvenc(encoder)

    def attempt(nv: bool) -> None:
        _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(final),
              "-vf", "scale=540:960", *_video_codec_args(nv, f"{kbps}k"),
              "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)])

    try:
        attempt(use_nvenc)
    except RuntimeError:
        if not use_nvenc:
            raise
        attempt(False)
    return out


def snapshot_grid(src: Path, out: Path, at: float = 2.0) -> Path:
    """Save one frame with a 10% grid so you can read off the facecam box (x, y, w, h)."""
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(src),
          "-frames:v", "1", "-vf", "drawgrid=w=iw/10:h=ih/10:t=2:c=red@0.8", str(out)])
    return out

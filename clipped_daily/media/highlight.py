"""Find the best part of a long clip so Shorts stay tight.

Heuristic (no extra AI needed):
- The climax of a stream moment is usually the LOUDEST part (yelling, laughing, chat going off).
- Clips are made right AFTER something happens, so the moment tends to be near the end.
We keep the build-up before the loudest point plus a few seconds after it, and snap
the cut points to pauses in speech so nobody is cut off mid-sentence.
"""
from __future__ import annotations

import numpy as np

SR = 16000


def loudness(audio: np.ndarray, hop: float = 0.25) -> np.ndarray:
    """RMS loudness per `hop` seconds, smoothed over ~1 second."""
    n = int(SR * hop)
    frames = len(audio) // n
    if frames == 0:
        return np.zeros(0)
    rms = np.sqrt(np.mean(audio[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-12)
    k = max(1, int(1.0 / hop))
    return np.convolve(rms, np.ones(k) / k, mode="same")


def _snap(t: float, words: list[tuple[float, float, str]], direction: str, reach: float = 3.0) -> float:
    """Move a cut point to the nearest gap between words (within `reach` seconds)."""
    if not words:
        return t
    if direction == "start":
        # Cut just before a word that starts a phrase, at or before t (never move later than t + 1).
        best = None
        for i, (s, _, _) in enumerate(words):
            gap_before = s - words[i - 1][1] if i else s
            if t - reach <= s <= t + 1 and gap_before >= 0.25:
                if best is None or abs(s - t) < abs(best - t):
                    best = s
        if best is not None:
            return max(0.0, best - 0.15)
        inside = [w for w in words if w[0] < t < w[1]]
        return inside[0][0] if inside else t
    best = None
    for i, (_, e, _) in enumerate(words):
        gap_after = words[i + 1][0] - e if i + 1 < len(words) else 9.0
        if t - 1 <= e <= t + reach and gap_after >= 0.25:
            if best is None or abs(e - t) < abs(best - t):
                best = e
    if best is not None:
        return best + 0.3
    inside = [w for w in words if w[0] < t < w[1]]
    return inside[0][1] + 0.2 if inside else t


def find_window(audio: np.ndarray, duration: float, words: list[tuple[float, float, str]],
                max_len: float = 35.0, after: float = 4.0) -> tuple[float, float]:
    """Return (start, end) seconds of the best part. Whole clip if it's already short."""
    if duration <= max_len:
        return 0.0, duration
    hop = 0.25
    loud = loudness(audio, hop)
    if loud.size == 0:
        return max(0.0, duration - max_len), duration
    # Favour later moments a little (clips are made right after the moment).
    t = np.arange(loud.size) * hop
    weighted = loud * (0.75 + 0.25 * (t / max(duration, 1.0)))
    # Ignore the first few seconds: a peak there leaves no build-up.
    weighted[: int(min(8.0, duration / 4) / hop)] = 0
    peak = float(np.argmax(weighted) * hop)

    end = min(duration, peak + after)
    start = max(0.0, end - max_len)
    if start == 0.0:
        end = min(duration, max_len)
    start = _snap(start, words, "start")
    end = min(duration, _snap(end, words, "end"))
    if end - start < 8:
        start, end = max(0.0, duration - max_len), duration
    # Hard cap: never longer than max_len. Keep the end (the climax/reaction), drop extra build-up.
    if end - start > max_len:
        start = end - max_len
    return round(start, 2), round(end, 2)


def shift_words(words: list[tuple[float, float, str]], start: float, end: float) -> list[tuple[float, float, str]]:
    """Words inside the window, re-timed so the window starts at 0."""
    return [(max(0.0, s - start), min(end, e) - start, w) for s, e, w in words if e > start and s < end]

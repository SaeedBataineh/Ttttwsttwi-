"""Speech-to-text with faster-whisper (GPU when available) and ASS subtitle generation."""
from __future__ import annotations

import gc
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_model = None
_model_key: tuple | None = None


def _load(model_name: str, device: str):
    global _model, _model_key
    from faster_whisper import WhisperModel

    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    key = (model_name, device)
    if _model is None or _model_key != key:
        compute = "float16" if device == "cuda" else "int8"
        log.info("Loading Whisper %s on %s (%s)", model_name, device, compute)
        _model = WhisperModel(model_name, device=device, compute_type=compute)
        _model_key = key
    return _model, device


def unload() -> None:
    """Free GPU memory (call after a render batch so games get the VRAM back)."""
    global _model, _model_key
    _model, _model_key = None, None
    gc.collect()


def transcribe(path: Path, model_name: str = "small", device: str = "auto") -> list[tuple[float, float, str]]:
    """Return word-level timestamps [(start, end, word)]."""
    model, used = _load(model_name, device)
    try:
        segments, _ = model.transcribe(str(path), language="en", word_timestamps=True, vad_filter=True)
        words = [(w.start, w.end, w.word.strip()) for seg in segments for w in (seg.words or [])]
    except Exception as e:
        if used != "cuda":
            raise
        # Usually missing cuBLAS/cuDNN DLLs on Windows. Fall back to CPU so the clip still renders.
        log.warning("GPU transcription failed (%s); retrying on CPU", e)
        model, _ = _load(model_name, "cpu")
        segments, _ = model.transcribe(str(path), language="en", word_timestamps=True, vad_filter=True)
        words = [(w.start, w.end, w.word.strip()) for seg in segments for w in (seg.words or [])]
    return [w for w in words if w[2]]


def chunk_words(words: list[tuple[float, float, str]], max_words: int = 3, max_gap: float = 0.6,
                max_len: float = 1.2) -> list[tuple[float, float, str]]:
    """Group words into short punchy caption chunks."""
    chunks: list[tuple[float, float, str]] = []
    cur: list[tuple[float, float, str]] = []
    for w in words:
        if cur and (len(cur) >= max_words or w[0] - cur[-1][1] > max_gap or w[1] - cur[0][0] > max_len):
            chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
    # Close tiny gaps so captions don't flicker.
    out = []
    for i, (s, e, t) in enumerate(chunks):
        if i + 1 < len(chunks) and chunks[i + 1][0] - e < 0.3:
            e = chunks[i + 1][0]
        out.append((s, e, t))
    return out


def _ts(t: float) -> str:
    t = max(t, 0.0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(out: Path, chunks: list[tuple[float, float, str]], *, caption_y: int, duration: float,
              hook: str | None = None, hook_y: int = 400, watermark: str | None = None) -> Path:
    """Write a 1080x1920 ASS subtitle file with captions, optional hook title and watermark."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Colours are &HAABBGGRR
        "Style: Caption,Arial,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1",
        "Style: Hook,Arial,64,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,5,90,90,0,1",
        "Style: Mark,Arial,34,&H90FFFFFF,&H90FFFFFF,&H90000000,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,8,0,0,50,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end = _ts(duration + 1)
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{end},Hook,,0,0,0,,{{\\pos(540,{hook_y})}}{_esc(hook)}")
    if watermark:
        lines.append(f"Dialogue: 0,{_ts(0)},{end},Mark,,0,0,0,,{_esc(watermark)}")
    yellow = "{\\c&H00F7FF&}"  # highlight colour for the last word of each chunk
    for s, e, text in chunks:
        words = _esc(text).upper().split()
        if len(words) > 1:
            words[-1] = yellow + words[-1]
        body = " ".join(words)
        # Small pop-in animation.
        anim = "{\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)}"
        lines.append(f"Dialogue: 2,{_ts(s)},{_ts(e)},Caption,,0,0,0,,{{\\pos(540,{caption_y})}}{anim}{body}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out

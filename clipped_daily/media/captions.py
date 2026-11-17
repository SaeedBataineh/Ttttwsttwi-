"""Speech-to-text with faster-whisper (GPU when available) and ASS subtitle generation."""
from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


_DLL_HANDLES: list = []     # keep references so Windows never unloads them
CUDA_DLLS: list[str] = []   # what got loaded, for `doctor`


def _nvidia_dirs() -> list[Path]:
    import site

    bases = list(sys.path)
    try:
        bases += site.getsitepackages() + [site.getusersitepackages()]
    except Exception:
        pass
    bases.append(str(Path(sys.prefix) / "Lib" / "site-packages"))
    out: list[Path] = []
    for base in dict.fromkeys(bases):
        nv = Path(base) / "nvidia"
        if not nv.is_dir():
            continue
        for sub in ("cuda_runtime", "cuda_nvrtc", "cublas", "cudnn"):
            d = nv / sub / "bin"
            if d.is_dir() and d not in out:
                out.append(d)
    return out


def register_cuda_dlls() -> list[str]:
    """On Windows, load cuBLAS/cuDNN from the pip packages (nvidia-cublas-cu12,
    nvidia-cudnn-cu12) by FULL PATH so faster-whisper always finds them.

    Adding folders to PATH alone isn't reliable on Windows; a DLL that is already
    loaded in the process is always found by name.
    """
    if os.name != "nt":
        return []
    import ctypes

    dirs = _nvidia_dirs()
    for d in dirs:
        try:
            _DLL_HANDLES.append(os.add_dll_directory(str(d)))
        except OSError:
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    wanted = ("cudart64", "nvrtc64", "cublasLt64", "cublas64", "cudnn64", "cudnn_")
    for d in dirs:
        # cublasLt before cublas, cudnn64 before its sub-libraries
        for dll in sorted(d.glob("*.dll"), key=lambda f: (not f.name.startswith(("cublasLt", "cudnn64")), f.name)):
            if dll.name.startswith(wanted) and dll.name not in CUDA_DLLS:
                try:
                    _DLL_HANDLES.append(ctypes.WinDLL(str(dll)))
                    CUDA_DLLS.append(dll.name)
                except OSError as e:
                    log.debug("Could not preload %s: %s", dll, e)
    if dirs and "cublas64_12.dll" not in CUDA_DLLS:
        log.warning("CUDA folders found but cublas64_12.dll didn't load: %s", ", ".join(map(str, dirs)))
    return CUDA_DLLS


register_cuda_dlls()


def gpu_selftest(model_name: str = "tiny") -> str:
    """Actually run Whisper on the GPU for a moment (this is when missing DLLs show up)."""
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cuda", compute_type="float16")
    audio = (np.random.default_rng(0).standard_normal(16000 * 2) * 0.01).astype("float32")
    segments, _ = model.transcribe(audio, language="en", vad_filter=False)
    list(segments)
    del model
    gc.collect()
    return "Whisper ran on the GPU"

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


def load_audio(path: Path, sr: int = 16000):
    """Decode audio to 16 kHz mono float32 with ffmpeg (avoids PyAV version breakage)."""
    import subprocess

    import numpy as np

    from .render import ffmpeg_bin

    proc = subprocess.run([ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio decode failed: {proc.stderr.decode(errors='ignore')[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


BEAM_SIZE = 1  # 1 = greedy decoding: ~2-3x faster than the default 5, nearly as accurate for captions


def _run(model, audio) -> list[tuple[float, float, str]]:
    segments, _ = model.transcribe(audio, language="en", word_timestamps=True, vad_filter=True,
                                   beam_size=BEAM_SIZE, condition_on_previous_text=False)
    return [(w.start, w.end, w.word.strip()) for seg in segments for w in (seg.words or [])]


def transcribe(path: Path, model_name: str = "small", device: str = "auto", audio=None) -> list[tuple[float, float, str]]:
    """Return word-level timestamps [(start, end, word)]."""
    if audio is None:
        audio = load_audio(path)
    if audio.size == 0:
        return []
    model, used = _load(model_name, device)
    try:
        words = _run(model, audio)
    except Exception as e:
        if used != "cuda":
            raise
        # Usually missing cuBLAS/cuDNN DLLs on Windows. Fall back to CPU so the clip still renders.
        log.warning("GPU transcription failed (%s); retrying on CPU", e)
        model, _ = _load(model_name, "cpu")
        words = _run(model, audio)
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

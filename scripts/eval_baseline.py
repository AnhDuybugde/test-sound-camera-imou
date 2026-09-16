#!/usr/bin/env python3
"""P0 baseline: do dac trung am thanh luu trong audio/*.wav (offline, stdlib+vosk).

Do cho moi file:
  - duration, sample_rate, channels, DC offset, peak, clip %
  - RMS per 30ms: p10/p50/p90/max, % frame vuot nguong VAD hien tai (0.03)
  - mo phong VAD giong imou_rtsp_listen (min_speech_ms=300, silence_ms=800)
    -> so segment, mean RMS moi segment
  - chay Vosk-small-vn tren tung segment + full file (neu co model)

Usage (Ubuntu):
  python3 scripts/eval_baseline.py
  python3 scripts/eval_baseline.py --corpus audio --model models/vosk-model-small-vn-0.4
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "imou_talk"))

TARGET_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = TARGET_RATE * FRAME_MS // 1000 * 2


def decode_to_pcm16k(path: Path) -> tuple[bytes, int]:
    """Decode bat ky wav/codec nao ve PCM s16le mono 16k bang ffmpeg (neu can)."""
    try:
        with wave.open(str(path), "rb") as w:
            nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
            if nch == 1 and sw == 2 and sr == TARGET_RATE:
                return raw, sr
    except wave.Error:
        pass
    # fallback: ffmpeg resample
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-i", str(path), "-ac", "1", "-ar", str(TARGET_RATE),
           "-f", "s16le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed for {path}: {out.stderr.decode()[:300]}")
    return out.stdout, TARGET_RATE


def frame_stats(pcm: bytes) -> dict:
    n_frames = len(pcm) // FRAME_BYTES
    rms_list: list[float] = []
    dc_sum = 0
    dc_n = 0
    peak = 0
    clip = 0
    total = 0
    for f in range(n_frames):
        fr = pcm[f * FRAME_BYTES:(f + 1) * FRAME_BYTES]
        s_sum = 0
        sq = 0
        n = len(fr) // 2
        for i in range(0, len(fr) - 1, 2):
            s = struct.unpack_from("<h", fr, i)[0]
            s_sum += s
            sq += s * s
            if abs(s) > peak:
                peak = abs(s)
            if abs(s) >= 32760:
                clip += 1
            total += 1
        dc_sum += s_sum
        dc_n += n
        rms_list.append(math.sqrt(sq / max(1, n)) / 32768.0)
    rms_list.sort()
    def pct(q: float) -> float:
        if not rms_list:
            return 0.0
        return rms_list[min(len(rms_list) - 1, int(q * len(rms_list)))]
    return {
        "n_frames": n_frames,
        "rms_p10": round(pct(0.10), 5),
        "rms_p50": round(pct(0.50), 5),
        "rms_p90": round(pct(0.90), 5),
        "rms_max": round(pct(0.999), 5),
        "vad_hit_003": round(sum(1 for r in rms_list if r >= 0.03) / max(1, len(rms_list)), 4),
        "dc_offset": round((dc_sum / max(1, dc_n)) / 32768.0, 6),
        "peak": peak,
        "clip_ratio": round(clip / max(1, total), 6),
    }


def simulate_vad(pcm: bytes, thr=0.03, min_speech_ms=300, silence_ms=800):
    """Mo phong dung logic imou_rtsp_listen: onset sau min_speech, dong sau silence."""
    n_frames = len(pcm) // FRAME_BYTES
    speaking = False
    voiced_ms = 0
    silence = 0
    seg_start = 0
    segments: list[tuple[int, int]] = []
    for f in range(n_frames):
        fr = pcm[f * FRAME_BYTES:(f + 1) * FRAME_BYTES]
        n = len(fr) // 2
        sq = 0
        for i in range(0, len(fr) - 1, 2):
            s = struct.unpack_from("<h", fr, i)[0]
            sq += s * s
        level = math.sqrt(sq / max(1, n)) / 32768.0
        speech = level >= thr
        if not speaking:
            if speech:
                if voiced_ms == 0:
                    seg_start = f
                voiced_ms += FRAME_MS
                if voiced_ms >= min_speech_ms:
                    speaking = True
                    silence = 0
            else:
                voiced_ms = 0
        else:
            if speech:
                silence = 0
            else:
                silence += FRAME_MS
                if silence >= silence_ms:
                    segments.append((seg_start, f))
                    speaking = False
                    voiced_ms = 0
    if speaking:
        segments.append((seg_start, n_frames))
    return segments


def transcribe(pcm: bytes, model) -> str:
    import json as _json
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(model, TARGET_RATE)
    rec.SetWords(False)
    for i in range(0, len(pcm), 4000):
        rec.AcceptWaveform(pcm[i:i + 4000])
    try:
        return _json.loads(rec.FinalResult()).get("text", "").strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="audio")
    ap.add_argument("--model", default="models/vosk-model-small-vn-0.4")
    ap.add_argument("--no-stt", action="store_true")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_absolute():
        corpus = REPO_ROOT / corpus
    wavs = sorted(corpus.glob("*.wav"))
    if not wavs:
        print(f"no wav in {corpus}", file=sys.stderr)
        return 1

    model = None
    if not args.no_stt:
        try:
            from vosk import Model
            mp = Path(args.model)
            if not mp.is_absolute():
                mp = REPO_ROOT / mp
            if mp.exists():
                print(f"Loading Vosk {mp} ...", flush=True)
                model = Model(str(mp))
            else:
                print(f"model not found {mp}, skip STT", file=sys.stderr)
        except ImportError:
            print("vosk not installed, skip STT", file=sys.stderr)

    report = []
    for wav in wavs:
        pcm, sr = decode_to_pcm16k(wav)
        dur = len(pcm) / TARGET_RATE / 2
        st = frame_stats(pcm)
        segs = simulate_vad(pcm)
        seg_infos = []
        for si, (a, b) in enumerate(segs):
            seg_pcm = pcm[a * FRAME_BYTES:b * FRAME_BYTES]
            n = len(seg_pcm) // 2
            sq = 0
            for i in range(0, len(seg_pcm) - 1, 2):
                s = struct.unpack_from("<h", seg_pcm, i)[0]
                sq += s * s
            mean_rms = math.sqrt(sq / max(1, n)) / 32768.0
            text = transcribe(seg_pcm, model) if model is not None else "(stt off)"
            seg_infos.append({
                "seg": si + 1,
                "t_start": round(a * FRAME_MS / 1000, 2),
                "dur_s": round((b - a) * FRAME_MS / 1000, 2),
                "mean_rms": round(mean_rms, 5),
                "text": text,
            })
        full_text = transcribe(pcm, model) if model is not None else "(stt off)"
        entry = {
            "file": wav.name, "duration_s": round(dur, 2),
            **st,
            "n_vad_segments": len(segs), "segments": seg_infos,
            "vosk_full": full_text,
        }
        report.append(entry)
        print(f"\n== {wav.name} ({dur:.1f}s) ==")
        print(f"   DC={st['dc_offset']} peak={st['peak']} clip={st['clip_ratio']}")
        print(f"   RMS p10/p50/p90/max = {st['rms_p10']}/{st['rms_p50']}/{st['rms_p90']}/{st['rms_max']}")
        print(f"   VAD>=0.03: {st['vad_hit_003']*100:.1f}% frames, segments={len(segs)}")
        for s in seg_infos:
            print(f"   seg{s['seg']} t={s['t_start']}s dur={s['dur_s']}s rms={s['mean_rms']} text={s['text']!r}")
        print(f"   FULL: {full_text!r}")

    out = REPO_ROOT / "audio" / f"baseline-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

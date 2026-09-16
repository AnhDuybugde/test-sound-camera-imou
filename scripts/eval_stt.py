#!/usr/bin/env python3
"""P4: so sanh Vosk-small-vn vs faster-whisper offline tren audio/*.wav.

Ubuntu: pip3 install --user faster-whisper (da cai). Model tai lan dau
ve ~/.cache/huggingface (tiny ~75MB, small ~466MB, can mang).

Usage:
  python3 scripts/eval_stt.py
  python3 scripts/eval_stt.py --corpus audio --fw-models tiny,small --lang vi
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "imou_talk"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from eval_baseline import decode_to_pcm16k  # noqa: E402
from imou_audio_pre import preprocess_segment  # noqa: E402

TARGET_RATE = 16000


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def vosk_text(pcm: bytes, model) -> tuple[str, float]:
    import json as _json
    from vosk import KaldiRecognizer
    t0 = time.monotonic()
    rec = KaldiRecognizer(model, TARGET_RATE)
    rec.SetWords(False)
    for i in range(0, len(pcm), 4000):
        rec.AcceptWaveform(pcm[i:i + 4000])
    try:
        text = _json.loads(rec.FinalResult()).get("text", "").strip()
    except Exception:
        text = ""
    return text, time.monotonic() - t0


def fw_text(pcm: bytes, fw_model, lang: str) -> tuple[str, float]:
    import tempfile
    t0 = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        with wave.open(f.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_RATE)
            w.writeframes(pcm)
        tmp = f.name
    try:
        segments, _info = fw_model.transcribe(tmp, language=lang, beam_size=5,
                                              vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
    finally:
        Path(tmp).unlink(missing_ok=True)
    return text, time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="audio")
    ap.add_argument("--vosk-model", default="",
                    help="duong dan model Vosk; de trong = bo qua Vosk")
    ap.add_argument("--fw-models", default="small,medium")
    ap.add_argument("--lang", default="vi")
    ap.add_argument("--no-agc", action="store_true")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_absolute():
        corpus = REPO_ROOT / corpus
    wavs = sorted(corpus.glob("*.wav"))
    wavs = [w for w in wavs if not w.name.startswith(("baseline-", "eval-")) and w.stat().st_size > 1000]
    if not wavs:
        print(f"no wav in {corpus}", file=sys.stderr)
        return 1

    from vosk import Model
    vosk_model = None
    if Path(args.vosk_model).name:
        vmp = Path(args.vosk_model)
        if not vmp.is_absolute():
            vmp = REPO_ROOT / vmp
        print(f"Loading Vosk {vmp} ...", flush=True)
        vosk_model = Model(str(vmp))

    from faster_whisper import WhisperModel
    fw_loaded = {}
    for name in [m.strip() for m in args.fw_models.split(",") if m.strip()]:
        print(f"Loading faster-whisper {name} (lan dau se tai model) ...", flush=True)
        fw_loaded[name] = WhisperModel(name, device="cpu", compute_type="int8")

    report = []
    for wav in wavs:
        raw_pcm, _ = decode_to_pcm16k(wav)
        pcm, q, rej = preprocess_segment(raw_pcm, use_agc=not args.no_agc)
        dur = len(pcm) / TARGET_RATE / 2
        row: dict = {"file": wav.name, "dur_s": round(dur, 2),
                     "quality": q, "rejected": rej}
        print(f"\n== {wav.name} ({dur:.1f}s, rms={q['rms']} snr={q['snr_db']}dB) ==")
        if vosk_model is not None:
            vt, vdt = vosk_text(pcm, vosk_model)
            row["vosk_small"] = {"text": vt, "sec": round(vdt, 2),
                                 "rtf": round(vdt / max(0.01, dur), 3)}
            print(f"   vosk-small [{vdt:.1f}s]: {vt!r}")
        for name, fm in fw_loaded.items():
            try:
                ft, fdt = fw_text(pcm, fm, args.lang)
            except Exception as exc:
                ft, fdt = f"(loi: {exc})", 0.0
            row[f"fw_{name}"] = {"text": ft, "sec": round(fdt, 2),
                                 "rtf": round(fdt / max(0.01, dur), 3)}
            print(f"   fw-{name} [{fdt:.1f}s]: {ft!r}")
        report.append(row)

    out = REPO_ROOT / "audio" / f"eval-stt-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

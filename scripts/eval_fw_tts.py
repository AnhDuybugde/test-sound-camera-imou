#!/usr/bin/env python3
"""So sanh fw-small vs fw-medium tren case ZeroTTS (co ground truth -> WER).

Usage (Ubuntu):
  python3 scripts/eval_fw_tts.py
  python3 scripts/eval_fw_tts.py --versions clean,quiet,noisy
Output: audio/eval-fw-tts-<stamp>.json + bang WER/time.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_RATE = 16000


def norm_vi(s: str) -> list[str]:
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.split() if s else []


def wer(ref: str, hyp: str) -> tuple[float, int, int]:
    r, h = norm_vi(ref), norm_vi(hyp)
    if not r:
        return (0.0 if not h else 1.0), 0, len(h)
    prev = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        cur = [i]
        for j in range(1, len(h) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (0 if r[i - 1] == h[j - 1] else 1)))
        prev = cur
    return prev[len(h)] / len(r), prev[len(h)], len(r)


def transcribe_file(fw_model, path: Path, lang="vi") -> tuple[str, float]:
    t0 = time.monotonic()
    segments, _ = fw_model.transcribe(str(path), language=lang, beam_size=5,
                                      vad_filter=False)
    return " ".join(s.text.strip() for s in segments).strip(), time.monotonic() - t0


def wav_dur(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="audio/tts-cases.json")
    ap.add_argument("--versions", default="clean,quiet,noisy")
    ap.add_argument("--models", default="small,medium")
    ap.add_argument("--lang", default="vi")
    args = ap.parse_args()

    mp = Path(args.manifest)
    if not mp.is_absolute():
        mp = REPO_ROOT / mp
    manifest = json.loads(mp.read_text(encoding="utf-8"))["cases"]
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]

    from faster_whisper import WhisperModel
    models = {}
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"Loading faster-whisper {name} ...", flush=True)
        models[name] = WhisperModel(name, device="cpu", compute_type="int8")

    rows = []
    tot: dict[str, dict] = {m: {"err": 0, "ref": 0, "t": 0.0, "n": 0} for m in models}
    for case in manifest:
        for ver in versions:
            wav = REPO_ROOT / "audio" / case[ver]
            dur = wav_dur(wav)
            line = {"id": f"{case['id']}-{ver}", "ref": case["text"],
                    "dur_s": round(dur, 2), "hyps": {}}
            print(f"\n== {case['id']}-{ver} ({dur:.1f}s) REF: {case['text']}", flush=True)
            for name, fm in models.items():
                hyp, dt = transcribe_file(fm, wav, args.lang)
                w, err, nref = wer(case["text"], hyp)
                line["hyps"][name] = {"text": hyp, "sec": round(dt, 2),
                                      "rtf": round(dt / max(0.01, dur), 3),
                                      "wer": round(w, 3)}
                tot[name]["err"] += err
                tot[name]["ref"] += nref
                tot[name]["t"] += dt
                tot[name]["n"] += 1
                print(f"   {name:6s} WER={w:.2f} [{dt:.1f}s] {hyp!r}", flush=True)
            rows.append(line)

    print("\n===== TONG HOP =====")
    for name, t in tot.items():
        print(f"{name:6s}: WER tb={t['err']/max(1,t['ref']):.3f} "
              f"tong tgian={t['t']:.1f}s ({t['n']} files)")
    out = REPO_ROOT / "audio" / f"eval-fw-tts-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"rows": rows, "total": tot},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

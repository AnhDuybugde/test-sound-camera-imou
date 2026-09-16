#!/usr/bin/env python3
"""Sinh file wav test tieng Viet bang ZeroTTS (ONNX/CPU) + ban pha far-field.

Usage (Ubuntu):
  python3 scripts/make_tts_cases.py
Output: audio/tts-caseNN-<clean|quiet|noisy>.wav (16k mono) + audio/tts-cases.json
Ground truth = chinh text dau vao -> dung de tinh WER fw-small vs fw-medium.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "audio"

CASES = [
    ("01", "Xin chào, hôm nay trời đẹp quá."),
    ("02", "Cuộc họp sẽ diễn ra vào ngày 31/12/2025 lúc 9 giờ 30 phút."),
    ("03", "Bạn đã cập nhật driver cho camera chưa?"),
    ("04", "Hệ thống camera quan sát hoạt động liên tục để đảm bảo an ninh cho toàn bộ khu vực."),
    ("05", "Mẹ mua mía ở chợ về cho bé."),
    ("06", "Một hai ba bốn năm sáu bảy tám chín mười."),
    ("07", "Anh Duy đang kiểm tra âm thanh của micro camera ngoài hành lang."),
]

VOICE = "maichi"


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed: {r.stderr[:500]}")


def main() -> int:
    from zerotts import ZeroTTS

    print("Loading ZeroTTS (lan dau se tai model ~200M params) ...", flush=True)
    tts = ZeroTTS.from_pretrained("zeroweight-ai/ZeroTTS")

    manifest = []
    for cid, text in CASES:
        raw = OUT_DIR / f"tts-case{cid}-raw48k.wav"
        print(f"[{cid}] {text}", flush=True)
        audio = tts.synthesize(text, voice=VOICE)
        tts.save_audio(audio, str(raw))

        base = OUT_DIR / f"tts-case{cid}"
        # clean 16k mono
        clean = Path(str(base) + "-clean.wav")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(raw), "-ac", "1", "-ar", "16000", str(clean)])
        # quiet ~ far-field (-18dB)
        quiet = Path(str(base) + "-quiet.wav")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(clean), "-af", "volume=-18dB", str(quiet)])
        # noisy: tron pink noise SNR thap (~10dB)
        noisy = Path(str(base) + "-noisy.wav")
        dur_out = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(clean)],
            capture_output=True, text=True)
        dur = float(dur_out.stdout.strip() or "2.0")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(clean),
             "-f", "lavfi", "-i",
             f"anoisesrc=color=pink:duration={dur}:sample_rate=16000:amplitude=0.035",
             "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=shortest,volume=2.2",
             "-ac", "1", "-ar", "16000", str(noisy)])
        raw.unlink(missing_ok=True)
        manifest.append({"id": cid, "text": text,
                         "clean": clean.name, "quiet": quiet.name, "noisy": noisy.name})
        print(f"     -> {clean.name} / {quiet.name} / {noisy.name}", flush=True)

    mp = OUT_DIR / "tts-cases.json"
    mp.write_text(json.dumps({"created": datetime.now().isoformat(),
                              "voice": VOICE, "cases": manifest},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved manifest {mp} ({len(manifest)} cases x 3 versions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

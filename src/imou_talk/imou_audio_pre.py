#!/usr/bin/env python3
"""P2: nhe DSP truoc STT (stdlib only, khong them dependency).

Muc dich: lam sach + chuan hoa moi speech segment truoc khi dua vao Vosk,
va loai bo segment nhieu bang gate SNR/ZCR thay vi chi dung mean RMS.

Ham chinh:
  remove_dc(pcm)          - tru DC offset
  apply_agc(pcm, ...)     - scale segment ve target RMS + limiter chong clip
  zero_crossing_rate(pcm) - ti le qua 0 (speech ~0.02-0.25, nhieu trang ~0.5)
  segment_quality(pcm)    - {rms, zcr, snr_db, peak, clip} (snr uoc luong
                            p90/p10 tren frame 30ms ben trong segment)
  preprocess_segment(pcm, ...) -> (pcm_out, info, reject_reason)
"""
from __future__ import annotations

import math
import struct

FRAME_BYTES = 16000 * 30 // 1000 * 2  # 960


def _samples(pcm: bytes) -> list[int]:
    return [struct.unpack_from("<h", pcm, i)[0] for i in range(0, len(pcm) - 1, 2)]


def _pack(samples: list[int]) -> bytes:
    out = bytearray(len(samples) * 2)
    for i, s in enumerate(samples):
        struct.pack_into("<h", out, i * 2, max(-32768, min(32767, int(s))))
    return bytes(out)


def remove_dc(pcm: bytes) -> bytes:
    if len(pcm) < 2:
        return pcm
    n = len(pcm) // 2
    mean = sum(struct.unpack_from("<h", pcm, i)[0] for i in range(0, len(pcm) - 1, 2)) / max(1, n)
    if abs(mean) < 1.0:
        return pcm
    return _pack([s - int(round(mean)) for s in _samples(pcm)])


def apply_agc(pcm: bytes, target_rms: float = 0.12, max_gain: float = 6.0) -> tuple[bytes, float]:
    """Scale ve target RMS. Tra ve (pcm_moi, gain). Limiter mem chong clip."""
    if len(pcm) < 2:
        return pcm, 1.0
    samps = _samples(pcm)
    rms = math.sqrt(sum(s * s for s in samps) / len(samps)) / 32768.0
    if rms < 1e-6:
        return pcm, 1.0
    gain = min(max_gain, target_rms / rms)
    if abs(gain - 1.0) < 0.05:
        return pcm, 1.0
    peak = max(abs(s) for s in samps)
    if peak * gain >= 32000:  # soft limiter: giam gain de peak < 0.95 full-scale
        gain = (0.95 * 32767) / max(1, peak)
    return _pack([s * gain for s in samps]), round(gain, 3)


def zero_crossing_rate(pcm: bytes) -> float:
    samps = _samples(pcm)
    if len(samps) < 2:
        return 0.0
    zc = sum(1 for a, b in zip(samps, samps[1:]) if (a >= 0) != (b >= 0))
    return zc / (len(samps) - 1)


def _frame_rms_list(pcm: bytes) -> list[float]:
    out = []
    for f in range(len(pcm) // FRAME_BYTES):
        fr = pcm[f * FRAME_BYTES:(f + 1) * FRAME_BYTES]
        n = len(fr) // 2
        sq = 0
        for i in range(0, len(fr) - 1, 2):
            s = struct.unpack_from("<h", fr, i)[0]
            sq += s * s
        out.append(math.sqrt(sq / max(1, n)) / 32768.0)
    return sorted(out)


def segment_quality(pcm: bytes) -> dict:
    rms_list = _frame_rms_list(pcm)
    if not rms_list:
        return {"rms": 0.0, "zcr": 0.0, "snr_db": 0.0, "peak": 0, "clip": 0.0}
    n = len(pcm) // 2
    sq = 0
    peak = 0
    clip = 0
    for i in range(0, len(pcm) - 1, 2):
        s = struct.unpack_from("<h", pcm, i)[0]
        sq += s * s
        peak = max(peak, abs(s))
        if abs(s) >= 32760:
            clip += 1
    rms = math.sqrt(sq / max(1, n)) / 32768.0
    p10 = rms_list[max(0, int(0.10 * len(rms_list)))]
    p90 = rms_list[min(len(rms_list) - 1, int(0.90 * len(rms_list)))]
    snr_db = 20 * math.log10((p90 + 1e-9) / (p10 + 1e-9)) if p10 > 0 else 60.0
    return {
        "rms": round(rms, 5),
        "zcr": round(zero_crossing_rate(pcm), 4),
        "snr_db": round(min(60.0, snr_db), 2),
        "peak": peak,
        "clip": round(clip / max(1, n), 6),
    }


def preprocess_segment(
    pcm: bytes,
    target_rms: float = 0.12,
    max_gain: float = 6.0,
    min_snr_db: float = 4.0,
    zcr_range: tuple[float, float] = (0.005, 0.45),
    use_agc: bool = True,
) -> tuple[bytes, dict, str]:
    """Tien xu ly 1 segment. Tra ve (pcm_out, info, reject_reason or '')."""
    pcm = remove_dc(pcm)
    q0 = segment_quality(pcm)
    gain = 1.0
    if use_agc:
        pcm, gain = apply_agc(pcm, target_rms, max_gain)
    q = segment_quality(pcm)
    q["gain"] = gain
    q["rms_pre"] = q0["rms"]
    reason = ""
    if q["snr_db"] < min_snr_db:
        reason = f"low-snr {q['snr_db']}dB < {min_snr_db}dB"
    elif not (zcr_range[0] <= q["zcr"] <= zcr_range[1]):
        reason = f"zcr {q['zcr']} ngoai {zcr_range} (nhieu/clip?)"
    return pcm, q, reason

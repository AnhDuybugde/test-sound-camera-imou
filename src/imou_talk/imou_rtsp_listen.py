#!/usr/bin/env python3
"""Listen to Imou camera mic via RTSP (not VisualTalk) + VAD + STT.

New pipeline (receive-only, does not touch speak path):
  camera mic -> RTSP :554 /cam/realmonitor?channel=&subtype=
  -> (LAN direct | DHP2P/PTCP relay to remote 554 -> 127.0.0.1:1554)
  -> ffmpeg -map 0:a:0 -vn -ac 1 -ar 16000 -f s16le pipe:1
  -> PCM 16k mono -> energy/webrtc VAD -> speech segments -> Vosk (vi)

Why not VisualTalk for listening:
  visualtalk.xav is the machine->speaker direction. Reusing its DHAV
  interleave ($) for mic capture is fragile (video/audio misclassify,
  AAC split errors, G711 mis-guess) and the old code fed EVERY decoded
  chunk to Vosk continuously -> small model hallucinates on silence /
  comfort noise / decoder clicks ("tap am khong ton tai").

Usage - LAN direct (camera and PC in same LAN):
  python -u src/imou_talk/imou_rtsp_listen.py --ip 192.168.1.38 --seconds 30

Usage - remote via P2P relay (camera o mang khac):
  python -u src/imou_talk/imou_rtsp_listen.py --via-p2p --serial <ID> --seconds 30

Echo suppression with speaker:
  When TTS/speak starts, create the inhibit file; delete it when done.
  While the file exists (plus --cooldown-ms after) PCM is still drained
  but nothing is sent to ASR.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import wave
from pathlib import Path

try:
    from imou_env import load_repo_dotenv
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_env import load_repo_dotenv  # type: ignore[no-redef]

try:
    from imou_audio_pre import preprocess_segment
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_audio_pre import preprocess_segment  # type: ignore[no-redef]


TARGET_RATE = 16000
FRAME_MS = 30  # VAD frame
FRAME_BYTES = TARGET_RATE * FRAME_MS // 1000 * 2  # 960 bytes @16k mono s16le


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    raise RuntimeError("ffmpeg not found (install imageio-ffmpeg or add ffmpeg to PATH)")


def build_rtsp_url(args) -> str:
    if args.rtsp_url:
        return args.rtsp_url
    # NOTE: '=' is legal in userinfo (sub-delims, RFC3986) and Dahua/Imou
    # cameras expect it raw (e.g. "L2=5qC8t"). Only encode chars that would
    # break the URL structure (@ : / ? # space %).
    pw = urllib.parse.quote(args.password or "", safe="!$&'()*+,;=")
    return (
        f"rtsp://{args.username}:{pw}@{args.ip}:554"
        f"/cam/realmonitor?channel={args.channel}&subtype={args.subtype}"
    )


def redact_url(url: str, password: str | None) -> str:
    if password:
        return url.replace(password, "<redacted>").replace(
            urllib.parse.quote(password, safe="!$&'()*+,;="), "<redacted>"
        )
    return url


def rms_level(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    total = 0
    for i in range(0, len(pcm) - 1, 2):
        s = struct.unpack_from("<h", pcm, i)[0]
        total += s * s
    return math.sqrt(total / max(1, n)) / 32768.0


def vu_bar(level: float, width: int = 24) -> str:
    filled = int(min(1.0, level * 3.0) * width)
    return "#" * filled + "-" * (width - filled)


class Vad:
    """Energy VAD, optionally backed by webrtcvad if installed."""

    def __init__(self, mode: str, threshold: float, aggressiveness: int):
        self.mode = mode
        self.threshold = threshold
        self._webrtc = None
        if mode == "webrtc":
            try:
                import webrtcvad  # type: ignore

                self._webrtc = webrtcvad.Vad(aggressiveness)
            except ImportError as exc:
                raise RuntimeError(
                    "webrtcvad not installed; use --vad-mode energy or pip install webrtcvad-wheels"
                ) from exc
        elif mode != "energy":
            raise ValueError("vad-mode must be energy or webrtc")

    def is_speech(self, frame: bytes, level: float) -> bool:
        if self._webrtc is not None:
            # webrtcvad needs exactly 10/20/30ms of 16-bit mono PCM
            if len(frame) != FRAME_BYTES:
                return level >= self.threshold
            try:
                return self._webrtc.is_speech(frame, TARGET_RATE)
            except Exception:
                return level >= self.threshold
        return level >= self.threshold


def transcribe_segment(rec_model, pcm: bytes) -> str:
    """Feed one speech segment to a fresh Vosk recognizer, return text."""
    import json

    from vosk import KaldiRecognizer  # type: ignore

    rec = KaldiRecognizer(rec_model, TARGET_RATE)
    rec.SetWords(False)
    for i in range(0, len(pcm), 4000):
        rec.AcceptWaveform(pcm[i : i + 4000])
    try:
        return json.loads(rec.FinalResult()).get("text", "").strip()
    except Exception:
        return ""


def transcribe_segment_fw(fw_model, pcm: bytes, lang: str = "vi") -> str:
    """Feed one speech segment to faster-whisper (via temp wav), return text."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_RATE)
            w.writeframes(pcm)
    try:
        segments, _info = fw_model.transcribe(tmp, language=lang, beam_size=5,
                                              vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:
        return ""
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass


def run_rtsp_listen(args, rtsp_url: str) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        pass

    ffmpeg = find_ffmpeg()
    vad = Vad(args.vad_mode, args.vad_threshold, args.vad_aggressiveness)

    # --- STT model (only if STT enabled) ---
    vosk_model = None
    fw_model = None
    stt_engine = getattr(args, "stt", "vosk")
    if not args.no_stt and stt_engine == "faster-whisper":
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError:
            _in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
            _hint = "pip install faster-whisper" if _in_venv else "pip3 install --user faster-whisper"
            print(f"Missing dependency: {_hint}", file=sys.stderr)
            return 2
        print(f"Loading faster-whisper {args.fw_model} (int8 CPU) ...", flush=True)
        fw_model = WhisperModel(args.fw_model, device="cpu", compute_type="int8")
    elif not args.no_stt:
        try:
            from vosk import Model  # type: ignore
        except ImportError:
            print("Missing dependency: python -m pip install vosk", file=sys.stderr)
            return 2
        model_path = Path(args.model)
        if not model_path.is_absolute():
            repo_root = Path(__file__).resolve().parent.parent.parent
            model_path = repo_root / model_path
        if not model_path.exists():
            print(f"Vosk model not found at {model_path}", file=sys.stderr)
            return 2
        print(f"Loading Vosk model {model_path} ...", flush=True)
        vosk_model = Model(str(model_path))

    # --- ffmpeg persistent audio pipe (stderr kept for diagnosis) ---
    # Offline test hook: if input is a local .wav file, decode it instead of RTSP.
    is_file = rtsp_url.lower().endswith(".wav") and Path(rtsp_url).exists()
    # P1: optional ffmpeg audio pre-filter (applied before PCM reaches VAD/STT).
    # Default is a light speech-friendly chain; use --no-audio-filter to bypass
    # or --audio-filter "<chain>" for experiments (offline replay supported).
    # P3: --denoise rnnoise thay the afftdn bang arnndn + model .rnnn.
    af = (args.audio_filter or "").strip() if not args.no_audio_filter else ""
    if args.denoise == "rnnoise" and not args.no_audio_filter:
        mp = Path(args.denoise_model)
        if not mp.is_absolute():
            mp = Path(__file__).resolve().parent.parent.parent / mp
        if not mp.exists():
            print(f"denoise model not found: {mp}, fallback ve audio-filter thuong",
                  file=sys.stderr, flush=True)
        else:
            af = f"highpass=f=80,arnndn=m={mp}:mix={args.denoise_mix}"
    if is_file:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-i", rtsp_url,
            "-ac", "1", "-ar", str(TARGET_RATE),
        ]
        if af:
            cmd += ["-af", af]
        cmd += ["-f", "s16le", "pipe:1"]
    else:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", str(TARGET_RATE),
        ]
        if af:
            cmd += ["-af", af]
        cmd += ["-f", "s16le", "pipe:1"]
    print("ffmpeg: " + redact_url(" ".join(cmd), args.password), flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    assert proc.stdout is not None and proc.stderr is not None
    import threading as _th

    _ffmpeg_err: list[bytes] = []
    _pw_variants = [p for p in {
        args.password or "",
        urllib.parse.quote(args.password or "", safe="!$&'()*+,;="),
    } if p]

    def _scrub(text: str) -> str:
        for pv in _pw_variants:
            text = text.replace(pv, "<redacted>")
        return text

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore
                _ffmpeg_err.append(line)
                if len(_ffmpeg_err) > 40:
                    del _ffmpeg_err[: len(_ffmpeg_err) - 40]
                if args.debug:
                    sys.stderr.write("[ffmpeg] " + _scrub(line.decode(errors="replace")))
                    sys.stderr.flush()
        except Exception:
            pass

    _th.Thread(target=_drain_stderr, daemon=True).start()

    wav_file = None
    if args.dump_wav:
        wav_file = wave.open(args.dump_wav, "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_RATE)

    seg_dir = Path(args.dump_segments) if args.dump_segments else None
    if seg_dir is not None:
        seg_dir.mkdir(parents=True, exist_ok=True)

    pre_roll_frames: collections.deque[bytes] = collections.deque(
        maxlen=max(1, int(args.pre_roll_ms / FRAME_MS))
    )
    speaking = False
    segment = bytearray()
    silence_ms = 0
    voiced_ms = 0
    inhibit_until = 0.0
    seg_index = 0
    n_segments = 0
    n_speech_frames = 0
    n_total_frames = 0
    start = time.monotonic()
    last_vu = 0.0
    leftover = bytearray()

    def inhibited(now: float) -> bool:
        if args.inhibit_file and Path(args.inhibit_file).exists():
            return True
        return now < inhibit_until

    def emit_segment(seg_bytes: bytes, el: float) -> None:
        """Xu ly 1 segment da dong: gate RMS -> P2 pre -> dump -> STT."""
        nonlocal seg_index, n_segments
        # skip tiny blips
        if len(seg_bytes) < int(args.min_speech_ms / 1000 * TARGET_RATE * 2):
            pre_roll_frames.clear()
            return
        seg_index += 1
        dur = len(seg_bytes) / TARGET_RATE / 2
        seg_rms = rms_level(seg_bytes)
        if seg_rms < args.min_seg_rms:
            n_segments += 1
            if args.debug or args.vu:
                print(f"\n[{el:6.1f}s] skip noise seg "
                      f"{dur:.1f}s rms={seg_rms:.4f}", flush=True)
            pre_roll_frames.clear()
            return
        n_segments += 1
        # P2: DC-block + AGC + quality gate truoc STT
        seg_pcm, seg_q, seg_reject = preprocess_segment(
            seg_bytes,
            target_rms=args.agc_target,
            max_gain=args.agc_max_gain,
            min_snr_db=args.min_snr_db,
            use_agc=not args.no_agc,
        )
        if seg_reject and not args.no_quality_gate:
            if args.debug or args.vu:
                print(f"\n[{el:6.1f}s] skip low-q seg "
                      f"{dur:.1f}s {seg_reject} rms={seg_q['rms']} "
                      f"snr={seg_q['snr_db']}dB zcr={seg_q['zcr']}", flush=True)
            pre_roll_frames.clear()
            return
        seg_path = ""
        if seg_dir is not None:
            seg_path = str(seg_dir / f"seg{seg_index:03d}.wav")
            try:
                with wave.open(seg_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(TARGET_RATE)
                    w.writeframes(seg_pcm)
            except Exception as exc:
                print(f"\ndump warn: {exc}", flush=True)
                seg_path = ""
        if args.no_stt or (vosk_model is None and fw_model is None):
            print(f"\n[{el:6.1f}s] speech {dur:.1f}s (STT off)"
                  + (f" -> {seg_path}" if seg_path else ""), flush=True)
        else:
            if fw_model is not None:
                text = transcribe_segment_fw(fw_model, seg_pcm,
                                             getattr(args, "fw_lang", "vi"))
            else:
                text = transcribe_segment(vosk_model, seg_pcm)
            tag = f" -> {seg_path}" if seg_path else ""
            qtag = (f" [rms={seg_q['rms']} snr={seg_q['snr_db']}dB "
                    f"zcr={seg_q['zcr']} g={seg_q['gain']}]") if args.debug else ""
            if text:
                print(f"\n[{el:6.1f}s] {text}{tag}{qtag}", flush=True)
            elif args.debug:
                print(f"\n[{el:6.1f}s] (speech {dur:.1f}s, no text){tag}{qtag}", flush=True)
        pre_roll_frames.clear()

    print("Listening via RTSP... speak into the camera. Press Ctrl+C to stop.", flush=True)
    print(f"VAD={args.vad_mode} thr={args.vad_threshold} silence={args.silence_ms}ms "
          f"min_speech={args.min_speech_ms}ms pre_roll={args.pre_roll_ms}ms "
          f"af={af if af else 'off'} stt={getattr(args, 'stt', 'vosk')}", flush=True)
    try:
        while True:
            now = time.monotonic()
            if args.seconds > 0 and now - start >= args.seconds:
                if speaking and len(segment) > 0:
                    emit_segment(bytes(segment), now - start)
                    speaking = False
                    segment = bytearray()
                break
            # update cooldown edge: inhibit file just disappeared
            if args.inhibit_file and not Path(args.inhibit_file).exists():
                if inhibit_until == 0.0:
                    pass  # never inhibited
            chunk = proc.stdout.read(4096)
            if not chunk:
                # ffmpeg closed / RTSP dropped — dump recent ffmpeg errors
                print("\nffmpeg/RTSP stream ended.", flush=True)
                rc = proc.poll()
                if n_total_frames == 0:
                    print(f"ffmpeg exited early (rc={rc}). Recent ffmpeg log:", flush=True)
                    for line in _ffmpeg_err[-15:]:
                        print("  [ffmpeg] " + _scrub(line.decode(errors="replace").rstrip()), flush=True)
                    print("Hints: sai password? RTSP bi khoa tam thoi sau nhieu login sai? "
                          "camera doi IP? thu subtype 0/1, channel 1, hoac --via-p2p.", flush=True)
                if speaking and len(segment) > 0:
                    emit_segment(bytes(segment), time.monotonic() - start)
                    speaking = False
                    segment = bytearray()
                break
            if wav_file is not None:
                try:
                    wav_file.writeframes(chunk)
                except Exception:
                    pass
            leftover += chunk
            while len(leftover) >= FRAME_BYTES:
                frame = bytes(leftover[:FRAME_BYTES])
                del leftover[:FRAME_BYTES]
                n_total_frames += 1
                level = rms_level(frame)
                is_speech = vad.is_speech(frame, level) if not inhibited(now) else False

                # VU meter ~2x/sec
                if args.vu and now - last_vu > 0.5:
                    last_vu = now
                    el = now - start
                    tag = "MUTE " if inhibited(now) else ("TALK " if speaking else "idle ")
                    print(f"\r[{el:6.1f}s] {tag}|{vu_bar(level)}| {level:.3f}", end="", flush=True)

                if not speaking:
                    pre_roll_frames.append(frame)
                    if is_speech:
                        voiced_ms += FRAME_MS
                        if voiced_ms >= args.min_speech_ms:
                            # speech onset: include pre-roll
                            speaking = True
                            segment = bytearray(b"".join(pre_roll_frames))
                            silence_ms = 0
                            voiced_ms = 0
                    else:
                        voiced_ms = 0
                else:
                    segment += frame
                    if is_speech:
                        silence_ms = 0
                        n_speech_frames += 1
                    else:
                        silence_ms += FRAME_MS
                    # track inhibit during speech -> abort segment
                    if inhibited(now):
                        speaking = False
                        segment.clear()
                        silence_ms = 0
                        inhibit_until = now + args.cooldown_ms / 1000.0
                        continue
                    max_bytes = int(args.max_segment_s * TARGET_RATE * 2)
                    if silence_ms >= args.silence_ms or len(segment) >= max_bytes:
                        # close segment
                        el = now - start
                        seg_pcm = bytes(segment)
                        speaking = False
                        segment = bytearray()
                        silence_ms = 0
                        emit_segment(seg_pcm, el)
            # if inhibit file appeared, mark cooldown start for after it disappears
            if args.inhibit_file and Path(args.inhibit_file).exists():
                inhibit_until = now + args.cooldown_ms / 1000.0
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).", flush=True)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        if wav_file is not None:
            try:
                wav_file.close()
                print(f"Saved PCM 16k mono to {args.dump_wav}", flush=True)
            except Exception:
                pass
        el = time.monotonic() - start
        print(f"\nDone: {el:.1f}s, {n_total_frames} frames, "
              f"{n_segments} speech segments.", flush=True)
    return 0


async def run_with_p2p(args) -> int:
    """Open DHP2P relay to remote 554, then listen via local RTSP URL."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_dhp2p import DHP2PTunnel, p2p_handshake  # type: ignore

    supplied = args.password or os.environ.get("IMOU_CAMERA_PASSWORD") or os.environ.get("IMOU_PASSWORD")
    serial = args.serial
    if not serial or not supplied:
        print("Missing --serial/--password (or IMOU_CAMERA_PASSWORD env).", file=sys.stderr)
        return 2
    if not args.password:
        args.password = supplied
    ptcp = await p2p_handshake(
        serial, relay_mode=True, dtype=args.type,
        username=args.username, password=supplied, debug=args.debug,
    )
    tunnel = DHP2PTunnel(ptcp, args.remote_port, debug=args.debug)
    server_task = asyncio.create_task(tunnel.start(args.bind_host, args.bind_port))
    await asyncio.sleep(args.startup_delay)
    # RTSP through tunnel: same user/pass, local host/port
    pw = urllib.parse.quote(args.password or "", safe="!$&'()*+,;=")
    rtsp_url = (
        f"rtsp://{args.username}:{pw}@{args.bind_host}:{args.bind_port}"
        f"/cam/realmonitor?channel={args.channel}&subtype={args.subtype}"
    )
    print(f"P2P tunnel ready; RTSP via {args.bind_host}:{args.bind_port}", flush=True)
    try:
        return await asyncio.to_thread(run_rtsp_listen, args, rtsp_url)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


def parse_args() -> argparse.Namespace:
    load_repo_dotenv()
    _repo_root = Path(__file__).resolve().parent.parent.parent
    _default_ip = (
        os.environ.get("IMOU_IP")
        or os.environ.get("IMOU_CAMERA_IP")
        or "192.168.1.38"
    )
    _default_user = (
        os.environ.get("IMOU_USER")
        or os.environ.get("IMOU_USERNAME")
        or "admin"
    )
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--rtsp-url", default="", help="full RTSP URL (overrides --ip/--channel/--subtype)")
    src.add_argument("--ip", default=_default_ip, help="camera LAN IP for direct mode (env IMOU_IP)")
    p.add_argument("--username", default=_default_user)
    p.add_argument("--password", default=os.environ.get("IMOU_CAMERA_PASSWORD") or os.environ.get("IMOU_PASSWORD") or "")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--subtype", type=int, default=1, help="1=low-res+audio (lighter), 0=4K+audio")
    # P2P relay mode
    p.add_argument("--via-p2p", action="store_true", help="tunnel remote 554 over DHP2P relay instead of LAN")
    p.add_argument("--serial", default="", help="camera serial for --via-p2p")
    p.add_argument("--type", type=int, default=0)
    p.add_argument("--bind-host", default="127.0.0.1")
    p.add_argument("--bind-port", type=int, default=1554)
    p.add_argument("--remote-port", type=int, default=554)
    p.add_argument("--startup-delay", type=float, default=2.0)
    # VAD / suppression
    p.add_argument("--vad-mode", choices=("energy", "webrtc"), default="energy")
    p.add_argument("--audio-filter", default="highpass=f=80,afftdn=nr=12:nf=-25",
                   help="ffmpeg -af chain applied before VAD/STT (default light denoise)")
    p.add_argument("--no-audio-filter", action="store_true", help="bypass ffmpeg -af")
    p.add_argument("--denoise", choices=("off", "rnnoise"), default="off",
                   help="P3: denoise nang qua arnndn (can model .rnnn)")
    p.add_argument("--denoise-model", default="models/rnnoise/cb.rnnn", help="P3: model arnndn")
    p.add_argument("--denoise-mix", type=float, default=0.9, help="P3: mix output vs input (-1..1)")
    p.add_argument("--vad-threshold", type=float, default=0.03,
                   help="energy RMS threshold (room silence p50 ~0.022, speech ~0.05+)")
    p.add_argument("--min-seg-rms", type=float, default=0.025,
                   help="skip STT for segments below this mean RMS (noise-segment gate)")
    p.add_argument("--agc-target", type=float, default=0.12, help="P2: target RMS sau AGC")
    p.add_argument("--agc-max-gain", type=float, default=6.0, help="P2: gain toi da cua AGC")
    p.add_argument("--min-snr-db", type=float, default=4.0, help="P2: loai segment SNR thap (0=tat)")
    p.add_argument("--no-agc", action="store_true", help="P2: tat AGC")
    p.add_argument("--no-quality-gate", action="store_true", help="P2: chi log SNR/ZCR, khong loai segment")
    p.add_argument("--vad-aggressiveness", type=int, default=2, help="webrtcvad 0-3")
    p.add_argument("--silence-ms", type=int, default=800, help="end segment after this silence")
    p.add_argument("--min-speech-ms", type=int, default=300, help="require this much voice to start")
    p.add_argument("--pre-roll-ms", type=int, default=500, help="keep audio before onset")
    p.add_argument("--max-segment-s", type=float, default=8.0,
                   help="force-close long segments (small Vosk model degrades past ~8s)")
    p.add_argument("--cooldown-ms", type=int, default=1000, help="hold-off after speaker inhibit ends")
    p.add_argument("--inhibit-file", default="", help="while this file exists, drain PCM but skip ASR")
    # STT / output
    p.add_argument("--seconds", type=float, default=0, help="0 = until Ctrl+C")
    p.add_argument("--model", default="models/vosk-model-small-vn-0.4")
    p.add_argument("--stt", choices=("vosk", "faster-whisper"), default="faster-whisper",
                   help="P4: engine STT (faster-whisper can pip3 install --user faster-whisper)")
    p.add_argument("--fw-model", default="medium", help="P4: faster-whisper model (tiny/base/small/...)")
    p.add_argument("--fw-lang", default="vi", help="P4: ngon ngu faster-whisper")
    p.add_argument("--no-stt", action="store_true")
    p.add_argument("--vu", action="store_true")
    p.add_argument("--dump-wav", default="", help="save decoded PCM as wav (default: auto audio/rtsp-YYYYMMDD-HHMMSS.wav)")
    p.add_argument("--dump-segments", default="")
    p.add_argument("--no-auto-dump", action="store_true", help="disable auto WAV save to audio/")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    if not args.dump_wav and not args.no_auto_dump:
        try:
            _audio_dir = _repo_root / "audio"
            _audio_dir.mkdir(parents=True, exist_ok=True)
            _stamp = time.strftime("%Y%m%d-%H%M%S")
            args.dump_wav = str(_audio_dir / f"rtsp-{_stamp}.wav")
        except OSError:
            args.dump_wav = ""
    return args


def main() -> int:
    args = parse_args()
    if args.via_p2p:
        return asyncio.run(run_with_p2p(args))
    rtsp_url = build_rtsp_url(args)
    return run_rtsp_listen(args, rtsp_url)


if __name__ == "__main__":
    raise SystemExit(main())

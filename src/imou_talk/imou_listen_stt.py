#!/usr/bin/env python3
"""Listen to Imou camera mic via VisualTalk P2P and stream speech-to-text.

Pipeline:
  camera mic -> DHP2P/PTCP relay (remote 8086) -> visualtalk.xav session
  -> DHAV interleaved frames ($) -> AAC/G711 payload -> ffmpeg -> PCM 16k mono
  -> Vosk (vi) -> terminal

Receive-only: this script never calls send_audio(), so it does not play
through the laptop speaker and avoids feedback with the laptop mic.
Walk away from the PC and speak into the camera.

Requires:
  python -m pip install vosk imageio-ffmpeg
  Vietnamese model: models/vosk-model-small-vn-0.4 (auto-downloaded)

Example:
  python -u src/imou_talk/imou_listen_stt.py --serial <ID> --seconds 60
  python -u src/imou_talk/imou_listen_stt.py --serial <ID> --no-stt --seconds 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
import zipfile
from pathlib import Path

try:
    from imou_dhp2p import DHP2PTunnel, p2p_handshake
    from imou_visualtalk import VisualTalkClient
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_dhp2p import DHP2PTunnel, p2p_handshake  # type: ignore[no-redef]
    from imou_visualtalk import VisualTalkClient  # type: ignore[no-redef]

try:
    from imou_env import load_repo_dotenv
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_env import load_repo_dotenv  # type: ignore[no-redef]

VI_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-vn-0.4.zip"
VI_MODEL_NAME = "vosk-model-small-vn-0.4"
TARGET_PCM_RATE = 16000


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


def ensure_vosk_model(model_path: Path, auto_download: bool) -> Path:
    if model_path.exists() and any(model_path.iterdir()):
        return model_path
    if not auto_download:
        raise RuntimeError(
            f"Vosk model not found at {model_path}. "
            "Re-run with --auto-download or download "
            f"{VI_MODEL_URL} and unzip it there."
        )
    model_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = model_path.parent / (VI_MODEL_NAME + ".zip")
    print(f"Downloading Vietnamese Vosk model (~40MB) to {tmp_zip} ...", flush=True)
    urllib.request.urlretrieve(VI_MODEL_URL, tmp_zip)
    print("Unzipping model ...", flush=True)
    with zipfile.ZipFile(tmp_zip, "r") as zf:
        zf.extractall(model_path.parent)
    try:
        tmp_zip.unlink()
    except OSError:
        pass
    if not model_path.exists():
        raise RuntimeError(f"model unzip failed, expected {model_path}")
    return model_path


def is_adts(payload: bytes) -> bool:
    return len(payload) >= 7 and payload[0] == 0xFF and (payload[1] & 0xF0) == 0xF0


def parse_dhav_frame(frame: bytes):
    """Return (payload, seq, total_len, frame_type, is_audio) or None if invalid."""
    if len(frame) < 36 or frame[:4] != b"DHAV":
        return None
    total_len = struct.unpack_from("<I", frame, 12)[0]
    if total_len != len(frame):
        # tolerate mismatch, still try to extract
        pass
    if frame[-8:-4] != b"dhav":
        return None
    payload = frame[28:-8] if len(frame) >= 36 else b""
    seq = struct.unpack_from("<I", frame, 8)[0]
    frame_type = frame[4]
    audio_marker = frame[0x18:0x1B] if len(frame) >= 0x1B else b""
    # Audio talk frames pack as type 0xF0 + marker 83 01 1a (see imou_dhav.py).
    # Video/other frames have different type/marker and KB-size payloads.
    is_audio = (frame_type == 0xF0 and audio_marker == b"\x83\x01\x1a")
    return payload, seq, total_len, frame_type, is_audio


def guess_codec(payloads: list[bytes]) -> str:
    if not payloads:
        return "aac-adts"
    adts = sum(1 for p in payloads if is_adts(p))
    if adts / max(1, len(payloads)) > 0.8:
        return "aac-adts"
    # G711 payloads are typically 160/320 bytes without ADTS sync
    return "mulaw"


def spawn_decoder(ffmpeg: str, codec: str, recv_rate: int):
    if codec == "aac-adts":
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-i", "pipe:0", "-ar", str(TARGET_PCM_RATE), "-ac", "1",
               "-f", "s16le", "pipe:1"]
    elif codec in ("mulaw", "alaw"):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-f", codec, "-ar", str(recv_rate), "-ac", "1",
               "-i", "pipe:0", "-ar", str(TARGET_PCM_RATE), "-ac", "1",
               "-f", "s16le", "pipe:1"]
    else:
        raise ValueError(f"unknown recv codec {codec}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)


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


def run_stt_loop(args, sock: socket.socket) -> int:
    ffmpeg = find_ffmpeg()
    model_path = Path(args.model)
    if not model_path.is_absolute():
        # resolve relative to repo root (parent of src/imou_talk)
        repo_root = Path(__file__).resolve().parent.parent.parent
        model_path = repo_root / model_path

    recognizer = None
    vosk_model = None
    if not args.no_stt:
        try:
            from vosk import KaldiRecognizer, Model  # type: ignore
        except ImportError:
            print("Missing dependency: python -m pip install vosk", file=sys.stderr)
            return 2
        ensure_vosk_model(model_path, auto_download=not args.no_auto_download)
        print(f"Loading Vosk model {model_path} ...", flush=True)
        vosk_model = Model(str(model_path))
        recognizer = KaldiRecognizer(vosk_model, TARGET_PCM_RATE)
        recognizer.SetWords(False)

    sock.settimeout(1.0)
    buf = bytearray()
    start = time.monotonic()
    stats: dict[int, int] = {}
    audio_stats: dict[int, int] = {}
    type_stats: dict[int, int] = {}
    invalid = 0
    skipped_video = 0
    detect_buf: list[bytes] = []
    codec = args.recv_codec if args.recv_codec != "auto" else None
    decoder = None
    pcm_accum = bytearray()
    wav_file = None
    raw_dump = None
    if args.dump_wav:
        wav_file = wave.open(args.dump_wav, "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_PCM_RATE)
    if args.dump_raw:
        raw_dump = open(args.dump_raw, "wb")

    stop_reader = threading.Event()
    pcm_queue: asyncio.Queue | None = None
    import queue as queue_mod
    pcm_q: queue_mod.Queue[bytes | None] = queue_mod.Queue(maxsize=256)
    last_partial = ""

    def decoder_reader():
        nonlocal last_partial
        pending = bytearray()
        silence_chunks = 0
        # VAD gate: only feed Vosk when PCM is loud enough; on long silence
        # flush the recognizer so comfort noise/decoder clicks cannot grow
        # into hallucinated words. Pure input-side filtering.
        vad_thr = getattr(args, "vad_threshold", 0.0) or 0.0
        reset_after = max(1, int(getattr(args, "vad_silence_reset_ms", 1500) / 125))
        inhibit_path = getattr(args, "inhibit_file", "") or ""

        def inhibited() -> bool:
            return bool(inhibit_path) and Path(inhibit_path).exists()

        while not stop_reader.is_set():
            if decoder is None or decoder.stdout is None:
                time.sleep(0.05)
                continue
            try:
                chunk = decoder.stdout.read(4000)
            except Exception:
                return
            if not chunk:
                time.sleep(0.02)
                continue
            if wav_file is not None:
                try:
                    wav_file.writeframes(chunk)
                except Exception:
                    pass
            lvl = rms_level(chunk)
            if args.no_stt or recognizer is None:
                if args.vu:
                    el = time.monotonic() - start
                    print(f"\r[{el:6.1f}s] |{vu_bar(lvl)}| {lvl:.3f}", end="", flush=True)
                continue
            if inhibited() or (vad_thr > 0 and lvl < vad_thr):
                silence_chunks += 1
                if silence_chunks >= reset_after:
                    # drop accumulated noise context; ignore errors
                    try:
                        recognizer.FinalResult()
                    except Exception:
                        pass
                    silence_chunks = 0
                if args.vu and silence_chunks % 4 == 0:
                    el = time.monotonic() - start
                    tag = "MUTE " if inhibited() else "mute "
                    print(f"\r[{el:6.1f}s] {tag}|{vu_bar(lvl)}| {lvl:.3f}", end="", flush=True)
                continue
            silence_chunks = 0
            pending += chunk
            # feed ~0.25s at a time is already chunk-sized; feed directly
            try:
                is_final = recognizer.AcceptWaveform(chunk)
            except Exception as exc:
                print(f"\nVosk error: {exc}", flush=True)
                continue
            el = time.monotonic() - start
            if is_final:
                try:
                    text = json.loads(recognizer.Result()).get("text", "").strip()
                except Exception:
                    text = ""
                if text:
                    print(f"\n[{el:6.1f}s] {text}", flush=True)
                    last_partial = ""
            else:
                try:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                except Exception:
                    partial = ""
                if partial and partial != last_partial:
                    last_partial = partial
                    if args.vu:
                        lvl = rms_level(chunk)
                        print(f"\r[{el:6.1f}s] |{vu_bar(lvl)}| ... {partial}", end="", flush=True)
                    else:
                        print(f"\r[{el:6.1f}s] ... {partial}", end="", flush=True)

    reader_thread = threading.Thread(target=decoder_reader, daemon=True)
    reader_thread.start()

    print("Listening to camera mic... walk away from the PC and speak into the camera.", flush=True)
    print("Press Ctrl+C to stop.\n", flush=True)
    try:
        while True:
            elapsed = time.monotonic() - start
            if args.seconds > 0 and elapsed >= args.seconds:
                break
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                print("\nCamera closed the stream.", flush=True)
                break
            buf += data
            while len(buf) >= 6:
                if buf[0] != 0x24:  # '$'
                    nxt = buf.find(b"$", 1)
                    if nxt == -1:
                        buf.clear()
                        break
                    del buf[:nxt]
                    continue
                ch = buf[1]
                flen = struct.unpack_from(">I", buf, 2)[0]
                if flen < 36 or flen > 2_000_000:
                    del buf[0]
                    invalid += 1
                    continue
                if len(buf) < 6 + flen:
                    break
                frame = bytes(buf[6:6 + flen])
                del buf[:6 + flen]
                parsed = parse_dhav_frame(frame)
                if parsed is None:
                    invalid += 1
                    continue
                payload, seq, _, frame_type, is_audio = parsed
                stats[ch] = stats.get(ch, 0) + 1
                type_stats[frame_type] = type_stats.get(frame_type, 0) + 1
                if not is_audio:
                    skipped_video += 1
                    continue
                if args.audio_channel >= 0 and ch != args.audio_channel:
                    continue
                audio_stats[ch] = audio_stats.get(ch, 0) + 1
                if raw_dump is not None:
                    try:
                        raw_dump.write(struct.pack("B", ch) + struct.pack(">I", len(payload)) + payload)
                    except Exception:
                        pass
                if decoder is None:
                    if codec is None:
                        detect_buf.append(payload)
                        if len(detect_buf) >= 30:
                            codec = guess_codec(detect_buf)
                            print(f"Detected incoming audio: {codec} "
                                  f"(audio channels: { {hex(k): v for k, v in audio_stats.items()} }, "
                                  f"all channels: { {hex(k): v for k, v in stats.items()} })", flush=True)
                            decoder = spawn_decoder(ffmpeg, codec, args.recv_rate)
                            try:
                                for p in detect_buf:
                                    decoder.stdin.write(p)  # type: ignore
                            except BrokenPipeError:
                                pass
                            detect_buf.clear()
                        continue
                    else:
                        decoder = spawn_decoder(ffmpeg, codec, args.recv_rate)
                else:
                    pass
                if decoder is not None and decoder.stdin is not None:
                    try:
                        decoder.stdin.write(payload)
                    except BrokenPipeError:
                        print("\nDecoder pipe broke; stopping.", flush=True)
                        break
                # periodic stats line when no audio yet
                if decoder is None and sum(stats.values()) % 50 == 0 and args.debug:
                    print(f"  ... waiting for codec detect, frames={dict(stats)} invalid={invalid}", flush=True)
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).", flush=True)
    finally:
        stop_reader.set()
        try:
            if decoder is not None and decoder.stdin is not None:
                decoder.stdin.close()
        except Exception:
            pass
        time.sleep(0.3)
        if decoder is not None:
            try:
                decoder.terminate()
            except Exception:
                pass
        if recognizer is not None and not args.no_stt:
            try:
                final_text = json.loads(recognizer.FinalResult()).get("text", "").strip()
                if final_text:
                    print(f"\n[final] {final_text}", flush=True)
            except Exception:
                pass
        if wav_file is not None:
            try:
                wav_file.close()
                print(f"Saved PCM 16k mono to {args.dump_wav}", flush=True)
            except Exception:
                pass
        if raw_dump is not None:
            try:
                raw_dump.close()
                print(f"Saved raw payloads to {args.dump_raw}", flush=True)
            except Exception:
                pass
        total = sum(stats.values())
        audio_total = sum(audio_stats.values())
        print(f"\nDone: {total} DHAV frames ({audio_total} audio, {skipped_video} video/other skipped), "
              f"audio_channels={ {hex(k): v for k, v in audio_stats.items()} }, "
              f"all_channels={ {hex(k): v for k, v in stats.items()} }, "
              f"types={ {hex(k): v for k, v in type_stats.items()} }, "
              f"invalid={invalid}, codec={codec or 'undetected (no audio?)'}", flush=True)
        if audio_total == 0:
            print("No AUDIO frames received (only video). Try --audio-channel 12 (0x0c) or --debug.", flush=True)
            return 3
        return 0


def run_listen(args, password: str) -> int:
    last_error: Exception | None = None
    for attempt in range(1, args.attempts + 1):
        client = None
        try:
            print(f"Opening visualtalk session (attempt {attempt}/{args.attempts})...", flush=True)
            client = VisualTalkClient(
                args.host, args.port, username=args.username, password=password,
                nonce=None, created=None, password_digest_override=None,
                lightweight_digest=None, timeout=args.timeout,
            )
            responses = client.start_talk(args)
            for index, response in enumerate(responses):
                print(f"Cseq {index}: {response.status_line} body={len(response.body)}", flush=True)
                if args.dump_body and response.body:
                    print(response.body[:500], flush=True)
                if response.code >= 400:
                    raise RuntimeError(f"camera rejected Cseq {index}: {response.status_line}")
            # receive-only: do NOT send_audio
            return run_stt_loop(args, client.sock)
        except (OSError, TimeoutError, EOFError, RuntimeError) as exc:
            last_error = exc
            print(f"Listen attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
            if attempt < args.attempts:
                time.sleep(args.retry_delay)
        finally:
            if client is not None:
                client.close()
    print(f"Listen was not started. Last error: {last_error}", file=sys.stderr, flush=True)
    return 1


async def listen_once(args) -> int:
    supplied = args.password or os.environ.get("IMOU_CAMERA_PASSWORD") or os.environ.get("IMOU_PASSWORD")
    from pathlib import Path as _P

    device = None
    if not (args.serial and supplied):
        import json as _json

        dev_path = _P(args.device_json)
        if dev_path.exists():
            devices = _json.loads(dev_path.read_text())
            device = devices[args.device_index]
    serial = args.serial or (device["Sn"] if device else None)
    password = supplied or (device["Pwd"] if device else None)
    if not serial or not password:
        print("Missing serial/password: use --serial/--password or IMOU_CAMERA_PASSWORD env (see .env).",
              file=sys.stderr)
        return 2
    ptcp = await p2p_handshake(serial, relay_mode=True, dtype=args.type,
                               username=args.username, password=password, debug=args.debug)
    tunnel = DHP2PTunnel(ptcp, args.remote_port, debug=args.debug)
    server_task = asyncio.create_task(tunnel.start(args.host, args.port))
    await asyncio.sleep(args.startup_delay)
    try:
        return await asyncio.to_thread(run_listen, args, password)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


def parse_args() -> argparse.Namespace:
    load_repo_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device-json", default="standalone-app/out/assets/device.json")
    p.add_argument("--device-index", type=int, default=0)
    p.add_argument("--serial")
    p.add_argument("--password")
    p.add_argument("--username", default="admin")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18086)
    p.add_argument("--remote-port", type=int, default=8086)
    p.add_argument("--type", type=int, default=0)
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--subtype", type=int, default=0)
    p.add_argument("--encrypt", type=int, default=3)
    p.add_argument("--track1", type=int, default=0)
    p.add_argument("--track2", type=int, default=0)
    p.add_argument("--talk-track", type=int, default=64)
    p.add_argument("--sdp")
    p.add_argument("--open-only", action="store_true")
    p.add_argument("--dump-body", action="store_true")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--startup-delay", type=float, default=2.0)
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--retry-delay", type=float, default=8.0)
    p.add_argument("--debug", action="store_true")
    # listen/STT specific
    p.add_argument("--seconds", type=float, default=0, help="0 = until Ctrl+C")
    p.add_argument("--model", default="models/vosk-model-small-vn-0.4")
    p.add_argument("--no-auto-download", action="store_true")
    p.add_argument("--recv-codec", choices=("auto", "aac-adts", "mulaw", "alaw"), default="auto")
    p.add_argument("--recv-rate", type=int, default=8000, help="assumed rate for G711 payloads")
    p.add_argument("--audio-channel", type=int, default=-1, help="interleaved channel to keep, -1 = all audio (e.g. 12 for 0x0c)")
    p.add_argument("--no-stt", action="store_true", help="only show audio level, no transcription")
    p.add_argument("--vu", action="store_true", help="show mic level meter (implied with --no-stt)")
    p.add_argument("--vad-threshold", type=float, default=0.03,
                   help="input-side energy gate: PCM chunks below this RMS are NOT sent to "
                        "Vosk (0 disables). Room silence p50 ~0.022, speech ~0.05+.")
    p.add_argument("--vad-silence-reset-ms", type=int, default=1500,
                   help="flush Vosk context after this much gated silence")
    p.add_argument("--inhibit-file", default="",
                   help="while this file exists, drain PCM but skip ASR (speaker echo guard)")
    p.add_argument("--dump-wav", default="", help="save decoded PCM as wav, e.g. mic-test.wav")
    p.add_argument("--dump-raw", default="", help="save raw DHAV payloads for debug")
    args = p.parse_args()
    if args.no_stt:
        args.vu = True
    return args


def main() -> int:
    # Vietnamese output on Windows cp1252 console would crash on đ/â/... 
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        pass
    args = parse_args()
    return asyncio.run(listen_once(args))


if __name__ == "__main__":
    raise SystemExit(main())

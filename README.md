# Imou camera WAV talkback

Send a prerecorded WAV file to an Imou camera speaker without Chrome, a virtual microphone, or laptop microphone capture.

This repository has been tested at the protocol level with an Imou Ranger Dual Pro 10MP (`IPC-S2XEP-10M0S`): the camera accepted the VisualTalk session (`200 OK`) and received DHAV audio frames. Actual audible output still depends on camera firmware, speaker volume, selected channel, and Imou cloud/P2P availability.

## Current technical approach

```text
WAV file
  -> ffmpeg: AAC-LC / ADTS, mono, 16 kHz
  -> DHAV interleaved audio frames
  -> visualtalk.xav talkback session
  -> DHP2P/PTCP relay tunnel (remote camera port 8086)
  -> Imou camera speaker
```

The implementation is a small, Python-standard-library VisualTalk sender derived from the public research project [home-assistant-tools/imou-life](https://github.com/home-assistant-tools/imou-life). It uses the camera's P2P route rather than direct RTSP, ONVIF, NetSDK, browser OpenSDK, or microphone loopback. Those approaches are intentionally not included here.

## Included files

- `audio/camera-test-vi.wav` — prerecorded Vietnamese test phrase.
- `scripts/speak-camera.ps1` — Windows entry point.
- `src/imou_talk/` — minimal DHP2P, WSSE, VisualTalk, and DHAV modules required for sending audio.
- `.env.example` — credential shape; the actual `.env` is ignored.

## Requirements

- Windows PowerShell.
- Python 3.10 or newer.
- Python package `imageio-ffmpeg` (the launcher locates its bundled `ffmpeg`).
- Network access to Imou's P2P service. No camera LAN port needs to be exposed.

Install the one dependency:

```powershell
python -m pip install imageio-ffmpeg
```

## Configuration

Copy `.env.example` to `.env`, then fill only these values:

```dotenv
IMOU_DEVICE_ID=your_camera_device_id
IMOU_PASSWORD=your_local_device_password_or_safety_code
```

Quoted values are accepted. Never commit `.env`, logs containing credentials, device serials, packet captures, or Imou access tokens.

## Send the bundled WAV

```powershell
.\scripts\speak-camera.ps1
```

Or send another audio file supported by ffmpeg:

```powershell
.\scripts\speak-camera.ps1 -AudioPath .\audio\my-message.wav
```

The script neither records nor opens the laptop microphone, and it does not play the WAV through the laptop speaker. It converts and sends the file only.

Successful transport looks like:

```text
Cseq 0: HTTP/1.1 200 OK
Cseq 1: HTTP/1.1 200 OK
Sent <n> DHAV audio frames to the camera.
```

`200 OK` plus frames sent verifies the P2P/VisualTalk transport; it is not by itself proof that the camera speaker is audible. If no sound is heard, tune the gain/codec/channel for the installed camera firmware.

## Listen to the camera mic (RTSP + VAD, separate from talkback)

Talkback (`visualtalk.xav`) is the machine-to-speaker direction. Mic capture
uses the live RTSP stream instead (`src/imou_talk/imou_rtsp_listen.py`):

```text
camera mic -> RTSP :554 /cam/realmonitor -> ffmpeg audio-only
-> PCM 16k mono -> energy VAD -> speech segments -> Vosk (vi)
```

LAN (same network as the camera):

```powershell
.\scripts\listen-camera-rtsp.ps1 -Seconds 30 -Vu
```

Remote (P2P relay to camera port 554, then RTSP locally):

```powershell
.\scripts\listen-camera-rtsp.ps1 -ViaP2p -Seconds 30 -Vu
```

Key options: `-VadThreshold 0.03` (room silence p50 ~0.022, speech ~0.05+),
`-MinSegRms 0.025` (segment-level noise gate), `-MaxSegmentS 8` (small Vosk
model degrades on longer segments), `-SilenceMs 800`, `-MinSpeechMs 300`,
`-InhibitFile <path>` (create this
file while the speaker plays TTS; ASR pauses plus a cooldown, PCM keeps
draining). Only speech segments reach Vosk, so room/comfort noise no longer
produces hallucinated transcripts. The legacy VisualTalk listener
(`listen-camera-stt.ps1`) also gained the same input-side gate
(`-VadThreshold`, `-InhibitFile`); the speak path is untouched.

### Pre-processing + STT engines (P1-P4, Ubuntu)

Direct `python` runs auto-load `.env` (`src/imou_talk/imou_env.py`), auto-save
each run to `audio/rtsp-YYYYMMDD-HHMMSS.wav`, and take `--ip/--username`
defaults from `IMOU_IP`/`IMOU_USER`, so this is enough:

```bash
python3 -u src/imou_talk/imou_rtsp_listen.py --seconds 30 --vu
```

New listen flags (also on `listen-camera-rtsp.ps1` as `-AudioFilter`,
`-Denoise`, `-Stt`, `-FwModel`):

- `--audio-filter` (default `highpass=f=80,afftdn=nr=12:nf=-25`),
  `--no-audio-filter` — ffmpeg pre-filter before VAD/STT.
- `--denoise rnnoise` (+ `--denoise-model models/rnnoise/cb.rnnn`,
  `--denoise-mix 0.9`) — RNNoise via `arnndn`. Download once:
  `curl -o models/rnnoise/cb.rnnn https://raw.githubusercontent.com/richardpl/arnndn-models/master/cb.rnnn`.
- `--no-agc` / `--agc-target 0.12` / `--min-snr-db 4.0` /
  `--no-quality-gate` — Python DSP in `src/imou_talk/imou_audio_pre.py`
  (DC-block, per-segment AGC + limiter, SNR/ZCR gate; `--debug` prints
  `rms/snr/zcr/gain` per segment).
- `--stt faster-whisper` (default) + `--fw-model medium` (default) +
  `--fw-lang vi` — fw-small nhanh hơn (~2.6x) nhưng WER cao hơn ~50% tương
  đối; `--stt vosk` giữ lại làm fallback. Ubuntu:
  `pip3 install --user faster-whisper` (model downloads to
  `~/.cache/huggingface` on first run).

Measured on the bundled captures (Ubuntu 22.04, ffmpeg 4.4):

| file | Vosk-small | faster-whisper tiny | faster-whisper small |
|---|---|---|---|
| `audio/camera-test-vi.wav` (clean) | wrong (`...cảm mẹ già`) | `Xin chào, đây là thử là camera.` (0.2s) | `Xin chào, đây là Thử Lo Camera.` (1.0s) |
| `audio/rtsp-*.wav` (noisy cam) | word-salad hallucination | shorter hallucination | numbers hallucination |

Takeaway: Vosk-small is the bottleneck even on clean audio; default is now
`--stt faster-whisper --fw-model medium` for Vietnamese accuracy (still
realtime: RTF ~0.4 on CPU; `small` is ~2.6x faster at ~50% higher WER).
Denoise lowers the noise floor
(RMS p50 0.008 → 0.002, VAD hits 23% → 16%) but cannot fix a weak model.

Offline replay for tuning (no camera needed — a local `.wav` goes through
the same ffmpeg → VAD → pre → STT path, trailing segments are flushed):

```bash
python3 -u src/imou_talk/imou_rtsp_listen.py --rtsp-url audio/rtsp-XXX.wav --vu --debug --no-auto-dump
python3 scripts/eval_baseline.py            # RMS/DC/clip/VAD/Vosk per file -> audio/baseline-*.json
python3 scripts/eval_stt.py --fw-models tiny,small   # vosk vs whisper -> audio/eval-stt-*.json
```

If RTSP refuses connections (RST / `Failed reading RTSP data -10054`) while
VisualTalk still returns `200 OK`, the camera's RTSP service is locked or
down: wait out the login lockout, reboot via the Imou app, confirm the LAN
IP and device password, and ensure RTSP/ONVIF is enabled. Verified working
profile earlier: `channel=1 subtype=0/1`, audio `AAC LC 16 kHz mono`
(`subtype=1` is lighter: H264 640x480).

## Security and scope

Use this only with cameras and accounts you own or are authorized to administer. This is interoperability/research code, not an official Imou SDK. Firmware and cloud-side protocol changes can break it.

## Upstream attribution

The protocol modules are derived from [imou-life](https://github.com/home-assistant-tools/imou-life), retained here only as the minimal code path needed for WAV talkback. Consult that project for its license, notices, and broader bridge functionality.

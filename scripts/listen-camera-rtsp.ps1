param(
    [string]$Ip = "",
    [int]$Channel = 1,
    [int]$Subtype = 1,
    [string]$RtspUrl = "",
    [double]$Seconds = 0,
    [string]$Model = "models/vosk-model-small-vn-0.4",
    [string]$DumpWav = "",
    [string]$DumpSegments = "",
    [string]$DumpJson = "",
    [string]$InhibitFile = "",
    [double]$VadThreshold = 0.03,
    [double]$MinSegRms = 0.025,
    [double]$MaxSegmentS = 8,
    [int]$SilenceMs = 800,
    [int]$MinSpeechMs = 300,
    [string]$AudioFilter = "",
    [string]$Denoise = "off",
    [string]$Stt = "faster-whisper",
    [string]$FwModel = "medium",
    [switch]$NoAudioFilter,
    [switch]$ViaP2p,
    [switch]$NoStt,
    [switch]$Vu,
    [switch]$Debug
)

$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$taskEnvFile = Join-Path $taskRoot ".env"
$taskListener = Join-Path $taskRoot "src\imou_talk\imou_rtsp_listen.py"

if (!(Test-Path -LiteralPath $taskListener)) { throw "Missing listener: $taskListener" }

$taskEnv = @{}
if (Test-Path -LiteralPath $taskEnvFile) {
    Get-Content -LiteralPath $taskEnvFile | ForEach-Object {
        if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $taskValue = $Matches[2].Trim()
            if ($taskValue.Length -ge 2 -and (
                ($taskValue.StartsWith('"') -and $taskValue.EndsWith('"')) -or
                ($taskValue.StartsWith("'") -and $taskValue.EndsWith("'"))
            )) {
                $taskValue = $taskValue.Substring(1, $taskValue.Length - 2)
            }
            $taskEnv[$Matches[1]] = $taskValue
        }
    }
}

$taskIp = $Ip
if ($taskIp -eq "" -and $taskEnv.IMOU_IP) { $taskIp = $taskEnv.IMOU_IP }
if ($taskIp -eq "") { $taskIp = "192.168.1.38" }
$taskUser = $taskEnv.IMOU_USER
if (!$taskUser) { $taskUser = "admin" }
$taskPass = $taskEnv.IMOU_PASSWORD
if (!$taskPass) { $taskPass = $taskEnv.IMOU_DEVICE_CODE }

$taskFfmpeg = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
$env:Path = (Split-Path $taskFfmpeg) + ';' + $env:Path
$env:IMOU_CAMERA_PASSWORD = $taskPass

python -c "import vosk" 2>$null
if ($LASTEXITCODE -ne 0 -and !$NoStt -and $Stt -eq "vosk") {
    Write-Host "Installing vosk (one time)..." -ForegroundColor Yellow
    python -m pip install vosk
    if ($LASTEXITCODE -ne 0) { throw "Failed to install vosk" }
}
if ($Stt -ne "vosk" -and !$NoStt) {
    python -c "import faster_whisper" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing faster-whisper (one time)..." -ForegroundColor Yellow
        python -m pip install faster-whisper
        if ($LASTEXITCODE -ne 0) { throw "Failed to install faster-whisper" }
    }
}

$taskArgs = @("-u", $taskListener, "--username", $taskUser, "--password", $taskPass,
    "--channel", "$Channel", "--subtype", "$Subtype",
    "--seconds", "$Seconds", "--model", $Model,
    "--vad-threshold", "$VadThreshold", "--silence-ms", "$SilenceMs", "--min-speech-ms", "$MinSpeechMs",
    "--min-seg-rms", "$MinSegRms", "--max-segment-s", "$MaxSegmentS")
if ($RtspUrl -ne "") { $taskArgs += @("--rtsp-url", $RtspUrl) } else { $taskArgs += @("--ip", $taskIp) }
if ($ViaP2p) { $taskArgs += "--via-p2p"; $taskArgs += @("--serial", $taskEnv.IMOU_DEVICE_ID) }
if ($DumpWav -ne "") { $taskArgs += @("--dump-wav", $DumpWav) }
if ($DumpSegments -ne "") { $taskArgs += @("--dump-segments", $DumpSegments) }
if ($DumpJson -ne "") { $taskArgs += @("--dump-json", $DumpJson) }
if ($InhibitFile -ne "") { $taskArgs += @("--inhibit-file", $InhibitFile) }
if ($AudioFilter -ne "") { $taskArgs += @("--audio-filter", $AudioFilter) }
if ($Denoise -ne "" -and $Denoise -ne "off") { $taskArgs += @("--denoise", $Denoise) }
if ($Stt -ne "" -and $Stt -ne "vosk") { $taskArgs += @("--stt", $Stt) }
if ($FwModel -ne "") { $taskArgs += @("--fw-model", $FwModel) }
if ($NoAudioFilter) { $taskArgs += "--no-audio-filter" }
if ($NoStt) { $taskArgs += "--no-stt" }
if ($Vu) { $taskArgs += "--vu" }
if ($Debug) { $taskArgs += "--debug" }

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 | Out-Null } catch {}

Write-Host "Nghe mic camera qua RTSP (audio-only + VAD). Noi vao camera, nhan Ctrl+C de dung." -ForegroundColor Green
python @taskArgs
exit $LASTEXITCODE

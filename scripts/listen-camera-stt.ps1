param(
    [double]$Seconds = 0,
    [string]$Model = "models/vosk-model-small-vn-0.4",
    [string]$DumpWav = "",
    [switch]$NoStt,
    [switch]$Vu,
    [string]$RecvCodec = "auto",
    [int]$AudioChannel = -1,
    [double]$VadThreshold = 0.03,
    [string]$InhibitFile = ""
)

$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$taskEnvFile = Join-Path $taskRoot ".env"
$taskListener = Join-Path $taskRoot "src\imou_talk\imou_listen_stt.py"

if (!(Test-Path -LiteralPath $taskEnvFile)) { throw "Missing .env" }
if (!(Test-Path -LiteralPath $taskListener)) { throw "Missing listener: $taskListener" }

$taskEnv = @{}
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

if (!$taskEnv.IMOU_DEVICE_ID -or !$taskEnv.IMOU_PASSWORD) {
    throw "IMOU_DEVICE_ID and IMOU_PASSWORD must be set in .env"
}

# ffmpeg via imageio-ffmpeg (same approach as speak-camera.ps1)
$taskFfmpeg = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
$env:Path = (Split-Path $taskFfmpeg) + ';' + $env:Path
$env:IMOU_CAMERA_PASSWORD = $taskEnv.IMOU_PASSWORD

# Vosk dependency check
python -c "import vosk" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing vosk (one time)..." -ForegroundColor Yellow
    python -m pip install vosk
    if ($LASTEXITCODE -ne 0) { throw "Failed to install vosk" }
}

$taskArgs = @(
    "-u", $taskListener,
    "--serial", $taskEnv.IMOU_DEVICE_ID,
    "--seconds", "$Seconds",
    "--model", $Model,
    "--recv-codec", $RecvCodec,
    "--audio-channel", "$AudioChannel",
    "--timeout", "20",
    "--attempts", "3",
    "--retry-delay", "8",
    "--vad-threshold", "$VadThreshold"
)
if ($InhibitFile -ne "") { $taskArgs += @("--inhibit-file", $InhibitFile) }
if ($DumpWav -ne "") { $taskArgs += @("--dump-wav", $DumpWav) }
if ($NoStt) { $taskArgs += "--no-stt" }
if ($Vu) { $taskArgs += "--vu" }

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 | Out-Null } catch {}

Write-Host "Nghe mic camera, ban ra xa va noi vao camera. Nhan Ctrl+C de dung." -ForegroundColor Green
python @taskArgs
exit $LASTEXITCODE

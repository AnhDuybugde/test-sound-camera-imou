param(
    [string]$AudioPath = (Join-Path $PSScriptRoot "..\audio\camera-test-vi.wav")
)

$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$taskEnvFile = Join-Path $taskRoot ".env"
$taskSender = Join-Path $taskRoot "src\imou_talk\imou_pure_talk.py"

if (!(Test-Path -LiteralPath $taskEnvFile)) { throw "Missing .env" }
if (!(Test-Path -LiteralPath $taskSender)) { throw "Missing VisualTalk sender: $taskSender" }
if (!(Test-Path -LiteralPath $AudioPath)) { throw "Audio file not found: $AudioPath" }

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

$taskFfmpeg = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
$env:Path = (Split-Path $taskFfmpeg) + ';' + $env:Path
$env:IMOU_CAMERA_PASSWORD = $taskEnv.IMOU_PASSWORD

python -u $taskSender --serial $taskEnv.IMOU_DEVICE_ID --audio $AudioPath --codec aac-adts --sample-rate 16000 --timeout 20 --attempts 3 --retry-delay 8
exit $LASTEXITCODE

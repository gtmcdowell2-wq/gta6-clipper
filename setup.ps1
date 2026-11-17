# Windows setup: run from the project folder in PowerShell.
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

# Python 3.11+ (the Microsoft Store "python" stub doesn't count)
$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3.13", "python")) {
    $exe, $rest = $c.Split(" ")
    if (Have $exe) {
        $v = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($v -eq "True") { $py = $c; break }
    }
}
if (-not $py) {
    Write-Host "Installing Python 3.12..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Refresh-Path
    $py = "py -3.12"
}
Write-Host "Python: $py"

if (-not (Have "git")) {
    Write-Host "Installing git..."
    winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
}

# Gyan.FFmpeg is the full build (libass for captions, NVENC, loudnorm)
if (-not (Have "ffmpeg")) {
    Write-Host "Installing ffmpeg (full build)..."
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
}
Refresh-Path

if (-not (Test-Path ".venv")) {
    $exe, $rest = $py.Split(" ")
    & $exe @rest -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# GPU transcription needs NVIDIA's cuBLAS + cuDNN runtime libraries.
if (Have "nvidia-smi") {
    Write-Host "NVIDIA GPU found: installing CUDA runtime libraries for Whisper..."
    & .\.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"
}

# TwitchDownloaderCLI fetches Twitch chat replays (the chat-velocity signal).
if (-not (Test-Path "tools\TwitchDownloaderCLI.exe")) {
    Write-Host "Downloading TwitchDownloaderCLI..."
    $ver = "1.56.5"
    New-Item -ItemType Directory -Force -Path tools | Out-Null
    $zip = Join-Path $env:TEMP "TwitchDownloaderCLI.zip"
    Invoke-WebRequest -Uri "https://github.com/lay295/TwitchDownloader/releases/download/$ver/TwitchDownloaderCLI-$ver-Windows-x64.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath tools -Force
    Remove-Item $zip
}

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

& .\.venv\Scripts\python.exe run_pipeline.py doctor
Write-Host ""
Write-Host "Next: put your keys in .env, then run:  .\.venv\Scripts\python.exe run_pipeline.py start"

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host '=== SubLearn AI Demo ===' -ForegroundColor Cyan

$Py = Get-Command py -ErrorAction SilentlyContinue
if (-not $Py) { throw 'Chua tim thay Python Launcher (py). Hay cai Python 3.11+ va thu lai.' }

$AiDir = Join-Path $Root 'ai-service'
$VenvPython = Join-Path $AiDir '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host 'Dang tao Python virtual environment...' -ForegroundColor Yellow
    Push-Location $AiDir
    py -m venv .venv
    Pop-Location
}

Write-Host 'Dang cai dat Python dependencies...' -ForegroundColor Yellow
& (Join-Path $AiDir '.venv\Scripts\python.exe') -m pip install -r (Join-Path $AiDir 'requirements.txt')

$FrontDir = Join-Path $Root 'frontend'
if (-not (Test-Path (Join-Path $FrontDir 'node_modules\onnxruntime-web')) -or
    -not (Test-Path (Join-Path $FrontDir 'node_modules\@tailwindcss\vite'))) {
    Write-Host 'Dang cai dat Node dependencies...' -ForegroundColor Yellow
    Push-Location $FrontDir
    npm install
    Pop-Location
}

Start-Process powershell -ArgumentList '-NoExit','-Command',"Set-Location '$AiDir'; & '.\.venv\Scripts\Activate.ps1'; uvicorn app.main:app --reload --host 127.0.0.1 --port 8001"
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList '-NoExit','-Command',"Set-Location '$FrontDir'; npm run dev"
Write-Host 'Da mo 2 terminal. Truy cap http://localhost:5173' -ForegroundColor Green

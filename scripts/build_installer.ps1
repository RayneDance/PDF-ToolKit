$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$portableDir = Join-Path $repoRoot "dist\pdf-toolkit-gui"
$releaseDir = Join-Path $repoRoot "dist\release"
$stageDir = Join-Path $repoRoot "dist\installer-stage"
$appStageDir = Join-Path $stageDir "app"
$iconPath = Join-Path $stageDir "pdf-toolkit-installer.ico"
$issPath = Join-Path $repoRoot "installer\pdf-toolkit.iss"

function Get-PythonExe {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }
    return "python"
}

function Get-IsccExe {
    if ($env:ISCC_PATH -and (Test-Path $env:ISCC_PATH)) {
        return $env:ISCC_PATH
    }

    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "Inno Setup was not found. Install Inno Setup 6, add iscc.exe to PATH, or set ISCC_PATH."
}

$pythonExe = Get-PythonExe
if (-not (Test-Path $portableDir)) {
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_gui.ps1")
}

if (-not (Test-Path $portableDir)) {
    throw "Portable app folder not found at $portableDir"
}

if (Test-Path $stageDir) {
    Remove-Item $stageDir -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
New-Item -ItemType Directory -Force -Path $appStageDir | Out-Null
Copy-Item -Path (Join-Path $portableDir "*") -Destination $appStageDir -Recurse -Force

& $pythonExe (Join-Path $repoRoot "scripts\export_app_icon.py") --output $iconPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to generate installer icon."
}

$appVersion = & $pythonExe -c "import sys; sys.path.insert(0, r'$repoRoot\src'); import pdf_toolkit; print(pdf_toolkit.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to resolve application version."
}
$appVersion = $appVersion.Trim()

$isccExe = Get-IsccExe
& $isccExe "/DAppVersion=$appVersion" "/DSourceDir=$appStageDir" "/DOutputDir=$releaseDir" "/DSetupIconFile=$iconPath" $issPath
if ($LASTEXITCODE -ne 0) {
    throw "Installer build failed."
}

Write-Host "Created installer:"
Write-Host " - $(Join-Path $releaseDir 'pdf-toolkit-setup-windows-x64.exe')"

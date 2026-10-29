#Requires -Version 5.1
<#
.SYNOPSIS
    Собирает Python-сайдкар VoiceRunaCore в PyInstaller onedir.

.DESCRIPTION
    Шаги:
      1. venv в packaging/.venv (Python 3.12, TZ §4);
      2. pip install -r sidecar/requirements.txt + PyInstaller 6;
      3. PyInstaller -> packaging/dist/sidecar;
      4. проверка, что есть и voiceruna-sidecar.exe, и папка _internal.

    Именно эту папку packaging/build-app.ps1 копирует в app/src-tauri/resources/sidecar,
    откуда bundle.resources tauri.conf.json кладёт её в установщик как "sidecar/".

    Результат:
        packaging/dist/sidecar/voiceruna-sidecar.exe
        packaging/dist/sidecar/_internal/...

.PARAMETER PythonVersion
    Версия Python для venv. По умолчанию 3.12.

.PARAMETER PythonPath
    Явный путь к python.exe; перебивает -PythonVersion.

.PARAMETER Clean
    Удалить packaging/dist и packaging/.pyinstaller перед сборкой.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging/build-sidecar.ps1 -Clean
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12',
    [string]$PythonPath = '',
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
if (-not $PackagingDir) {
    $PackagingDir = (Get-Location).Path
}
$RepoRoot = Split-Path -Parent $PackagingDir
$SidecarSrc = Join-Path $RepoRoot 'sidecar'
$Requirements = Join-Path $SidecarSrc 'requirements.txt'
$SpecFile = Join-Path (Join-Path $PackagingDir 'pyinstaller') 'voiceruna-sidecar.spec'
$VenvDir = Join-Path $PackagingDir '.venv'
$VenvPython = Join-Path (Join-Path $VenvDir 'Scripts') 'python.exe'
$DistDir = Join-Path $PackagingDir 'dist'
$WorkDir = Join-Path $PackagingDir '.pyinstaller'
$OutDir = Join-Path $DistDir 'sidecar'
$OutExe = Join-Path $OutDir 'voiceruna-sidecar.exe'
$OutInternal = Join-Path $OutDir '_internal'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Remove-Safe([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if (-not $full.StartsWith($PackagingDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Отказ удалять путь вне packaging/: $full"
    }
    Write-Host "    удаляю $full"
    Remove-Item -LiteralPath $full -Recurse -Force
}

function Resolve-Python {
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath)) {
            throw "PythonPath не существует: $PythonPath"
        }
        return @{ Program = $PythonPath; Args = @() }
    }

    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source "-$PythonVersion" --version
        if ($LASTEXITCODE -eq 0) {
            return @{ Program = $py.Source; Args = @("-$PythonVersion") }
        }
    }

    $python = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($python) {
        $reported = (& $python.Source --version) 2>&1
        if ("$reported" -match 'Python\s+3\.(\d+)') {
            if ($Matches[1] -eq $PythonVersion.Split('.')[1]) {
                return @{ Program = $python.Source; Args = @() }
            }
            throw ("Найден Python $reported, а нужен $PythonVersion (TZ §4). " +
                   "Поставьте Python $PythonVersion или укажите -PythonPath.")
        }
    }
    throw ("Python $PythonVersion не найден. Установите Python $PythonVersion " +
           "(https://www.python.org/downloads/ или winget install Python.Python.3.12) " +
           "либо укажите -PythonPath C:\path\to\python.exe.")
}

if (-not (Test-Path -LiteralPath $SpecFile)) {
    throw "Не найден spec: $SpecFile"
}
if (-not (Test-Path -LiteralPath $Requirements)) {
    throw "Не найден requirements: $Requirements"
}
$SidecarEntry = Join-Path (Join-Path $SidecarSrc 'voiceruna') '__main__.py'
if (-not (Test-Path -LiteralPath $SidecarEntry)) {
    throw "Не найден вход сайдкара: $SidecarEntry"
}

if ($Clean) {
    Write-Step 'Чистка прошлых сборок'
    Remove-Safe $DistDir
    Remove-Safe $WorkDir
}

Write-Step "Python ${PythonVersion}: проверка"
$python = Resolve-Python
Write-Host "    $($python.Program) $($python.Args -join ' ')"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Step "Создаю venv: $VenvDir"
    $venvArgs = @()
    $venvArgs += $python.Args
    $venvArgs += @('-m', 'venv', $VenvDir)
    & $python.Program @venvArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Не удалось создать venv (код выхода ' + $LASTEXITCODE + ').'
    }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "venv создан, но нет $VenvPython"
}

Write-Step 'pip: обновление pip/wheel'
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip wheel
if ($LASTEXITCODE -ne 0) {
    throw 'Не удалось обновить pip/wheel.'
}

Write-Step "pip install -r $Requirements"
& $VenvPython -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) {
    throw 'Не удалось поставить зависимости сайдкара.'
}

Write-Step 'pip install PyInstaller'
& $VenvPython -m pip install --disable-pip-version-check "pyinstaller>=6.11,<7"
if ($LASTEXITCODE -ne 0) {
    throw 'Не удалось поставить PyInstaller 6.'
}

Write-Step 'PyInstaller: onedir-сборка'
$env:VOX_REPO_ROOT = $RepoRoot
$pyinstallerArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--clean',
    '--distpath', $DistDir,
    '--workpath', $WorkDir,
    $SpecFile
)
& $VenvPython @pyinstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller завершился с кодом $LASTEXITCODE."
}

Write-Step 'Проверка результата'
if (-not (Test-Path -LiteralPath $OutExe)) {
    throw "Нет exe сайдкара: $OutExe"
}
if (-not (Test-Path -LiteralPath $OutInternal)) {
    throw ("Нет папки _internal в $OutDir. Это признак onefile или PyInstaller < 6; " +
           "TZ §3 требует onedir (exe + _internal).")
}

$totalBytes = (Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
if (-not $totalBytes) {
    $totalBytes = 0
}
Write-Host ''
Write-Host "Готово: $OutExe" -ForegroundColor Green
Write-Host ("Размер папки sidecar: {0:N1} МБ" -f ($totalBytes / 1MB))
Write-Host 'Дальше: powershell -ExecutionPolicy Bypass -File packaging/build-app.ps1'

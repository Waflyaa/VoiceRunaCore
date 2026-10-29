#Requires -Version 5.1
<#
.SYNOPSIS
    Собирает установщик VoiceRunaCore (Tauri 2 + NSIS) целиком.

.DESCRIPTION
    Порядок:
      1. проверка тулчейна: Node.js/npm, Rust (cargo), MSVC Build Tools, @tauri-apps/cli;
      2. проверка ресурса сайдкара: packaging/dist/sidecar (exe + _internal);
      3. копирование сайдкара в app/src-tauri/resources/sidecar (это и есть bundle.resources);
      4. npm ci && npm run build в app/ (если не -SkipFrontend);
      5. tauri build -> app/src-tauri/target/release/bundle/nsis/*.exe.

    Почему копирование, а не путь ../../packaging/dist/sidecar прямо в конфиге:
    tauri-build копирует bundle.resources в target/<profile> на КАЖДОЙ сборке, включая
    cargo check/tauri dev. Если источник — собранный сайдкар, tauri dev не запустится
    без 150 МБ PyInstaller-сборки. Поэтому в репозитории лежит пустой каталог
    resources/sidecar/.gitkeep (dev работает через python -m voiceruna), а этот скрипт
    наполняет его настоящим onedir перед релизной сборкой.

.PARAMETER BuildSidecar
    Сначала запустить packaging/build-sidecar.ps1 (нужен Python 3.12).

.PARAMETER SkipFrontend
    Не запускать npm ci / npm run build (фронтенд уже собран в app/dist).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging/build-app.ps1 -BuildSidecar
#>
[CmdletBinding()]
param(
    [switch]$BuildSidecar,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
if (-not $PackagingDir) {
    $PackagingDir = (Get-Location).Path
}
$RepoRoot = Split-Path -Parent $PackagingDir
$AppDir = Join-Path $RepoRoot 'app'
$TauriDir = Join-Path $AppDir 'src-tauri'
$ConfFile = Join-Path $TauriDir 'tauri.conf.json'
$SidecarDir = Join-Path (Join-Path $PackagingDir 'dist') 'sidecar'
$SidecarExe = Join-Path $SidecarDir 'voiceruna-sidecar.exe'
$SidecarInternal = Join-Path $SidecarDir '_internal'
$ResourceDir = Join-Path (Join-Path $TauriDir 'resources') 'sidecar'
$ResourceExe = Join-Path $ResourceDir 'voiceruna-sidecar.exe'
$ResourceInternal = Join-Path $ResourceDir '_internal'
$NsisDir = Join-Path $TauriDir 'target\release\bundle\nsis'
$TauriCli = Join-Path $AppDir 'node_modules\.bin\tauri.cmd'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-MsvcBuildTools {
    $programFilesX86 = ${env:ProgramFiles(x86)}
    if ($programFilesX86) {
        $vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path -LiteralPath $vswhere) {
            # -all обязателен: установка в состоянии "reboot required" (isComplete=false)
            # не попадает в -latest, хотя компилятор уже установлен и работает.
            $installation = & $vswhere -all -prerelease -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
            if ($LASTEXITCODE -eq 0 -and $installation) {
                return $true
            }
            foreach ($candidate in @($installation)) {
                if (-not $candidate) { continue }
                $vcvars = Join-Path $candidate 'VC\Auxiliary\Build\vcvars64.bat'
                if (Test-Path -LiteralPath $vcvars) {
                    return $true
                }
            }
        }
    }
    if (Get-Command 'cl.exe' -ErrorAction SilentlyContinue) {
        return $true
    }
    if (Get-Command 'link.exe' -ErrorAction SilentlyContinue) {
        return $true
    }
    return $false
}

function Assert-Tools {
    $missing = New-Object System.Collections.ArrayList
    if (-not (Get-Command 'node' -ErrorAction SilentlyContinue)) {
        [void]$missing.Add('Node.js LTS 20+ — https://nodejs.org (вместе с ним ставится npm)')
    }
    if (-not (Get-Command 'npm' -ErrorAction SilentlyContinue)) {
        [void]$missing.Add('npm — ставится вместе с Node.js')
    }
    if (-not (Get-Command 'cargo' -ErrorAction SilentlyContinue)) {
        [void]$missing.Add('Rust stable — winget install Rustlang.Rustup; затем: rustup default stable-x86_64-pc-windows-msvc')
    }
    if (-not (Test-MsvcBuildTools)) {
        [void]$missing.Add('MSVC Build Tools — Visual Studio Installer -> Desktop development with C++ (MSVC v143 + Windows 10/11 SDK)')
    }
    if (-not (Test-Path -LiteralPath $TauriCli)) {
        [void]$missing.Add('@tauri-apps/cli в app/ — npm install --save-dev @tauri-apps/cli@^2 (в package.json его пока нет)')
    }
    if ($missing.Count -gt 0) {
        Write-Host 'Не хватает компонентов для сборки Tauri:' -ForegroundColor Red
        foreach ($item in $missing) {
            Write-Host "  - $item" -ForegroundColor Red
        }
        throw 'Сборка невозможна: установите перечисленные компоненты и повторите запуск.'
    }
}

if (-not (Test-Path -LiteralPath $ConfFile)) {
    throw "Не найден tauri.conf.json: $ConfFile"
}
if (-not (Test-Path -LiteralPath (Join-Path $AppDir 'package.json'))) {
    throw "Не найден app/package.json — фронтенд ещё не создан?"
}

Assert-Tools
Write-Step 'Тулчейн найден: node, npm, cargo, MSVC, @tauri-apps/cli'

if ($BuildSidecar) {
    Write-Step 'Сайдкар: packaging/build-sidecar.ps1'
    & (Join-Path $PackagingDir 'build-sidecar.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'build-sidecar.ps1 завершился с ошибкой.'
    }
}

Write-Step 'Проверка собранного сайдкара (TZ §3, onedir)'
if (-not (Test-Path -LiteralPath $SidecarExe)) {
    throw ("Нет собранного сайдкара: $SidecarExe" + [Environment]::NewLine +
           "Сначала: powershell -ExecutionPolicy Bypass -File packaging/build-sidecar.ps1" + [Environment]::NewLine +
           "или повторите этот скрипт с -BuildSidecar.")
}
if (-not (Test-Path -LiteralPath $SidecarInternal)) {
    throw ("В $SidecarDir нет папки _internal: сайдкар собран onefile или старым PyInstaller." + [Environment]::NewLine +
           "TZ §3 требует onedir (exe + _internal). Пересоберите: packaging/build-sidecar.ps1 -Clean")
}
Write-Host "    ok: $SidecarExe (+ _internal)"

Write-Step "Копирую сайдкар в ресурсы: $ResourceDir"
if (-not (Test-Path -LiteralPath $ResourceDir)) {
    New-Item -ItemType Directory -Path $ResourceDir -Force | Out-Null
}
# .gitkeep остаётся, остальное пересобираем, чтобы не смешать версии
Get-ChildItem -LiteralPath $ResourceDir -Force |
    Where-Object { $_.Name -ne '.gitkeep' } |
    Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $SidecarDir '*') -Destination $ResourceDir -Recurse -Force
if (-not (Test-Path -LiteralPath $ResourceExe)) {
    throw "После копирования нет $ResourceExe — проверьте $SidecarDir"
}
if (-not (Test-Path -LiteralPath $ResourceInternal)) {
    throw "После копирования нет $ResourceInternal"
}
$resourceBytes = (Get-ChildItem -LiteralPath $ResourceDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
if (-not $resourceBytes) {
    $resourceBytes = 0
}
Write-Host ("    ok: ресурс sidecar/ = {0:N1} МБ" -f ($resourceBytes / 1MB))

if (-not $SkipFrontend) {
    Write-Step 'Фронтенд: npm ci && npm run build'
    Push-Location $AppDir
    try {
        if (Test-Path -LiteralPath (Join-Path $AppDir 'package-lock.json')) {
            & npm ci
        }
        else {
            & npm install
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'Установка npm-зависимостей завершилась с ошибкой.'
        }
        & npm run build
        if ($LASTEXITCODE -ne 0) {
            throw 'npm run build завершился с ошибкой.'
        }
    }
    finally {
        Pop-Location
    }
}
else {
    $distIndex = Join-Path (Join-Path $AppDir 'dist') 'index.html'
    if (-not (Test-Path -LiteralPath $distIndex)) {
        Write-Warning "-SkipFrontend указан, но $distIndex отсутствует: tauri build упадёт."
    }
}

Write-Step 'Tauri: сборка NSIS-установщика'
Push-Location $AppDir
try {
    & $TauriCli build
    if ($LASTEXITCODE -ne 0) {
        throw "tauri build завершился с кодом $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Step 'Поиск установщика'
if (-not (Test-Path -LiteralPath $NsisDir)) {
    throw "Нет папки NSIS: $NsisDir (tauri build не дошёл до bundler?)"
}
# @(...) обязателен: Set-StrictMode + единственный найденный файл (скаляр) ломает .Count
$installers = @(Get-ChildItem -LiteralPath $NsisDir -Filter '*.exe' -File -ErrorAction SilentlyContinue |
    Sort-Object -Property LastWriteTime -Descending)
if (-not $installers -or $installers.Count -eq 0) {
    throw "В $NsisDir нет установщика *.exe"
}
$installer = $installers[0]
$sizeMb = $installer.Length / 1MB

Write-Host ''
Write-Host 'Готово.' -ForegroundColor Green
Write-Host "Установщик: $($installer.FullName)"
Write-Host ("Размер: {0:N1} МБ" -f $sizeMb)
Write-Host ''
Write-Host 'Проверка после установки:'
Write-Host '  - иконка в трее, клик открывает окно, Ctrl+Alt+V показывает окно и сразу слушает;'
Write-Host '  - крестик скрывает окно, выход — только пункт «Выход» в трее;'
Write-Host '  - второй запуск открывает окно первого экземпляра;'
Write-Host '  - сайдкар слушает только 127.0.0.1 (см. logs\sidecar.out.log в %APPDATA%\VoiceRunaCore).'

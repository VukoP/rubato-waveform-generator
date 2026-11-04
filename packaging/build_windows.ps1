# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
# Builds dist/Rubato-Waveform-Generator-<version>-windows-x64.zip from a Python environment that
# has requirements-dev.txt installed and the package installed (pip install -e .).
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE" }
}

$name = 'Rubato Waveform Generator'
$version = (& python -B -c 'import rubato_waveform; print(rubato_waveform.__version__)').Trim()
$zip = "dist/Rubato-Waveform-Generator-$version-windows-x64.zip"

Remove-Item -Recurse -Force dist, "build/$name" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force build | Out-Null
Invoke-Checked python @('-B', 'packaging/make_third_party_licenses.py', 'build/THIRD_PARTY_LICENSES.txt')
Invoke-Checked python @('-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', 'dist', '--workpath', 'build', 'packaging/rubato_waveform.spec')

Compress-Archive -Path "dist/$name" -DestinationPath $zip
Write-Host "Built $zip"

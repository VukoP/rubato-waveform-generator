#!/usr/bin/env bash
# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
# Builds dist/Rubato-Waveform-Generator-<version>-macos-arm64.zip from a Python environment that
# has requirements-dev.txt installed and the package installed (pip install -e .).
#   CODESIGN_IDENTITY  optional signing identity; unset means an ad-hoc signature ("-").
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

py="${PYTHON:-python}"
name="Rubato Waveform Generator"
version="$("$py" -B -c 'import rubato_waveform; print(rubato_waveform.__version__)')"
app="dist/$name.app"
zip="dist/Rubato-Waveform-Generator-$version-macos-arm64.zip"
identity="${CODESIGN_IDENTITY:--}"

rm -rf dist "build/$name"
mkdir -p build
"$py" -B packaging/make_third_party_licenses.py build/THIRD_PARTY_LICENSES.txt
"$py" -B -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/rubato_waveform.spec

codesign --force --deep --options runtime --entitlements packaging/entitlements.plist --sign "$identity" "$app"
codesign --verify --deep --strict "$app"

ditto -c -k --keepParent "$app" "$zip"
echo "Built $zip"

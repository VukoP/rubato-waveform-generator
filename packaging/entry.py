# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Frozen-app entry point: the package uses relative imports, so PyInstaller needs a top-level script."""

from rubato_waveform.app import main

if __name__ == "__main__":
    main()

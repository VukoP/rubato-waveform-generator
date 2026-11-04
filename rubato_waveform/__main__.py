# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Entry point for ``python -m rubato_waveform``."""

import sys

from .cli import main

sys.exit(main())

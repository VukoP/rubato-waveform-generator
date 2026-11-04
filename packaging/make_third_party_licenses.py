# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Write THIRD_PARTY_LICENSES.txt for what the frozen app actually ships.

The package list is the runtime dependency closure of pillow, numpy and pywebview on this OS
(read from installed metadata), so build and test tools never end up in the file. The text itself
comes from pip-licenses. Usage: make_third_party_licenses.py OUTPUT.txt
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import PackageNotFoundError, requires
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOTS = ("pillow", "numpy", "pywebview")


def runtime_closure() -> list[str]:
    seen: dict[str, None] = {}
    todo = list(ROOTS)
    while todo:
        name = canonicalize_name(todo.pop())
        if name in seen:
            continue
        seen[name] = None
        try:
            lines = requires(name) or []
        except PackageNotFoundError:
            continue                      # a dependency of another OS
        for line in lines:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return sorted(seen)


def runtime_notice() -> str:
    """The interpreter and libraries PyInstaller bundles besides the Python packages."""
    text = ("\n\nBundled runtime\n----------------\n"
            f"CPython {sys.version.split()[0]}, the Python interpreter and standard library, is bundled under\n"
            "the Python Software Foundation License Version 2 (https://docs.python.org/3/license.html).\n"
            "OpenSSL 3 (libssl and libcrypto, used by Python's ssl module) is bundled under the Apache\n"
            "License 2.0 (https://www.openssl.org/source/license.html).\n")
    if sys.platform == "win32":
        text += ("If present, the Microsoft Visual C++ runtime (vcruntime140*.dll) shipped with Python is\n"
                 "Microsoft's redistributable and is covered by its own license terms.\n")
    return text


def main() -> int:
    out = Path(sys.argv[1])
    cmd = [sys.executable, "-B", "-m", "piplicenses", "--with-license-file", "--no-license-path",
           "--with-notice-file", "--format=plain-vertical", "--packages", *runtime_closure()]
    text = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    header = ("Third-party software shipped in this application.\n"
              "The Rubato Waveform Generator itself is under the Apache License 2.0 (see LICENSE).\n"
              "The bundled fonts carry their own SIL Open Font License texts under "
              "rubato_waveform/ui/fonts/.\nFFmpeg is not bundled.\n\n")
    out.write_text(header + text + runtime_notice(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

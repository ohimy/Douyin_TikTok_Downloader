"""Fetch pinned, redistributable XHS signing assets for development or packaging.

Local API fork addition; modification notice dated 2026-10-20.
Upstream MIT text is retained in local_api/xhs_assets/THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

COMMIT = "d96edb9584330622931064781db00c3f96fa2abe"
SOURCE = (
    "https://raw.githubusercontent.com/Nirvana919/XHS_Crawler/"
    f"{COMMIT}/Spider_XHS-xhs_new_api/static/xhs_main_260411.js"
)
SHA256 = "723dc6ef64836b0998aa4ba85796e2ffd99bfb20f222d69adcbcf66ea589292d"
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "local_api" / "xhs_assets"


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    target = ASSETS / "xhs_main_260411.js"
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != SHA256:
        with urlopen(SOURCE, timeout=60) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != SHA256:
            raise SystemExit("Downloaded Xiaohongshu signing asset failed integrity verification.")
        target.write_bytes(content)
    package = ASSETS / "package.json"
    package.write_text('{"private":true,"dependencies":{"crypto-js":"4.2.0"}}\n', encoding="utf-8")
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("Node.js 20+ and npm are required to prepare the Xiaohongshu runtime.")
    subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=ASSETS, check=True)
    print(f"Prepared Xiaohongshu runtime assets in {ASSETS}")


if __name__ == "__main__":
    main()

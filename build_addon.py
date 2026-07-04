"""Package the addon folder into hardSubReader-<version>.nvda-addon.
Reads the version from addon/manifest.ini. Run: python build_addon.py
"""
import re
import zipfile
from pathlib import Path

root = Path(__file__).parent / "addon"
manifest = (root / "manifest.ini").read_text(encoding="utf-8")
version = re.search(r"^version\s*=\s*(\S+)", manifest, re.M).group(1)
out = Path(__file__).parent / f"hardSubReader-{version}.nvda-addon"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            z.write(p, p.relative_to(root))

print(f"Built {out.name} ({out.stat().st_size} bytes)")

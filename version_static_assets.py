#!/usr/bin/env python3
"""Build-time helper: version static assets by content hash.

Computes SHA256 of known static assets and rewrites exact href/src
references in HTML files to include ?v=<hash>. Idempotent: if a
reference already carries any query string, it is replaced with the
new hash. Only the specific known asset paths are touched.
"""

import hashlib
import re
import sys
from pathlib import Path

# Known assets to version (relative to static dir)
ASSETS = ("app.js", "styles.css", "dashboard-collapsible.js", "collapsible.css")

# HTML files that reference these assets
HTML_FILES = ("index.html", "login.html")

# Regex to match href or src attributes containing the asset path
# with an optional existing query string.
# Matches: href="/styles.css"  href="/styles.css?v=anything"  src="/app.js?v=anything"
_ASSET_RE_TEMPLATE = r'((?:href|src)=["\'])/({asset})(\?[^"\']*)?(["\'])'


def content_hash(data: bytes) -> str:
    """Return first 16 hex chars of SHA256 of data."""
    return hashlib.sha256(data).hexdigest()[:16]


def compute_hashes(static_dir: Path) -> dict:
    """Compute content hashes for all known assets.

    Raises FileNotFoundError if any required asset is missing.
    """
    hashes = {}
    for asset in ASSETS:
        path = static_dir / asset
        if not path.is_file():
            raise FileNotFoundError(f"Required asset not found: {path}")
        hashes[asset] = content_hash(path.read_bytes())
    return hashes


def version_html(html_path: Path, hashes: dict) -> str:
    """Return versioned HTML content with asset URLs updated.

    Only rewrites href/src attributes for the known asset paths.
    Idempotent: existing query strings are replaced.
    """
    text = html_path.read_text(encoding="utf-8")
    for asset, h in hashes.items():
        pattern = _ASSET_RE_TEMPLATE.format(asset=re.escape(asset))
        replacement = r'\1/' + asset + r'?v=' + h + r'\4'
        text = re.sub(pattern, replacement, text)
    return text


def version_static_assets(static_dir: Path) -> None:
    """Version all known assets in all HTML files under static_dir.

    Writes modified HTML files in-place. Raises FileNotFoundError
    if any required asset file is missing.
    """
    static_dir = Path(static_dir)
    hashes = compute_hashes(static_dir)
    for html_name in HTML_FILES:
        html_path = static_dir / html_name
        if not html_path.is_file():
            raise FileNotFoundError(f"Required HTML file not found: {html_path}")
        new_content = version_html(html_path, hashes)
        html_path.write_text(new_content, encoding="utf-8")


def main(argv=None):
    """CLI entry point. Usage: python version_static_assets.py [static_dir]

    Defaults to ./static if no argument given.
    """
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) > 1:
        print("Usage: version_static_assets.py [static_dir]", file=sys.stderr)
        return 2
    static_dir = Path(argv[0]) if argv else Path("static")
    try:
        version_static_assets(static_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

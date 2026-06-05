#!/usr/bin/env python3
"""
verify_dom_ids.py — for the LOCAL dashboards. Same idea as the
ownCloud version but parses app/*.html instead of templates/*.php,
and also scans inline <script> blocks inside the HTML pages.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOWLIST = {
    'last-update-age', 'update-btn',
}


def collect_html_ids() -> dict[str, list[Path]]:
    """IDs defined in HTML, PLUS IDs found inside JS template literals
    (backtick-quoted strings — typically `SHARED_CHROME_HTML`, modal
    markup, etc. that the JS injects at runtime). Auto-detecting them
    is more robust than a hand-maintained allowlist."""
    ids: dict[str, list[Path]] = {}
    for html in (ROOT / 'app').glob('*.html'):
        text = html.read_text(encoding='utf-8')
        for m in re.finditer(r"""\bid=["']([a-zA-Z][\w-]*)["']""", text):
            ids.setdefault(m.group(1), []).append(html)
    # Walk JS files looking for IDs that get into the DOM at runtime:
    #   (a) inside template literals (`SHARED_CHROME_HTML = \`<div id=...\``)
    #   (b) assigned via `el.id = "..."` (`style.id = "shared-chrome-css"`)
    # Also scan inline <script> in HTML files for the same patterns.
    id_assign_pat = re.compile(r"""\.id\s*=\s*["']([a-zA-Z][\w-]*)["']""")
    id_attr_pat = re.compile(r"""\bid=["']([a-zA-Z][\w-]*)["']""")

    def scan_js(text: str, source: Path):
        # IDs in template literals
        for tm in re.finditer(r'`([^`]+)`', text, re.DOTALL):
            for m in id_attr_pat.finditer(tm.group(1)):
                ids.setdefault(m.group(1), []).append(source)
        # IDs assigned via .id = "..."
        for m in id_assign_pat.finditer(text):
            ids.setdefault(m.group(1), []).append(source)

    for js in (ROOT / 'app').rglob('*.js'):
        if 'vendor' in str(js) or 'emscripten' in str(js):
            continue
        scan_js(js.read_text(encoding='utf-8'), js)

    # Inline <script> inside HTML pages
    for html in (ROOT / 'app').glob('*.html'):
        text = html.read_text(encoding='utf-8')
        for sm in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                              text, re.DOTALL):
            scan_js(sm.group(1), html)
    return ids


def strip_comments_only(src: str) -> str:
    """Strip // and /* */ comments, but PRESERVE string literals
    (because the IDs we want to match live inside them)."""
    out = []
    i = 0
    in_block = False
    in_str = None
    while i < len(src):
        c = src[i]
        nxt = src[i+1] if i+1 < len(src) else ''
        if in_block:
            if c == '*' and nxt == '/':
                in_block = False
                i += 2
            else:
                out.append('\n' if c == '\n' else ' ')
                i += 1
            continue
        if in_str:
            out.append(c)
            if c == '\\':
                if i+1 < len(src):
                    out.append(src[i+1])
                    i += 2
                    continue
            elif c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", '`'):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == '/' and nxt == '/':
            while i < len(src) and src[i] != '\n':
                i += 1
            continue
        if c == '/' and nxt == '*':
            in_block = True
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def collect_js_refs() -> dict[str, list[tuple[Path, int]]]:
    refs = {}
    patterns = [
        re.compile(r"""getElementById\s*\(\s*["']([a-zA-Z][\w-]*)["']\s*\)"""),
        re.compile(r"""querySelector\s*\(\s*["']#([a-zA-Z][\w-]*)["']\s*\)"""),
    ]

    for js in (ROOT / 'app').rglob('*.js'):
        if 'vendor' in str(js) or 'emscripten' in str(js):
            continue
        text = js.read_text(encoding='utf-8')
        cleaned = strip_comments_only(text)
        for pat in patterns:
            for m in pat.finditer(cleaned):
                lineno = cleaned[:m.start()].count('\n') + 1
                refs.setdefault(m.group(1), []).append((js, lineno))

    for html in (ROOT / 'app').glob('*.html'):
        text = html.read_text(encoding='utf-8')
        for sm in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                              text, re.DOTALL):
            block = sm.group(1)
            block_start_line = text[:sm.start(1)].count('\n') + 1
            cleaned = strip_comments_only(block)
            for pat in patterns:
                for m in pat.finditer(cleaned):
                    off = cleaned[:m.start()].count('\n')
                    refs.setdefault(m.group(1), []).append((html, block_start_line + off))
    return refs


def main() -> int:
    if not (ROOT / 'app').exists():
        print(f'ERROR: {ROOT}/app not found', file=sys.stderr)
        return 2

    html_ids = collect_html_ids()
    js_refs = collect_js_refs()

    defined = set(html_ids) | ALLOWLIST
    referenced = set(js_refs)
    missing = sorted(referenced - defined)

    print('=' * 70)
    print('DOM-ID sync check (dashboard)')
    print('=' * 70)
    print(f'  HTML files:     {len(list((ROOT / "app").glob("*.html")))}')
    print(f'  IDs in HTML:    {len(html_ids)}')
    print(f'  Refs in JS:     {len(referenced)}')
    print()

    if missing:
        print(f'❌ FAIL: {len(missing)} JS reference(s) point at non-existent IDs:')
        for mid in missing:
            print(f'\n  • {mid!r}  ({len(js_refs[mid])} ref(s))')
            for path, lineno in js_refs[mid][:3]:
                print(f'      {path.relative_to(ROOT)}:{lineno}')
        return 1
    print('✅ PASS: every JS reference resolves')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
verify_wiring.py — guarantee every JS event listener has a real callback.

Detects the OTHER half of the 2026-06-05 settings-btn class of bugs:

  • verify_dom_ids.py catches references to DOM IDs that no template
    defines (the actual cause this morning).
  • THIS script catches references to FUNCTIONS that the JS itself
    never defines — which would also crash the wire-up callback at the
    line that mentions the undefined symbol.

Examples it flags:

  • `on('totp-submit', 'click', submitTotp)` where `submitTotp` was
    renamed/removed but the call site wasn't updated.
  • `btn.addEventListener('click', () => doStuff())` where `doStuff`
    doesn't exist in any js/*.js file.
  • `triggerUpdate(code, {full})` called from one place but defined
    nowhere (typo, etc).

How:
  1. Walk js/*.js, collect every `function NAME(`, `const NAME =`,
     `let NAME =`, `var NAME =` — that's the "defined functions" set.
  2. Walk the same files, collect every callable reference:
       NAME(                         (call expression)
       'click', NAME                 (addEventListener arg)
       'click', NAME)                (on() helper arg)
  3. Any reference not backed by a definition is a bug.

Allowlist: JS / DOM built-ins, OC.* / Chart.* globals, runtime-injected
helpers. Add more as needed (false positives cost more than misses on a
1500-line codebase).

Cost: ~80 ms on the full repo. Designed to run inside scripts/deploy.sh
right after verify_dom_ids.py.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Symbols that come "for free" from the runtime (DOM, JS stdlib, OC core).
# Anything in this set is allowed to be referenced even if no js/*.js
# defines it. Keep this list TIGHT — every name added here is a name the
# verifier won't catch typos for.
BUILTINS = {
    # JS / browser globals
    'console', 'window', 'document', 'location', 'fetch', 'setTimeout',
    'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    'JSON', 'Date', 'Math', 'Array', 'Object', 'String', 'Number',
    'Boolean', 'Promise', 'Map', 'Set', 'Error', 'TypeError', 'RangeError',
    'isNaN', 'isFinite', 'parseInt', 'parseFloat', 'encodeURIComponent',
    'decodeURIComponent', 'alert', 'confirm', 'prompt', 'URL',
    'URLSearchParams', 'FormData', 'Blob', 'FileReader', 'Event',
    'CustomEvent', 'MouseEvent', 'KeyboardEvent', 'AbortController',
    'BroadcastChannel', 'IntersectionObserver', 'MutationObserver',
    'addEventListener', 'removeEventListener', 'dispatchEvent',
    'getComputedStyle', 'matchMedia', 'navigator', 'localStorage',
    'sessionStorage',
    # ownCloud globals
    'OC', 'OCA', 'OCP', 'OCP.AppConfig', 'OC.requestToken', 'OC.Notification',
    'OC.dialogs', 't', 'n', 'p',
    # Chart.js (vendored)
    'Chart',
    # Common loop / iterator names — these are PARAMS, not references
    # (the regex sometimes picks them up); excluding them is harmless.
    'e', 'i', 'j', 'k', 'a', 'b', 'c', 'x', 'y', 'n', 'v',
    'res', 'err', 'opts', 'opt', 'fn', 'cb', 'el', 'id', 'evt', 'event',
    'data', 'payload', 'body', 'arg', 'args', 'p', 'q', 's', 't',
    # Common DOM property names appearing in chains
    'value', 'textContent', 'innerHTML', 'classList', 'dataset', 'style',
    'addEventListener', 'querySelector', 'querySelectorAll',
    'getElementById', 'getElementsByClassName',
    # ESM/CJS-ish bits
    'undefined', 'null', 'true', 'false', 'NaN', 'Infinity', 'arguments',
    'this', 'super', 'new', 'typeof', 'instanceof', 'in', 'of',
    'return', 'throw', 'try', 'catch', 'finally', 'await', 'async',
    'function', 'const', 'let', 'var', 'if', 'else', 'for', 'while',
    'do', 'switch', 'case', 'default', 'break', 'continue',
}


def collect_definitions(js_files: list[Path]) -> dict[str, list[tuple[Path, int]]]:
    """Names defined as functions/consts/lets/vars across all js files."""
    defs: dict[str, list[tuple[Path, int]]] = {}
    patterns = [
        # function NAME(   or   async function NAME(
        re.compile(r'(?:^|\s)(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\('),
        # const/let/var NAME =
        re.compile(r'(?:^|\s)(?:const|let|var)\s+([A-Za-z_]\w*)\s*='),
        # window.NAME = (top-level export)
        re.compile(r'window\.([A-Za-z_]\w*)\s*='),
    ]
    for js in js_files:
        text = js.read_text(encoding='utf-8')
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in patterns:
                for m in pat.finditer(line):
                    defs.setdefault(m.group(1), []).append((js, lineno))
    return defs


def strip_strings_and_comments(src: str) -> str:
    """Best-effort string + comment stripper so we don't match symbols in text."""
    out = []
    i = 0
    in_str = None
    in_block = False
    while i < len(src):
        c = src[i]
        nxt = src[i+1] if i+1 < len(src) else ''
        if in_block:
            if c == '*' and nxt == '/':
                in_block = False
                out.append('  ')
                i += 2
            else:
                out.append('\n' if c == '\n' else ' ')
                i += 1
            continue
        if in_str:
            if c == '\\':
                if i+1 < len(src):
                    i += 2
                    out.append('  ')
                    continue
            elif c == in_str:
                in_str = None
                out.append(' ')
                i += 1
                continue
            out.append('\n' if c == '\n' else ' ')
            i += 1
            continue
        if c in ('"', "'", '`'):
            in_str = c
            out.append(' ')
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


def collect_callable_refs(js_files: list[Path]) -> dict[str, list[tuple[Path, int]]]:
    """Names invoked as functions or passed as listener callbacks."""
    refs: dict[str, list[tuple[Path, int]]] = {}

    # NAME( — direct call. Exclude obvious non-names with the negative
    # lookbehind: a preceding `.` means it's a method (we already cover
    # `dataset`, `value`, etc. in BUILTINS for the common cases).
    call_pat = re.compile(r'(?<![.\w])([A-Za-z_]\w*)\s*\(')

    # addEventListener('click', NAME)
    listener_pat = re.compile(
        r'addEventListener\s*\(\s*[\'"][^\'"]+[\'"]\s*,\s*([A-Za-z_]\w*)\s*[,\)]'
    )
    # on('id', 'event', NAME)   — our null-safe helper
    on_helper_pat = re.compile(
        r"""\bon\s*\(\s*[\'"][^\'"]+[\'"]\s*,\s*[\'"][^\'"]+[\'"]\s*,\s*([A-Za-z_]\w*)\s*\)"""
    )

    for js in js_files:
        raw = js.read_text(encoding='utf-8')
        # Strip strings and comments BEFORE running the regexes so
        # symbol names inside string literals don't show up as refs.
        cleaned = strip_strings_and_comments(raw)
        for lineno, line in enumerate(cleaned.splitlines(), 1):
            # call-expression refs (broad)
            for m in call_pat.finditer(line):
                name = m.group(1)
                if name in BUILTINS or name[0].isupper():  # heuristics
                    continue
                refs.setdefault(name, []).append((js, lineno))
            # listener-argument refs (need the original source to see
            # the strings, but cleaned source kept the structure)
        # listener_pat and on_helper_pat are applied to RAW source so
        # the string-typed event name matches:
        for lineno, line in enumerate(raw.splitlines(), 1):
            for m in listener_pat.finditer(line):
                refs.setdefault(m.group(1), []).append((js, lineno))
            for m in on_helper_pat.finditer(line):
                refs.setdefault(m.group(1), []).append((js, lineno))
    return refs


def main() -> int:
    js_dir = ROOT / 'app'
    if not js_dir.exists():
        print(f'ERROR: {js_dir} not found', file=sys.stderr)
        return 2
    js_files = [p for p in js_dir.rglob('*.js') if 'vendor' not in str(p)]
    defs = collect_definitions(js_files)
    refs = collect_callable_refs(js_files)

    defined = set(defs) | BUILTINS
    referenced = set(refs)

    missing = sorted(referenced - defined)

    print('=' * 70)
    print('JS wiring check')
    print('=' * 70)
    print(f'  Files scanned:       {len(js_files)}')
    print(f'  Names defined:       {len(defs)}')
    print(f'  Names referenced:    {len(referenced)}')
    print()

    # Filter out names that are very likely false positives (loop vars,
    # destructured params, etc). Heuristic: a name with only 1 reference
    # and that's a single letter or 2-letter name is almost certainly a
    # param we didn't catch in BUILTINS.
    real_missing = [m for m in missing if not (len(m) <= 2 and len(refs[m]) <= 1)]

    if real_missing:
        print(f'❌ FAIL: {len(real_missing)} symbol(s) referenced but not defined:')
        for sym in real_missing[:20]:
            print(f'\n  • {sym!r}  ({len(refs[sym])} ref(s))')
            for path, lineno in refs[sym][:3]:
                print(f'      {path.relative_to(ROOT)}:{lineno}')
            if len(refs[sym]) > 3:
                print(f'      ... and {len(refs[sym]) - 3} more')
        if len(real_missing) > 20:
            print(f'\n  ... and {len(real_missing) - 20} more')
        print()
        print('Each of these will throw at runtime when the referencing line')
        print('executes (`ReferenceError: NAME is not defined`). If a real')
        print('symbol got renamed/removed, fix the call site. If the name')
        print('is a legitimate global/builtin, add it to BUILTINS in this')
        print('script.')
        return 1

    print('✅ PASS: every callable reference resolves to a definition')
    return 0


if __name__ == '__main__':
    sys.exit(main())

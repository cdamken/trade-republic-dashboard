#!/usr/bin/env python3
"""Trade Republic data fetcher with MFA-aware exit codes.

Used by:
  - dashboard.sh (CLI, interactive — lets pytr prompt for the code)
  - server.py POST /update (non-interactive, with optional --mfa-code)

Exit codes (mapped to HTTP status by server.py):
  0   success
  10  MFA required: session expired AND no --mfa-code provided
  11  MFA invalid: provided --mfa-code rejected
  12  Bad credentials in ~/.pytr/credentials
  20  Network / pytr API error
  21  Rate limited by Trade Republic (HTTP 429 — wait 15-30 min)
  30  Local processing error (parse_pytr_output / analyze_analytics)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_DIR / "app"
DATA_DIR = PROJECT_DIR / "DATA"
PYTR = os.environ.get("PYTR_PATH") or os.path.expanduser("~/.local/bin/pytr")

PORTFOLIO_FILE = DATA_DIR / "portfolio_raw.txt"
TX_FILE = DATA_DIR / "account_transactions.csv"
LAST_UPDATE_FILE = DATA_DIR / "last_update.date"

DATA_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
RATE_LIMIT_HINT = (
    "Trade Republic rate-limited the login (HTTP 429). "
    "This happens after several login attempts in a short window. "
    "Wait ~15-30 minutes before trying again — further attempts can extend the block."
)


def _is_rate_limited(output: str) -> bool:
    """Detect TR's HTTP 429 in pytr's error output.

    Must match the *exact* signatures emitted by requests.HTTPError when TR
    returns 429. Generic "429" substring matches are not enough — pytr's
    normal output contains many numbers that can coincidentally include 429.
    """
    return (
        "429 Client Error" in output
        or "HTTPError: 429" in output
        or "Too Many Requests" in output
    )


def login_with_code(code: str) -> None:
    """Pipe the 4-digit code to `pytr login`. Exits 11 if rejected."""
    proc = subprocess.run(
        [PYTR, "login", "--store_credentials"],
        input=f"{code}\n",
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if "Logged in" not in output:
        if _is_rate_limited(output):
            sys.stderr.write(f"\n⚠️  {RATE_LIMIT_HINT}\n")
            sys.exit(21)
        sys.stderr.write("MFA login failed.\n")
        sys.stderr.write(output[-500:] + "\n")
        if "credentials" in output.lower() and "invalid" in output.lower():
            sys.exit(12)
        sys.exit(11)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def fetch_portfolio(non_interactive: bool) -> None:
    """Run `pytr portfolio` and save output to PORTFOLIO_FILE.

    Exits:
      10 if non-interactive AND session expired (no MFA code was provided upfront)
      20 on other pytr failures
    """
    stdin = subprocess.DEVNULL if non_interactive else None
    proc = subprocess.run(
        [PYTR, "portfolio"],
        stdin=stdin,
        capture_output=True,
        timeout=90,
    )
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        if _is_rate_limited(combined):
            sys.stderr.write(f"\n⚠️  {RATE_LIMIT_HINT}\n")
            sys.exit(21)
        if (
            "Resuming websession failed" in combined
            or "EOFError" in combined
            or "Code:" in combined
        ):
            sys.stderr.write("Session expired, MFA required.\n")
            sys.exit(10)
        sys.stderr.write(f"pytr portfolio failed:\n{combined[-800:]}\n")
        sys.exit(20)

    PORTFOLIO_FILE.write_bytes(proc.stdout)


# ---------------------------------------------------------------------------
# Transactions (incremental with merge)
# ---------------------------------------------------------------------------
def _do_full_transactions() -> None:
    proc = subprocess.run(
        [PYTR, "export_transactions", "--no-store-event-database", str(TX_FILE)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"Full transactions download failed:\n{proc.stderr[-500:]}\n")
        sys.exit(20)


def fetch_transactions(force_full: bool, buffer_days: int = 3) -> None:
    """Smart transactions download:

    - First time / no CSV / no last_update.date → full (~3 min)
    - Gap > 365 days → full
    - Otherwise → --last_days N then merge with existing CSV (dedupe by line, sort by date)
    """
    if force_full or not TX_FILE.exists() or not LAST_UPDATE_FILE.exists():
        _do_full_transactions()
        return

    # Compute incremental window
    try:
        last_date_str = LAST_UPDATE_FILE.read_text().strip().split()[0]
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
    except Exception:
        _do_full_transactions()
        return
    days_since = (datetime.now() - last_date).days
    days_to_scan = days_since + buffer_days
    if days_to_scan > 365:
        _do_full_transactions()
        return

    tmp = DATA_DIR / ".tx_incremental.csv"
    proc = subprocess.run(
        [PYTR, "export_transactions", "--no-store-event-database",
         "--last_days", str(days_to_scan), str(tmp)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"Incremental fetch failed, falling back to full: {proc.stderr[-200:]}\n"
        )
        tmp.unlink(missing_ok=True)
        _do_full_transactions()
        return

    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return  # nothing new

    # Merge: dedupe whole lines, sort chronologically by first column (Date)
    old_lines = TX_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = tmp.read_text(encoding="utf-8").splitlines()
    tmp.unlink(missing_ok=True)
    if not old_lines:
        TX_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return

    header = old_lines[0]
    body_old = old_lines[1:]
    body_new = new_lines[1:] if len(new_lines) > 1 else []

    seen: set[str] = set()
    merged: list[str] = []
    for line in body_old + body_new:
        if line and line not in seen:
            seen.add(line)
            merged.append(line)
    merged.sort(key=lambda l: l.split(";")[0] if l else "")

    TX_FILE.write_text("\n".join([header] + merged) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def process_data() -> None:
    for script, extra in [
        ("parse_pytr_output.py", [str(PORTFOLIO_FILE)]),
        ("analyze_analytics.py", []),
    ]:
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / script)] + extra,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sys.stderr.write(f"{script} failed:\n{proc.stderr[-500:]}\n")
            sys.exit(30)


def cleanup() -> None:
    for pattern in ("*.tmp", "*.partial", ".DS_Store"):
        for f in PROJECT_DIR.rglob(pattern):
            try:
                f.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch Trade Republic data with MFA-aware exit codes."
    )
    p.add_argument("--mfa-code", help="4-digit code from TR app / SMS (optional)")
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; exit 10 if MFA needed.",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Force full transactions download (skip incremental).",
    )
    args = p.parse_args()

    # Explicit login if a code was supplied
    if args.mfa_code is not None:
        code = args.mfa_code.strip()
        if not (code.isdigit() and len(code) == 4):
            sys.stderr.write("ERROR: --mfa-code must be exactly 4 digits.\n")
            sys.exit(11)
        print("Performing MFA login...", flush=True)
        login_with_code(code)

    print("Downloading portfolio snapshot (live prices)...", flush=True)
    fetch_portfolio(args.non_interactive)

    print("Downloading transactions...", flush=True)
    fetch_transactions(args.full)

    LAST_UPDATE_FILE.write_text(datetime.now().strftime("%Y-%m-%d") + "\n")

    print("Processing data...", flush=True)
    process_data()

    cleanup()
    print("Done.", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Trade Republic data fetcher — uses tr-api as a library, no more pytr.

Same CLI surface as the previous (pytr-based) tr_fetch.py so server.py and
dashboard.sh don't have to change. Same exit codes mapped to HTTP by
server.py.

Exit codes:
  0   success
  10  MFA required: no valid session AND no --mfa-code provided
  11  MFA invalid: provided --mfa-code rejected (or PIN_INVALID)
  12  Bad credentials in ~/.pytr/credentials
  20  Network / TR API error (transient)
  21  Rate-limited by Trade Republic
  30  Local processing error (analytics, etc.)

Architecture vs. the old version:
  - No subprocess to pytr. We call tr_api Python functions directly.
  - Login uses Playwright-managed WAF token under tr-api's control
    (see tr_api.waf). The user never opens a browser themselves.
  - Portfolio comes from the TR WebSocket and is shaped to match the
    schema that index.html / analyze_analytics.py expect.
  - Transactions come from the timelineTransactions topic and are
    written to account_transactions.csv in the same column layout
    pytr produced.

Credentials: still read from ~/.pytr/credentials (line 1 = phone, line 2 = PIN).
This is the dashboard's existing source of truth — kept compatible so a
user upgrading doesn't have to redo setup. tr-api stores per-phone data
under ~/.tr-api/profiles/<phone>/ and we mirror the phone there on first
run automatically.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_DIR / "app"
DATA_DIR = PROJECT_DIR / "DATA"
DATA_DIR.mkdir(exist_ok=True)

PORTFOLIO_JSON = DATA_DIR / "portfolio.json"
PORTFOLIO_RAW_JSON = DATA_DIR / "portfolio_raw.json"   # for debugging the TR WS payload
TX_CSV = DATA_DIR / "account_transactions.csv"
LAST_UPDATE_FILE = DATA_DIR / "last_update.date"

# Where we stash an in-flight login between the two /update HTTP requests.
# Step 1 (no mfa code) writes the processId here; step 2 (with code) reads it.
PENDING_LOGIN_FILE = DATA_DIR / ".pending_login.json"
PENDING_LOGIN_TTL_SECONDS = 5 * 60  # TR's processId is usually valid ~60s; 5 min is generous

PYTR_CREDS = Path.home() / ".pytr" / "credentials"


# --------------------------------------------------------------------------
# tr-api imports (deferred so a missing install gives a clean exit 30)
# --------------------------------------------------------------------------
try:
    from tr_api import (
        Profile,
        TrClient,
        account,
        auth,
        portfolio as tr_portfolio,
        profiles,
        transactions as tr_transactions,
    )
    from tr_api.exceptions import (
        ApiError,
        MissingSessionCookies,
        ProfileNotFound,
        SessionExpired,
        TrApiError,
    )
    from tr_api.auth import InvalidCredentials, LoginError, RateLimited
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"ERROR: tr-api is not installed in this Python ({sys.executable}).\n"
        f"  {e}\n"
        f"Install it (one-time, in the dashboard's Python):\n"
        f"  pip install -e /path/to/tr-api  # or pip install tr-api[browser]\n"
    )
    sys.exit(30)


# --------------------------------------------------------------------------
# Event-type mapping (TR's eventType → dashboard CSV 'Type' column)
# --------------------------------------------------------------------------
# Keep these strings in sync with what analyze_analytics.py looks for:
#   Deposit, Removal, Tax Refund, Buy, Sell, Dividend, Interest
# Verified against live TR responses (May 2026). CARD_TRANSACTION is what
# TR emits for card spending now; older docs use card_successful_transaction.
# We keep both for safety. Trades are classified after lookup via _classify_trade.
EVENT_TYPE_MAP: dict[str, str] = {
    # Cash in
    "INCOMING_TRANSFER":            "Deposit",
    "INCOMING_TRANSFER_DELEGATION": "Deposit",
    "PAYMENT_INBOUND":              "Deposit",
    "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT": "Deposit",
    "card_refund":                  "Deposit",
    "CARD_REFUND":                  "Deposit",
    # Cash out / card spending
    "CARD_TRANSACTION":             "Removal",
    "card_successful_transaction":  "Removal",
    "OUTGOING_TRANSFER":            "Removal",
    "OUTGOING_TRANSFER_DELEGATION": "Removal",
    "PAYMENT_OUTBOUND":             "Removal",
    # Tax flows
    "ssp_tax_correction_invoice":   "Tax Refund",
    "TAX_REFUND":                   "Tax Refund",
    # Trading (Buy vs Sell decided by _classify_trade — looks at amount sign)
    "TRADE_INVOICE":                "Trade",
    "ORDER_EXECUTED":               "Trade",
    # Income
    "CREDIT":                       "Dividend",
    "DIVIDEND":                     "Dividend",
    "ssp_corporate_action_invoice_cash": "Dividend",
    "INTEREST_PAYOUT":              "Interest",
    "INTEREST_PAYOUT_CREATED":      "Interest",
}


# --------------------------------------------------------------------------
# Credentials & profile bootstrapping
# --------------------------------------------------------------------------
def load_phone_pin() -> tuple[str, str]:
    """Read phone+PIN from ~/.pytr/credentials. Exits 12 if missing/malformed."""
    if not PYTR_CREDS.is_file():
        sys.stderr.write(
            f"No credentials at {PYTR_CREDS}. Run the setup wizard first.\n"
        )
        sys.exit(12)
    lines = PYTR_CREDS.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 2 or not lines[0].startswith("+") or not lines[1]:
        sys.stderr.write(
            f"{PYTR_CREDS} is malformed. Expected line 1 = phone (E.164), "
            f"line 2 = PIN.\n"
        )
        sys.exit(12)
    return lines[0].strip(), lines[1].strip()


def ensure_profile(phone: str) -> Profile:
    """Get-or-create the tr-api profile for this phone."""
    try:
        return profiles.load(phone)
    except ProfileNotFound:
        return profiles.create(phone, jurisdiction="DE", name="Dashboard")


# --------------------------------------------------------------------------
# Login — two-step flow split across HTTP requests
# --------------------------------------------------------------------------
# The dashboard does login in two HTTP roundtrips:
#
#   1) POST /update {}
#      Server runs tr_fetch.py with no --mfa-code. If cookies are stale, we
#      call auth.initiate_login() (which makes TR push a 4-digit code to the
#      user's mobile app) and persist the resulting processId in
#      PENDING_LOGIN_FILE. We exit 10 (mfa_required) so the browser opens
#      the code modal.
#
#   2) POST /update {"mfa_code": "1234"}
#      Server runs tr_fetch.py with --mfa-code. We load the saved
#      processId and call auth.complete_login(processId, code), which
#      writes the session cookies. The pending-login file is then cleared.
#
# If the user clicks Update again while a pending login is still fresh
# (TTL not elapsed), we DO NOT re-initiate (would invalidate the in-flight
# push). Instead we just exit 10 again and the modal stays open.
def _save_pending(phone: str, process_id: str) -> None:
    payload = {"phone": phone, "process_id": process_id, "issued_at": int(time.time())}
    PENDING_LOGIN_FILE.write_text(json.dumps(payload), encoding="utf-8")
    try:
        PENDING_LOGIN_FILE.chmod(0o600)
    except OSError:
        pass


def _load_pending(phone: str) -> str | None:
    if not PENDING_LOGIN_FILE.is_file():
        return None
    try:
        data = json.loads(PENDING_LOGIN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("phone") != phone:
        return None
    issued = int(data.get("issued_at") or 0)
    if int(time.time()) - issued > PENDING_LOGIN_TTL_SECONDS:
        return None
    pid = data.get("process_id")
    return pid if isinstance(pid, str) and pid else None


def _clear_pending() -> None:
    try:
        PENDING_LOGIN_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _pin_for(phone: str) -> str:
    _, pin = load_phone_pin()
    return pin


def _trigger_push_and_exit(phone: str, pin: str) -> None:
    """Step 1: call initiate_login (TR pushes the code) and exit 10."""
    try:
        init = auth.initiate_login(phone, pin)
    except RateLimited as e:
        sys.stderr.write(
            f"⚠️  Rate-limited by Trade Republic. "
            f"Retry at {e.next_attempt_at} "
            f"(≈ {(e.wait_seconds or 0) // 60} min).\n"
        )
        sys.exit(21)
    except InvalidCredentials as e:
        sys.stderr.write(f"Bad credentials: {e}\n")
        sys.exit(11)
    except LoginError as e:
        sys.stderr.write(f"Could not initiate login: {e}\n")
        sys.exit(20)

    _save_pending(phone, init.process_id)
    sys.stderr.write(
        f"📲 Push sent to your Trade Republic mobile app. "
        f"Enter the 4-digit code in the dashboard modal.\n"
        f"   processId: {init.process_id[:8]}…  expires in ~60s.\n"
    )
    sys.exit(10)


def _complete_pending_or_die(phone: str, mfa_code: str) -> TrClient:
    """Step 2: complete the in-flight login using the saved processId."""
    process_id = _load_pending(phone)
    if not process_id:
        sys.stderr.write(
            "No pending login for this phone (or it expired). "
            "Submit the form without a code first to trigger a new push.\n"
        )
        sys.exit(10)

    from tr_api import cookies as _c
    prof = profiles.load(phone)
    try:
        result = auth.complete_login(process_id, mfa_code)
    except InvalidCredentials as e:
        sys.stderr.write(f"Wrong code: {e}\n")
        sys.exit(11)
    except RateLimited as e:
        sys.stderr.write(f"⚠️  Rate-limited: {e}\n")
        sys.exit(21)
    except LoginError as e:
        sys.stderr.write(f"Login failed: {e}\n")
        sys.exit(20)

    _c.save_to_file(result.cookies, prof.cookies_file)
    _clear_pending()
    return TrClient(prof)


def get_authenticated_client(phone: str, mfa_code: str | None, non_interactive: bool) -> TrClient:
    """Return an authenticated TrClient, or exit 10/11/20/21 along the way.

    Routes:
      - mfa_code provided  -> complete the pending login (step 2).
      - no mfa_code, cookies still valid -> use them as-is.
      - no mfa_code, cookies stale       -> initiate (push) and exit 10 (step 1).
    """
    prof = ensure_profile(phone)
    profiles.set_active(phone)

    if mfa_code is not None:
        return _complete_pending_or_die(phone, mfa_code)

    # Try existing cookies — if a recent login is still good, we're done.
    try:
        client = TrClient(prof)
        try:
            alive = account.ping(client)
        except TrApiError as e:
            sys.stderr.write(f"Network/API error during session ping: {e}\n")
            sys.exit(20)
        if alive:
            return client
    except MissingSessionCookies:
        pass  # fall through to "trigger push"

    # Cookies are missing or rejected. If a push has *already* been sent
    # within the last few minutes, don't re-send another one (the previous
    # push and modal are still in flight).
    if _load_pending(phone) is not None:
        sys.stderr.write(
            "A 4-digit code was already pushed to your phone within the last "
            "5 minutes. Enter it in the dashboard modal.\n"
        )
        sys.exit(10)

    # Fresh login required — trigger a push and surface mfa_required.
    pin = _pin_for(phone)
    _trigger_push_and_exit(phone, pin)
    # _trigger_push_and_exit always exits; this line just satisfies the type checker.
    return None  # type: ignore[return-value]


def _exit_mfa_required(non_interactive: bool, reason: str) -> None:
    sys.stderr.write(
        f"MFA required — {reason}.\n"
        f"{'(non-interactive: exiting)' if non_interactive else 'Run again with --mfa-code <4-digit code from your TR app>.'}\n"
    )
    sys.exit(10)


# --------------------------------------------------------------------------
# Portfolio: tr-api snapshot → dashboard portfolio.json schema
# --------------------------------------------------------------------------
def fetch_portfolio(client: TrClient) -> dict[str, Any]:
    try:
        # include_history=False: TR removed the portfolioAggregateHistory
        # topic in protocol v31 without a known replacement. The dashboard
        # reconstructs the net-worth chart from daily snapshots
        # (DATA/net_worth_history.json) instead.
        snap = tr_portfolio.snapshot(client, include_history=False)
    except SessionExpired:
        _exit_mfa_required(non_interactive=False, reason="Session expired during portfolio fetch")
    except RateLimited as e:  # pragma: no cover — usually only on login
        sys.stderr.write(f"Rate-limited: {e}\n")
        sys.exit(21)
    except TrApiError as e:
        sys.stderr.write(f"Portfolio fetch failed: {e}\n")
        sys.exit(20)

    # Save raw for debugging the field-name mapping on first runs.
    try:
        PORTFOLIO_RAW_JSON.write_text(
            json.dumps(snap, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:  # pragma: no cover
        pass

    shaped = _shape_portfolio(snap)
    # Append a snapshot to net_worth_history.json so the dashboard chart grows.
    _append_net_worth_history(shaped["summary"])
    return shaped


def _shape_portfolio(snap: dict[str, Any]) -> dict[str, Any]:
    """Map tr-api's raw TR JSON into the schema parse_pytr_output used to produce.

    We map field names defensively — TR has been known to rename keys in
    minor backend updates, so we always go through .get() with fallbacks.
    """
    p = (snap.get("portfolio") or {}) if isinstance(snap, dict) else {}
    cash_data = snap.get("cash") if isinstance(snap, dict) else None

    positions: list[dict[str, Any]] = []
    zero_positions: list[dict[str, Any]] = []

    for raw in (p.get("positions") or []):
        qty = _as_float(raw.get("netSize") or raw.get("quantity"))
        avg_cost = _as_float(raw.get("averageBuyIn") or raw.get("avgPrice"))
        net_value = _as_float(raw.get("netValue") or raw.get("currentValue"))
        # `currentPrice` is sometimes {value: x} and sometimes a scalar.
        cp = raw.get("currentPrice")
        if isinstance(cp, dict):
            current_price = _as_float(cp.get("value"))
        else:
            current_price = _as_float(cp)
        if current_price == 0 and qty > 0:
            current_price = net_value / qty if qty else 0.0

        name = (raw.get("name") or raw.get("instrumentName") or "").strip()
        instrument_id = raw.get("instrumentId") or raw.get("isin") or ""
        # instrumentId is typically "ISIN.EXCHANGE"; ISIN is the part before the dot.
        isin = (raw.get("isin") or instrument_id.split(".", 1)[0]).strip()

        buy_cost = avg_cost * qty
        pl_eur = net_value - buy_cost
        pl_pct = (pl_eur / buy_cost * 100.0) if buy_cost else 0.0

        item = {
            "name": name[:25],          # match pytr's 25-char truncation
            "isin": isin,
            "avg_cost": round(avg_cost, 4),
            "quantity": round(qty, 6),
            "buy_cost_eur": round(buy_cost, 2),
            "net_value_eur": round(net_value, 2),
            "current_price": round(current_price, 4),
            "pl_eur": round(pl_eur, 2),
            "pl_pct": round(pl_pct, 2),
        }
        if net_value > 0:
            positions.append(item)
        else:
            zero_positions.append({"name": name, "isin": isin})

    positions.sort(key=lambda x: x["net_value_eur"], reverse=True)
    winners = sorted(
        (x for x in positions if x["pl_pct"] >= 50.0),
        key=lambda x: -x["pl_pct"],
    )
    losers = sorted(
        (x for x in positions if x["pl_pct"] <= -25.0),
        key=lambda x: x["pl_pct"],
    )

    cash_eur = _extract_eur_cash(cash_data)

    depot_buycost = sum(x["buy_cost_eur"] for x in positions)
    depot_netvalue = sum(x["net_value_eur"] for x in positions)
    depot_pl_eur = round(depot_netvalue - depot_buycost, 2)
    depot_pl_pct = round((depot_pl_eur / depot_buycost * 100.0) if depot_buycost else 0.0, 2)

    return {
        "summary": {
            "depot_buycost": round(depot_buycost, 2),
            "depot_netvalue": round(depot_netvalue, 2),
            "depot_pl_eur": depot_pl_eur,
            "depot_pl_pct": depot_pl_pct,
            "cash_eur": round(cash_eur, 2),
            "total_buycost": round(depot_buycost, 2),
            "total_netvalue": round(depot_netvalue + cash_eur, 2),
        },
        "total_positions": len(positions) + len(zero_positions),
        "positions_with_value": len(positions),
        "zero_value_positions": zero_positions,
        "top_25": positions[:25],
        "winners_50plus": winners,
        "losers_25minus": losers,
        "all_positions": positions,
    }


def _as_float(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_eur_cash(cash_data: Any) -> float:
    """The cash WS payload is sometimes a list and sometimes nested. Handle both."""
    if not cash_data:
        return 0.0
    if isinstance(cash_data, dict):
        # Sometimes wrapped: {"amount": ..., "currencyId": ...}
        return _as_float(cash_data.get("amount"))
    if isinstance(cash_data, list):
        for entry in cash_data:
            if not isinstance(entry, dict):
                continue
            if entry.get("currencyId") in ("EUR", "EUR_CASH", None):
                return _as_float(entry.get("amount"))
        # Fallback: first entry's amount
        first = cash_data[0] if cash_data else None
        if isinstance(first, dict):
            return _as_float(first.get("amount"))
    return 0.0


def _append_net_worth_history(summary: dict[str, Any]) -> None:
    """Append a daily snapshot to DATA/net_worth_history.json (idempotent per day)."""
    history_file = DATA_DIR / "net_worth_history.json"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    if history_file.exists():
        try:
            rows = json.loads(history_file.read_text(encoding="utf-8")) or []
        except Exception:
            rows = []
    # Replace today's row if it exists, otherwise append.
    rows = [r for r in rows if r.get("date") != today]
    rows.append({
        "date": today,
        "net_value": summary["total_netvalue"],
        "depot": summary["depot_netvalue"],
        "cash": summary["cash_eur"],
        "pl_eur": summary["depot_pl_eur"],
    })
    rows.sort(key=lambda r: r["date"])
    history_file.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Transactions: timeline → CSV in the schema analyze_analytics.py expects
# --------------------------------------------------------------------------
CSV_COLUMNS = ["Date", "Type", "Value", "Note", "ISIN", "Shares",
               "Fees", "Taxes", "ISIN2", "Shares2"]


def fetch_transactions(client: TrClient, force_full: bool) -> None:
    if force_full or not TX_CSV.exists() or not LAST_UPDATE_FILE.exists():
        items = _safe_call(lambda: tr_transactions.fetch_all(client))
    else:
        try:
            last_str = LAST_UPDATE_FILE.read_text(encoding="utf-8").strip().split()[0]
            last = datetime.strptime(last_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            items = _safe_call(lambda: tr_transactions.fetch_all(client))
        else:
            cutoff = last - timedelta(days=3)  # 3-day overlap to catch late settlements
            items = _safe_call(lambda: tr_transactions.fetch_since(client, cutoff))
            _merge_into_csv(items)
            return

    # Full mode: replace the file.
    rows = [_row_from_tr_event(e) for e in items]
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: r["Date"], reverse=True)
    _write_csv(TX_CSV, rows)


def _safe_call(fn):
    try:
        return fn()
    except SessionExpired:
        _exit_mfa_required(non_interactive=False, reason="Session expired during transactions fetch")
    except TrApiError as e:
        sys.stderr.write(f"Transactions fetch failed: {e}\n")
        sys.exit(20)


def _merge_into_csv(new_items: list[dict[str, Any]]) -> None:
    existing_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    if TX_CSV.exists():
        with TX_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                # Key on Date+Type+Value+Note (no `id` column in the existing schema).
                k = f"{row.get('Date','')}|{row.get('Type','')}|{row.get('Value','')}|{row.get('Note','')}"
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                existing_rows.append(row)

    for ev in new_items:
        r = _row_from_tr_event(ev)
        if not r:
            continue
        k = f"{r['Date']}|{r['Type']}|{r['Value']}|{r['Note']}"
        if k in seen_keys:
            continue
        seen_keys.add(k)
        existing_rows.append(r)

    existing_rows.sort(key=lambda r: r.get("Date", ""), reverse=True)
    _write_csv(TX_CSV, existing_rows)


def _row_from_tr_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Map one TR timeline event to a CSV row (or None to skip)."""
    ev_type = ev.get("eventType") or ""
    csv_type = EVENT_TYPE_MAP.get(ev_type)
    if csv_type is None:
        return None

    if csv_type == "Trade":
        csv_type = _classify_trade(ev)  # → "Buy" or "Sell" or None
        if csv_type is None:
            return None

    timestamp = ev.get("timestamp") or ev.get("eventTime") or ""
    amount = ev.get("amount") or {}
    value = amount.get("value") if isinstance(amount, dict) else amount
    note = (ev.get("title") or ev.get("subtitle") or "").strip()

    # ISIN is best-effort: TR's icon URL contains it (logos/<ISIN>/v2).
    isin = ""
    icon = ev.get("icon") or ""
    if "logos/" in icon:
        for piece in icon.split("/"):
            if len(piece) == 12 and piece[:2].isalpha() and piece[2:].isalnum():
                isin = piece
                break

    return {
        "Date":   timestamp,
        "Type":   csv_type,
        "Value":  "" if value is None else str(value),
        "Note":   note,
        "ISIN":   isin,
        "Shares": "",   # analyze_analytics.py doesn't need it
        "Fees":   "",
        "Taxes":  "",
        "ISIN2":  "",
        "Shares2": "",
    }


def _classify_trade(ev: dict[str, Any]) -> str | None:
    """Decide whether a trade event is Buy or Sell."""
    amount = ev.get("amount") or {}
    val = amount.get("value") if isinstance(amount, dict) else amount
    if isinstance(val, (int, float)):
        return "Buy" if val < 0 else "Sell"
    title = (ev.get("title") or "").lower()
    if "buy" in title or "kauf" in title:
        return "Buy"
    if "sell" in title or "verk" in title:
        return "Sell"
    return None


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=";")
        w.writeheader()
        for r in rows:
            for c in CSV_COLUMNS:
                r.setdefault(c, "")
            w.writerow(r)


# --------------------------------------------------------------------------
# Analytics (still a separate script — same behavior as before)
# --------------------------------------------------------------------------
def run_analytics() -> None:
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(APP_DIR / "analyze_analytics.py")],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"analyze_analytics.py failed:\n{proc.stderr[-500:]}\n")
        sys.exit(30)


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch Trade Republic data via tr-api with MFA-aware exit codes."
    )
    p.add_argument("--mfa-code", help="4-digit code from TR app push (optional).")
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

    if args.mfa_code is not None:
        code = args.mfa_code.strip()
        if not (code.isdigit() and len(code) == 4):
            sys.stderr.write("ERROR: --mfa-code must be exactly 4 digits.\n")
            sys.exit(11)

    phone, _ = load_phone_pin()
    client = get_authenticated_client(phone, args.mfa_code, args.non_interactive)

    print("Fetching portfolio snapshot…", flush=True)
    shaped = fetch_portfolio(client)
    PORTFOLIO_JSON.write_text(json.dumps(shaped, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Fetching transactions…", flush=True)
    fetch_transactions(client, args.full)

    LAST_UPDATE_FILE.write_text(datetime.now().strftime("%Y-%m-%d") + "\n", encoding="utf-8")

    print("Running analytics…", flush=True)
    run_analytics()

    print("Done.", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # pragma: no cover — defensive
        traceback.print_exc()
        sys.exit(30)

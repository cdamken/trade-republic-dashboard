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
import asyncio
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
        activity_log as tr_activity_log,
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
# Targets analyze_analytics.py vocabulary: Deposit / Removal / Tax Refund /
# Buy / Sell / Dividend / Interest.
#
# IMPORTANT: TR renamed almost every eventType during 2026. Old names
# (INCOMING_TRANSFER, TRADE_INVOICE, DIVIDEND, ...) no longer appear on
# live responses — observed via a fresh fetch_all on 2026-05-28:
#
#   timelineTransactions (6000 items)         timelineActivityLog (858)
#   ----------------------------------         -----------------------
#   TRADING_SAVINGSPLAN_EXECUTED   4181        ORDER_CANCELED         202
#   SSP_CORPORATE_ACTION_CASH       694        SSP_CORPORATE_ACTION_*
#   TRADING_TRADE_EXECUTED          465        TRADING_ORDER_*
#   CARD_TRANSACTION                321        ORDER_EXPIRED, ...
#   BANK_TRANSACTION_*              ~178       (mostly order lifecycle
#   SPARE_CHANGE_AGGREGATE           35         and corp-action info —
#   SAVEBACK_AGGREGATE                8         no actual cash events,
#   SSP_TAX_CORRECTION               34         so unmapped here)
#   INTEREST_PAYOUT                   8
#   CARD_REFUND                       2
#
# Old names kept under #legacy for accounts whose historical CSV rows
# came from pytr (so re-running over an existing pytr-era CSV doesn't
# silently downgrade them).
EVENT_TYPE_MAP: dict[str, str] = {
    # --- Cash in -----------------------------------------------------------
    "BANK_TRANSACTION_INCOMING":           "Deposit",
    "CARD_REFUND":                         "Deposit",
    # legacy
    "INCOMING_TRANSFER":                   "Deposit",
    "INCOMING_TRANSFER_DELEGATION":        "Deposit",
    "PAYMENT_INBOUND":                     "Deposit",
    "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT":   "Deposit",
    "card_refund":                         "Deposit",

    # --- Cash out: card spending (consumption) ---------------------------
    # CARD_TRANSACTION is lifestyle consumption: bakery, supermarket, fuel.
    # The money leaves your wealth entirely. Tracked separately from
    # Withdrawal (below) so analytics can show "money you put into TR
    # for investing" net of "money you took back to your main bank"
    # without conflating it with day-to-day spending.
    "CARD_TRANSACTION":                    "Removal",
    "CRYPTO_TRANSFER_NETWORK_FEE":         "Removal",
    # legacy
    "card_successful_transaction":         "Removal",

    # --- Cash out: withdrawals back to your main bank -------------------
    # BANK_TRANSACTION_OUTGOING* is SEPA / direct debit / scheduled
    # transfers from TR to a non-TR account. Typically the user's own
    # bank, but generically: money leaves TR's balance and goes to
    # another bank. It's still the user's money — just in a different
    # account — so analytics treats it as a withdrawal of capital from
    # the TR investment account, not consumption.
    "BANK_TRANSACTION_OUTGOING":           "Withdrawal",
    "BANK_TRANSACTION_OUTGOING_DIRECT_DEBIT": "Withdrawal",
    "BANK_TRANSACTION_OUTGOING_SCHEDULED": "Withdrawal",
    # legacy
    "OUTGOING_TRANSFER":                   "Withdrawal",
    "OUTGOING_TRANSFER_DELEGATION":        "Withdrawal",
    "PAYMENT_OUTBOUND":                    "Withdrawal",

    # --- Tax flows --------------------------------------------------------
    "SSP_TAX_CORRECTION":                  "Tax Refund",
    # legacy
    "ssp_tax_correction_invoice":          "Tax Refund",
    "TAX_REFUND":                          "Tax Refund",

    # --- Trading ----------------------------------------------------------
    # SAVINGSPLAN / SPARE_CHANGE / SAVEBACK are ALWAYS buys (the user can't
    # sell via these flows), so map straight to Buy — skipping the
    # amount-sign classification (which would also work but is unnecessary).
    "TRADING_SAVINGSPLAN_EXECUTED":        "Buy",
    "SPARE_CHANGE_AGGREGATE":              "Buy",
    "SAVEBACK_AGGREGATE":                  "Buy",
    # Manual trades / private market — classify_trade decides Buy vs Sell
    # by amount sign.
    "TRADING_TRADE_EXECUTED":              "Trade",
    "PRIVATE_MARKET_FUND_TRADE_EXECUTED":  "Trade",
    # legacy
    "TRADE_INVOICE":                       "Trade",
    "ORDER_EXECUTED":                      "Trade",

    # --- Income (dividends, interest) -------------------------------------
    "SSP_CORPORATE_ACTION_CASH":           "Dividend",
    "INTEREST_PAYOUT":                     "Interest",
    "INTEREST_PAYOUT_CREATED":             "Interest",
    # legacy
    "CREDIT":                              "Dividend",
    "DIVIDEND":                            "Dividend",
    "ssp_corporate_action_invoice_cash":   "Dividend",

    # --- Intentionally NOT mapped -----------------------------------------
    # CARD_VERIFICATION (€1 pre-auth, refunded) — noise.
    # TRADING_SAVINGSPLAN_EXECUTION_FAILED — informative, not a cash event.
    # SSP_CORPORATE_ACTION_CASH_NON_DIVIDEND — spinoff cash with no
    #   matching position credit; revisit if a user wants it surfaced.
    # All timelineActivityLog event types (ORDER_CANCELED,
    #   SSP_CORPORATE_ACTION_ACTIVITY/INFORMATIVE/INSTRUCTION/UPCOMING,
    #   CSX_CHAT_ACTIVITY, ORDER_EXPIRED, DOCUMENTS_ACCEPTED, ...) — these
    #   are order lifecycle / informational, NOT financial events. We keep
    #   fetching the topic in case a future TR change moves cash events
    #   onto it, but for now drop everything we see.
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
        # snapshot_full() does compactPortfolio + instrument-per-ISIN +
        # ticker-per-ISIN on a single WS connection, so positions come back
        # with names AND live prices. The simpler snapshot() returns only
        # {instrumentId, netSize, averageBuyIn} and is no use for a dashboard
        # that wants to render named rows with current value.
        snap = tr_portfolio.snapshot_full(client)
        # Also pull the by-category view so we can tag each position with
        # the TR bucket it belongs to (stocksAndETFs / cryptos / bonds /
        # privateMarkets / others). This is what TR's mobile "Wealth"
        # screen uses to break down the depot into separate tiles.
        # Cheap second call: same auth, ~1s.
        from tr_api import accounts as tr_accounts
        pairs = tr_accounts.account_pairs(client)
        default_pair = pairs.default_pair()
        cat_snap: dict[str, Any] = {}
        if default_pair is not None:
            cat_snap = tr_portfolio.compact_portfolio_by_type(
                client, sec_acc_no=default_pair.securities_account_number
            )
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

    # Build {isin -> TR category} from the by-type snapshot.
    isin_to_category: dict[str, str] = {}
    for cat in (cat_snap.get("categories") or []):
        cat_type = str(cat.get("categoryType") or "others")
        for pos in (cat.get("positions") or []):
            isin = str(pos.get("isin") or "")
            if isin:
                isin_to_category[isin] = cat_type

    shaped = _shape_portfolio(snap, isin_to_category)
    # Append a snapshot to net_worth_history.json so the dashboard chart grows.
    _append_net_worth_history(shaped["summary"])
    return shaped


def _shape_portfolio(
    snap: dict[str, Any],
    isin_to_category: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map tr-api's raw TR JSON into the schema the dashboard frontend expects.

    snap should come from tr_portfolio.snapshot_full(), so each position
    already has {instrumentId, isin, name, netSize, averageBuyIn,
    currentPrice}. TR returns numeric fields as decimal STRINGS — we cast
    via _as_float to keep precision-y math sane.

    A position lands in `zero_value_positions` when we genuinely couldn't
    compute a current value (price=0 AND no value provided). A position
    where the price wasn't resolved (ticker fan-out failed) but quantity
    and avg cost are known still gets surfaced — we fall back to
    avg_cost * qty so the user at least sees an estimate of buy cost.
    """
    p = (snap.get("portfolio") or {}) if isinstance(snap, dict) else {}
    cash_data = snap.get("cash") if isinstance(snap, dict) else None

    positions: list[dict[str, Any]] = []
    zero_positions: list[dict[str, Any]] = []

    for raw in (p.get("positions") or []):
        qty = _as_float(raw.get("netSize") or raw.get("virtualSize") or raw.get("quantity"))
        avg_cost = _as_float(raw.get("averageBuyIn") or raw.get("avgPrice"))

        # `currentPrice` from snapshot_full is the scalar price string we
        # picked from ticker.last/bid/ask. Older TR responses sometimes
        # wrapped it as {"value": ...}; tolerate both.
        cp = raw.get("currentPrice")
        if isinstance(cp, dict):
            current_price = _as_float(cp.get("value") or cp.get("price"))
        else:
            current_price = _as_float(cp)

        # TR ALSO sometimes ships a netValue directly — prefer that if
        # present, otherwise compute price * qty.
        net_value = _as_float(raw.get("netValue") or raw.get("currentValue"))
        if net_value <= 0 and current_price > 0 and qty > 0:
            net_value = current_price * qty
        if current_price <= 0 and qty > 0 and net_value > 0:
            current_price = net_value / qty

        instrument_id = str(raw.get("instrumentId") or raw.get("isin") or "")
        # instrumentId might be "ISIN.EXCHANGE"; ISIN is the part before the dot.
        isin = str(raw.get("isin") or instrument_id.split(".", 1)[0])
        name = (raw.get("name") or raw.get("instrumentName") or "").strip()
        if not name:
            name = isin  # fallback so the row is at least identifiable

        buy_cost = avg_cost * qty
        pl_eur = net_value - buy_cost if net_value > 0 else 0.0
        pl_pct = (pl_eur / buy_cost * 100.0) if (buy_cost and net_value > 0) else 0.0

        item = {
            "name": name[:25],          # match pytr's 25-char truncation
            "isin": isin,
            "category": (isin_to_category or {}).get(isin, "others"),
            "avg_cost": round(avg_cost, 4),
            "quantity": round(qty, 6),
            "buy_cost_eur": round(buy_cost, 2),
            "net_value_eur": round(net_value, 2),
            "current_price": round(current_price, 4),
            "pl_eur": round(pl_eur, 2),
            "pl_pct": round(pl_pct, 2),
        }
        # A "real" position is one we have at least qty+avg_cost for AND
        # whose computed value is non-zero. Otherwise it's a placeholder
        # (likely TR didn't return a ticker for it, e.g. a delisted bond).
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

    # Per-bucket totals — mirrors what TR's mobile "Wealth" screen shows
    # as separate tiles (Brokerage / Bonds / Private Equity / etc.). The
    # category labels come straight from compactPortfolioByType.
    by_category: dict[str, dict[str, Any]] = {}
    for pos in positions:
        cat = pos.get("category") or "others"
        bucket = by_category.setdefault(cat, {
            "count": 0,
            "buy_cost_eur": 0.0,
            "net_value_eur": 0.0,
        })
        bucket["count"] += 1
        bucket["buy_cost_eur"] += pos["buy_cost_eur"]
        bucket["net_value_eur"] += pos["net_value_eur"]
    # Round + add P/L per bucket
    for cat, b in by_category.items():
        b["buy_cost_eur"] = round(b["buy_cost_eur"], 2)
        b["net_value_eur"] = round(b["net_value_eur"], 2)
        b["pl_eur"] = round(b["net_value_eur"] - b["buy_cost_eur"], 2)
        b["pl_pct"] = round(
            (b["pl_eur"] / b["buy_cost_eur"] * 100.0) if b["buy_cost_eur"] else 0.0, 2
        )

    return {
        "summary": {
            "depot_buycost": round(depot_buycost, 2),
            "depot_netvalue": round(depot_netvalue, 2),
            "depot_pl_eur": depot_pl_eur,
            "depot_pl_pct": depot_pl_pct,
            "cash_eur": round(cash_eur, 2),
            "total_buycost": round(depot_buycost, 2),
            "total_netvalue": round(depot_netvalue + cash_eur, 2),
            "by_category": by_category,
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


async def _paginate_topic_on_ws(ws, topic: str, *, cutoff=None, max_pages: int = 200):
    """Paginate a single TR timeline topic on an EXISTING WS connection.

    If `cutoff` is provided (a datetime), stops as soon as an item with
    timestamp < cutoff is seen — same semantics as fetch_since.
    """
    items = []
    cursor = None
    for _ in range(max_pages):
        payload = {"type": topic}
        if cursor is not None:
            payload["after"] = cursor
        page = await ws.fetch_one(payload)
        page_items = page.get("items") or []
        if cutoff is not None:
            for it in page_items:
                ts_raw = it.get("timestamp") or it.get("eventTime") or ""
                if isinstance(ts_raw, str) and ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        return items
                except (ValueError, TypeError):
                    pass
                items.append(it)
        else:
            items.extend(page_items)
        cursor = (page.get("cursors") or {}).get("after")
        if cursor is None:
            return items
    return items


def fetch_transactions(client: TrClient, force_full: bool) -> None:
    """Fetch BOTH timelineTransactions (cash flow) and timelineActivityLog
    (trades, dividends, corporate actions) on a SINGLE WebSocket connection
    and merge into one CSV.

    Why one WS, not two:
      pytr subscribes to both topics back-to-back on the same WS (see
      pytr/timeline.py). When we used tr_api.transactions.fetch_all +
      tr_api.activity_log.fetch_all (two SEPARATE asyncio.run + WS), the
      second topic consistently returned 0 items for at least Carlos's
      account — TR appears to do something stateful per-session that
      makes a fresh second WS see an empty activityLog. Doing both on
      one WS (the pytr pattern) is what restores the trade history.

    Same shape as pytr's timeline export: a single CSV with every
    Buy/Sell/Dividend/Removal/Deposit/Interest/Tax-Refund row.
    """
    from tr_api.protocol import TrWebSocket
    from tr_api import transactions as _tx_mod, activity_log as _act_mod

    if not (force_full or not TX_CSV.exists() or not LAST_UPDATE_FILE.exists()):
        # Incremental path — uses a cutoff stop predicate.
        try:
            last_str = LAST_UPDATE_FILE.read_text(encoding="utf-8").strip().split()[0]
            cutoff = datetime.strptime(last_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=3)
        except Exception:
            cutoff = None
    else:
        cutoff = None

    async def _go():
        async with TrWebSocket(client.session.cookies) as ws:
            tx_items = await _paginate_topic_on_ws(ws, _tx_mod.TOPIC, cutoff=cutoff)
            print(
                f"  timelineTransactions: {len(tx_items)} items"
                + (f" (since {cutoff:%Y-%m-%d})" if cutoff else ""),
                flush=True,
            )
            act_items = await _paginate_topic_on_ws(ws, _act_mod.TOPIC, cutoff=cutoff)
            print(
                f"  timelineActivityLog:  {len(act_items)} items"
                + (f" (since {cutoff:%Y-%m-%d})" if cutoff else ""),
                flush=True,
            )
            return tx_items, act_items

    try:
        tx_items, act_items = asyncio.run(_go())
    except SessionExpired:
        _exit_mfa_required(non_interactive=False, reason="Session expired during transactions fetch")
    except TrApiError as e:
        sys.stderr.write(f"Transactions fetch failed: {e}\n")
        sys.exit(20)

    items = tx_items + act_items

    if cutoff is not None:
        # Incremental — merge with what's already in CSV (dedupes by Date|Type|Value|Note).
        _merge_into_csv(items)
        return

    # Full mode — replace the file.
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

    # Full timestamp (date + time) so the UI can show a staleness chip
    # ("hace N min", color-coded). The incremental-fetch logic above only
    # cares about the date part — `.split()[0]` extracts it on read.
    LAST_UPDATE_FILE.write_text(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8"
    )

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

#!/usr/bin/env python3
"""Generate DATA/analytics.json from DATA/account_transactions.csv + portfolio.json.

Sections:
  - cash_flow: solo movimientos externos (entra/sale del wealth de TR)
       deposits + tax_refunds  →  ENTRA
       removals (card spending) →  SALE
       net_capital_in = entradas − salidas
       lifetime_pl    = current_value − net_capital_in
  - dividends: monthly chart + recent + total received
  - allocation: rough split Stocks / ETFs / Crypto / Cash
  - history: net worth daily snapshots (appended each run)
"""
import csv
import json
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date as _date_t, timedelta
from pathlib import Path


# ============================================================================
# Portfolio analytics helpers (XIRR, forward dividends, yield on cost,
# top contributors, benchmark fetch). Added 2026-06-01 — see CLAUDE.md
# "What analytics matter" research note.
# ============================================================================

def _xirr_npv(rate, days, amounts):
    return sum(a / (1 + rate) ** (d / 365.0) for a, d in zip(amounts, days))


def xirr(cash_flows, tol=1e-7):
    """Annualized money-weighted return (XIRR), as a percent.

    cash_flows = list of (datetime.date, amount) tuples.
      amount<0 = outflow (capital committed);
      amount>0 = inflow  (capital pulled back, or terminal value).

    Solver: Newton-Raphson with several initial guesses; falls back to
    bisection between (-0.95, 10.0) if Newton fails. Returns None when
    flows have only one sign or no root exists in the search range.
    """
    if not cash_flows or len(cash_flows) < 2:
        return None
    cash_flows = sorted(cash_flows, key=lambda x: x[0])
    t0 = cash_flows[0][0]
    days = [(d - t0).days for d, _ in cash_flows]
    amounts = [float(a) for _, a in cash_flows]
    if all(a >= 0 for a in amounts) or all(a <= 0 for a in amounts):
        return None

    # Try Newton from several initial guesses.
    for guess in (0.10, 0.0, -0.10, 0.30, -0.30, 0.50):
        rate = guess
        for _ in range(80):
            try:
                npv  = _xirr_npv(rate, days, amounts)
                dnpv = sum(-d / 365.0 * a / (1 + rate) ** (d / 365.0 + 1)
                           for a, d in zip(amounts, days))
            except (OverflowError, ZeroDivisionError):
                break
            if abs(dnpv) < 1e-12:
                break
            new_rate = rate - npv / dnpv
            if new_rate <= -0.999:
                new_rate = -0.99
            if abs(new_rate - rate) < tol:
                return round(new_rate * 100, 2)
            rate = new_rate

    # Newton failed → bisection in [-0.95, 10.0].
    lo, hi = -0.95, 10.0
    try:
        f_lo = _xirr_npv(lo, days, amounts)
        f_hi = _xirr_npv(hi, days, amounts)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(120):
        mid = (lo + hi) / 2
        try:
            f_mid = _xirr_npv(mid, days, amounts)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < tol or abs(hi - lo) < tol:
            return round(mid * 100, 2)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(((lo + hi) / 2) * 100, 2)


def forward_dividend_income(div_payments, today):
    """Naive next-12-month projection: scale up the last-N-days window to 365.

    Returns (projected_amount, basis_days, payments_used) or (None, 0, 0) when
    fewer than 90 days of dividend history is available (too noisy to project).
    """
    if not div_payments or not today:
        return None, 0, 0
    cutoff = (today - timedelta(days=365)).isoformat()
    relevant = [p for p in div_payments
                if p.get('date', '') >= cutoff and p.get('type') == 'Dividend']
    if not relevant:
        return None, 0, 0
    dates = sorted(p['date'] for p in relevant)
    try:
        d_first = datetime.fromisoformat(dates[0]).date()
        d_last  = datetime.fromisoformat(dates[-1]).date()
    except ValueError:
        return None, 0, 0
    span_days = max(1, (d_last - d_first).days)
    if span_days < 90:
        return None, span_days, len(relevant)
    total = sum(float(p.get('amount', 0) or 0) for p in relevant)
    scaled = total * (365.0 / span_days) if span_days < 365 else total
    return round(scaled, 2), span_days, len(relevant)


def fetch_benchmark_monthly(symbol, start_date, end_date, cache_path=None):
    """Yahoo Finance v8 chart endpoint — monthly closes between two dates.

    Returns list of {"date": "YYYY-MM-DD", "close": float}. Cached on disk
    so repeated runs don't hammer Yahoo. Returns [] on any failure (caller
    must tolerate that — benchmark overlay is a nice-to-have, not critical).
    """
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            # Reuse cache if fetched < 24h ago AND covers the requested range.
            fetched_at = datetime.fromisoformat(cached.get('fetched_at', '1970-01-01T00:00:00'))
            if (datetime.now() - fetched_at).total_seconds() < 86400:
                if cached.get('symbol') == symbol and cached.get('history'):
                    return cached['history']
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    try:
        p1 = int(datetime.combine(start_date, datetime.min.time()).timestamp())
        p2 = int(datetime.combine(end_date,   datetime.min.time()).timestamp())
        # interval=1d so the benchmark replay line moves day-by-day,
        # not in monthly stair-steps.
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?period1={p1}&period2={p2}&interval=1d&events=history")
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
        result = payload.get('chart', {}).get('result', [{}])[0]
        ts = result.get('timestamp', []) or []
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', []) or []
        history = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            d = datetime.utcfromtimestamp(t).date().isoformat()
            history.append({"date": d, "close": round(float(c), 4)})
        if cache_path and history:
            cache_path.write_text(json.dumps({
                "symbol":     symbol,
                "fetched_at": datetime.now().isoformat(timespec='seconds'),
                "history":    history,
            }, indent=2))
        return history
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, KeyError, TimeoutError) as e:
        # Graceful degradation — log to stderr and return empty.
        import sys as _sys
        _sys.stderr.write(f"[benchmark] {symbol} fetch failed: {e}\n")
        return []


def replay_against_benchmark(monthly_flows, bench_history):
    """Simulate buying the benchmark with the user's external cash flows.

    monthly_flows: list of {month: 'YYYY-MM', deposits, tax_refunds, removals,
                            withdrawals, net_flow}
    bench_history: list of {date, close} from fetch_benchmark_monthly

    Returns list of {date: 'YYYY-MM-DD', value: float} — what the user's
    capital would be worth today if every external inflow had bought the
    benchmark at that month's close and every outflow had sold proportionally.
    """
    if not monthly_flows or not bench_history:
        return []
    bench_by_month = {h['date'][:7]: h['close'] for h in bench_history}
    units = 0.0
    out = []
    for f in monthly_flows:
        m = f['month']
        close = bench_by_month.get(m)
        if close is None or close <= 0:
            # No price for this month — skip flow, carry units forward.
            if out:
                out.append({"date": m + "-01", "value": out[-1]['value']})
            continue
        net = float(f.get('net_flow', 0) or 0)
        if net != 0:
            units += net / close
        value = round(units * close, 2)
        out.append({"date": m + "-01", "value": value})
    # Final marker at the last benchmark close (today-ish).
    if bench_history and units > 0:
        last = bench_history[-1]
        out.append({"date": last['date'], "value": round(units * last['close'], 2)})
    return out


def process_analytics():
    base_dir = Path(__file__).resolve().parent.parent / "DATA"
    csv_path = base_dir / 'account_transactions.csv'
    portfolio_json = base_dir / 'portfolio.json'
    history_file = base_dir / 'net_worth_history.json'

    analytics_data = {
        "cash_flow": {
            # Money IN (you to TR, or TR paying you)
            "deposits":      {"count": 0, "total": 0.0},  # external bank → TR
            "tax_refunds":   {"count": 0, "total": 0.0},  # Finanzamt → TR
            # Money OUT — split into two distinct concepts:
            #   removals    = card spending (CONSUMPTION, lifestyle)
            #   withdrawals = transfers from TR back to your own bank
            #                 (still your money, just in a different account)
            "removals":      {"count": 0, "total": 0.0},
            "withdrawals":   {"count": 0, "total": 0.0},
            # Internal trading totals (raw sums — no chart needed)
            "buys":          {"count": 0, "total": 0.0},
            "sells":         {"count": 0, "total": 0.0},
            # Annualized money-weighted return (XIRR). Replaces the static
            # "Lifetime P/L %" — IRR is time-aware so it actually compares
            # to "what an index returned over the same period". %, e.g. 7.4
            "xirr": None,
            # Computed
            "net_capital_in":  0.0,  # deposits + tax_refunds − withdrawals
                                     # ("how much of my net worth I dedicated
                                     #  to investing in TR right now")
            "net_traded":      0.0,  # buys − sells
            "current_value":   0.0,  # total_netvalue (portfolio + cash)
            # lifetime_pl = current_value + card_spending − net_capital_in − investment_income
            #             = "gain from price appreciation on the capital I've
            #                committed to TR, ignoring lifestyle spending and
            #                investment-income receipts"
            "lifetime_pl":     0.0,
            "lifetime_pl_pct": 0.0,
            "monthly": [],
        },
        "dividends": {
            "monthly": {},
            "total_received": 0,
            "count": 0,
            "recent": [],
            "all_payments": [],   # full history (date, name, isin, amount, type)
            "by_issuer": {},      # name -> {count, total, isin, last_date}
            # Forward projection: scale last-365d Dividend rows up to a full year.
            "forward_12mo": None,
            "forward_12mo_basis_days": 0,
            "forward_12mo_payments_used": 0,
            # Annual dividend income (forward) / total buy cost. %. e.g. 2.4
            "yield_on_cost": None,
        },
        "allocation": {
            "categories": {"Stocks": 0, "ETFs": 0, "Crypto": 0, "Cash": 0},
            "total": 0,
        },
        "history": [],
        # Top / bottom 5 holdings by P/L €. Filled from portfolio.json.
        "contributors": {"top": [], "bottom": []},
        # MSCI World replay — what your cash flows would be worth today
        # if every external inflow had bought IWDA.AS at that month's close.
        # None when the Yahoo fetch failed (offline / rate-limit / etc.).
        "benchmark": None,
    }

    # =========================================================================
    # 1. Parse CSV — only external cash flows + dividends section
    # =========================================================================
    monthly_flow = defaultdict(lambda: {
        "deposits": 0.0, "removals": 0.0, "withdrawals": 0.0, "tax_refunds": 0.0,
        "buys": 0.0, "sells": 0.0,
    })

    if csv_path.exists():
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter=';')
            for row in reader:
                t_type = (row.get('Type') or '').strip()
                date_str = (row.get('Date') or '')[:10]
                month = date_str[:7] if date_str else None
                try:
                    val = float(row.get('Value') or '0')
                except (TypeError, ValueError):
                    continue
                abs_val = abs(val)
                cf = analytics_data["cash_flow"]

                # External flows only
                if t_type == "Deposit":
                    cf["deposits"]["count"] += 1
                    cf["deposits"]["total"] += abs_val
                    if month: monthly_flow[month]["deposits"] += abs_val
                elif t_type == "Removal":
                    cf["removals"]["count"] += 1
                    cf["removals"]["total"] += abs_val
                    if month: monthly_flow[month]["removals"] += abs_val
                elif t_type == "Withdrawal":
                    cf["withdrawals"]["count"] += 1
                    cf["withdrawals"]["total"] += abs_val
                    if month: monthly_flow[month]["withdrawals"] += abs_val
                elif t_type == "Tax Refund":
                    cf["tax_refunds"]["count"] += 1
                    cf["tax_refunds"]["total"] += abs_val
                    if month: monthly_flow[month]["tax_refunds"] += abs_val
                elif t_type == "Buy":
                    cf["buys"]["count"] += 1
                    cf["buys"]["total"] += abs_val
                    if month: monthly_flow[month]["buys"] += abs_val
                elif t_type == "Sell":
                    cf["sells"]["count"] += 1
                    cf["sells"]["total"] += abs_val
                    if month: monthly_flow[month]["sells"] += abs_val

                # Dividends section (chart + full history + by-issuer breakdown).
                # Independent of cash_flow.
                if t_type == "Dividend" or t_type == "Interest":
                    div = analytics_data["dividends"]
                    div["monthly"][month] = div["monthly"].get(month, 0) + abs_val
                    div["total_received"] += abs_val
                    div["count"] += 1
                    name = row.get('Note', 'Unknown') or 'Unknown'
                    isin = row.get('ISIN', '') or ''
                    div["all_payments"].append({
                        "date": date_str,
                        "name": name,
                        "isin": isin,
                        "amount": abs_val,
                        "type": t_type,    # "Dividend" or "Interest"
                    })
                    # Aggregate by issuer name (with ISIN if available).
                    key = name
                    bi = div["by_issuer"].setdefault(key, {
                        "name": name, "isin": isin,
                        "count": 0, "total": 0.0,
                        "first_date": date_str, "last_date": date_str,
                    })
                    bi["count"] += 1
                    bi["total"] += abs_val
                    # Track date range for the issuer
                    if date_str < bi["first_date"]: bi["first_date"] = date_str
                    if date_str > bi["last_date"]:  bi["last_date"] = date_str
                    # Pick up an ISIN if a later row has one and we didn't.
                    if not bi["isin"] and isin: bi["isin"] = isin

        # Sort full history newest-first, keep `recent` as the first 10 for
        # backwards compatibility with anything that still reads it.
        div = analytics_data["dividends"]
        div["all_payments"].sort(key=lambda x: x['date'], reverse=True)
        div["recent"] = div["all_payments"][:10]
        # Round totals on by_issuer for cleaner JSON.
        for bi in div["by_issuer"].values():
            bi["total"] = round(bi["total"], 2)

    cf = analytics_data["cash_flow"]
    # "Net capital in TR" = capital you've put into TR for investing, net
    # of withdrawals back to your main bank. Card spending is NOT
    # subtracted here — it's lifestyle consumption funded from the TR
    # cash balance, not a reduction of the investing commitment.
    cf["net_capital_in"] = (
        cf["deposits"]["total"]
        + cf["tax_refunds"]["total"]
        - cf["withdrawals"]["total"]
    )
    cf["net_traded"] = cf["buys"]["total"] - cf["sells"]["total"]
    for m in sorted(monthly_flow.keys()):
        d = monthly_flow[m]
        # net_flow: full external picture (deposits + refunds − removals − withdrawals)
        net = d["deposits"] + d["tax_refunds"] - d["removals"] - d["withdrawals"]
        # net_invested: change in committed cost basis = buys − sells.
        # This is what the Analytics chart line tracks now (2026-06-01) and
        # what the benchmark replay uses, so apples-to-apples comparison.
        net_invested = d["buys"] - d["sells"]
        cf["monthly"].append({
            "month": m,
            "deposits": round(d["deposits"], 2),
            "removals": round(d["removals"], 2),
            "withdrawals": round(d["withdrawals"], 2),
            "tax_refunds": round(d["tax_refunds"], 2),
            "buys": round(d["buys"], 2),
            "sells": round(d["sells"], 2),
            "net_flow": round(net, 2),
            "net_invested": round(net_invested, 2),
        })

    # =========================================================================
    # 2. Allocation + history from portfolio.json
    # =========================================================================
    if portfolio_json.exists():
        with open(portfolio_json, 'r') as f:
            p_data = json.load(f)
            summary = p_data.get('summary', {})
            cash_eur = summary.get('cash_eur', 0)
            total_netvalue = summary.get('total_netvalue', 0)

            analytics_data["allocation"]["categories"]["Cash"] = cash_eur
            for pos in p_data.get('all_positions', []):
                val = pos.get('net_value_eur', 0)
                name = pos.get('name', '').lower()
                if 'etf' in name or 'msci' in name or 'nasdaq' in name:
                    analytics_data["allocation"]["categories"]["ETFs"] += val
                elif any(c in name for c in ['bitcoin', 'ethereum', 'crypto', 'solana', 'xrp']):
                    analytics_data["allocation"]["categories"]["Crypto"] += val
                else:
                    analytics_data["allocation"]["categories"]["Stocks"] += val
            analytics_data["allocation"]["total"] = sum(
                analytics_data["allocation"]["categories"].values()
            )

            # Top / bottom 5 contributors — which holdings drove (or dragged
            # down) the P/L. Sorted by absolute P/L €.
            valued = [pos for pos in p_data.get('all_positions', [])
                      if (pos.get('net_value_eur') or 0) > 0]
            valued.sort(key=lambda p: (p.get('pl_eur') or 0), reverse=True)
            def _contrib(pos):
                return {
                    "name":          pos.get('name', '—'),
                    "isin":          pos.get('isin', ''),
                    "category":      pos.get('category', ''),
                    "net_value_eur": round(float(pos.get('net_value_eur') or 0), 2),
                    "pl_eur":        round(float(pos.get('pl_eur') or 0), 2),
                    "pl_pct":        round(float(pos.get('pl_pct') or 0), 2),
                }
            analytics_data["contributors"]["top"]    = [_contrib(p) for p in valued[:5]]
            analytics_data["contributors"]["bottom"] = [_contrib(p) for p in valued[-5:][::-1]]

            # Lifetime P/L = how much your positions appreciated on the
            # capital you committed to TR, ignoring lifestyle spending
            # and dividend/interest income.
            #
            #   lifetime_pl = current_value + card_spending
            #                 − net_capital_in − investment_income
            #
            # Intuition: the value sitting in TR today, plus the money
            # you've already spent on card consumption (because that
            # spending came from the same pool of capital you committed),
            # minus the actual net capital you put in and minus
            # dividends/interest receipts (which are returns ON capital,
            # not capital itself). Whatever's left is pure price
            # appreciation on the portfolio.
            #
            # When net_capital_in is non-positive — typically because the
            # CSV is incomplete (timelineActivityLog gap → missing
            # deposits/dividends history) — set lifetime_pl to None so
            # the UI shows "—" instead of a misleading €0.00.
            cf["current_value"] = total_netvalue
            investment_income = (
                analytics_data["dividends"]["total_received"] or 0.0
            )
            if cf["net_capital_in"] > 0:
                cf["lifetime_pl"] = (
                    total_netvalue
                    + cf["removals"]["total"]      # add back card spending
                    - cf["net_capital_in"]
                    - investment_income
                )
                cf["lifetime_pl_pct"] = cf["lifetime_pl"] / cf["net_capital_in"] * 100
            else:
                cf["lifetime_pl"] = None
                cf["lifetime_pl_pct"] = None
                cf["lifetime_pl_note"] = (
                    "Net capital in TR is non-positive — your transaction "
                    "history is likely incomplete (TR splits trades and "
                    "dividends across timelineTransactions and "
                    "timelineActivityLog; if the latter returned empty, "
                    "deposits and trade history can be missing). "
                    "Lifetime P/L can't be computed reliably."
                )

            # Net invested trajectory (2026-06-01 — was external cash flow):
            #
            # Each day's value = cumulative (buys − sells) up to that day.
            # This is the **cost basis** of all positions you're holding
            # right now, plus everything you've ever bought and sold. Goes
            # up when you buy, down when you sell.
            #
            # Why this and not "external cash flow" (deposits − removals)?
            # Carlos uses the TR card heavily, so removals (card spending)
            # dominate and the cash-flow line gave nonsense numbers (€7k
            # net inflow when he'd actually committed >€100k to positions
            # over time). buys − sells is what an investor calls
            # "committed capital" — same currency as the benchmark replay.
            today = datetime.now().strftime('%Y-%m-%d')
            daily_wealth = {}
            running = 0.0
            if csv_path.exists():
                with open(csv_path, mode='r', encoding='utf-8') as f:
                    reader = csv.DictReader(f, delimiter=';')
                    rows = sorted(reader, key=lambda r: (r.get('Date') or ''))
                    for row in rows:
                        t_type = (row.get('Type') or '').strip()
                        date = (row.get('Date') or '')[:10]
                        if not date:
                            continue
                        try:
                            val = float(row.get('Value') or '0')
                        except (TypeError, ValueError):
                            continue
                        if t_type == 'Buy':
                            running += abs(val)
                        elif t_type == 'Sell':
                            running -= abs(val)
                        # Skip Deposit/Removal/Withdrawal/Tax/Div/Int —
                        # those don't change the cost basis of positions.
                        daily_wealth[date] = round(running, 2)

            # Build the sorted history list of cost-basis daily values.
            # We deliberately DO NOT replace the final row with today's
            # actual market value — that would create a visually jarring
            # vertical jump (cost basis ~€4k → market value ~€68k) that
            # makes the rest of the trajectory unreadable. The current
            # market value lives in its own KPI card at the top of the
            # page; the chart stays focused on capital-injection history.
            history = [{"date": d, "value": daily_wealth[d]} for d in sorted(daily_wealth.keys())]
            # Ensure today is represented even if there were no events on it.
            if history and history[-1]['date'] != today:
                history.append({"date": today, "value": history[-1]['value']})
            elif not history:
                history.append({"date": today, "value": 0.0})
            history = history[-365:]  # keep up to ~1 year
            with open(history_file, 'w') as f:
                json.dump(history, f, indent=2)
            analytics_data["history"] = history

    # =========================================================================
    # 3. XIRR, forward dividends, yield on cost, benchmark replay
    #    (computed last so they can use everything above)
    # =========================================================================
    today_d = datetime.now().date()

    # --- XIRR: deliberately conservative ------------------------------------
    # TR is a HYBRID account (investment + everyday payment account via the
    # TR card). Standard XIRR doesn't apply cleanly:
    #
    #   - Including card spending as "inflow" → NPV often has no real root
    #     (the spending sum is huge compared to deposits, NPV stays positive
    #     for every plausible rate).
    #   - Excluding card spending → XIRR collapses to the misleading
    #     "lost N%/yr" because deposits look much bigger than current value.
    #
    # We compute XIRR ONLY for Deposit-in / Withdrawal-out / terminal-value
    # because that's the academic definition. Carlos's setup will often
    # produce a misleading number here — the UI should show it as
    # "investment-only IRR" alongside the more meaningful "Lifetime P/L"
    # (which accounts for the consumption pool).
    xirr_flows = []
    if csv_path.exists():
        with open(csv_path, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f, delimiter=';'):
                t_type = (row.get('Type') or '').strip()
                date_str = (row.get('Date') or '')[:10]
                if not date_str:
                    continue
                try:
                    val = float(row.get('Value') or '0')
                    d   = datetime.fromisoformat(date_str).date()
                except (TypeError, ValueError):
                    continue
                amt = abs(val)
                if t_type == 'Deposit':
                    xirr_flows.append((d, -amt))
                elif t_type == 'Withdrawal':
                    xirr_flows.append((d, +amt))
    if cf["current_value"] > 0:
        xirr_flows.append((today_d, +cf["current_value"]))
    cf["xirr"] = xirr(xirr_flows)

    # --- Forward 12-month dividend income + yield on cost -------------------
    fwd, basis_days, npayments = forward_dividend_income(
        analytics_data["dividends"]["all_payments"], today_d
    )
    analytics_data["dividends"]["forward_12mo"] = fwd
    analytics_data["dividends"]["forward_12mo_basis_days"] = basis_days
    analytics_data["dividends"]["forward_12mo_payments_used"] = npayments
    if fwd is not None and cf["buys"]["total"] > 0:
        analytics_data["dividends"]["yield_on_cost"] = round(
            fwd / cf["buys"]["total"] * 100, 2
        )

    # --- Benchmark replays (MSCI World, S&P 500, Nasdaq 100, all EUR) ------
    # Three UCITS ETFs listed in Amsterdam (EUR-denominated so no FX noise):
    #   IWDA.AS — iShares Core MSCI World UCITS
    #   VUSA.AS — Vanguard S&P 500 UCITS
    #   EQQQ.AS — Invesco EQQQ Nasdaq-100 UCITS
    # If Yahoo fails for one, the others still render (graceful per-symbol).
    benchmarks_out = []
    if cf["monthly"]:
        first_month = cf["monthly"][0]["month"]
        try:
            start_d = datetime.fromisoformat(first_month + "-01").date()
        except ValueError:
            start_d = today_d - timedelta(days=365)
        cache_dir = base_dir / "benchmark_cache"
        cache_dir.mkdir(exist_ok=True)
        BENCHMARKS = [
            ("IWDA.AS", "MSCI World",  "#fbbf24"),  # amber
            ("VUSA.AS", "S&P 500",     "#34d399"),  # emerald
            ("CNDX.AS", "Nasdaq 100",  "#c084fc"),  # iShares Nasdaq 100 UCITS, EUR
        ]
        # Replay uses net_invested (buys − sells) so the comparison is
        # apples-to-apples with the user's line (cumulative buys − sells).
        replay_input = [{"month": m["month"], "net_flow": m["net_invested"]} for m in cf["monthly"]]
        for sym, label, color in BENCHMARKS:
            cache_path = cache_dir / (sym.replace(".", "_") + ".json")
            bench_history = fetch_benchmark_monthly(sym, start_d, today_d, cache_path=cache_path)
            replayed = replay_against_benchmark(replay_input, bench_history) if bench_history else []
            if replayed:
                benchmarks_out.append({
                    "symbol":  sym,
                    "label":   label,
                    "color":   color,
                    "history": replayed,
                })
    analytics_data["benchmarks"] = benchmarks_out
    # Keep legacy 'benchmark' field for backwards compat — JS reads new field.
    analytics_data["benchmark"] = benchmarks_out[0] if benchmarks_out else None

    with open(base_dir / 'analytics.json', 'w') as f:
        json.dump(analytics_data, f, indent=2)
    print(f"✅ Analytics updated: {base_dir / 'analytics.json'}")
    print(f"   Deposits in:        €{cf['deposits']['total']:>10,.2f}  ({cf['deposits']['count']} tx)")
    print(f"   Tax refunds in:     €{cf['tax_refunds']['total']:>10,.2f}  ({cf['tax_refunds']['count']} tx)")
    print(f"   Removals out:       €{cf['removals']['total']:>10,.2f}  ({cf['removals']['count']} tx)")
    print(f"   Net capital in TR:  €{cf['net_capital_in']:>10,.2f}")
    print(f"   Current value:      €{cf['current_value']:>10,.2f}")
    if cf["lifetime_pl"] is None:
        print(f"   Lifetime P/L:       —  (incomplete data, see analytics.json:cash_flow.lifetime_pl_note)")
    else:
        print(f"   Lifetime P/L:       €{cf['lifetime_pl']:>10,.2f}  ({cf['lifetime_pl_pct']:+.2f}%)")


if __name__ == "__main__":
    process_analytics()

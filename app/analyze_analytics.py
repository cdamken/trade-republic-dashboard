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
from collections import defaultdict
from datetime import datetime
from pathlib import Path


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
            # Per-month breakdown for the "Capital invested over time" line
            # chart. Cumulative (buys − sells) over months = how much
            # capital you've committed to the market.
            # Shape: {"2024-01": {"buys": X, "sells": Y}, ...}
            "buys_sells_by_month": {},
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
        },
        "allocation": {
            "categories": {"Stocks": 0, "ETFs": 0, "Crypto": 0, "Cash": 0},
            "total": 0,
        },
        "history": [],
    }

    # =========================================================================
    # 1. Parse CSV — only external cash flows + dividends section
    # =========================================================================
    monthly_flow = defaultdict(lambda: {
        "deposits": 0.0, "removals": 0.0, "withdrawals": 0.0, "tax_refunds": 0.0,
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
                    if month:
                        m = cf["buys_sells_by_month"].setdefault(month, {"buys": 0.0, "sells": 0.0})
                        m["buys"] += abs_val
                elif t_type == "Sell":
                    cf["sells"]["count"] += 1
                    cf["sells"]["total"] += abs_val
                    if month:
                        m = cf["buys_sells_by_month"].setdefault(month, {"buys": 0.0, "sells": 0.0})
                        m["sells"] += abs_val

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
        # Monthly net flow reflects everything coming in vs. everything
        # going out (including both withdrawals AND card spending) so the
        # bar chart shows the full picture per month.
        net = d["deposits"] + d["tax_refunds"] - d["removals"] - d["withdrawals"]
        cf["monthly"].append({
            "month": m,
            "deposits": round(d["deposits"], 2),
            "removals": round(d["removals"], 2),
            "withdrawals": round(d["withdrawals"], 2),
            "tax_refunds": round(d["tax_refunds"], 2),
            "net_flow": round(net, 2),
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

            # Net worth history — reconstructed from CSV external flows.
            #
            # We can't know historical *market value* per day without
            # historical position prices (TR doesn't expose them via the
            # API we use). What we CAN compute exactly is the "cost-basis
            # wealth" trajectory: the sum of external cash flows up to
            # each day.
            #
            #   wealth_at_cost(day X) = Σ deposits + tax_refunds
            #                          + dividends + interest
            #                          − removals (card spending)
            #
            # Buys and sells don't move total wealth — they just shift
            # between cash and positions — so we don't count them here.
            #
            # This line shows the user's capital-injection trajectory.
            # Today's marker uses the actual market value (total_netvalue),
            # which is higher than the cost-basis line by the accumulated
            # portfolio appreciation.
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
                        if t_type in ('Deposit', 'Tax Refund', 'Dividend', 'Interest'):
                            running += abs(val)
                        elif t_type == 'Removal':
                            running -= abs(val)
                        # Buy/Sell don't change total wealth (just shift
                        # between cash and positions), so skip them.
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

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
            # External flows (TR ↔ outside world)
            "deposits":      {"count": 0, "total": 0.0},
            "removals":      {"count": 0, "total": 0.0},  # Card spending (out)
            "tax_refunds":   {"count": 0, "total": 0.0},
            # Internal trading totals (just the raw sums — no chart needed)
            "buys":          {"count": 0, "total": 0.0},
            "sells":         {"count": 0, "total": 0.0},
            # Computed
            "net_capital_in":  0.0,  # deposits + tax_refunds − removals
            "net_traded":      0.0,  # buys − sells (money still parked in positions)
            "current_value":   0.0,  # total_netvalue (portfolio + cash)
            "lifetime_pl":     0.0,  # current_value − net_capital_in
            "lifetime_pl_pct": 0.0,
            # Monthly trend (sorted YYYY-MM, external flows only)
            "monthly": [],
        },
        "dividends": {
            "monthly": {},
            "total_received": 0,
            "recent": [],
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
        "deposits": 0.0, "removals": 0.0, "tax_refunds": 0.0,
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
                elif t_type == "Tax Refund":
                    cf["tax_refunds"]["count"] += 1
                    cf["tax_refunds"]["total"] += abs_val
                    if month: monthly_flow[month]["tax_refunds"] += abs_val
                elif t_type == "Buy":
                    cf["buys"]["count"] += 1
                    cf["buys"]["total"] += abs_val
                elif t_type == "Sell":
                    cf["sells"]["count"] += 1
                    cf["sells"]["total"] += abs_val

                # Dividends section (chart + recent), independent of cash_flow
                if t_type == "Dividend" or t_type == "Interest":
                    analytics_data["dividends"]["monthly"][month] = \
                        analytics_data["dividends"]["monthly"].get(month, 0) + abs_val
                    analytics_data["dividends"]["total_received"] += abs_val
                    analytics_data["dividends"]["recent"].append({
                        "date": date_str,
                        "name": row.get('Note', 'Unknown'),
                        "amount": abs_val,
                    })

        analytics_data["dividends"]["recent"].sort(key=lambda x: x['date'], reverse=True)
        analytics_data["dividends"]["recent"] = analytics_data["dividends"]["recent"][:10]

    cf = analytics_data["cash_flow"]
    cf["net_capital_in"] = (
        cf["deposits"]["total"]
        + cf["tax_refunds"]["total"]
        - cf["removals"]["total"]
    )
    cf["net_traded"] = cf["buys"]["total"] - cf["sells"]["total"]
    for m in sorted(monthly_flow.keys()):
        d = monthly_flow[m]
        net = d["deposits"] + d["tax_refunds"] - d["removals"]
        cf["monthly"].append({
            "month": m,
            "deposits": round(d["deposits"], 2),
            "removals": round(d["removals"], 2),
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

            # Lifetime P/L (current value vs net capital injected from outside).
            # When net_capital_in is non-positive — typically because the CSV
            # is incomplete (timelineActivityLog gap → no deposits/dividends
            # historically) — leave lifetime_pl as None so the UI can show a
            # "—" / "incomplete data" placeholder instead of a misleading
            # €0.00 (+0.00%) that looks like the user actually broke even.
            cf["current_value"] = total_netvalue
            if cf["net_capital_in"] > 0:
                cf["lifetime_pl"] = total_netvalue - cf["net_capital_in"]
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

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
            # Computed
            "net_capital_in":  0.0,  # deposits + tax_refunds − removals
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

            # Lifetime P/L (current value vs net capital injected from outside)
            cf["current_value"] = total_netvalue
            if cf["net_capital_in"] > 0:
                cf["lifetime_pl"] = total_netvalue - cf["net_capital_in"]
                cf["lifetime_pl_pct"] = cf["lifetime_pl"] / cf["net_capital_in"] * 100

            # Net worth history (daily snapshot)
            today = datetime.now().strftime('%Y-%m-%d')
            history = []
            if history_file.exists():
                try:
                    with open(history_file, 'r') as f:
                        history = json.load(f)
                except Exception:
                    history = []
            if history and history[-1]['date'] == today:
                history[-1]['value'] = total_netvalue
            else:
                history.append({"date": today, "value": total_netvalue})
            history = history[-180:]
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
    print(f"   Lifetime P/L:       €{cf['lifetime_pl']:>10,.2f}  ({cf['lifetime_pl_pct']:+.2f}%)")


if __name__ == "__main__":
    process_analytics()

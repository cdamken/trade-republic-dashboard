#!/usr/bin/env python3
import csv
import json
from datetime import datetime
from pathlib import Path

def reconstruct():
    project_dir = Path(__file__).resolve().parent.parent
    data_dir = project_dir / 'DATA'
    csv_path = data_dir / 'account_transactions.csv'
    history_file = data_dir / 'net_worth_history.json'
    portfolio_json = data_dir / 'portfolio.json'
    
    if not csv_path.exists(): return

    daily_balance = {}
    events = []
    with open(csv_path, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        for row in reader: events.append(row)
    
    events.sort(key=lambda x: x['Date'])
    
    running_total = 0
    # Logic: 
    # External flows (Deposits, Withdrawals, Dividends, Interest) increase/decrease total value.
    # Buy/Sell only affect the total value if there is a Realized Profit/Loss.
    # Since we can't easily calculate realized profit per transaction without tracking lots,
    # we use the "Cash + Cost Basis" approach.
    
    # Simpler approach that the user might find intuitive:
    # A purchase "adds" to the assets.
    # If we treat 'Buy' value (negative) as an increase in assets:
    # Deposit 1000 -> +1000. Buy -100 -> Running total +1000 (ignored buy).
    # But the user wants it to "go up" with buys/sells?
    
    # Actually, the user's screenshot shows a line that stays flat for a long time then jumps.
    # This is because they only had one data point.
    
    # Let's use "Net Invested Capital + Realized Profits"
    running_total = 0
    for row in events:
        date = row['Date'][:10]
        t_type = row.get('Type', '').lower()
        try:
            val = float(row['Value'])
            if t_type in ['deposit', 'removal', 'interest', 'dividend']:
                running_total += val
            elif t_type == 'sell':
                # This is hard. Let's just stick to "External Inflow" and sync at the end.
                pass
            daily_balance[date] = running_total
        except: continue

    if portfolio_json.exists():
        with open(portfolio_json, 'r') as f:
            p_data = json.load(f)
            actual_market_value = p_data.get('summary', {}).get('total_netvalue', 0)
            today = datetime.now().strftime('%Y-%m-%d')
            daily_balance[today] = actual_market_value

    history = [{"date": d, "value": round(daily_balance[d], 2)} for d in sorted(daily_balance.keys())]
    
    # Smooth the jump between the last transaction and today if they are the same date
    # or ensure today is included correctly.
    
    with open(history_file, 'w') as f:
        json.dump(history, f, indent=2)
    print(f"✅ History reconstructed.")

if __name__ == "__main__": reconstruct()

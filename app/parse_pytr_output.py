#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

def parse_portfolio(raw_path):
    text = Path(raw_path).read_text()
    lines = text.split('\n')
    positions = []
    summary = {}
    pos_re = re.compile(
        r'^(.{1,25}?)\s+([A-Z0-9]{12})\s+'
        r'([\d,\.]+)\s*\*\s*([\d,\.]+)\s*=\s*'
        r'([\d,\.]+)\s*->\s*([\d,\.]+)\s+'
        r'([\d,\.]+)\s+(-?[\d,\.]+)\s+(-?[\d,\.]+)%'
    )
    for line in lines:
        if line.startswith('Depot'):
            m = re.search(r'Depot\s+([\d,\.]+)\s*->\s*([\d,\.]+)\s+(-?[\d,\.]+)\s+(-?[\d,\.]+)%', line)
            if m:
                summary['depot_buycost'] = float(m.group(1).replace(',', ''))
                summary['depot_netvalue'] = float(m.group(2).replace(',', ''))
                summary['depot_pl_eur'] = float(m.group(3).replace(',', ''))
                summary['depot_pl_pct'] = float(m.group(4).replace(',', ''))
            continue
        if line.startswith('Cash'):
            m = re.search(r'Cash\s+\w*\s+([\d,\.]+)', line)
            if m: summary['cash_eur'] = float(m.group(1).replace(',', ''))
            continue
        if line.startswith('Total'):
            m = re.search(r'Total\s+([\d,\.]+)\s*->\s*([\d,\.]+)', line)
            if m:
                summary['total_buycost'] = float(m.group(1).replace(',', ''))
                summary['total_netvalue'] = float(m.group(2).replace(',', ''))
            continue
        m = pos_re.match(line)
        if m:
            positions.append({
                'name': m.group(1).strip(),
                'isin': m.group(2),
                'avg_cost': float(m.group(3).replace(',', '')),
                'quantity': float(m.group(4).replace(',', '')),
                'buy_cost_eur': float(m.group(5).replace(',', '')),
                'net_value_eur': float(m.group(6).replace(',', '')),
                'current_price': float(m.group(7).replace(',', '')),
                'pl_eur': float(m.group(8).replace(',', '')),
                'pl_pct': float(m.group(9).replace(',', '')),
            })
    missing = re.findall(r'Missing price for (.+?)\s*\(([A-Z0-9]{12})\)', text)
    zero_positions = [{'name': name.strip(), 'isin': isin} for name, isin in missing]
    return positions, summary, zero_positions

def main():
    project_dir = Path(__file__).resolve().parent.parent
    out_dir = project_dir / "DATA"
    out_dir.mkdir(exist_ok=True)
    raw_file = sys.argv[1] if len(sys.argv) > 1 else str(out_dir / 'portfolio_raw.txt')
    
    positions, summary, zero_positions = parse_portfolio(raw_file)
    positions.sort(key=lambda p: p['net_value_eur'], reverse=True)
    winners = [p for p in positions if p['pl_pct'] >= 50]
    losers = [p for p in positions if p['pl_pct'] <= -25]
    result = {
        'summary': summary,
        'total_positions': len(positions) + len(zero_positions),
        'positions_with_value': len(positions),
        'zero_value_positions': zero_positions,
        'top_25': positions[:25],
        'winners_50plus': sorted(winners, key=lambda p: -p['pl_pct']),
        'losers_25minus': sorted(losers, key=lambda p: p['pl_pct']),
        'all_positions': positions,
    }
    with open(out_dir / 'portfolio.json', 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'✅ Data saved to: {out_dir / "portfolio.json"}')

if __name__ == '__main__': main()

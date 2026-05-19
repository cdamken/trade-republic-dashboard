#!/usr/bin/env python3
"""
Trade Republic Export Parser

Parsea los CSV de transacciones que TR exporta.
Estos archivos son la fuente de verdad MÁS confiable porque vienen del broker.

USO:
    python3 parse_tr_export.py OLD/20260418_Transaction\\ export\\ 2.csv

OUTPUT:
    - Resumen de transacciones
    - P/L realizado por activo
    - Lista de posiciones que probablemente sigan abiertas (compradas - vendidas)
    - Total de comisiones e impuestos
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime


def parse_decimal(s):
    """TR usa coma decimal en algunos exports."""
    if not s or s.strip() == '':
        return 0.0
    s = s.strip().replace('€', '').replace(' ', '')
    # Si tiene "," como decimal y "." como separador miles
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_export(csv_path):
    """Parsea un CSV export de Trade Republic."""
    transactions = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        # Detect delimiter (TR usa ; o ,)
        sample = f.read(2048)
        f.seek(0)
        delim = ';' if sample.count(';') > sample.count(',') else ','
        reader = csv.DictReader(f, delimiter=delim)
        for row in reader:
            transactions.append(row)
    return transactions


def summarize(transactions):
    """Genera resumen agregado."""
    by_isin = defaultdict(lambda: {
        'name': '',
        'isin': '',
        'buys': 0.0, 'sells': 0.0,
        'shares_bought': 0.0, 'shares_sold': 0.0,
        'fees': 0.0, 'taxes': 0.0,
        'dividends': 0.0,
    })

    totals = {
        'total_buys': 0.0,
        'total_sells': 0.0,
        'total_dividends': 0.0,
        'total_fees': 0.0,
        'total_taxes': 0.0,
        'total_interest': 0.0,
        'transactions': len(transactions),
    }

    # TR CSV columns (verified format):
    # datetime, date, account_type, category, type, asset_class, name, symbol,
    # shares, price, amount, fee, tax, currency, original_amount, ...
    BUY_TYPES = {'BUY', 'PRIVATE_MARKET_BUY'}
    SELL_TYPES = {'SELL', 'WORTHLESS', 'LIQUIDATION_PROCEEDS', 'FINAL_MATURITY'}
    DIV_TYPES = {'DIVIDEND', 'EARNINGS', 'INTERMEDIATE_SECURITIES_DISTRIBUTION', 'LIQUIDATION_DIVIDEND'}
    INTEREST_TYPES = {'INTEREST_PAYMENT'}

    for tx in transactions:
        type_field = (tx.get('type') or '').strip().upper()
        isin = (tx.get('symbol') or '').strip()  # TR uses symbol as ISIN-like
        name = (tx.get('name') or '').strip()
        value = parse_decimal(tx.get('amount') or '0')
        shares = parse_decimal(tx.get('shares') or '0')
        fees = parse_decimal(tx.get('fee') or '0')
        taxes = parse_decimal(tx.get('tax') or '0')

        is_buy = type_field in BUY_TYPES
        is_sell = type_field in SELL_TYPES
        is_div = type_field in DIV_TYPES
        is_int = type_field in INTEREST_TYPES

        if isin:
            by_isin[isin]['name'] = name
            by_isin[isin]['isin'] = isin

        # In TR CSV: BUY amount is negative (cash out), SELL amount positive (cash in)
        abs_value = abs(value)
        if is_buy:
            totals['total_buys'] += abs_value
            if isin:
                by_isin[isin]['buys'] += abs_value
                by_isin[isin]['shares_bought'] += shares
        elif is_sell:
            totals['total_sells'] += abs_value
            if isin:
                by_isin[isin]['sells'] += abs_value
                by_isin[isin]['shares_sold'] += shares
        elif is_div:
            totals['total_dividends'] += abs_value
            if isin:
                by_isin[isin]['dividends'] += abs_value
        elif is_int:
            totals['total_interest'] += abs_value

        totals['total_fees'] += abs(fees)
        totals['total_taxes'] += abs(taxes)
        if isin:
            by_isin[isin]['fees'] += abs(fees)
            by_isin[isin]['taxes'] += abs(taxes)

    return totals, dict(by_isin)


def likely_open_positions(by_isin):
    """Posiciones donde shares_bought > shares_sold."""
    open_positions = []
    for isin, data in by_isin.items():
        net_shares = data['shares_bought'] - data['shares_sold']
        if net_shares > 0.0001:
            net_invested = data['buys'] - data['sells']
            open_positions.append({
                'name': data['name'],
                'isin': isin,
                'net_shares': net_shares,
                'net_invested_eur': net_invested,
                'realized_pl': data['sells'] - data['buys'] if data['sells'] > 0 else 0,
                'dividends_received': data['dividends'],
            })
    return sorted(open_positions, key=lambda x: x['net_invested_eur'], reverse=True)


def closed_positions(by_isin):
    """Posiciones donde shares_bought ≈ shares_sold (cerradas)."""
    closed = []
    for isin, data in by_isin.items():
        net_shares = data['shares_bought'] - data['shares_sold']
        if abs(net_shares) < 0.0001 and data['shares_sold'] > 0:
            realized_pl = data['sells'] - data['buys']
            closed.append({
                'name': data['name'],
                'isin': isin,
                'realized_pl': realized_pl,
                'shares_traded': data['shares_bought'],
            })
    return sorted(closed, key=lambda x: x['realized_pl'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description='Parse Trade Republic CSV exports')
    parser.add_argument('csv_file', help='Path to TR transaction export CSV')
    parser.add_argument('--top', type=int, default=20, help='Show top N positions')
    args = parser.parse_args()

    try:
        transactions = parse_export(args.csv_file)
    except FileNotFoundError:
        print(f'❌ File not found: {args.csv_file}')
        sys.exit(1)

    if not transactions:
        print('❌ No transactions parsed. Check CSV format.')
        sys.exit(1)

    print(f'\n📂 Parsed {len(transactions)} transactions from: {args.csv_file}\n')

    # Show first transaction columns to verify parsing
    if transactions:
        print('🔍 Detected columns:', list(transactions[0].keys())[:8], '...\n')

    totals, by_isin = summarize(transactions)

    print('=' * 70)
    print('📊 SUMMARY')
    print('=' * 70)
    print(f'  Total Buys:          €{totals["total_buys"]:>12,.2f}')
    print(f'  Total Sells:         €{totals["total_sells"]:>12,.2f}')
    print(f'  Total Dividends:     €{totals["total_dividends"]:>12,.2f}')
    print(f'  Total Interest:      €{totals["total_interest"]:>12,.2f}')
    print(f'  Total Fees:          €{totals["total_fees"]:>12,.2f}')
    print(f'  Total Taxes:         €{totals["total_taxes"]:>12,.2f}')
    net_invested = totals['total_buys'] - totals['total_sells']
    print(f'  Net invested in mkt: €{net_invested:>12,.2f}  (capital still deployed in market)')
    print(f'  Unique ISINs:        {len(by_isin)}')

    # Open positions
    open_pos = likely_open_positions(by_isin)
    print(f'\n=' * 1, '=' * 69, sep='')
    print(f'📈 LIKELY OPEN POSITIONS (top {args.top} by net invested)')
    print('=' * 70)
    print(f'{"Name":<40} {"Net Shares":>12} {"Net €":>12} {"Div":>10}')
    print('-' * 70)
    for pos in open_pos[:args.top]:
        print(f'{pos["name"][:38]:<40} {pos["net_shares"]:>12.4f} {pos["net_invested_eur"]:>12,.2f} {pos["dividends_received"]:>10,.2f}')

    # Closed positions (top winners and losers)
    closed = closed_positions(by_isin)
    if closed:
        print(f'\n' + '=' * 70)
        print('🏆 TOP REALIZED WINNERS (closed positions)')
        print('=' * 70)
        for pos in closed[:10]:
            if pos['realized_pl'] > 0:
                print(f'{pos["name"][:40]:<42} +€{pos["realized_pl"]:>10,.2f}')

        print(f'\n' + '=' * 70)
        print('💔 TOP REALIZED LOSERS (closed positions)')
        print('=' * 70)
        for pos in closed[-10:]:
            if pos['realized_pl'] < 0:
                print(f'{pos["name"][:40]:<42} -€{abs(pos["realized_pl"]):>10,.2f}')


if __name__ == '__main__':
    main()

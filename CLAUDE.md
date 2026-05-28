# CLAUDE.md — Trade-Republic-Dashboard

> Context for AI assistants. Humans: see [README.md](README.md).

## What this is

Local single-user dashboard for Trade Republic. Runs a Python HTTP
server on `localhost:8085` and renders a static dark-themed UI for
portfolio + analytics. Uses [`tr-api`](https://github.com/cdamken/tr-api)
as a library (since Phase 9 commit `3546f67`, 2026-05-22; replaced
`pytr`).

## Position in the trio

```
   tr-api (library)  ──┐
                       ├──► Trade-Republic-Dashboard  (this repo — upstream)
                       │      │
                       │      │  port: copy verbatim + minimal ownCloud patches
                       │      ▼
                       └──► Trade-Republic-owncloud (multi-user ownCloud app)
```

**This repo is upstream for the ownCloud port.** Any UI/UX or
data-shaping change should land here first. The ownCloud port's
`UPSTREAM.md` documents the few divergences forced by the multi-user
context (per-user paths, env-injected creds, etc.).

## Workflow rule

When fixing a bug or adding a feature:

1. Land it here first.
2. Verify locally (run `./dashboard.sh`, do an Update Now).
3. Port to `Trade-Republic-owncloud` (mostly a copy-paste of the
   touched files; UI is verbatim, JS paths get translated to
   ownCloud routes, Python data dir is per-user).
4. Sync to `oc_Apps/trade_republic/` and to the server.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Browser → http://localhost:8085/app/index.html    │
└──────────────────┬─────────────────────────────────┘
                   │ HTTP
┌──────────────────▼─────────────────────────────────┐
│  app/server.py  (Python stdlib http.server)        │
│   • /  /app/*                  → static files       │
│   • /DATA/*.json              → JSON output of fetches│
│   • /setup_status, /setup     → phone+PIN config    │
│   • /update                   → invokes tr_fetch.py │
│   • /reset                    → wipes profile+DATA  │
└──────────────────┬─────────────────────────────────┘
                   │ subprocess
┌──────────────────▼─────────────────────────────────┐
│  app/tr_fetch.py                                   │
│   • Reads phone+PIN from ~/.pytr/credentials       │
│   • Uses ~/.tr-api/profiles/<phone>/cookies.txt    │
│   • Opens ONE WS for both timeline topics          │
│     (see _paginate_topic_on_ws — pytr pattern)     │
│   • Writes DATA/portfolio.json,                    │
│           DATA/account_transactions.csv            │
│   • Calls app/analyze_analytics.py at the end      │
└──────────────────┬─────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────┐
│  app/analyze_analytics.py                          │
│   • Reads the CSV, computes analytics.json         │
│   • Reconstructs net_worth_history.json from CSV   │
│     cash-flow trajectory                           │
└────────────────────────────────────────────────────┘
```

## Key concepts (read this before touching analytics)

### EVENT_TYPE_MAP — the 2026 rename

TR renamed most eventTypes during 2026. The map in `tr_fetch.py` has
the current strings; old names are kept under `# legacy` so re-
processing pytr-era CSVs doesn't downgrade rows.

| Type column (CSV) | Modern TR eventType | Meaning |
|---|---|---|
| Deposit | `BANK_TRANSACTION_INCOMING`, `CARD_REFUND` | Money in |
| Withdrawal | `BANK_TRANSACTION_OUTGOING*` | Transfer to user's own bank |
| Removal | `CARD_TRANSACTION`, `CRYPTO_TRANSFER_NETWORK_FEE` | Lifestyle consumption |
| Tax Refund | `SSP_TAX_CORRECTION` | Finanzamt refund |
| Buy | `TRADING_SAVINGSPLAN_EXECUTED`, `SPARE_CHANGE_AGGREGATE`, `SAVEBACK_AGGREGATE`, + Trade with negative amount | Stock/ETF/crypto bought |
| Sell | Trade with positive amount | Stock/ETF/crypto sold |
| Dividend | `SSP_CORPORATE_ACTION_CASH` | Dividend/coupon |
| Interest | `INTEREST_PAYOUT(_CREATED)` | TR's cash-account interest |

### Withdrawal vs Removal (the analytics distinction)

`BANK_TRANSACTION_OUTGOING*` is "**you moved money from TR back to
your own bank**" — still your money, just elsewhere. It's a
**Withdrawal**, separate from **Removal** (card spending = real
consumption). This split matters for `net_capital_in`:

```
net_capital_in = Deposits + Tax refunds − Withdrawals
                ^                         ^
                NOT subtracting card spending — that's lifestyle, not
                capital flowing out of TR's "committed for investing"
                pool.

lifetime_pl   = current_value + Card spending
                              − net_capital_in − investment_income
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                Price appreciation on the capital you've committed to TR,
                excluding lifestyle spending and excluding dividend/interest
                receipts (those are returns ON capital, not capital itself).
```

### One WebSocket for both timeline topics

TR's `timelineTransactions` and `timelineActivityLog` MUST be
subscribed on the same WS connection. Two separate `fetch_all` calls
(opening separate WSs) returns 0 items on the second. See
`_paginate_topic_on_ws` and the surrounding `asyncio.run` block in
`tr_fetch.py::fetch_transactions`.

### net_worth_history reconstruction

`analyze_analytics.py` rebuilds the daily history from the CSV's
external cash flows (Deposits, Tax refunds, Dividends, Interest minus
Withdrawals and Removals) — cumulatively. This is a **cost-basis**
trajectory, NOT a true net-worth-over-time (we don't have historical
market values of positions). Today's actual market value lives in
the KPI card above the chart.

## File layout

```
app/
├── server.py                    HTTP server + /update flow + MFA modal logic
├── tr_fetch.py                  tr-api → CSV/JSON writer (one-WS for both topics)
├── analyze_analytics.py         CSV → analytics.json + net_worth_history.json
├── extract_tr.js                Browser-extension prototype (unused)
├── index.html                   Portfolio page
├── analytics.html               Cash-flow / dividends / allocation / net-worth charts
├── parse_pytr_output.py         Legacy parser (pre-tr-api era)
└── parse_tr_export.py           Legacy
DATA/                            Generated; gitignored
├── portfolio.json
├── portfolio_raw.json
├── account_transactions.csv     5 columns; one row per event
├── analytics.json
├── net_worth_history.json
├── last_update.date
└── server.log                   dashboard.sh redirects server.py stdout/stderr here
dashboard.sh                     Launcher (start/stop/restart/update/full/reset)
```

## Workflow rules

1. **Charts**: refined style helpers (`vGradient`, `AXIS_BASE`,
   `TOOLTIP`, `ANIMATION`) at the top of `analytics.html`'s inline
   `<script>`. Mirror them in the ownCloud port's `js/analytics.js`
   if you change any.
2. **Server logging**: every tr_fetch.py invocation logs a line like
   `[tr_fetch] exit=N status=...` to stderr → server.log. Useful for
   debugging "what went wrong".
3. **MFA `processId` lifetime**: ~60 seconds. The Full Reload checkbox
   in the MFA modal must NOT wipe `.pending_login.json` — fix lives in
   `server.py::_wipe_data_keep_session` which excludes it explicitly.
4. **Restart correctly**: `./dashboard.sh stop && ./dashboard.sh start`
   uses the venv's Python (the one with `tr-api` installed). Bare
   `python3 app/server.py` will use the system Python and exit 30
   ("tr-api is not installed") on every /update.

## Recently resolved

- **2026-05-28**: 'Documents' button + `POST /download_docs` endpoint.
  Shells out to `tr-api docs download --out DATA/documents/`. Files
  land in `DATA/documents/<YYYY>/<kind>/<file>.pdf`. Idempotent.
  Verified end-to-end against real TR.
- **2026-05-28**: `EVENT_TYPE_MAP` updated for TR's 2026 rename. CSV
  went from 331 rows to 5,979.
- **2026-05-28**: Withdrawal vs Removal split — `net_capital_in` now
  matches user's mental model ("money I committed to TR for investing").
- **2026-05-28**: One-WebSocket pattern (pytr-style) for combined
  timeline fetches.
- **2026-05-28**: Net-worth history reconstructed from CSV (was: only
  today's snapshot).

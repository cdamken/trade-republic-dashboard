# 📊 Trade Republic Dashboard

Local, private, and fast dashboard to visualize your Trade Republic portfolio with **100% verified data** — bypassing the slowness and the inverted signs bug of the official web app.

Includes a web UI with an **Update Now button + MFA modal** so you never need to drop to a terminal to refresh data.

![screenshot placeholder](docs/screenshot.png)

---

## 🎯 Why this exists

Trade Republic's web app has three real problems:

1. **It's slow** — takes 10–30 s to load and sometimes shows `€0.00` while prices are still streaming in.
2. **Inverted P/L signs** — losing positions show up as winning and vice versa (verified on Visa, Mastercard, Meta, UnitedHealth, and several others).
3. **No useful search or filters** — with 300+ positions, finding anything is painful.

This dashboard solves all three by using **[pytr](https://github.com/pytr-org/pytr)** (a community Python client that talks to TR's internal WebSocket) and rendering the data in local HTML with search, filters, sorting, and analytics.

---

## ✨ Features

- **Single script orchestrator** (`./dashboard.sh`) for everything: smart update, start/stop server, status.
- **First-time setup wizard in the browser** — on first launch, a welcome modal asks for your TR phone + PIN. No need to touch the terminal.
- **Web UI Update button with MFA modal** — when your TR session expires, a 4-digit code modal appears in the browser.
- **Smart incremental transactions sync** — only fetches what's new (~2s) instead of redownloading the whole history (~3min).
- **CLI fallback** — if you prefer, the script still supports the interactive flow with pytr's terminal prompt.
- **Analytics page** — dividends chart, allocation pie, net worth timeline, cash flow breakdown (deposits / removals / lifetime P/L).
- **Cash flow tracking** — see exactly how much you've put into TR vs. spent with the card vs. what remains.
- **100% local** — only the pytr ↔ TR WebSocket connection talks to the internet. Server is `localhost:8085`. Nothing is uploaded anywhere.

---

## 🚀 Quick Start

```bash
git clone https://github.com/cdamken/trade-republic-dashboard.git
cd trade-republic-dashboard

# One-time setup (see below)
./dashboard.sh

# After that: just run again to refresh + open the browser
./dashboard.sh
```

That's it. The script opens `http://localhost:8085/app/index.html` in your default browser.

---

## ⚙️ Setup (one-time)

Required: Python 3.10+, macOS or Linux, an active Trade Republic account.

```bash
# 1. Install pytr via pipx (recommended on macOS due to PEP 668)
brew install pipx           # or your package manager
pipx ensurepath
pipx install pytr

# 2. Install Chromium for Playwright (used by pytr to bypass AWS WAF)
~/.local/pipx/venvs/pytr/bin/playwright install chromium

# 3. Launch the dashboard — it will guide you through login in the browser
./dashboard.sh
```

The first time the dashboard opens, if no `~/.pytr/credentials` exists, a **welcome wizard** asks for your TR phone number and PIN. After that, the MFA modal opens for the 4-digit security code TR pushes to your phone. From that point on, everything is automatic.

> If you'd rather log in from the terminal, run `~/.local/bin/pytr login --store_credentials` once before launching the dashboard.

> Credentials are stored locally in `~/.pytr/credentials` with permissions `0600` (rw owner only). The file is plain text — the same file pytr uses. Never transmitted anywhere except to the official Trade Republic API.

> If `pytr` is in a non-standard location, set `PYTR_PATH=/path/to/pytr` in your env.

---

## 🛠️ Commands

```
./dashboard.sh              Smart update + start server + open browser  (default)
./dashboard.sh update       Same as above (explicit alias)
./dashboard.sh full         Force re-download of everything (~3 min)
./dashboard.sh start        Just start the local HTTP server
./dashboard.sh stop         Stop the server
./dashboard.sh restart      stop + start
./dashboard.sh status       Show data files, last update, server state
```

You can override the port with `TR_DASHBOARD_PORT=9000 ./dashboard.sh`.

---

## 🧠 How the smart update works

The dashboard consumes **two datasets** from TR with different characteristics:

| File | Changes | Size | Incremental? |
| :--- | :--- | ---: | :--- |
| `DATA/portfolio_raw.txt` | minute-to-minute (live prices) | ~40 KB | ❌ pytr always re-downloads (~5 s) |
| `DATA/account_transactions.csv` | a few times a day at most | ~1 MB | ✅ `--last_days N` + local merge (~2 s) |

### Algorithm

```
1. Portfolio: always re-download (live, ~5s)

2. Transactions: real incremental
   a. No CSV yet?               → full download (~3 min, one time)
   b. No last_update.date?      → full download (baseline)
   c. Gap > 365 days?           → full download (safety)
   d. Otherwise:
      → pytr export_transactions --last_days (days_since + 3 buffer)
      → merge: dedupe whole lines + sort chronologically
      → report: "Transactions: 14,211 → 14,225  (+14 new)"

3. Process:
   - parse_pytr_output.py  → DATA/portfolio.json
   - analyze_analytics.py  → DATA/analytics.json + DATA/net_worth_history.json

4. Cleanup auxiliary files (.DS_Store, *.tmp, *.partial)

5. Start local server (port 8085) if not running

6. Open browser at http://localhost:8085/app/index.html
```

**Measured performance:**
- Daily update: **~5–10 seconds** (vs 3 minutes without incremental)
- 1-week gap: ~10 seconds
- 1-month gap: ~15 seconds
- Forced full (`./dashboard.sh full`): ~3 minutes

---

## 🔐 Web UI Update + MFA modal

Click **"🔄 Update Now"** in the dashboard header.

- **Session valid** → the dashboard refreshes in a few seconds.
- **Session expired** → a modal appears asking for the 4-digit code that TR pushed to your phone (or sent via SMS if you press Enter on an empty input).
- **Wrong code** → modal shows an inline error; you can retry.

Internally, the browser POSTs to `/update`. The server invokes `app/tr_fetch.py --non-interactive` (with `--mfa-code` if you supplied one in the modal) and maps the exit code to an HTTP status:

| `tr_fetch.py` exit | HTTP | JSON status |
| ---: | ---: | :--- |
| 0 | 200 | `ok` |
| 10 | 401 | `mfa_required` |
| 11 | 401 | `mfa_invalid` |
| 12 | 401 | `auth_failed` |
| 20 | 502 | `api_error` |
| 30 | 500 | `config_error` |

This pattern is borrowed from the GBM dashboard architecture (which uses TOTP with the same skeleton; here we adapt it for TR's 4-digit push challenge).

---

## 📂 Repository layout

```
trade-republic-dashboard/
├── README.md                ← this file
├── LICENSE                  ← MIT
├── .gitignore               ← excludes DATA/ and state files
├── dashboard.sh             ← single orchestrator
│
├── app/                     ← project code (versioned)
│   ├── server.py                 HTTP server (port 8085) + POST /update
│   ├── tr_fetch.py               pytr wrapper with MFA-aware exit codes
│   ├── index.html                Portfolio page (search/filter/sort)
│   ├── analytics.html            Analytics page (dividends, cash flow, allocation, history)
│   ├── parse_pytr_output.py      Parser: portfolio_raw.txt → DATA/portfolio.json
│   ├── analyze_analytics.py      Calculates dividends, allocation, cash flow, history
│   ├── reconstruct_history.py    Reconstructs net worth history from CSV
│   └── extract_tr.js             (legacy) DOM extractor for the TR web app
│
└── DATA/                    ← downloaded / generated / state (gitignored)
    ├── portfolio_raw.txt         Raw `pytr portfolio` output
    ├── portfolio.json            Structured portfolio (consumed by index.html)
    ├── account_transactions.csv  Full transactions history
    ├── analytics.json            Analytics (consumed by analytics.html)
    ├── net_worth_history.json    Daily net worth snapshots
    ├── last_update.date          Date of last successful update
    ├── last_update.log           Log of last run
    ├── server.log                HTTP server log
    └── server.pid                Running server PID
```

---

## 🔒 Privacy & security

- **Everything is local.** Server binds to `localhost:8085`. The only outbound traffic is `pytr ↔ TR WebSocket`.
- **pytr is open source** ([github.com/pytr-org/pytr](https://github.com/pytr-org/pytr)) — community-maintained, widely used. TR doesn't block it for personal use.
- **Credentials** live in `~/.pytr/credentials` (encrypted by pytr). Skip `--store_credentials` if you'd rather log in each time.
- **`.gitignore` excludes `DATA/`** — committing this repo never leaks your portfolio data.
- **No telemetry, no external API calls, no analytics scripts in the HTML.**

---

## 🐛 Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `pytr login` fails with Playwright error | `~/.local/pipx/venvs/pytr/bin/playwright install chromium` |
| Browser shows "Cannot connect" / 404 | Server not running. `./dashboard.sh start` |
| `portfolio.json` is empty or stale | Run `./dashboard.sh` |
| You suspect the transactions CSV is wrong | Run `./dashboard.sh full` to force a clean re-download |
| Some positions show "Missing price" | TR doesn't price warrants or tiny fractions. Normal. |
| Port 8085 in use | `./dashboard.sh restart`, or set `TR_DASHBOARD_PORT=9000` |
| Update button does nothing | Open DevTools → Network tab → check the `/update` request |

---

## 📐 Technical notes

- **Default port: 8085.** Override with `TR_DASHBOARD_PORT=...` (read by both `dashboard.sh` and `app/server.py` should you change them).
- **PROJECT_DIR is auto-detected** from the script's location. Move the folder freely.
- **All Python scripts use `Path(__file__).resolve().parent.parent`** — fully portable.
- **`DATA/`** is created automatically on first run.

---

## 🙏 Credits

- [pytr-org/pytr](https://github.com/pytr-org/pytr) — without this, there's no dashboard. They reverse-engineered TR's WebSocket and maintain a great Python client.
- Architecture for the Update/MFA flow inspired by a sibling project using GBM México's API (TOTP variant of the same pattern).

---

## 📄 License

**Business Source License 1.1** — see [LICENSE](LICENSE).

Free for **personal, non-commercial use**. Hosting it as a service, redistributing
the source, or any kind of commercial / business use is not permitted without a
separate agreement with the author. The license auto-converts to **Apache 2.0**
on **May 19, 2030**.

For commercial licensing or any other questions, open an issue.

---

## ⚠️ Disclaimer

This is an unofficial, community-built tool. It is not affiliated with, endorsed by, or supported by Trade Republic Bank GmbH. Use at your own risk. The author is not responsible for any account issues, data loss, or financial decisions made based on the data shown by this dashboard.

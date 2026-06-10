"""Local HTTP server for the Trade Republic Dashboard.

Endpoints:
  GET  /app/*.html        — dashboard HTML
  GET  /DATA/*.json       — data fetched by tr_fetch.py
  POST /update            — request a refresh. Body: {} or {"mfa_code": "1234"}
                            Returns one of:
                              200 {"status": "ok", "output": "..."}
                              401 {"status": "mfa_required"}   (sesión expirada, no se proporcionó código)
                              401 {"status": "mfa_invalid"}    (código TR rechazado)
                              401 {"status": "auth_failed"}    (credenciales pytr inválidas)
                              502 {"status": "api_error"}      (red / pytr)
                              500 {"status": "config_error"}   (procesamiento local)
                              504 {"status": "timeout"}        (>3 min)
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = 8085
PROJECT_DIR = Path(__file__).resolve().parent.parent

# CSRF defense: every POST (/update, /setup, /reset, /download_docs,
# /settings) must come from a page served by THIS server — i.e. Origin
# matches localhost:PORT. Any other origin (a malicious page in another
# tab calling fetch() to localhost:8085) is rejected with 403.
#
# Requests with no Origin header (e.g. CLI tools like curl, dashboard.sh)
# are still allowed — browsers always send Origin on POST, so a missing
# Origin means "not a browser cross-site request". Ported verbatim from
# gbm-dashboard 2026-06-02.
_ALLOWED_ORIGINS = frozenset(
    {
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
    }
)
FETCH_SCRIPT = PROJECT_DIR / "app" / "tr_fetch.py"

# pytr stores credentials at ~/.pytr/credentials (2 lines: phone, PIN).
# We kept this path so users upgrading from the pytr-era don't have to redo setup;
# the cookie/profile state has moved to ~/.tr-api/profiles/<phone>/.
PYTR_CREDS = Path.home() / ".pytr" / "credentials"
TR_API_DIR = Path.home() / ".tr-api"
DATA_DIR = PROJECT_DIR / "DATA"

# Optional per-installation app settings (separate from credentials so
# wiping creds doesn't lose the user's documents-folder choice). Stored
# as JSON so future settings can join without schema churn.
APP_CONFIG = Path.home() / ".pytr" / "dashboard_config.json"
DEFAULT_DOCS_DIR = Path.home() / "Documents" / "Trade_Republic_Docs"


def _read_app_config() -> dict:
    if not APP_CONFIG.is_file():
        return {}
    try:
        return json.loads(APP_CONFIG.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_app_config(cfg: dict) -> None:
    APP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(APP_CONFIG)


def _docs_out_dir() -> Path:
    """Resolve the user-configured download path, with a sensible default
    in ~/Documents/Trade_Republic_Docs/. Always expanded to an absolute path."""
    cfg = _read_app_config()
    raw = (cfg.get("documents_path") or "").strip()
    if not raw:
        return DEFAULT_DOCS_DIR
    return Path(raw).expanduser().resolve()

# Map tr_fetch.py exit codes to (HTTP status, JSON status string)
EXIT_CODE_MAP = {
    0:  (200, "ok"),
    10: (401, "mfa_required"),
    11: (401, "mfa_invalid"),
    12: (401, "auth_failed"),
    20: (502, "api_error"),
    21: (429, "rate_limited"),
    30: (500, "config_error"),
}


# ---------------------------------------------------------------------------
# State-wipe helpers (used when the user changes account or PIN via /setup)
# ---------------------------------------------------------------------------
def _wipe_for_account_change(old_phone: str) -> None:
    """User switched to a different TR account. Drop everything tied to the
    previous identity so it can't leak into the new view:
      - the old tr-api profile dir (cookies, meta, active marker)
      - all generated data in DATA/ (portfolio, transactions, analytics, history)
      - the in-flight login state (.pending_login.json), if any
    """
    import shutil

    if old_phone:
        old_profile = TR_API_DIR / "profiles" / old_phone
        if old_profile.is_dir():
            try:
                shutil.rmtree(old_profile)
            except OSError:
                pass

    # The 'active' pointer file may still point to the old phone; clear it.
    active = TR_API_DIR / "active"
    if active.is_file():
        try:
            active.unlink()
        except OSError:
            pass

    # Wipe DATA folder contents (preserve the directory itself). This also
    # clears .pending_login.json so any in-flight push for the OLD phone
    # can't be completed against the NEW phone.
    if DATA_DIR.is_dir():
        for f in DATA_DIR.iterdir():
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            except OSError:
                pass

    # Legacy pytr session, in case it's still hanging around from old installs
    pytr_dir = Path.home() / ".pytr"
    if pytr_dir.is_dir():
        for f in pytr_dir.iterdir():
            if f.name.startswith("cookies."):
                try:
                    f.unlink()
                except OSError:
                    pass


def _wipe_data_keep_session() -> None:
    """Drop all locally derived data (DATA/*) but keep the user's
    credentials, tr-api session cookies, AND any in-flight login state.
    Used by the 'Full Reload' UI button: the user wants to re-download
    everything from TR but doesn't want to re-authenticate.

    Critically, we MUST preserve `.pending_login.json` here: this function
    runs in the same /update request that's about to call tr_fetch.py
    with --mfa-code. If we delete the pending_login file first, tr_fetch
    can't find the processId from the previous /update and will exit
    mfa_required, forcing the user to re-trigger the push from scratch.
    """
    import shutil

    # Files to preserve across a Full Reload. Anything else under DATA/
    # is regenerated by the next tr_fetch.py run.
    KEEP = {".pending_login.json"}

    if DATA_DIR.is_dir():
        for f in DATA_DIR.iterdir():
            if f.name in KEEP:
                continue
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            except OSError:
                pass


def _wipe_session_only(phone: str) -> None:
    """User kept the same phone but changed the PIN. Drop just the cookies so
    the next refresh forces a fresh login (which validates the new PIN). DATA
    stays — it's still the same account. Also clear any in-flight push state
    since the old PIN is no longer valid."""
    if phone:
        cookies_file = TR_API_DIR / "profiles" / phone / "cookies.txt"
        if cookies_file.is_file():
            try:
                cookies_file.unlink()
            except OSError:
                pass

    # Drop any pending login (push that's mid-flight)
    pending = DATA_DIR / ".pending_login.json"
    if pending.is_file():
        try:
            pending.unlink()
        except OSError:
            pass

    # Legacy pytr cookies too
    pytr_dir = Path.home() / ".pytr"
    if pytr_dir.is_dir():
        for f in pytr_dir.iterdir():
            if f.name.startswith("cookies."):
                try:
                    f.unlink()
                except OSError:
                    pass


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    # Less noisy access log: only show POSTs and the setup endpoint
    def log_message(self, format, *args):
        if any(p in self.requestline for p in ("/update", "/setup", "POST")):
            super().log_message(format, *args)

    # Force the browser to revalidate every request. Without this, a stale
    # cached HTML/JS makes a UI change invisible until the user does a hard
    # reload — and silently causes weirdness like "Update Now did nothing"
    # when the cached JS no longer matches the running server.
    def end_headers(self):
        path = getattr(self, "path", "")
        if path.startswith("/app/") or path.startswith("/DATA/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        # Per-page CSV exports — focused subsets of account_transactions.csv
        # and portfolio.json, one file per dashboard page. The button on
        # each page is a plain `<a href="/export/X.csv" download>` so no
        # JS is needed to drive them.
        if self.path == "/export/orders.csv":
            return self._export_orders_csv()
        if self.path == "/export/ledger.csv":
            return self._export_ledger_csv()
        if self.path == "/export/dividends.csv":
            return self._export_dividends_csv()
        if self.path == "/export/holdings.csv":
            return self._export_holdings_csv()

        if self.path == "/setup_status":
            phone = None
            if PYTR_CREDS.is_file():
                try:
                    first = PYTR_CREDS.read_text(encoding="utf-8").splitlines()[:1]
                    phone = (first[0].strip() if first else None) or None
                except OSError:
                    phone = None
            self._json(200, {
                "setup_complete": PYTR_CREDS.is_file() and bool(phone),
                "phone": phone,
            })
            return
        if self.path == "/settings":
            cfg = _read_app_config()
            self._json(200, {
                "documents_path": cfg.get("documents_path") or str(DEFAULT_DOCS_DIR),
                "default_documents_path": str(DEFAULT_DOCS_DIR),
            })
            return
        # Don't expose the project root, and redirect any unknown path
        # (e.g. /Dashboard/, /foo, /typo) to the portfolio page instead
        # of a bare 404 — easier when the user mistypes or pastes a
        # nonsense URL like `localhost:8085/Dashboard/`.
        ALLOWED_PREFIXES = ("/app/", "/DATA/", "/export/")
        if self.path in ("/", "/app", "/app/") or not (
            self.path.startswith(ALLOWED_PREFIXES)
            or self.path in ("/setup_status", "/settings")
        ):
            self.send_response(302)
            self.send_header("Location", "/app/index.html")
            self.end_headers()
            return
        # Block directory listings under the allowed prefixes (e.g.
        # `/DATA/` would otherwise show every fetched JSON file). Individual
        # files (e.g. `/DATA/portfolio.json`) still work — they don't end
        # in `/`. Anything else falls through to super().do_GET() below.
        if self.path.endswith("/"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Directory listing disabled. Go to /app/index.html\n")
            return
        # Anything else: serve static files via the parent class
        super().do_GET()

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        # CSRF defense. Browsers always send Origin on POST; if a request
        # arrives with a foreign origin, refuse before any handler runs.
        # An empty/missing Origin (CLI tools, server-side scripts) is
        # treated as trusted — those would have to be on the same machine
        # to even reach this port anyway.
        origin = self.headers.get("Origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            self._json(403, {"status": "forbidden", "detail": "bad origin"})
            return

        if self.path == "/setup":
            return self._handle_setup()
        if self.path == "/reset":
            return self._handle_reset()
        if self.path == "/download_docs":
            return self._handle_download_docs()
        if self.path == "/settings":
            return self._handle_settings_set()
        if self.path != "/update":
            self.send_response(404)
            self.end_headers()
            return

        # Parse JSON body (optional)
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
                return

        # Validate MFA code (4 digits for TR, vs 6 for TOTP)
        mfa_code = body.get("mfa_code")
        if mfa_code is not None:
            mfa_code = str(mfa_code).strip()
            if not (mfa_code.isdigit() and len(mfa_code) == 4):
                self._json(
                    400,
                    {"status": "bad_request", "detail": "mfa_code must be 4 digits"},
                )
                return

        # Optional "full reload" flag from the UI: wipes the locally
        # cached portfolio / transactions BEFORE re-fetching so we start
        # from a clean slate. Cookies are preserved (this is not Switch
        # Account), so the user doesn't have to re-authenticate.
        force_full = bool(body.get("full"))
        if force_full:
            _wipe_data_keep_session()

        # Build subprocess command
        cmd = [sys.executable, str(FETCH_SCRIPT), "--non-interactive"]
        if mfa_code:
            cmd += ["--mfa-code", mfa_code]
        if force_full:
            # Forces tr_fetch to redownload the entire transactions history
            # rather than the incremental window.
            cmd += ["--full"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            self._json(
                504, {"status": "timeout", "detail": "tr_fetch.py > 300s"}
            )
            return
        except Exception as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return

        http_status, json_status = EXIT_CODE_MAP.get(
            result.returncode, (500, "error")
        )
        # Sanitize: only keep tail of stderr (may contain useful detail but not entire trace)
        last_stderr_line = (result.stderr.strip().splitlines() or [""])[-1][:200]

        # Log every tr_fetch.py invocation result so debugging doesn't require
        # the user to capture the JSON response from the browser. Goes to the
        # server's own stdout/stderr, which dashboard.sh redirects to srv.log.
        # `mfa_code_redacted` so we don't accidentally print 4-digit codes
        # alongside their resulting status.
        mfa_redacted = "yes" if mfa_code else "no"
        full_flag   = "yes" if force_full else "no"
        print(
            f"[tr_fetch] exit={result.returncode} status={json_status} "
            f"mfa_code={mfa_redacted} full={full_flag} "
            f"stderr_tail={last_stderr_line!r}",
            file=sys.stderr,
            flush=True,
        )

        payload = {"status": json_status}
        if http_status == 200:
            payload["output"] = result.stdout[-2000:]
        else:
            payload["detail"] = last_stderr_line
        self._json(http_status, payload)

    # ------------------------------------------------------------------ setup
    def _handle_setup(self):
        """Save phone + PIN. Same endpoint covers first-time setup AND changing
        credentials while the dashboard is running.

        Behaviour follows the gbm-dashboard pattern:
          - If phone changes -> wipe the old tr-api profile's cookies and the
            DATA folder, so the new account doesn't see stale data and the
            next update triggers a fresh MFA login.
          - If only PIN changes -> wipe the current profile's cookies (a new
            login is required to confirm the PIN works), but keep DATA.

        The browser is expected to fire POST /update right after — that's
        what triggers the actual TR login + push notification."""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            self._json(400, {"status": "bad_request", "detail": "empty body"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
            return

        phone = (body.get("phone") or "").strip()
        pin = (body.get("pin") or "").strip()

        # Validate phone: must start with + and contain 8-15 digits after it
        import re
        if not re.fullmatch(r"\+\d{8,15}", phone):
            self._json(400, {
                "status": "bad_phone",
                "detail": "phone must be like +4912345678 (no spaces, no dashes)",
            })
            return
        if not (pin.isdigit() and 4 <= len(pin) <= 6):
            self._json(400, {
                "status": "bad_pin",
                "detail": "pin must be 4-6 digits",
            })
            return

        # Detect change vs. existing credentials, BEFORE we overwrite them.
        previous_phone = ""
        previous_pin = ""
        if PYTR_CREDS.is_file():
            try:
                lines = PYTR_CREDS.read_text(encoding="utf-8").splitlines()
                previous_phone = (lines[0] if lines else "").strip()
                previous_pin = (lines[1] if len(lines) > 1 else "").strip()
            except OSError:
                pass
        phone_changed = bool(previous_phone) and previous_phone != phone
        pin_changed = (not phone_changed) and bool(previous_pin) and previous_pin != pin

        # Write credentials file (atomic + 0600)
        try:
            PYTR_CREDS.parent.mkdir(parents=True, exist_ok=True)
            tmp = PYTR_CREDS.with_suffix(".tmp")
            tmp.write_text(f"{phone}\n{pin}\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(PYTR_CREDS)
        except Exception as e:
            self._json(500, {"status": "write_failed", "detail": str(e)})
            return

        # Apply wipe policy
        if phone_changed:
            _wipe_for_account_change(previous_phone)
        elif pin_changed:
            _wipe_session_only(phone)

        self._json(200, {
            "status": "ok",
            "next": "post_update_with_mfa",
            "account_changed": phone_changed,
            "pin_changed": pin_changed,
        })

    # ------------------------------------------------------------------ reset
    def _handle_reset(self):
        """Erase the current account: pytr credentials, cookies, and project DATA.
        Requires confirmation flag in the body: {"confirm": "delete"}."""
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
                return

        if body.get("confirm") != "delete":
            self._json(400, {
                "status": "confirm_required",
                "detail": 'send {"confirm": "delete"} to actually reset',
            })
            return

        import shutil
        removed = []
        errors = []

        # 1. Credentials + cookies.
        #    - ~/.pytr/credentials still stores phone+PIN (kept as source of truth).
        #    - ~/.pytr/cookies.* — legacy pytr session files; remove if present.
        #    - ~/.tr-api/ — new location for tr-api profiles + cookies.
        pytr_dir = Path.home() / ".pytr"
        if pytr_dir.is_dir():
            for f in pytr_dir.iterdir():
                if f.name == "credentials" or f.name.startswith("cookies."):
                    try:
                        f.unlink()
                        removed.append(str(f.relative_to(Path.home())))
                    except Exception as e:
                        errors.append(f"{f.name}: {e}")

        tr_api_dir = Path.home() / ".tr-api"
        if tr_api_dir.is_dir():
            try:
                shutil.rmtree(tr_api_dir)
                removed.append(".tr-api/ (tr-api profiles + cookies)")
            except Exception as e:
                errors.append(f".tr-api/: {e}")

        # 2. Project DATA/ contents (keep the directory itself; recreate it empty)
        data_dir = PROJECT_DIR / "DATA"
        if data_dir.is_dir():
            for f in data_dir.iterdir():
                try:
                    if f.is_dir():
                        shutil.rmtree(f)
                    else:
                        f.unlink()
                    removed.append(f"DATA/{f.name}")
                except Exception as e:
                    errors.append(f"DATA/{f.name}: {e}")

        if errors:
            self._json(500, {
                "status": "partial",
                "removed": removed,
                "errors": errors,
            })
            return

        self._json(200, {"status": "ok", "removed": removed})

    # ------------------------------------------------------------- settings
    def _handle_settings_set(self):
        """Update per-installation app settings. Currently supports:
          - documents_path: where `docs download` writes PDFs

        We don't auto-create the path here (that happens lazily on first
        download) so the user can configure a network drive that's
        currently disconnected.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            self._json(400, {"status": "bad_request", "detail": "empty body"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
            return

        cfg = _read_app_config()
        old_dir = _docs_out_dir()  # resolve BEFORE writing new config
        migrated_files = 0
        if "documents_path" in body:
            new_path = (body.get("documents_path") or "").strip()
            if new_path:
                # Sanity check: expand ~, resolve, and verify it doesn't
                # collapse to root/empty.
                resolved = Path(new_path).expanduser()
                if str(resolved).strip("/") == "":
                    self._json(400, {
                        "status": "bad_path",
                        "detail": "documents_path cannot be empty or root",
                    })
                    return
                cfg["documents_path"] = str(resolved)
            else:
                cfg.pop("documents_path", None)  # falls back to default

        try:
            _write_app_config(cfg)
        except Exception as e:
            self._json(500, {"status": "write_failed", "detail": str(e)})
            return

        # If the documents path actually changed and the old location had
        # files in it, migrate them so the user doesn't lose previous
        # downloads. We move (not copy) to preserve disk space; users who
        # had downloads in DATA/documents/ from before the configurable-
        # path feature get them auto-migrated to ~/Documents/Trade_Republic_Docs/.
        new_dir = _docs_out_dir()
        try:
            if old_dir.resolve() != new_dir.resolve() and old_dir.is_dir():
                import shutil
                new_dir.mkdir(parents=True, exist_ok=True)
                for item in old_dir.iterdir():
                    dest = new_dir / item.name
                    if dest.exists():
                        continue  # don't overwrite — user can dedupe manually
                    shutil.move(str(item), str(dest))
                    migrated_files += 1
                # If we emptied the old dir, prune it (best-effort).
                try:
                    old_dir.rmdir()
                except OSError:
                    pass
        except Exception as e:
            print(f"[settings] migration failed: {e}", file=sys.stderr, flush=True)

        self._json(200, {
            "status": "ok",
            "documents_path": cfg.get("documents_path") or str(DEFAULT_DOCS_DIR),
            "migrated_files": migrated_files,
            "old_path": str(old_dir) if migrated_files else None,
        })

    # --------------------------------------------------------- download_docs
    def _handle_download_docs(self):
        """Download every PDF Trade Republic has issued for this account.

        Body is optional JSON:
          {"since": "YYYY-MM-DD", "kinds": "trades,dividends,..."}

        Files land in DATA/documents/<YYYY>/<kind>/<file>.pdf. Idempotent —
        re-running only fetches what's missing.

        Exit codes inherited from tr-api CLI (see tr-api/docs/cli-contract.md).
        """
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"status": "bad_request", "detail": "invalid JSON"})
                return

        # Fast pre-check: is the TR session still alive? If not, try the
        # silent refresh first (pytr-style) — that revives most "expired"
        # sessions without re-login. Only if the refresh itself fails do
        # we ask the user to do a fresh MFA login.
        ping_cmd = [sys.executable, "-m", "tr_api.cli", "--json", "ping"]
        try:
            ping = subprocess.run(ping_cmd, capture_output=True, text=True, timeout=15)
            ping_env = json.loads(ping.stdout or "{}")
            alive = ping_env.get("ok") and ping_env.get("data", {}).get("alive")
            if not alive:
                # Try refresh — costs ~5-10s (Playwright + WAF + HTTP GET)
                refresh_cmd = [sys.executable, "-m", "tr_api.cli", "--json", "auth", "refresh"]
                refresh = subprocess.run(refresh_cmd, capture_output=True, text=True, timeout=30)
                refresh_env = json.loads(refresh.stdout or "{}")
                if refresh_env.get("ok") and refresh_env.get("data", {}).get("ok"):
                    print("[docs] session refreshed silently via auth refresh",
                          file=sys.stderr, flush=True)
                else:
                    self._json(401, {
                        "status": "auth_required",
                        "detail": "Your Trade Republic session expired and the "
                                  "silent refresh failed. Click 'Update Now' "
                                  "to do a full re-login (MFA push), then try "
                                  "Documents again.",
                    })
                    return
        except Exception as e:
            # Don't block the user if ping itself blows up — fall through
            # and let the download attempt return whatever real error
            print(f"[docs] pre-ping failed: {e}", file=sys.stderr, flush=True)

        out_dir = _docs_out_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            self._json(400, {
                "status": "bad_path",
                "detail": f"Cannot create documents folder {out_dir}: {e}. "
                          f"Check your Settings or pick a different folder.",
            })
            return

        # Build tr-api CLI command. We always pass --json so we can parse the
        # exit envelope below.
        cmd = [
            sys.executable, "-m", "tr_api.cli", "--json",
            "docs", "download",
            "--out", str(out_dir),
        ]
        since = body.get("since")
        if since:
            cmd += ["--since", str(since)]
        kinds = body.get("kinds")
        if kinds:
            cmd += ["--kinds", str(kinds)]

        try:
            # 30 min ceiling — full history with thousands of PDFs is plausible.
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            self._json(504, {"status": "timeout", "detail": "docs download > 30 min"})
            return
        except Exception as e:
            self._json(500, {"status": "error", "detail": str(e)})
            return

        # Parse the JSON envelope tr-api always emits with --json.
        try:
            envelope = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            envelope = {"ok": False, "error": "ParseError", "message": result.stderr[-500:]}

        print(
            f"[docs] exit={result.returncode} ok={envelope.get('ok')} "
            f"counts={envelope.get('data', {}).get('counts')}",
            file=sys.stderr,
            flush=True,
        )

        if envelope.get("ok"):
            data = envelope.get("data") or {}
            self._json(200, {
                "status": "ok",
                "out_dir": data.get("out_dir"),
                "counts": data.get("counts", {}),
                "manifest": data.get("manifest"),
            })
            return

        # Map known tr-api exit codes to HTTP statuses
        exit_code = envelope.get("exit_code", result.returncode)
        if exit_code in (20, 30):  # MISSING_COOKIES / SESSION_EXPIRED
            self._json(401, {"status": "auth_required",
                             "detail": envelope.get("message", "")})
        elif exit_code == 41:
            self._json(429, {"status": "rate_limited",
                             "detail": envelope.get("message", "")})
        else:
            self._json(500, {
                "status": "error",
                "exit_code": exit_code,
                "detail": envelope.get("message", "")[:500],
            })

    # ---- CSV exports ---------------------------------------------------
    # Each endpoint is a focused subset of account_transactions.csv or
    # portfolio.json — gives the user exactly the columns visible on the
    # corresponding dashboard page. Lighter than the full CSV download
    # you'd give to an accountant.

    # eventType filters for each export. EVENT_TYPE_MAP in tr_fetch.py
    # is the authoritative list — we mirror the categories here in plain
    # tuples so the server doesn't import tr_fetch (which pulls tr-api).
    _BUY_SELL_EVENT_TYPES = (
        "TRADING_TRADE_EXECUTED",
        "TRADING_SAVINGSPLAN_EXECUTED",
        "SPARE_CHANGE_AGGREGATE",
        "SAVEBACK_AGGREGATE",
        "CRYPTO_BUY_EXECUTED",
        "CRYPTO_SELL_EXECUTED",
    )
    _DIVIDEND_EVENT_TYPES = (
        "SSP_CORPORATE_ACTION_CASH",
        "ssp_corporate_action_invoice_cash",  # legacy lowercase variant
    )

    def _send_csv(self, filename: str, rows):
        """Stream a list-of-lists as text/csv with a download disposition."""
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
        for row in rows:
            w.writerow(row)
        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_account_transactions(self):
        """Generator yielding (date, type, value, note, isin, shares, fees,
        taxes, isin2, shares2, event_type, event_subtype) tuples from
        DATA/account_transactions.csv. Returns [] silently if missing."""
        import csv as _csv
        path = DATA_DIR / "account_transactions.csv"
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8", newline="") as f:
            r = _csv.reader(f, delimiter=";")
            header = next(r, None)
            if not header:
                return
            for row in r:
                # Pad short rows to 12 cols so callers can index safely.
                row = (row + [""] * 12)[:12]
                yield row

    def _export_orders_csv(self):
        rows = [["date", "side", "eventType", "isin", "security",
                 "quantity", "amount_eur", "status"]]
        for r in self._read_account_transactions():
            date, typ, val, note, isin, shares, _fees, _taxes, _i2, _s2, ev, sub = r
            # Buy/sell heuristic: known event type OR Type column == "Buy"/"Sell".
            is_trade = (ev in self._BUY_SELL_EVENT_TYPES) or typ in ("Buy", "Sell")
            if not is_trade:
                continue
            # Side: prefer the explicit Type column; fall back to amount sign
            # (negative = Buy, positive = Sell — TR convention).
            if typ in ("Buy", "Sell"):
                side = typ
            else:
                try:
                    side = "Buy" if float(val or 0) < 0 else "Sell"
                except ValueError:
                    side = ""
            status = sub or "executed"
            rows.append([date, side, ev, isin, note, shares, val, status])
        self._send_csv("orders.csv", rows)

    def _export_ledger_csv(self):
        rows = [["date", "eventType", "category", "description",
                 "related_isin", "amount_eur", "status"]]
        # Category mapping: same categories the dashboard's Ledger page shows.
        WITHDRAWAL_PREFIX = "BANK_TRANSACTION_OUTGOING"
        DEPOSIT_TYPES = ("BANK_TRANSACTION_INCOMING", "CARD_REFUND")
        for r in self._read_account_transactions():
            date, typ, val, note, isin, _sh, _f, _t, _i2, _s2, ev, sub = r
            if ev in self._BUY_SELL_EVENT_TYPES or typ in ("Buy", "Sell"):
                cat = "trade"
            elif ev in self._DIVIDEND_EVENT_TYPES or typ == "Dividend":
                cat = "dividend"
            elif ev in DEPOSIT_TYPES or typ == "Deposit":
                cat = "deposit"
            elif ev.startswith(WITHDRAWAL_PREFIX) or typ == "Withdrawal":
                cat = "withdrawal"
            elif ev == "CARD_TRANSACTION" or typ == "Removal":
                cat = "card_spending"
            elif ev == "SSP_TAX_CORRECTION" or typ == "Tax Refund":
                cat = "tax_refund"
            elif ev.startswith("INTEREST_PAYOUT") or typ == "Interest":
                cat = "interest"
            else:
                cat = "other"
            rows.append([date, ev, cat, note, isin, val, sub or ""])
        self._send_csv("ledger.csv", rows)

    def _export_dividends_csv(self):
        rows = [["date", "security", "isin", "amount_eur", "currency", "status"]]
        for r in self._read_account_transactions():
            date, typ, val, note, isin, _sh, _f, _t, _i2, _s2, ev, sub = r
            if ev in self._DIVIDEND_EVENT_TYPES or typ == "Dividend":
                rows.append([date, note, isin, val, "EUR", sub or "credited"])
        self._send_csv("dividends.csv", rows)

    def _export_holdings_csv(self):
        rows = [["name", "isin", "type", "qty", "fifo",
                 "current_price", "value_eur", "daily_pnl"]]
        path = DATA_DIR / "portfolio.json"
        if not path.is_file():
            self._send_csv("holdings.csv", rows)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_csv("holdings.csv", rows)
            return
        for p in (data.get("all_positions") or []):
            rows.append([
                p.get("name") or p.get("security") or "",
                p.get("isin") or "",
                p.get("type") or p.get("category") or "",
                p.get("qty") or p.get("quantity") or "",
                p.get("avg_cost") or p.get("fifo_avg_cost") or "",
                p.get("current_price") or "",
                p.get("net_value_eur") or "",
                p.get("daily_pnl_eur") or p.get("pl_eur") or "",
            ])
        self._send_csv("holdings.csv", rows)

    # ---- helpers -------------------------------------------------------
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded so Chrome keep-alive connections don't block /update calls."""
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Background session keepalive thread (pytr-style)
# ---------------------------------------------------------------------------
# Every ~290s we call `tr-api auth refresh` which GETs
# /api/v1/auth/web/session — TR rotates JSESSIONID + tr_session and
# saves them back to the cookie file. As a result, the session stays
# alive as long as the dashboard process runs, even when the user is
# AFK for hours. Without this, cookies die after ~5-15 min idle and
# the user has to do a fresh MFA login.
#
# Runs as a daemon thread (dies with the process). Sleeps in short
# slices so a Ctrl-C feels instant rather than waiting up to 290s.
KEEPALIVE_INTERVAL_SEC = 290
KEEPALIVE_SLICE_SEC = 5


def _session_keepalive_loop() -> None:
    # First refresh runs a bit after startup so we don't race the first
    # /update from the UI (which may itself open Playwright for MFA).
    next_due = time.time() + 60.0
    while True:
        # Sleep in small slices so the daemon thread is interruptible.
        while time.time() < next_due:
            time.sleep(KEEPALIVE_SLICE_SEC)
        try:
            # Only refresh if creds are configured — first-time setup
            # shouldn't trigger Playwright in the background.
            if PYTR_CREDS.is_file():
                r = subprocess.run(
                    [sys.executable, "-m", "tr_api.cli", "--json", "auth", "refresh"],
                    capture_output=True, text=True, timeout=45,
                )
                envelope = json.loads(r.stdout or "{}")
                ok = envelope.get("ok") and envelope.get("data", {}).get("ok")
                changed = envelope.get("data", {}).get("cookies_changed") or []
                print(
                    f"[keepalive] refresh ok={bool(ok)} changed={changed}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as e:
            print(f"[keepalive] refresh attempt failed: {e}", file=sys.stderr, flush=True)
        next_due = time.time() + KEEPALIVE_INTERVAL_SEC


_keepalive_thread = threading.Thread(
    target=_session_keepalive_loop, daemon=True, name="tr-session-keepalive"
)
_keepalive_thread.start()


os.chdir(PROJECT_DIR)
with ThreadedServer(("", PORT), Handler) as httpd:
    print(f"🚀 Dashboard Server running at http://localhost:{PORT}/app/index.html")
    print(f"🔁 Session keepalive thread started (refresh every {KEEPALIVE_INTERVAL_SEC}s)")
    httpd.serve_forever()

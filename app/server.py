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
from pathlib import Path

PORT = 8085
PROJECT_DIR = Path(__file__).resolve().parent.parent
FETCH_SCRIPT = PROJECT_DIR / "app" / "tr_fetch.py"

# pytr stores credentials at ~/.pytr/credentials (2 lines: phone, PIN).
# We kept this path so users upgrading from the pytr-era don't have to redo setup;
# the cookie/profile state has moved to ~/.tr-api/profiles/<phone>/.
PYTR_CREDS = Path.home() / ".pytr" / "credentials"
TR_API_DIR = Path.home() / ".tr-api"
DATA_DIR = PROJECT_DIR / "DATA"

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

    # ------------------------------------------------------------------ GET
    def do_GET(self):
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
        # Don't expose the project root directory listing.
        if self.path in ("/", "/app", "/app/"):
            self.send_response(302)
            self.send_header("Location", "/app/index.html")
            self.end_headers()
            return
        # Anything else: serve static files via the parent class
        super().do_GET()

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if self.path == "/setup":
            return self._handle_setup()
        if self.path == "/reset":
            return self._handle_reset()
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

        # Build subprocess command
        cmd = [sys.executable, str(FETCH_SCRIPT), "--non-interactive"]
        if mfa_code:
            cmd += ["--mfa-code", mfa_code]

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


os.chdir(PROJECT_DIR)
with ThreadedServer(("", PORT), Handler) as httpd:
    print(f"🚀 Dashboard Server running at http://localhost:{PORT}/app/index.html")
    httpd.serve_forever()

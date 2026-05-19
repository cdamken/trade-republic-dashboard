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

# Map tr_fetch.py exit codes to (HTTP status, JSON status string)
EXIT_CODE_MAP = {
    0:  (200, "ok"),
    10: (401, "mfa_required"),
    11: (401, "mfa_invalid"),
    12: (401, "auth_failed"),
    20: (502, "api_error"),
    30: (500, "config_error"),
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    # Less noisy access log: only show POST /update lines
    def log_message(self, format, *args):
        if "/update" in self.requestline or "update" in format:
            super().log_message(format, *args)

    def do_POST(self):
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

    # ---- helpers -------------------------------------------------------
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


os.chdir(PROJECT_DIR)
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"🚀 Dashboard Server running at http://localhost:{PORT}/app/index.html")
    httpd.serve_forever()

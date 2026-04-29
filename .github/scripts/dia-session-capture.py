"""
dia-session-capture.py — mitmproxy addon for Dia login session extraction.

Two jobs:
  1. Password injection: intercepts any POST with a "password" field destined for
     account.diabrowser.engineering and replaces the React-truncated 1-char value
     with the real password from /tmp/.dia-pass. Covers both /signup/start AND
     whatever /signin/* endpoint the login flow uses.

  2. Cookie capture: records every Set-Cookie header from account.diabrowser.engineering
     and writes /tmp/dia-session/storage_state.json in Playwright format after each
     auth response, so the file is available even if the workflow terminates early.

Usage:
  mitmdump -w /tmp/dia-traffic.mitm --listen-port 8080 -s .github/scripts/dia-session-capture.py
"""

import json
import os
import time
from mitmproxy import http

PASS_FILE = "/tmp/.dia-pass"
SESSION_DIR = "/tmp/dia-session"
SESSION_FILE = f"{SESSION_DIR}/storage_state.json"
AUTH_RESULT_FILE = f"{SESSION_DIR}/auth-result.json"
TARGET_HOST = "account.diabrowser.engineering"


class SessionCaptureAddon:

    def __init__(self):
        os.makedirs(SESSION_DIR, exist_ok=True)
        self.cookies = {}     # name → Playwright cookie dict
        self.auth_result = {}

    # ------------------------------------------------------------------
    # Password injection — generalised for any auth POST
    # ------------------------------------------------------------------

    def request(self, flow: http.HTTPFlow) -> None:
        if TARGET_HOST not in flow.request.host:
            return
        if flow.request.method != "POST":
            return

        try:
            body = json.loads(flow.request.content)
        except Exception:
            return

        if "password" not in body:
            return

        if not os.path.exists(PASS_FILE):
            print(f"[session-capture] WARNING: {PASS_FILE} missing — passing unmodified", flush=True)
            return

        try:
            password = open(PASS_FILE).read().strip()
        except Exception as e:
            print(f"[session-capture] ERROR reading password: {e}", flush=True)
            return

        original_len = len(body.get("password", ""))
        if original_len >= len(password):
            # Already full length — probably not truncated, don't modify
            return

        body["password"] = password
        new_content = json.dumps(body).encode()
        flow.request.content = new_content
        flow.request.headers["content-length"] = str(len(new_content))
        print(
            f"[session-capture] Password injected on POST {flow.request.path} "
            f"(was {original_len} chars → {len(password)} chars)",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Cookie + auth token capture
    # ------------------------------------------------------------------

    def response(self, flow: http.HTTPFlow) -> None:
        if TARGET_HOST not in flow.request.host:
            return

        status = flow.response.status_code

        # Harvest Set-Cookie headers
        set_cookies = flow.response.headers.get_all("set-cookie")
        if set_cookies:
            for raw in set_cookies:
                self._parse_and_store_cookie(raw)

        # Log auth responses
        if flow.request.method == "POST" and status in (200, 201):
            try:
                body = json.loads(flow.response.content)
                if body.get("success"):
                    result_data = body.get("result", {})
                    print(
                        f"[session-capture] Auth success: {flow.request.path} → "
                        f"result keys: {list(result_data.keys()) if isinstance(result_data, dict) else type(result_data).__name__}",
                        flush=True,
                    )
                    self.auth_result = {
                        "path": flow.request.path,
                        "status": status,
                        "success": True,
                        "result": result_data,
                        "timestamp": time.time(),
                    }
                    try:
                        with open(AUTH_RESULT_FILE, "w") as f:
                            json.dump(self.auth_result, f, indent=2)
                    except Exception:
                        pass
                elif not body.get("success"):
                    err = body.get("error", {})
                    print(
                        f"[session-capture] Auth FAIL on {flow.request.path}: "
                        f"{err.get('code')} — {err.get('message')}",
                        flush=True,
                    )
            except Exception:
                pass

        # Write state after every response that touched cookies
        if set_cookies:
            self._write_storage_state()

    def _parse_and_store_cookie(self, raw: str):
        """Parse a Set-Cookie header string into a Playwright cookie dict."""
        parts = [p.strip() for p in raw.split(";")]
        if not parts or "=" not in parts[0]:
            return

        name, _, value = parts[0].partition("=")
        name = name.strip()
        value = value.strip()

        cookie = {
            "name": name,
            "value": value,
            "domain": TARGET_HOST,
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        }

        for attr in parts[1:]:
            al = attr.lower()
            if al == "httponly":
                cookie["httpOnly"] = True
            elif al == "secure":
                cookie["secure"] = True
            elif al.startswith("path="):
                cookie["path"] = attr[5:].strip()
            elif al.startswith("domain="):
                cookie["domain"] = attr[7:].strip()
            elif al.startswith("samesite="):
                val = attr[9:].strip().capitalize()
                if val in ("Strict", "Lax", "None"):
                    cookie["sameSite"] = val
            elif al.startswith("max-age="):
                try:
                    cookie["expires"] = int(time.time()) + int(attr[8:])
                except Exception:
                    pass
            elif al.startswith("expires="):
                # Best-effort; leave as -1 if parse fails
                pass

        self.cookies[name] = cookie
        print(f"[session-capture] Cookie captured: {name} (httpOnly={cookie['httpOnly']})", flush=True)

    def _write_storage_state(self):
        """Write current state to SESSION_FILE in Playwright storage_state format."""
        state = {
            "cookies": list(self.cookies.values()),
            "origins": [
                {
                    "origin": f"https://{TARGET_HOST}",
                    "localStorage": [],   # populated by dia-cdp-extract.py
                }
            ],
        }
        try:
            with open(SESSION_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print(
                f"[session-capture] storage_state.json updated "
                f"({len(self.cookies)} cookies)",
                flush=True,
            )
        except Exception as e:
            print(f"[session-capture] ERROR writing state: {e}", flush=True)


addons = [SessionCaptureAddon()]

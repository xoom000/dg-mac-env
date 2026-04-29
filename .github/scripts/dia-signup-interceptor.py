"""
dia-signup-interceptor.py — mitmproxy addon for Dia signup password injection.

Problem: osascript keystrokes only deliver 1 character to Dia's React-controlled
password fields (re-render consumes subsequent keystrokes). Dia submits
POST /signup/start with {"password": "-"} → server returns 400 password_too_short.

Fix: intercept POST /signup/start in-flight and replace the password field
with the real password before the request reaches the server.

Dia's TLS fingerprint, x-dia-device-id, x-client-version, and Cloudflare __cf_bm
cookie all stay intact — we only modify the JSON body.

Usage: mitmdump -w /tmp/dia-traffic.mitm --listen-port 8080 -s .github/scripts/dia-signup-interceptor.py
"""

import json
import os

from mitmproxy import http


PASS_FILE = "/tmp/.dia-pass"
TARGET_PATH = "/signup/start"
INTERCEPTED_FILE = "/tmp/dia-interceptor-result.json"


class SignupPasswordInjector:

    def request(self, flow: http.HTTPFlow) -> None:
        if TARGET_PATH not in flow.request.path:
            return
        if flow.request.method != "POST":
            return

        print(f"[interceptor] Caught POST {flow.request.pretty_url}", flush=True)

        # Read full password from temp file
        if not os.path.exists(PASS_FILE):
            print(f"[interceptor] WARNING: {PASS_FILE} not found — passing request unmodified", flush=True)
            return

        try:
            password = open(PASS_FILE).read().strip()
        except Exception as e:
            print(f"[interceptor] ERROR reading password: {e}", flush=True)
            return

        # Parse the request body
        try:
            body = json.loads(flow.request.content)
        except Exception as e:
            print(f"[interceptor] ERROR parsing body: {e} — body: {flow.request.content[:200]}", flush=True)
            return

        original_password = body.get("password", "")
        print(f"[interceptor] Original password length: {len(original_password)}", flush=True)

        # Replace password with full value
        body["password"] = password
        new_content = json.dumps(body).encode()
        flow.request.content = new_content
        flow.request.headers["content-length"] = str(len(new_content))

        print(f"[interceptor] Replaced password (len={len(password)}) — forwarding to server", flush=True)

        # Write intercept record for the main workflow to check
        try:
            with open(INTERCEPTED_FILE, "w") as f:
                json.dump({
                    "intercepted": True,
                    "email": body.get("email", ""),
                    "original_password_len": len(original_password),
                    "injected_password_len": len(password),
                }, f)
        except Exception as e:
            print(f"[interceptor] WARNING: could not write result file: {e}", flush=True)

    def response(self, flow: http.HTTPFlow) -> None:
        if TARGET_PATH not in flow.request.path:
            return
        if flow.request.method != "POST":
            return

        status = flow.response.status_code
        body_preview = flow.response.content[:300].decode("utf-8", errors="replace")
        print(f"[interceptor] /signup/start response: {status} — {body_preview}", flush=True)

        # Update result file with response
        try:
            result = {}
            if os.path.exists(INTERCEPTED_FILE):
                result = json.load(open(INTERCEPTED_FILE))
            result["response_status"] = status
            result["response_body"] = body_preview
            result["success"] = (status in (200, 201))
            with open(INTERCEPTED_FILE, "w") as f:
                json.dump(result, f)
        except Exception as e:
            print(f"[interceptor] WARNING: could not update result file: {e}", flush=True)


addons = [SignupPasswordInjector()]

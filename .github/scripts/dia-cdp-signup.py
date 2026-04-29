#!/usr/bin/env python3
"""
dia-cdp-signup.py — CDP Stage 2 form fill for Dia account provisioning.

Stage 1 (osascript) enters the email and fires POST /signin/method, which
sets a session cookie and navigates Dia's native webview to the signup form.
That native webview is NOT exposed as a CDP target.

This script:
1. Connects to the browser-level CDP WS
2. Creates a NEW, inspectable tab via Target.createTarget
3. Navigates it to account.diabrowser.engineering/signup — the session cookie
   from Stage 1 carries over (same browser cookie jar)
4. Fills the form using React nativeInputValueSetter
5. Submits

Pattern: Hunter's Approach A (React nativeInputValueSetter).
Ref: engagements/bcny-2026-04-28/notes/cdp-input-pattern.md
"""

import json
import os
import sys
import time
import urllib.request

import websocket  # pip: websocket-client

CDP_PORT = 9222
SIGNUP_URL = "https://account.diabrowser.engineering/signup"
SIGNIN_URL = "https://account.diabrowser.engineering/"

# -------------------------------------------------------------------------
# Browser-level CDP
# -------------------------------------------------------------------------

def get_browser_ws_url():
    url = f"http://localhost:{CDP_PORT}/json/version"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read()).get("webSocketDebuggerUrl")


class BrowserCDP:
    """Persistent browser-level CDP connection."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=20)
        self._next_id = 1

    def send(self, method, params=None, session_id=None, timeout=20):
        mid = self._next_id
        self._next_id += 1
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(deadline - time.time())
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            resp = json.loads(raw)
            if resp.get("id") == mid:
                return resp
        return None

    def drain_events(self, seconds=1):
        self.ws.settimeout(seconds)
        while True:
            try:
                self.ws.recv()
            except (websocket.WebSocketTimeoutException, websocket.WebSocketConnectionClosedException):
                break

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# -------------------------------------------------------------------------
# React input fill helper
# -------------------------------------------------------------------------

FILL_JS = """
(function() {
    var inputs = document.querySelectorAll('input[type="password"]');
    if (inputs.length < 2) {
        // Dump all inputs for diagnostics
        var all = Array.from(document.querySelectorAll('input')).map(function(e) {
            return {type: e.type, name: e.name, id: e.id, placeholder: e.placeholder};
        });
        return 'ERR:fields=' + inputs.length + ':all=' + JSON.stringify(all);
    }
    var nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    function reactSet(el, val) {
        el.focus();
        nativeSetter.call(el, val);
        el.dispatchEvent(new Event('input',  {bubbles: true, composed: true}));
        el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    }
    reactSet(inputs[0], PASSWORD);
    reactSet(inputs[1], PASSWORD);
    // ToS checkbox
    var cb = document.querySelector('input[type="checkbox"]');
    if (cb && !cb.checked) {
        cb.click();
        cb.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return 'OK:fields=' + inputs.length + ':tos=' + (cb ? cb.checked : 'not-found');
})()
"""

SUBMIT_JS = """
(function() {
    var btn = document.querySelector('button[type="submit"]');
    if (!btn) {
        var btns = Array.from(document.querySelectorAll('button'));
        btn = btns.find(function(b) { return b.innerText.toLowerCase().includes('sign up'); });
    }
    if (!btn) return 'NO_BUTTON';
    btn.click();
    return 'CLICKED:' + btn.innerText.trim();
})()
"""


def evaluate(cdp, session_id, js, timeout=20):
    r = cdp.send("Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
        "awaitPromise": False,
    }, session_id=session_id, timeout=timeout)
    if not r:
        return None
    val = r.get("result", {}).get("result", {}).get("value")
    err = r.get("result", {}).get("exceptionDetails")
    if err:
        print(f"  JS exception: {err}", flush=True)
    return val


def wait_for_password_fields(cdp, session_id, max_wait=30):
    """Poll until 2 password fields appear on the page."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        count = evaluate(cdp, session_id,
            "document.querySelectorAll('input[type=\"password\"]').length")
        print(f"  Password fields: {count}", flush=True)
        if count and int(count) >= 2:
            return True
        # Also check URL
        url = evaluate(cdp, session_id, "window.location.href")
        print(f"  Current URL: {url}", flush=True)
        time.sleep(2)
    return False


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def main():
    pass_file = "/tmp/.dia-pass"
    if not os.path.exists(pass_file):
        print("ERROR: /tmp/.dia-pass not found", flush=True)
        sys.exit(1)
    password = open(pass_file).read().strip()

    # --- Browser WS ---
    print("Getting browser WS URL...", flush=True)
    browser_ws_url = None
    for attempt in range(10):
        try:
            browser_ws_url = get_browser_ws_url()
            if browser_ws_url:
                break
        except Exception as e:
            print(f"  /json/version attempt {attempt+1}: {e}", flush=True)
        time.sleep(2)
    if not browser_ws_url:
        print("ERROR: Could not get browser WS URL", flush=True)
        sys.exit(1)
    print(f"Browser WS: {browser_ws_url}", flush=True)

    cdp = BrowserCDP(browser_ws_url)
    try:
        # --- Create a new inspectable tab ---
        print("Creating new inspectable tab...", flush=True)
        r = cdp.send("Target.createTarget", {"url": "about:blank"})
        if not r:
            print("ERROR: Target.createTarget timed out", flush=True)
            sys.exit(1)
        target_id = r.get("result", {}).get("targetId")
        print(f"New target ID: {target_id}", flush=True)
        if not target_id:
            print(f"ERROR: no targetId in response: {r}", flush=True)
            sys.exit(1)

        # --- Attach to the new target ---
        print("Attaching to new target...", flush=True)
        r = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        if not r:
            print("ERROR: attachToTarget timed out", flush=True)
            sys.exit(1)
        session_id = r.get("result", {}).get("sessionId")
        print(f"Session: {session_id}", flush=True)
        if not session_id:
            print(f"ERROR: no sessionId in response: {r}", flush=True)
            sys.exit(1)

        # Drain any queued events
        cdp.drain_events(1)

        # --- Enable Page domain ---
        cdp.send("Page.enable", session_id=session_id, timeout=10)
        cdp.drain_events(0.5)

        # --- Navigate to signup URL ---
        print(f"Navigating to {SIGNUP_URL}...", flush=True)
        r = cdp.send("Page.navigate", {"url": SIGNUP_URL},
                     session_id=session_id, timeout=30)
        print(f"Navigate result: {r}", flush=True)
        cdp.drain_events(1)

        # --- Wait for page to load and signup form to appear ---
        print("Waiting for signup form...", flush=True)
        time.sleep(3)

        # Check current URL — may have redirected
        url = evaluate(cdp, session_id, "window.location.href")
        print(f"Page URL after navigate: {url}", flush=True)

        if not wait_for_password_fields(cdp, session_id, max_wait=20):
            # Try /signin as fallback — may redirect to signup after session check
            print(f"No password fields at {SIGNUP_URL}, trying {SIGNIN_URL}...", flush=True)
            cdp.send("Page.navigate", {"url": SIGNIN_URL}, session_id=session_id, timeout=20)
            time.sleep(5)
            url = evaluate(cdp, session_id, "window.location.href")
            print(f"  URL after signin navigate: {url}", flush=True)
            if not wait_for_password_fields(cdp, session_id, max_wait=20):
                all_inputs = evaluate(cdp, session_id,
                    "JSON.stringify(Array.from(document.querySelectorAll('input')).map(function(e){return {type:e.type,name:e.name,id:e.id,placeholder:e.placeholder}}))")
                print(f"  All inputs: {all_inputs}", flush=True)
                print("ERROR: signup form not found", flush=True)
                sys.exit(1)

        # --- Fill form ---
        print("Filling form...", flush=True)
        fill_js = FILL_JS.replace("PASSWORD", json.dumps(password))
        r = evaluate(cdp, session_id, fill_js)
        print(f"Fill result: {r}", flush=True)
        if r and str(r).startswith("ERR"):
            print(f"ERROR: form fill failed: {r}", flush=True)
            sys.exit(1)
        time.sleep(0.5)

        # --- Submit ---
        print("Submitting...", flush=True)
        r = evaluate(cdp, session_id, SUBMIT_JS)
        print(f"Submit: {r}", flush=True)

        # Wait for post-submit
        time.sleep(5)
        url = evaluate(cdp, session_id, "window.location.href")
        print(f"Post-submit URL: {url}", flush=True)

        # Check traffic via mitmproxy for /signup/start response
        # (visible in workflow logs from mitmdump stdout)

    finally:
        cdp.close()


if __name__ == "__main__":
    main()

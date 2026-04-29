#!/usr/bin/env python3
"""
dia-cdp-signup.py — CDP Stage 2 form fill for Dia account provisioning.

Called after Stage 1 (email entry via osascript) has fired and Dia has
navigated to the "Create your Dia account" form. Fills password fields,
checks ToS, and clicks Submit via Chrome DevTools Protocol.

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
SIGNUP_URL_PATTERN = "account.diabrowser.engineering"


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def get_targets():
    url = f"http://localhost:{CDP_PORT}/json"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def find_signup_target(targets):
    """Find the Dia signup webview target."""
    for t in targets:
        if SIGNUP_URL_PATTERN in t.get("url", ""):
            return t
    # Fallback: first page target that is not devtools
    for t in targets:
        if t.get("type") == "page" and "devtools" not in t.get("url", ""):
            return t
    return None


def cdp_evaluate(ws, expression, timeout=15):
    """Send Runtime.evaluate and return the result value."""
    msg_id = int(time.time() * 1000) % 100000
    ws.send(json.dumps({
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        }
    }))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.settimeout(deadline - time.time())
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            result = msg.get("result", {}).get("result", {})
            err = msg.get("result", {}).get("exceptionDetails")
            if err:
                print(f"  JS exception: {err}", flush=True)
            return result.get("value")
    raise TimeoutError(f"CDP evaluate timed out for: {expression[:80]}")


def fill_react_input(ws, selector, value):
    """Approach A — React native prototype setter + input event."""
    js = """
(function() {
    var el = document.querySelector('SELECTOR');
    if (!el) return 'NOT_FOUND';
    var nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    nativeSetter.call(el, VALUE);
    el.dispatchEvent(new Event('input',  {bubbles: true, composed: true}));
    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    return 'OK:' + el.name + ':' + el.type;
})()
""".replace("SELECTOR", selector).replace("VALUE", json.dumps(value))
    return cdp_evaluate(ws, js)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main():
    pass_file = "/tmp/.dia-pass"
    if not os.path.exists(pass_file):
        print("ERROR: /tmp/.dia-pass not found", flush=True)
        sys.exit(1)
    password = open(pass_file).read().strip()

    # --- Discover CDP target (signup form should already be loaded by now) ---
    print("Discovering CDP targets...", flush=True)
    targets = None
    for attempt in range(15):  # up to 30s
        try:
            targets = get_targets()
            target = find_signup_target(targets)
            if target:
                break
            print(f"  attempt {attempt+1}: no signup target yet — URLs: {[t.get('url','') for t in targets]}", flush=True)
        except Exception as e:
            print(f"  attempt {attempt+1}: CDP poll error: {e}", flush=True)
        time.sleep(2)
    else:
        print("ERROR: Dia signup tab not found after 30s", flush=True)
        sys.exit(1)

    ws_url = target["webSocketDebuggerUrl"]
    print(f"Attaching: {target.get('url', 'unknown')}", flush=True)

    ws = websocket.create_connection(ws_url, timeout=15)
    try:
        # Wait for the form to fully render (React hydration)
        time.sleep(3)

        # Confirm password fields present before proceeding
        field_count = cdp_evaluate(ws, "document.querySelectorAll('input[type=\"password\"]').length")
        print(f"Password fields visible: {field_count}", flush=True)
        if not field_count or int(field_count) < 2:
            # One more wait — form may still be transitioning
            print("  Waiting 5s more for form to settle...", flush=True)
            time.sleep(5)
            field_count = cdp_evaluate(ws, "document.querySelectorAll('input[type=\"password\"]').length")
            print(f"  Password fields after wait: {field_count}", flush=True)
            if not field_count or int(field_count) < 2:
                print("ERROR: Expected 2 password fields, aborting", flush=True)
                sys.exit(1)

        # Step 1: Fill password
        r = fill_react_input(ws, 'input[type="password"]:first-of-type', password)
        print(f"Password fill: {r}", flush=True)
        time.sleep(0.5)

        # Step 2: Fill confirm password
        r = fill_react_input(ws, 'input[type="password"]:last-of-type', password)
        print(f"Confirm fill: {r}", flush=True)
        time.sleep(0.5)

        # Step 3: ToS checkbox
        tos_js = """
(function() {
    var cb = document.querySelector('input[type="checkbox"]');
    if (!cb) return 'NO_CHECKBOX';
    if (!cb.checked) {
        cb.click();
        cb.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return 'CHECKED:' + cb.checked;
})()
"""
        r = cdp_evaluate(ws, tos_js)
        print(f"ToS: {r}", flush=True)
        time.sleep(0.5)

        # Step 4: Submit
        submit_js = """
(function() {
    var btn = document.querySelector('button[type="submit"]');
    if (!btn) {
        var btns = Array.from(document.querySelectorAll('button'));
        btn = btns.find(b => b.innerText.toLowerCase().includes('sign up'));
    }
    if (!btn) return 'NO_BUTTON';
    btn.click();
    return 'CLICKED:' + btn.innerText.trim();
})()
"""
        r = cdp_evaluate(ws, submit_js)
        print(f"Submit: {r}", flush=True)

        # Wait for post-submit navigation
        time.sleep(5)

        url = cdp_evaluate(ws, "window.location.href")
        print(f"Post-submit URL: {url}", flush=True)

    finally:
        ws.close()


if __name__ == "__main__":
    main()

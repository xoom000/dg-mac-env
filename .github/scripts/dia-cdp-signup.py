#!/usr/bin/env python3
"""
dia-cdp-signup.py — CDP Stage 2 form fill for Dia account provisioning.

Called after Stage 1 (email entry via osascript) has fired and Dia has
navigated to the "Create your Dia account" form. Fills password fields,
checks ToS, and clicks Submit via Chrome DevTools Protocol.

Dia sandboxes page targets — /json returns []. We use the browser-level
WebSocket from /json/version + Target.getTargets + Target.attachToTarget
to reach the signup webview.

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
# Browser-level CDP helpers
# ---------------------------------------------------------------------------

def get_browser_ws_url():
    """Get the browser-level WebSocket URL from /json/version."""
    url = f"http://localhost:{CDP_PORT}/json/version"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data.get("webSocketDebuggerUrl") or data.get("Browser")


def get_all_targets_via_browser_ws(browser_ws_url):
    """
    Connect to the browser-level WS and call Target.getTargets.
    Returns list of target dicts. Closes the connection immediately.
    """
    ws = websocket.create_connection(browser_ws_url, timeout=15)
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        deadline = time.time() + 15
        while time.time() < deadline:
            ws.settimeout(deadline - time.time())
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg.get("result", {}).get("targetInfos", [])
    finally:
        ws.close()
    return []


def attach_to_target(browser_ws_url, target_id):
    """
    Attach to a target via browser-level WS using Target.attachToTarget.
    Returns (ws, session_id) — caller must close ws.
    """
    ws = websocket.create_connection(browser_ws_url, timeout=15)
    ws.send(json.dumps({
        "id": 2,
        "method": "Target.attachToTarget",
        "params": {"targetId": target_id, "flatten": True}
    }))
    deadline = time.time() + 15
    while time.time() < deadline:
        ws.settimeout(deadline - time.time())
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        msg = json.loads(raw)
        if msg.get("id") == 2:
            session_id = msg.get("result", {}).get("sessionId")
            if session_id:
                return ws, session_id
    ws.close()
    return None, None


# ---------------------------------------------------------------------------
# Session-scoped CDP evaluate
# ---------------------------------------------------------------------------

def cdp_evaluate_session(ws, session_id, expression, timeout=15):
    """
    Send Runtime.evaluate via a session (for targets attached through
    the browser-level WS). Session messages require the sessionId field.
    """
    msg_id = int(time.time() * 1000) % 100000
    ws.send(json.dumps({
        "id": msg_id,
        "sessionId": session_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        }
    }))
    deadline = time.time() + timeout
    while time.time() < deadline:
        ws.settimeout(deadline - time.time())
        try:
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
    raise TimeoutError(f"CDP session evaluate timed out: {expression[:80]}")


def fill_react_input(ws, session_id, selector, value):
    """Approach A — React native prototype setter + input + change events."""
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
    return cdp_evaluate_session(ws, session_id, js)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main():
    pass_file = "/tmp/.dia-pass"
    if not os.path.exists(pass_file):
        print("ERROR: /tmp/.dia-pass not found", flush=True)
        sys.exit(1)
    password = open(pass_file).read().strip()

    # --- Get browser-level WS URL ---
    print("Getting browser WS URL from /json/version...", flush=True)
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
        print("ERROR: Could not get browser WS URL from /json/version", flush=True)
        sys.exit(1)
    print(f"Browser WS: {browser_ws_url}", flush=True)

    # --- Discover signup target via Target.getTargets ---
    print("Discovering targets via Target.getTargets...", flush=True)
    target_id = None
    for attempt in range(20):  # up to 40s
        try:
            targets = get_all_targets_via_browser_ws(browser_ws_url)
            print(f"  attempt {attempt+1}: {len(targets)} targets — {[(t.get('type','?'), t.get('url','')[:60]) for t in targets]}", flush=True)
            for t in targets:
                if SIGNUP_URL_PATTERN in t.get("url", ""):
                    target_id = t["targetId"]
                    print(f"  Found signup target: {t.get('url')}", flush=True)
                    break
            if not target_id:
                # Fallback: any page/webview that is not devtools/extension
                for t in targets:
                    url = t.get("url", "")
                    ttype = t.get("type", "")
                    if ttype in ("page", "webview", "iframe") and "devtools" not in url and "extension" not in url and url:
                        target_id = t["targetId"]
                        print(f"  Fallback target: {ttype} {url[:60]}", flush=True)
                        break
            if target_id:
                break
        except Exception as e:
            print(f"  attempt {attempt+1}: error: {e}", flush=True)
        time.sleep(2)

    if not target_id:
        print("ERROR: No suitable target found after 40s", flush=True)
        sys.exit(1)

    # --- Attach to target ---
    print(f"Attaching to target {target_id}...", flush=True)
    ws, session_id = attach_to_target(browser_ws_url, target_id)
    if not ws or not session_id:
        print("ERROR: Failed to attach to target", flush=True)
        sys.exit(1)
    print(f"Session: {session_id}", flush=True)

    try:
        # Wait for React hydration
        time.sleep(3)

        # Confirm password fields present
        field_count = cdp_evaluate_session(ws, session_id,
            "document.querySelectorAll('input[type=\"password\"]').length")
        print(f"Password fields visible: {field_count}", flush=True)
        if not field_count or int(field_count) < 2:
            print("  Waiting 5s more for form...", flush=True)
            time.sleep(5)
            field_count = cdp_evaluate_session(ws, session_id,
                "document.querySelectorAll('input[type=\"password\"]').length")
            print(f"  Password fields after wait: {field_count}", flush=True)
            if not field_count or int(field_count) < 2:
                # Dump all inputs for diagnosis
                all_inputs = cdp_evaluate_session(ws, session_id,
                    "JSON.stringify(Array.from(document.querySelectorAll('input')).map(e=>({type:e.type,name:e.name,id:e.id,placeholder:e.placeholder})))")
                print(f"  All inputs on page: {all_inputs}", flush=True)
                print("ERROR: Expected 2 password fields, aborting", flush=True)
                sys.exit(1)

        # Step 1: Fill password
        r = fill_react_input(ws, session_id, 'input[type="password"]:first-of-type', password)
        print(f"Password fill: {r}", flush=True)
        time.sleep(0.5)

        # Step 2: Fill confirm password
        r = fill_react_input(ws, session_id, 'input[type="password"]:last-of-type', password)
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
        r = cdp_evaluate_session(ws, session_id, tos_js)
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
        r = cdp_evaluate_session(ws, session_id, submit_js)
        print(f"Submit: {r}", flush=True)

        # Wait for post-submit navigation
        time.sleep(5)

        url = cdp_evaluate_session(ws, session_id, "window.location.href")
        print(f"Post-submit URL: {url}", flush=True)

    finally:
        ws.close()


if __name__ == "__main__":
    main()

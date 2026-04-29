#!/usr/bin/env python3
"""
dia-cdp-extract.py — CDP session state extractor for Dia post-login.

Strategy:
  - The Dia auth webview (login/signup forms) is sandboxed, not a CDP target.
  - After successful login, Dia transitions to its main browser UI — THAT tab IS inspectable.
  - This script waits for a non-auth CDP target to appear, then extracts:
      (a) All cookies via browser-level Network.getCookies
      (b) localStorage + sessionStorage via Runtime.evaluate per target
      (c) IndexedDB database names (best-effort) via IndexedDB.requestDatabaseNames
  - Also merges with mitmproxy-captured cookies from /tmp/dia-session/storage_state.json
    if it exists (written by dia-session-capture.py addon).
  - Writes:
      /tmp/dia-session/storage_state.json  — Playwright format
      /tmp/dia-session/auth-tokens.json    — raw Set-Cookie values + extracted tokens

Usage:
  python3 dia-cdp-extract.py
  (Run after Dia login completes — waits up to 120s for CDP targets)

Dependencies:
  pip install websocket-client
"""

import json
import os
import sys
import time
import urllib.request

import websocket

CDP_PORT = 9222
SESSION_DIR = "/tmp/dia-session"
SESSION_FILE = f"{SESSION_DIR}/storage_state.json"
AUTH_TOKENS_FILE = f"{SESSION_DIR}/auth-tokens.json"
TARGET_HOST = "account.diabrowser.engineering"

# URLs to skip when looking for main app targets
SKIP_URL_PATTERNS = [
    "devtools://",
    "chrome-extension://",
    "about:",
    "data:",
    "chrome://",
    "account.diabrowser",  # auth webview — not inspectable, skip
]

os.makedirs(SESSION_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def get_json(path, timeout=5):
    url = f"http://localhost:{CDP_PORT}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def get_browser_ws():
    data = get_json("/json/version")
    return data.get("webSocketDebuggerUrl")


class CDP:
    def __init__(self, ws_url, timeout=20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 1

    def send(self, method, params=None, session_id=None, timeout=20):
        mid = self._id
        self._id += 1
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            resp = json.loads(raw)
            if resp.get("id") == mid:
                return resp
        return None

    def drain(self, secs=0.5):
        self.ws.settimeout(secs)
        while True:
            try:
                self.ws.recv()
            except (websocket.WebSocketTimeoutException,
                    websocket.WebSocketConnectionClosedException):
                break

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def evaluate(cdp, session_id, js, timeout=15):
    r = cdp.send("Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
        "awaitPromise": False,
    }, session_id=session_id, timeout=timeout)
    if not r:
        return None
    err = r.get("result", {}).get("exceptionDetails")
    if err:
        print(f"  JS error: {err}", flush=True)
        return None
    return r.get("result", {}).get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Target enumeration — wait for main browser tab to appear
# ---------------------------------------------------------------------------

def get_targets():
    """Try /json/list first, fall back to /json."""
    for path in ("/json/list", "/json"):
        try:
            data = get_json(path)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def is_main_app_target(t):
    url = t.get("url", "")
    title = t.get("title", "")
    t_type = t.get("type", "")
    ws_url = t.get("webSocketDebuggerUrl", "")
    if not ws_url:
        return False
    if t_type not in ("page", "iframe", "worker", "background_page"):
        return False
    for pattern in SKIP_URL_PATTERNS:
        if pattern in url:
            return False
    return True


def wait_for_main_app_target(max_wait=120):
    """Poll CDP /json until a post-login inspectable target appears."""
    print(f"Waiting for main browser tab (up to {max_wait}s)...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        targets = get_targets()
        print(f"  CDP targets: {len(targets)} total", flush=True)
        for t in targets:
            print(f"    type={t.get('type')} url={t.get('url','')[:60]}", flush=True)
        candidates = [t for t in targets if is_main_app_target(t)]
        if candidates:
            print(f"  Main app target(s) found: {len(candidates)}", flush=True)
            return candidates
        time.sleep(3)
    print("Timeout waiting for main app target.", flush=True)
    return []


# ---------------------------------------------------------------------------
# Session extraction
# ---------------------------------------------------------------------------

def extract_target_storage(cdp, target):
    """Connect to a target and extract cookies, localStorage, sessionStorage."""
    ws_url = target.get("webSocketDebuggerUrl")
    target_url = target.get("url", "")
    print(f"\nExtracting from target: {target_url[:80]}", flush=True)

    result = {
        "url": target_url,
        "cookies_js": [],
        "local_storage": [],
        "session_storage": [],
        "idb_databases": [],
    }

    try:
        t_cdp = CDP(ws_url, timeout=15)
    except Exception as e:
        print(f"  Connect failed: {e}", flush=True)
        return result

    try:
        # document.cookie
        cookies_raw = evaluate(t_cdp, None, "document.cookie")
        if cookies_raw:
            result["cookies_js"] = [
                {"name": p.split("=", 1)[0].strip(), "value": p.split("=", 1)[1].strip() if "=" in p else ""}
                for p in cookies_raw.split(";") if p.strip()
            ]
            print(f"  document.cookie: {len(result['cookies_js'])} entries", flush=True)

        # localStorage
        ls_raw = evaluate(t_cdp, None, "JSON.stringify(Object.entries(localStorage))")
        if ls_raw:
            try:
                entries = json.loads(ls_raw)
                result["local_storage"] = [{"name": k, "value": v} for k, v in entries]
                print(f"  localStorage: {len(result['local_storage'])} keys", flush=True)
            except Exception as e:
                print(f"  localStorage parse error: {e}", flush=True)

        # sessionStorage
        ss_raw = evaluate(t_cdp, None, "JSON.stringify(Object.entries(sessionStorage))")
        if ss_raw:
            try:
                entries = json.loads(ss_raw)
                result["session_storage"] = [{"name": k, "value": v} for k, v in entries]
                print(f"  sessionStorage: {len(result['session_storage'])} keys", flush=True)
            except Exception as e:
                print(f"  sessionStorage parse error: {e}", flush=True)

        # IndexedDB databases (best-effort — requires Storage.getUsageAndQuota to be enabled)
        idb_names_raw = evaluate(t_cdp, None, """
(function() {
    if (!window.indexedDB) return '[]';
    return new Promise(function(resolve) {
        try {
            var req = window.indexedDB.databases ? window.indexedDB.databases() : null;
            if (!req) { resolve('[]'); return; }
            req.then(function(dbs) {
                resolve(JSON.stringify(dbs.map(function(d){ return {name: d.name, version: d.version}; })));
            }).catch(function(){ resolve('[]'); });
        } catch(e) { resolve('[]'); }
    });
})()
        """)
        if idb_names_raw and idb_names_raw != "[]":
            try:
                result["idb_databases"] = json.loads(idb_names_raw)
                print(f"  IndexedDB: {result['idb_databases']}", flush=True)
            except Exception:
                pass

    finally:
        t_cdp.close()

    return result


def extract_browser_cookies(cdp):
    """Use browser-level Network.getCookies — returns ALL cookies for all domains."""
    print("\nExtracting browser-level cookies via Network.getCookies...", flush=True)
    cdp.send("Network.enable", timeout=10)
    r = cdp.send("Network.getCookies", {}, timeout=20)
    if not r:
        return []
    raw_cookies = r.get("result", {}).get("cookies", [])
    print(f"  Browser cookies: {len(raw_cookies)}", flush=True)
    # Filter to our target domain
    relevant = [c for c in raw_cookies if TARGET_HOST in c.get("domain", "")]
    print(f"  Relevant ({TARGET_HOST}): {len(relevant)}", flush=True)
    return raw_cookies, relevant


# ---------------------------------------------------------------------------
# Merge with mitmproxy-captured state
# ---------------------------------------------------------------------------

def load_mitm_state():
    """Load cookies already captured by dia-session-capture.py addon."""
    if not os.path.exists(SESSION_FILE):
        return [], []
    try:
        with open(SESSION_FILE) as f:
            state = json.load(f)
        mitm_cookies = state.get("cookies", [])
        print(f"Loaded {len(mitm_cookies)} cookies from mitmproxy capture", flush=True)
        return mitm_cookies, state.get("origins", [])
    except Exception as e:
        print(f"Could not load mitmproxy state: {e}", flush=True)
        return [], []


# ---------------------------------------------------------------------------
# Write output files
# ---------------------------------------------------------------------------

def write_storage_state(all_cookies, origins):
    state = {
        "cookies": all_cookies,
        "origins": origins,
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nWrote {SESSION_FILE} ({len(all_cookies)} cookies, {len(origins)} origins)", flush=True)


def write_auth_tokens(browser_cookies, mitm_cookies, target_results):
    tokens = {
        "browser_cookies_all": browser_cookies,
        "mitm_cookies": mitm_cookies,
        "target_extracts": target_results,
    }
    with open(AUTH_TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"Wrote {AUTH_TOKENS_FILE}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Load existing mitmproxy-captured cookies (may be empty if no Set-Cookie yet)
    mitm_cookies, mitm_origins = load_mitm_state()

    # 2. Get browser-level CDP WS
    print("Connecting to browser-level CDP...", flush=True)
    browser_ws = None
    for attempt in range(15):
        try:
            browser_ws = get_browser_ws()
            if browser_ws:
                break
        except Exception as e:
            print(f"  /json/version attempt {attempt+1}: {e}", flush=True)
        time.sleep(2)
    if not browser_ws:
        print("ERROR: Could not get browser WS", flush=True)
        sys.exit(1)
    print(f"Browser WS: {browser_ws}", flush=True)

    cdp = CDP(browser_ws)

    # 3. Extract browser-level cookies (all domains)
    try:
        all_browser_cookies, relevant_cookies = extract_browser_cookies(cdp)
    except Exception as e:
        print(f"Browser cookie extract failed: {e}", flush=True)
        all_browser_cookies = []
        relevant_cookies = []

    # 4. Wait for main app target to appear (post-login Dia window)
    targets = wait_for_main_app_target(max_wait=120)

    # 5. Extract per-target storage
    target_results = []
    all_ls_entries = []
    for t in targets:
        r = extract_target_storage(cdp, t)
        target_results.append(r)
        all_ls_entries.extend(r.get("local_storage", []))

    cdp.close()

    # 6. Merge cookies: mitmproxy-captured + browser-level CDP
    # Build dedup dict keyed by (name, domain)
    merged = {}
    for c in mitm_cookies:
        merged[(c["name"], c.get("domain", TARGET_HOST))] = c
    # CDP browser cookies override (more complete — include expires, etc.)
    for c in all_browser_cookies:
        key = (c.get("name"), c.get("domain", ""))
        # Convert CDP cookie format to Playwright format
        pw_cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax"),
        }
        merged[key] = pw_cookie
    final_cookies = list(merged.values())
    print(f"\nMerged cookie count: {len(final_cookies)}", flush=True)

    # 7. Build origins with localStorage
    origins = [
        {
            "origin": f"https://{TARGET_HOST}",
            "localStorage": [ls for ls in all_ls_entries],
        }
    ]
    # Also include mitmproxy origins that have localStorage (may have been populated)
    for origin in mitm_origins:
        if origin.get("origin", "") != f"https://{TARGET_HOST}" and origin.get("localStorage"):
            origins.append(origin)

    # 8. Write output files
    write_storage_state(final_cookies, origins)
    write_auth_tokens(all_browser_cookies, mitm_cookies, target_results)

    print("\nExtraction complete.", flush=True)
    print(f"  storage_state.json: {len(final_cookies)} cookies, {len(origins)} origins", flush=True)
    print(f"  auth-tokens.json: written", flush=True)


if __name__ == "__main__":
    main()

"""Split Sheet — hosted.

One user, one password, two official connections (Oura and Strava). Syncs
itself whenever you open it and the data has gone stale, so there is no cron
job to keep alive and nothing to remember to press.

Runs on Python's standard library plus psycopg for Postgres. Start with:
    python3 app.py
"""

import datetime as dt
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import socketserver
import threading
import traceback
import urllib.parse

import coach
import providers
import store

HERE = os.path.dirname(os.path.abspath(__file__))
def _env(key, default=""):
    """Read an env var and strip stray whitespace/newlines from copy-paste."""
    return os.environ.get(key, default).strip()


PORT = int(_env("PORT", "8080"))
HISTORY_DAYS = 240
STALE_MINUTES = 90

APP_PASSWORD = _env("APP_PASSWORD")
SECRET = _env("SECRET_KEY") or hashlib.sha256(
    (APP_PASSWORD or "dev").encode()).hexdigest()
BASE_URL = _env("BASE_URL", f"http://localhost:{PORT}").rstrip("/")

CFG = {
    "oura": (_env("OURA_CLIENT_ID"), _env("OURA_CLIENT_SECRET")),
    "strava": (_env("STRAVA_CLIENT_ID"), _env("STRAVA_CLIENT_SECRET")),
}
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

_sync_lock = threading.Lock()
_states = {}


def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(name, default):
    path = os.path.join(HERE, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


# ------------------------------------------------------------------- sessions

def make_cookie():
    raw = secrets.token_urlsafe(24)
    sig = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]
    return raw + "." + sig


def valid_cookie(val):
    if not val or "." not in val:
        return False
    raw, sig = val.rsplit(".", 1)
    want = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, want)


# ----------------------------------------------------------------------- sync

def window():
    end = dt.date.today()
    return (end - dt.timedelta(days=HISTORY_DAYS)).isoformat(), end.isoformat()


def is_stale():
    d = store.get("data")
    if not d:
        return True
    ts = d.get("stats", {}).get("synced_at")
    if not ts:
        return True
    try:
        age = dt.datetime.utcnow() - dt.datetime.strptime(ts, "%Y-%m-%d %H:%M")
        return age.total_seconds() > STALE_MINUTES * 60
    except ValueError:
        return True


def do_sync():
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    try:
        start, end = window()
        report = {}

        runs, acts = {}, []
        try:
            tok = providers.token("strava", *CFG["strava"])
            runs, acts = providers.fetch_strava(tok, start, end)
            report["strava"] = f"{len(acts)} runs over {len(runs)} days"
        except Exception as e:
            report["strava"] = f"failed: {e}"

        oura, sleep_c, ready_c = {}, {}, {}
        try:
            tok = providers.token("oura", *CFG["oura"])
            oura, sleep_c, ready_c = providers.fetch_oura(tok, start, end)
            report["oura"] = f"{len(oura)} days"
        except Exception as e:
            report["oura"] = f"failed: {e}"

        if runs or oura:
            payload = providers.build(runs, oura, load_json("planned_miles.json", {}),
                                      start, end, acts, sleep_c, ready_c)
            store.put("data", payload)
            report["ok"] = True
        else:
            report["ok"] = False
        return report
    finally:
        _sync_lock.release()


def sync_in_background():
    threading.Thread(target=lambda: _safe(do_sync), daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()


# --------------------------------------------------------------------- server

LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Split Sheet</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0A0D14;color:#EDF0F6;font-family:system-ui,sans-serif}
form{width:min(320px,86vw);text-align:center}
h1{font-size:22px;margin:0 0 6px}p{color:#8A93A8;font-size:13.5px;margin:0 0 22px}
input{width:100%;padding:14px 16px;border-radius:12px;border:1px solid #262E40;
background:#141926;color:#EDF0F6;font-size:16px;margin-bottom:10px}
button{width:100%;padding:14px;border-radius:12px;border:0;background:#EDF0F6;
color:#0A0D14;font-size:15px;font-weight:600;cursor:pointer}
.err{color:#F2707E;font-size:13px;margin-top:14px}
</style>
<form method=post action=/login>
<h1>Split Sheet</h1><p>__SUB__</p>
<input type=password name=password placeholder="Password" autofocus required>
<button>Open</button>__ERR__</form>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", cookie=None):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, to, cookie=None):
        self.send_response(302)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _authed(self):
        if not APP_PASSWORD:
            return True
        raw = self.headers.get("Cookie", "")
        try:
            c = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return False
        return "ss" in c and valid_cookie(c["ss"].value)

    def _login(self, err=""):
        page = (LOGIN_PAGE
                .replace("__SUB__", "Your training, synced.")
                .replace("__ERR__", f'<div class="err">{err}</div>' if err else ""))
        self._send(401 if err else 200, page, "text/html")

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)

        if path == "/healthz":
            return self._send(200, json.dumps({"ok": True}))

        if not self._authed():
            if path.startswith("/api/"):
                return self._send(401, json.dumps({"error": "Sign in first."}))
            return self._login()

        if path == "/":
            if is_stale() and (providers.connected("oura") or providers.connected("strava")):
                sync_in_background()
            self.path = "/dashboard.html"
            return super().do_GET()

        if path == "/api/data":
            d = store.get("data")
            if not d:
                return self._send(404, json.dumps({"error": "Not synced yet."}))
            return self._send(200, json.dumps(d))

        if path == "/api/plan":
            return self._send(200, json.dumps(load_json("plan.json", {})))

        if path == "/api/status":
            d = store.get("data") or {}
            return self._send(200, json.dumps({
                "oura": providers.connected("oura"),
                "strava": providers.connected("strava"),
                "garmin": providers.connected("strava"),
                "last": (d.get("stats") or {}).get("synced_at"),
                "syncing": _sync_lock.locked(),
                "has_data": bool(d),
                "configured": dict({k: bool(v[0] and v[1]) for k, v in CFG.items()},
                                    anthropic=bool(ANTHROPIC_API_KEY))}))

        if path == "/api/sync":
            return self._send(200, json.dumps(do_sync()))

        if path == "/api/coach/history":
            return self._send(200, json.dumps({"messages": coach.history()}))

        if path.startswith("/connect/"):
            p = path.split("/")[-1]
            if p not in CFG:
                return self._send(404, "Unknown service.", "text/plain")
            cid, csec = CFG[p]
            if not (cid and csec):
                return self._send(400,
                    f"{p.title()} keys are missing. Add {p.upper()}_CLIENT_ID and "
                    f"{p.upper()}_CLIENT_SECRET in your host's environment settings.",
                    "text/plain")
            st = secrets.token_urlsafe(16)
            _states[st] = p
            return self._redirect(providers.authorize_url(
                p, cid, f"{BASE_URL}/oauth/{p}", st))

        if path.startswith("/oauth/"):
            p = path.split("/")[-1]
            st = (q.get("state") or [""])[0]
            if _states.pop(st, None) != p:
                return self._send(400, "That link expired. Start again from the app.",
                                  "text/plain")
            code = (q.get("code") or [""])[0]
            if not code:
                return self._send(400,
                    "Authorisation was declined: " + (q.get("error") or ["no code"])[0],
                    "text/plain")
            try:
                providers.exchange(p, code, *CFG[p], f"{BASE_URL}/oauth/{p}")
            except Exception as e:
                return self._send(500, f"Could not complete sign-in: {e}", "text/plain")
            sync_in_background()
            return self._redirect("/?connected=" + p)

        if path.startswith("/disconnect/"):
            providers.disconnect(path.split("/")[-1])
            return self._redirect("/")

        if path in ("/store.py", "/providers.py", "/app.py", "/coach.py", "/.env"):
            return self._send(403, "No.", "text/plain")

        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            given = (form.get("password") or [""])[0]
            if APP_PASSWORD and hmac.compare_digest(given, APP_PASSWORD):
                return self._redirect("/", f"ss={make_cookie()}; Path=/; HttpOnly; "
                                           "SameSite=Lax; Max-Age=7776000"
                                           + ("; Secure" if BASE_URL.startswith("https") else ""))
            return self._login("Wrong password.")

        if not self._authed():
            return self._send(401, json.dumps({"error": "Sign in first."}))

        if path == "/api/coach":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}")
            except ValueError:
                return self._send(400, json.dumps({"error": "Bad request."}))
            message = (body.get("message") or "").strip()[:2000]
            view = (body.get("view") or "").strip()[:20]
            if not message:
                return self._send(400, json.dumps({"error": "Empty message."}))
            try:
                text = coach.reply(message, ANTHROPIC_API_KEY,
                                   store.get("data"), load_json("plan.json", {}),
                                   view=view)
                return self._send(200, json.dumps({"reply": text}))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))

        return self._send(404, json.dumps({"error": "Not found."}))

    def do_DELETE(self):
        if not self._authed():
            return self._send(401, json.dumps({"error": "Sign in first."}))
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/coach/history":
            coach.clear_history()
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"error": "Not found."}))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    load_env()
    backend = store.init()
    print(f"\n  Split Sheet on port {PORT}  (store: {backend})")
    if not APP_PASSWORD:
        print("  ! APP_PASSWORD is not set — anyone with the link can read your data.")
    for p in CFG:
        state = "connected" if providers.connected(p) else "not connected"
        print(f"  {p:8} {state}")
    print(f"  {'coach':8} {'configured' if ANTHROPIC_API_KEY else 'no ANTHROPIC_API_KEY set'}")
    print()
    with Server(("0.0.0.0", PORT), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()

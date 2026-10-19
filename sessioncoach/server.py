"""Local dashboard server: binds 127.0.0.1 only. Reads open the database read-only; the refresh
endpoint (POST, same-origin only) runs an incremental ingest.

Loopback is shared by every user of the machine, so nothing is served without a session cookie. The cookie is
handed out once the browser shows the per-start secret that `serve()` puts in the URL it opens and prints
(`/?k=<secret>`), as Jupyter does. The secret stays valid while the process lives, so the printed link can be
reopened in another tab or browser."""
import datetime as dt
import hmac
import html as html_mod
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analysis, collect, cost, paths, store
from . import settings as settings_mod
from .projects import Labeler

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
REFRESH_LOCK = threading.Lock()
MAX_BODY = 64 * 1024
MAX_DAYS = 3650  # the longest window an API call asks for (ten years)
TOKEN_PLACEHOLDER = "__KIT_TOKEN__"
LOGIN_PAGE = """<!doctype html><html lang="%s"><head><meta charset="utf-8"><title>sessioncoach</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em">
<h1>sessioncoach</h1>
<p>%s</p>
</body></html>
"""
LOGIN_TEXT = {"en": "Open the link that <code>%s</code> printed in the terminal (it carries a one-time secret: <code>/?k=...</code>).",
              "es": "Abre el enlace que <code>%s</code> imprimi\u00f3 en la terminal (lleva un secreto: <code>/?k=...</code>)."}


def _same(got, want):
    """Constant-time comparison of two strings (bytes, so non-ASCII input is just a mismatch)."""
    return hmac.compare_digest((got or "").encode("utf-8"), want.encode("utf-8"))


def _ro(db):
    return store.open_readonly(db)


def _query_days(q):
    """`days` from the query (default 30; 0 = all time). ValueError when it is not a non-negative integer."""
    days = int(q.get("days", ["30"])[0] or 0)
    if days < 0:
        raise ValueError("days must be >= 0")
    return days


def _lang(q, headers):
    """"es" or "en": the `lang` parameter, else the browser's first Accept-Language, else English."""
    asked = (q.get("lang") or [""])[0] or (headers.get("Accept-Language") or "").split(",")[0]
    return "es" if asked.strip().lower()[:2] == "es" else "en"


def _query_date(q):
    """`date` from the query as a date, or None. ValueError when it is not YYYY-MM-DD."""
    date = (q.get("date") or [None])[0]
    return dt.date.fromisoformat(date) if date else None


def _known_dir_names(db):
    """Encoded working-dir names the database knows (read-only); settings resolves them to real directories."""
    con = _ro(db)
    try:
        names = {r[0] for r in con.execute("SELECT DISTINCT dir FROM session_dirs")}
        names |= {r[0] for r in con.execute("SELECT DISTINCT project_dir FROM cost_slices")}
    finally:
        con.close()
    return sorted(n for n in names if n)


def make_handler(db, token=None, settings=None, demo=False, key=None):
    """`token`: per-start secret embedded in the HTML and required (X-Kit-Token) on settings and refresh.

    `key`: per-start secret of the link `serve()` opens (`/?k=<key>`); showing it gets the session cookie that
    every page and API call needs. The cookie's value (`Handler.kit_session`) is another random value.

    `demo`: the database is `sessioncoach demo`'s synthetic one. The page says so, and the two endpoints that reach outside
    it (refresh reads the real transcripts, settings writes the real ~/.claude) are switched off.
    """
    token = token or secrets.token_urlsafe(32)
    key = key or secrets.token_urlsafe(32)
    session = secrets.token_urlsafe(32)
    settings = settings or settings_mod.Settings(projects=lambda: _known_dir_names(db), cwd=os.getcwd())

    class Handler(BaseHTTPRequestHandler):
        server_version = "sessioncoach"
        kit_token = token
        kit_settings = settings
        kit_demo = demo
        kit_key = key
        kit_session = session

        def log_message(self, *args):  # keep the terminal quiet
            return

        def _send(self, code, body, ctype, headers=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; "
                             "frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, data, anon=None):
            if anon is not None:
                data = anon.apply(data)
                data["anonymous"] = True
            return self._send(200, json.dumps(data), "application/json")

        # ---- request guards (DNS rebinding, CSRF, token) ----
        def _strict_host_ok(self):
            """Host must be loopback AND carry this server's port (DNS-rebinding guard)."""
            host = (self.headers.get("Host") or "")
            name, _, port = host.rpartition(":") if ":" in host else (host, "", "80")
            return name in ALLOWED_HOSTS and port == str(self.server.server_address[1])

        def _origin_ok(self, required):
            origin = self.headers.get("Origin")
            if origin is None:
                return not required
            u = urllib.parse.urlparse(origin)
            try:
                port = u.port
            except ValueError:
                return False
            return u.scheme == "http" and u.hostname in ALLOWED_HOSTS and port == self.server.server_address[1]

        def _token_ok(self):
            got = self.headers.get("X-Kit-Token") or ""
            return hmac.compare_digest(got.encode("utf-8"), self.kit_token.encode("utf-8"))

        def _cookie_name(self):
            # cookies ignore the port: one name per port keeps the dashboard and the demo apart
            return "sessioncoach_%d" % self.server.server_address[1]

        def _session_ok(self):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except CookieError:
                return False
            morsel = jar.get(self._cookie_name())
            return morsel is not None and _same(morsel.value, self.kit_session)

        def _login(self, url, q):
            """`/?k=<key>`: the right key sets the session cookie and redirects to the same page without it."""
            if not _same(q["k"][0], self.kit_key):
                return self._unauthorized("/", q)
            rest = urllib.parse.urlencode([(k, v) for k, vs in q.items() if k != "k" for v in vs])
            cookie = "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (self._cookie_name(), self.kit_session)
            return self._send(303, "", "text/plain", {"Location": url.path + ("?" + rest if rest else ""), "Set-Cookie": cookie})

        def _unauthorized(self, path, q=None):
            if path.startswith("/api/"):
                return self._err(401, "open the link printed by the sessioncoach command")
            cmd = html_mod.escape("sessioncoach demo" if self.kit_demo else "sessioncoach dashboard")
            lang = _lang(q or {}, self.headers)
            return self._send(401, LOGIN_PAGE % (lang, LOGIN_TEXT[lang] % cmd), "text/html; charset=utf-8")

        def _fetch_site_ok(self):
            return self.headers.get("Sec-Fetch-Site") in (None, "same-origin")

        def _err(self, code, msg, **extra):
            return self._send(code, json.dumps(dict(extra, error=msg)), "application/json")

        def _body(self):
            """Parsed JSON object from the body, or None after sending the error."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._err(415, "Content-Type must be application/json")
                return None
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._err(411, "Content-Length required")
                return None
            if n < 0 or n > MAX_BODY:
                self._err(413, "body too large")
                return None
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._err(400, "invalid JSON")
                return None
            return body

        def _settings_post(self, action):
            if self.kit_demo:
                return self._err(403, "disabled in demo mode")
            if not (self._strict_host_ok() and self._origin_ok(True) and self._fetch_site_ok() and self._token_ok()):
                return self._err(403, "forbidden")
            body = self._body()
            if body is None:
                return
            if not isinstance(body, dict):
                return self._err(400, "body must be a JSON object")
            try:
                out = self.kit_settings.preview(body) if action == "preview" else self.kit_settings.apply(body)
            except settings_mod.SettingsError as e:
                return self._err(e.code, e.msg, **e.extra)
            return self._json(out)

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ALLOWED_HOSTS:  # DNS-rebinding guard
                return self._send(403, "forbidden host", "text/plain")
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path in ("/", "/index.html") and q.get("k"):
                return self._login(url, q)
            if not self._session_ok():
                return self._unauthorized(url.path, q)
            if url.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "dashboard.html"), "rb") as fh:
                    html = fh.read().replace(TOKEN_PLACEHOLDER.encode(), self.kit_token.encode())
                return self._send(200, html, "text/html; charset=utf-8")
            if url.path == "/api/settings":
                if self.kit_demo:
                    return self._err(403, "disabled in demo mode")
                if not (self._strict_host_ok() and self._fetch_site_ok() and self._token_ok()) or (q.get("anon") or ["0"])[0] == "1":
                    return self._err(403, "forbidden")
                return self._json(self.kit_settings.state())
            f = {k: (q.get(k) or [None])[0] or None for k in ("tenant", "project", "account")}
            anon = None
            if (q.get("anon") or ["0"])[0] == "1":
                from . import anon as anon_mod
                from . import limits as limits_mod
                con_a = _ro(db)
                try:
                    anon = anon_mod.Anonymizer(con_a, limits_mod.load_alerts().get("budgets") or [],
                                               lang=_lang(q, self.headers), labeler=Labeler.load())
                finally:
                    con_a.close()
                f = {k: anon.unmap(v) for k, v in f.items()}
            if url.path == "/api/data":
                try:
                    days = min(_query_days(q), MAX_DAYS)  # 0 stays all time; huge values would overflow
                except ValueError:
                    return self._err(400, "days must be a non-negative integer")
                con = _ro(db)
                try:
                    data = analysis.build(con, days=days or None, labeler=Labeler.load(), **f)
                    data["verification"] = cost.verify(con)
                finally:
                    con.close()
                data["db"] = db
                data["demo"] = self.kit_demo
                return self._json(data, anon)
            if url.path == "/api/limits":
                from . import limits
                con = _ro(db)
                try:
                    data = analysis.limits_view(con, labeler=Labeler.load())
                    data["budgets"] = limits.evaluate_budgets(con, dry=True)
                    data["thresholds"] = {k: v for k, v in limits.load_alerts().items() if k in ("five_hour", "seven_day", "context")}
                finally:
                    con.close()
                return self._json(data, anon)
            if url.path == "/api/alerts":
                from . import alertlog
                try:
                    days = min(_query_days(q) or 7, MAX_DAYS)  # 0 means the default week here; huge values would overflow
                except ValueError:
                    return self._err(400, "days must be a non-negative integer")
                con = _ro(db)
                try:
                    data = alertlog.view(con, days=days, labeler=Labeler.load())
                finally:
                    con.close()
                if anon is not None:  # names the anonymizer never saw must not leak in clear
                    for x in data["events"] + data["missed"]:
                        anon.cover(x.get("project"))
                        anon.cover((x.get("data") or {}).get("name"), "budget")
                return self._json(data, anon)
            if url.path == "/api/review":
                from . import review
                try:
                    date = _query_date(q)
                except ValueError:
                    return self._err(400, "date must be YYYY-MM-DD")
                con = _ro(db)
                try:
                    data = review.build(con, date, labeler=Labeler.load())
                finally:
                    con.close()
                return self._json(data, anon)
            if url.path == "/api/day":
                try:
                    date = _query_date(q)
                except ValueError:
                    return self._err(400, "date must be YYYY-MM-DD")
                con = _ro(db)
                try:
                    data = analysis.day(con, date=date, labeler=Labeler.load(), **f)
                finally:
                    con.close()
                return self._json(data, anon)
            return self._send(404, "not found", "text/plain")

        def do_POST(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin") or ""
            origin_host = urllib.parse.urlparse(origin).hostname if origin else None
            if host not in ALLOWED_HOSTS or (origin and origin_host not in ALLOWED_HOSTS):
                return self._send(403, "forbidden", "text/plain")  # DNS-rebinding / cross-site guard
            path = urllib.parse.urlparse(self.path).path
            if not self._session_ok():
                return self._unauthorized(path)
            if path in ("/api/settings/preview", "/api/settings/apply"):
                return self._settings_post(path.rsplit("/", 1)[1])
            if path != "/api/refresh":
                return self._send(404, "not found", "text/plain")
            if not (self._strict_host_ok() and self._origin_ok(False) and self._fetch_site_ok() and self._token_ok()):
                return self._send(403, "forbidden", "text/plain")
            if self.kit_demo:  # nothing to re-read: the demo's transcripts never change
                return self._send(200, json.dumps({"added": {"req": 0, "cost": 0, "sub": 0}}), "application/json")
            with REFRESH_LOCK:
                con = store.connect(db)
                try:
                    added = collect.ingest(con, paths.transcript_roots())
                    cost.rebuild_slices(con)
                    from . import limits
                    limits.evaluate_budgets(con)
                finally:
                    con.close()
            return self._send(200, json.dumps({"added": added}), "application/json")

    return Handler


def _windows():
    return sys.platform == "win32"


def _listen(port, handler):
    """The server bound to 127.0.0.1:`port`; SystemExit with a one-line hint when the port cannot be had."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler, bind_and_activate=False)
    if _windows():  # there SO_REUSEADDR lets a second process bind the same port: ask for it exclusively instead
        httpd.allow_reuse_address = False
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            httpd.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
    try:
        httpd.server_bind()
        httpd.server_activate()
    except OSError as e:
        httpd.server_close()
        raise SystemExit("sessioncoach: cannot listen on 127.0.0.1:%d (%s); use another port: --port N, or --port 0 "
                         "for any free one" % (port, e.strerror or e)) from None
    return httpd


def serve(port=8787, open_browser=True, db=None, demo=False):
    db = db or paths.db_path()
    if not os.path.exists(db):
        raise SystemExit("No database yet at %s. Run: sessioncoach ingest" % db)
    key = secrets.token_urlsafe(32)
    httpd = _listen(port, make_handler(db, demo=demo, key=key))
    url = "http://127.0.0.1:%d/?k=%s" % (httpd.server_address[1], key)
    print("sessioncoach dashboard: %s  (Ctrl+C to stop; local only, nothing leaves this machine)" % url)
    print("  Keep this link private: it is what lets a browser in (other users of this machine share 127.0.0.1).")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

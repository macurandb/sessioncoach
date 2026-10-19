"""Regression tests for defects found in code review. Nothing here touches the real machine."""
import http.client
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)
from serverauth import session_cookie

from sessioncoach import accounts, installer, limits, nudge, paths, review, scheduler, server, settings, store
from sessioncoach.projects import Labeler


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        patch = mock.patch.dict(os.environ, {"KIT_DATA_DIR": self.data})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestNotifyArgv(Tmp):
    def test_osascript_gets_text_as_arguments(self):
        title, message = "Claude · límite 5 h al 90%", 'Se reinicia a las 10:00 "ya"'
        with mock.patch.object(limits.sys, "platform", "darwin"), \
                mock.patch.object(limits, "_which", return_value=None), \
                mock.patch.object(limits.subprocess, "run") as run:
            limits.notify(title, message)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-2:], [title, message])
        self.assertEqual(cmd[0], "/usr/bin/osascript")
        self.assertFalse([c for c in cmd[:-2] if title in c or message in c])


class TestAlreadySent(Tmp):
    def test_two_connections_do_not_collide(self):
        db = os.path.join(self.tmp, "m.db")
        a, b = store.connect(db), store.connect(db)
        self.assertFalse(limits.already_sent(a, "k"))
        a.commit()
        self.assertTrue(limits.already_sent(b, "k"))

    def test_insert_lost_to_another_writer_counts_as_sent(self):
        db = os.path.join(self.tmp, "m.db")
        a, b = store.connect(db), store.connect(db)

        class Racy:  # the SELECT saw nothing; the other writer inserted before our INSERT
            def execute(self, sql, params=()):
                if sql.startswith("SELECT"):
                    return a.execute("SELECT 1 FROM alerts_sent WHERE 0")
                return b.execute(sql, params)
        a.execute("INSERT INTO alerts_sent (key, ts) VALUES ('k', 1)")
        a.commit()
        self.assertTrue(limits.already_sent(Racy(), "k"))


@unittest.skipIf(sys.platform == "win32", "POSIX modes")
class TestPrivateDataDir(Tmp):
    def setUp(self):
        super().setUp()
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.agents = os.path.join(self.tmp, "agents")

    def assertPrivate(self, *sub):
        for p in (self.data, os.path.join(self.data, *sub)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)

    def test_ingest_install_creates_the_data_dir_private(self):
        with mock.patch.dict(os.environ, {"KIT_SCHEDULER": "launchd"}):
            installer.ingest_install(30, "/opt/bin/sessioncoach", self.agents, load=False)
        self.assertPrivate("logs")

    def test_review_install_creates_the_data_dir_private(self):
        with mock.patch.dict(os.environ, {"KIT_SCHEDULER": "launchd"}):
            review.install(None, "/opt/bin/sessioncoach", self.agents, load=False)
        self.assertPrivate("logs")

    def test_scheduler_install_creates_logs_and_crontab_backup_private(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="0 1 * * * old\n" if cmd == ["crontab", "-l"] else "", stderr="")
        scheduler.install(installer.ingest_job(30), "/opt/bin/sessioncoach", run=run, be="cron")
        self.assertPrivate("logs")
        self.assertPrivate("backups")

    def test_scheduler_install_schtasks_state_dir_private(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        scheduler.install(installer.ingest_job(30), "/opt/bin/sessioncoach", run=run, be="schtasks")
        self.assertPrivate("scheduler")

    def test_private_file_deep_in_the_data_dir_creates_it_private(self):
        settings.write_atomic(os.path.join(self.data, "chains", "ab12", "statusline-chain.sh"), b":\n", 0o600)
        self.assertPrivate("chains")

    def test_panel_writing_the_chain_creates_the_data_dir_private(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".claude"))
        S = settings.Settings(home=home, load_agent=False, kit_bin="/opt/bin/sessioncoach")
        req = {"scope": "global", "statusline": True}
        pv = S.preview(req)
        S.apply(dict(req, files=[{k: f[k] for k in ("path", "before", "after")} for f in pv["files"]]))
        self.assertPrivate()


class TestMalformedPrivateJson(Tmp):
    def write(self, name, text):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_helper_gives_a_dict_for_anything(self):
        p = os.path.join(self.tmp, "f.json")
        self.assertEqual(paths.load_json_object(p), {})
        for text in ("[1, 2]", "null", '"x"', "{broken", "", "\xff"):
            with open(p, "w", encoding="latin-1") as fh:
                fh.write(text)
            self.assertEqual(paths.load_json_object(p), {}, text)
        with open(p, "w") as fh:
            fh.write('{"a": 1}')
        self.assertEqual(paths.load_json_object(p), {"a": 1})

    def test_projects_rules_of_the_wrong_shape(self):
        for text in ("[]", '{"rules": 3}', '{"rules": {"a": 1}}', '{"rules": [1, "x", null]}'):
            self.write("projects.json", text)
            self.assertEqual(Labeler.load()("-Users-me-code-app"), ("-Users-me-code-app".lstrip("-").replace("-", "/"), "unassigned"), text)

    def test_projects_rules_ignores_only_the_bad_entries(self):
        self.write("projects.json", '{"rules": [7, {"match": "-Users-me-*", "project": "p", "tenant": "t"}]}')
        self.assertEqual(Labeler.load()("-Users-me-x"), ("p", "t"))

    def test_accounts_of_the_wrong_shape(self):
        for text in ("[]", '{"accounts": []}', '{"accounts": "x"}'):
            self.write("accounts.json", text)
            self.assertEqual(accounts.load_map(), {}, text)
        self.write("accounts.json", '{"accounts": {"a": 5, "b": {"label": "B"}}}')
        self.assertEqual(accounts.load_map(), {"b": {"label": "B"}})

    def test_transcript_roots_with_odd_accounts(self):
        for text in ("[]", '{"accounts": []}', '{"accounts": {"a": 5, "b": {"roots": 7}, "c": {"roots": ["/x/projects", 3]}}}'):
            self.write("accounts.json", text)
            with mock.patch.dict(os.environ, {"KIT_PROJECTS_DIRS": "/base/projects"}):
                roots = paths.transcript_roots()
            self.assertEqual(roots[0], os.path.normpath("/base/projects"), text)

    def test_alerts_that_are_not_an_object_use_the_defaults(self):
        self.write("alerts.json", "[1, 2]")
        self.assertEqual(limits.load_alerts(), limits.DEFAULT_ALERTS)

    def test_account_detection_with_odd_login_file(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        for text in ("[]", '{"oauthAccount": []}', '{"oauthAccount": "x"}'):
            with open(os.path.join(cfg, ".claude.json"), "w") as fh:
                fh.write(text)
            self.assertEqual(accounts.detect(os.path.join(cfg, "projects")), {}, text)

    def test_check_tolerates_odd_settings(self):
        sp = os.path.join(self.tmp, "settings.json")
        cases = [
            {"statusLine": "echo hi"},
            {"statusLine": ["x"]},
            {"hooks": []},
            {"hooks": {"UserPromptSubmit": "x"}},
            {"hooks": {"UserPromptSubmit": ["x", {"hooks": "y"}, {"hooks": [7, None]}]}},
            {"cleanupPeriodDays": "soon"},
        ]
        for case in cases:
            with open(sp, "w") as fh:
                json.dump(case, fh)
            self.assertEqual(installer._scope_items("t", sp), [], case)
        with open(sp, "w") as fh:
            fh.write("[1]")
        self.assertEqual(installer._scope_items("t", sp)[0][0], installer.WARN)
        with mock.patch.object(paths, "HOME", self.tmp):
            self.assertEqual(installer.retention_days(self.tmp), installer.DEFAULT_RETENTION_DAYS)


class TestAtomicSettingsWrite(Tmp):
    """The CLI rewrites ~/.claude/settings.json: a failure half way must leave the original intact."""

    def setUp(self):
        super().setUp()
        self.sp = os.path.join(self.tmp, "settings.json")
        with open(self.sp, "w", encoding="utf-8") as fh:
            json.dump({"theme": "dark", "note": "café"}, fh)
        with open(self.sp, "rb") as fh:
            self.original = fh.read()

    def intact(self):
        with open(self.sp, "rb") as fh:
            self.assertEqual(fh.read(), self.original)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_every_writer_goes_through_a_temp_file(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            for fn in (lambda: limits.install(self.sp, "/opt/bin/sessioncoach"), lambda: nudge.install(self.sp, "/opt/bin/sessioncoach")):
                with self.assertRaises(OSError):
                    fn()
                self.intact()

    def test_uninstall_writers_too(self):
        limits.install(self.sp, "/opt/bin/sessioncoach")
        nudge.install(self.sp, "/opt/bin/sessioncoach")
        with open(self.sp, "rb") as fh:
            self.original = fh.read()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            for fn in (lambda: limits.uninstall(self.sp), lambda: nudge.uninstall(self.sp)):
                with self.assertRaises(OSError):
                    fn()
                self.intact()

    def test_written_as_utf8_with_a_single_backup(self):
        with mock.patch("locale.getpreferredencoding", return_value="cp1252"):
            nudge.install(self.sp, "/opt/bin/sessioncoach")
        with open(self.sp, "rb") as fh:
            self.assertEqual(json.loads(fh.read().decode("utf-8"))["note"], "café")
        self.assertEqual([f for f in os.listdir(self.tmp) if ".bak" in f], ["settings.json.bak-hook"])

    def test_a_later_backup_keeps_the_first_one(self):
        with open(self.sp, "w") as fh:
            fh.write("before")
        first = paths.backup_file(self.sp, ".bak-x")
        paths.backup_file(self.sp, ".bak-x")  # same content: nothing to keep apart
        self.assertEqual([f for f in os.listdir(self.tmp) if ".bak" in f], ["settings.json.bak-x"])
        for text in ("after", "later"):
            with open(self.sp, "w") as fh:
                fh.write(text)
            with mock.patch("time.strftime", return_value="20261017-120000"):
                self.assertEqual(paths.backup_file(self.sp, ".bak-x"), first)
        kept = {f: paths._read(os.path.join(self.tmp, f)).decode() for f in os.listdir(self.tmp) if ".bak" in f}
        self.assertEqual(kept, {"settings.json.bak-x": "later", "settings.json.bak-x-20261017-120000": "before",
                                "settings.json.bak-x-20261017-120000-2": "after"})
        if os.name != "nt":  # no POSIX modes on Windows
            self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o600)

    def test_settings_keeps_its_public_writer(self):
        self.assertIs(settings.write_atomic, paths.write_atomic)


class TestLaunchctlTimeouts(Tmp):
    def setUp(self):
        super().setUp()
        self.agents = os.path.join(self.tmp, "agents")
        os.makedirs(self.agents)
        env = mock.patch.dict(os.environ, {"KIT_SCHEDULER": "launchd"})
        env.start()
        self.addCleanup(env.stop)
        uid = mock.patch.object(os, "getuid", create=True, return_value=501)  # launchd is macOS-only; os.getuid too
        uid.start()
        self.addCleanup(uid.stop)

    @staticmethod
    def hang_on(verb):
        def run(cmd, **kw):
            if cmd[:2] == ["launchctl", verb]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0)
        return mock.Mock(side_effect=run)

    def test_every_launchctl_call_has_a_timeout(self):
        calls = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch.object(subprocess, "run", calls):
            installer.ingest_install(30, "/opt/bin/sessioncoach", self.agents, load=True)
            review.install(None, "/opt/bin/sessioncoach", self.agents, load=True)
            plist = scheduler.plist_path(scheduler.launchd_label("ingest"), self.agents)
            scheduler.launchd_remove(plist, True)
        launchctl = [c for c in calls.call_args_list if c[0][0][0] == "launchctl"]
        self.assertEqual(len(launchctl), 5)
        self.assertTrue(all(c[1].get("timeout") == 30 for c in launchctl))

    def test_a_hung_bootstrap_is_a_clear_error(self):
        for fn in (lambda: installer.ingest_install(30, "/opt/bin/sessioncoach", self.agents, load=True),
                   lambda: review.install(None, "/opt/bin/sessioncoach", self.agents, load=True)):
            with mock.patch.object(subprocess, "run", self.hang_on("bootstrap")), self.assertRaises(RuntimeError) as ctx:
                fn()
            self.assertIn("launchctl bootstrap", str(ctx.exception))
            self.assertIn("timed out", str(ctx.exception))

    def test_a_hung_bootout_is_ignored_when_installing_and_removing(self):
        with mock.patch.object(subprocess, "run", self.hang_on("bootout")):
            installer.ingest_install(30, "/opt/bin/sessioncoach", self.agents, load=True)
            review.install(None, "/opt/bin/sessioncoach", self.agents, load=True)
            plist = scheduler.plist_path(scheduler.launchd_label("ingest"), self.agents)
            self.assertTrue(scheduler.launchd_remove(plist, True))
        self.assertFalse(os.path.exists(plist))


class TestServerQueryValidation(Tmp):
    def setUp(self):
        super().setUp()
        os.makedirs(self.data)
        db = os.path.join(self.data, "metrics.db")
        store.connect(db).close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(db))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            conn.request("GET", path, headers={"Cookie": session_cookie(self.httpd)})
            r = conn.getresponse()
            return r.status, json.loads(r.read().decode("utf-8"))
        finally:
            conn.close()

    def test_bad_values_are_a_400_with_a_message(self):
        for path in ("/api/data?days=abc", "/api/data?days=-3", "/api/review?date=yesterday", "/api/day?date=2026-13-45"):
            status, body = self.get(path)
            self.assertEqual(status, 400, path)
            self.assertIn("error", body)

    def test_good_values_still_work(self):
        for path in ("/api/data?days=7", "/api/data?days=0", "/api/data", "/api/day?date=2026-10-01", "/api/review?date=2026-10-01"):
            self.assertEqual(self.get(path)[0], 200, path)

    def test_a_huge_days_is_clamped_not_an_overflow(self):
        for path in ("/api/data?days=999999999999", "/api/alerts?days=999999999999"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("error", body)


class TestServeBind(Tmp):
    def setUp(self):
        super().setUp()
        os.makedirs(self.data)
        self.db = os.path.join(self.data, "metrics.db")
        store.connect(self.db).close()

    def test_a_port_in_use_is_one_line_with_a_hint(self):
        import socket
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        with mock.patch("webbrowser.open") as opened, mock.patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as cm:
            server.serve(port=port, db=self.db)
        msg = str(cm.exception.code)
        self.assertIn("cannot listen on 127.0.0.1:%d" % port, msg)
        self.assertIn("--port N, or --port 0", msg)
        self.assertNotIn("\n", msg)
        opened.assert_not_called()

    def test_on_windows_the_port_is_asked_for_exclusively(self):
        import socket
        handler = server.make_handler(self.db)
        opt = socket.SO_KEEPALIVE  # stands in for SO_EXCLUSIVEADDRUSE, which only Windows has
        with mock.patch.object(server, "_windows", return_value=True), \
                mock.patch.object(socket, "SO_EXCLUSIVEADDRUSE", opt, create=True):
            httpd = server._listen(0, handler)
        self.addCleanup(httpd.server_close)
        self.assertFalse(httpd.allow_reuse_address)
        self.assertEqual(httpd.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
        self.assertNotEqual(httpd.socket.getsockopt(socket.SOL_SOCKET, opt), 0)
        with mock.patch.object(server, "_windows", return_value=True), mock.patch.object(socket, "SO_EXCLUSIVEADDRUSE", None, create=True):
            other = server._listen(0, handler)  # an old Python without the constant: still no SO_REUSEADDR
        self.addCleanup(other.server_close)
        self.assertEqual(other.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
        here = server._listen(0, handler)
        self.addCleanup(here.server_close)
        self.assertTrue(here.allow_reuse_address or sys.platform == "win32")


class TestServerLogin(Tmp):
    """Loopback is shared with the other users of the machine: nothing is served without the session cookie."""

    KEY, TOKEN = "the-link-secret", "the-csrf-token"

    def setUp(self):
        super().setUp()
        os.makedirs(self.data)
        db = os.path.join(self.data, "metrics.db")
        store.connect(db).close()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        S = settings.Settings(home=self.home, load_agent=False, kit_bin="/opt/bin/sessioncoach")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(db, token=self.TOKEN, settings=S, key=self.KEY))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def call(self, method, path, cookie=None, body=None):
        h = {"Host": "127.0.0.1:%d" % self.port, "X-Kit-Token": self.TOKEN}
        if cookie is not None:
            h["Cookie"] = cookie
        if method == "POST":
            h.update({"Origin": "http://127.0.0.1:%d" % self.port, "Content-Type": "application/json"})
            body = json.dumps(body or {"scope": "global", "nudge_hook": True}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8"), r
        finally:
            conn.close()

    def test_without_the_cookie_nothing_is_served(self):
        status, page, r = self.call("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn("sessioncoach dashboard", page)
        self.assertNotIn(self.TOKEN, page)
        self.assertNotIn(server.TOKEN_PLACEHOLDER, page)
        self.assertNotIn("kit-token", page)
        for path in ("/index.html", "/api/data", "/api/limits", "/api/alerts", "/api/review", "/api/day", "/api/settings",
                     "/api/data?k=" + self.KEY):  # the secret only works on the page
            self.assertEqual(self.call("GET", path)[0], 401, path)
        for path in ("/api/settings/preview", "/api/settings/apply", "/api/refresh"):
            status, body, _ = self.call("POST", path)
            self.assertEqual(status, 401, path)
            self.assertIn("error", json.loads(body))
        self.assertEqual(os.listdir(os.path.join(self.home, ".claude")), [])

    def test_the_login_page_is_english_unless_spanish_is_asked_for(self):
        page = self.call("GET", "/")[1]
        self.assertIn('<html lang="en">', page)
        self.assertIn("Open the link", page)
        self.assertNotIn("Abre el enlace", page)
        for path, accept in (("/?lang=es", None), ("/", "es-ES,es;q=0.9,en;q=0.8")):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", path, headers=dict({"Accept-Language": accept} if accept else {}))
            page = conn.getresponse().read().decode("utf-8")
            conn.close()
            self.assertIn('<html lang="es">', page, path)
            self.assertIn("Abre el enlace", page, path)
            self.assertNotIn("Open the link", page, path)

    def test_language_choice(self):
        self.assertEqual(server._lang({}, {}), "en")
        self.assertEqual(server._lang({}, {"Accept-Language": "en-US,es;q=0.5"}), "en")
        self.assertEqual(server._lang({}, {"Accept-Language": "ES-mx"}), "es")
        self.assertEqual(server._lang({"lang": ["es"]}, {"Accept-Language": "en"}), "es")
        self.assertEqual(server._lang({"lang": ["en"]}, {"Accept-Language": "es"}), "en")

    def test_anonymous_api_calls_default_to_english_labels(self):
        from sessioncoach import anon
        with mock.patch.object(anon, "Anonymizer", wraps=anon.Anonymizer) as made:
            self.assertEqual(self.call("GET", "/api/data?anon=1", cookie=session_cookie(self.httpd))[0], 200)
            self.assertEqual(made.call_args[1]["lang"], "en")
            self.assertEqual(self.call("GET", "/api/data?anon=1&lang=es", cookie=session_cookie(self.httpd))[0], 200)
            self.assertEqual(made.call_args[1]["lang"], "es")

    def test_a_bad_or_foreign_cookie_is_refused(self):
        good = session_cookie(self.httpd)
        other_port = "sessioncoach_%d=%s" % (self.port + 1, self.httpd.RequestHandlerClass.kit_session)
        for cookie in ("sessioncoach_%d=nope" % self.port, other_port, 'a="unterminated', "\xe9=\xe9", ""):
            self.assertEqual(self.call("GET", "/api/data", cookie=cookie)[0], 401, cookie)
        self.assertEqual(self.call("GET", "/api/data", cookie="theme=dark; " + good)[0], 200)

    def test_a_wrong_secret_is_refused(self):
        for k in ("nope", "", "%C3%A9", self.KEY + "x"):
            status, page, r = self.call("GET", "/?k=" + k)
            self.assertEqual(status, 401, k)
            self.assertIsNone(r.getheader("Set-Cookie"))
            self.assertNotIn(self.TOKEN, page)

    def test_the_right_secret_sets_the_cookie_and_drops_itself_from_the_url(self):
        status, _, r = self.call("GET", "/?k=" + self.KEY)
        self.assertEqual(status, 303)
        self.assertEqual(r.getheader("Location"), "/")
        cookie = r.getheader("Set-Cookie")
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        pair = cookie.split(";")[0]
        self.assertEqual(pair, session_cookie(self.httpd))
        self.assertNotIn(self.KEY, cookie)
        status, page, _ = self.call("GET", "/", cookie=pair)
        self.assertEqual(status, 200)
        self.assertIn('<meta name="kit-token" content="%s">' % self.TOKEN, page)
        self.assertEqual(self.call("GET", "/api/data", cookie=pair)[0], 200)
        self.assertEqual(self.call("GET", "/api/settings", cookie=pair)[0], 200)
        self.assertEqual(self.call("POST", "/api/settings/preview", cookie=pair)[0], 200)
        # the link keeps working while the server lives (another tab, another browser); other params survive
        status, _, r = self.call("GET", "/?anon=1&k=%s&lang=en" % self.KEY)
        self.assertEqual((status, r.getheader("Location")), (303, "/?anon=1&lang=en"))

    def test_the_csrf_token_is_still_needed_with_the_cookie(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/settings/preview", body=b"{}", headers={
            "Host": "127.0.0.1:%d" % self.port, "Origin": "http://127.0.0.1:%d" % self.port,
            "Content-Type": "application/json", "Cookie": session_cookie(self.httpd)})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_a_foreign_host_is_still_a_403_before_any_login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/?k=" + self.KEY, headers={"Host": "evil.example:%d" % self.port})
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader("Set-Cookie")), (403, None))
        conn.close()


class TestServeLink(Tmp):
    def test_serve_opens_and_prints_the_link_with_the_secret(self):
        os.makedirs(self.data)
        db = os.path.join(self.data, "metrics.db")
        store.connect(db).close()
        httpd = mock.Mock(server_address=("127.0.0.1", 8799))
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(server, "ThreadingHTTPServer", return_value=httpd) as cls, \
                mock.patch("webbrowser.open") as opened, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            server.serve(port=8799, db=db, demo=True)
        handler = cls.call_args[0][1]
        url = "http://127.0.0.1:8799/?k=%s" % handler.kit_key
        self.assertEqual(len(handler.kit_key), 43)
        self.assertTrue(handler.kit_demo)
        opened.assert_called_once_with(url)
        self.assertIn(url, out.getvalue())
        httpd.server_close.assert_called_once_with()

    def test_two_starts_get_different_secrets(self):
        a, b = server.make_handler("x.db"), server.make_handler("x.db")
        self.assertNotEqual(a.kit_key, b.kit_key)
        self.assertNotEqual(a.kit_session, b.kit_session)
        self.assertNotEqual(a.kit_key, a.kit_session)


class TestEmailChannel(Tmp):
    def deliver(self, code):
        os.makedirs(self.data)
        con = store.connect(os.path.join(self.data, "m.db"))
        self.addCleanup(con.close)
        cfg = {"channels": ["email"], "email_command": [sys.executable, "-c", "import sys; sys.exit(%d)" % code]}
        with mock.patch.object(review, "texts", return_value=("title", "message", "body")):
            return review.deliver(con, {"day": "2026-10-09"}, cfg)

    def test_email_counts_as_sent_only_when_the_command_succeeds(self):
        self.assertEqual(self.deliver(0), ["email"])

    def test_a_failing_email_command_is_not_reported_as_sent(self):
        self.assertEqual(self.deliver(3), [])


if __name__ == "__main__":
    unittest.main()

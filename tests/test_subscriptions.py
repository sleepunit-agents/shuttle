"""Subscriptions (shuttle/subscriptions.py, tools/loom-subscriptions): which
account an agent runs on, what each credential's state is, and that only
names are ever kept."""
import json
import shutil
import re
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shuttle import subscriptions as subs  # noqa: E402


class Subscriptions(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="shuttle-subs-"))
        self.env = mock.patch.dict(os.environ, {"LOOM_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        d = subs.load()
        subs.add_subscription(d, "main", "anthropic", "The household's Max", owner="robin", plan="Max")
        subs.add_subscription(d, "spare", "anthropic", "The spare", owner="robin", plan="Pro")
        subs.add_subscription(d, "chatgpt", "openai", "ChatGPT", owner="robin")
        subs.add_credential(d, "main", "long-lived", secret="claude-main")
        subs.add_credential(d, "spare", "long-lived", secret="claude-spare")
        subs.add_credential(d, "chatgpt", "device", secret="codex-chatgpt-auth.json")
        d["assign"]["robin"] = ["main", "spare"]
        subs.save(d)

    def test_check_records_status_and_usage_and_keeps_no_value(self):
        d = subs.load()
        values = {"claude-main": "sk-ant-oat01-" + "M" * 40, "claude-spare": "sk-ant-oat01-" + "S" * 40}
        seen = []

        def probe(token):
            seen.append(token)
            return ("valid", {"5h": 0.2, "7d": 0.5, "reset_5h": 1, "reset_7d": 2}) if "M" in token else ("expired", {})
        subs.check(d, values.__getitem__, probe=probe, now=1000)
        subs.save(d)
        self.assertEqual(sorted(seen), sorted(values.values()), "the OpenAI device login isn't probed this way")
        d = subs.load()
        self.assertEqual(d["subscriptions"]["main"]["credentials"][0]["status"], "valid")
        self.assertEqual(d["subscriptions"]["main"]["usage"]["7d"], 0.5)
        self.assertEqual(d["subscriptions"]["spare"]["credentials"][0]["status"], "expired")
        raw = subs.path().read_text()
        self.assertNotIn("sk-ant", raw)
        # A probe that fails with the value in its message keeps only the error's kind.
        def leaky(token):
            raise RuntimeError(f"bad token {token}")
        subs.check(d, values.__getitem__, only="main", probe=leaky, now=2000)
        subs.save(d)
        self.assertEqual(subs.load()["subscriptions"]["main"]["credentials"][0]["error"], "RuntimeError")
        self.assertNotIn("sk-ant", subs.path().read_text())
        self.assertEqual(oct(subs.path().stat().st_mode & 0o777), "0o600")

    def test_pick_is_the_first_with_room_and_falls_to_the_next(self):
        d = subs.load()
        for name in ("main", "spare"):
            d["subscriptions"][name]["credentials"][0]["status"] = "valid"
        d["subscriptions"]["main"]["usage"] = {"5h": 0.4, "7d": 0.6}
        self.assertEqual(subs.pick(d, "robin"), ("main", "claude-main"))
        d["subscriptions"]["main"]["usage"] = {"5h": 0.99, "7d": 0.6}   # out of room this window
        self.assertEqual(subs.pick(d, "robin"), ("spare", "claude-spare"))
        d["subscriptions"]["spare"]["credentials"][0]["status"] = "expired"
        self.assertEqual(subs.pick(d, "robin"), ("main", "claude-main"), "nothing better: the full one, never an expired one")
        self.assertEqual(subs.pick(d, "wren")[0], "main", "an agent with no order uses any of the provider's")
        self.assertEqual(subs.pick(d, "robin", "openai"), ("chatgpt", "codex-chatgpt-auth.json"))

    def test_a_session_login_is_checked_from_its_file_without_its_token(self):
        f = self.home / "creds.json"
        f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-" + "A" * 40, "refreshToken": "sk-ant-ort01-" + "R" * 40,
                                                   "refreshTokenExpiresAt": 2_000_000_000_000, "subscriptionType": "max",
                                                   "rateLimitTier": "default_claude_max_20x", "scopes": ["user:inference", "user:profile"]}}))
        d = subs.load()
        subs.add_credential(d, "main", "session", file=str(f))
        subs.check(d, lambda n: "sk-ant-oat01-" + "M" * 40, only="main", probe=lambda t: ("valid", {}), now=1000)
        subs.save(d)
        c = subs.load()["subscriptions"]["main"]["credentials"][1]
        self.assertEqual((c["status"], c["expires"], c["scopes"]), ("valid", 2_000_000_000, ["user:inference", "user:profile"]))
        self.assertEqual(subs.load()["subscriptions"]["main"]["plan"], "Max")   # kept: the owner already said Max
        self.assertNotIn("sk-ant", subs.path().read_text())
        f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y", "refreshTokenExpiresAt": 500}}))
        d = subs.load()
        subs.check(d, lambda n: "", only="main", probe=lambda t: ("valid", {}), now=1000)
        self.assertEqual(d["subscriptions"]["main"]["credentials"][1]["status"], "expired")

    def jwt(self, claims):
        import base64
        b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        return f"{b({'alg': 'none'})}.{b(claims)}.sig"

    def test_an_openai_login_is_refreshed_here_and_issued_without_its_refresh_token(self):
        now = 1_800_000_000
        auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-should-not-travel", "extra": 1, "tokens": {"access_token": self.jwt({"exp": now + 600, "iat": now - 863400}), "refresh_token": "rt-old",
                "id_token": self.jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}), "account_id": "a"}}
        store = {"codex-chatgpt-auth.json": json.dumps(auth)}
        d = subs.load()
        d["subscriptions"]["chatgpt"]["credentials"][0]["status"] = "valid"
        posted = []

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body

        def post(req, timeout):
            posted.append(json.loads(req.data))
            return Resp(json.dumps({"access_token": self.jwt({"exp": now + 864000, "iat": now}), "refresh_token": "rt-new"}).encode())
        name, secret, val = subs.issue(d, "robin", "openai", store.__getitem__, store.__setitem__, now=now, post=post)
        self.assertEqual((name, secret), ("chatgpt", "codex-chatgpt-auth.json"))
        self.assertEqual(posted[0]["grant_type"], "refresh_token")
        self.assertEqual(posted[0]["refresh_token"], "rt-old")
        self.assertEqual(json.loads(store[secret])["tokens"]["refresh_token"], "rt-new", "the rotated login is stored")
        house = json.loads(val)
        self.assertEqual(house["tokens"]["refresh_token"], "", "a house can't rotate it")
        self.assertEqual(sorted(house), ["OPENAI_API_KEY", "auth_mode", "last_refresh", "tokens"])
        self.assertIsNone(house["OPENAI_API_KEY"])
        self.assertEqual(subs._jwt_claims(house["tokens"]["access_token"])["exp"], now + 864000)
        self.assertFalse(list(self.home.glob(".refreshed-*")), "the safety copy goes once the store has it")
        # Plenty of access left: no refresh.
        posted.clear()
        subs.issue(d, "robin", "openai", store.__getitem__, store.__setitem__, now=now, post=post)
        self.assertEqual(posted, [])
        # The check reads it without the network.
        subs.check(d, store.__getitem__, only="chatgpt", now=now)
        c = d["subscriptions"]["chatgpt"]["credentials"][0]
        self.assertEqual((c["status"], c["access_until"]), ("valid", now + 864000))
        self.assertEqual(d["subscriptions"]["chatgpt"]["plan"], "Pro")

    def test_a_failed_store_keeps_the_refreshed_login_on_disk(self):
        now = 1_800_000_000
        auth = {"tokens": {"access_token": self.jwt({"exp": now}), "refresh_token": "rt-old", "id_token": ""}}
        store = {"codex-chatgpt-auth.json": json.dumps(auth)}
        d = subs.load()
        d["subscriptions"]["chatgpt"]["credentials"][0]["status"] = "valid"

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"access_token": "x.e30.y", "refresh_token": "rt-new"}).encode()

        def bad_store(k, v):
            raise RuntimeError("store down")
        with self.assertRaises(RuntimeError):
            subs.issue(d, "robin", "openai", store.__getitem__, bad_store, now=now, post=lambda req, timeout: Resp())
        kept = list(self.home.glob(".refreshed-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(kept[0].read_text())["tokens"]["refresh_token"], "rt-new")
        self.assertEqual(oct(kept[0].stat().st_mode & 0o777), "0o600")

    def test_a_claude_session_login_is_refreshed_here_and_issued_access_only(self):
        now = 1_800_000_000
        login = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-" + "A" * 40, "refreshToken": "sk-ant-ort01-" + "R" * 40,
                                   "expiresAt": (now + 1800) * 1000, "scopes": ["user:inference", "user:sessions:claude_code"],
                                   "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}
        store = {"claude-session-main.json": json.dumps(login)}
        d = subs.load()
        c = subs.add_credential(d, "main", "session", secret="claude-session-main.json")
        c["status"] = "valid"
        posted = []

        class Resp:
            def __init__(self, b): self.b = b
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.b

        def post(req, timeout):
            posted.append((req.full_url, json.loads(req.data)))
            return Resp(json.dumps({"access_token": "sk-ant-oat01-" + "N" * 40, "refresh_token": "sk-ant-ort01-" + "S" * 40,
                                    "expires_in": 28800, "scope": "user:inference user:sessions:claude_code user:profile"}).encode())
        name, secret, val = subs.issue(d, "robin", "anthropic", store.__getitem__, store.__setitem__, now=now, post=post, kind="session")
        self.assertEqual((name, secret), ("main", "claude-session-main.json"))
        url, body = posted[0]
        self.assertEqual(url, subs.CLAUDE_TOKEN_URL)
        self.assertEqual((body["grant_type"], body["client_id"]), ("refresh_token", subs.CLAUDE_CLIENT_ID))
        self.assertEqual(body["refresh_token"], "sk-ant-ort01-" + "R" * 40)
        self.assertEqual(json.loads(store[secret])["claudeAiOauth"]["refreshToken"], "sk-ant-ort01-" + "S" * 40, "rotated, stored")
        house = json.loads(val)["claudeAiOauth"]
        self.assertEqual((house["accessToken"], house["refreshToken"]), ("sk-ant-oat01-" + "N" * 40, ""))
        self.assertEqual(house["expiresAt"], (now + 28800) * 1000)
        self.assertIn("user:sessions:claude_code", house["scopes"])
        self.assertEqual(sorted(house), ["accessToken", "expiresAt", "rateLimitTier", "refreshToken", "scopes", "subscriptionType"])
        # Plenty left: no refresh. The check keeps it alive and reads real usage, in fractions.
        posted.clear()
        usage_body = json.dumps({"five_hour": {"utilization": 12.0, "resets_at": "2027-01-15T13:00:00Z"},
                                 "seven_day": {"utilization": 40.0}, "seven_day_opus": {"utilization": 55.0}}).encode()
        subs.check(d, store.__getitem__, only="main", probe=lambda t: ("valid", {}), now=now, store=store.__setitem__, post=post,
                   read_usage=lambda tok: subs.claude_usage(tok, urlopen=lambda req, timeout: Resp(usage_body)))
        self.assertEqual(posted, [])
        u = d["subscriptions"]["main"]["usage"]
        self.assertEqual((u["5h"], u["7d"], u["7d_opus"], u["via"]), (0.12, 0.4, 0.55, "usage endpoint"))
        self.assertIn("reset_5h", u)

    def test_two_issues_at_once_refresh_a_login_once(self):
        # Houses claimed together each issue; the refresh token rotates, so a
        # second refresh with the old one is refused (or, at OpenAI, reuse).
        import threading
        import time as _time
        now = 1_800_000_000
        auth = {"tokens": {"access_token": self.jwt({"exp": now + 60, "iat": now - 864000}), "refresh_token": "rt-old", "id_token": ""}}
        store = {"codex-chatgpt-auth.json": json.dumps(auth)}
        posted, out = [], []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"access_token": self.tok, "refresh_token": "rt-new"}).encode()

        def post(req, timeout):
            posted.append(json.loads(req.data)["refresh_token"])
            _time.sleep(0.3)
            r = Resp()
            r.tok = self.jwt({"exp": now + 864000, "iat": now})
            return r

        def one():
            d = subs.load()
            out.append(subs.issue(d, "robin", "openai", store.__getitem__, store.__setitem__, now=now, post=post))
        ts = [threading.Thread(target=one) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(posted, ["rt-old"], "one refresh, with the token that was current")
        self.assertEqual(len(out), 3)
        self.assertEqual({json.loads(v)["tokens"]["access_token"] for _, _, v in out}, {json.loads(store["codex-chatgpt-auth.json"])["tokens"]["access_token"]})

    def test_a_refused_refresh_is_retried_once_on_a_login_rotated_meanwhile(self):
        import io
        import urllib.error
        now = 1_800_000_000
        old = {"claudeAiOauth": {"accessToken": "a-old", "refreshToken": "r-old", "expiresAt": (now + 60) * 1000}}
        fresh = {"claudeAiOauth": {"accessToken": "a-fresh", "refreshToken": "r-fresh", "expiresAt": (now + 28800) * 1000}}
        store = {"claude-session-main.json": json.dumps(old)}
        d = subs.load()
        subs.add_credential(d, "main", "session", secret="claude-session-main.json")["status"] = "valid"
        posted = []

        def refused(req, timeout):
            posted.append(json.loads(req.data)["refresh_token"])
            # Someone else (a sign-in on the page) stored a new login meanwhile.
            store["claude-session-main.json"] = json.dumps(fresh)
            raise urllib.error.HTTPError(req.full_url, 400, "invalid_grant", {}, io.BytesIO(b"{}"))
        _, _, val = subs.issue(d, "robin", "anthropic", store.__getitem__, store.__setitem__, now=now, post=refused, kind="session")
        self.assertEqual(posted, ["r-old"])
        self.assertEqual(json.loads(val)["claudeAiOauth"]["accessToken"], "a-fresh", "the login stored meanwhile, already fresh")
        # Refused with nothing changed: a real refusal, not retried.
        store["claude-session-main.json"] = json.dumps(old)
        posted.clear()

        def refused_again(req, timeout):
            posted.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, "invalid_grant", {}, io.BytesIO(b"{}"))
        with self.assertRaises(urllib.error.HTTPError):
            subs.issue(d, "robin", "anthropic", store.__getitem__, store.__setitem__, now=now, post=refused_again, kind="session")
        self.assertEqual(posted, [1])

    def test_every_outbound_call_names_its_client(self):
        # Cloudflare in front of platform.claude.com answers Python's default
        # User-Agent with 403 "error code: 1010" (2026-10-26: the broker's first
        # refresh, and every one after, never reached the auth server).
        seen = []

        class Resp:
            headers = {}
            def __init__(self, b): self.b = b
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.b

        def capture(body):
            def call(req, timeout):
                seen.append(req.get_header("User-agent"))
                return Resp(body)
            return call
        now = 1_800_000_000
        subs.refresh_claude({"refreshToken": "r"}, capture(json.dumps({"access_token": "a"}).encode()), now)
        subs.refresh_openai({"tokens": {"refresh_token": "r"}}, capture(json.dumps({"access_token": "a"}).encode()))
        subs.claude_usage("t", urlopen=capture(b"{}"))
        subs.probe_anthropic("t", urlopen=capture(json.dumps({"content": []}).encode()))
        self.assertEqual(len(seen), 4)
        for ua in seen:
            self.assertEqual(ua, subs.USER_AGENT)
        self.assertNotIn("Python-urllib", subs.USER_AGENT)

    def test_check_fails_when_a_refresh_fails_so_its_unit_can_alert(self):
        # 2026-10-26: every refresh failed (Cloudflare 403) for hours and the
        # check exited 0; its unit's OnFailure= never fired.
        import io
        import urllib.error
        now = int(subs.time.time())
        login = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": (now + 60) * 1000}}
        store = {"claude-session-main.json": json.dumps(login)}
        d = subs.load()
        subs.add_credential(d, "main", "session", secret="claude-session-main.json")["status"] = "valid"
        subs.save(d)

        def refused(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"error code: 1010"))
        err = io.StringIO()
        with mock.patch.object(subs, "secret_reader", return_value=store.__getitem__), \
             mock.patch.object(subs, "store_secret", store.__setitem__), \
             mock.patch.object(subs.urllib.request, "urlopen", refused), \
             mock.patch.object(subs, "probe_anthropic", return_value=("valid", {})), \
             mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            code = subs.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("main: claude-session-main.json couldn't be refreshed (HTTPError)", err.getvalue())
        # Refreshed fine: back to 0.
        fresh = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": (now + 86400) * 1000}}
        store["claude-session-main.json"] = json.dumps(fresh)
        with mock.patch.object(subs, "secret_reader", return_value=store.__getitem__), \
             mock.patch.object(subs, "store_secret", store.__setitem__), \
             mock.patch.object(subs, "probe_anthropic", return_value=("valid", {})), \
             mock.patch.object(subs, "claude_usage", return_value={"via": "usage endpoint"}), \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(subs.main(["check"]), 0)

    def test_an_agent_is_issued_only_its_own_through_the_api(self):
        from importlib.machinery import SourceFileLoader
        tool = SourceFileLoader("loom_subscriptions_tool", str(ROOT / "tools/loom-subscriptions")).load_module()
        d, ran = subs.load(), []

        def run(argv):
            ran.append(argv)
            return 0, "the-view", "issued main/claude-session-main.json\n"

        def call(caller, argv, path="run"):
            return tool.api_answer(d, {"caller": caller, "path": path, "body": {"argv": argv}}, run)
        robin = {"kind": "agent", "agent": "robin"}
        got = call(robin, ["issue", "robin", "anthropic", "--kind", "session"])
        self.assertEqual((got["status"], got["body"]["stdout"]), (200, "the-view"))
        self.assertEqual(ran, [["issue", "robin", "anthropic", "--kind", "session"]])
        for caller, argv, status in [(robin, ["issue", "mark", "anthropic"], 403),          # not its own
                                     ({"kind": "agent", "agent": "mark"}, ["issue", "mark", "anthropic"], 403),  # no order here
                                     (robin, ["pick", "robin", "anthropic"], 403),          # only issue
                                     (robin, ["issue", "robin", "anthropic", "--kind", "x"], 403),
                                     (robin, ["issue", "robin", "nobody"], 403),
                                     ({}, ["issue", "robin", "anthropic"], 403),
                                     (robin, ["issue"], 400)]:
            self.assertEqual(call(caller, argv)["status"], status, (caller, argv))
        self.assertEqual(call(robin, ["issue", "robin", "anthropic"], path="ui")["status"], 404)
        self.assertEqual(call({"kind": "owner"}, ["issue", "robin", "openai"])["status"], 200)
        self.assertEqual(len(ran), 2)

    def test_names_and_kinds_are_checked(self):
        d = subs.load()
        for bad in [lambda: subs.add_subscription(d, "Bad Name", "anthropic", ""),
                    lambda: subs.add_subscription(d, "x", "nope", ""),
                    lambda: subs.add_credential(d, "main", "magic", secret="s"),
                    lambda: subs.add_credential(d, "main", "long-lived"),
                    lambda: subs.add_credential(d, "ghost", "long-lived", secret="s")]:
            with self.assertRaises(ValueError):
                bad()

    def test_an_agent_owner_gets_their_agents_fuel_read_only(self):
        # loom's docs/specs/owners.md: Loren owns robin, who runs on main → spare.
        def tool(*args, stdin="{}"):
            r = subprocess.run([sys.executable, str(ROOT / "tools/loom-subscriptions"), *args], input=stdin,
                               capture_output=True, text=True, env=os.environ)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        loren = {"kind": "owner", "who": "loren", "agents": ["robin"]}
        self.assertEqual(tool("ui", "--json")["owners"], 2)
        page = tool("ui", "page", "--json", stdin=json.dumps({"path": "", "caller": loren}))
        self.assertEqual(page["title"], "Fuel")
        text = json.dumps(page, ensure_ascii=False)
        self.assertIn("The household's Max → The spare", text)
        self.assertNotIn("ChatGPT", text)
        self.assertNotIn("signin-start", text)
        act = lambda action, fields: tool("ui", "act", "--json", stdin=json.dumps({"action": action, "fields": fields, "caller": loren}))
        self.assertFalse(act("check", {"sub": "chatgpt"})["ok"])
        self.assertFalse(act("assign", {"agent": "robin", "order": "chatgpt"})["ok"])
        self.assertFalse(act("signin-start", {"sub": "main"})["ok"])
        self.assertEqual(subs.load()["assign"]["robin"], ["main", "spare"])
        from importlib.machinery import SourceFileLoader
        tool_mod = SourceFileLoader("loom_subscriptions_tool2", str(ROOT / "tools/loom-subscriptions")).load_module()
        ran = []
        # issue gives a credential's value: never to a person who owns only some agents, theirs included.
        for own in (["someone"], ["robin"]):
            r = tool_mod.api_answer(subs.load(), {"caller": {"kind": "owner", "agents": own}, "path": "run", "body": {"argv": ["issue", "robin", "anthropic"]}},
                                    lambda argv: ran.append(argv) or (0, "", ""))
            self.assertEqual(r["status"], 403)
        self.assertEqual(ran, [])

    def test_each_agents_fuel_is_on_its_own_tab_and_the_household_links_to_it(self):
        def tool(*args, stdin="{}", scope=None):
            env = dict(os.environ, **({"LOOM_SCOPE": scope} if scope else {}))
            r = subprocess.run([sys.executable, str(ROOT / "tools/loom-subscriptions"), *args], input=stdin,
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        m = tool("ui", "--json")
        self.assertEqual((m["tab"], m["agentTab"]), ({"label": "Subscriptions", "kind": "setting"}, {"label": "Fuel"}))
        home = tool("ui", "page", "--json", stdin=json.dumps({"path": ""}))
        who = [x for x in home["sections"] if x["id"] == "who"][0]
        self.assertEqual([i["page"] for i in who["blocks"][1]["items"]], ["agent:robin"])
        self.assertNotIn('"action": "assign"', json.dumps(home), "an order is set on the agent's own tab")
        page = tool("ui", "page", "--json", stdin=json.dumps({"path": ""}), scope="robin")
        form = [x for x in page["sections"] if x["id"] == "order"][0]["blocks"][0]
        self.assertEqual((form["hidden"], form["fields"][0]["value"]), ({"agent": "robin"}, "main, spare"))
        act = lambda fields, scope: tool("ui", "act", "--json", stdin=json.dumps({"action": "assign", "fields": fields}), scope=scope)
        self.assertFalse(act({"agent": "kim", "order": "main"}, "robin")["ok"], "robin's page sets robin's order only")
        self.assertTrue(act({"agent": "robin", "order": "spare, main"}, "robin")["ok"])
        self.assertEqual(subs.load()["assign"]["robin"], ["spare", "main"])
        loren = {"kind": "owner", "who": "loren", "agents": ["robin"]}
        ro = tool("ui", "page", "--json", stdin=json.dumps({"path": "", "caller": loren}), scope="robin")
        self.assertNotIn("order", [x["id"] for x in ro["sections"]], "an agent owner reads it, never sets it")

    def test_the_page_and_its_actions(self):
        def tool(*args, stdin="{}"):
            r = subprocess.run([sys.executable, str(ROOT / "tools/loom-subscriptions"), *args], input=stdin,
                               capture_output=True, text=True, env=os.environ)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout) if r.stdout.strip().startswith("{") else r.stdout
        m = tool("ui", "--json")
        self.assertEqual((m["v"], m["tab"]["label"], m["tile"]["value"]), (2, "Subscriptions", "3"))
        page = tool("ui", "page", "--json", stdin=json.dumps({"path": ""}))
        self.assertEqual([s["id"] for s in page["sections"]][:5], ["usage", "logins", "sub-chatgpt", "sub-main", "sub-spare"])
        # Logins: a status row a credential, the section toned by its worst; accounts are kv cards
        # with the credential table folded under Machine details.
        logins = page["sections"][1]["blocks"][0]
        self.assertEqual(logins["type"], "status")
        self.assertTrue(all(i["tone"] in ("ok", "warn", "bad") and i["text"] for i in logins["items"]))
        main = page["sections"][3]["blocks"]
        self.assertEqual((main[0]["type"], main[1]["type"], main[1]["summary"]), ("kv", "details", "Machine details"))
        self.assertNotIn("sk-ant", json.dumps(page))
        ok = tool("ui", "act", "--json", stdin=json.dumps({"action": "assign", "fields": {"agent": "robin", "order": "spare,main"}}))
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(subs.load()["assign"]["robin"], ["spare", "main"])
        bad = tool("ui", "act", "--json", stdin=json.dumps({"action": "assign", "fields": {"agent": "robin", "order": "ghost"}}))
        self.assertFalse(bad["ok"])
        added = tool("ui", "act", "--json", stdin=json.dumps({"action": "add", "fields": {"name": "third", "provider": "anthropic", "label": "Third"}}))
        self.assertTrue(added["ok"])
        self.assertIn("third", subs.load()["subscriptions"])
        self.assertEqual(tool("ui", "page", "--json", stdin=json.dumps({"path": "nope"}))["title"], "Not found")


    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "needs age")
    def test_its_part_of_the_household_bundle_round_trips_and_names_the_files_that_stay_behind(self):
        d = subs.load()
        subs.add_credential(d, "chatgpt", "device", file="/home/robin/.codex/auth.json")
        subs.save(d)
        key = self.home / "owner.key"
        r = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True)
        recipient = re.search(r"age1[0-9a-z]+", r.stderr + r.stdout).group(0)

        def tool(*args, stdin="", **env):
            return subprocess.run([sys.executable, str(ROOT / "tools/loom-subscriptions"), *args], input=stdin,
                                  capture_output=True, text=True, env={**os.environ, **env})
        self.assertNotEqual(tool("bundle", "export", LOOM_EXPORT_RECIPIENTS="").returncode, 0, "never in the clear")
        part = tool("bundle", "export", LOOM_EXPORT_RECIPIENTS=recipient)
        self.assertEqual(part.returncode, 0, part.stderr)
        self.assertTrue(part.stdout.startswith("-----BEGIN AGE ENCRYPTED FILE-----"))
        self.assertNotIn("claude-main", part.stdout)
        # Not over a household that already has its own.
        self.assertNotEqual(tool("bundle", "import", stdin=part.stdout, LOOM_IMPORT_IDENTITY=str(key)).returncode, 0)
        fresh = self.home / "fresh"
        r = tool("bundle", "import", stdin=part.stdout, LOOM_IMPORT_IDENTITY=str(key), LOOM_HOME=str(fresh))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((fresh / "subscriptions.json").read_text())["assign"]["robin"], ["main", "spare"])
        self.assertEqual(oct((fresh / "subscriptions.json").stat().st_mode & 0o777), "0o600")
        self.assertIn("/home/robin/.codex/auth.json", r.stdout)


class LoginWords(unittest.TestCase):
    """How a login reads on the page: how long it's good for, or what's wrong."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("loom_subscriptions_tool", str(ROOT / "tools/loom-subscriptions"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.tool = importlib.util.module_from_spec(spec)
        loader.exec_module(self.tool)

    def test_words_and_tones(self):
        now = 1_000_000_000
        login = self.tool._login
        self.assertEqual(login({"status": "valid", "expires": now + 41 * 86400 + 60}, now), ("ok", "Good for 41 days"))
        self.assertEqual(login({"status": "valid", "expires": now + 2 * 86400 + 60}, now), ("warn", "Expires in 2 days"))
        self.assertEqual(login({"status": "valid", "expires": now + 5 * 3600}, now), ("warn", "Expires in 5h"))
        self.assertEqual(login({"status": "valid", "expires": now - 1}, now), ("bad", "Expired"))
        self.assertEqual(login({"status": "expired"}, now), ("bad", "Expired"))
        self.assertEqual(login({"status": "unknown", "error": "URLError"}, now), ("warn", "Unknown (URLError)"))
        self.assertEqual(login({"status": "valid"}, now), ("ok", "Valid"))

    def test_usage_meters_tone_as_they_fill(self):
        d = {"subscriptions": {"main": {"label": "Main", "usage": {"5h": 0.27, "7d": 0.88, "checked": 1}}}}
        m = self.tool._meters(d, 100)
        self.assertEqual([(i["label"], i["value"], i.get("tone")) for i in m],
                         [("Main · 5 hours", 0.27, None), ("Main · 7 days", 0.88, "warn")])


class PlainWords(LoginWords):
    """Fuel's labels as a person reads them: no "0m ago", no 24-hour clock, no ISO date."""

    def test_under_a_minute_is_just_now(self):
        ago = self.tool._ago
        self.assertEqual([ago(1000 - s, 1000) for s in (0, 30, 59)], ["just now"] * 3)
        self.assertEqual((ago(1000 - 60, 1000), ago(1000 - 7200, 1000), ago(None, 1000)), ("1m ago", "2h ago", "never"))

    def test_a_meter_read_just_now_says_so(self):
        now = time.mktime((2026, 3, 2, 9, 0, 0, 0, 0, -1))
        reset = time.mktime((2026, 3, 2, 17, 0, 0, 0, 0, -1))   # a Monday, 5 PM local
        d = {"subscriptions": {"main": {"label": "Main", "usage": {"5h": 0.27, "reset_5h": reset, "checked": now}}}}
        self.assertEqual(self.tool._meters(d, now)[0]["detail"], "resets Mon 5:00 PM · read just now")

    def test_the_cli_usage_line_is_a_12_hour_clock(self):
        reset = time.mktime((2026, 3, 2, 17, 0, 0, 0, 0, -1))
        line = self.tool._usage({"usage": {"5h": 0.5, "reset_5h": reset, "checked": reset}}, reset)
        self.assertEqual(line, "5 hours 50% (resets Mon 5:00 PM), checked just now")

    def test_a_sign_in_note_dates_like_a_person(self):
        now = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
        note = self.tool._note
        self.assertEqual(note("signed in from loom's page 2026-10-25; refreshed only here", now),
                         "signed in from loom's page Oct 25; refreshed only here")
        self.assertEqual(note("this machine's session login, signed in from loom's page 2026-10-25", now),
                         "this machine's session login, signed in from loom's page Oct 25")
        self.assertEqual(note("signed in from loom's page 2025-12-03", now), "signed in from loom's page Dec 3, 2025")
        self.assertEqual(note("Pat's spare, 2026-10-25", now), "Pat's spare, 2026-10-25", "a note someone wrote stays as written")

    def test_the_credentials_table_shows_the_note_that_way(self):
        now = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
        d = {"subscriptions": {"main": {"label": "Main", "provider": "anthropic", "usage": {}, "credentials": [
            {"kind": "session", "where": {"secret": "x"}, "status": "valid", "checked": now - 5,
             "note": "signed in from loom's page 2026-10-25; refreshed only here"}]}}, "assign": {}}
        from unittest import mock
        with mock.patch.object(self.tool.signin, "installed", return_value=False):
            page = self.tool.overview(d, now)
        sub = next(s for s in page["sections"] if s["id"] == "sub-main")
        row = sub["blocks"][1]["blocks"][0]["rows"][0]
        self.assertEqual(row[3:], ["just now", "signed in from loom's page Oct 25; refreshed only here"])
        logins = next(s for s in page["sections"] if s["id"] == "logins")["blocks"][0]["items"]
        self.assertEqual(logins[0]["meta"], "just now")


class Tile(LoginWords):
    def test_home_tile_is_the_fullest_window(self):
        d = {"subscriptions": {
            "a": {"label": "Claude", "credentials": [], "usage": {"5h": 0.27, "7d": 0.62, "reset_7d": 1_000_000_000}},
            "b": {"label": "ChatGPT", "credentials": [], "usage": {}}}, "assign": {}}
        t = self.tool.manifest(d, 1)["tile"]
        self.assertEqual((t["value"], t["tone"]), ("62%", "ok"))
        self.assertTrue(t["detail"].startswith("Claude · 7 days, resets "))
        d["subscriptions"]["a"]["usage"]["5h"] = 0.9
        self.assertEqual(self.tool.manifest(d, 1)["tile"]["tone"], "warn")
        self.assertEqual(self.tool.manifest({"subscriptions": {}, "assign": {}}, 1)["tile"]["detail"], "none yet")


class Fuel(LoginWords):
    """Each agent's Fuel on loom's Home: what it runs on now, and how much of the week is left."""

    def d(self):
        good = [{"kind": "long-lived", "status": "valid", "where": {"secret": "x"}}]
        return {"subscriptions": {
            "claude": {"label": "Claude", "credentials": good, "usage": {"5h": 0.03, "7d": 0.42, "reset_7d": 1_000_000_000}},
            "gpt": {"label": "ChatGPT", "credentials": good, "usage": {}}},
            "assign": {"ana": ["claude", "gpt"], "bo": ["gpt", "claude"]}}

    def test_fuel_is_the_first_with_room(self):
        f = self.tool.fuel(self.d())
        self.assertEqual(f["ana"]["readings"][0]["value"], "Claude: 58% of the week left")
        self.assertTrue(f["ana"]["readings"][0]["detail"].startswith("5h 3% used · resets "))
        self.assertEqual(f["ana"]["readings"][0]["tone"], "ok")
        self.assertNotIn("state", f["ana"])
        self.assertEqual(f["bo"]["readings"][0]["value"], "ChatGPT: usage not read yet")
        self.assertNotIn("tone", f["bo"]["readings"][0], "unread usage isn't green")

    def test_a_full_one_passes_to_the_next_and_none_left_is_unavailable(self):
        d = self.d()
        d["subscriptions"]["claude"]["usage"]["7d"] = 0.99
        self.assertEqual(self.tool.fuel(d)["ana"]["readings"][0]["value"], "ChatGPT: usage not read yet")
        d["subscriptions"]["gpt"]["usage"] = {"5h": 0.98}
        ana = self.tool.fuel(d)["ana"]
        self.assertEqual(ana["state"], "unavailable")
        self.assertTrue(ana["reason"].startswith("out of fuel: Claude, ChatGPT full until "))
        for s in d["subscriptions"].values():
            s["credentials"][0]["status"] = "expired"
        self.assertEqual(self.tool.fuel(d)["bo"]["reason"], "every login it runs on is expired")

    def test_the_manifest_carries_it(self):
        self.assertIn("ana", self.tool.manifest(self.d(), 1)["agents"])


class ExpiredAsk(LoginWords):
    def test_an_expired_login_asks_with_a_renew_button(self):
        d = {"subscriptions": {"main": {"label": "Main", "provider": "anthropic", "usage": {},
                                        "credentials": [{"kind": "long-lived", "where": {"secret": "x"}, "status": "expired"}]}}, "assign": {}}
        from unittest import mock
        with mock.patch.object(self.tool.signin, "installed", return_value=True):
            [ask] = self.tool.manifest(d, 1)["asks"]
        self.assertEqual((ask["tone"], ask["actions"][0]["action"], ask["actions"][0]["fields"]), ("bad", "signin-start", {"sub": "main"}))
        with mock.patch.object(self.tool.signin, "installed", return_value=False):
            self.assertNotIn("actions", self.tool.manifest(d, 1)["asks"][0])

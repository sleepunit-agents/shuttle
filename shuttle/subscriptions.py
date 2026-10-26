"""Subscriptions: the model accounts the household's agents run on.

A **subscription** is one account with a provider (an Anthropic Claude plan,
an OpenAI ChatGPT plan): whose it is, what plan, and its **credentials**, the
ways in. Claude has two kinds: a long-lived token (`claude setup-token`:
about a year, inference only, what houses and bridges run on) and a session
login (`claude auth login`: short-lived with a refresh token, the whole
account; remote-control needs it). Each credential has a status (valid,
expired, unknown) from its last check, and each subscription its **usage**:
the 5-hour and 7-day windows, read from the rate-limit headers of a one-token
call (a long-lived token can't read the usage endpoint).

Each agent has an ordered list of subscriptions; `pick` is the first with a
valid credential and room left, so one running out falls to the next, and
spreading load is a matter of order. Only names are kept here (secret names
in the secrets store, or a file's path), never a value:

    $LOOM_HOME/subscriptions.json   (default ~/.config/loom; 0600)

    python3 -m shuttle.subscriptions check [SUB]    probe every credential, record status and usage
    python3 -m shuttle.subscriptions pick AGENT [PROVIDER]   the secret to use now (prints its name)
    python3 -m shuttle.subscriptions issue AGENT PROVIDER [--kind session]   what a house gets (value on stdout)
"""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROVIDERS = {"anthropic": "Claude", "openai": "OpenAI"}
KINDS = {"long-lived": "long-lived token", "session": "session login", "api-key": "API key", "device": "device login"}
NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
API = "https://api.anthropic.com/v1/messages"
H = "anthropic-ratelimit-unified-"
PROBE_MODEL = "claude-haiku-5-5"
# Codex's own login (open source: codex-rs login): its public client, and the
# token endpoint its refresh calls. The refresh here does exactly what it does.
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# A house's copy of an OpenAI login carries no refresh token (so it can't rotate
# the one loom's machine holds), so it must carry plenty of access time: the
# login is refreshed here when under min(24h, half its lifetime) is left (as
# fletch does, fwdai/fletch#885: about one refresh every 9 days).
ISSUE_MIN_S = 24 * 3600
FULL = 0.97   # a window this used counts as out of room
# Claude Code's own login (its public client and token endpoint), for a session
# login the broker holds in the store: it alone refreshes it, and a house gets
# only the access token (a view with no refresh token), so nothing races it.
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Every outbound call names its client: Cloudflare in front of platform.claude.com
# answers Python's default User-Agent with 403 "error code: 1010", before the auth
# server sees the request (2026-10-26: the broker's first refresh never got through).
USER_AGENT = "claude-cli (loom subscriptions)"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# A session access token lives about 8 hours: a house's view gets at least this
# much, and the check (every 30 minutes) refreshes under it, which also keeps the
# refresh token from lapsing unused.
CLAUDE_ISSUE_MIN_S = 4 * 3600


def path():
    return Path(os.environ.get("LOOM_HOME") or Path.home() / ".config/loom") / "subscriptions.json"


def load():
    try:
        d = json.loads(path().read_text())
        if isinstance(d, dict) and d.get("v") == 1:
            d.setdefault("subscriptions", {})
            d.setdefault("assign", {})
            return d
    except (OSError, ValueError):
        pass
    return {"v": 1, "subscriptions": {}, "assign": {}}


def save(d):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    tmp.replace(p)


def add_subscription(d, name, provider, label, owner="", plan="", email=""):
    if not NAME.match(name or ""):
        raise ValueError("a subscription's name is lowercase letters, digits and dashes, like home-max")
    if provider not in PROVIDERS:
        raise ValueError(f"provider is one of {', '.join(PROVIDERS)}")
    sub = d["subscriptions"].setdefault(name, {"credentials": []})
    sub.update({"provider": provider, "label": (label or name)[:80], "owner": owner[:60], "plan": plan[:40], "email": email[:120]})
    return sub


def add_credential(d, sub, kind, secret=None, file=None, key=None, note="", expires=None):
    """A way into the subscription: a secret in the store, or a file (KEY=VALUE when `key`)."""
    if sub not in d["subscriptions"]:
        raise ValueError(f"no subscription {sub}")
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    if bool(secret) == bool(file):
        raise ValueError("a credential is a secret's name or a file, one of them")
    creds = d["subscriptions"][sub]["credentials"]
    where = {"secret": secret} if secret else {"file": file, **({"key": key} if key else {})}
    for c in creds:
        if c["where"] == where:
            c.update(kind=kind, note=note or c.get("note", ""))
            return c
    c = {"kind": kind, "where": where, "note": note, "added": int(time.time()), "status": "unknown"}
    if expires:
        c["expires"] = expires
    creds.append(c)
    return c


def describe(c):
    w = c["where"]
    return f"secret {w['secret']}" if "secret" in w else f"{w['file']}" + (f" ({w['key']})" if w.get("key") else "")


def value(c, read_secret):
    """A credential's value, for checking it here; never stored or shown."""
    w = c["where"]
    if "secret" in w:
        return read_secret(w["secret"]).strip()
    text = Path(w["file"]).expanduser().read_text()
    if not w.get("key"):
        return text.strip()
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == w["key"]:
            return v.strip().strip('"')
    raise KeyError(f"{w['key']} isn't in {w['file']}")


def probe_anthropic(token, urlopen=urllib.request.urlopen):
    """(status, usage) for a Claude long-lived token: one max_tokens=1 call."""
    body = {"model": PROBE_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "."}],
            "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json", "User-Agent": USER_AGENT, "x-app": "cli"})
    try:
        with urlopen(req, timeout=15) as r:
            status, headers = "valid", dict(r.headers.items())
    except urllib.error.HTTPError as e:
        headers = dict(e.headers.items())
        status = "expired" if e.code in (401, 403) else "valid" if e.code == 429 else f"error {e.code}"
    h = {k.lower(): v for k, v in headers.items()}
    usage = {}
    for w in ("5h", "7d"):
        try:
            usage[w] = float(h[f"{H}{w}-utilization"])
            usage[f"reset_{w}"] = int(float(h[f"{H}{w}-reset"]))
        except (KeyError, ValueError):
            pass
    return status, usage


def _epoch(v):
    """A timestamp in seconds, whether it was written in seconds or milliseconds."""
    v = float(v)
    return v / 1000 if v > 1e11 else v


def check_session(c, now):
    """A Claude session login, from its credentials file alone: never its token, and
    never a refresh (only the login's own owner may refresh it: the token rotates).
    Returns (status, facts about the plan) or None when it isn't that kind of file."""
    if "file" not in c["where"]:
        return None
    o = json.loads(Path(c["where"]["file"]).expanduser().read_text()).get("claudeAiOauth")
    if not isinstance(o, dict):
        return None
    if not o.get("refreshToken"):
        return "expired", {}
    until = o.get("refreshTokenExpiresAt")
    status = "expired" if until and _epoch(until) <= now else "valid"
    facts = {k: o[k] for k in ("subscriptionType", "rateLimitTier") if o.get(k)}
    if until:
        c["expires"] = int(_epoch(until))
    c["scopes"] = [x for x in o.get("scopes", []) if isinstance(x, str)][:12]
    return status, facts


def _jwt_claims(token):
    import base64
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, AttributeError):
        return {}


def check_openai(auth, now):
    """A Codex (ChatGPT) login, from its auth.json: (status, facts, access expiry)."""
    t = auth.get("tokens") or {}
    if not isinstance(t, dict) or not t.get("refresh_token"):
        return "expired", {}, None
    claims = _jwt_claims(t.get("id_token", ""))
    plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
    access = _jwt_claims(t.get("access_token", "")).get("exp") or claims.get("exp")
    return "valid", ({"plan": plan} if plan else {}), access


def refresh_openai(auth, post=None):
    """A fresh access token for a Codex login (the CLI's own refresh). Returns the
    new auth.json; the refresh token in it may be new (they rotate), so the
    caller stores it before it lets the old one go."""
    t = auth["tokens"]
    body = json.dumps({"client_id": OPENAI_CLIENT_ID, "grant_type": "refresh_token",
                       "refresh_token": t["refresh_token"], "scope": "openid profile email"}).encode()
    req = urllib.request.Request(OPENAI_TOKEN_URL, data=body, method="POST", headers={"content-type": "application/json", "User-Agent": USER_AGENT})
    with (post or urllib.request.urlopen)(req, timeout=20) as r:
        got = json.loads(r.read())
    new = dict(auth)
    new["tokens"] = {**t, **{k: got[k] for k in ("id_token", "access_token", "refresh_token") if got.get(k)}}
    new["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return new


def refresh_claude(o, post=None, now=None):
    """A fresh access token for a Claude session login (claudeAiOauth), the way
    Claude Code refreshes. The refresh token rotates: the caller stores the
    result before anything else."""
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": o["refreshToken"],
                       "client_id": CLAUDE_CLIENT_ID, "scope": CLAUDE_SCOPES}).encode()
    req = urllib.request.Request(CLAUDE_TOKEN_URL, data=body, method="POST", headers={"content-type": "application/json", "User-Agent": USER_AGENT})
    with (post or urllib.request.urlopen)(req, timeout=20) as r:
        got = json.loads(r.read())
    new = dict(o)
    new["accessToken"] = got["access_token"]
    new["refreshToken"] = got.get("refresh_token") or o["refreshToken"]
    new["expiresAt"] = int((now or time.time()) + int(got.get("expires_in", 28800))) * 1000
    if got.get("scope"):
        new["scopes"] = got["scope"].split()
    return new


def claude_usage(token, urlopen=urllib.request.urlopen):
    """The account's usage windows, as fractions, from the usage endpoint (a
    session token can read it; a long-lived one can't)."""
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={"authorization": f"Bearer {token}",
                                                            "anthropic-beta": "oauth-2025-04-20", "User-Agent": USER_AGENT})
    with urlopen(req, timeout=20) as r:
        got = json.loads(r.read())
    u = {"via": "usage endpoint"}
    for key, w in (("five_hour", "5h"), ("seven_day", "7d"), ("seven_day_opus", "7d_opus"), ("seven_day_sonnet", "7d_sonnet")):
        win = got.get(key)
        if isinstance(win, dict) and win.get("utilization") is not None:
            u[w] = round(float(win["utilization"]) / 100, 4)
            if win.get("resets_at"):
                from datetime import datetime
                u[f"reset_{w}"] = int(datetime.fromisoformat(win["resets_at"].replace("Z", "+00:00")).timestamp())
    return u


def _keep(secret, text):
    """A just-rotated login, on disk until the store has it, so a failed write can't lose it."""
    keep = path().with_name(f".refreshed-{secret}")
    fd = os.open(keep, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return keep


@contextlib.contextmanager
def refresh_lock(secret):
    """One refresh of a login at a time, across processes (fletch's single-flight)."""
    path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path().with_name(f".refresh-{secret}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def locked_refresh(secret, read_secret, store, stale, refresh, token):
    """A stored login, refreshed and stored back if stale(login). Its refresh
    token rotates, so two refreshes at once spend it twice (a refusal, or at
    OpenAI a reuse): under a lock per login it's read inside the lock, and a
    second caller finds the first's fresh login. A refusal is retried once, if
    the stored login changed meanwhile (rotated somewhere else). Returns
    (login, whether this call refreshed it)."""
    with refresh_lock(secret):
        login = json.loads(read_secret(secret).strip())
        if not stale(login):
            return login, False
        try:
            new = refresh(login)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 401):
                raise
            again = json.loads(read_secret(secret).strip())
            if token(again) == token(login):
                raise
            if not stale(again):
                return again, False
            new = refresh(again)
        # Kept on disk until the store has it, so a failed write can't lose it.
        text = json.dumps(new)
        keep = _keep(secret, text)
        store(secret, text)
        keep.unlink()
        return new, True


def fresh_claude_session(c, read_secret, store, now, post=None):
    """A store-held Claude session login, refreshed (and stored back) when under
    CLAUDE_ISSUE_MIN_S is left. Returns its claudeAiOauth."""
    def stale(login):
        o = login["claudeAiOauth"]
        if not o.get("refreshToken"):
            raise ValueError("a session login with no refresh token")
        return _epoch(o.get("expiresAt") or 0) - now < CLAUDE_ISSUE_MIN_S
    login, refreshed = locked_refresh(c["where"]["secret"], read_secret, store, stale,
                                      lambda l: {**l, "claudeAiOauth": refresh_claude(l["claudeAiOauth"], post, now)},
                                      lambda l: l["claudeAiOauth"].get("refreshToken"))
    o = login["claudeAiOauth"]
    if refreshed:
        c["refreshed"] = int(now)
    c.update(status="valid", checked=int(now), access_until=int(_epoch(o["expiresAt"])))
    c["scopes"] = [x for x in o.get("scopes", []) if isinstance(x, str)][:12]
    return o


def check(d, read_secret, only=None, probe=probe_anthropic, now=None, store=None, post=None, read_usage=None):
    """Check every credential (that can be checked) and record status and usage."""
    now = int(now or time.time())
    for name, sub in d["subscriptions"].items():
        if only and name != only:
            continue
        for c in sub["credentials"]:
            if sub["provider"] == "anthropic" and c["kind"] == "session" and "secret" in c["where"] and store:
                try:
                    o = fresh_claude_session(c, read_secret, store, now, post)
                    c.pop("error", None)
                except Exception as e:  # only the kind: the text could carry a token
                    c.update(status="unknown", checked=now, error=type(e).__name__)
                    continue
                if o.get("subscriptionType") and not sub.get("plan"):
                    sub["plan"] = o["subscriptionType"].capitalize() + (f" ({o['rateLimitTier']})" if o.get("rateLimitTier") else "")
                try:
                    sub["usage"] = {**(read_usage or claude_usage)(o["accessToken"]), "checked": now}
                except Exception as e:
                    c["error"] = "usage: " + type(e).__name__
                continue
            if sub["provider"] == "anthropic" and c["kind"] == "session":
                try:
                    got = check_session(c, now)
                except (OSError, ValueError) as e:
                    got = ("unknown", {})
                    c["error"] = type(e).__name__
                if got:
                    c["status"], facts = got[0], got[1]
                    c["checked"] = now
                    c.pop("error", None) if got[0] != "unknown" else None
                    if facts.get("subscriptionType") and not sub.get("plan"):
                        sub["plan"] = facts["subscriptionType"].capitalize() + (f" ({facts['rateLimitTier']})" if facts.get("rateLimitTier") else "")
                continue
            if sub["provider"] == "openai" and c["kind"] == "device":
                try:
                    status, facts, access = check_openai(json.loads(value(c, read_secret)), now)
                    c.pop("error", None)
                except (OSError, ValueError, KeyError) as e:
                    status, facts, access = "unknown", {}, None
                    c["error"] = type(e).__name__
                c.update(status=status, checked=now)
                if access:
                    c["access_until"] = int(access)
                if facts.get("plan") and not sub.get("plan"):
                    sub["plan"] = facts["plan"].capitalize()
                continue
            if sub["provider"] != "anthropic" or c["kind"] != "long-lived":
                continue
            try:
                status, usage = probe(value(c, read_secret))
            except Exception as e:  # unreadable, unreachable: say so, keep going
                # Only the exception's kind: its text could carry the value it was handed.
                status, usage = "unknown", {}
                c["error"] = type(e).__name__
            else:
                c.pop("error", None)
            c.update(status=status, checked=now)
            # The usage endpoint's reading (a session login's) is the better one: keep it.
            if usage and not (sub.get("usage", {}).get("via") == "usage endpoint" and sub["usage"].get("checked") == now):
                sub["usage"] = {**usage, "checked": now}
    return d


def has_room(sub):
    u = sub.get("usage") or {}
    return all(u.get(w) is None or u[w] < FULL for w in ("5h", "7d"))


# What a body runs on, by provider: Claude's long-lived token, OpenAI's device login or an API key.
RUNS_ON = {"anthropic": ("long-lived",), "openai": ("device", "api-key")}


def pick(d, agent, provider="anthropic", kinds=None):
    """The secret an agent's next body should run on: its first subscription
    (in its order) of that provider with a valid credential a body can run on
    and room left; else the first with one that isn't expired. An agent whose
    order names none of the provider's uses any of them."""
    order = [n for n in d["assign"].get(agent, []) if d["subscriptions"].get(n, {}).get("provider") == provider] \
        or sorted(n for n, s in d["subscriptions"].items() if s["provider"] == provider)
    kinds = kinds or RUNS_ON.get(provider, ())
    fallback = None
    for name in order:
        sub = d["subscriptions"][name]
        for c in sub["credentials"]:
            if c["kind"] not in kinds or "secret" not in c["where"] or c.get("status") == "expired":
                continue
            if c.get("status") == "valid" and has_room(sub):
                return name, c["where"]["secret"]
            fallback = fallback or (name, c["where"]["secret"])
    return fallback or (None, None)


def issue(d, agent, provider, read_secret, store, now=None, post=None, kind=None):
    """What a house of this agent gets for a provider: (subscription, secret
    name, value). For Claude, the long-lived token pick chooses. For OpenAI,
    the Codex login, refreshed here first if its access has under
    ISSUE_MIN_S left (and stored back before anything else), with its refresh
    token taken out, so nothing in a house can rotate it. With kind "session"
    (Claude): the broker's session login, refreshed here the same way, as a
    credentials.json view with the access token only (remote-control needs a
    session login; the long-lived token can't)."""
    now = now or time.time()
    name, secret = pick(d, agent, provider, (kind,) if kind else None)
    if not secret:
        raise LookupError(f"no {provider} {kind or ''} subscription for {agent}".replace("  ", " "))
    if provider == "anthropic" and kind == "session":
        c = next(c for c in d["subscriptions"][name]["credentials"] if c["where"].get("secret") == secret)
        o = fresh_claude_session(c, read_secret, store, now, post)
        # An allowlist: what Claude Code reads, never the refresh token.
        view = {k: o[k] for k in ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier") if k in o}
        return name, secret, json.dumps({"claudeAiOauth": {**view, "refreshToken": ""}})
    if provider != "openai":
        return name, secret, read_secret(secret).strip()
    def stale(auth):
        _, _, access = check_openai(auth, now)
        claims = _jwt_claims(auth["tokens"].get("access_token", ""))
        life = (claims.get("exp", 0) - claims.get("iat", 0)) if claims.get("iat") else 2 * ISSUE_MIN_S
        return not access or access - now < min(ISSUE_MIN_S, life / 2)
    auth, refreshed = locked_refresh(secret, read_secret, store, stale, lambda a: refresh_openai(a, post),
                                     lambda a: a["tokens"].get("refresh_token"))
    if refreshed:
        for c in d["subscriptions"][name]["credentials"]:
            if c["where"].get("secret") == secret:
                c.update(status="valid", checked=int(now), refreshed=int(now))
                _, _, access = check_openai(auth, now)
                c["access_until"] = int(access) if access else None
    # An allowlist, not a copy minus one field: only what Codex needs to call,
    # never the refresh token or an API key the login file may also carry.
    t = auth["tokens"]
    house = {"auth_mode": auth.get("auth_mode") or "chatgpt", "OPENAI_API_KEY": None, "last_refresh": auth.get("last_refresh"),
             "tokens": {"access_token": t["access_token"], "id_token": t.get("id_token", ""),
                        "account_id": t.get("account_id"), "refresh_token": ""}}
    return name, secret, json.dumps(house)


def store_secret(name, value):
    """Into the secrets store, on stdin."""
    r = subprocess.run([os.environ.get("SHUTTLE_SIGNIN_LOOM", "loom"), "secrets", "add", name, "--force"],
                       input=value, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("the secrets store refused the refreshed login")


def secret_reader():
    """How to read a secret here: the shuttle instance's way when there is one,
    else the secrets store itself (this runs host-side, on loom's machine), so a
    house's claim never hangs on a missing SHUTTLE_INSTANCE."""
    if os.environ.get("SHUTTLE_INSTANCE"):
        from shuttle import instance
        return instance.load().read_secret

    def read(name):
        r = subprocess.run(["loom-secrets", "get", name, "--reveal"], capture_output=True, text=True, timeout=20,
                           env={**os.environ, "LOOM_SECRETS_ALLOW_GET": "1"})
        if r.returncode != 0:
            raise OSError(f"the secrets store has no {name}")
        return r.stdout
    return read


def main(argv):
    d = load()
    if argv[:1] == ["check"]:
        check(d, secret_reader(), argv[1] if len(argv) > 1 else None, store=store_secret)
        save(d)
        for name, sub in sorted(d["subscriptions"].items()):
            u = sub.get("usage") or {}
            print(f"{name}: " + ", ".join(f"{describe(c)} {c['status']}" for c in sub["credentials"])
                  + (f"; 5h {u['5h']:.0%}, 7d {u['7d']:.0%}" if "5h" in u and "7d" in u else ""))
        return 0
    kind = argv[argv.index("--kind") + 1] if "--kind" in argv[:-1] else None
    argv = [a for i, a in enumerate(argv) if a != "--kind" and (i == 0 or argv[i - 1] != "--kind")]
    if argv[:1] == ["issue"] and len(argv) == 3:
        try:
            name, secret, val = issue(d, argv[1], argv[2], secret_reader(), store_secret, kind=kind)
        except (LookupError, urllib.error.URLError, KeyError, ValueError, RuntimeError) as e:
            print(f"subscriptions: can't issue {argv[2]} for {argv[1]}: {type(e).__name__}: {str(e)[:120] if isinstance(e, LookupError) else ''}", file=sys.stderr)
            return 1
        save(d)
        print(f"issued {name}/{secret}", file=sys.stderr)
        sys.stdout.write(val)
        return 0
    if argv[:1] == ["pick"] and len(argv) in (2, 3):
        name, secret = pick(d, argv[1], argv[2] if len(argv) == 3 else "anthropic")
        if not secret:
            print(f"no {argv[2] if len(argv) == 3 else 'anthropic'} subscription for {argv[1]}", file=sys.stderr)
            return 1
        print(secret)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Signing a harness in from loom's page: click, do the provider's own login,
and the token lands in the secrets store, where the houses get it. Never back
on the page.

Each runner kind says how its login goes (HARNESSES below). There are two shapes:
  paste-code  the CLI prints a link; you sign in; the provider shows a code;
              you paste it back and the CLI prints the token (claude setup-token).
  device      the CLI prints a link and a code to enter there; it finishes on
              its own and leaves its credential in a file (codex login --device-auth).

The login is the harness's own CLI, run here in a pty by a small detached
helper, because a sign-in takes minutes and a page action gets seconds. The
page and the helper talk through a state file per sign-in (0600, in a 0700
dir): the link, then the outcome. A pasted code goes through a file the
helper reads once and deletes. Nothing that's written there is a token.

    python3 -m shuttle.signin run <harness> <id>     (the helper; started by start())
"""
import json
import os
import pty
import re
import secrets as _secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HARNESSES = {
    "claude": {
        "label": "Claude",
        "kind": "paste-code",
        "command": ["claude", "setup-token"],
        "link": re.compile(r"https://claude\.com/cai/oauth/authorize\?\S+"),
        "token": re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,}"),
        "secret": "claude-oauth-token",
        "note": "Sign in at the link; Claude then shows a code to paste here.",
    },
    # The session login of the machine the sign-in runs on (loom's): the whole
    # account (remote-control needs it). Its refresh token rotates, so it has
    # one owner, this machine's own claude, which keeps it in its credentials
    # file and refreshes itself. Nothing goes to the secrets store.
    "claude-session": {
        "label": "Claude (this machine's session login)",
        "kind": "paste-login",
        "command": ["claude", "auth", "login", "--claudeai"],
        "link": re.compile(r"https://claude\.(?:com|ai)/\S*oauth/authorize\?\S+"),
        "status": ["claude", "auth", "status"],
        "file": "~/.claude/.credentials.json",
        "secret": None,
        "note": "Sign in at the link; Claude then shows a code to paste here. This machine keeps the login and refreshes it.",
    },
    # A Claude session login of loom's own, for houses (remote-control): signed in
    # into a private config dir, then kept in the secrets store, where the
    # subscriptions broker is its only refresher. A second login of the account,
    # never this machine's own (one refresher per login).
    "claude-broker": {
        "label": "Claude (a session login for houses)",
        "kind": "paste-login",
        "command": ["claude", "auth", "login", "--claudeai"],
        "link": re.compile(r"https://claude\.(?:com|ai)/\S*oauth/authorize\?\S+"),
        "status": ["claude", "auth", "status"],
        "home_env": "CLAUDE_CONFIG_DIR",
        "file": ".credentials.json",
        "secret": "claude-session.json",
        "token": re.compile(r"sk-ant-o[a-z]t01-[A-Za-z0-9_-]{20,}"),
        "note": "Sign in at the link; Claude then shows a code to paste here. Loom keeps this login and refreshes it for the houses.",
    },
    "codex": {
        "label": "Codex (OpenAI)",
        "kind": "device",
        "command": ["codex", "login", "--device-auth"],
        "link": re.compile(r"https://auth\.openai\.com/\S+"),
        "user_code": re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b"),
        "file": "auth.json",   # left in $CODEX_HOME when it's done
        "home_env": "CODEX_HOME",
        "secret": "codex-auth.json",
        "note": "Open the link and enter the code shown; this page finishes on its own.",
    },
}

CODE = re.compile(r"^[A-Za-z0-9#_.\-]{8,400}$")
ID = re.compile(r"^[0-9a-f]{16}$")
WAIT_CODE_S = 15 * 60
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")


def state_dir():
    d = Path(os.environ.get("SHUTTLE_SIGNIN_DIR") or Path.home() / ".local/state/shuttle-signin")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def _write(path, data):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    tmp.replace(path)


def read(sid):
    if not ID.match(sid or ""):
        return None
    try:
        return json.loads((state_dir() / f"{sid}.json").read_text())
    except (OSError, ValueError):
        return None


def installed(harness):
    h = HARNESSES.get(harness)
    return bool(h) and shutil.which(h["command"][0]) is not None


def start(harness, wait_s=12, secret=None, subscription=None):
    """Start a sign-in; returns (id, state) once the link is up (or it failed).
    `secret` names where the token goes (default the harness's own), and
    `subscription` registers it as that subscription's credential (shuttle.subscriptions)."""
    if harness not in HARNESSES:
        raise ValueError(f"no harness {harness}")
    if not installed(harness):
        raise ValueError(f"{HARNESSES[harness]['command'][0]} isn't installed here")
    sid = _secrets.token_hex(8)
    if secret is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", secret):
        raise ValueError("a secret's name is letters, digits, dots, dashes and underscores")
    target = {"secret": secret or HARNESSES[harness]["secret"], **({"subscription": subscription} if subscription else {})}
    if not target["secret"]:
        target.pop("secret")
    _write(state_dir() / f"{sid}.json", {"harness": harness, "status": "starting", "at": time.time(), **target})
    subprocess.Popen([sys.executable, "-m", "shuttle.signin", "run", harness, sid],
                     cwd=str(Path(__file__).resolve().parent.parent), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + wait_s
    while time.time() < end:
        st = read(sid) or {}
        if st.get("status") != "starting":
            return sid, st
        time.sleep(0.2)
    return sid, read(sid) or {}


def submit_code(sid, code, wait_s=10):
    """Hand the pasted code to the helper; returns the state after (done, failed, or still waiting)."""
    st = read(sid)
    if not st or st.get("status") != "waiting":
        return st
    if HARNESSES[st["harness"]]["kind"] not in ("paste-code", "paste-login"):
        return st
    code = (code or "").strip()
    if not CODE.match(code):
        return {**st, "status": "waiting", "error": "That doesn't look like the code the sign-in page showed."}
    p = state_dir() / f"{sid}.code"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(code)
    end = time.time() + wait_s
    while time.time() < end:
        st = read(sid) or {}
        if st.get("status") in ("done", "failed"):
            return st
        time.sleep(0.25)
    return read(sid)


def _store(name, value):
    """Into the secrets store, on stdin: never argv, never a file."""
    r = subprocess.run([os.environ.get("SHUTTLE_SIGNIN_LOOM", "loom"), "secrets", "add", name, "--force"],
                       input=value, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"the secrets store said no: {(r.stderr or r.stdout).strip()[-200:]}")


def _register_file(st, file):
    """A session login this machine keeps itself becomes the subscription's credential (a file)."""
    if not st.get("subscription"):
        return
    from shuttle import subscriptions
    d = subscriptions.load()
    c = subscriptions.add_credential(d, st["subscription"], "session", file=str(Path(file).expanduser()),
                                     note=f"this machine's session login, signed in from loom's page {time.strftime('%Y-%m-%d')}")
    c.update(status="valid", checked=int(time.time()))
    subscriptions.save(d)


def _register(st, kind):
    """A sign-in for a subscription becomes one of its credentials (valid as of now)."""
    if not st.get("subscription"):
        return
    from shuttle import subscriptions
    d = subscriptions.load()
    c = subscriptions.add_credential(d, st["subscription"], kind, secret=st["secret"],
                                     note=f"signed in from loom's page {time.strftime('%Y-%m-%d')}")
    c.update(status="valid", checked=int(time.time()))
    subscriptions.save(d)


def _scrub(text, h):
    """What may be said about a failure: never anything shaped like the token."""
    if "token" in h:
        text = h["token"].sub("<token>", text)
    return text.strip()[-300:]


def run(harness, sid):
    """The helper: drive the harness's login in a pty, then store what it gives."""
    h = HARNESSES[harness]
    path = state_dir() / f"{sid}.json"
    code_file = state_dir() / f"{sid}.code"
    first = read(sid) or {}
    st = {"harness": harness, "at": time.time(), **({"secret": first.get("secret") or h["secret"]} if (first.get("secret") or h["secret"]) else {}),
          **({"subscription": first["subscription"]} if first.get("subscription") else {})}
    env = dict(os.environ, BROWSER="/bin/false", NO_COLOR="1")
    home = None
    if h.get("home_env"):
        home = tempfile.mkdtemp(prefix="signin-")
        env[h["home_env"]] = home
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(h["command"][0], h["command"], env)
    # A wide terminal, so the link comes out on one line.
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
    out = ""
    raw = ""

    def pump(timeout):
        # Escapes are stripped from the whole output, not chunk by chunk: one
        # split across two reads would otherwise stay in, splitting a token.
        nonlocal out, raw
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            try:
                raw += os.read(fd, 65536).decode(errors="replace")
            except OSError:
                return False
            out = ANSI.sub("", raw)
        return True

    def finish(status, **extra):
        # What the CLI said, scrubbed, beside the state: so a failure can be read afterwards.
        try:
            dbg = state_dir() / f"{sid}.log"
            fd2 = os.open(dbg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd2, "w") as f:
                f.write(_scrub(out[-6000:], h) if "token" in h else out[-6000:])
        except OSError:
            pass
        _write(path, {**st, "status": status, "at": time.time(), **extra})
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        code_file.unlink(missing_ok=True)
        if home:
            shutil.rmtree(home, ignore_errors=True)

    try:
        end = time.time() + 30
        link = None
        while time.time() < end and not link:
            if not pump(0.5):
                break
            m = h["link"].search(out)
            link = m.group(0) if m else None
        if not link:
            return finish("failed", error=f"{h['command'][0]} didn't give a sign-in link: {_scrub(out, h)}")
        st["link"] = link
        if h["kind"] == "device":
            m = h["user_code"].search(out[out.find(link):] if link in out else out)
            st["user_code"] = m.group(0) if m else None
        _write(path, {**st, "status": "waiting", "at": time.time()})

        end = time.time() + WAIT_CODE_S
        if h["kind"] == "paste-login":
            while time.time() < end and not code_file.exists():
                pump(0.5)
            if not code_file.exists():
                return finish("failed", error="No code came back in 15 minutes; start again.")
            code = code_file.read_text().strip()
            code_file.unlink(missing_ok=True)
            _write(path, {**st, "status": "checking", "at": time.time()})
            os.write(fd, code.encode())
            pump(0.5)
            os.write(fd, b"\r")
            # Done is when this machine's claude says it's logged in (it wrote its own file).
            end = time.time() + 180
            while time.time() < end:
                pump(2.0)
                r = subprocess.run(h["status"], capture_output=True, text=True, timeout=30, env=env)
                try:
                    if json.loads(r.stdout).get("loggedIn"):
                        if home:
                            # Its own login: into the store, and the private dir goes with finish().
                            _store(st["secret"], (Path(home) / h["file"]).read_text())
                            _register(st, "session")
                        else:
                            _register_file(st, h["file"])
                        return finish("done")
                except ValueError:
                    pass
            return finish("failed", error=f"claude didn't say it was logged in: {_scrub(out[-400:], h)}")
        if h["kind"] == "paste-code":
            while time.time() < end and not code_file.exists():
                pump(0.5)
            if not code_file.exists():
                return finish("failed", error="No code came back in 15 minutes; start again.")
            code = code_file.read_text().strip()
            code_file.unlink(missing_ok=True)
            _write(path, {**st, "status": "checking", "at": time.time()})
            # The code, then Enter on its own: a TUI can take text and a newline in one
            # write as a paste and not submit it.
            os.write(fd, code.encode())
            pump(0.5)
            os.write(fd, b"\r")
            end = time.time() + 180
            token = None
            while time.time() < end and not token:
                alive = pump(0.5)
                m = h["token"].search(out)
                token = m.group(0) if m else None
                if not alive:
                    break
            if not token:
                return finish("failed", error=f"{h['command'][0]} didn't take the code: {_scrub(out[-400:], h)}")
            _store(st["secret"], token)
            _register(st, "long-lived")
            return finish("done")
        # device: it finishes by itself, leaving its credential in a file.
        cred = Path(home) / h["file"]
        while time.time() < end and not cred.exists():
            if not pump(1.0):
                break
        if not cred.exists():
            return finish("failed", error=f"{h['command'][0]} didn't finish signing in: {_scrub(out[-300:], h)}")
        _store(st["secret"], cred.read_text())
        _register(st, "device")
        return finish("done")
    except Exception as e:  # the helper never leaves a sign-in hanging as "waiting"
        return finish("failed", error=_scrub(str(e), h))


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"] and len(sys.argv) == 4 and sys.argv[2] in HARNESSES and ID.match(sys.argv[3]):
        run(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)

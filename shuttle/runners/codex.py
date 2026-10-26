"""CodexRunner: bodies as `codex exec --json` runs (t-1439).

    shuttle.json  "runner": {"kind": "codex",
                             "codex": "codex",          the binary; a bare name is found on PATH
                             "profile": "shuttle",      -p <profile>: the inbox hooks (shuttle.codex_profile)
                             "model": null,             -m <model>; null is codex's own default
                             "runs_dir": "<state_dir>/runs"}   one dir per body

A body is one foreground `codex exec` process, started in its own session,
with the prompt on stdin. Its id is a key this runner picks at launch
(secrets.token_hex(8)): random, so its first 8 characters (the inbox dir, the
sent-log id) never collide, and known before the process starts, so the body's
env carries it as SHUTTLE_BODY for the send tools and the inbox hook. The
thread id Codex reports in its first event is only recorded, in
<run>/thread_id (for `codex exec resume`, and logs).

    <runs_dir>/<key>/
      pid            the codex process (also its process group)
      events.jsonl   its stdout: thread.started, turn.started, item.*, turn.completed|turn.failed
      stderr.txt     its stderr
      last.txt       -o: the last agent message
      thread_id      read lazily from events.jsonl
      rc             its exit status, once this runner reaped it

Files, not pipes, so a body outlives a bridge restart (the units use
KillMode=process) and a runner built afresh (every _cfg() call) finds it
again from disk. Nothing is held in memory: the runner never waits on a
Popen. list() reaps its own finished children with waitpid(WNOHANG), and a
run whose events say the turn ended counts as ended whatever its pid says
(a zombie must not read as working until hang_kill_s, review C3).

Codex has no per-tool deny for the names shuttle denies, so a non-empty deny
list is refused: the adapters' startup checks refuse a codex instance with an
always_deny or a guest venue (review C6), and start() raises as a backstop.

Stdlib only.
"""
import json
import os
import re
import shutil
import signal
from pathlib import Path

from shuttle.runner import Runner

# The env keys a Claude Code launcher leaves behind. A Codex body inherits the
# launcher's env, so they would tell its tools (and the inbox hook) it is a
# Claude session it is not. CLAUDE_JOB_DIR too: an owner's tools that key on
# it would act inside that Claude job's dir.
CLAUDE_ENV_PREFIX, CLAUDE_ENV_KEYS = "CLAUDE_CODE_", ("CLAUDECODE", "CLAUDE_JOB_DIR")
PRUNE_AFTER_S = 24 * 3600


def find_binary(codex):
    """The codex binary's path (a path, or a name on PATH), or None."""
    if "/" in codex:
        p = Path(codex)
        return p if p.is_file() and os.access(p, os.X_OK) else None
    found = shutil.which(codex)
    return Path(found) if found else None


def scrub_env(env):
    """`env` without Claude Code's session keys."""
    return {k: v for k, v in env.items()
            if not k.startswith(CLAUDE_ENV_PREFIX) and k not in CLAUDE_ENV_KEYS}


STOP_GRACE_S = 5   # a finished turn's codex gets this long after turn end to exit before SIGTERM
CUT_OFF = "cut off before its turn finished"   # last_entry's detail for a turn that never ended


class CodexRunner(Runner):
    name = "codex"
    # The registry here is the runs dir; list() fails only when it can't be read.
    registry_hint = None   # set per instance: names the runs dir
    auth_hint = ("Check `codex login status` for the bridge's user (and its CODEX_HOME); "
                 "bodies run under its login.")
    # Measured 2026-10-22, codex-cli 0.162.1, a CODEX_HOME with no auth.json:
    # every attempt, and the closing turn.failed, says "unexpected status 401
    # Unauthorized: Missing bearer or basic authentication in header, ...".
    # A death's why keeps 80 characters, and this is in the first 35.
    AUTH_FAILURE_RE = re.compile(r"\b401 Unauthorized\b", re.IGNORECASE)
    # Item errors that are warnings, not failures: the two the bypass flag
    # prints at start, and the transport fallback. Every item.completed error
    # is ignored (a failed turn ends in turn.failed); this list is what was seen.
    BYPASS_WARNING = "`--dangerously-bypass-hook-trust` is enabled"
    # Top-level `error` events Codex emits while it retries: not the end.
    RETRYING = re.compile(r"^Reconnecting\.\.\. \d+/\d+ ")

    def __init__(self, codex, profile, model, runs_dir, inbox_dir, subprocess, time, secrets):
        """No I/O here: the bridge builds a runner on every _cfg() call.
        `inbox_dir` is a callable (the bridge's inbox base), read at start()."""
        self.codex = str(codex)
        self.profile = profile
        self.model = model
        self.runs_dir = Path(runs_dir)
        self.inbox_dir = inbox_dir
        self.subprocess, self.time, self.secrets = subprocess, time, secrets
        runs = str(self.runs_dir).replace("{", "{{").replace("}", "}}")
        self.registry_hint = (f"the codex runs dir {runs} has been unreadable {{n}} ticks "
                              "running; the bridge can't reap, so dead bodies keep their {conv}.")

    # ------------------------------------------------------------ launch

    def resolve_model(self):
        return self.model

    def argv(self, run, model, cwd):
        return [str(find_binary(self.codex) or self.codex), "exec", "--json", "--skip-git-repo-check",
                "--dangerously-bypass-hook-trust", "--dangerously-bypass-approvals-and-sandbox",
                "-p", self.profile, *(["-m", model] if model else []),
                "-C", str(cwd), "-o", str(run / "last.txt"), "-"]

    def start(self, prompt, model, deny, env, cwd):
        """`codex exec`, the prompt on stdin, stdout and stderr to files in the
        run dir. Returns {"key"}."""
        if deny:
            raise ValueError(f"the codex runner can't deny tools ({', '.join(deny)}): "
                             "refusing to start a body that would keep them")
        key = self.secrets.token_hex(8)
        run = self.runs_dir / key
        run.mkdir(parents=True, mode=0o700)
        env = scrub_env(os.environ if env is None else env)
        env["SHUTTLE_BODY"] = key
        env["SHUTTLE_INBOX_DIR"] = str(self.inbox_dir())
        with open(run / "events.jsonl", "wb") as out, open(run / "stderr.txt", "wb") as err:
            proc = self.subprocess.Popen(self.argv(run, model, cwd), cwd=str(cwd),
                                         stdin=self.subprocess.PIPE, stdout=out, stderr=err,
                                         start_new_session=True, env=env)
        (run / "pid").write_text(f"{proc.pid}\n")
        try:
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
        except BrokenPipeError:   # it died at once: its events/stderr say why
            pass
        return {"key": key}

    def claim(self, handle, exclude):
        """The key start() picked: known at launch, nothing to wait for."""
        return {handle["key"]} - set(exclude)

    # ------------------------------------------------------------ one run, from disk

    def _run(self, aid):
        return self.runs_dir / str(aid)

    def _pid(self, run):
        try:
            return int((run / "pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def _events(self, run):
        try:
            lines = (run / "events.jsonl").read_text(errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue   # a line still being written
            if isinstance(e, dict):
                out.append(e)
        if out and out[0].get("type") == "thread.started" and not (run / "thread_id").exists():
            try:
                (run / "thread_id").write_text(f"{out[0].get('thread_id')}\n")
            except OSError:
                pass
        return out

    @staticmethod
    def _turn_over(events):
        return any(e.get("type") in ("turn.completed", "turn.failed") for e in events)

    def _ours(self, pid, run):
        """Is `pid` still this run's codex? Its argv names the run dir (-o
        <run>/last.txt); a recycled pid's does not. A zombie's cmdline is empty."""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return str(run).encode() in cmdline

    def _alive(self, run):
        """The run's process is running. Reaps it when it is our finished child.
        Once its rc is written it was reaped, and its pid may be someone
        else's now: never waited on again."""
        pid = self._pid(run)
        if pid is None or (run / "rc").exists():
            return False
        try:
            got, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:   # not our child (a bridge restart): ask /proc
            return self._ours(pid, run)
        except OSError:
            return False
        if got == 0:
            return True
        try:
            (run / "rc").write_text(f"{os.waitstatus_to_exitcode(status)}\n")
        except (OSError, ValueError):
            pass
        return False

    # ------------------------------------------------------------ the registry

    def list(self, strict=False):
        """One row per run dir: "working" while its process runs and its turn
        isn't over, else "ended". Prunes run dirs ended more than a day ago."""
        try:
            runs = [d for d in self.runs_dir.iterdir() if d.is_dir()] if self.runs_dir.exists() else []
        except OSError as e:
            if strict:
                print(f"codex runs dir unreadable ({e!r}); not reaping this tick", flush=True)
                return None
            return []
        rows, now = [], self.time.time()
        for run in sorted(runs):
            alive = self._alive(run)
            working = alive and not self._turn_over(self._events(run))
            if not alive and now - self._newest(run) > PRUNE_AFTER_S:
                shutil.rmtree(run, ignore_errors=True)
                continue
            rows.append({"id": run.name, "status": "working" if working else "ended"})
        return rows

    def working(self, status):
        return status == "working"

    def _group_left(self, pgid, key):
        """Is anything of this run still in process group `pgid` (the codex
        pid; start_new_session made it the group)? A member counts only if
        its env carries this run's SHUTTLE_BODY, so a group id reused by
        another session is never taken for this run's."""
        tag = f"SHUTTLE_BODY={key}\0".encode()
        try:
            procs = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return False
        for p in procs:
            try:
                stat = Path(f"/proc/{p}/stat").read_text()
                if int(stat.rsplit(")", 1)[1].split()[2]) != pgid:
                    continue
                if tag in Path(f"/proc/{p}/environ").read_bytes():
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def stop(self, aid):
        """SIGTERM its process group: while its pid is still this run's
        process, or, once that exited, while anything it backgrounded is still
        in the group (_group_left). A run whose turn already ended gets until
        STOP_GRACE_S after that end (events.jsonl's mtime) to exit on its own
        first: the reaper's tick can land while codex is still writing its
        last message and session (smoke test, 2026-10-22). Measured from the
        turn end, not the stop, so a tick never waits more than STOP_GRACE_S in
        all. The run dir stays for the reaper."""
        run = self._run(aid)
        pid = self._pid(run)
        if pid is None:
            return
        if not self._ours(pid, run):
            self._alive(run)   # reap a zombie leader, so its rc is written
            if self._group_left(pid, run.name):
                self._killpg(pid)
            return
        if self._turn_over(self._events(run)):
            try:
                deadline = (run / "events.jsonl").stat().st_mtime + STOP_GRACE_S
            except OSError:
                deadline = 0
            while self._alive(run) and self.time.time() < deadline:
                self.time.sleep(0.2)
            if not self._alive(run) and not self._group_left(pid, run.name):
                return
        self._killpg(pid)
        self._alive(run)   # reap it now if it already went

    @staticmethod
    def _killpg(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------ the turn

    def done(self, aid):
        return any(e.get("type") == "turn.completed" for e in self._events(self._run(aid)))

    def parked(self, aid):
        return False

    def _newest(self, run):
        newest = 0.0
        for name in ("events.jsonl", "pid", "stderr.txt"):
            try:
                newest = max(newest, (run / name).stat().st_mtime)
            except OSError:
                pass
        return newest

    def quiet_s(self, aid):
        """Seconds since its last event."""
        try:
            return self.time.time() - (self._run(aid) / "events.jsonl").stat().st_mtime
        except OSError:
            return 0.0

    def last_entry(self, aid):
        """Its turn, in the shape the core reads:
        - turn.completed: {"state": "done", "text": its last agent message};
        - turn.failed, or (no turn end) a top-level error that isn't a retry
          notice: {"state": "blocked"} with the message as both text and detail,
          the harness-failure shape (retried; an auth message parks);
        - neither, but it started a turn: {"state": "working", "detail": CUT_OFF,
          "text": ""}, a turn cut off (killed mid-turn), as Claude's reads;
        - nothing at all: None, a body that never drew breath (died silent).
        item.completed errors are warnings (the bypass flag's two at start, the
        transport fallback) and never a failure."""
        events = self._events(self._run(aid))
        for e in events:
            if e.get("type") == "turn.completed":
                text = ""
                for i in events:
                    item = i.get("item") or {}
                    if i.get("type") == "item.completed" and item.get("type") == "agent_message":
                        text = item.get("text") or ""
                return {"state": "done", "detail": "", "text": text}
        for e in events:
            if e.get("type") == "turn.failed":
                msg = str((e.get("error") or {}).get("message") or "turn failed")
                return {"state": "blocked", "detail": msg, "text": msg}
        errors = [str(e.get("message") or "") for e in events if e.get("type") == "error"]
        errors = [m for m in errors if m and not self.RETRYING.match(m)]
        if errors:
            return {"state": "blocked", "detail": errors[-1], "text": errors[-1]}
        if any(e.get("type") == "turn.started" for e in events):
            return {"state": "working", "detail": CUT_OFF, "text": ""}
        return None

    # ------------------------------------------------------------ how failures read

    def api_error(self, entry):
        """A failed turn that isn't the login: "API error", retried."""
        return entry.get("state") == "blocked" and not self.auth_failure(entry.get("text") or "")

    def auth_failure(self, text):
        return bool(self.AUTH_FAILURE_RE.search(text))


# ---------------------------------------------------------------- config

OPTIONS = {"codex": str, "profile": str, "model": (str, type(None)), "runs_dir": str}


def option_errors(instance, section):
    """Cheap: the options' types. For runner.errors(), on every build."""
    out = []
    for k, t in OPTIONS.items():
        if k in section and not isinstance(section[k], t):
            out.append(f"{instance.path}: runner.{k} must be a "
                       f"{'string or null' if k == 'model' else 'string'}, not {section[k]!r}")
    return out


def startup_errors(instance, section):
    """The startup check's codex part: the binary is there, and so is the
    profile with the shuttle's inbox hooks (shuttle.codex_profile --check)."""
    from shuttle import codex_profile
    out = []
    codex = _codex(instance, section)
    if find_binary(codex) is None:
        out.append(f"{instance.path}: runner.codex ({codex}) is not an executable "
                   "(a path, or a name on PATH)")
    hook = codex_profile.DEFAULT_HOOK
    if not hook.is_file():
        out.append(f"the shuttle's inbox hook {hook} is missing: a codex body would never hear "
                   "follow-ups")
    problem = codex_profile.check(codex_profile.codex_home(), section.get("profile") or "shuttle",
                                  hook)
    if problem:
        out.append(f"runner.profile: {problem}")
    return out


def _codex(instance, section):
    c = section.get("codex") or "codex"
    return str(instance.expand(c)) if "/" in c else c


def build(instance, b):
    """The bridge's CodexRunner. Paths from shuttle.json; the runs dir defaults
    to <the bridge's state dir>/runs; the inbox dir is the bridge's (b.inbox)."""
    section = instance.raw.get("runner") or {}
    runs_dir = (instance.expand(section["runs_dir"]) if section.get("runs_dir")
                else Path(b.STATE_DIR) / "runs")
    return CodexRunner(codex=_codex(instance, section), profile=section.get("profile") or "shuttle",
                       model=section.get("model"), runs_dir=runs_dir,
                       inbox_dir=lambda: b.inbox.base(),
                       subprocess=b.subprocess, time=b.time, secrets=b.secrets)

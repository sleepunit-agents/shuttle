"""The runner: everything the shuttle knows about the agent harness (t-1438).

A runner starts a body, finds it again, says whether it is mid-turn, done,
parked or quiet, and stops it. The core, the dispatch and the spawn ask the
bridge's runner for all of that, and for how its failures read: the
registry-failure page's text, what counts as an API error, what counts as an
auth failure, and the hint in the auth give-up page (t-1439). One per bridge,
built from the instance:

    shuttle.json  "runner": {"kind": "claude"}      optional; default "claude"
                  "runner": {"kind": "codex", ...}   shuttle/runners/codex.py

`Runner` is the interface. `ClaudeRunner` is Claude Code run as `claude --bg`:
a daemon owns each body, `claude agents --json` lists them, and each body's
job dir (<jobs_dir>/<aid8>/state.json, timeline.jsonl) says how its turn is
going. A new body is found by a random spawn token in its prompt (the job's
intent), so the token is a Claude detail and never leaves this file.

Stdlib only.
"""
import json
import os
import re
from pathlib import Path


class Runner:
    """A harness that runs bodies. Every method must be provided, and every
    attribute set."""

    name = None   # "claude"
    # The registry-failure page, after "<agent>-<bridge>: ". A str.format
    # template: {n} is the failed ticks running, {conv} the adapter's CONV_NOUN.
    registry_hint = None
    # The auth give-up page's last words: where a human looks first.
    auth_hint = None

    def api_error(self, entry):
        """Is this last_entry() an upstream API error (the reaper retries it and
        says "API error")? `entry` is never None."""
        raise NotImplementedError

    def auth_failure(self, text):
        """Is this text (a death's why, or a last entry's text) the harness's
        login failing? The give-up then parks the batch until auth is back."""
        raise NotImplementedError

    def resolve_model(self):
        """The model to start the next body on."""
        raise NotImplementedError

    def start(self, prompt, model, deny, env, cwd):
        """Launch one body on `prompt` (never on argv: message text must not show
        in /proc/<pid>/cmdline), denied the tools in `deny`, with `env` (None
        inherits) in `cwd`. Returns a launch handle for claim()."""
        raise NotImplementedError

    def claim(self, handle, exclude):
        """The ids of the body `handle` started, never one in `exclude` (bodies
        already owned). May wait for it to register; an empty set when it never
        shows. The caller acts on the claim only when exactly one id comes back."""
        raise NotImplementedError

    def find(self, handle, exclude):
        """claim() in one look, without waiting: the bridge asks again later about
        a launch it couldn't confirm. `handle` is what start() returned, minus
        anything that isn't JSON (it is kept in state)."""
        raise NotImplementedError

    def list(self, strict=False):
        """Every body the harness runs: [{"id", "status"}, ...]. When it can't
        tell: [], or None with strict=True (the reaper must not read a failed
        call as "every body is gone")."""
        raise NotImplementedError

    def working(self, status):
        """Does a list() status mean the body is mid-turn?"""
        raise NotImplementedError

    def stop(self, aid):
        """Stop the body. Never raises for a body that is already gone."""
        raise NotImplementedError

    def done(self, aid):
        """The body ended its turn on its own."""
        raise NotImplementedError

    def parked(self, aid):
        """The body's only work left is waiting on monitors: nobody will ever act
        on it, so the reaper may stop it once it is quiet."""
        raise NotImplementedError

    def quiet_s(self, aid):
        """Seconds since the body last showed any activity (0 if never)."""
        raise NotImplementedError

    def last_entry(self, aid):
        """The body's last timeline entry, {"state", "detail", "text"}, or None.
        The core reads it to tell a failure ("blocked" with the harness's own
        error text) and a silent death from a body that chose not to reply."""
        raise NotImplementedError


class ClaudeRunner(Runner):
    """Claude Code, `claude --bg`.

    `subprocess`, `time` and `secrets` are the adapter's (b.subprocess, ...):
    the bridges' tests swap those on the bridge module, so a test never
    starts a real body. `agents` and `intent` are what claim() polls with;
    build_runner() points them at the adapter's shims (b.agents,
    b._job_intent), which tests swap too. Left None, they are this runner's
    own list() and intent()."""

    name = "claude"
    registry_hint = ("`claude agents` has failed {n} ticks running; the bridge can't reap, "
                     "so dead bodies keep their {conv}. Check the --bg daemon.")
    auth_hint = "Check the --bg daemon's token / `claude auth status` (t-1320 shape)."
    # The give-up branch's test for an auth reason (t-1320). "could not
    # refresh...login" is the 2026-10-06 auth-race shape (t-338); "oauth session
    # expired" is conclave's wording for the same class (t-1322, 2026-10-17).
    AUTH_FAILURE_RE = re.compile(
        r"login expired|oauth session expired|could not refresh .{0,20}login|not authenticated",
        re.IGNORECASE,
    )
    TOKEN_LINE = "(bridge spawn token {token}: ignore this line)"
    CLAIM_POLLS = 10   # one a second

    def __init__(self, claude, jobs_dir, model_resolver, preferred_model,
                 subprocess, time, secrets, agents=None, intent=None):
        self.claude = Path(claude)
        self.jobs_dir = Path(jobs_dir)
        self.model_resolver = model_resolver
        self.preferred_model = preferred_model
        self.subprocess, self.time, self.secrets = subprocess, time, secrets
        self._agents = agents or (lambda: self.list())
        self._intent = intent or self.intent

    # ------------------------------------------------------------ launch

    def resolve_model(self):
        """Ask the instance's model resolver which model is usable right now
        (e.g. scoped weekly cap -> Opus -> Sonnet). Only a `claude-` answer
        counts; anything else, or a failure, is the preferred model."""
        if not self.model_resolver:
            return self.preferred_model
        try:
            out = self.subprocess.run([str(self.model_resolver), self.preferred_model],
                                      capture_output=True, text=True, timeout=30).stdout.strip()
            if out.startswith("claude-"):
                return out
        except Exception as e:
            print(f"model resolver failed: {e!r}", flush=True)
        return self.preferred_model

    def start(self, prompt, model, deny, env, cwd):
        """`claude --bg`, the prompt on stdin with a fresh spawn token on line 2.

        How the bridge finds THIS body among new agents: a fresh random token in
        the prompt (job intent is the full prompt). Message text rides in the
        same prompt, so a quoted send line must never be what claims a body
        (t-1411 security review). Line 2, so it survives even if job intent were
        ever truncated; line 1 stays the brief's opening, which the board's
        activity rows key on."""
        token = self.secrets.token_hex(8)
        head, _, rest = prompt.partition("\n")
        prompt = f"{head}\n{self.TOKEN_LINE.format(token=token)}\n{rest}"
        before = {a.get("id") for a in self._agents()}
        # With nothing to deny, no flag: a bare --disallowedTools would swallow
        # whatever argument came next.
        argv = [str(self.claude), "--bg", "--model", model, "--dangerously-skip-permissions",
                *(["--disallowedTools", *deny] if deny else [])]
        # A Codex body's key (t-1439), inherited by a bridge started under one:
        # a Claude body must never key its inbox or sends on it. Only then is
        # an env built; otherwise env goes through as given (None = inherit).
        inherited = os.environ if env is None else env
        if "SHUTTLE_BODY" in inherited:
            env = {k: v for k, v in inherited.items() if k != "SHUTTLE_BODY"}
        proc = self.subprocess.Popen(argv, cwd=str(cwd), stdin=self.subprocess.PIPE,
                                     stdout=self.subprocess.DEVNULL, stderr=self.subprocess.DEVNULL,
                                     start_new_session=True, env=env)
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        return {"token": token, "before": before}

    def claim(self, handle, exclude):
        """Poll once a second, up to CLAIM_POLLS times, for new bodies whose job
        intent carries this launch's token. A bare set-diff also claimed any
        other sleeve that started in the same 10s: a wake got owned as the wrong
        body, held that person's DM ~8 min, then was "retried" as
        died-without-reply (2026-10-16, t-1272)."""
        for _ in range(self.CLAIM_POLLS):
            self.time.sleep(1)
            new_ids = self.find(handle, exclude)
            if new_ids:
                return new_ids
        return set()

    def find(self, handle, exclude):
        marker = f"(bridge spawn token {handle['token']}:"
        before = set(handle.get("before") or ())
        return {aid for aid in {a.get("id") for a in self._agents()} - before
                if aid and marker in self._intent(aid)} - set(exclude)   # never re-claim one already owned

    # ------------------------------------------------------------ the registry

    def list(self, strict=False):
        """`claude agents --json`: [{id, status, createdAt}, ...]."""
        try:
            r = self.subprocess.run([str(self.claude), "agents", "--json"], capture_output=True,
                                    text=True, timeout=20)
            if r.returncode != 0:
                raise RuntimeError(f"rc={r.returncode}")
            return json.loads(r.stdout) if r.stdout.strip() else []
        except Exception as e:
            if strict:
                print(f"claude agents failed ({e!r}); not reaping this tick", flush=True)
                return None
            return []

    def working(self, status):
        return status in ("running", "working", "busy")

    def api_error(self, entry):
        """A last timeline entry that is an API-error block."""
        text = (entry.get("text") or "") + " " + (entry.get("detail") or "")
        return entry.get("state") == "blocked" and "API Error" in text

    def auth_failure(self, text):
        return bool(self.AUTH_FAILURE_RE.search(text))

    def stop(self, aid):
        self.subprocess.run([str(self.claude), "kill", aid], stdout=self.subprocess.DEVNULL,
                            stderr=self.subprocess.DEVNULL, timeout=20)

    # ------------------------------------------------------------ the job dir

    def _job(self, aid):
        try:
            return json.loads((self.jobs_dir / aid[:8] / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def intent(self, aid):
        """The prompt a body's job was started with ("" if unknown)."""
        return (self._job(aid) or {}).get("intent") or ""

    def done(self, aid):
        """The job's state is "done": the body ended its own turn."""
        return (self._job(aid) or {}).get("state") == "done"

    def parked(self, aid):
        """The job's only in-flight work is Monitor tasks (it ended its turn and
        is 'watching'; nobody is ever going to act on that)."""
        inflight = (self._job(aid) or {}).get("inFlight") or {}
        kinds = inflight.get("kinds") or []
        tasks = inflight.get("tasks") or 0
        return bool(tasks > 0 and kinds and all(k == "monitor" for k in kinds)
                    and not inflight.get("queued"))

    def quiet_s(self, aid):
        """Seconds since the job last wrote its state or timeline."""
        d = self.jobs_dir / aid[:8]
        newest = 0.0
        for name in ("timeline.jsonl", "state.json"):
            try:
                newest = max(newest, (d / name).stat().st_mtime)
            except OSError:
                pass
        return (self.time.time() - newest) if newest else 0.0

    def last_entry(self, aid):
        try:
            return json.loads((self.jobs_dir / aid[:8] / "timeline.jsonl")
                              .read_text().strip().splitlines()[-1])
        except (OSError, IndexError, json.JSONDecodeError):
            return None


# ---------------------------------------------------------------- choosing one

def _claude(instance, b):
    return ClaudeRunner(
        claude=b.CLAUDE, jobs_dir=b.JOBS_DIR,
        model_resolver=b.MODEL_RESOLVER, preferred_model=b.PREFERRED_MODEL,
        subprocess=b.subprocess, time=b.time, secrets=b.secrets,
        # Late-bound through the adapter, so a test that swaps b.agents or
        # b._job_intent also steers the claim (and the spawn's `before` list).
        agents=lambda: b.agents(), intent=lambda aid: b._job_intent(aid))


def _codex(instance, b):
    from shuttle.runners import codex
    return codex.build(instance, b)


KINDS = {"claude": _claude, "codex": _codex}


def kind(instance):
    """shuttle.json's runner.kind, "claude" when absent. Not checked here (see errors())."""
    section = instance.raw.get("runner")
    k = section.get("kind", "claude") if isinstance(section, dict) else "claude"
    return k


def errors(instance):
    """[] when the instance names a runner this shuttle has, with options of the
    right types, else the problems. Cheap (no I/O): build_runner() runs it on
    every _cfg() call. The startup check runs startup_errors()."""
    section = instance.raw.get("runner")
    if section is not None and not isinstance(section, dict):
        return [f"{instance.path}: runner must be an object, e.g. {{\"kind\": \"claude\"}}"]
    k = kind(instance)
    host = section.get("host") if isinstance(section, dict) else None
    if host is not None:
        from shuttle import bodies
        names = list(host.values()) if isinstance(host, dict) else [host]
        if not names or not all(isinstance(n, str) and bodies.PROFILE.match(n) for n in names):
            return [f"{instance.path}: runner.host is the body host's service name (lowercase), "
                    f"or {{bridge: name}}, not {host!r}"]
    if not isinstance(k, str) or k not in KINDS:
        return [f"{instance.path}: runner.kind {k!r} is not one of: {', '.join(sorted(KINDS))}"]
    if k == "codex":
        from shuttle.runners import codex
        return codex.option_errors(instance, section)
    return []


def startup_errors(instance):
    """For a bridge's startup check: errors(), then what the kind needs on this
    host (codex: its binary, and the profile with the shuttle's inbox hooks)."""
    problems = errors(instance)
    if problems:
        return problems
    if kind(instance) == "codex":
        from shuttle.runners import codex
        return codex.startup_errors(instance, instance.raw.get("runner") or {})
    return []


def deny_errors(instance, guest_venues=()):
    """For a bridge's startup check (review C6): a runner that can't deny tools
    (codex) must not start where a body would be denied any: an instance with
    an always_deny, or a bridge with guest venues (their bodies lose guest_deny).
    The adapter passes its guest venues; the instance can't see routes.json."""
    if kind(instance) != "codex":
        return []
    out = []
    deny = instance.raw.get("always_deny")
    if isinstance(deny, list) and deny:
        out.append(f"{instance.path}: runner.kind codex can't deny tools, and always_deny is "
                   f"{deny}: make it [] or use the claude runner")
    if guest_venues:
        out.append(f"{instance.path}: runner.kind codex can't deny tools, and there are guest "
                   f"venues ({', '.join(map(str, guest_venues))}) whose bodies must lose "
                   "guest_deny: use the claude runner")
    return out


def host_profile(instance, bridge=None):
    """shuttle.json's runner.host, for this bridge: the body host's profile when the agent's
    bodies run in its own house (shuttle/bodies.py), else None. One name for every bridge,
    or {bridge: name}, since each bridge's bodies may use different secrets."""
    section = instance.raw.get("runner")
    host = section.get("host") if isinstance(section, dict) else None
    return host.get(bridge) if isinstance(host, dict) else host


def build_runner(instance, b, bridge=None):
    """The bridge's runner. `b` is the adapter: a Claude runner takes its
    binary, jobs dir, model resolver and preferred model from b.CLAUDE,
    b.JOBS_DIR, b.MODEL_RESOLVER, b.PREFERRED_MODEL (the adapter's globals,
    which tests re-point), and launches through b.subprocess, b.time and
    b.secrets; a Codex runner takes its runs dir default from b.STATE_DIR and
    its inbox dir from b.inbox. Raises ValueError for a kind it doesn't know
    (the startup check refuses that first)."""
    problems = errors(instance)
    if problems:
        raise ValueError(problems[0])
    local = KINDS[kind(instance)](instance, b)
    profile = host_profile(instance, bridge)
    if not profile:
        return local
    from shuttle import bodies, inbox
    wire = bodies.Wire(profile)
    remote = bodies.RemoteRunner(wire, policy=local, time=b.time, secrets=b.secrets)
    inbox.configure(remote=bodies.RemoteInbox(wire, remote))
    return remote

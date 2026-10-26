"""The instance: who the agent is, where its files are, what its briefs say (t-1436).

One shuttle.json per agent instance, found through SHUTTLE_INSTANCE (a path;
there is no default, and a bridge refuses to start without it). Paths in it
expand `~`; relative ones resolve against the file's own directory.

    {"agent": {"slug", "name", "tag"}, "owner": {"name"},
     "workdir", "state_root", "inbox_dir", "now_dir", "secrets" | "secret_cmd",
     "commands": {"claude", "loom", "alert", "model", "send": {bridge: path}},
     "runner": {"kind": "claude"} | {"kind": "codex", "codex", "profile", "model", "runs_dir"},
     "preferred_model", "always_deny": [...], "guest_deny": [...],
     "channel_notes": {routes key: brief name}, "briefs": "briefs", "ext": "ext/x.py",
     "<bridge>": {...per-bridge section...}}

agent.slug is required, and one of secrets (the instance's secrets dir, a
file per secret) or secret_cmd (a command that prints one secret's value,
given its name: a store such as loom-secrets). Defaults:
name and tag = slug, workdir = the file's directory, state_root =
~/.local/state, inbox_dir and now_dir = <state_root>/<slug>-inbox and -now,
briefs = briefs/, runner.kind = "claude" (shuttle/runner.py).

Briefs are the instance's prose: <briefs>/<name>.md, `string.Template` syntax
(`$slot`, `${slot}`, `$$` for a literal `$`). Shuttle computes the slot values
and substitutes them once; a value is never re-templated, so a `$5` or `$new`
in a message comes out verbatim. check() renders every brief with dummy slots
so a typo stops the bridge at startup, before it reaches a person.

The extension module (`ext`) holds behaviour only one instance has, called
through hook points. Every hook is optional; a missing one is a no-op:

    tick(bridge, state)              once per Discord tick
    dm_extra(bridge, rec, tier)  ->  {"text": str, "env": dict|None, "on_claimed": callable|None}
    check(instance)              ->  [problem, ...]: the ext's own startup check (its
                                     config and data); every bridge's startup_check
                                     refuses to start on a non-empty list or a raise

Stdlib only.
"""
import importlib.util
import json
import os
import string
import subprocess
import types
from pathlib import Path


def _no_tick(bridge, state):
    return None


def _no_dm_extra(bridge, rec, tier):
    return {"text": "", "env": None, "on_claimed": None}


def _no_check(instance):
    return []


_HOOKS = {"tick": _no_tick, "dm_extra": _no_dm_extra, "check": _no_check}


class Instance:
    def __init__(self, path, raw):
        self.path = Path(path)
        self.dir = self.path.parent
        self.raw = raw
        agent = raw.get("agent")
        slug = agent.get("slug") if isinstance(agent, dict) else None
        if not slug or not isinstance(slug, str):
            raise SystemExit(f"{self.path}: agent.slug is required")
        self.agent_slug = slug
        self.agent_name = agent.get("name") or slug
        self.agent_tag = agent.get("tag") or slug
        self.owner_name = (raw.get("owner") or {}).get("name") or "the owner"
        self.workdir = self.expand(raw.get("workdir") or ".")
        self.state_root = self.expand(raw.get("state_root") or "~/.local/state")
        self.inbox_dir = (self.expand(raw["inbox_dir"]) if raw.get("inbox_dir")
                          else self.state_root / f"{slug}-inbox")
        self.now_dir = (self.expand(raw["now_dir"]) if raw.get("now_dir")
                        else self.state_root / f"{slug}-now")
        has = lambda k: isinstance(raw.get(k), str) and raw[k] != ""
        if not has("secrets") and not has("secret_cmd"):
            raise SystemExit(f"{self.path}: secrets is required (the instance's secrets dir), "
                             "or secret_cmd (a command that prints a secret, given its name)")
        self.secrets = self.expand(raw["secrets"]) if has("secrets") else None
        self.secret_cmd = self.expand(raw["secret_cmd"]) if has("secret_cmd") else None
        self.commands = {}
        for name, value in (raw.get("commands") or {}).items():
            if isinstance(value, dict):
                self.commands[name] = {k: self.expand(v) for k, v in value.items()}
            else:
                self.commands[name] = self.expand(value)
        self.preferred_model = raw.get("preferred_model")
        self.always_deny = tuple(raw.get("always_deny") or ())
        self.guest_deny = tuple(raw.get("guest_deny") or ())
        self.channel_notes = dict(raw.get("channel_notes") or {})
        self.briefs_dir = self.expand(raw.get("briefs") or "briefs")
        self._texts = {}   # brief name -> its text, read once (check() reads them at startup)
        self._ext = None

    def read_secret(self, name):
        """One secret's value: secret_cmd's output when the instance has one,
        else the file of that name in the secrets dir. OSError when it can't."""
        if not name or "/" in name or name.startswith("."):
            raise OSError(f"bad secret name {name!r}")
        if self.secret_cmd:
            r = subprocess.run([str(self.secret_cmd), name], capture_output=True, text=True)
            if r.returncode != 0:
                raise OSError(f"{self.secret_cmd} {name}: {r.stderr.strip() or f'exit {r.returncode}'}")
            return r.stdout
        return (self.secrets / name).read_text()

    def expand(self, value):
        """`~` expanded; a relative path resolves against the instance's directory."""
        p = Path(os.path.expanduser(str(value)))
        return p if p.is_absolute() else self.dir / p

    def state_dir(self, bridge):
        return self.state_root / f"{self.agent_slug}-{bridge}"

    def section(self, name):
        """A per-bridge section ("slack", "discord"), or {}."""
        return self.raw.get(name) or {}

    # ------------------------------------------------------------ briefs

    def _brief_file(self, name):
        return self.briefs_dir / f"{name}.md"

    def _text(self, name):
        """The brief's text, read from disk the first time and kept: a file
        edited or deleted after startup can't break a render mid-run."""
        if name not in self._texts:
            p = self._brief_file(name)
            try:
                self._texts[name] = p.read_text()
            except FileNotFoundError:
                raise FileNotFoundError(f"brief {name!r}: no file {p}") from None
        return self._texts[name]

    def brief(self, name, /, **slots):
        """The brief `name` with `slots` substituted, exactly. A missing file
        raises FileNotFoundError, a missing slot KeyError; both name the brief.
        `name` is positional-only, so a brief may have a slot called `name`.
        The text is read once per Instance (see _text)."""
        text = self._text(name)
        try:
            return string.Template(text).substitute(slots)
        except KeyError as e:
            raise KeyError(f"brief {name!r}: no value for slot {e.args[0]!r}") from None
        except ValueError as e:
            raise ValueError(f"brief {name!r}: {e}") from None

    def check(self, slots, required=(), uses=None):
        """Render each brief the caller will use, `slots` = {brief name: the
        slot names it will provide}, with each slot set to a sentinel. So a
        missing file, a slot the brief names but the bridge never provides
        (a typo like `$conext`) and a syntax error (`$ name`, a bare `$5`) all
        show up here, at startup. A name in `required` must have a file too.
        `uses` = {brief name: slot names} the brief must actually use (a guest
        brief that drops `$cards` would otherwise render fine without them).
        Only the named briefs are read: other files in the dir (another
        bridge's briefs, a README) are ignored. Reading them here also loads
        them for good (see _text). Returns ["<name>.md: <error>", ...], [] when
        all is well."""
        errors = []
        uses = uses or {}
        for name, names in slots.items():
            sentinels = {k: f"\x00{k}\x00" for k in names}
            try:
                out = self.brief(name, **sentinels)
            except FileNotFoundError:
                errors.append(f"{name}.md: no such brief in {self.briefs_dir}")
                continue
            except Exception as e:
                errors.append(f"{name}.md: {e.args[0] if e.args else e}")
                continue
            for k in uses.get(name, ()):
                if sentinels.get(k, f"\x00{k}\x00") not in out:
                    errors.append(f"{name}.md: never uses slot {k!r}")
        for name in required:
            if name not in slots and not self._brief_file(name).is_file():
                errors.append(f"{name}.md: required brief is missing from {self.briefs_dir}")
        return errors

    def runner_errors(self):
        """For a bridge's startup check: [] when runner.kind names a runner this
        shuttle has (absent means "claude") and what it needs on this host is
        there (codex: its binary and profile), else the problems."""
        from shuttle import runner
        return runner.startup_errors(self)

    def runner_deny_errors(self, guest_venues=()):
        """For a bridge's startup check: [] unless the runner can't deny tools
        (codex) and a body would be denied some: always_deny, or the bridge's
        guest venues (the adapter passes them; routes.json is its)."""
        from shuttle import runner
        return runner.deny_errors(self, guest_venues)

    # ------------------------------------------------------------ ext

    def ext(self):
        """The extension module's hooks, loaded once. Missing hooks are no-ops.

        The module is loaded in isolation: it is not put in sys.modules (so
        pickle can't find classes defined in it), and its directory is not put
        on sys.path, so a sibling helper must be imported by explicit path
        (importlib.util.spec_from_file_location on Path(__file__).parent / ...)."""
        if self._ext is None:
            mod = None
            if self.raw.get("ext"):
                p = self.expand(self.raw["ext"])
                if not p.is_file():
                    raise SystemExit(f"{self.path}: ext module {p} does not exist")
                spec = importlib.util.spec_from_file_location(f"shuttle_ext_{self.agent_slug}", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            self._ext = types.SimpleNamespace(
                module=mod, **{k: getattr(mod, k, None) or noop for k, noop in _HOOKS.items()})
        return self._ext

    def ext_errors(self):
        """For a bridge's startup check: load the ext and run its check(instance).
        Returns ["ext: ..."] when it won't load, ["ext check: ..."] for each
        problem it reports (or for a raise, or a result that isn't a list of
        str), [] when all is well."""
        try:
            ext = self.ext()
        except BaseException as e:   # a missing ext file is a SystemExit
            return [f"ext: {e!r}"]
        try:
            out = ext.check(self)
        except Exception as e:
            return [f"ext check: raised {e!r}"]
        if not isinstance(out, list) or not all(isinstance(x, str) for x in out):
            return [f"ext check: returned {out!r}, not a list of str"]
        return [f"ext check: {x}" for x in out]


def load(path=None):
    """The instance at `path`, else at $SHUTTLE_INSTANCE. Exits if neither."""
    if path is None:
        path = os.environ.get("SHUTTLE_INSTANCE")
        if not path:
            raise SystemExit("SHUTTLE_INSTANCE is not set: point it at the instance's "
                             "shuttle.json (e.g. Environment=SHUTTLE_INSTANCE=... in the unit)")
    path = Path(os.path.expanduser(str(path))).resolve()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"shuttle instance file {path} does not exist") from None
    except OSError as e:
        raise SystemExit(f"shuttle instance file {path} can't be read: {e}") from None
    except ValueError as e:
        raise SystemExit(f"shuttle instance file {path} is not valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise SystemExit(f"shuttle instance file {path} is not a JSON object")
    return Instance(path, raw)

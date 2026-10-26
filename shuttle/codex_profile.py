"""The Codex profile the shuttle's Codex bodies run under (t-1439).

    python3 -m shuttle.codex_profile [--codex-home DIR] [--profile NAME] [--force] [--check]

writes $CODEX_HOME/<profile>.config.toml (default ~/.codex/shuttle.config.toml)
with two hook tables, PostToolUse (matcher "*") and Stop, each running
`python3 <hook>`: this checkout's inbox hook, shuttle/hooks/inbox_hook.py. Run
it from the shuttle checkout the bridge runs, with the bridge's CODEX_HOME:
the runner's startup check compares the file against that checkout's hook.
(--hook PATH writes another hook path; it is for tests only, since the startup
check would refuse the result.) A Codex body is started with `-p <profile>`, so only
bridge bodies get the inbox hooks, while they share the user's login and the
rest of the user's config.

NAME is letters, digits, "_" and "-". An existing file that says anything
else is kept unless --force: it may be the owner's own profile.

--check writes nothing: it exits 1, saying why, when the file is missing or
says anything else. The Codex runner's startup check runs the same check, so a
bridge never starts bodies whose follow-ups would go unheard. (Codex ignores
a -p profile that has no file, measured on 0.162.1: the hooks would just not
run.)

Stdlib only.
"""
import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

DEFAULT_PROFILE = "shuttle"
DEFAULT_HOOK = Path(__file__).resolve().parent / "hooks" / "inbox_hook.py"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def name_error(profile):
    """Why `profile` can't name a profile file, or None."""
    if isinstance(profile, str) and NAME_RE.match(profile):
        return None
    return (f"bad profile name {profile!r}: a profile name is letters, digits, "
            "'_' and '-' only")


def codex_home(given=None):
    """--codex-home, else $CODEX_HOME, else ~/.codex."""
    if given:
        return Path(os.path.expanduser(str(given)))
    env = os.environ.get("CODEX_HOME")
    return Path(os.path.expanduser(env)) if env else Path.home() / ".codex"


def path(home, profile=DEFAULT_PROFILE):
    problem = name_error(profile)
    if problem:
        raise ValueError(problem)
    return Path(home) / f"{profile}.config.toml"


def hook_command(hook=DEFAULT_HOOK):
    return f"python3 {shlex.quote(str(hook))}"


def render(hook=DEFAULT_HOOK):
    """The profile file's text. TOML basic strings take JSON's escapes."""
    cmd = json.dumps(hook_command(hook))
    return ("# Written by `python3 -m shuttle.codex_profile`: the shuttle's inbox hook,\n"
            "# for bodies started with -p <this profile>. Rewrite it with that command.\n"
            "\n"
            "[[hooks.PostToolUse]]\n"
            'matcher = "*"\n'
            "[[hooks.PostToolUse.hooks]]\n"
            'type = "command"\n'
            f"command = {cmd}\n"
            "\n"
            "[[hooks.Stop]]\n"
            "[[hooks.Stop.hooks]]\n"
            'type = "command"\n'
            f"command = {cmd}\n")


def write(home, profile=DEFAULT_PROFILE, hook=DEFAULT_HOOK, force=False):
    """Write the profile file (atomically) and return its path. An existing
    file that says anything else raises FileExistsError, unless `force`."""
    p = path(home, profile)
    text = render(hook)
    if not force:
        try:
            old = p.read_text()
        except FileNotFoundError:
            old = None
        if old is not None and old != text:
            raise FileExistsError(f"{p} exists and is not this profile: kept "
                                  "(--force overwrites it)")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)
    return p


def check(home, profile=DEFAULT_PROFILE, hook=DEFAULT_HOOK):
    """None when the profile file is exactly what write() writes, else why not."""
    problem = name_error(profile)
    if problem:
        return problem
    p = path(home, profile)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"{p} is missing: run `python3 -m shuttle.codex_profile` to write it"
    except OSError as e:
        return f"{p} can't be read: {e}"
    if text != render(hook):
        return (f"{p} is not the shuttle's profile for hook {hook}: rewrite it with "
                "`python3 -m shuttle.codex_profile`")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m shuttle.codex_profile",
                                 description="Write (or --check) the Codex profile with the "
                                             "shuttle's inbox hooks.")
    ap.add_argument("--codex-home", help="default: $CODEX_HOME, else ~/.codex")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="default: %(default)s")
    ap.add_argument("--hook", default=str(DEFAULT_HOOK),
                    help="tests only (the startup check wants the default): %(default)s")
    ap.add_argument("--force", action="store_true", help="overwrite a file that says anything else")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if it differs")
    args = ap.parse_args(argv)
    problem = name_error(args.profile)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    home = codex_home(args.codex_home)
    hook = Path(os.path.expanduser(args.hook)).resolve()
    if args.check:
        problem = check(home, args.profile, hook)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print(f"{path(home, args.profile)}: ok")
        return 0
    try:
        print(f"wrote {write(home, args.profile, hook, args.force)}")
    except FileExistsError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

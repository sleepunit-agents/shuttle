"""Upgrade when idle: a bridge stops itself, cleanly, when nobody's talking to it
and newer code is waiting, so the service that keeps it up starts it fresh.

A bridge runs the shuttle (and works in the agent's repo) that its house cloned
when it started. Nobody should have to notice an upgrade: so the bridge waits
until it's idle (nothing talking, nothing waiting, for IDLE_S), looks at most
every CHECK_S, and only then asks git whether its checkouts are behind their
upstream. If one is, it says so, and the adapter stops the way it always stops
(state saved; Slack drains its queue into pending). loom-box keeps the service
up: a fresh house clones current code, and the saved state (cursors, pending)
comes with it.

SHUTTLE_UPGRADE=off turns it off (a bridge run by hand, or one being debugged).
"""
import os
import subprocess
import time
from pathlib import Path

IDLE_S = 300
CHECK_S = 600
GIT_TIMEOUT_S = 30


def behind(repo, run=subprocess.run):
    """(head, upstream) when the checkout at `repo` is strictly behind its upstream, else None.
    Never raises: a repo git can't read, or a fetch that fails, is not a reason to stop."""
    def git(*args):
        r = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        if not (Path(repo) / ".git").exists():
            return None
        if git("fetch", "-q") is None:
            return None
        head, up = git("rev-parse", "HEAD"), git("rev-parse", "@{upstream}")
        if not head or not up or head == up:
            return None
        # Behind, not merely different: a checkout with its own commits isn't replaced under it.
        if run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, up],
               capture_output=True, timeout=GIT_TIMEOUT_S).returncode != 0:
            return None
        return head, up
    except (OSError, subprocess.SubprocessError):
        return None


class Upgrader:
    """Called once a tick with the board's talking and waiting lists; says why to stop, or None."""

    def __init__(self, repos, now=time.time, check=behind, env=os.environ):
        self.repos = [Path(r) for r in repos if r]
        self.now, self.check = now, check
        self.off = env.get("SHUTTLE_UPGRADE", "").lower() in ("off", "0", "no", "false")
        self.idle_since = None
        self.last_check = 0.0

    def tick(self, talking, waiting):
        if self.off or not self.repos:
            return None
        t = self.now()
        if talking or waiting:
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = t
        if t - self.idle_since < IDLE_S or t - self.last_check < CHECK_S:
            return None
        self.last_check = t
        for repo in self.repos:
            b = self.check(repo)
            if b:
                return f"upgrading: {repo.name} is behind ({b[0][:7]} → {b[1][:7]}) and nobody's talking; stopping so a fresh house starts on it"
        return None

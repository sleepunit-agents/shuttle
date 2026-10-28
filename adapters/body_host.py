#!/usr/bin/env python3
"""The body host: an agent's bodies, started in its own house (shuttle/bodies.py).

    SHUTTLE_INSTANCE=<shuttle.json> python3 adapters/body_host.py <profile> [--idle-s N]

Run as a service of the agent's house, with the secrets its bodies may use
(those the bridge's house had). The bridge, wherever it runs, says what to
start through shuttle's store; this starts it with the instance's own runner
(claude --bg), in the instance's workdir, mirrors each body's follow-ups into
the house's inbox dir (the bridge-inbox hook reads them there), and reports
what its bodies are doing. With --idle-s N it leaves after N seconds with no live
body and nothing asked; the default, 0, stays (a kept-up service).
"""
import argparse
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shuttle import bodies, inbox  # noqa: E402
from shuttle import instance as shuttle_instance  # noqa: E402
from shuttle import runner as shuttle_runner  # noqa: E402


class _Adapter:
    """What a runner takes from a bridge module, for a process that isn't one."""

    def __init__(self, inst):
        self.CLAUDE = inst.commands.get("claude") or Path.home() / ".local/bin/claude"
        self.JOBS_DIR = Path.home() / ".claude/jobs"
        self.MODEL_RESOLVER = inst.commands.get("model")
        self.PREFERRED_MODEL = inst.preferred_model
        self.STATE_DIR = inst.state_dir("bodies")
        self.subprocess, self.time, self.secrets, self.inbox = subprocess, time, secrets, inbox
        self._runner = None

    def agents(self):
        return self._runner.list()

    def _job_intent(self, aid):
        return self._runner.intent(aid)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("profile")
    ap.add_argument("--idle-s", type=int, default=0)
    a = ap.parse_args(argv)
    inst = shuttle_instance.load()
    inbox.configure(inbox_dir=inst.inbox_dir)
    b = _Adapter(inst)
    # The instance's own harness, never its `host` (that's the bridge's way here).
    raw = dict(inst.raw, runner={k: v for k, v in (inst.raw.get("runner") or {}).items() if k != "host"})
    local = shuttle_runner.KINDS[shuttle_runner.kind(inst)](type(inst)(inst.path, raw), b)
    b._runner = local
    host = bodies.Host(bodies.Wire(a.profile), local, inbox=inbox, cwd=inst.workdir, idle_s=a.idle_s)
    print(f"{inst.agent_slug} body host {a.profile} up: idle-s={a.idle_s}", flush=True)
    host.run()


if __name__ == "__main__":
    main()

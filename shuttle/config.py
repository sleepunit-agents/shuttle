"""Per-bridge configuration, passed into every core function.

The core has no module globals for paths or limits. Each adapter builds one
Config, so two bridges in the same process (or the same test) never share a
state file or a sent log.
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    name: str            # "discord" | "slack": the bridge's name in logs and nowlog
    state_dir: Path      # <state_root>/<slug>-<name>
    # Derived from state_dir unless given (a bridge's tests re-point them one by one).
    state_file: Path = None
    state_lock: Path = None
    sent_log: Path = None    # "<epoch> <job8> <chan> <bytes>" per post, written by the send tool
    send: Path = None        # the send tool (the instance's per-bridge send command) the notices go through
    send_env: dict = field(default=None, compare=False)   # its environment
    alert: Path = None       # pages a human; None means nobody can be paged
    workdir: Path = None     # where bodies start (the instance's workdir)
    # The harness that runs the bodies (shuttle.runner): its binary, job dirs and
    # model choice are its own, not the Config's.
    runner: object = field(default=None, compare=False, repr=False)
    tag: str = "agent"       # the "(tag)" the failure notices open with
    agent: str = "agent"     # the agent's slug: pages say "<agent>-<name>"
    always_deny: tuple = ()  # tools no body this bridge starts may use
    # Limits. The same on both bridges except alert_after, which counts ticks.
    max_attempts: int = 2      # spawns per batch before giving up (API error or silent death)
    reap_grace: float = 30     # idle this long before reaping, unless its job says done
    parked_kill_s: float = 60  # parked (monitor-only) this long -> reap
    hang_kill_s: float = 1800  # silent this long while working -> hung, kill
    alert_after: int = 20      # the runner's list() failing this many ticks running -> page
    max_live: int = 5          # bodies this bridge runs at once
    min_avail_mb: int = 1500   # don't spawn below this MemAvailable
    cooldown_s: float = 20     # min seconds between spawns in one conversation
    wait_ttl: float = 3600     # a retry or leftover that can't be admitted is dropped after this

    def __post_init__(self):
        for name, leaf in (("state_file", "state.json"), ("state_lock", "state.json.lock"),
                           ("sent_log", "sent.log")):
            if getattr(self, name) is None:
                object.__setattr__(self, name, self.state_dir / leaf)

#!/usr/bin/env python3
"""The inbox hook (t-1409, harness-neutral since t-1439): a working body hears
the messages that arrived in its conversation while it worked.

Wire it as a PostToolUse and a Stop hook, in Claude Code or Codex (both take
the same hook JSON):

    python3 <shuttle>/shuttle/hooks/inbox_hook.py

- PostToolUse: anything pending in this body's inbox becomes
  `hookSpecificOutput.additionalContext`.
- Stop: anything pending blocks the stop (`{"decision": "block", "reason"}`);
  a body ending its turn with a monitor or cron armed is told once to cancel
  them.

Which body: env SHUTTLE_BODY (the key the runner picked and set at launch),
else stdin `session_id` (a Claude body, whose session id is its body id).
SHUTTLE_BODY must win: a Codex body's stdin `session_id` is its thread id, not
its key, and it may inherit CLAUDE_CODE_SESSION_ID from whoever launched it, so
neither of those is ever the lookup for it.

Which inbox dir: $SHUTTLE_INBOX_DIR, else the inbox_dir of $SHUTTLE_INSTANCE,
else of the instance file the caller passes, else the caller's fallback dir.
A body's harness may set none of these; a caller (an instance's own hook
wrapper) passes what it knows.

Fails open: always exits 0. Bad stdin is ignored silently; any other error is
one line on stderr. A message not delivered mid-turn is still picked up by
the bridge after the turn.
"""
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):      # run as a file: put this checkout's shuttle on the path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shuttle import inbox  # noqa: E402

PARKED = ("You're a chat body: end with nothing armed. Cancel the monitor/cron you left "
          "running (the bridge reaps parked bodies, and nobody will act on it), then end. "
          "If something needs watching later, leave a taskstore item or a loom note.")


def body_id(hook, env=None):
    """The body's id: env SHUTTLE_BODY, else the hook's stdin session_id."""
    env = os.environ if env is None else env
    return env.get("SHUTTLE_BODY") or hook.get("session_id") or ""


def inbox_dir(instance=None, fallback=None, env=None):
    """Where the inboxes are (see the module doc). `instance` is a shuttle.json
    path, `fallback` a dir; either may be None. A named instance that won't
    load raises RuntimeError; nothing found at all raises too."""
    env = os.environ if env is None else env
    d = env.get("SHUTTLE_INBOX_DIR")
    if d:
        return Path(d)
    path = env.get("SHUTTLE_INSTANCE") or instance
    if path:
        from shuttle import instance as inst
        try:
            return inst.load(path).inbox_dir
        except SystemExit as e:     # load() exits on a bad file; a hook must not
            raise RuntimeError(str(e)) from None
    if fallback:
        return Path(fallback)
    raise RuntimeError("no inbox dir: set SHUTTLE_INBOX_DIR or SHUTTLE_INSTANCE")


def render(items, venue):
    lines = "\n".join(it.get("text", "") for it in items)
    n = len(items)
    return (f"New message{'s' if n != 1 else ''} in {venue} while you were working "
            f"(addressed to you, same as your brief):\n{lines}")


def handle(hook, instance=None, fallback=None, env=None):
    """The output for one hook event (a dict to print), or None for nothing."""
    sid = body_id(hook, env)
    inbox.configure(inbox_dir=inbox_dir(instance, fallback, env))
    if not sid or not inbox.path_for(sid).is_dir():
        return None
    ev = hook.get("hook_event_name")
    if ev not in ("PostToolUse", "Stop"):
        return None
    # A Claude subagent's tool calls carry the body's session_id too (checked
    # 2026-10-20), plus agent_id. Messages handed to a subagent never reach
    # the body, so only the body's own calls take from the inbox.
    if hook.get("agent_id"):
        return None
    items = inbox.take(sid)
    venue = inbox.meta(sid).get("venue") or "the channel"
    if ev == "PostToolUse":
        if not items:
            return None
        text = (render(items, venue) + "\nFold them into what you're doing; reply in the "
                "channel as usual. If one changes the task, change course.")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
    if items:
        return {"decision": "block", "reason": render(items, venue) + "\nHandle these before you end."}
    if (hook.get("background_tasks") or hook.get("session_crons")) and not hook.get("stop_hook_active"):
        return {"decision": "block", "reason": PARKED}
    # The turn ends: what the body did, for its conversation's page (shuttle/deeds.py).
    _deeds(sid, hook, instance, env)
    return None


def _deeds(sid, hook, instance, env):
    from shuttle import deeds, nowlog
    env = os.environ if env is None else env
    path = env.get("SHUTTLE_INSTANCE") or instance
    if path and not env.get("SHUTTLE_NOW_DIR"):
        try:
            from shuttle import instance as inst
            nowlog.configure(now_dir=inst.load(path).now_dir)
        except (SystemExit, Exception):  # noqa: BLE001 - the board is best effort
            pass
    deeds.record(sid, hook.get("transcript_path"), inbox.meta(sid), env)


def run(instance=None, fallback=None, label="inbox-hook", stdin=None, stdout=None, stderr=None):
    """The hook: read the event from stdin, print the output. Never raises;
    returns 0 (the exit code)."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        try:
            hook = json.load(stdin)
        except ValueError:
            return 0
        if not isinstance(hook, dict):
            return 0
        out = handle(hook, instance, fallback)
        if out is not None:
            print(json.dumps(out), file=stdout)
    except Exception as e:  # fail open: the bridge still handles anything left over
        print(f"{label}: {e!r}", file=stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())

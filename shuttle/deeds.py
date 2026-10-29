"""What a body did, from its own session, for the conversation's page on loom
("is there any way to go to like conversation details? I'm not trying to view
a transcript here", 2026-10-29).

A body's session lives in its house and goes with it, so the body says what it
did when each turn ends: the inbox hook's Stop calls record(), which reads the
body's transcript and sends one `did` event to the now board. Identifiers and
counts only, never what was said: the tasks it touched (t-NNNN and the verb),
the pull requests it opened or merged, how many memories it saved (never their
titles: an agent's memories stay its own), the knowledge pages it wrote or
checked, what it asked for (an approval's kind, a secret's name), the files it
changed (a count), its tools (a count each), and the session's own cost-state
(time, cost, models). Each record is the whole session so far, so the latest
one per body is the answer and a lost one costs nothing.

Never raises into the hook: record() swallows everything.
"""
import json
import os
import re
import sys
import time
from collections import Counter

from shuttle import nowlog

CAP = 20          # at most this many of each list in one record
TASK = re.compile(r"\btaskstore\s+(claim|close|create|comment|update|reopen|link|unlink)\b(?:\s+(t-\d+))?")
TASK_ID = re.compile(r'"id"\s*:\s*"(t-\d+)"')
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_MERGE = re.compile(r"\bpr\s+merge\s+(\d+)\b.*?(?:-R|--repo)\s+([\w.-]+/[\w.-]+)|\bpr\s+merge\s+(?:-R|--repo)\s+([\w.-]+/[\w.-]+)\s+(\d+)")
APPROVAL = re.compile(r"\bapprovals\s+request\b.*?--kind\s+(\S+)(?:.*?--target\s+(\S+))?")
SECRET = re.compile(r"\bsecret\s+request\s+([\w.-]+)")
GIT = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|push)\b")


def _text(content):
    """A tool result's text, whether a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _tool(name):
    """mcp__loom__remember -> loom remember; a built-in stays itself."""
    parts = name.split("__")
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 and parts[0] == "mcp" else name


def summarize(lines):
    """The record's fields from a Claude Code session's JSONL lines (strings or
    dicts). A subagent's own calls (isSidechain) count too: they were the body's
    work. Anything unreadable is skipped."""
    uses, results = [], {}
    cost = None
    for line in lines:
        try:
            r = line if isinstance(line, dict) else json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(r, dict):
            continue
        kind = r.get("type")
        if kind == "cost-state":
            cost = r
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        for b in content:
            if not isinstance(b, dict):
                continue
            if kind == "assistant" and b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                uses.append((b.get("id"), b["name"], b.get("input") if isinstance(b.get("input"), dict) else {}))
            elif kind == "user" and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = _text(b.get("content"))
    tools = Counter()
    tasks, prs, knowledge, asks, files = {}, {}, {}, [], set()
    memories = commits = pushes = 0
    for uid, name, inp in uses:
        tools[_tool(name)] += 1
        out = results.get(uid, "")
        if name == "Bash":
            cmd = str(inp.get("command") or "")
            for verb, tid in TASK.findall(cmd):
                if verb == "create" and not tid:
                    tid = next(iter(TASK_ID.findall(out)), "")
                if tid:
                    tasks.setdefault(tid, [])
                    if verb not in tasks[tid]:
                        tasks[tid].append(verb)
            if re.search(r"\bpr\s+create\b", cmd):
                for repo, n in PR_URL.findall(out):
                    prs.setdefault(f"{repo}#{n}", "opened")
            for m in PR_MERGE.finditer(cmd):
                n, repo = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
                prs[f"{repo}#{n}"] = "merged"
            for k, target in APPROVAL.findall(cmd):
                asks.append({"kind": "approval", "what": (target or k)[:80]})
            for s in SECRET.findall(cmd):
                asks.append({"kind": "secret", "what": s[:80]})
            for g in GIT.findall(cmd):
                if g == "commit":
                    commits += 1
                else:
                    pushes += 1
        elif name == "mcp__loom__remember":
            memories += 1
        elif name in ("mcp__loom__knowledge_write", "mcp__loom__knowledge_verify"):
            slug = inp.get("slug") or inp.get("path")
            if isinstance(slug, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*", slug) and ".." not in slug.split("/"):
                knowledge.setdefault(slug[:120], "wrote" if name.endswith("write") else "checked")
        elif name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            p = inp.get("file_path") or inp.get("notebook_path")
            if isinstance(p, str):
                files.add(p)
    rec = {
        "tools": dict(tools.most_common(CAP)),
        "tasks": [{"id": k, "did": v} for k, v in list(tasks.items())[:CAP]],
        "prs": [{"ref": k, "did": v} for k, v in list(prs.items())[:CAP]],
        "memories": memories,
        "knowledge": [{"slug": k, "did": v} for k, v in list(knowledge.items())[:CAP]],
        "asks": asks[:CAP],
        "files": len(files),
        "commits": commits,
        "pushes": pushes,
    }
    if cost:
        models = cost.get("modelUsage") if isinstance(cost.get("modelUsage"), dict) else {}
        rec["cost"] = {
            "usd": round(float(cost.get("totalCostUSD") or 0), 4),
            "ms": int(cost.get("totalDuration") or 0),
            "started": int(cost.get("startTime") or 0),
            "added": int(cost.get("totalLinesAdded") or 0),
            "removed": int(cost.get("totalLinesRemoved") or 0),
            "models": {m: round(float((u or {}).get("costUSD") or 0), 4) for m, u in list(models.items())[:6]},
        }
    return rec


def record(body, transcript_path, meta, env=None):
    """Send this body's `did` record to the now board. Quiet on any failure."""
    env = os.environ if env is None else env
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return None
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            rec = summarize(f)
        bridge = meta.get("bridge") if isinstance(meta.get("bridge"), str) else None
        if not bridge:
            return None
        nowlog.configure(agent=env.get("HOUSE_AGENT") or None)
        full = {"v": 1, "t": time.time(), "agent": nowlog._agent(), "bridge": bridge, "kind": "did",
                "body": str(body)[:8], "chan": meta.get("chan"), "venue": meta.get("venue"), **rec}
        if nowlog._remote():
            nowlog._post("event", {"record": full})
        else:   # a bridge on the board's own machine: its own events file
            nowlog.emit(bridge, "did", **{k: v for k, v in full.items() if k not in ("v", "t", "agent", "bridge", "kind")})
        return full
    except Exception as e:  # noqa: BLE001 - a hook never fails its body
        print(f"deeds: {e!r}", file=sys.stderr)
        return None

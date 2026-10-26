# shuttle

A bridge between chat platforms (Discord, Slack) and agent bodies: it carries
a person's messages to Claude Code bodies and their replies back, with one
adapter per platform. Loom-adjacent: it reads loom person cards and writes no
memory, while loom holds who the agent is. Named for the loom part that carries
thread through the cloth.

This repo holds no instance data: no names, platform ids, tiers, paths or
prose about any one agent or owner. Everything specific to a deployment lives
in an instance directory outside it, found through `SHUTTLE_INSTANCE`.

## Running an instance

An instance is a directory with a `shuttle.json` and its selvedge and briefs
beside it:

    kit/bridge/
      shuttle.json     the instance file (below)
      routes.json      where and when the agent answers (platform ids)
      people.json      who may reach it, and at what tier
      briefs/          the prose each body is handed (`<name>.md`)
      ext/kit.py       optional extension hooks

Point the adapter at it and run it with the Python that has its dependencies
(Slack needs `websocket-client`):

    SHUTTLE_INSTANCE=~/kit/bridge/shuttle.json python3 adapters/discord.py
    SHUTTLE_INSTANCE=~/kit/bridge/shuttle.json python3 adapters/slack.py

Each is a daemon (no arguments; one per instance, guarded by a lock in its
state dir), meant to run under a service manager that sets
`SHUTTLE_INSTANCE`. With it unset, or the file missing or not a JSON object,
the adapter exits with a message. The `shuttle` library is imported from the
checkout the adapter sits in; stdlib only.

### shuttle.json

Paths expand `~`; a relative path resolves against the file's directory.
`agent.slug` and one of `secrets` or `secret_cmd` are required by the loader; "required for a bridge" below is what
that bridge's startup check insists on.

    {
      "agent":  {"slug": "kit", "name": "Kit", "tag": "kit"},
      "owner":  {"name": "Robin"},
      "workdir": "~/kit",
      "state_root": "~/.local/state",
      "inbox_dir": "~/.local/state/kit-inbox",
      "now_dir": "~/.local/state/kit-now",
      "secrets": "~/.config/loom/kit/secrets",
      "commands": {
        "claude": "~/.local/bin/claude",
        "loom": "~/.local/bin/loom",
        "alert": "~/kit/bin/kit-alert",
        "model": "~/kit/bin/kit-model",
        "send": {"discord": "~/kit/bin/discord-send", "slack": "~/kit/bin/slack-send"}
      },
      "runner": {"kind": "claude"},
      "preferred_model": "claude-opus-5-5",
      "always_deny": ["mcp__handoff"],
      "guest_deny": ["mcp__mail", "mcp__calendar"],
      "channel_notes": {"lab_channels": "discord-lab"},
      "briefs": "briefs",
      "ext": "ext/kit.py",
      "discord": {
        "token_file": "discord-token",
        "guest_cap_note": "that's all from me for today. See you tomorrow!"
      },
      "slack": {
        "secrets_env": "slack-work.env",
        "flag_to": {"via": "discord", "chan_from_routes": "notify.general"}
      }
    }

| Key | Meaning | Required |
|---|---|---|
| `agent.slug` | The agent's id: names state dirs (`<state_root>/<slug>-<bridge>`), the board's keys (`<slug>\|<bridge>\|<chan>`), and its bot id in `routes.json` (`bots.<slug>`). | yes (loader) |
| `agent.name`, `agent.tag` | Display name; the tag in failure notices, `(<tag>)`. Both default to the slug. | no |
| `owner.name` | The owner's name in notices. Default "the owner". | no |
| `workdir` | The bodies' working directory. Default: the instance file's directory. | no |
| `state_root` | Parent of the state dirs. Default `~/.local/state`. | no |
| `inbox_dir` | Per-body inbox (follow-ups to a working body). Default `<state_root>/<slug>-inbox`. | no |
| `now_dir` | The "now" board. Default `<state_root>/<slug>-now`. | no |
| `secrets` | Credentials dir (the token and env files below sit in it, one file per secret). No default. | this or `secret_cmd` (loader) |
| `secret_cmd` | Instead of `secrets`: a command that prints one secret's value, given its name as its only argument (e.g. a wrapper around `loom-secrets get <name> --reveal`), so no plaintext file is needed. Wins over `secrets` when both are set. | this or `secrets` (loader) |
| `runner.kind` | The harness bodies run on (see Runners). Default `claude`; an unknown kind refuses the start. | no |
| `runner.codex`, `runner.profile`, `runner.model`, `runner.runs_dir` | The `codex` runner's options (see Runners, Codex). | no |
| `commands.claude` | The Claude Code binary (the `claude` runner's). Both bridges default to `~/.local/bin/claude`. | no |
| `commands.loom` | The loom binary, for person cards (`loom person show <slug>`). Without it, no cards. | no |
| `commands.alert` | An executable that pages the owner. | yes, both bridges |
| `commands.model` | A model resolver, consulted before a spawn (the `claude` runner: run with the preferred model as its argument; only an answer starting `claude-` is used). | no |
| `commands.send.<bridge>` | The command a body posts with. | yes: `discord` for Discord, `slack` for Slack |
| `preferred_model` | The model bodies are asked to use. | no |
| `always_deny` | Tools no body may use. A list; `[]` is allowed, but it must be stated. | yes, both bridges |
| `guest_deny` | Extra tools denied to guest-venue bodies. | yes if `routes.json` has guest venues |
| `channel_notes` | `{routes.json key: brief name}`: a brief added to the prompt of any channel (or thread under one) listed under that key. The key must be a list or object in `routes.json`. | no |
| `briefs` | The briefs directory. Default `briefs`. | no |
| `ext` | The extension module (see Extension hooks). Must exist if named. | no |

Per-bridge sections:

| Key | Meaning | Required |
|---|---|---|
| `discord.token_file` | The token's secret name (a file inside `secrets`, or the name passed to `secret_cmd`). Default `discord-token`. | no (the file must be readable) |
| `discord.guest_cap_note` | What a guest is told when their daily spawns run out. | no |
| `slack.secrets_env` | The env-file secret (in `secrets`, or through `secret_cmd`) holding `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_TEAM_ID`, `SLACK_BOT_USER_ID`. Default `slack.env`. | all four required |
| `slack.flag_to` | Where a tier-2 ask is flagged for the owner: `{"via": <a commands.send key>, "chan_from_routes": <dotted routes.json key>}`, or `"chan": <channel id>` in place of `chan_from_routes`. | only when a brief uses `$send_discord` or `$general` |

An instance's extension may read further sections of its own (the shuttle
ignores them); `Instance.section(name)` returns one.

### Selvedge files

Read from the instance directory, never from this repo. Platform ids are the
only trusted identity; names are renameable.

- `routes.json`: the route map. The adapters read `guild`, `bots` (the
  agent's own bot id at `bots.<slug>`), `ignore`, `only` (Discord: an
  allowlist; when set, only these channels and their threads are polled,
  even for @mentions), `notify` (per agent: where
  a guest venue's worries go; also `notify.general` if `flag_to` points at
  it), `channels` and `threads` (per-venue entries: `primary`, `peer`,
  `guest_venue`, and `readers`: the loom slugs whose person cards a guest
  venue's bodies carry), `webhook_peers`, `bot_roles` (Discord: per bot
  slug, the managed role id(s) "@<bot>" autocompletes to; a ping of one
  counts as a mention of that bot when the message's `mention_roles` confirms
  it), and any key named by `channel_notes`. Discord reads it at start (ids) and
  per tick (routing); missing or unreadable, it polls nothing rather than
  guess.
- `people.json`: `people` (slug to `name`, `role`, `tier` 0 to 3, `discord`
  ids, `slack` as team to ids, `venues` and `daily_spawns` for guests,
  `grant` and `restrict` standing authorities) and `agents` (peer agents, by
  name and `kind`). Re-read when it changes. Tiers: 0 unresolved or read-only,
  1 guest, 2 known colleague, 3 the owner; an id in no entry is a stranger.
- `projects`: not read by the shuttle; an extension may keep its own files
  beside the instance.

### Briefs

`<briefs>/<name>.md`, in `string.Template` syntax: `$slot`, `${slot}`, `$$`
for a literal `$`. The shuttle computes each slot's value and substitutes it
once, never re-templating, so a `$5` in a message comes out verbatim. The
files are exact bytes: a missing or added final newline changes what a body
reads (several fragments are meant to run on into the next one).

Discord (`discord` and `discord-guest` are required; `discord.md` must use
`$notes`, `discord-guest.md` must use `$cards`):

| Brief | Slots |
|---|---|
| `discord` | where, chan_id, chan_name, new, context, send, owner_line, notes |
| `discord-owner` | label, id |
| `discord-no-owner` | (none) |
| `discord-guest` | cards, send, notify_chan |
| `discord-peer` | peers, pair |
| `discord-peer-bot` | label, name, note, uid |
| `discord-peer-webhook` | name |
| `discord-pair` | name |
| each `channel_notes` brief | (none) |

Slack (all required; each must use the slots in parentheses):

| Brief | Slots |
|---|---|
| `slack-dm` | name, slug, tier, tier_note, card_note, cid, send, new, context (uses tier_note, card_note) |
| `slack-tier-2`, `slack-tier-3` | name, role, send_discord, general |
| `slack-card` | slug, card (uses card) |
| `slack-grant` | grant (uses grant) |
| `slack-restrict` | restrict (uses restrict) |
| `slack-group` | cid, names, roster, tier, note, restricts, cards, slugs, send, new, context (uses note, restricts, cards) |
| `slack-group-tier-2`, `slack-group-tier-3` | send_discord, general |
| `slack-group-owner-present` | (none) |
| `slack-group-restrict` | name, restrict (uses restrict) |

### The startup check

Both adapters run `startup_check()` before doing anything, dry (no network,
nothing written), and exit listing every problem. They refuse to start when:

- a required brief is missing, names a slot the bridge doesn't provide
  (a typo like `$conext`), has a syntax error (a bare `$5`), or drops a slot
  it must use;
- `commands.send.<bridge>` is unset, `always_deny` is not a list, or
  `commands.alert` is not an executable file;
- `runner` is not an object, or `runner.kind` names no runner this shuttle has;
- the runner is `codex` and its binary isn't there, the shuttle's inbox hook
  isn't there, or its profile file is missing or isn't the shuttle's
  (`python3 -m shuttle.codex_profile --check`); or `always_deny` is non-empty,
  or (Discord) `routes.json` has guest venues: Codex can't deny tools;
- Discord: `routes.json` lacks `bots.<slug>`, a `channel_notes` key isn't a
  list or object in it, or there are guest venues and no `guest_deny` or no
  `notify.<slug>`; the token file is unreadable;
- Slack: a brief that uses `$send_discord` or `$general` has no resolvable
  `slack.flag_to`; the env file lacks any of the four credentials;
- the extension won't load, raises in `check`, or reports problems.

A guest venue whose readers' cards are unread never spawns: it holds and
retries rather than run without its rules.

### Extension hooks

An `ext` module holds behaviour only one instance has. All hooks are optional;
a missing one is a no-op. The module is loaded in isolation (not in
`sys.modules`; its directory is not on `sys.path`). `bridge` is the calling
adapter's proxy of its own seams (`api`, `human_allowed`, `JOBS_DIR`,
`INSTANCE`, and so on), so the extension uses what the core uses.

    def tick(bridge, state): ...
    def dm_extra(bridge, rec, tier) -> {"text": str, "env": dict | None, "on_claimed": callable | None}: ...
    def check(instance) -> [problem, ...]: ...

- `tick`: Discord, once per tick. If it raises, the tick carries on; the
  failure is logged and the owner paged once per distinct error.
- `dm_extra`: Slack 1:1 DMs at tier 2. `text` is appended to the tier note,
  `env` (str to str) is added to the body's launch environment, and
  `on_claimed(aid)` runs once the new body is claimed. What the body sees of
  them depends on the runner: `claude --bg` hands the prompt to a daemon,
  so the launch env never reaches the body (a carrier in its job dir must
  do that); a `codex` body is the launched process, so `env` reaches it as
  is, and `aid` is its body key (`SHUTTLE_BODY`). It fails closed: a
  raise or a wrong shape gives that DM no body (its extras may be the body's
  scope). The DM's messages are held as a leftover and dm_extra is retried
  each tick, for up to the wait TTL (an hour) measured from the oldest held
  message. Then they are dropped: the person gets the standard failure
  notice and the owner is paged. Each distinct failure (exception type and
  where it was raised) pages once when it starts and once when it first drops
  a DM; the log gets one line when a DM starts failing and one when it works
  again or is dropped. While a DM is held, the bridge's state has a non-empty
  `leftover`, so a graceful restart that waits for idle waits for it too.
- `check`: every bridge's startup check. Returns a list of problem strings;
  a non-empty list, a raise or a non-list refuses the start.

## Runners

A runner is the harness the bodies run on: everything the shuttle knows about
one lives in `shuttle/runner.py`, behind one interface, so the core, the
dispatch and the spawn don't launch, list or read bodies themselves, and
don't word or classify a harness's failures. Each
bridge builds one from the instance, at the same point it builds its
`Config` (`runner.build_runner(instance, b)`; the core reaches it as
`b.runner`). Choose it in `shuttle.json`:

    "runner": {"kind": "claude"}

Absent, it is `claude`. The kinds this shuttle has:

| Kind | Class | What it runs |
|---|---|---|
| `claude` | `ClaudeRunner` | Claude Code, `claude --bg` (`commands.claude`, `commands.model`, `preferred_model`) |
| `codex` | `CodexRunner` (`shuttle/runners/codex.py`) | `codex exec --json`, one process per body (`runner.codex`, `.profile`, `.model`, `.runs_dir`) |

### The interface

| Member | Returns | Contract |
|---|---|---|
| `name` | str | `"claude"` |
| `resolve_model()` | str | the model the next body starts on |
| `start(prompt, model, deny, env, cwd)` | a launch handle | launch one body; the prompt never goes on argv (it would show in `/proc/<pid>/cmdline`); `deny` is every tool it may not use; `env` None inherits |
| `claim(handle, exclude)` | set of ids | the body `handle` started, never one in `exclude` (already owned); may wait; empty when it never shows. The shuttle acts on a claim only when exactly one id comes back |
| `list(strict=False)` | `[{"id", "status"}]` or None | every body the harness runs; when it can't tell, `[]`, or None with `strict` (the reaper then reaps nothing that tick) |
| `working(status)` | bool | a `list()` status means mid-turn |
| `stop(aid)` | None | stop the body; quiet if it is already gone |
| `done(aid)` | bool | its turn ended on its own (reaped now, not after the grace) |
| `parked(aid)` | bool | its only work left is waiting on monitors (reaped once quiet) |
| `quiet_s(aid)` | float | seconds since it last showed activity, 0 if never (hung past `hang_kill_s`) |
| `last_entry(aid)` | `{"state", "detail", "text"}` or None | its last timeline entry: `state` "blocked" with the harness's error as both `text` and `detail` is a failure; `state` "done", or a closing `text` of its own, is a body that chose not to reply; None is a body that never drew breath |
| `api_error(entry)` | bool | this last entry is an upstream API error: retried, its why "API error". Any other harness failure is retried too, its why "ended blocked: <its text>" |
| `auth_failure(text)` | bool | this why (or last entry text) is the harness's login failing: the give-up parks the batch and replays it once a body ends cleanly, and pages |
| `registry_hint` | str | the registry-failure page after `<agent>-<bridge>: `, a `str.format` template with `{n}` (ticks running) and `{conv}` (the adapter's noun) |
| `auth_hint` | str | the last words of the auth give-up page: where to look |

How the two harnesses measured so far meet it:

| | Claude Code (`claude --bg`) | Codex (`codex exec --json`) |
|---|---|---|
| launch | detached; a daemon owns the job | a foreground process the runner owns |
| knowing which body it is | poll `claude agents`, claim by spawn token in job intent | a key the runner picks at launch, in the body's env as `SHUTTLE_BODY` (the thread id is only recorded) |
| alive / status | `claude agents --json` status | the process is alive and its turn not over |
| turn over | job `state.json` state "done" | `turn.completed` event |
| failed | last timeline entry `blocked` + "API Error" / harness text | `turn.failed`, or a fatal top-level `error` event |
| auth failure | "Login expired", "OAuth session expired", ... | "401 Unauthorized" (measured) |
| quiet | mtime of job `timeline.jsonl`/`state.json` | mtime of its events file |
| parked on monitors | job `inFlight.kinds` all "monitor" | n/a (always False) |
| stop | `claude kill <id>` | kill the process group |
| mid-turn messages | the inbox, read by a PostToolUse/Stop hook | the same inbox, read by a Codex hook |

### Adding one

A new runner is a subclass of `Runner` that provides every member above (the
base raises `NotImplementedError`), plus an entry in `runner.KINDS` that
builds it from the instance and the adapter `b`. Launch through
`b.subprocess`, `b.time` and `b.secrets`, never the modules themselves: the
bridges' tests swap those on the adapter so they never start a real body.
The id it hands back is the body's id everywhere else: the owned record, the
board, the inbox (`<aid8>`, its first 8 characters) and the sent log (the
send command logs the posting body's `<aid8>`, so the body must be able to
tell its send command who it is). Its first 8 characters must be unique among
live bodies: a harness id with a timestamp prefix (Codex's UUIDv7 thread ids
share theirs for about a minute) is not a body id.

The body must also hear follow-ups mid-turn. While it holds a conversation the
bridge appends that conversation's new messages to its inbox, not a new body,
and the harness needs a hook that hands them over (`shuttle/inbox.py`):

    <inbox dir>/<aid8>/      dir 0700, files 0600
      meta.json              {"venue", "chan", "bridge"}
      pending.jsonl          {"id", "text", "msg", "t"} per message, appended by the bridge
      delivered.jsonl        what the hook handed over, each stamped "rt" (read time)
      closed                 present once the bridge closed it: the hook then does nothing
      lock                   fcntl lock, shared by bridge and hook

- After each tool call, the hook calls `inbox.take(aid)` (it moves pending to
  delivered under the lock) and gives the body the texts.
- At the end of a turn, the hook does not let the body stop while anything is
  pending.
- The hook runs inside the body. A Claude body has none of the bridge's
  variables, so the hook calls `inbox.configure(inbox_dir=...)` itself (from
  `instance.load(...)`). A Codex body has `SHUTTLE_BODY` and
  `SHUTTLE_INBOX_DIR`, which its runner sets (below).

The hook is `shuttle/hooks/inbox_hook.py`, the same for Claude Code and Codex
(both take its JSON: PostToolUse `hookSpecificOutput.additionalContext`, Stop
`{"decision": "block", "reason"}`). Point the harness's PostToolUse and Stop
hooks at `python3 <shuttle>/shuttle/hooks/inbox_hook.py`; it puts its own
checkout on the path. It always exits 0 (an error is one line on stderr).

- **Which body:** env `SHUTTLE_BODY` if set, else stdin `session_id`. A runner
  whose id is not the harness's session id (Codex: the stdin `session_id` is
  the thread id) sets `SHUTTLE_BODY=<id>` in the body's env. Never key on
  `CLAUDE_CODE_SESSION_ID`: a body launched from a Claude session inherits it.
  The reverse holds too: a Claude session started under a Codex body inherits
  `SHUTTLE_BODY`, so a hook that only ever runs in Claude sessions should key
  on the Claude session, not on `SHUTTLE_BODY`, and `ClaudeRunner` strips
  `SHUTTLE_BODY` from the env it starts `claude --bg` with.
- **Which inbox dir:** `SHUTTLE_INBOX_DIR`, else the `inbox_dir` of
  `SHUTTLE_INSTANCE`. An instance's own wrapper can call
  `inbox_hook.run(instance=..., fallback=..., label=...)` to add an instance
  file and a last-resort dir for bodies that have neither variable.
- **The send command** logs the same id: `SHUTTLE_BODY` if set, else
  whatever it logged before. The bridges set `SHUTTLE_BODY=bridge` for their
  own posts, so a bridge's post is never counted as a body's reply.
- The bridge counts a delivered message as answered when the body posted in
  a later second than its `rt`; whatever is still pending when the turn ends
  goes to a fresh body.

### Codex

`"runner": {"kind": "codex"}` runs each body as one foreground
`codex exec --json` process (codex-cli 0.162.1 measured):

    codex exec --json --skip-git-repo-check --dangerously-bypass-hook-trust
          --dangerously-bypass-approvals-and-sandbox -p <profile> [-m <model>]
          -C <workdir> -o <run>/last.txt -

| Option | Meaning | Default |
|---|---|---|
| `runner.codex` | the binary: a path (relative to the instance file) or a name on PATH | `codex` |
| `runner.profile` | the Codex profile with the shuttle's inbox hooks | `shuttle` |
| `runner.model` | `-m`; null leaves it to Codex | null |
| `runner.runs_dir` | where each body's run dir goes | `<state_dir>/runs` (per bridge) |

- **The body id** is a key the runner picks at launch (`secrets.token_hex(8)`).
  The body's env carries it as `SHUTTLE_BODY`, with `SHUTTLE_INBOX_DIR` (the
  bridge's inbox dir). A send tool must log `SHUTTLE_BODY` as the posting
  body's id, and the inbox hook keys on it. The claim is immediate.
- **The env** is the plan's, or else the bridge process's **whole** env
  (a `claude --bg` body gets its daemon's env instead), minus every
  `CLAUDE_CODE_*` key, `CLAUDECODE` and `CLAUDE_JOB_DIR`: a bridge started from
  a Claude Code session must not tell a Codex body it is that session or job.
  Anything else the bridge's units put in the env, secrets included, reaches
  every Codex body.
- **`--dangerously-bypass-hook-trust`** makes Codex run every hook it loads
  without review, not only the shuttle's: hooks in the user's own
  `config.toml` and in any project config under the workdir run too. Keep
  those to hooks you'd let a body run.
- **The run dir**, `<runs_dir>/<key>/`: `pid`, `events.jsonl` (its stdout),
  `stderr.txt`, `last.txt`, `thread_id` (recorded for `codex exec resume`),
  `rc`. Files, not pipes, so a body outlives a bridge restart and a runner
  built afresh finds it. `list()` reaps finished children (`waitpid`), counts
  a run whose events end the turn as ended whatever its pid says, and prunes
  run dirs ended over a day ago. `stop()` signals the process group only if
  `/proc/<pid>/cmdline` still names the run dir or, once codex has exited, a
  process still in its group carries the run's `SHUTTLE_BODY` (whatever the
  body backgrounded). A run whose turn ended gets until 5 s after that end to
  exit on its own before the signal.
- **Failures:** `turn.failed` is a harness failure (retried); the two
  `item.completed` errors the bypass flag prints at start, and Codex's
  "Reconnecting..." notices, are not. A `401 Unauthorized` (Codex's text with
  no login) parks and pages like Claude's "Login expired".
- **No tool deny.** Codex has no per-tool deny for these names, so a codex
  instance must have `"always_deny": []` and, on Discord, no guest venues:
  the startup check refuses either, and `start()` raises on a deny list as a
  backstop. A real deny (e.g. disabling MCP servers per run) is later work.
- **`plan["env"]` reaches the body** (unlike `claude --bg`), and an ext's
  `on_claimed(aid)` gets the body key.

Setting one up:

1. `codex login` as the bridge's user (bodies share its login and config).
2. Write the profile from **the shuttle checkout the bridge's units run**, with
   **the same `CODEX_HOME`** they have:

       cd <that shuttle checkout> && CODEX_HOME=<the units' CODEX_HOME> \
         python3 -m shuttle.codex_profile [--profile <runner.profile>]

   It writes `$CODEX_HOME/<profile>.config.toml` (default
   `~/.codex/shuttle.config.toml`): a PostToolUse (matcher `*`) and a Stop
   hook, each `python3 <that checkout>/shuttle/hooks/inbox_hook.py`. Pass
   `--profile` when `runner.profile` isn't `shuttle`. The startup check
   compares the whole file against that checkout's hook, so a profile written
   from another checkout (a worktree, a pip copy) or into another CODEX_HOME
   makes the bridge refuse to start. An existing file that says anything else
   is kept unless `--force`. `--check` exits non-zero when it is missing or
   different. (Codex silently ignores a `-p` profile with no file. The
   installer's `--hook PATH` is for tests only: the startup check refuses a
   profile written with it.)
3. In `shuttle.json`: `"runner": {"kind": "codex"}`, `"always_deny": []`, and
   no guest venues in `routes.json`.
4. The send command logs `SHUTTLE_BODY` (see above), and the bridge's units
   keep `KillMode=process`, so bodies survive a bridge restart.

## Boundary

- **Reads** loom person cards (by slug) and the selvedge: the instance's
  `people.json` and `routes.json`, at paths it's given.
- **Writes no memory.** Bodies write to loom; the shuttle never does.
- **Holds no instance data.** Platform ids, tiers, routes, names, paths and
  brief prose are the instance's and never live in this repo
  (`tests/test_no_instance.py` enforces it).
- **No module globals** for paths or limits in the core: every core function
  takes a `shuttle.config.Config`. The inbox and board dirs are set once by
  the adapter (`inbox.configure`, `nowlog.configure`) or by `SHUTTLE_INBOX_DIR`,
  `SHUTTLE_NOW_DIR`, `SHUTTLE_NOW_IDS`; with none of them, they raise.
  That holds in every process, not only the bridge: anything outside it that
  reads or writes the inbox or the board (a hook running inside a body's
  session, which has none of these variables; a reporting tool) must call
  `configure` itself, e.g. with the dirs from `instance.load(...)`.
- Stdlib only.

## Layout

- `shuttle/`: the core (`core`, `dispatch`, `spawn`, `selvedge`, `config`),
  the runners (`runner`; `runners/codex`, and `codex_profile`, which writes
  the Codex profile),
  the instance loader (`instance`) and the libraries the adapters share
  (`inbox`, `nowlog`, `people`).
- `adapters/discord.py`, `adapters/slack.py`: the bridges.
- `tools/similarity.py`: how alike two files' same-named functions are.

## Tests

    python3 -m unittest discover -s tests -t .

`tests/test_no_instance.py` fails, with file:line, on any word specific to a
deployment. An instance can add private names (its people, its agents)
without committing them: set `SHUTTLE_GUARD_EXTRA` to a file of regexes, one
per line (`#` lines and blanks skipped).

## On loom's page

The shuttle is a loom piece (loom's `docs/extensions.md`, "The page", contract
v2). With `tools/loom-shuttle` on PATH as `loom-shuttle`, and
`SHUTTLE_INSTANCE` set in loom-http's environment, loom's web page (`/ui`)
gets a **Bridges** tab with no other setup:

    loom-shuttle ui --json         the manifest: the tab, a Home tile, asks
    loom-shuttle ui page --json    stdin {"path": "", "query": {}}: the Bridges page

- **Tile:** how many bridges are live, and the latest conversation
  ("#general, 60s ago", or "idle"). It's `bad` while a bridge is dark.
- **Asks:** one per dark bridge ("Slack bridge is dark since 13:02"), and none
  otherwise. A bridge is dark after max(3 x its interval, 30s) without a
  snapshot.
- **The page** (`""`; any other path is Not found):
  - a section per bridge: its state and when it was checked, bodies in use
    out of the cap, today's turns (`picked`) and sends (`delivered`), the
    pickup wait's median and max over the last 24h, who it's talking to now
    and how many are waiting. When every socket drop in the last 24h came
    back up within a minute (and there were at least two), a muted note says
    so: that's the platform rotating the socket, not an outage.
  - **Conversations, last 24h:** where, with whom, and turns. A name links to
    loom's person page (`loom:people/<slug>`) when its slug is known: the
    event's `members` (people.json slugs), or the people.json person whose
    `name` it is, if no one else has that name. A slug is never derived from
    a name, agents aren't linked, and neither is a room with several people.
  - **House rules** (the side column): the instance's `guest_deny` and
    `always_deny` (without the `mcp__` prefix) and `preferred_model`.
- There are no actions: `ui act --json` answers `ok: false`.

It reads the snapshots and only the events files that cover the last 24h
(this month's, and last month's when the window reaches into it), streamed.
It never shows a conversation's content or a body's id. The code is
`shuttle/loomui.py`.

## Tools

    python3 tools/similarity.py adapters/discord.py adapters/slack.py

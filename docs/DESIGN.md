# Valet: design

Status: living design document. The sections marked "As built" describe what is implemented.

## What Valet is

A place to run and control **always-on AI agents** from anywhere, with complete and easy control over two things:

- **Environment**: everything an agent can reach and everything it is told. Its computer, tools, MCP servers, skills,
  browser, secrets and context, set anywhere from "one tool in an empty box" to "my whole computer and my whole life".
- **Directives**: who the agent is, how it behaves, and what it may do without asking.

And one property that cuts across both: **the harness is swappable**. An agent runs on Claude Code today, and on
DeepSeek Harness, Codex, Gemini CLI, OpenCode or anything else tomorrow, without being redefined.

## Principles

1. **The agent is the unit.** Everything (computer, browser profiles, vault entries, MCP servers, sessions, files) is
   something an agent is given, not a top-level feature with its own screen.
2. **Enforce at the environment, not in the harness.** Harnesses have different, incompatible permission systems.
   Valet owns the boundary instead: the computer the agent runs in, what that computer can reach, and the gateway
   every external tool goes through. Inside its box, an agent can be let loose.
3. **Authorization is explicit and present from the first turn.** An agent should never need days of context to
   learn that it's allowed to act. Standing authority is written down, scoped, and backed by guardrails the agent
   knows about (see [Directives](#directives)).
4. **Definitions are files, versioned.** An agent's personality and environment are text you can diff, roll back and
   copy. The apps edit them; git keeps their history.
5. **One event stream, any harness.** Conversations are stored in a harness-neutral format, so the apps never care
   which harness produced them.
6. **Nothing reads a terminal screen.** No tmux scraping, no TUI menu guessing. Harnesses are driven over structured
   protocols only.

## Architecture

```
            iOS / macOS apps · web UI
                       │  HTTPS + WebSocket (owner auth)
┌──────────────────────┴───────────────────────────────────────────┐
│ Control plane                                                    │
│  API · agent registry · definitions (git) · event store          │
│  inbox: approvals + messages · push (APNs) · webhook             │
│  vault · rules · audit                                           │
│  MCP gateway (/mcp, one endpoint per agent)                      │
│  browser service (Chrome profiles, live view)                    │
└──────────────────────┬───────────────────────────────────────────┘
                       │  outbound WebSocket from each runner
        ┌──────────────┴──────────────┐
┌───────┴────────┐             ┌──────┴─────────┐
│ Runner         │             │ Runner         │   one per machine
│ (any box)      │             │ (Mac, later)   │
│ supervisor     │             │                │
│ ACP client ────┼──► agent processes:          │
│ compiler       │     container  or  host      │
└────────────────┘             └────────────────┘
```

- **Control plane**: the only thing the apps talk to. Holds state, secrets, policy and the inbox. Never runs agents.
- **Runner**: a small daemon on each machine that can host agents. It dials *out* to the control plane, so a Mac
  behind NAT works the same as the control plane's own box. It starts and stops agent processes, compiles definitions into harness
  config, and speaks ACP to each harness.
- **Agent process**: one harness (e.g. Claude Code through its ACP adapter) running inside the agent's computer.

## The agent definition

Each agent is a directory in the definitions repo:

```
agents/scout/
  agent.yaml          # environment, harness, triggers
  persona.md          # who it is and how it behaves
  skills/             # agent-specific skills (shared ones live in /skills)
  memory/             # written by the agent itself; versioned like the rest
```

### `agent.yaml`

Two ends of the range, to show it covers both:

```yaml
# A locked-down helper: one tool, no shell, no internet.
name: summarizer
harness: claude
model: claude-haiku-5-5
computer:
  level: none            # none | container | host
tools:
  mcp: [valet.shared]    # only the shared space, through the gateway
directives: [persona.md] # no owner profile, no standing authority
```

```yaml
# A full-trust assistant: its own persistent computer, the browser with the owner's logins, everything.
name: jarvis
display_name: Jarvis     # how the apps show it (the name is the ID); optional, like the emoji
emoji: 🎩
harness: claude          # claude | deepseek | codex | gemini | opencode | <any ACP agent in the registry>
model: claude-opus-5-5
provider: anthropic      # or deepseek, openai, kimi, bedrock… (what the harness supports)
computer:
  level: container
  image: valet/agent-full    # Debian + harnesses + dev tools + Chrome
  network: open              # none | gateway | allowlist | open
  mounts:
    - host: /srv/projects    # bind-mounted from the host, read-write
      path: /work/projects
  # workdir: /Users/me/Code/project   # level host only: the folder it works in (default: its own, under the runner's data)
tools:
  mcp: [valet.browser, valet.shared, valet.agents, github, gmail, calendar]
  skills: ["*"]
browser:
  profiles: [alice]           # rules per site still apply, from the profile
secrets:                     # injected as env vars inside the computer
  - GITHUB_TOKEN
  - vault: aws/alice          # or a vault entry
permissions: trusted         # locked | ask | trusted | unrestricted (see Permissions)
directives: [owner, authority/full, persona.md, environment]
memory: on
triggers:
  - schedule: "0 8 * * *"
    prompt: "Morning briefing: inbox, calendar, anything that broke overnight."
  - webhook: github           # POST /hooks/jarvis/github wakes it with the payload
```

### The compiler

ACP has no notion of system prompts, skills or permission settings, and every harness keeps those in its own files.
So the runner **compiles** a definition into the harness's native configuration before starting it:

| Concept | Claude Code | DeepSeek Harness | Codex | Gemini CLI | OpenCode |
|---|---|---|---|---|---|
| ACP command | Claude ACP adapter | `dsh --profile acp` (native) | Codex ACP adapter | native | native |
| Directives | appended to Claude Code's system prompt (`session/new` `_meta.systemPrompt.append`) | `AGENTS.md` (user-global, `DSH_AGENTS_HOME`) | `AGENTS.md` | `GEMINI.md` | `AGENTS.md` |
| MCP servers | one `valet` server → the gateway: Claude's in ACP `session/new`, dsh's in its own config (an `mcp-client` row, so a call can wait out an ask) | same | same | same | same |
| Skills | a local plugin `valet` in the config dir (`_meta.claudeCode.options.plugins`), with the SDK's `skills` filter set to exactly its skills | `skill-filesystem` in the profile patch, its only root the config dir's `skills/` (`includeDefaultRoots: false`) | `.codex/skills/` | extension | `.opencode/` |
| Permissions | permission mode, set with ACP `session/set_mode`; managed settings so only Valet's rules and hooks count; for `locked`, a read-only tool list in `session/new`'s `_meta` | Valet's approval gate (a plugin in the profile patch) + Valet's own `permission` preset (sandbox, approval), always written | `approval_policy` + `sandbox_mode` | approval mode | permission config |
| Model / provider | env (`ANTHROPIC_MODEL`; `ANTHROPIC_BASE_URL` for other providers, later) | profile patch; `session/set_config_option` for model and effort | `config.toml` | env | config |
| Composition | settings files | a `--patch` layer (`dsh.patch.yml`) over the `acp` profile: every part, down to the agent loop, is a replaceable plugin | config | config | config |

Each harness is a small adapter class: `compile(definition) -> files + env + argv`, plus the name of its ACP
command. The files go in the agent's config dir, rewritten on every start and read-only inside the computer
(`/valet`), so an agent can't edit its own directives. Adding a harness means writing one of these and adding it to the image. Anything an adapter can't express
(e.g. a harness with no skills support) is reported in the agent screen, not silently dropped.

Model and harness are separate choices. DeepSeek models run in DeepSeek Harness or in the Claude harness (through
DeepSeek's Anthropic-compatible endpoint); DeepSeek Harness in turn runs Anthropic, OpenAI, Kimi and GLM models.

DeepSeek Harness is worth singling out: it's built for exactly this kind of hosting. It speaks standard ACP v1 with
no private extensions, runs several persistent sessions per process, resumes them across processes, and is composed
entirely of plugins. That last part means Valet can go deeper than config files when it wants to: a Valet plugin
mounted in the agent's profile could, for example, feed the agent's own approval policy straight from Valet's rules.

## Harnesses: ACP as the single interface

The runner talks to every harness over the **Agent Client Protocol** (JSON-RPC over stdio):

- `session/new` with `cwd` and `mcpServers`; `session/load` or `session/resume` (whichever the harness advertises;
  DeepSeek Harness has only `resume`) to continue after a restart
- `session/prompt` to send a message; `session/update` streams message chunks, thoughts, tool calls, plans, mode
  changes and compactions
- `session/request_permission` when the harness wants approval: the runner turns it into an inbox item
- `session/cancel` to stop a turn

Claude Code runs through the official ACP adapter (built on the Agent SDK); Codex through its adapter; DeepSeek
Harness, Gemini CLI, OpenCode and ~45 others speak ACP natively. If an adapter lacks something we need, we fork or
write our own adapter rather than add a second code path to the runner.

Harnesses implement different parts of ACP (DeepSeek Harness, for example, has no modes, plans or terminals, and its
permission prompts are allow or reject once). The runner reads each harness's advertised capabilities at `initialize`
and the apps show only what that harness supports.

The event store keeps ACP `session/update` payloads, lightly normalized, per agent and session. The apps render that
one format.

Known risks, to check in M0:
- **Claude auth (settled).** Valet is personal, so Claude agents use the owner's subscription: a long-lived token from
  `claude setup-token`, passed into the agent's computer as `CLAUDE_CODE_OAUTH_TOKEN`. An API key remains possible.
- **Adapter maturity.** The Claude and Codex ACP adapters are third-party (Zed / ACP project). Pin versions; keep an
  end-to-end test per harness.
- **DeepSeek Harness is a developer preview.** Its own safety notice says it hasn't had a security audit and its
  extension contracts may still change. Running it in a `container` (the default) is the answer to the first; pinned
  versions and the per-harness test are the answer to the second.

## Computer levels

| Level | What it is | Default for |
|---|---|---|
| `none` | A minimal container: no built-in tools at all (only the gateway's), an empty workspace at every start, no mounts, network `gateway` | narrow single-purpose agents |
| `container` | **Default.** The agent's own persistent container: home volume, workspace, chosen image, chosen network policy, optional bind mounts | almost everything |
| `host` | The harness runs directly as a Unix user on the runner's machine: your user (everything) or a dedicated one, in its working folder (`workdir`: one you name, or its own under the runner's data) | agents that must manage the machine itself |

- Containers are created by the runner through the Docker API; only the runner holds the Docker socket.
- `network: gateway` means the only reachable addresses are the control plane's gateway and the harness's model
  endpoints (each adapter declares its hosts; without them the agent couldn't answer); `allowlist` adds domains,
  with their subdomains; `open` is unrestricted. `none` is accepted and means `gateway`: a container with no network
  at all can't reach its model. Web search runs at the provider, outside any policy, so it's off unless `open`.
- As built (M2d): the agent's container is alone on its own `--internal` Docker network (`inhibit_ipv4`, so not
  even the Docker host is reachable), with a per-agent HTTP CONNECT proxy (`valet/runner/egress.mjs`, a sidecar
  container also on Docker's default network) as the only way out, set through `HTTPS_PROXY`/`HTTP_PROXY`/
  `NODE_USE_ENV_PROXY`. One proxy per agent network, so the proxy knows which agent is asking without any
  credential. The runner drives it with the docker CLI, like the agent.
- `none` as built: Claude Code gets an empty built-in tool list; DeepSeek Harness every built-in tool row off and
  Valet's gate refusing all but the gateway's tools. Its home volume stays (sessions resume); `/work` doesn't.
- Stop keeps the volumes. Destroy is a separate, confirmed action. As built (M4): on its Files screen, once it's
  stopped and its container is gone, naming the volumes it deletes; refused while it runs.
- `host` is opt-in per agent and visibly marked in the apps. Its permissions default to `ask`.
- Every agent has a **terminal** (a shell in its computer) in the apps, for you, not for the harness. As built (M4):
  the runner opens it on a PTY (`docker exec -it` as the agent's user in its container; on `host` the runner
  user's login shell in its working folder, with a clean environment) and relays it as unnumbered one-way frames
  over its link (docs/PROTOCOL.md), flow-controlled by the owner's acks; the control plane bridges each to one
  owner WebSocket. Closing it ends everything the shell started, in a container too (found by a mark in their
  environment, since `docker exec` leaves them running when its client dies). Its **files** go the same way, as
  commands: Python on the host, a small node script (`valet/runner/computer.cjs`, run with `docker exec` as the
  agent's user) in a container, both confining every path to the agent's folders after resolving links. What the
  owner does there is audited on the control plane; what the shell prints is never logged. A runner says what it
  can (`hello.features`), so an older one is told to update rather than failing oddly.

## Tools: the MCP gateway

Every MCP server an agent gets is reached through **one** endpoint: `https://valet…/mcp/agents/<name>` with that
agent's token. The gateway:

- exposes only the tools the definition allows, merged from Valet's own servers (`valet.browser`, `valet.shared`,
  `valet.agents`, …) and external ones (remote HTTP servers, or stdio servers the gateway runs itself)
- holds the external servers' credentials: the agent never sees a GitHub or Gmail token unless it's listed in `secrets`
- applies a per-tool rule (allow / ask / deny), defaulting from the tool's `readOnlyHint`: read-only tools run,
  everything else follows the agent's permission level
- logs every call to the audit log

Because this sits outside the harness, it works identically on Claude, Codex or anything else.

As built (M2, first slice; GUIDE.md "Giving an agent tools"):

- External servers are registered in `mcp.yaml` at the definitions repo's root; `tools.mcp` names the ones an agent
  gets. Tools are namespaced `<server>__<tool>` (dots become `_`, so `valet.agents` → `valet_agents__…`).
- Credentials are `${secret:NAME}` references to Valet's secrets (decrypted when the gateway connects) or
  `${env:NAME}` references to the control plane's environment; the agent never sees them whether or not they're
  in its `secrets`, and an upstream error that quotes one is redacted.
- Without a rule, the decision comes from the tool's annotations alone, taking MCP's defaults: only
  `readOnlyHint: true` is read-only, and a non-read-only tool is destructive unless it says `destructiveHint:
  false`. So "commits" at `trusted` means destructive or unannotated tools; `locked` denies every non-read-only
  tool, and denied tools are left out of the agent's tool list.
- Rules (M3) come first: `tools.rules.<server>.<tool>` (or `"*"` for all of a server's tools) is allow, ask or deny,
  so a rule's allow lists a tool even at `locked`, and a deny hides it at any level. Then a live grant (an "Allow for
  an hour" answer, a row in `tool_grants`) turns an ask into an allow. The decision is one pure function
  (`gateway/policy.py`, `decide_tool`); rules apply at the next call and stay out of the restart fingerprint. The browser has rules of its own (below).
- Stdio servers run on the control plane's host as its user, outside any sandbox.
- The harness doesn't ask again for gateway tools (Claude: a managed allow rule, `mcp__valet`, since only managed
  rules count; dsh's gate lets `mcp__valet__*` through): the gateway's ask is the only one. Claude's locked tool
  list names built-in tools only, so a locked agent keeps the gateway's read-only tools.

Valet's own servers:

- `valet.browser`: the browser tools, rules and vault `login()`
- `valet.shared`: files and notes shared between you and the agent (each agent has its own)
- `valet.memory`: the agent's own memory files
- `valet.agents`: talk to other agents and read their status (off by default). As built: `list_agents`,
  `agent_status`, and `send_message` (M4)
- `valet.owner`: message the owner, ask a question (becomes an inbox item with push), schedule its own wake-ups. As
  built (M4): `message`, `ask`, `schedule_wakeup`, `wakeups`, `cancel_wakeup`, allowed at every level; push is M3's

As built (M3, the browser and the vault; GUIDE.md "Giving an agent the browser"):

- Profiles and their site rules are `browser.yaml` at the repo's root; an agent's `browser.profiles` picks them and
  its `browser.rules` beat theirs. The decision (`control/browser/policy.py`) is pure: the agent's most specific
  matching rule for the action's kind (read, interact, commit, login), then the profile's, then the level's
  baseline; an `ask` with a live hour grant is an `allow`. Rules are read at every call, so they're left out of
  the restart fingerprint.
- `valet.browser` is a builtin with `decides_calls`: the gateway lists its tools as `allow` at every level and
  passes the call through, and the server applies the browser's rules itself, so nothing asks twice. Another
  server that decides its own calls (M4's) sets the same flag.
- Browser asks are inbox items of kind `browser`, made by `Hub.ask_item` (the generic form of the gateway's
  `ask_tool`), with a screenshot and four answers; "always" commits a rule to the agent's definition. An agent
  waiting on an answer doesn't hold the profile's lock, and the action is prepared again once it's allowed.
- One real Chrome per profile on the control plane's host (started without automation flags, reached over CDP),
  stopped when idle; the owner's live view streams CDP screencast frames and dispatches input under the same lock.
  The viewer says its size and scale and the streamed tab is laid out at them (a device-metrics override, dropped
  when the screencast stops), so the picture fills the view without bars, as sharp as the viewer's screen.
- The vault is a table in the control plane's database, encrypted with the secrets' master key and its own
  associated data (`vault:<name>`). `login()` fills it into the page; `- vault: <entry>` in `secrets` gives the
  password as an env var. Page text is masked with every vault secret and password-field value.

As built (M4): `valet.memory` and `valet.shared` (GUIDE.md "Memory", "The shared space"):

- Which servers an agent reaches through the gateway is one answer, `AgentDefinition.gateway_servers()`: those
  `tools.mcp` lists, each once, then `valet.memory` when the definition says `memory: true` (`tools.mcp` can't list
  it). The token at start, the fingerprint's gateway URL, the gateway's listing and lookup, the environment layer,
  Claude's managed allow rule and dsh's gate prefix all use it.
- A server can decide its own calls (`Upstream.decides_calls`): the gateway then lets every call through at
  any level, without asking, still auditing it (a `tools.rules` deny still hides a tool). `valet.browser` does (its
  site rules ask), and so does `valet.memory`: memory is the agent's notebook, only ever its own files, and an agent
  that had to ask to remember would stop remembering.
- Memory is Markdown files in the definitions repo, `agents/<name>/memory/<file>.md`, one commit per change from
  either side (the agent's tools, the owner's API and settings page). Limits: 64 KiB a file, 200 files and 1 MiB an
  agent. The `memory` layer is generated at each start like the environment; neither is in the restart
  fingerprint. Memory commits are kept out of the agent's settings History.
- The shared space: one list, anyone adds, edits and deletes, what agents add or change
  is unseen until the owner looks. The index is the `shared` table, the bytes `<data>/shared/<id>/<name>`. Files
  travel inline through the tool call when small, and
  otherwise through a transfer ticket: a URL on the agent's own gateway endpoint, issued by a tool call the gateway
  already decided, good for one file for 15 minutes while the agent runs with the token it had. The ticket is the
  URL's credential, so the agent's gateway token never has to reach its shell. Agents' additions go through one
  call, `SharedSpace.shared_added`, which pushes and calls the webhook (Notifier.shared_added).
- Since M5 the shared space is per agent (the request: "a place for me and the agent to share, not for everyone"): each
  item has an `agent`, and one rule, `SharedSpace._row(id, who)`, lets the owner reach any item and an agent only its
  own, so every tool, ticket and approved ask goes through it and another agent's id is "no such item". Items from
  the one list went to the agent that added them (else last changed them) when it still had a definitions folder,
  once, at the first start after the upgrade; the rest are unassigned, the owner's to move or delete. In the web UI
  Shared is a section of the agent's page; the header tab went, and the unassigned list is a card on the agents list
  only while there are any. `SharedSpace.forget_agent` is what Delete agent calls.

## Permissions

Three layers, from hardest to softest:

1. **Boundary**: computer level, network policy, mounts, secrets. Can't be talked around.
2. **Gateway and browser rules**: per-tool and per-site allow / ask / deny, the vault. Enforced server-side.
3. **Harness permissions**: what the harness asks about inside its box. Set from the definition's preset:

| Preset | Harness | Gateway / browser |
|---|---|---|
| `locked` | deny everything not listed | deny unless listed |
| `ask` | ask before edits and commands | ask for non-read-only |
| `trusted` | allow everything inside the computer | ask for commits (send, pay, delete, publish…) |
| `unrestricted` | allow everything | allow, log only |

A `container` agent at `trusted` is the sweet spot: free inside its box, and the box decides what's reachable.

The harness column compiles like this today (the gateway column is the gateway's rule above):

| Preset | Claude Code: a permission mode | DeepSeek Harness: Valet's gate + dsh's own preset |
|---|---|---|
| `locked` | `default`, and only read-only tools exist (Read, Glob, Grep, WebSearch, TodoWrite) | the gate denies every tool but reading and bookkeeping |
| `ask` | `default`: asks before edits and commands | the gate asks before every tool but reading and bookkeeping |
| `trusted` | `bypassPermissions`; on the `host`, `acceptEdits` (it still asks before commands) | the gate lets everything run; dsh never asks in a container, and on the `host` keeps its workspace sandbox and asks to leave it |
| `unrestricted` | `bypassPermissions` | the gate lets everything run; dsh's sandbox is off and it never asks |

`trusted` on the `host` stays short of "allow everything" because there is no box around it. The runner sets
Claude's mode with `session/set_mode` on every session it creates, loads or resumes, so a level change reaches old
sessions on the next start. DeepSeek Harness has no modes, so Valet mounts its own plugin, which carries the level
and puts every session, new or resumed, on the level's dsh preset.

All asks, from any layer, land in **one inbox**: harness permission requests, gateway tool calls, browser commits,
agent questions. Each one has a push, approve once / for an hour / always, and "always" writes a rule back into the
definition (so it's visible and reversible).

As built (M3): an item carries its options and clients draw them as they come. A gateway ask offers Allow once,
Allow for an hour, Always allow and Reject; "always" is a commit to the agent's `agent.yaml`, shown and removable
in Settings › Tools next to the hour's grants. A harness's own request keeps its allow once and reject (its own
"always" lasts only as long as its session). Every new pending item is announced once, by one hook
(`Hub.item_created`): an APNs push to each registered device (category `VALET_INBOX`, time-sensitive, token auth
from the owner's key) and a JSON POST to the owner's webhook. Neither ever holds up the ask, and neither carries
the call's arguments.

## Directives

The prompt an agent starts with is assembled from layers, listed in `directives` and joined in that order, as
written (no templating). Shared layers live at the definitions repo's root (`owner.md`, `authority/<preset>.md`);
the control plane sends every layer an agent lists with `agent.start`, and the adapter hands the joined text to the
harness (Claude: system-prompt append; DeepSeek Harness: `AGENTS.md`).

| Layer | Written by | Contents |
|---|---|---|
| `owner` | you, once | Who the owner is, how they work, preferences, languages, the machines and accounts they have |
| `authority/<preset>` | you, a few presets (Valet ships strict, standard and full; one per agent at most) | Standing authorization: what the agent may do on the owner's behalf without asking, and what it must ask about |
| `persona.md` | you, per agent | Name, role, tone, how it reports, what it focuses on |
| `environment` | **generated** at each start | Exactly what this agent has: computer level, permission level and what it means in its harness, MCP tools and what a call to each does, its skills, how its memory works and what the shared space is when it has them, secrets by name, and how Valet enforces rules (browser profiles and triggers when they exist) |
| `memory` | the agent (and you) | What it has learned: **generated** at each start from its memory files (an index, then the files newest first within 32 KiB, the rest named), which it changes with `valet.memory` |

The generated `environment` layer matters as much as the authority: an agent that knows *which* guardrails exist
and that Valet enforces them doesn't need to be its own safety net, so it acts instead of hesitating. Example text
for a full-trust agent (the shipped `authority/full` says this, with the environment adding the specifics):

> You work for the owner, on the owner's machines and accounts. The owner has authorized you to sign in and sign up on sites with
> Valet's `login()`, fill forms with the owner's details, install and run software on this computer, and manage the owner's
> repositories, without asking first. Ask before spending money, messaging people on the owner's behalf, or deleting
> anything you didn't create. Valet enforces these limits server-side: if something isn't allowed, Valet blocks it or
> asks the owner, so act and let Valet decide. You never see the owner's passwords; `login()` fills them for you.

Passwords never enter the context: the vault and `login()` stay as they are.

## Always-on

- **Sessions persist.** The runner keeps each agent's harness process alive and resumes its session (`session/load`)
  after restarts. When context grows too large the harness compacts; `memory/` carries what matters across.
- **Triggers** wake an agent with a prompt: schedules, webhooks, a message from you, a message from another agent, or
  a wake-up the agent scheduled itself.
- **Turns** run one at a time per session; triggers that arrive mid-turn queue. A turn interrupted by a crash is
  marked *Interrupted* and waits for you to retry (no silent replays of side effects).
- **Status** per agent: idle, working, waiting on you, stopped, failed, with the current turn's activity live.

As built (M4, triggers; GUIDE.md "Always-on", API.md "Triggers" and "The prompt queue"):

- **One delivery path** (`control/prompts.py`) for every prompt: yours, schedules, webhooks, other agents, your
  answers, wake-ups. A prompt starts a turn if its session is idle with nothing waiting, else joins that session's
  queue (`prompt_queue`), which drains when a turn ends and when the agent comes up. Claiming a prompt, the turn
  row and its `user_message` event are one SQLite transaction, so after a crash a prompt is still queued or is a
  turn the runner's next hello settles; an `interrupted` or failed turn is sent again only by your Retry. Your own
  prompt to a working session queues too (no more 409).
- **The main session**: one per agent (`agents.main_session`), made on the first prompt that needs it; triggers
  (unless `session: new`), agent messages and answers go there. A harness that can't resume it (the runner's
  "cannot resume session") gets a new one, and the prompts bound for the old one move over (yours isn't resent).
- **Where a prompt came from**: `turns.source` (owner, schedule, webhook:<name>, agent:<name>, wakeup, answer)
  and a label, carried by the `user_message` event; the prompt itself starts with one `Valet: ...` line. The
  environment layer explains that line, the agent's triggers in plain words, its main session, and valet.owner.
- **Schedules** (`shared/cron.py`, `control/scheduler.py`): standard 5-field cron in `VALET_TZ` (zoneinfo wall
  clock, so DST is right), an asyncio task on an injectable clock. A run fires only for an agent meant to be
  running; one it can't take yet waits up to `VALET_TRIGGER_GRACE`, then is missed; after downtime only the latest
  due run catches up. Every run is a `trigger_runs` row (skips and misses folded).
- **Webhooks** (`control/webhooks.py`): `POST /hooks/<agent>/<name>` with a per-webhook key (Bearer, `?key=`, or
  GitHub's `X-Hub-Signature-256`), stored encrypted with the master key because an HMAC needs the key itself. Any
  failed authentication answers like a webhook that doesn't exist. The body reaches the agent between
  `UNTRUSTED WEBHOOK DATA` markers. A call the agent can't take now is refused (503), not stored.
- **Agents to agents**: `valet.agents` `send_message`, into the other agent's main session, rate-limited per
  sender.
- **valet.owner** (`control/gateway/owner.py`): `message`, `ask` (options and/or your own words), and wake-ups.
  Its calls are allowed at every permission level: an `Upstream` with `decides_calls` decides them itself. The
  `message` and `question` inbox items (`Hub.ask_item`) wait on no call: they don't expire with the turn, the
  agent's stop or a restart, and don't make the agent "waiting on you". An answer comes back as a prompt.
- **Activity**: the running turn's latest tool call by title, else "thinking" or "writing" (`Agent.activity`),
  throttled to one agent frame a second; with `queued` and `next_trigger` on the Agent shape.

Not done yet: a webhook call to a stopped agent isn't kept for later; push for valet.owner's items (M3's push);
memory (`memory/`, the `memory` directive) and compaction across sessions; a schedule's runs survive an edit of
its expression only under the old trigger.

## Clients

The apps reorganize around agents:

- **Agents**: the list, with status. Tap one: its conversation (live stream), its computer (terminal, files, browser
  live view), its definition (environment, directives, triggers, as forms backed by the files; raw edit available).
- **Inbox**: every approval and every question from any agent, newest first.
- **Shared**: files and notes (in the web UI since M4: search, filters, notes, uploads, previews, the
  unseen count on its tab; per agent since M5, a section of the agent's page).
- **Settings**: owner profile, authority presets, runners, vault, browser profiles, MCP servers.

**As built (M5, phase 1)**: native SwiftUI apps for iOS and macOS (`apps/`, iOS 17 and macOS 14), on the same REST
API and `/ws/client` socket as the web UI, signed in as a native client (a bearer token in the Keychain, this
device only). A shared package, ValetKit, holds lenient models (unknown fields, statuses, kinds and frames are kept,
never an error), the API client, the socket (reconnect with backoff, refetching an open session's events after the
last one applied), and the event folder: a pure value type that turns a session's events into its conversation,
the Swift twin of the web UI's renderer. The screens: Agents (status, activity, queued prompts, next trigger,
missing secrets; Start / Stop / Restart), an agent (its sessions, New session, secrets asked for when a start needs
them), a session (live Markdown, folded thoughts, tool cards, plans, permission asks inline, Cancel, queued
prompts), Inbox (every pending item with every option it offers; questions take words) and Settings (server, the
Face ID / Touch ID lock, push devices, runners, sign out). iOS has tabs; the Mac has a sidebar and a menu bar extra
with the pending asks. Push: each new inbox item, category `VALET_INBOX` with Allow once, Allow for an hour and
Reject, mapped onto what the item offers when pressed and answered from the background after unlock; an iOS
notification service extension narrows the buttons to the kind for pushes sent with `mutable-content`.

**As built (M5, phase 2)**: browser asks in the Inbox and on the session (the action, its site and kind, the
screenshot, every option in order), and the take-over: the live view, on iOS and macOS, from `/ws/browser/{profile}`
(JPEG frames decoded off the main thread; tap, drag, scroll, pinch to zoom the picture; typing through a field and a
row of keys; address, Back, Reload, tabs; the busy state), with Done and I can't beside the page. Shared (live, the
unseen count on its tab, search and filters, notes read and written, file previews, Quick Look, the share sheet or
Save, uploads from Files and Photos). An agent's memory (its files, editing, history and restore). An agent's
settings: the web UI's form, saved whole through the same `PUT .../definition`, each refused field's error under it,
the raw files and their history for the rest. Settings gains browser profiles (site rules, Chrome, delete) and the
vault (write-only passwords and 2FA secrets), the notification webhook and its test, and each device's last push.
An agent's page continues a Claude Code session or hands a conversation off. Always-on (M4): an agent's page shows
its main session, its triggers, the wake-ups it set (cancel one there) and its recent runs; its settings edit the
triggers (a schedule's meaning as it's typed, a webhook's address and key); a session offers Retry on a failed or
interrupted turn, shows where each prompt came from, and lets a queued prompt be removed.

**As built (M5, phase 3)**: the agent's computer. Its page opens a **Terminal** (SwiftTerm 1.18.0, on iOS and
macOS, through `/ws/terminal/{agent}`): shells as tabs (at most 8), each sized by its view (a rotation, a split view
or the keyboard resizes the shell), the web UI's colours light and dark, and on iOS a row of keys above the keyboard
(Esc, Ctrl, Tab, arrows); the shells close when the screen goes away. **Files**: a navigation stack, one screen per
folder, with breadcrumbs (an earlier crumb goes back to its screen) and the roots as Places when there are several;
text and picture previews, Quick Look, Share (iOS) or Save… and Open (Mac); uploads from Files and Photos (and drops
on the Mac) with Replace / Keep both when a name is taken; New folder. A stopped container agent's first screen lists
its volumes, with Delete volumes behind a confirmation that names each one. The composer takes **attachments**: the
paperclip (Photos, Files), drops, and ⌘V on the Mac; each uploads at once and shows as a chip (thumbnail, name,
size, ×), and Send carries their ids (`{"text", "attachments"}`), queued like any prompt and kept, with the text,
when the server refuses it. The conversation shows the owner's pictures (fetched with the token, tap to enlarge) and
files as rows that open them in the agent's Files.

**As built (the apps' gaps)**: the live view sends its size and scale (`viewport`) and fills its space on a phone, upright
or on its side (where the bars take a row each), and in any Mac window; input still maps through fractions of the
frame. The Mac's sidebar follows navigation (opening an agent from All agents selects it), keeps an agent that's
momentarily missing while the list reloads, and is restored at launch. The terminal keeps 5000 lines of scrollback,
as the web UI does. Every upload (the composer, Files, Shared) takes its files through one path, so drops and ⌘V
on the Mac take pictures as well as files, in Shared too.

The web UI covers the same screens, more plainly. Its agent **settings page** is the first of those forms: name
and emoji, persona (from templates), harness and model, computer, permission level, tools, the browser profiles it
uses and its own site rules, secrets (vault passwords among them); the raw files and the history (with restore)
under Advanced. Settings that don't do anything yet aren't shown. The control-plane API is versioned and documented.

The browser is a tab of its own in the web UI (since M3): the profiles (running or not, stop), each with its title,
note and site rules as a form, its tabs and its saved logins; the live hour grants; the audit log with its
screenshots; `browser.yaml` raw under Advanced; and the vault, whose passwords and 2FA secrets go in through fields
that are never filled. The live view is a full-screen layer over the page (a canvas the JPEG frames are drawn on,
by mouse or by touch, with the profile's waiting asks in a bar on top). A browser ask, in the inbox and in the
session, shows the screenshot, the site, the action and every option the item carries; `request_human` adds the
live view, Done and I can't.

Since M4 an agent's page opens its **Terminal** (xterm.js, vendored under `static/vendor/xterm`, loaded only there;
tabs for several shells; a key bar on phones) and **Files** (its folders, previews, uploads, downloads, its
volumes), and the composer takes **attachments**: images go in the prompt as ACP image blocks when the harness says
it takes them, anything else is put in the agent's workspace and named in the prompt; the session shows both.

## Stack

- **Control plane and runner: Python 3.13**, FastAPI, SQLite. Python has ACP, MCP and Agent SDK libraries.
- **Apps: Swift / SwiftUI** (iOS + macOS), ValetKit holds the networking, Keychain, push and live view.
- **Definitions**: a git repo the control plane writes to; every change is a commit.

## Bringing conversations over

Conversations come over one at a time, as they are:

- **Continue** a Claude Code session (Claude Code's own, in `~/.claude/projects`):
  a Claude Code agent at the `host` level whose working folder (`computer.workdir`) is the session's folder loads
  it with `session/load`, as the same session. The runner reads the transcripts read-only, the way the Agent SDK
  does (folder names, titles), and the adapter's replay is stored as the session's history, one turn per exchange,
  so the apps show it like any other session. No transcript is parsed into Valet's own format: Claude Code stays
  the owner of its context.
- **Hand off** anything else (another harness, another level or machine, a Valet session): a new session of any
  agent whose first message is the old conversation, condensed without a model (what was said, one line per tool
  call, the newest 60 KB, a header naming the source). Honest and cheap; the new agent rereads files it needs.

A conversation's model, permission mode and environment variables don't come with it: they belong
to the agent's definition, set on its settings page.

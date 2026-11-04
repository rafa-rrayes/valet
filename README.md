# Valet

A control plane for always-on AI agents. Agents run on any harness that speaks the
[Agent Client Protocol](https://agentclientprotocol.com) (Claude Code and
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) today), each in its own container or on a
machine you choose. You set an agent up from a form, decide how much it may do without asking, and use it from the
web UI or the iOS and macOS apps: watch the reply stream in, approve or reject its permission requests from your
phone, give it tools, a browser, memory, schedules and webhooks.

Documentation: [docs/DESIGN.md](docs/DESIGN.md) (the design and how it's built), [docs/API.md](docs/API.md) (the
REST API and client socket), [docs/PROTOCOL.md](docs/PROTOCOL.md) (control plane to runner) and
[apps/README.md](apps/README.md) (the native apps).

## The pieces

- **Control plane** (`valet-control`, `valet/control/`): FastAPI and SQLite. It holds the owner's password,
  the agent definitions (a git repo), the event store, the inbox and the vault, and runs the MCP gateway and the
  browser profiles' Chrome. It serves the REST API and the client WebSocket ([docs/API.md](docs/API.md)) and the
  web UI. It never runs agents.
- **Runner** (`valet-runner`, `valet/runner/`): runs on the machine that hosts the agents. It dials out to the
  control plane over a WebSocket ([docs/PROTOCOL.md](docs/PROTOCOL.md)), starts each agent's harness and speaks
  ACP to it over stdio. A `container` agent is `docker run -i --rm` of its image. Its `/home/agent` and `/work`
  are named volumes that outlive the container. A `host` agent is a plain subprocess on the runner's machine.
  Each harness has a small adapter (`valet/runner/harness/`) that compiles the definition into its command, env
  and config files; the files go in the agent's config dir, which a container sees read-only at `/valet`.
- **Web UI** (`valet/control/static/`): a phone-first page served by the control plane. It is plain HTML, CSS and
  JS with no build step.
- **Native apps** (`apps/`): Valet for iOS and macOS, in SwiftUI, on the same API and socket as the web UI: the
  agents, live sessions, the inbox with push notifications you can answer from, Face ID, the agent's terminal and
  files, attachments. How to build them and point them at a control plane: [apps/README.md](apps/README.md).
- **Agent image** (`docker/agent-base/`): Node 24, Claude Code and its ACP adapter
  `@agentclientprotocol/claude-agent-acp@0.99.1`, and DeepSeek Harness `@deepseek-ai/dsh@0.2.1-rc.5`. The runner
  also runs each restricted agent's network proxy from it ([below](#its-computer-and-the-internet)).

## Quickstart

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), git and Docker. State lives in `./data` unless
`VALET_DATA` says otherwise; it is git-ignored.

```sh
uv sync

# 1. Agent image (once, and after changing the Dockerfile)
docker build -t valet/agent-base docker/agent-base

# 2. Control plane: owner password, a runner token, then serve on 127.0.0.1:8780
uv run valet-control set-password
uv run valet-control add-runner mac          # prints vrun_... once; only its hash is stored
uv run valet-control serve

# 3. Runner (another terminal, or another machine that can reach the control plane)
export VALET_RUNNER_TOKEN=vrun_...
export CLAUDE_CODE_OAUTH_TOKEN=...          # from `claude setup-token`: Claude agents use your subscription
export DEEPSEEK_API_KEY=...                 # DeepSeek Harness agents (platform.deepseek.com)
uv run valet-runner
```

Open http://127.0.0.1:8780 and sign in. To reach it from the phone, put it behind a tunnel or reverse proxy with
https and set `VALET_PUBLIC_URL=https://...`, which makes the session cookie `Secure`. Keep the control plane
itself on loopback, and set `VALET_PUBLIC_URL` to the exact address the browser uses: browser requests and the
live socket from any other origin are refused. Behind a Cloudflare tunnel, also set `VALET_TRUST_CF_IP=1` so the
login rate limit counts per client (`cf-connecting-ip`, honored only from loopback) rather than per tunnel.

### Configuration

| variable | default | |
|---|---|---|
| `VALET_DATA` | `./data` | database (`valet.db`) and, by default, the definitions repo |
| `VALET_DEFINITIONS` | `$VALET_DATA/definitions` | git repo of `agents/<name>/` |
| `VALET_HOST`, `VALET_PORT` | `127.0.0.1`, `8780` | where `valet-control serve` listens |
| `VALET_PUBLIC_URL` | `http://127.0.0.1:$VALET_PORT` | https makes cookies `Secure` |
| `VALET_RPC_TIMEOUT` | `60` | seconds to wait for a runner's reply |
| `VALET_TRUST_CF_IP` | off | `1`: behind a Cloudflare tunnel, trust `cf-connecting-ip` from loopback for the login limit |
| `VALET_MASTER_KEY` | | the key secrets are encrypted with, 64 hex characters; overrides the key file |
| `VALET_KEY_FILE` | `$VALET_DATA/master.key` | the key file: 32 random bytes, mode 0600, created the first time a secret is set |
| `VALET_GATEWAY_URL` | `$VALET_PUBLIC_URL` | the control plane as agents reach it, for the MCP gateway (below). Container agents can't use `127.0.0.1`: on Docker Desktop use `http://host.docker.internal:8780` |
| `VALET_GATEWAY_ASK_TIMEOUT` | `600` | seconds a gateway call waits for your answer before it's refused |
| `VALET_GATEWAY_CALL_TIMEOUT` | `300` | seconds an upstream MCP tool call may take |
| `VALET_APNS_KEY_FILE` | | push: your APNs auth key (the `.p8` file from Apple). Push is on when this, the key ID and the team ID are all set |
| `VALET_APNS_KEY_ID`, `VALET_APNS_TEAM_ID` | | push: the key's ID and your Apple developer team ID |
| `VALET_APNS_TOPIC` | `br.com.rrayes.valet` | push: the apps' bundle ID |
| `VALET_APNS_SANDBOX` | off | push: `1` for development builds of the apps (APNs' sandbox) |
| `VALET_CHROME` | Playwright's Chromium | the Chrome the browser profiles run ([below](#giving-an-agent-the-browser)), e.g. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| `VALET_HEADLESS` | headless unless `DISPLAY` is set | `1`: Chrome without windows; `0`: with windows (on a Mac, which has no `DISPLAY`) |
| `VALET_BROWSER_IDLE` | `1800` | seconds a profile's Chrome stays up unused (no agent action, nobody watching) before it's stopped |
| `VALET_CHROME_NO_SANDBOX` | only as root | `1`: Chrome without its own sandbox (needed in a container as a non-root user, where Docker blocks the user namespaces it uses: the container is the boundary); `0`: never |
| `VALET_MAX_FILE_MB` | `100` | the largest file the shared space takes, in MiB (uploads over it are refused as they arrive) |
| `VALET_TZ` | the machine's | the time zone schedules run in, e.g. `America/Sao_Paulo` ([Always-on](#always-on)) |
| `VALET_OWNER_NAME` | `the owner` | what Valet's texts call you: the directive presets, tool descriptions, notifications ("Waits for the owner's approval" becomes "Waits for Rafa's approval" with `Rafa`). The authority presets are stored with `{owner}`/`{Owner}` in them and rendered at each agent start |
| `VALET_TRIGGER_GRACE` | `600` | seconds a due schedule or wake-up waits for an agent that can't take it yet, before it's missed |
| `VALET_CONTROL_URL` | `ws://127.0.0.1:8780/ws/runner` | runner: where to dial |
| `VALET_RUNNER_TOKEN` | (required) | runner: from `valet-control add-runner` |
| `VALET_RUNNER_DATA` | `./data/runner` | runner: workspaces of `host` agents, and every agent's compiled config (`agents/<name>/config`) |
| `VALET_AGENT_IMAGE` | `valet/agent-base` | runner: image for `container` agents without `computer.image`, and for every agent's network proxy (it needs `node`) |
| `VALET_DOCKER` | `docker` | runner: docker CLI |
| `VALET_HARNESS_CMD_<HARNESS>` | | runner: replace a harness's ACP command, e.g. `VALET_HARNESS_CMD_CLAUDE="python tests/fake_agent.py"` |
| `VALET_RECONNECT_MAX` | `30` | runner: reconnect backoff ceiling, seconds |
| `CLAUDE_CODE_OAUTH_TOKEN` | | runner: Claude subscription token from `claude setup-token`, passed through to Claude agents (never on a command line) |
| `ANTHROPIC_API_KEY` | | runner: API key alternative; Claude Code prefers it over the subscription when both are set |
| `DEEPSEEK_API_KEY` | | runner: passed through to DeepSeek Harness agents |

Each agent gets only its own harness's keys: a Claude agent never sees `DEEPSEEK_API_KEY`, and the reverse. The
runner tells the control plane which harnesses it has a key for (names, never values), so the settings page can
mark the others "no key".

`valet-control revoke-runner NAME` revokes a runner's token; a connected runner is dropped within 5 seconds.

### Deploying with Docker

The control plane runs in Docker (`docker/control/Dockerfile`: Python 3.13, Playwright's Chromium for amd64 and
arm64, headed on Xvfb, Node for stdio MCP servers, a non-root user), as a compose app (`docker/compose.yml`, written for Dokploy's raw compose, fine with
`docker compose`: the `valet-data` volume, the port on `127.0.0.1` and on the Docker bridge for agents). The runner runs on the
host itself as a systemd service (`docker/valet-runner.service`, settings in `docker/runner.env.example`), so `host`
agents work in your folders as you and container agents use the host's Docker:

```sh
docker build -f docker/control/Dockerfile -t valet/control:latest .
docker volume create valet-data
docker run -d --name valet-control -v valet-data:/data -p 127.0.0.1:8780:8780 \
    -e VALET_MASTER_KEY=... -e VALET_PUBLIC_URL=https://valet.example.com valet/control:latest
docker exec -it valet-control valet-control set-password
```

Without root, `docker/valet-runner.user.service` runs the runner as a systemd user service (paths with `%h`, your home).

`GET /api/me` is the health check. Every `valet-control` subcommand works in the container (`docker exec`).

## Setting up an agent

Each agent has a **Settings** page in the web UI: one page you scroll through, with a default for everything.

- **Identity**: a display name and an emoji, which is how the apps show the agent. The ID it got when it was
  created doesn't change; for a new ID, copy the agent.
- **Personality**: who it is and how it behaves, as plain text (`persona.md`). Start from a template (Assistant,
  Coder, Researcher or Blank) and edit it.
- **What it knows**: whether it's told about you, its standing authority (none or a preset), and whether it's told
  its environment; **Preview what it's told** shows the whole text, unsaved changes included. See
  [What agents are told](#what-agents-are-told).
- **Brain**: Claude or DeepSeek, each marked ready, or "no key" when the connected runners lack that harness's key.
  Then the model, from a menu, or Other for any model ID.
- **Computer**: its own computer, a private Linux container whose files are kept between restarts; no computer,
  for an agent that only talks and uses the tools you give it; or this machine, where it runs as the runner's user
  and reaches what that user can. Its own computer can get folders shared from the runner's machine, and an
  **Internet** choice: full internet, only its model and Valet's tools, or only the sites you list
  ([below](#its-computer-and-the-internet)).
- **Permissions**: how much it does without asking, one of four [levels](#permission-levels).
- **Tools**: the [MCP servers](#giving-an-agent-tools-mcp-servers-through-the-gateway) it can use, each a switch;
  a server it has lists its tools and whether a call runs, asks you, or is blocked at its level. `valet.browser`
  says it decides each call by its own site rules, and offers only Block.
- **Browser**: the [browser profiles](#giving-an-agent-the-browser) it uses, each a switch (the first one turns
  `valet.browser` on, the last one off), its own site rules, and its live "Allow for an hour" grants with Revoke.
- **Secrets**: the [secrets](#secrets) it gets as env vars, each marked set or not set, with a write-only field
  to set its value; a password from the [vault](#the-vault) shows its env var and its entry, and **A password from
  the vault…** adds one.

**Save** keeps the changes. A running agent carries on with its old settings until it restarts, and the app says
it needs one; **Save & restart** does both. A new name or emoji shows at once. A mistake (a shared folder that
isn't an absolute path, say) shows next to its field, and nothing is saved until it's fixed.

**Advanced** has the two files as text, for anything the form doesn't cover, and the **History**: every saved
version, newest first, each with **Restore**. Restoring writes that version back as a new one, so nothing is lost.
Saving the form rewrites `agent.yaml` in a standard layout: comments typed into the file by hand don't survive it,
though the history keeps them. Settings the page doesn't have yet (triggers) are kept as they are.

**Delete agent…**, at the bottom of the page, deletes it after a confirmation saying what goes. It stops, and Valet
forgets it: its folder (settings, personality, its own skills, its memory) leaves the definitions repo in one
commit (`Delete agent <name>`; the history keeps it), and its sessions, inbox items, Shared notes and files, queued messages, triggers,
wake-ups, grants and attachments go for good. The audit logs (gateway calls, the browser's, the computer's) stay.
Its runner removes its containers and the files it made for it there; its own computer's volumes go only if you
tick the box (left, they come back to a new agent with the same name). A host agent's working folder is yours and
stays: Valet never deletes it, nor anything else it didn't make. Its runner has to be connected (unless it was revoked). A new agent can then take the name and starts
clean. Every open screen of the agent, in the web UI and the apps, takes it off its lists.

**New agent** asks for a name (the ID is made from it), a template or an existing agent to copy, and a brain, then
**Create & start**. A template sets the personality, an emoji and a level: Coder starts at trusted, the others at
ask. It also turns on everything an agent can be told: about you, the authority preset suggested for its level, its
personality and its environment. A copy takes everything but the display name. Its personality comes as written, so if it names the original
agent, change that line.

### Permission levels

The level sets what the harness asks you about inside its computer. The computer is the harder limit: on its own
computer an agent reaches that container, its shared folders and what its Internet choice lets through; on this
machine it reaches what the runner's user can.

| level | Claude Code | DeepSeek Harness |
|---|---|---|
| **locked** | Only Read, Glob, Grep, WebSearch and TodoWrite exist, and its gateway's read-only tools: it reads, searches files and the web, and keeps a todo list. It can't edit files or run commands. | Valet's gate refuses every tool except reading and bookkeeping (read, grep, glob, web search, todos, goals, jobs, subagents). |
| **ask** (default) | Asks before edits and commands (Claude Code's `default` mode). | Valet's gate asks before every tool except reading and bookkeeping. |
| **trusted** | Own computer: never asks (`bypassPermissions`). This machine: edits without asking, still asks before commands (`acceptEdits`). | Own computer: never asks. This machine: the gate doesn't ask; dsh keeps its own `workspace-write` sandbox and asks before a command leaves it. |
| **unrestricted** | Never asks, on either computer (`bypassPermissions`). | Never asks, on either computer. On this machine dsh's own sandbox is off too. |

Reading never asks on its own computer, at any level. A new level applies from the agent's next start, to every
session it starts or resumes, older ones included: the runner sets Claude's mode on each, and Valet's gate puts
each DeepSeek session on the level's preset. Claude Code doesn't offer `bypassPermissions` to root: then the session
keeps a stricter mode and the runner logs it; if a harness can't give a locked or ask session its strict mode, the
session doesn't run. Permission rules and hooks an agent writes into its own Claude settings files don't count:
Valet's managed settings (`allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`) leave only its own rules in
force, so a trusted agent can't leave itself an allow rule that outlives a downgrade. The level also decides MCP tools, in the gateway rather than in the harness
([below](#giving-an-agent-tools-mcp-servers-through-the-gateway)), and what it does in the browser, with the
browser's own site rules on top ([below](#giving-an-agent-the-browser)).

### Its computer and the internet

| computer (`computer.level`) | what it is |
|---|---|
| **Its own computer** (`container`, default) | `docker run` of its image: `/home/agent` and `/work` are volumes kept between restarts, plus any shared folders. |
| **No computer** (`none`) | It talks and uses its gateway tools, nothing more. No built-in tools: Claude Code gets an empty tool list (`_meta.claudeCode.options.tools: []`); DeepSeek Harness gets every built-in tool row off in `dsh.patch.yml` and Valet's gate refusing anything but `mcp__valet__*`. It still runs in a container (the harness needs one), with an empty `/work` at every start, no shared folders (refused on save), and the internet set to "Only its model and Valet's tools". `/home/agent` is kept, so its sessions resume. |
| **This machine** (`host`) | A subprocess on the runner's machine, as the runner's user. The Internet choice doesn't apply, and the settings page doesn't show it: it reaches what that user can. Its MCP servers are still only the gateway: Claude Code runs with `strictMcpConfig`, so the user's own `~/.claude.json` servers (user and project scope) and a project's `.mcp.json` aren't loaded. It works in its **working folder** (`computer.workdir`, "Working folder" on the settings page): an absolute path on that machine, which must exist (the agent fails to start, saying so, if it doesn't), or by default a folder of its own the runner creates, `<VALET_RUNNER_DATA>/agents/<name>/work`. The harness runs there and every session starts there, and its environment layer names it. |

**Internet** (`computer.network`), for its own computer. Whatever the choice, the agent always reaches its model
and the MCP gateway (`VALET_GATEWAY_URL`'s host and port):

| choice | `network:` | reaches | Claude Code | DeepSeek Harness |
|---|---|---|---|---|
| **Full internet** | `open` (default) | anything | | |
| **Only its model and Valet's tools** | `gateway` | its model and the gateway | `api.anthropic.com:443` | `api.deepseek.com:443` (the `deepseek-official` route) |
| **Only these sites** | `allowlist` | that, and each site in `computer.allowlist` with its subdomains, on ports 80 and 443 | as above | as above |

`network: none` means the same as `gateway`: a container with no network at all couldn't reach its model, so the
agent couldn't answer. Web search is off under `gateway` and `allowlist`, since it runs at the model's provider,
outside the agent's computer, where no network setting holds it: Claude Code gets a managed deny rule for
`WebSearch` (and `locked` doesn't list it), DeepSeek Harness its `tool-web` row with `web_search` off. Fetching a
page (WebFetch, `web_fetch`) goes through the agent's own network, so it reaches the listed sites only.

```yaml
computer:
  network: allowlist
  allowlist: [github.com, pypi.org]   # github.com covers api.github.com; "https://..." or "*." is not needed
```

How: the agent's container sits alone on its own internal Docker network (`valet-agent-<name>-net`, no route out,
not even to the Docker host), next to a small HTTP proxy (`valet-agent-<name>-egress`, `valet/runner/egress.mjs`
run by `node` from `VALET_AGENT_IMAGE`) that is also on Docker's default network. Inside, `HTTPS_PROXY`,
`HTTP_PROXY` (and lowercase), `NO_PROXY=localhost,127.0.0.1,::1` and `NODE_USE_ENV_PROXY=1` point everything at
it; a program that ignores them can't connect at all (there's no DNS outside either). The proxy lets through
CONNECT and plain HTTP to what's allowed and answers anything else with a 403 that says why; the runner logs each
one (`network blocked CONNECT github.com:443`). The runner starts the network and the proxy before the agent, and
removes both when it stops; if the proxy dies, the agent fails with "its network proxy exited". A harness whose
model hosts Valet doesn't know (a DeepSeek provider route other than `deepseek-official`) only starts with
`allowlist`, its host listed, or `open`.

## What agents are told

Besides its personality, an agent can be told who you are, what it may do for you without asking (its standing
authority), and exactly what Valet set up for it. Each is a layer; an agent's `directives` lists the ones it gets,
and Valet joins their texts, in that order, into what the agent reads at the start of every session.

| layer | what it is | where it's edited |
|---|---|---|
| `owner` | **About you**: who you are, how you work, your languages, machines and accounts. One text, shared by every agent that lists it. | **Agents › About you & authority** (`owner.md` at the definitions repo's root) |
| `authority/<preset>` | **Standing authority**: what the agent may do for you without asking first, and what it must ask about. One preset per agent at most. | the same screen (`authority/<preset>.md`); picked per agent in its settings |
| `persona.md` | **Personality**, as before | the agent's settings |
| `environment` | **Its environment**, written by Valet at every start: its computer, its permission level and what that means in its harness, its MCP tools and what a call to each does (runs, waits for your approval, blocked), its browser profiles and how their rules decide, its skills (names and what each is for, and how its harness loads one), how its memory works if it has memory, what the shared space is if it has `valet.shared`, its secrets by name, and how Valet enforces your limits | nowhere: it follows from the settings |
| `memory` | **Its memory**, written by Valet at every start: how memory works, an index of its memory files, then the files themselves, newest first, while they fit in 32 KiB (the rest by name, to read with its tools). Empty while `memory` is off | the agent itself, with `valet.memory`; you, in its **Settings › Memory** ([Memory](#memory)) |

Valet ships three authority presets, created in the definitions repo the first time this version runs, and
editable like everything else there: **strict** (do what you ask, and only that), **standard** (do the work
without confirming each step; ask before messaging as you, spending money, deleting what it didn't create or
changing shared systems), and **full trust** (act for you: install and run software, manage your repositories, sign
in and fill forms, use every tool; ask before spending money, messaging people for you, or deleting what it didn't
create). Add your own with **New preset**; one no agent uses can be deleted. The settings page marks the preset
suggested for the agent's level: strict for locked, standard for ask, full trust for trusted and unrestricted.

The authority is what you want; the permission level is what Valet enforces. The environment layer tells the agent
both, so it acts instead of hesitating. For a trusted container agent with GitHub through the gateway and one
secret, it reads, in part (each paragraph is one line):

```markdown
## Permission level: trusted

Inside your container nothing waits for approval: edit files, run commands and install software as you need.

## Tools through Valet

They come from Valet's MCP server `valet`: your names for them are mcp__valet__<server>__<tool>, with dots in the server's name as _ (mcp__valet__github__get_issue). Valet decides every call. Tools run, except destructive ones (sending, deleting, publishing, paying...), which wait for the owner's approval. Their credentials stay in Valet.

- github: GitHub issues and pull requests. Runs: get_issue, create_issue. Waits for the owner's approval: delete_repo.

## Skills

Skills are instructions the owner gave you for particular tasks, read only when you need them. Load one with the Skill tool when a task calls for it; its name there is valet:<name>. They are read-only: you can't change them.

- code-review: Use when reviewing a pull request or a diff someone asks you to look at.

## Secrets

Set as environment variables in your computer: GH_TOKEN. Use them by name (like $GH_TOKEN); never print, log or send a value anywhere it doesn't belong.

## How Valet enforces this

Valet enforces these limits outside you: your computer is the boundary, Valet's gateway decides every tool call, and anything that needs the owner's approval waits in Valet's inbox, with a notification on the owner's phone. If something isn't allowed, Valet blocks it or asks the owner, so act and let Valet decide. Don't ask the owner in chat for permission Valet already checks; ask for what you need to know, or for what only the owner can decide.
```

The text goes to the harness as written, no templating: Claude Code gets it appended to its own system prompt, and
DeepSeek Harness as its `AGENTS.md`. Editing the owner profile or a preset makes the running agents that list it say
they need a restart, as a settings change does; the environment and memory layers are written fresh at each start
(an MCP server that takes over 10 seconds to list its tools is shown as unavailable), and a change to its memory
never asks for a restart: the agent reads it fresh with its tools. An agent listing a preset that doesn't exist
can't be saved from the settings page or started: pick another in its settings. Agents created before this keep
`directives: [persona.md]` until you turn the others on.

## Defining an agent

An agent is a directory in the definitions repo: `agents/<name>/agent.yaml` and `agents/<name>/persona.md`. The
settings page and the API write it, and every change is a commit (author `Valet <valet@localhost>`). What agents
share sits at the repo's root: `owner.md`, `authority/<preset>.md` and `mcp.yaml`. New agents
from the settings page start from a template in `valet/shared/catalog.py` (which also lists every directive: see
[What agents are told](#what-agents-are-told)) or a copy of another agent; `POST /api/agents {"name": "helper"}`
with nothing else starts from this one:

```yaml
name: helper            # the ID: lowercase letters, digits, dashes, not `owner`; must match the directory
# display_name: Helper  # how the apps show it: one line, at most 40 characters (default: the name)
# emoji: 🧭             # shown next to it: one, no spaces
harness: claude          # claude | deepseek
computer:
  level: container      # container (default) | none (no computer: only its gateway tools) | host (the runner's machine)
  # image: valet/agent-base
  # network: open       # open | gateway (only its model and Valet's tools) | allowlist (+ these sites); none = gateway
  # allowlist: [github.com]
  # mounts: [{host: /Users/me/notes, path: /notes, readonly: true}]
  # workdir: /Users/me/Code/project   # host only: its working folder (default: its own under the runner's data)
permissions: ask        # locked | ask | trusted | unrestricted (see Permission levels)
directives: [persona.md]  # what it's told, in order: owner, authority/<preset>, persona.md, environment, memory
# memory: true          # its own memory: valet.memory through the gateway (see Memory)
# model: claude-sonnet-5-5   # claude: ANTHROPIC_MODEL; deepseek: e.g. deepseek-v4-pro (default deepseek-v4-flash)
# provider: deepseek         # deepseek only: a dsh provider route (deepseek -> deepseek-official); needs a model
# runner: mac                # pin to a runner; otherwise its last runner, else the first connected one
```

How each harness gets the definition:

| | Claude Code | DeepSeek Harness |
|---|---|---|
| command | `claude-agent-acp` | `dsh --profile acp` |
| `directives` | their texts joined in order, appended to Claude Code's system prompt (`_meta.systemPrompt.append` on `session/new`) | their texts joined in order, `/valet/agents/AGENTS.md`, read as user-global instructions (`DSH_AGENTS_HOME`) |
| `model`, `provider` | `ANTHROPIC_MODEL` | `/valet/dsh.patch.yml`, a `--patch` layer over the `acp` profile |
| sessions after a restart | `session/load` (history replayed) | `session/resume` (no replay; history stays in `~/.dsh`) |
| `permissions` | a Claude Code permission mode, set with `session/set_mode` on every session; managed settings in `_meta.claudeCode.options.managedSettings` (only Valet's permission rules and hooks count; reading pre-approved on its own computer, the gateway's tools, `mcp__valet`, everywhere: the gateway decides those); for `locked`, only read-only built-in tools (`_meta.claudeCode.options.tools`), plus `Skill` if it has skills | Valet's gate (a dsh plugin Valet writes into the config dir) with the level's mode, and dsh's own `permission` preset in `/valet/dsh.patch.yml`. Reading and bookkeeping, and the gateway's tools (`mcp__valet__*`), pass the gate at every level. dsh alone would only ask when a command leaves its sandbox |
| the gateway (`tools.mcp`) | an `http` MCP server `valet` in every `session/new`, `session/load` and `session/resume` | an `mcp-client` row in `/valet/dsh.patch.yml`, the token read from its env (`VALET_GATEWAY_TOKEN`), never in the file |
| skills | a local plugin `valet` at `/valet/claude-plugin` (`_meta.claudeCode.options.plugins`), and the SDK's `skills` filter set to exactly its skills (`valet:<name>`), so it sees no others | `/valet/skills`, the only root of dsh's `skill-filesystem` (`includeDefaultRoots: false`, in `/valet/dsh.patch.yml`) |
| permission choices | allow / reject, once or always | allow / reject, once |
| its own sandbox | none | in a container: off (dsh can't confine there), the container is the boundary; on the host: `workspace-write`, except for `unrestricted` |
| replies | streamed token by token | each message arrives whole when it's done |

DeepSeek Harness also runs with telemetry off (`DSH_TELEMETRY_MODE=DISABLED`). Its ACP server waits for Valet's
gate to load, so if the gate can't load the agent fails to start rather than running without approvals. The full `agent.yaml` shape from
DESIGN.md parses and is stored (tools other than MCP servers); only the fields above, `secrets`, `tools.mcp`,
`tools.skills`, `browser`, `memory` and `triggers` (below) do anything yet, and `display_name` and `emoji` only in
the apps.
An edit takes effect the next time the agent starts; until then the agent says it needs a restart (a new display
name or emoji shows at once).

### Secrets

A token an agent needs (a GitHub token, an npm token, an API key for its own scripts) is a **secret**: you set its
value once in Valet, and every agent that lists it gets it as an env var of the same name inside its computer.

In the web UI, an agent's **Settings › Secrets** lists the secrets it gets: add one by name, and set its value
right there (the field is write-only: a value is never shown again). **Secrets & tools** (on the agents list) shows
every secret with what uses it, to set, change or delete. Starting an agent whose secret isn't set asks for the
value on the spot, then starts it. Or set it from the server's shell (the value is read without echo, or from a
pipe, never from the command line):

```sh
uv run valet-control set-secret GITHUB_TOKEN        # prompts for the value
uv run valet-control set-secret NPM_TOKEN < npm-token.txt
uv run valet-control delete-secret GITHUB_TOKEN
```

or through the API (`PUT /api/secrets/GITHUB_TOKEN {"value": "..."}`, owner auth; `GET /api/secrets` lists names
and which agents and MCP servers use them, never values). Then list it in the agent's definition and start (or
restart) it:

```yaml
secrets: [GITHUB_TOKEN]
```

Inside the agent, `$GITHUB_TOKEN` is set. If a listed secret isn't set, the start is refused with a message naming
it. Change a secret's value and the agents running with it show "restart to apply". Names are env var names
(`GITHUB_TOKEN`), and can't be ones Valet or the harnesses set (`HOME`, `PATH`, `ANTHROPIC_*`, `CLAUDE_*`,
`DEEPSEEK_*`, `DSH_*`, `VALET_*`, `DOCKER_*`...; the full rule is in [docs/API.md](docs/API.md#secrets)): the
harness's own key stays in the runner's environment.

A password saved in the [vault](#the-vault) can be given the same way, by its entry's name: `- vault: aws/alice`
sets `$AWS_ALICE_PASSWORD` (the name in capitals, `/`, `.` and `-` as `_`, then `_PASSWORD`; `VAULT_` in front
when the name starts with a digit or a reserved prefix, so `0/alice` sets `$VAULT_0_ALICE_PASSWORD`), or
`- {vault: aws/alice, env: AWS_PW}` sets `$AWS_PW`. An entry that doesn't exist or has no password refuses the start
(`missing_secrets` says `vault:aws/alice`); a new password makes the agents running with it show "restart to apply".
The username and the 2FA secret stay in the vault.

A secret can also be an MCP server's credential, `${secret:NAME}` in `mcp.yaml` ([below](#giving-an-agent-tools-mcp-servers-through-the-gateway)).
That one stays in the control plane: the agent doesn't list it and never sees it.

The control plane keeps them encrypted (AES-256-GCM) with a key in
`$VALET_DATA/master.key`, or `VALET_MASTER_KEY`. That key protects the database on its own (a copy or a backup of
`valet.db`); whoever can read both can read the secrets, so keep the key out of backups of the database, or set
`VALET_MASTER_KEY` from elsewhere. Values reach the runner only in the `agent.start` command and live only in the
harness process's environment (for a container, through the docker CLI's env: `docker inspect` on the container
shows them, as it does the harness's key).

## Giving an agent tools: MCP servers through the gateway

Agents never connect to MCP servers themselves. The control plane runs an MCP gateway, and each agent with gateway
servers (those its `tools.mcp` lists, and `valet.memory` when it has `memory: true`) gets one endpoint on it,
`/mcp/agents/<name>`, with a token of its own (issued when it starts, revoked when it stops). The runner hands it to the harness as an MCP server called `valet`. The gateway holds the servers'
credentials, decides every call from your rules and the agent's `permissions`, asks you in the inbox when it should, and logs
every call.

**1. Register the server** in `mcp.yaml` at the root of the definitions repo: in the web UI under **Secrets & tools ›
Edit servers file**, or `PUT /api/gateway/registry`, or edit it there and commit:

```yaml
servers:
  github:                                  # a remote server, streamable HTTP
    description: GitHub issues and pull requests
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: Bearer ${secret:GITHUB_TOKEN}
  notes:                                   # a stdio server the control plane starts itself
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /srv/notes]
    env:
      LOG_LEVEL: info
      NOTES_KEY: ${env:NOTES_KEY}
```

`${secret:NAME}` is one of Valet's [secrets](#secrets), `${env:NAME}` a variable of the control plane's
environment. Both are filled in when the gateway connects to the server, and a changed value reconnects it at its
next use, without restarting any agent. Credentials stay in the control plane and never reach the agent, the
runner, the API (which shows header and env names, and the secrets a server uses by name), the logs or the call
log: an upstream error that quotes one shows `[redacted]` instead. A server whose secret isn't set doesn't connect
(`GET /api/agents/<name>/tools` says which), and its tools are missing until it's set. Server names are
lowercase letters, digits, `-` and single `_`; names starting with `valet` are Valet's own.

**2. Give it to the agent**: switch it on in its **Settings › Tools** (or list it in its `agent.yaml`), and
restart it:

```yaml
tools:
  mcp: [github, valet.agents]
```

`valet.agents` (the agents and their status, and `send_message` to one of them) and `valet.owner` (messages and
questions for you, and wake-ups: see [Always-on](#always-on)) are built in, and so are `valet.browser`
([below](#giving-an-agent-the-browser)), which decides each call by the browser's site rules instead of the table
below, and `valet.shared` ([the shared space](#the-shared-space)); `valet.memory` isn't listed here: it comes with
`memory: true` ([Memory](#memory)), and Settings › Tools doesn't offer it. The agent sees each tool as
`<server>__<tool>` (`github__create_issue`, `valet_agents__list_agents`; Claude Code shows them as
`mcp__valet__...`). Settings › Tools lists a server's tools with what a call does at the agent's level ("runs",
"asks you", "blocked"), the level you've picked included before you save it (`GET /api/agents/<name>/tools`,
`?permissions=` for another level).

**What happens on a call**, by the agent's `permissions` and the tool's MCP annotations:

| permissions | read-only tools (`readOnlyHint`) | others (additive: `destructiveHint: false`) | destructive, or unannotated |
|---|---|---|---|
| `locked` | run | refused (and not listed) | refused (and not listed) |
| `ask` | run | ask you | ask you |
| `trusted` | run | run | ask you |
| `unrestricted` | run | run | run (logged) |

An ask is an inbox item of kind `tool` (server, tool, arguments); the call waits for your answer, up to
`VALET_GATEWAY_ASK_TIMEOUT` (each harness is told to wait as long as the gateway may hold a call: the ask,
then `VALET_GATEWAY_CALL_TIMEOUT`, plus a minute):

- **Allow once**: this call.
- **Allow for an hour**: this call, and the same tool for this agent for the next hour without asking. Its
  **Settings › Tools** lists the hour's grants, each with **Revoke**.
- **Always allow**: this call, and a rule in its `agent.yaml` (one commit, "Inbox: always allow github create_issue
  for scout") so it never asks for that tool again. The rule shows in its Settings › Tools, where you can take it
  back.
- **Reject**: the agent is told you said no.

**Rules** come before the level. In **Settings › Tools**, each tool of a server it has can be set to run, ask you or
be blocked, or left to its level, and so can all of a server's tools at once; they're `tools.rules` in
`agent.yaml`:

```yaml
tools:
  mcp: [github]
  rules:
    github:
      create_issue: allow     # runs without asking, even when the agent is locked
      delete_repo: deny       # never: not even listed to the agent
      "*": ask                # every other github tool asks you, whatever the level
```

A tool's own rule wins, then its server's `"*"`, then the level (the table above). An hour's grant turns an ask,
from a rule or the level, into a run; never a block. Rules apply at the next call: changing them needs no restart. The harness itself doesn't ask again for gateway tools: Claude Code gets a
managed allow rule for them (`mcp__valet`), and DeepSeek Harness's gate lets `mcp__valet__*` through. Every call,
with its decision and outcome, is in `GET /api/gateway/calls`.

**Reaching the gateway**: the runner gives agents `$VALET_GATEWAY_URL/mcp/agents/<name>`. A host agent can use
the default. A container agent needs an address its container can reach: on Docker Desktop,
`VALET_GATEWAY_URL=http://host.docker.internal:8780` (the runner adds `--add-host host.docker.internal:host-gateway`
to every container on the full internet, and to the network proxy of the others, so the same name works on Linux,
where the control plane must also listen on an address the Docker bridge reaches, e.g. `VALET_HOST=0.0.0.0` behind
a firewall). A restricted agent reaches the gateway through its proxy. The harness must take HTTP MCP servers
(Claude Code and DeepSeek Harness both do).

## Giving an agent the browser

The browser is a real Chrome with your logins, in **profiles**. Each profile is a Chrome of its own with its own
user-data-dir (`$VALET_DATA/browser/profiles/<name>`), so a site you sign in to in a profile stays signed in for
every agent that uses it. Chrome runs as a person runs it: no automation flags, so Google sign-in works. It starts
the first time something uses the profile, and stops after `VALET_BROWSER_IDLE` seconds unused. Agents use it
through `valet.browser`, one of Valet's own MCP servers. They never get JavaScript, cookies or saved passwords:
those would bypass the rules.

In the web UI it's the **Browser** tab: the profiles, whether each is running (with **Stop**), the live hour
grants with **Revoke**, the activity log (who did what where, the decision, the screenshot it asked with; **Older**
pages back, and it filters by agent and profile), the [vault](#the-vault), and `browser.yaml` as text under
Advanced. A profile's own screen has its title, note and site rules (for each site, each kind of action: Not set,
Run, Ask you or Block), its tabs with **View**, **Open a tab**, its saved logins, its activity, and **Delete
profile**, which asks whether to also wipe its logins (cookies and sessions: every site signs it out; without that
they stay on disk, and a new profile of the same name gets them back).

**1. Define the profiles**: **New profile** in the Browser tab, or in `browser.yaml` at the root of the
definitions repo (`PUT /api/browser/yaml`, the profile routes in [docs/API.md](docs/API.md#the-browser), or edit it
there and commit):

```yaml
profiles:
  alice:
    title: Personal logins          # how the apps show it: one line, at most 60 characters
    note: Personal accounts.      # shown to you and to the agents that use it
    sites:                        # this profile's rules, for every agent that uses it
      github.com: {commit: allow}
      "*.google.com": {read: allow, login: ask}
      admin.shopify.com/store: {commit: deny}
  work: {}
```

Profile names are lowercase letters, digits and dashes, at most 31. A profile can't be deleted while an agent lists
it or the vault has logins for it; deleting it stops its Chrome, and `?wipe=true` also deletes its data (every site
signs out).

**2. Give the agent the server and its profiles**: switch them on in its settings' **Browser** section (that
turns `valet.browser` on in Tools too), or in its definition, and restart it:

```yaml
tools:
  mcp: [valet.browser]
browser:
  profiles: [alice]
  rules:                          # its own rules, which beat the profile's
    github.com: {commit: allow}
```

An agent that lists a profile `browser.yaml` doesn't have can't be saved from the settings page or started. Its
tools take a `profile`, one of its own: `profiles`, `tabs`, `open_tab`, `navigate`, `snapshot` (the page as an
accessibility tree with refs like `e12`, in parts when it's long), `screenshot`, `click`, `type_text`,
`select_option`, `press_key`, `scroll`, `back`, `close_tab`, `login` and `request_human`.

**How a call is decided.** Each action is one of four kinds: **read** (open pages, read them, take screenshots,
scroll), **interact** (click links, type, choose options), **commit** (a button that saves, sends, pays, deletes,
publishes, signs out..., a submit button, Enter in a form, typing with `submit`) and **login** (Valet fills a saved
login). For the action's kind on its page, the first of these that says something decides: the agent's own rule
for the most specific of its sites that matches, then the profile's, then the agent's level:

| permissions | read | interact | commit | login |
|---|---|---|---|---|
| `locked` | run | refused | refused | refused |
| `ask` | run | ask you | ask you | ask you |
| `trusted` | run | run | ask you | run |
| `unrestricted` | run | run | run | run |

A rule can say `allow`, `ask` or `deny`, and beats the level both ways: `allow` lets even a locked agent do it,
`deny` stops even an unrestricted one. It names only the kinds it sets; the others fall through. Sites are the old
Valet's patterns: a host (`github.com`), `*.google.com` (the domain and every subdomain), `*` (everything), a glob
with `*` and `?`, optionally followed by a path prefix (`admin.shopify.com/store`). When several match, the longer
path wins, then fewer wildcards, then the longer host. Only `http(s)` pages (and `about:blank`) open at all. The
gateway's tool-annotation table doesn't apply to `valet.browser`: its tools are listed at every level, and nothing
asks twice. Every action, with its decision and outcome, is in the browser's audit log
(`GET /api/browser/audit`).

**Asks** land in the inbox as items of kind `browser`, and in the agent's conversation when a turn is running: the
site, the profile, what the agent wants to do (`click "Save"`, the text it would type, the login it would use), a
screenshot of the page, **Look at it live**, and four answers:

- **Allow once**: this action.
- **Allow for an hour**: this kind of action on this site, for this agent, for an hour (`GET /api/browser/grants`;
  revoke one with `DELETE`).
- **Always allow**: writes the rule into the agent's `browser.rules`, as a commit ("Inbox: always allow commit on
  github.com for scout"). Rules are read at every call, so it applies at once, without a restart.
- **Reject**.

The call waits up to `VALET_GATEWAY_ASK_TIMEOUT`. The agent doesn't hold the browser while it waits, so you can look
in the live view first; if the tab has moved to another site by the time you answer, nothing is done and the agent
is told why.

**Signing in.** `login()` fills the saved username, password and 2FA code into the page itself (never into an
iframe from another site, and never once the page has left the login's site). The agent never sees them:
`type_text` refuses password fields and any text containing a vault password, page text and URLs have every vault
password (of 4 characters or more), 2FA secret and password-field value masked as `••••••`, and screenshots blank
password fields. Page text
reaches the agent marked as untrusted website content. When a page needs a person (a captcha, a code sent by SMS
or email, a passkey or phone prompt), `request_human()` puts a `browser` item in the inbox with **Open the live
view**, **Done** and **I can't**, and the agent waits while you take over in the live view.

### The vault

The vault holds saved logins, each for one profile and one site: a unique name (`github/alice`: lowercase, up to
three parts separated by `/`), the site (a domain covers its subdomains, path or not, as a password manager does:
`instagram.com` covers `www.instagram.com`; a pasted URL loses its scheme and `www.`), a username, a password, a 2FA
secret (the base32 key, or the `otpauth://` link of its QR code) and a label. Add them in the web UI (Browser ›
**Vault**, or **New login** on a profile's screen, which picks the profile), or through `POST /api/vault`. The
vault lists them by profile; an entry's screen edits its site, username and label, and takes a new password or 2FA
secret in a field that's never filled (it says Saved or Not set, and **Clear** removes one at the save). The API
lists them with `has_password` and `has_totp`, never a value, and a password or 2FA secret can be replaced or
cleared but not read back. They're encrypted like secrets (AES-256-GCM, the master key, each bound to its own
name). `login()` picks the most specific entry of the profile that covers the page, or the one it names. An entry's
password can also be given to an agent as an env var ([Secrets](#secrets)).

### The live view

**Live view** (on the Browser tab, a profile's screen, or an ask) fills the window with the profile's tab: the
address bar, Back, Reload, the tab menu, **+ Tab**, and zoom (Fit, 2×, 3×). With a mouse, click, drag, double-click
and scroll on the page, and type straight into it once you've clicked it (paste too). On a phone, tap to click;
at Fit a swipe scrolls the page, zoomed in a swipe moves around it; type in **Type into the page**, and use the key
row (Enter, Backspace, Tab, Esc, the arrows, scroll up and down). A bar says when an agent's asks for the profile
are waiting (**Show** answers them there, **Done** included), when an agent is using the browser (what you do waits
for it), and when your input was dropped. Leaving the screen, or putting the app in the background, closes the
socket, so the browser can idle again.

Under it, `/ws/browser/<profile>` (owner auth, Valet's own origin) streams a tab as JPEG frames and takes your input: taps
and clicks, scrolling, typed text, special keys, an address to open, back, reload, and which tab to show or a new
one. Opening it starts the profile's Chrome. Your input waits for an agent's action in progress (and is dropped,
saying so, after 5 seconds), so it never lands in the middle of one; a browser being watched isn't stopped for
idling. The protocol is in [docs/API.md](docs/API.md#the-live-view); `GET /api/browser/running` lists the running
browsers, and `POST /api/browser/profiles/<name>/stop` stops one.

**Headless or not**: Chrome runs headless unless there's a `DISPLAY` (on a Linux server, Xvfb), or
`VALET_HEADLESS=0`. Some sites refuse a headless browser, Google sign-in among them: sign in with a window
(`VALET_HEADLESS=0` on a Mac, Xvfb on a server), and the profile keeps the session. `VALET_CHROME` picks the Chrome
(Google Chrome is closest to what you use; the default is Playwright's Chromium, installed with
`uv run playwright install chromium`).

## Giving an agent skills

A **skill** is a set of instructions an agent loads when a task calls for it: how you want code reviewed, how to
write release notes, how to use a tool of yours. It's a directory in the definitions repo, in the Agent Skills
format both harnesses read: a `SKILL.md` with a name and a description, then the instructions, and any other text
files the instructions point to.

```markdown
---
name: code-review
description: Use when reviewing a pull request or a diff someone asks you to look at.
---

1. Read the whole diff first, then the tests.
2. Say what you would change, most important first. See checklist.md.
```

The agent sees only the description until it decides to use the skill, so say *when* it applies. There are two
kinds:

- **Shared skills**, `skills/<name>/`: the library. An agent gets the ones its definition picks, by name or all
  of them (`"*"`, which includes any added later):

  ```yaml
  tools:
    skills: [code-review, release-notes]   # or ["*"]
  ```

- **An agent's own skills**, `agents/<agent>/skills/<name>/`: only for it, always given, no list needed. One with
  the same name as a shared skill is used instead of it. Copying an agent copies its own skills.

Make and edit them in the web UI (**Skills** from the agents list for the library; an agent's **Settings › Skills**
to switch shared ones on and add its own), through the API ([docs/API.md](docs/API.md#skills)), or as files in
the repo. Every change through Valet is a commit. Names are
lowercase letters and digits with single dashes (`code-review`, at most 64 characters) and match the directory;
the description is required (at most 1024 characters); files are UTF-8 text, at most 50 files, 256 KiB each and
1 MiB a skill, with relative paths and no hidden files or links. An agent carries at most 16 MiB of skills.

Skills go to the runner with the agent's start and are written into its config dir, which the agent can only read
(`/valet` in a container): **the agent can't change its own skills**. An agent has exactly the skills its
definition gives it: Claude Code's built-in skills, DeepSeek Harness's bundled ones, and any skill an agent writes
into its own home or workspace don't load. A change to a skill an agent has, or to which skills it picks, shows as "restart to apply". A picked name
the library doesn't have, or a skill that breaks the rules above (say, a `SKILL.md` without a description), is
left out of the start rather than failing it; the settings page and `GET /api/agents/<name>/skills` say which
and why. Loading a skill never asks permission; what the skill then has the agent do goes through its
permissions as usual.

## Memory

An agent with `memory: true` keeps notes for itself across sessions and restarts: Markdown files, one per subject,
in the definitions repo at `agents/<name>/memory/<file>.md`. It gets Valet's `valet.memory` server through the
gateway without listing it, with six tools: `list`, `read`, `write` (a whole file), `append`, `delete` and `search`.
Its calls never wait for your approval and are never refused, at any level: memory is the agent's own notebook. It
only ever reaches its own files. Each change is one commit (`Memory: scout wrote notes.md`), and a change never
needs a restart.

With the `memory` layer in its directives, every session starts with its memory in what it's told: how memory
works, an index of the files (name, title, size, date), and the files themselves, newest first, while they fit in
32 KiB; the ones that don't are named, for the agent to read with `read`. The environment layer explains the tools.

Turn it on in **Settings › Memory**: the switch sets both `memory` and the layer. The same section lists the files;
open one to read it (as Markdown), edit it, delete it, or look through its history and put an older version back
(through the API: [docs/API.md](docs/API.md#memory)). Your changes are commits too (`Memory: owner wrote scout's
notes.md`), and memory commits stay out of the agent's settings History. File names are lowercase letters and
digits with single dashes (`project-notes`, at most 64); a file holds at most 64 KiB, an agent at most 200 files
and 1 MiB. Copying an agent doesn't copy its memory.

## The shared space

Each agent has its own shared space: notes and files between you and that agent only, newest change first, in the
**Shared** section of the agent's page in the web UI. No other agent sees it. You and the agent can both add, edit or
delete anything in it. A note is a title and Markdown; a file is its bytes (in `$VALET_DATA/shared/`), its name and
an optional description. What the agent adds or changes is new until you've looked: a dot on the item, and "2 new in
Shared" under the agent in the agents list. The section searches titles and text, filters by who added it (you or
the agent) and kind, takes notes and uploads (several files at once, up to `VALET_MAX_FILE_MB` each); an item's page
shows notes and Markdown and text files as text and images inline, and downloads.

Shared used to be one list for every agent. Upgrading gives each item to the agent that added it (else the one that
last changed it), if that agent still exists; the rest wait under **Shared items with no agent** on the agents list
(shown only while there are any), where you move each to an agent or delete it. No agent sees those.

An agent gets it by listing `valet.shared` in `tools.mcp`, and its tools reach its own space and nothing else (another
agent's item id is "no such item", for every tool and every transfer URL). Its tools: `shared_list` and `shared_get`
(read-only), `shared_add_note` and `shared_add_file` (additive), `shared_update` and `shared_delete` (destructive),
decided like any tool at its level: at `ask`, an agent's addition waits for your approval. Files travel inline when
they're small: text up to 128 KiB as text, images up to 1 MiB as images, anything else up to 16 KiB as base64, and an
agent sends one as `content` or `content_base64` (up to 1 MiB). A bigger file goes through a **transfer URL**:
`shared_get` gives a download URL, and `shared_add_file` or `shared_update` with `upload: true` an upload URL to PUT
the file to, both on the agent's own gateway endpoint (`/mcp/agents/<name>/shared/<ticket>`). A URL is good for one
file, for 15 minutes, while the agent runs with the gateway token it had (a restart ends it), and an upload URL for
one PUT. The URL is the credential, so the agent fetches it from its shell with plain `curl` and its gateway token
never reaches the shell; a URL only comes from a call the gateway already decided, and each transfer is in the call
log. An agent with no computer has no shell, so its files travel inline only.

## Using it

**Start** the agent (**Create & start** does both), open **New session**, and talk to it. When the agent asks for
permission, the request shows up inline in the session and in the **Inbox**, with Allow and Reject (and, for a
tool through the gateway, Allow for an hour and Always allow). Pending requests survive a page reload and a
control-plane restart. The iOS and macOS apps ([apps/README.md](apps/README.md)) do the same, and get
each new inbox item as a push with its answers as notification buttons.

### Notifications

Valet tells you when something new waits in the inbox, once per item, and when an agent adds a note or a file to
its shared space, two ways (the agents list's
**Notifications** screen sets them up, with **Send test**):

- **A webhook**: a URL Valet POSTs JSON to, like a Slack or Discord incoming webhook. The message says which agent
  wants what ("🔐 Valet: Scout wants to use github › Create issue. Answer in the inbox: ...") and links to the
  inbox at `VALET_PUBLIC_URL`; never the tool's arguments. Best effort: 10 seconds, no retries.
- **Push** to Valet's iPhone and Mac apps, through Apple's push service: time-sensitive, naming the agent and
  what it wants, for the apps to answer from. The apps register their device when you let them notify you; the screen lists the devices, to
  remove one. The control plane needs your APNs auth key: create one under Certificates, Identifiers & Profiles ›
  Keys (Apple Push Notifications service), download its `.p8`, and set `VALET_APNS_KEY_FILE`, `VALET_APNS_KEY_ID`
  and `VALET_APNS_TEAM_ID` (and `VALET_APNS_SANDBOX=1` for development builds of the apps).

### Continuing a Claude Code session

A conversation you had with Claude Code outside Valet (in a terminal, say) can go on here,
with all its context, as the same session: stop it there, then continue it in a Claude Code agent on the same
machine (`computer.level: host`) whose working folder is the folder it ran in. On the agent's page,
**Continue a Claude Code session** lists the sessions Claude Code ran in that folder, newest first, by the title
Claude Code shows; **Continue here** loads one (ACP `session/load`: Claude Code reads its own transcript), its
history appears in the session as it was, and you go on from there. Only that agent kind can: the transcripts are
in `~/.claude/projects` of the runner's user, and a container has its own. A session Valet already has is listed
with the agent that has it. Don't run it in two places at once: Claude Code would append to the same transcript
from both.

### Handing a conversation off

Any conversation can start a new session of any agent, whatever its harness or level: **Hand off a conversation**
on the agent's page picks a Claude Code session the runner can read (by project folder) or a Valet session of any
agent, shows the message it makes, and **Start the session** sends it as the new session's first message, with
what you want done now after it. The message is the conversation condensed without a model: what was said, each
tool call as one line (`Bash: npm test`), thinking and tool output left out, the newest part that fits in 60 KB,
under a header saying where it came from and how much didn't fit. The agent knows what was said, not what was in
the files: it reads them again if it needs to. In the session it shows folded as "Handed off: ...".

### Attaching images and files

The **+** beside the message box attaches files; you can also paste an image into it or drop files on the
conversation. Each shows above the box as it uploads (x takes it off); up to 10 a message, each up to
`VALET_MAX_FILE_MB`. When you send, an image (PNG, JPEG, GIF or WebP, up to 5 MiB) goes to the agent in the message
itself if its harness takes images (it says so when it starts: `promptCapabilities.image`; Claude Code does).
Anything else is put in its computer, in `attachments/` of its workspace (`/work/attachments` in its container,
`<working folder>/attachments` on this machine; a name already there gets `-2`), and the message tells it the path
and size. A message sent while it's working waits in the queue with its attachments. An agent with no computer
only takes images, and only if its harness does. The session shows images as
pictures and files as links to them in the agent's files.

### Its computer: terminal, files and volumes

On the agent's page, while it runs on its own computer or this machine (not with no computer):

- **Terminal** opens a shell in its computer: in its container, as its user, in `/work`; on this machine, as the
  runner's user in its working folder, with a clean environment (none of the runner's variables, so none of its
  keys). **New terminal** opens another (up to 8), each in its own tab; closing a tab, or leaving the page, ends
  its shell and everything started from it. It follows the window's size. On a phone, a key bar adds Esc, Tab,
  Ctrl and the arrows. What the shell prints goes to your browser only, never into a log.
- **Files** browses its computer: its workspace and home (and shared folders) in a container, its working folder on
  this machine, never outside them, not even through a link. Open folders, view a text file or an image, download
  anything, upload (several at once, `VALET_MAX_FILE_MB` each; a name already there asks Replace or Keep both),
  make a folder.
- **Volumes** (on the Files screen of an agent with its own computer): its home and workspace are Docker volumes,
  kept between restarts. Once it's stopped, **Delete volumes** deletes them for good, after a confirmation naming
  them: its files, logins and history; it starts afresh next time. Refused while it runs.

Each terminal opened and closed (when, its size, how long, how it ended; not what was typed or printed), file
viewed, downloaded or uploaded, folder made, attachment placed and volume deletion is kept in the agent's computer
audit (`GET /api/agents/<name>/computer/audit`). A runner from before this needs updating first: the screens say so.

## Always-on

Besides you typing, an agent is prompted by its **triggers** (schedules and webhooks), by other agents, by your
answers to its questions, and by wake-ups it sets itself. Set triggers in its **Settings › Triggers**, or in its
`agent.yaml`:

```yaml
triggers:
  - schedule: "0 8 * * 1-5"       # cron: minute hour day-of-month month day-of-week
    prompt: Read the overnight build logs and tell me what broke.
  - webhook: deploy               # POST /hooks/<agent>/deploy
    prompt: A deploy finished; check the release notes.   # optional for a webhook
    session: new                  # main (default) | new: a new session each time
```

**Schedules** are cron expressions: five fields with lists, ranges, steps and names (`*/15 * * * *`,
`30 9 * * mon-fri`, `0 0 1 jan,jul *`), or `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`. Sunday is
`0` or `7`; a day-of-month and a day-of-week both restricted mean either one (cron's rule). They run in the
control plane's time zone, `VALET_TZ` (else the machine's), across daylight-saving changes. The settings page says what a schedule means and
when it runs next as you type it. A schedule fires only while its agent is meant to be running: a stopped agent's
runs are recorded as skipped. A run that comes due while the agent can't take it (its runner offline, the agent
still starting) waits for it up to `VALET_TRIGGER_GRACE` (10 minutes), then is recorded as missed, with the
reason. After the control plane or the agent was down, only the latest due run of each schedule catches up; the
earlier ones are recorded once, as missed. A new or edited schedule starts from now.

**Webhooks**: Valet serves `POST /hooks/<agent>/<name>` on the control plane's public address. The settings page
shows the address and its key (made the first time you look; **New key** replaces it, and the old one stops
working at once). The caller sends the key in one of three ways:

```sh
curl -X POST "$URL" -H "Authorization: Bearer vhk_..." -H "Content-Type: application/json" -d '{"version": "2.4"}'
curl -X POST "$URL?key=vhk_..." -d 'anything'
# or as the Secret of a GitHub webhook (content type JSON or form): Valet checks X-Hub-Signature-256
```

The prompt is the webhook's `prompt`, then what the call sent (JSON pretty-printed, text as it is, at most 20,000
characters of it, with a few headers like `X-GitHub-Event`) between two `UNTRUSTED WEBHOOK DATA` markers: the
agent is told it's data from outside, never instructions. Keys are stored encrypted with the secrets' master key.
The answer is `202 {"run_id", "outcome": "started" | "queued", ...}`; `503` if the agent is stopped or down (the
call isn't kept: send it again later), `401` with no key at all, and `404` for a wrong key, signature, agent or
webhook alike, so a caller can't learn which agents exist. Bodies are at most 1 MiB (413); each webhook takes 30
calls a minute and all of them together 300 (429); an address that fails 20 times in 10 minutes is refused for
the rest of that window.

**The main session.** Each agent has one: the conversation where its triggers (unless they say `session: new`),
other agents' messages and your answers arrive, made the first time one comes. It's marked "main" in the
sessions list. If the harness can't resume it after a restart (DeepSeek Harness without its history, say), Valet
makes a new one and sends the prompt that failed there, with whatever was waiting; a prompt you typed into the old
one isn't resent (its turn shows the error, and Retry).

**Where a prompt came from.** A prompt that you didn't type starts with one line, `Valet: ...`, saying what sent
it ("a scheduled prompt: every day at 08:00", "webhook `deploy` was called at ...", "a message from agent
`alpha`", "The owner answered the question you asked at ...", "the wake-up you scheduled at ..."), and the session
shows it over the message ("Schedule · Every day at 08:00", "From alpha"). The agent's page lists its triggers'
recent runs, delivered or not, and why not.

**One turn at a time, nothing lost.** A prompt for a session that's working waits in that session's queue and
goes when the turn ends, in order: yours too (the composer stays open while it works), and you can remove a
waiting one. A queue waits while the agent is stopped and outlives a control-plane restart. Placing a prompt, the
turn it starts and its first event are one database transaction, so a prompt is never lost or sent twice. A turn
cut off by a runner or control-plane restart is marked interrupted, and **Retry** under it (or under a failed
turn) sends its prompt again; nothing is resent on its own.

**Agents talking to each other**: with `valet.agents` in `tools.mcp`, `send_message(agent, text)` puts a prompt
in the other agent's main session (at most 20 messages in 10 minutes per sender). It returns at once; a reply
comes back the same way. The other agent must be running.

**Reaching you**: with `valet.owner` in `tools.mcp` an agent can

- `message(text)`: a note in your inbox, with Dismiss;
- `ask(question, options?)`: a question in your inbox, answered with one of its options, your own words, or both
  (or dismissed). The call returns at once; your answer comes back later as a prompt in the session that asked
  (else its main session), and waits for the agent if it's stopped;
- `schedule_wakeup(prompt, at? | in_minutes?)`, `wakeups()`, `cancel_wakeup(id)`: a prompt it gets later, in the
  session that set it (up to a year ahead, 50 pending).

These calls are allowed at every permission level: `valet.owner` decides them itself. Messages and questions stay
in the inbox until you deal with them, across the agent's stop and control-plane restarts, and don't mark the
agent as waiting on you.

**What it's doing**: the agents list and its page show what a working agent is doing ("Thinking…", the tool it's
running), how many prompts wait in its sessions, and its next schedule or wake-up.

## Tests

```sh
uv run pytest                                  # offline: 127.0.0.1 only, no Docker, no API key
VALET_TEST_DOCKER=1 uv run pytest -m docker    # the real image; a harness's key sends real prompts to it
VALET_TEST_CHROME=1 uv run pytest -m chrome    # a real headless Chrome (Playwright's, or VALET_CHROME)
```

The offline suite drives the real control plane (uvicorn on a free port), the real runner and
`tests/fake_agent.py`. The fake agent is an ACP agent whose behaviour is chosen by the prompt text: `permission`,
`slow`, `fail`, `crash`, `meta`, `tool`, `env NAME...` (those variables of its environment), `mcp-servers`,
`mcp-list`, `mcp <tool> <json>` (calls the gateway like a harness would), `blocks` (the prompt's blocks, as JSON:
what attachments became), anything else is echoed. It takes images unless started with `--no-image`. Loading a
session, it replays that session's Claude Code transcript if `$HOME/.claude/projects` has one (the tests write fake
ones under a temporary HOME with `tests/claude_transcripts.py`). It stands in for Claude through
`VALET_HARNESS_CMD_CLAUDE` / `RunnerConfig.harness_commands`. You can use it by hand too:

```sh
VALET_HARNESS_CMD_CLAUDE="$PWD/.venv/bin/python $PWD/tests/fake_agent.py" uv run valet-runner
```

with an agent at `computer.level: host`. `tests/fake_mcp.py` is the upstream MCP server behind the gateway, and
`tests/browser_fake.py` a scripted browser in place of Chrome; the `chrome` tests drive the real one against pages
they serve themselves (a login form with a 2FA step, a third-party iframe, buttons that commit).

Layout: `tests/` (shared units and the fake agent), `tests/e2e/` (the whole stack), `tests/control/` and
`tests/runner/` (each side on its own).

## Not done yet

- **Harness keys** come from the runner's environment only, one set per harness. Without its key an agent starts
  and initializes, but a prompt ends with the harness's authentication error. Secrets (above) are for everything
  else an agent needs.
- **DeepSeek Harness is a developer preview** (its own safety notice: no security audit yet, extension contracts
  may change). It's pinned, and runs in a container by default. Its model and reasoning effort can be changed per
  session over ACP (`session/set_config_option`), but the UI doesn't offer that yet: the definition's `model` applies.
- **The browser** runs on the control plane's host, one Chrome per profile; a control-plane restart stops it (the
  profile keeps its logins, the tabs close). Agents can't upload or download files, and the live view shows one
  tab per socket, without sound. "Always allow" and hour grants are for the exact host (`www.github.com`), not its
  domain: write a `*.github.com` rule for that.
- **Memory and the shared space**: a memory file's history shows its own commits; a file renamed by hand in the repo
  starts a new history. Each agent's Shared is between it and you only; an agent without `valet.shared` in its
  tools can't read what you leave there, and its Shared doesn't say so.
- **Directives**: a preset you edited can't be reset to Valet's text from the
  app (the shipped ones are in `valet/control/authority/`). The new-agent form doesn't preview what the agent will
  be told; its settings page does, once it exists. The environment's tool list is the one at the agent's start.
- **The MCP gateway is a first slice.** All of Valet's own servers exist: `valet.agents`, `valet.browser`,
  `valet.memory`, `valet.owner` and `valet.shared`. Stdio servers run on the control plane's host as its
  user, unsandboxed: register only ones you trust. Without a rule, the decision trusts the server's own
  annotations. Rules apply at the next call, but the environment layer's list of tools is the one at the agent's
  start. "Always allow" rewrites `agent.yaml` as a settings save does (comments in it go). Claude Code lists the gateway's tools when a session starts,
  so tools added later show up in its next session; DeepSeek Harness lists them when it starts, so they show up
  after a restart.
- **Network policies** are tested on Docker Desktop (macOS, Docker 28.3) only, not yet on Linux. There the
  control plane must listen where the Docker bridge reaches it, `host-gateway` needs Docker 20.10, and the agent's
  network must come up without an address for the Docker host (`com.docker.network.bridge.inhibit_ipv4`; check
  that `docker network inspect valet-agent-<name>-net` shows no Gateway): an engine that ignores the option leaves
  the Docker host's own services reachable from the agent. The proxy decides by name: an allowed site whose name resolves to a private address (your LAN, the
  Docker host) is reached there too, so list only sites you trust. Mounts are passed to Docker as given; nothing
  restricts them yet. Network and shared folders apply to its own computer only; on this machine they're ignored.
- **Always-on**: a webhook call to a stopped or down agent is refused (503), not kept for later. A schedule's
  identity is its expression: editing one starts it afresh, and its earlier runs stay listed under the old one.
  A wake-up that comes due while its agent is stopped is skipped, like a schedule's run. The `Valet:` line is
  plain text: a harness can't tell it apart from text in the prompt, so the agent's instructions are what keep a
  webhook's data from being taken as orders.
- **A harness's own "always"**: for a harness's permission request the UI offers Allow once and Reject; its
  "always" choices (Claude Code's, for its session only) show only when it offers nothing else.
- **Notifications** aren't withdrawn when an item is answered elsewhere (the push stays on the other devices).
  Push hasn't been tried against Apple's real service yet: it's tested against a stand-in that checks the same
  HTTP/2 requests and token.
- **Moving an agent between runners** isn't handled. If its runner is offline, starting it elsewhere doesn't stop
  the old copy. Permission requests raised while the runner is disconnected from the control plane never reach
  the inbox; they stay open until the turn is cancelled or the agent stops.
- **The agent's computer**: a terminal's shell on this machine runs as the runner's user, unsandboxed, like the
  agent itself. Files can be viewed, downloaded, uploaded and given new folders, not renamed, moved or deleted (the
  terminal does that); a folder shows its first 2000 entries. A terminal ends with its page: there's no coming back
  to a shell after a reload. Attachments are placed where the agent can read them, but nothing removes them from
  its workspace afterwards.

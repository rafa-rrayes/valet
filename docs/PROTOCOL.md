# Runner <-> control plane protocol (v1)

The runner dials **out** to the control plane. One WebSocket per runner:

```
GET {VALET_CONTROL_URL}            e.g. ws://127.0.0.1:8780/ws/runner
Authorization: Bearer vrun_<secret>
```

The token comes from `valet-control add-runner <name>`; the control plane stores only its sha256 and knows the
runner's name from it. Source of truth for every message: `valet/shared/protocol.py` (pydantic models; build them
there and send `.dump()`, parse with `parse_runner_message` / `parse_control_message`).

## Framing

- Every frame is one JSON text frame: an object with a `type`.
- Commands (control -> runner) carry `id` (uuid4 hex). The runner answers **every** command with exactly one
  `reply` of the same `id`: `{"type":"reply","id":..,"ok":true,"result":{..}}` or `{"ok":false,"error":"..."}`.
- Everything else is a one-way notification; no acks. The agent's terminals add one-way frames in both directions
  that carry no `id` and no `seq` (see "The agent's computer").
- ACP payloads embedded in messages (`update`, `tool_call`, `options`, `outcome`, `prompt`, `capabilities`) are ACP
  JSON exactly as on the ACP wire: camelCase, by alias, nulls dropped
  (`model.model_dump(by_alias=True, exclude_none=True, mode="json")` for acp.schema models).
- Keepalive: the websockets library's ping/pong (default on both sides). No application pings.

## Handshake

1. Runner connects with its bearer token. Bad/revoked token: the control plane closes with **4401**.
2. Runner sends `hello` first, a snapshot taken right after its notification `seq`:
   `{"type":"hello","protocol":1,"version":"0.2.0","boot":"<uuid of this runner process>","seq":N,"agents":[AgentState..],"turns":[ActiveTurn..],"pending_permissions":[PermissionRequest..],"harnesses":["claude",..],"features":["terminal","files","volumes","forget"]}`
   Anything else first, or `protocol != 1`: close **4400**.
   `harnesses` names the harnesses this runner can run: each adapter whose key is in the runner's environment
   (`claude`: `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`; `deepseek`: `DEEPSEEK_API_KEY`), and any harness
   it has a `VALET_HARNESS_CMD_<HARNESS>` command for. Names only, never key values. It says nothing about Docker
   or the agent image. Control keeps it while the runner is connected and shows it in the API (`Runner.harnesses`,
   and `ready` in `GET /api/catalog`).
   `features` names the optional parts of the protocol this runner has (`protocol.FEATURES`): `terminal`,
   `files`, `volumes` (see "The agent's computer"), `forget` (`agent.forget`). Control sends a feature's commands
   only to a runner that lists it; an older runner sends no `features` (or no field at all), and the API answers
   501 ("update it") for what it can't do. Both fields are optional, so either side may be the older one.
3. Control replies `{"type":"welcome","runner":"<name>","version":"0.2.0","last_seq":M}`, M being the last
   notification it applied from that `boot` (0 for a boot it hasn't seen). The runner then sends every
   notification it kept with `seq > M`, in order, and from then on its live traffic.
4. Once control has applied up to `seq` N (at once if M >= N), it reconciles with the hello:
   - each listed agent's state is recorded; agents placed on this runner but not listed become `stopped`;
   - agents placed on this runner with `desired = running` that aren't listed as `starting`/`running` get
     `agent.start`; an unplaced one (definition names no other runner) is claimed atomically, then started;
   - turns of this runner's agents in state `running` that aren't in `turns` become `interrupted`;
   - listed requests without an inbox item get one; pending inbox items held by this runner whose request isn't
     listed become `expired`.
5. If the same runner connects again, the older socket is closed with **4409**.

4401 and 4400 are fatal for the runner (stop, don't retry). Any other close: reconnect with backoff 1s doubling to
`VALET_RECONNECT_MAX` (30s). Agents keep running while disconnected.

## Notification numbering

Every runner -> control notification carries `seq`, numbered from 1 per runner process (`boot`). The runner keeps
the last 10,000 (at most 64 MB) and resends those after `welcome.last_seq` on every connection, so output produced
while offline, or lost in flight with a dying socket, still reaches the event store. Control stores the last seq
applied per runner (table `runner_seq`, so it survives a control restart), drops anything at or below it, and
logs a gap if the backlog overflowed. Replies are not numbered: a reply lost with its socket is lost (see
`permission.closed` reason `answered` for the one case where that matters).

A runner speaks only for agents placed on it: control drops notifications about any other agent (and `turn.end` /
`permission.closed` for turns or requests that aren't that agent's).

## Shared shapes

```
AgentState  = {"agent": str, "state": "starting"|"running"|"stopped"|"failed", "error": str|null,
               "capabilities": <ACP agentCapabilities>|null, "agent_info": <ACP agentInfo>|null}
ActiveTurn  = {"agent": str, "session_id": str, "turn_id": str}
PermissionRequest = the permission.request notification below (in a hello: the whole request, so control can
                    create an inbox item it never heard of)
```

## Control -> runner commands

| type | fields | reply result | notes |
|---|---|---|---|
| `agent.start` | `agent`, `definition` (AgentDefinition JSON), `files` (the text of each directive layer it lists, by file: `owner.md`, `authority/<preset>.md`, `persona.md`, `environment.md`, `memory.md`; and `skills/<name>/<path>`: text for each of its skills, see Skills), `secrets` ({"NAME": value}, default {}), `gateway` (`{"url": str, "token": "vagt_...", "timeout": float\|null}`\|null) | `{}` at once | Starts, or restarts if already running. Progress via `agent.status`: `starting` -> `running` / `failed`. The adapter joins the layers in `definition.directives`' order (`shared.directives.assemble`: non-empty texts, stripped, a blank line between) and gives the result to the harness as is: Claude's `_meta.systemPrompt.append`, dsh's `AGENTS.md`. `persona.md` is always sent; the others only when listed (`environment.md` and `memory.md` are written by the control plane at each start; `memory.md` is empty while the definition's `memory` is off). `secrets` are the values of the definition's named secrets: env vars of the harness process (see Secrets). `gateway`: see "The MCP gateway" below. |
| `agent.stop` | `agent` | `{}` once stopped | Container: stopped, volumes kept. Pending permissions close with reason `exited`. |
| `agent.forget` | `agent`, `volumes` (bool, default false) | `{"volumes": [names deleted], "kept": str\|null}` once done | Feature `forget`: the agent was deleted. It stops as `agent.stop` stops it (its terminals close; `agent.status` stopped, also when it never ran here), and this runner forgets it (it's not in the next hello): with the docker CLI, `docker rm -f` of its containers (its own, its network proxy's) and `docker network rm` of its network, found by their label `valet.agent=<agent>` (whatever its level is now; not by name: agent `foo`'s proxy, `valet-agent-foo-egress`, has agent `foo-egress`'s container's name); its volumes (`volumes.remove`'s) only with `volumes`; then what the runner made in its data dir `<VALET_RUNNER_DATA>/agents/<agent>`: `config/`, `egress/`, `work/` (a host agent's own working folder) if empty, and the dir if nothing's left. `kept`: `work/` if only that stays (it has files), the dir if anything else does, else null. A host agent's `computer.workdir` is never touched, even when it's in there. Errors: `Docker didn't delete them: ...`, `can't remove its files on the runner: ...`. |
| `session.new` | `agent` | `{"session_id": str, "modes": <ACP SessionModeState>\|null}` | ACP `session/new` (with the gateway in `mcpServers`, below), then `session/set_mode` to the mode the agent's permission level compiles to, if the harness has modes (Claude) and the session isn't in it already; `modes` as they are after that. A harness that doesn't offer that mode keeps its own (the runner logs it), except at `locked` and `ask` (the mode is the level's floor: the session doesn't run). Errors: `not running` if the agent isn't; `session/new failed: ...`, including a failed `session/set_mode`. |
| `session.prompt` | `agent`, `session_id`, `turn_id`, `prompt` (ACP ContentBlocks) | `{}` at once if accepted | Errors: `busy` (a turn runs in that session), `not running`, `cannot resume session` (unknown to this process and the harness has neither loadSession nor resume). Accepted, the turn is active (in the hello) at once; it loads the session first if this process doesn't know it (replays sent with `replay: true`; the gateway in `mcpServers` again), and sets its mode as `session.new` does; a failed load or mode change ends the turn with that error. Then `session.update`* and exactly one `turn.end`. |
| `session.cancel` | `agent`, `session_id` | `{}` at once | A turn still loading its session ends `cancelled` without prompting. Otherwise ACP `session/cancel`; pending permissions of that session are answered `{"outcome":"cancelled"}` and reported with `permission.closed` reason `cancelled`. The turn still ends with `turn.end` (`stop_reason: "cancelled"`). |
| `permission.answer` | `agent`, `request_id`, `outcome` (`{"outcome":"selected","optionId":..}` or `{"outcome":"cancelled"}`) | `{}` once handed to the harness | Error `unknown request` if no longer pending. Also reported as `permission.closed` reason `answered`. |
| `session.import` | `agent`, `session_id` | `{"session_id": str, "modes": <ACP SessionModeState>\|null, "title": str\|null}` once loaded | Continues a Claude Code session this process doesn't have (one that ran outside Valet) in a running Claude Code agent at computer level `host`: ACP `session/load` of a session in the agent's working folder (one `claude.sessions` with `agent` lists), with the gateway in `mcpServers`, then the mode as after `session.new`. The history goes up while it loads, before the reply: `session.update` with `replay` and `history` true under a made-up `turn_id` per exchange (a new one at each user message that follows something else), each exchange closed by a `turn.end` (`stop_reason: "end_turn"`). `title` is the session's title as `claude.sessions` lists it. Errors: `not running`; `already open` (this process has it); `only a Claude Code agent on this machine (computer level host) can continue a Claude Code session`; `this harness can't load sessions`; `no Claude Code session ... in ...`; `session/load failed: ...`. |
| `claude.sessions` | `agent`\|null, `folder`\|null | with `agent`: `{"cwd": str, "sessions": [ClaudeSession]}`; with `folder`: `{"folder": str, "sessions": [ClaudeSession]}`; with neither: `{"folders": [{"folder", "cwd": str\|null, "sessions": int, "updated_at": float}]}` | The Claude Code sessions on the runner's machine, read from `~/.claude/projects` of the runner's user (see "Claude Code transcripts" below). `agent` (a running Claude Code agent at level `host`): the ones Claude Code ran in its working folder, the ones `session.import` can continue. `folder`: one project folder's. Neither: the folders that have sessions (at most 100). ClaudeSession = `{"id", "title", "cwd": str\|null, "updated_at": float, "size": int}`; newest first, at most 200, subagents' transcripts left out. Errors: `not running`, not a host Claude Code agent (as for `session.import`), `no Claude Code project folder ...`. |
| `claude.transcript` | `folder`, `session_id`, `budget` (bytes, default 60000, at most 1000000) | `{"title": str, "cwd": str\|null, "entries": [{"role": "user"\|"assistant"\|"tool"\|"summary", "text": str}], "omitted": int}` | One Claude Code session's conversation, condensed for a hand-off (no model reads it): its current branch's user and assistant text, each tool call as one line with its key arguments, a compaction's summary; the newest entries whose rendered text fits in `budget`, `omitted` the count of older ones left out. Errors: no such folder or session. |

Timeouts: the control plane waits `VALET_RPC_TIMEOUT` (60s) for a reply (`session.import`: 300 s, since the
load starts Claude Code and replays the whole session first), then treats the command as failed. A
command whose socket drops before the reply is in doubt: for `session.prompt` the turn stays running until the
next hello settles it; for `permission.answer` the inbox item stays pending.

### Secrets

`agent.start.secrets` is the only place a secret's value crosses the link (since M2; a runner older than that
ignores the field and starts the agent without them). The control plane fills it from its encrypted store with
every name the definition lists, and the password of every vault entry it lists under that entry's env name
(`VaultSecret.env_name`: `env`, else e.g. `GITHUB_ALICE_PASSWORD`), and doesn't send the command at all if one
isn't set. The runner adds them to
the harness process's environment and nowhere else:

- `host`: the spawned process's env (`acp.default_environment()` + the harness's env + the secrets);
- `container`: the docker CLI's env, with `-e NAME` on its argv, so the value reaches the container without being
  on a command line;
- never on disk (the compiled config dir included), in a log line, a notification, the hello or an error.

A name that isn't an env var name or is reserved (API.md, Secrets), or one the harness sets itself, fails the
start: `agent.status` `failed` with an error naming it, e.g. `secret OPENAI_API_KEY: the harness sets
OPENAI_API_KEY itself`. Restarting is the only way to change them: there's no command to update a running agent.

### Skills

The agent's skills (API.md, Skills: its own, and the shared ones its definition picks) travel in `agent.start.files`,
one entry per file, keyed `skills/<name>/<path>` (`skills/code-review/SKILL.md`, `skills/code-review/checklist.md`).
The control plane has already checked them; the runner takes only well-formed keys (a skill name, a relative path
without `.` or `..` parts) and ignores the rest. Each adapter writes them
into the agent's config dir, read-only to the agent (`/valet` in a container), and points its harness there:

- Claude Code: a local plugin, `<config>/claude-plugin/` (`.claude-plugin/plugin.json` naming it `valet`, and
  `skills/<name>/...`), in `_meta.claudeCode.options.plugins` (`{"type": "local", "path": ..., "skipMcpDiscovery":
  true}`), with `_meta.claudeCode.options.skills` set to exactly `valet:<name>` for each (`[]` with none), so no
  other skill (Claude Code's own, or one the agent wrote under `~/.claude`) is offered. The `Skill` tool is
  pre-approved by a managed allow rule, and added to `locked`'s tool list, when it has skills.
- DeepSeek Harness: `<config>/skills/<name>/...`, the only root of its `skill-filesystem` provider
  (`includeDefaultRoots: false`, `customSkillDirs: [<config>/skills]`, `watch: false` in `dsh.patch.yml`). The
  `skill` tool passes Valet's gate at every level.

The files are part of the restart fingerprint (like the rest of `files`), so a changed skill shows as
`restart_needed`; a running agent's skills change only when it restarts.

### The MCP gateway

`agent.start` carries `gateway` when the agent has gateway servers (`AgentDefinition.gateway_servers()`: those
`tools.mcp` lists, and `valet.memory` with `memory: true`): the URL of its own
endpoint on the control plane's MCP gateway (`VALET_GATEWAY_URL` + `/mcp/agents/<name>`, as the agent reaches it)
and a bearer token for it (`vagt_...`, new at every start; the old one stops working, unless the runner refused
the start or wasn't connected; revoked at stop). The token grants nothing but that endpoint, for that agent, while
it runs. The runner keeps it in memory only, like `secrets`: out of logs and reprs. `timeout` is the longest the
gateway may hold one call, in seconds: the wait for the owner, then the upstream call (`VALET_GATEWAY_ASK_TIMEOUT`
+ `VALET_GATEWAY_CALL_TIMEOUT`, 900 by default; a runner that gets none assumes 900). The harness's MCP client is
told to wait that long plus a minute (`harness.gateway_wait_ms`), so the gateway's answer, or its refusal when the
ask expires, gets back.

DeepSeek Harness gets the gateway in its own config rather than over ACP: its ACP server builds an MCP client per
session with a fixed 60 s limit per call, which an ask can outlast. The adapter adds a `@deepseek-ai/dsh-mcp-client`
row to `dsh.patch.yml` (`serverName: valet`, so the tools are still `mcp__valet__<tool>`; `transport:
streamable-http`; the URL; `toolCallTimeoutMs`), whose `Authorization` header is the expression `!!js "'Bearer ' +
process.env.VALET_GATEWAY_TOKEN"`: the token is an env var of the harness process, never in the config dir, and
`dsh --dump-config` prints the expression. dsh leaves env names like `*TOKEN` out of the commands it runs. Its
sessions get `mcpServers: []`. dsh connects when it starts (retrying, by its defaults, 10 times over about two
and a half minutes, without stopping the agent) and lists the gateway's tools then, not per session.

Claude Code: if the harness advertised `agentCapabilities.mcpCapabilities.http` at initialize, every ACP
`session/new`, `session/load` and `session/resume` gets it as one MCP server:

```
"mcpServers": [{"type": "http", "name": "valet", "url": "<gateway.url>",
                "headers": [{"name": "Authorization", "value": "Bearer <gateway.token>"}]}]
```

Its env sets `MCP_TOOL_TIMEOUT` and `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` to the same wait (Claude Code cuts an HTTP
MCP call that's silent for 5 minutes otherwise). Without that capability (or with no `gateway`) `mcpServers` is
`[]` and the runner logs that the harness gets no gateway tools.
A container agent on the open internet also gets `--add-host host.docker.internal:host-gateway`, so a gateway URL
of `http://host.docker.internal:<port>` reaches the control plane's host on Docker Desktop and on Linux. Under a
restricted network (`gateway`, `allowlist`, and `none`, which means `gateway`) the agent's own proxy gets that
host entry instead and lets the gateway URL's host and port through (README, Its computer and the internet).
Nothing in `agent.start` changes: the runner reads the policy from the definition.

The gateway decides each call itself (allow / ask / deny from the agent's permission level), so the harness
configs let its tools through without asking a second time, at every level and on the host too: Claude Code gets
the managed allow rule `mcp__valet` (in `managedSettings.permissions.allow`: under
`allowManagedPermissionRulesOnly` an `allowedTools` entry wouldn't count), DeepSeek Harness's approval gate lets
`mcp__valet__*` through. Asks go to the owner's inbox from the control plane directly (kind `tool`, API.md); they
never pass through the runner.

### Claude Code transcripts

`claude.sessions`, `claude.transcript` and `session.import` read Claude Code's transcripts on the runner's
machine: `~/.claude/projects/<folder>/<session id>.jsonl` of the user the runner runs as, the same user and home
its host agents have (`runner/claude_sessions.py`, `shared/transcripts.py`). Read-only, and only there: a folder
must be a name as Claude Code makes them (ASCII letters, digits and `-`, at most 255), a session id a UUID, so no
command reaches a path outside that directory. Containers' Claude Code keeps its transcripts in the container, so
only host agents can continue one.

- The folder of a working folder: the path with every character but an ASCII letter or digit as `-`; past 200
  characters it is cut there and gets `-` and a hash of the whole path (Claude Code's own rule). A session of a
  host agent is looked up under its working folder's name and its real path's (symlinks resolved).
- A session's title, as Claude Code's own list shows it: the title the user gave it, else the one Claude Code
  generated, else its last prompt (or an older `summary` line), else its first one (read from the first and last
  64 KiB).
- A conversation (`claude.transcript`): the current branch, from the last message back through `parentUuid`
  (across a compaction through `logicalParentUuid`), subagents' and meta records left out; user and assistant text,
  a tool call as `name: key argument` (the command, the file, the pattern, the URL...), a compaction's summary as
  its own entry; text Claude Code wrapped in its own tags (reminders, a command's output) dropped, a slash
  command kept as `/name args`.

### The agent's computer

The owner's way into an agent's computer (`runner/terminal.py`, `runner/files.py`, `runner/computer.cjs`; the
control plane's side is `control/computer.py`). Each part is a feature the runner lists in its hello. All of them
need the agent running here with a computer of its own (level `container` or `host`): errors `not running`, and
`it has no computer (level none): no terminal and no files`; volumes don't (they outlast the agent).

| type | fields | reply result | notes |
|---|---|---|---|
| `terminal.open` | `agent`, `terminal` (uuid4 hex, the control plane's), `cols`, `rows` (2 to 1000; default 80x24) | `{}` once its shell runs | Feature `terminal`. A shell on a PTY the runner holds. Container: `docker exec -i -t -e TERM -e COLORTERM -e VALET_TERMINAL=<terminal> -w /work <its container> sh -c "<bash -l, else sh -l>"`: its user, its env, its folders. Host: the runner user's login shell in the agent's working folder, with a clean environment (`acp.default_environment()`, the locale, `TERM=xterm-256color`, `VALET_TERMINAL`; none of the runner's own variables, its token or keys). Then `terminal.output` frames, and one `terminal.closed`. At most 8 open per agent. Errors as above, `it has 8 terminals open already: close one first`, `can't start a shell: ...`. |
| `terminal.close` | `terminal` | `{}` (also for one that's gone) | Its process group gets SIGHUP, then SIGKILL after 2 s; in a container, every process whose environment carries its `VALET_TERMINAL` too (the shell inside isn't the docker CLI's child, and outlives it). `terminal.closed` follows (reason `closed`) if it was open. |
| `files.list` | `agent`, `path`\|null | `{"roots": [FileRoot], "path", "type": "dir"\|"file"\|"other", "size", "mtime", "entries": [FileEntry], "truncated"}` | Feature `files`. `path` null: the first root. A folder's entries, folders first, then by name; at most 2000 (`truncated`). FileRoot = `{"path", "label", "writable"}`: container `/work` (Workspace) and `/home/agent` (Home), then its shared folders (`Shared folder <host path>`, read-only when mounted so); host its working folder. FileEntry = `{"name", "type", "link": bool, "size", "mtime"}`: a link shows what it points to, or `other` when that's outside the roots or nowhere. Upload leftovers (`.valet-upload-*`) aren't listed. |
| `files.read` | `agent`, `path`, `offset` (default 0), `length` (at most 2 MiB, the default) | `{"size": int (the file's), "data": base64, "eof": bool}` | One chunk of a file. Error `not a file`. |
| `files.write` | `agent`, `upload` (hex id, the same for every chunk), `dir`, `name`, `offset`, `data` (base64, at most 2 MiB), `final`, `exists` (`fail`\|`replace`\|`rename`) | `{"size"}` so far; the final one `{"path", "size"}` | One chunk of an upload into folder `dir`. Offset 0 creates `<dir>/.valet-upload-<upload>` (O_EXCL, never through a link); each chunk appends at `offset`, which must be its size so far (`the upload is out of step`); the final one renames it to `name`: `exists` `fail` (error `exists`), `replace` (a file only: `<name> is a folder or a link: it can't be replaced`), `rename` (`name-2.ext`, `name-3.ext`...). A read-only root: `<root> is read-only`. An upload left without a chunk for 15 minutes has its partial file dropped at the agent's next files command. |
| `files.cancel` | `agent`, `upload`, `dir` | `{}` | Drops an upload's partial file. |
| `files.mkdir` | `agent`, `path` | `{"path"}` | Makes a folder and the missing ones above it inside a writable root, each inside its parent's resolved path; one that's there is fine. Error `<path> is not a folder` when something else is in the way. |
| `volumes.list` | `agent` | `{"volumes": [{"name", "mount"}]}` | Feature `volumes`. Those of `valet-agent-<agent>-home` (`/home/agent`) and `-work` (`/work`) that exist on this runner's Docker, whatever the agent's level or state; none on a runner without the docker CLI. |
| `volumes.remove` | `agent` | `{"removed": [names]}` | Deletes them for good. Errors `it's running: stop it first` (running or starting here), `its container still exists: stop it first`, `Docker didn't delete them: ...`. |

Paths are absolute, inside a root as written, with no `.` or `..` parts (`a path must be absolute`, `a path can't
have . or .. in it`, `<path> is outside its folders`), and are checked again where they live once their links are
resolved (`<path> leads outside its folders`): on the runner's machine for a host agent (Python), inside the
container for a container agent (`computer.cjs`, run as `docker exec -i <container> node -e <script> <op> <json>`:
every agent image has node, both harnesses being Node programs). Missing: `no such file or folder`. Bytes travel
base64 in chunks of at most 2 MiB, one command each, so a reply is the flow control and no frame is large.

Terminal frames have no `id` and no `seq`, are never kept or resent, and are handled in the order they come:

| direction | type | fields | |
|---|---|---|---|
| control -> runner | `terminal.input` | `terminal`, `data` (base64) | keystrokes and pastes, written to the PTY in order (beyond 1 MiB waiting for a shell that doesn't read, input is dropped) |
| control -> runner | `terminal.resize` | `terminal`, `cols`, `rows` | the PTY's new size (the shell gets SIGWINCH; the docker CLI passes it on) |
| control -> runner | `terminal.ack` | `terminal`, `bytes` | the control plane has handed on this many more bytes of output |
| runner -> control | `terminal.output` | `terminal`, `data` (base64) | what the shell wrote. The runner stops reading the PTY with 512 KiB (`TERMINAL_WINDOW`) unacknowledged and goes on below half of it, so a `cat` of a big file waits on the owner instead of flooding the link the agents' events share. Never logged or stored |
| runner -> control | `terminal.closed` | `terminal`, `code`\|null, `reason`\|null | once, after its last output: the shell exited (`code`), or it was closed (`reason`: `closed`, `the agent stopped`, `the agent restarted`, `the connection to the control plane dropped`) |

Frames for a terminal that isn't open are dropped. Terminals live and die with the connection: when it drops the
runner closes them and the control plane ends their owners' sockets (`the runner disconnected`); stopping or
restarting the agent closes its terminals.

## Runner -> control notifications

| type | fields | when |
|---|---|---|
| `agent.status` | AgentState fields | every state change of an agent's harness process |
| `session.update` | `agent`, `session_id`, `turn_id`\|null, `replay`, `history`, `update` (ACP SessionNotification.update) | each ACP `session/update`; `turn_id` is the running turn in that session, if any. `replay`: a load or resume replaying history control already has (it drops these). `history` (with `replay`): the history of a session being imported (`session.import`), which control keeps, user messages too, each exchange under its made-up `turn_id` |
| `permission.request` | `agent`, `session_id`, `turn_id`\|null, `request_id` (uuid4 hex), `tool_call` (ACP ToolCallUpdate), `options` (ACP PermissionOption list) | harness called `session/request_permission`; the ACP request stays open until answered |
| `permission.closed` | `agent`, `session_id`, `request_id`, `reason` (`answered`\|`cancelled`\|`exited`\|text), `outcome` (reason `answered`: the ACP outcome delivered) | a pending request ended: answered (recorded even if the reply to `permission.answer` was lost), or without the owner's answer |
| `turn.end` | `agent`, `session_id`, `turn_id`, `stop_reason` (ACP StopReason)\|null, `error`\|null | `session/prompt` returned or failed (incl. process exit: `error: "agent exited"`); and once per exchange of an imported history (`stop_reason: "end_turn"`) |

## Ordering

Within one connection the runner sends frames in the order the harness produced them. ACP's Python library runs
each incoming notification/request handler as its own task, so the runner's `session_update` and
`request_permission` handlers must enqueue their outgoing message **synchronously** (`put_nowait` before any
`await`) onto one queue drained by a single sender task. `turn.end` goes through the same queue after
`session/prompt` returns (the library already waits for in-flight updates of that session before returning).

## Example

Notifications in this example leave out `seq`.

```
runner  {"type":"hello","protocol":1,"version":"0.2.0","boot":"b1","seq":0,"agents":[],"turns":[],"pending_permissions":[],"harnesses":["claude","deepseek"],"features":["terminal","files","volumes","forget"]}
control {"type":"welcome","runner":"myhost","version":"0.2.0","last_seq":0}
control {"type":"agent.start","id":"a1","agent":"scout","definition":{...},"files":{"persona.md":"..."}}
runner  {"type":"reply","id":"a1","ok":true,"result":{}}
runner  {"type":"agent.status","agent":"scout","state":"starting",...}
runner  {"type":"agent.status","agent":"scout","state":"running","capabilities":{"loadSession":true,...}}
control {"type":"session.new","id":"a2","agent":"scout"}
runner  {"type":"reply","id":"a2","ok":true,"result":{"session_id":"S1","modes":null}}
control {"type":"session.prompt","id":"a3","agent":"scout","session_id":"S1","turn_id":"T1","prompt":[{"type":"text","text":"ls"}]}
runner  {"type":"reply","id":"a3","ok":true,"result":{}}
runner  {"type":"session.update","agent":"scout","session_id":"S1","turn_id":"T1","replay":false,"update":{"sessionUpdate":"tool_call","toolCallId":"c1","title":"ls","kind":"execute","status":"pending"}}
runner  {"type":"permission.request","agent":"scout","session_id":"S1","turn_id":"T1","request_id":"R1","tool_call":{"toolCallId":"c1","title":"ls"},"options":[{"optionId":"allow","name":"Allow","kind":"allow_once"},{"optionId":"reject","name":"Reject","kind":"reject_once"}]}
control {"type":"permission.answer","id":"a4","agent":"scout","request_id":"R1","outcome":{"outcome":"selected","optionId":"allow"}}
runner  {"type":"reply","id":"a4","ok":true,"result":{}}
runner  {"type":"session.update",...,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Done."}}}
runner  {"type":"turn.end","agent":"scout","session_id":"S1","turn_id":"T1","stop_reason":"end_turn","error":null}
```

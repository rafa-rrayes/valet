"""Runner <-> control plane WebSocket messages (docs/PROTOCOL.md is the prose version; this is the source of truth).

Every frame is one JSON object with a `type`. Commands from the control plane carry an `id`; the runner answers
each with exactly one Reply of the same id. Everything else is a one-way notification. ACP payloads inside these
messages (updates, tool calls, options, outcomes, content blocks) are ACP JSON as the harness sent it: camelCase
keys, by alias, nulls dropped."""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 1

# WebSocket close codes the control plane uses on /ws/runner.
CLOSE_UNAUTHORIZED = 4401   # missing, unknown or revoked runner token
CLOSE_BAD_PROTOCOL = 4400   # first frame wasn't a hello, or hello.protocol != PROTOCOL_VERSION
CLOSE_REPLACED = 4409       # the same runner connected again; the older connection is closed

AgentStateName = Literal["starting", "running", "stopped", "failed"]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------- runner -> control ----------

class AgentState(BaseModel):
    agent: str
    state: AgentStateName
    error: str | None = None
    capabilities: dict[str, Any] | None = None   # ACP InitializeResponse.agentCapabilities, once running
    agent_info: dict[str, Any] | None = None     # ACP InitializeResponse.agentInfo, if the harness sent one


class ActiveTurn(BaseModel):
    agent: str
    session_id: str
    turn_id: str


class _Note(_Msg):
    """A runner->control notification. `seq` numbers them per runner process (boot), from 1, so the runner can
    resend what the control plane missed while the link was down and the control plane can drop duplicates."""
    seq: int | None = None

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"seq"} if self.seq is None else None)


class PermissionRequest(_Note):
    """The harness called session/request_permission. The runner holds the ACP request open until a
    permission.answer with this request_id arrives, or the turn is cancelled, or the process exits."""
    type: Literal["permission.request"] = "permission.request"
    agent: str
    session_id: str
    turn_id: str | None = None
    request_id: str                             # runner-generated uuid4 hex; also the inbox item id
    tool_call: dict[str, Any]                   # ACP ToolCallUpdate
    options: list[dict[str, Any]]               # ACP PermissionOption: {optionId, name, kind}


class Hello(_Msg):
    """First frame after connecting. A snapshot taken right after notification `seq` of runner process `boot`:
    the control plane replies with the last seq it applied from that boot (Welcome.last_seq), the runner resends
    the rest, and once the control plane has applied up to `seq` it reconciles with this snapshot: agents it
    thinks are running but aren't listed get started again (if desired), turns it thinks are running but aren't
    listed become interrupted, listed requests without an inbox item get one, and pending inbox items whose
    request isn't listed expire."""
    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    version: str                                # valet package version of the runner
    boot: str = ""                              # uuid4 hex, new each time the runner process starts
    seq: int = 0                                # the last notification seq sent (or queued) before this snapshot
    agents: list[AgentState] = []
    turns: list[ActiveTurn] = []
    pending_permissions: list[PermissionRequest] = []   # requests still waiting on the owner
    harnesses: list[str] = []                   # harnesses it can run: it has their key, or a command for them
    # What it can do beyond the commands every v1 runner has (FEATURES): the control plane sends a feature's commands
    # only to a runner that lists it. An older runner sends none.
    features: list[str] = []


# Optional parts of the protocol a runner says it has in its hello (Hello.features).
FEATURES = ("terminal", "files", "volumes", "forget")


class Reply(_Msg):
    type: Literal["reply"] = "reply"
    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentStatus(_Note, AgentState):
    """Sent on every state change of an agent's harness process."""
    type: Literal["agent.status"] = "agent.status"


class SessionUpdate(_Note):
    """One ACP session/update notification, unchanged. replay=True while session/load or session/resume replays
    history (the control plane already has it and drops these). history=True (with replay) for the history of a
    session being imported (session.import): the control plane doesn't have it yet and keeps it, user messages
    too, under the made-up turn_id of its exchange."""
    type: Literal["session.update"] = "session.update"
    agent: str
    session_id: str
    turn_id: str | None = None
    replay: bool = False
    history: bool = False
    update: dict[str, Any]                      # ACP SessionNotification.update


class PermissionClosed(_Note):
    """A pending permission request ended: answered (outcome is what the harness got; the control plane may
    never have seen the reply to its permission.answer), or without the owner's answer (turn cancelled,
    process exited)."""
    type: Literal["permission.closed"] = "permission.closed"
    agent: str
    session_id: str
    request_id: str
    reason: str                                 # "answered" | "cancelled" | "exited" | free text
    outcome: dict[str, Any] | None = None       # reason "answered": the ACP outcome delivered


class TurnEnd(_Note):
    """session/prompt returned (stop_reason) or failed (error). Exactly one per accepted session.prompt, and one
    (stop_reason end_turn) per exchange of an imported session's history (session.import)."""
    type: Literal["turn.end"] = "turn.end"
    agent: str
    session_id: str
    turn_id: str
    stop_reason: str | None = None              # ACP StopReason: end_turn | max_tokens | max_turn_requests | refusal | cancelled
    error: str | None = None


class TerminalOutput(_Msg):
    """Bytes a terminal's PTY produced (base64). One-way and unnumbered: never kept or resent, so a terminal's output
    is lost with the link (its terminal is closed then). The runner sends at most TERMINAL_WINDOW bytes of it ahead of
    the control plane's terminal.ack."""
    type: Literal["terminal.output"] = "terminal.output"
    terminal: str
    data: str                                   # base64


class TerminalClosed(_Msg):
    """A terminal ended: its shell exited (code), or it was closed (terminal.close, the agent stopped, the
    connection dropped). Sent once, after its last output. One-way and unnumbered, like terminal.output."""
    type: Literal["terminal.closed"] = "terminal.closed"
    terminal: str
    code: int | None = None                     # the shell's exit status, when it exited on its own
    reason: str | None = None                   # why, when it didn't ("closed", "the agent stopped", ...)


# ---------- control -> runner ----------

class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    runner: str                                 # this runner's name, from its token
    version: str
    last_seq: int = 0                           # last notification applied from hello.boot; the runner resends after it


class _Command(_Msg):
    id: str                                     # uuid4 hex; the Reply carries it back


class GatewayAccess(BaseModel):
    """The agent's MCP gateway endpoint and its bearer token (vagt_...). The runner hands both to the harness as an
    MCP server named "valet": an `http` server in every session/new, session/load and session/resume, or (DeepSeek
    Harness) in the harness's own config, where its MCP client can be told to wait `timeout` for a call."""
    url: str                                    # e.g. http://host.docker.internal:8780/mcp/agents/scout
    token: str = Field(repr=False)              # a credential, like agent.start's secrets: kept out of reprs
    # The longest the gateway may take to answer one tool call, in seconds: the wait for the owner
    # (VALET_GATEWAY_ASK_TIMEOUT), then the upstream call (VALET_GATEWAY_CALL_TIMEOUT). None from a control plane that
    # doesn't send it: the runner takes their defaults, 600 + 300.
    timeout: float | None = None


class AgentStart(_Command):
    """Start (or restart, if the definition changed) the agent's harness. Reply comes at once ({}); progress is
    reported with agent.status: starting -> running | failed."""
    type: Literal["agent.start"] = "agent.start"
    agent: str
    definition: dict[str, Any]                  # AgentDefinition.model_dump(mode="json")
    # Directive layers by file (shared.directives): persona.md, and owner.md, authority/<preset>.md and
    # environment.md when the definition lists them. The adapter joins them in the definition's order.
    files: dict[str, str] = {}
    # The values of the definition's named secrets, {"GITHUB_TOKEN": "..."}: env vars of the harness process. The
    # runner keeps them in memory and in that env only: never on disk, on a command line, in a log or a message.
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    gateway: GatewayAccess | None = None        # when it has gateway servers (definition.gateway_servers())


class AgentStop(_Command):
    """Stop the harness process (container: stop it, keep volumes). Reply after it's down ({})."""
    type: Literal["agent.stop"] = "agent.stop"
    agent: str


class AgentForget(_Command):
    """The agent was deleted (feature forget): stop it as agent.stop does, remove its containers (its own, its
    network proxy's) and network by their valet.agent label, and what the runner made in its data dir (<runner
    data>/agents/<agent>), except a host agent's own working folder (work/) when it has files, and anything else in
    there. Its volumes go only with `volumes`; a host agent's computer.workdir is never touched. Reply
    {"volumes": [names removed], "kept": the folder kept, or None}."""
    type: Literal["agent.forget"] = "agent.forget"
    agent: str
    volumes: bool = False


class SessionNew(_Command):
    """ACP session/new. Reply: {"session_id": str, "modes": <ACP SessionModeState> | None}."""
    type: Literal["session.new"] = "session.new"
    agent: str


class SessionPrompt(_Command):
    """Start a turn. Reply at once: {} if accepted, error "busy" if a turn runs in that session, "not running" if
    the agent isn't, "cannot resume session" if this harness process doesn't know session_id (it restarted) and
    can neither load nor resume sessions. Accepted, the turn is active (listed in the hello) at once; an unknown
    session is first loaded (session/load, or session/resume), replays sent with replay=True, and a load that
    fails ends the turn with an error. Then session.update ... and exactly one turn.end."""
    type: Literal["session.prompt"] = "session.prompt"
    agent: str
    session_id: str
    turn_id: str                                # control-generated uuid4 hex
    prompt: list[dict[str, Any]]                # ACP ContentBlocks, e.g. [{"type": "text", "text": "hi"}]


class SessionCancel(_Command):
    """ACP session/cancel. Reply at once ({}); the turn still ends with turn.end (stop_reason "cancelled")."""
    type: Literal["session.cancel"] = "session.cancel"
    agent: str
    session_id: str


class PermissionAnswer(_Command):
    """The owner's answer. Reply {} if delivered to the harness (a permission.closed reason "answered" follows),
    error "unknown request" if it's no longer pending (the control plane then marks the inbox item expired,
    unless that permission.closed already recorded it answered)."""
    type: Literal["permission.answer"] = "permission.answer"
    agent: str
    request_id: str
    outcome: dict[str, Any]                     # ACP: {"outcome": "selected", "optionId": "..."} | {"outcome": "cancelled"}


class SessionImport(_Command):
    """Continue a Claude Code session Valet doesn't have yet, in a running Claude Code agent at computer level
    host: ACP session/load of `session_id`, one of the sessions of the agent's working folder (claude.sessions with
    `agent`). Its history goes up first, as session.update with replay and history true and a made-up turn_id per
    exchange (a new one at each user message that follows something else), each exchange closed by a turn.end
    (stop_reason end_turn). Then the session is in the permission level's mode, as after session.new, and the
    reply is {"session_id": str, "modes": <ACP SessionModeState> | None, "title": str | None (its title in
    claude.sessions)}. Errors: "not running", "already open"
    (this process has it), that the agent isn't Claude Code on this machine or the harness can't load sessions,
    that there's no such session in its folder, "session/load failed: ..."."""
    type: Literal["session.import"] = "session.import"
    agent: str
    session_id: str


class ClaudeSessions(_Command):
    """The Claude Code sessions on the runner's machine (~/.claude/projects of the runner's user). With `agent` (a
    running Claude Code agent at level host): the sessions it can continue, the ones Claude Code ran in its working
    folder, {"cwd": str, "sessions": [Session]}. With `folder` (a project folder's name): {"folder": str,
    "sessions": [Session]}. With neither: {"folders": [{"folder", "cwd": str | None, "sessions": int,
    "updated_at": float}]}, newest first. Session: {"id", "title", "cwd": str | None, "updated_at", "size"},
    newest first, subagents' transcripts left out. Errors: "not running", that it isn't a host Claude Code agent,
    no such folder."""
    type: Literal["claude.sessions"] = "claude.sessions"
    agent: str | None = None
    folder: str | None = None


class ClaudeTranscript(_Command):
    """One Claude Code session's conversation, for a hand-off: shared/transcripts.conversation of its current
    branch, the newest entries whose text (shared/handoff.render) fits in `budget` bytes. Reply {"title": str,
    "cwd": str | None, "entries": [{"role", "text"}], "omitted": int}. Errors: no such folder or session."""
    type: Literal["claude.transcript"] = "claude.transcript"
    folder: str
    session_id: str
    budget: int = Field(default=60_000, gt=0, le=1_000_000)


# ---------- the agent's computer (features terminal, files, volumes) ----------

TERMINAL_WINDOW = 512 * 1024    # bytes of terminal output the runner sends ahead of terminal.ack
FILE_CHUNK = 2 * 1024 * 1024    # the most bytes one files.read returns or one files.write carries


class TerminalOpen(_Command):
    """Open a shell in the agent's computer (feature terminal): container, `docker exec -it` a login shell in its
    container; host, the runner user's login shell in its working folder. Reply {} once it runs; then
    terminal.output, and terminal.closed when it ends. Errors: "not running", that the agent has no computer
    (level none), too many terminals, the shell not starting."""
    type: Literal["terminal.open"] = "terminal.open"
    agent: str
    terminal: str                               # control-generated uuid4 hex; names it in every frame
    cols: int = Field(default=80, ge=2, le=1000)
    rows: int = Field(default=24, ge=2, le=1000)


class TerminalClose(_Command):
    """End a terminal: its shell and what it runs get SIGHUP (and SIGKILL if they linger). Reply {} (also for a
    terminal that's already gone); terminal.closed follows if it was open."""
    type: Literal["terminal.close"] = "terminal.close"
    terminal: str


class TerminalInput(_Msg):
    """Bytes for a terminal's PTY (base64), in order. One-way: no id, no reply."""
    type: Literal["terminal.input"] = "terminal.input"
    terminal: str
    data: str                                   # base64


class TerminalResize(_Msg):
    """The terminal's new size. One-way."""
    type: Literal["terminal.resize"] = "terminal.resize"
    terminal: str
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=2, le=1000)


class TerminalAck(_Msg):
    """The control plane has passed on this many more bytes of the terminal's output. One-way."""
    type: Literal["terminal.ack"] = "terminal.ack"
    terminal: str
    bytes: int = Field(ge=0)


class FilesList(_Command):
    """What's at `path` in the agent's computer (feature files). Reply {"roots": [FileRoot], "path": str, "type":
    "dir"|"file"|"other", "size": int, "mtime": float, "entries": [FileEntry] (a folder's, by name; at most 2000),
    "truncated": bool}. `path` null: the first root. Paths are absolute, inside a root, without `.` or `..` parts,
    and stay inside one once their links are resolved. FileRoot = {"path", "label", "writable"}; FileEntry =
    {"name", "type": "dir"|"file"|"other", "link": bool, "size": int, "mtime": float} (a link: what it points to;
    one pointing outside the roots or nowhere is "other"). Errors: "not running", that the agent has no files (level
    none), that the path isn't absolute / has `..` / is outside its folders, "no such file or folder"."""
    type: Literal["files.list"] = "files.list"
    agent: str
    path: str | None = None


class FilesRead(_Command):
    """Up to `length` bytes of a file from `offset`. Reply {"size": int (the file's), "data": str (base64), "eof":
    bool}. Errors as files.list, and "not a file"."""
    type: Literal["files.read"] = "files.read"
    agent: str
    path: str
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=FILE_CHUNK, gt=0, le=FILE_CHUNK)


class FilesWrite(_Command):
    """One chunk of an upload into folder `dir`, as file `name`. The first chunk (offset 0) creates
    `<dir>/.valet-upload-<upload>`; each appends at `offset` (which must be the size so far); the `final` one renames
    it to `name`: `exists` says what to do if that's taken ("fail": error "exists"; "replace": a file is replaced,
    never a folder or a link; "rename": `name-2.ext`, `name-3.ext`...). Reply {"size": int} (so far), the final one
    {"path": str, "size": int}. Errors as files.list, and "not a folder", "read-only", "exists"."""
    type: Literal["files.write"] = "files.write"
    agent: str
    upload: str                                 # control-generated uuid4 hex, the same for every chunk
    dir: str
    name: str = Field(min_length=1, max_length=255)
    offset: int = Field(default=0, ge=0)
    data: str = ""                              # base64, at most FILE_CHUNK bytes
    final: bool = False
    exists: Literal["fail", "replace", "rename"] = "fail"


class FilesCancel(_Command):
    """Drop an upload that won't finish: its partial file goes. Reply {}."""
    type: Literal["files.cancel"] = "files.cancel"
    agent: str
    upload: str
    dir: str


class FilesMkdir(_Command):
    """Make a folder (and the folders above it that are missing) inside a writable root; one already there is fine.
    Each folder is made inside its parent's resolved path, so never through a link that leads outside. Reply
    {"path": str}. Errors as files.list, and "read-only", "not a folder"."""
    type: Literal["files.mkdir"] = "files.mkdir"
    agent: str
    path: str


class VolumesList(_Command):
    """The agent's Docker volumes on this runner (feature volumes), whatever its level and state: reply
    {"volumes": [{"name": str, "mount": str}]}, those of valet-agent-<agent>-home (/home/agent) and -work (/work)
    that exist."""
    type: Literal["volumes.list"] = "volumes.list"
    agent: str


class VolumesRemove(_Command):
    """Delete the agent's volumes. Refused ("it's running: stop it first") while it runs here or its container
    exists. Reply {"removed": [names]}."""
    type: Literal["volumes.remove"] = "volumes.remove"
    agent: str


RunnerMessage = Annotated[
    Hello | Reply | AgentStatus | SessionUpdate | PermissionRequest | PermissionClosed | TurnEnd | TerminalOutput
    | TerminalClosed,
    Field(discriminator="type"),
]
ControlMessage = Annotated[
    Welcome | AgentStart | AgentStop | SessionNew | SessionPrompt | SessionCancel | PermissionAnswer | SessionImport
    | ClaudeSessions | ClaudeTranscript | TerminalOpen | TerminalClose | TerminalInput | TerminalResize | TerminalAck
    | FilesList | FilesRead | FilesWrite | FilesCancel | FilesMkdir | VolumesList | VolumesRemove | AgentForget,
    Field(discriminator="type"),
]
# Control -> runner messages that aren't commands: no id, no reply, handled in the order they arrive.
ONE_WAY = (TerminalInput, TerminalResize, TerminalAck)
_runner_adapter = TypeAdapter(RunnerMessage)
_control_adapter = TypeAdapter(ControlMessage)


def parse_runner_message(data: dict[str, Any]):
    """Validate a frame the control plane received. Raises pydantic.ValidationError."""
    return _runner_adapter.validate_python(data)


def parse_control_message(data: dict[str, Any]):
    """Validate a frame the runner received. Raises pydantic.ValidationError."""
    return _control_adapter.validate_python(data)

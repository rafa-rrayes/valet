"""The gateway's per-tool rule: allow, ask or deny (DESIGN.md, "Permissions", the gateway column). Pure, so the
whole table is tested in one place.

decide() is the level's part: the agent's permission level and the tool's MCP annotations. Annotations are hints from
the server, read the way the MCP spec defines their defaults: a tool is read-only only if it says
`readOnlyHint: true`, and a tool that isn't read-only is destructive unless it says `destructiveHint: false`. A server
that says nothing gets the cautious reading.

decide_tool() is the whole decision: the owner's rule for the tool (tools.rules), else the rule for every tool of
its server ("*"), else the level's; then a live hour grant turns an `ask` into an `allow` (never a `deny`)."""
from collections.abc import Mapping
from typing import Any, Literal

Decision = Literal["allow", "ask", "deny"]
Source = Literal["rule", "grant", "level", "server"]  # server: it decides its own calls (valet.browser)
LEVELS = ("locked", "ask", "trusted", "unrestricted")
ALL = "*"                   # a rule for every tool of a server (shared.definition.ALL_TOOLS)


def read_only(annotations: Mapping[str, Any] | None) -> bool:
    return bool(annotations) and annotations.get("readOnlyHint") is True


def destructive(annotations: Mapping[str, Any] | None) -> bool:
    """Not read-only, and not explicitly additive (`destructiveHint: false`)."""
    return not read_only(annotations) and (annotations or {}).get("destructiveHint") is not False


def decide(level: str, annotations: Mapping[str, Any] | None) -> Decision:
    """`annotations`: the tool's MCP ToolAnnotations as wire JSON (camelCase), or None.

    locked        read-only tools run; anything else is denied
    ask           read-only tools run; anything else asks
    trusted       runs, except destructive tools ("commits": send, pay, delete, publish...), which ask
    unrestricted  runs (the audit log still records it)

    An unknown level is treated as `ask`."""
    if level == "unrestricted":
        return "allow"
    if read_only(annotations):
        return "allow"
    if level == "locked":
        return "deny"
    if level == "trusted":
        return "ask" if destructive(annotations) else "allow"
    return "ask"


def rule_for(rules: Mapping[str, str] | None, tool: str) -> str | None:
    """The owner's rule for `tool` among one server's rules: its own entry, else the server's "*", else None."""
    if not rules:
        return None
    return rules.get(tool) or rules.get(ALL)


def decide_tool(level: str, annotations: Mapping[str, Any] | None, rules: Mapping[str, str] | None, tool: str,
                granted: bool = False) -> tuple[Decision, Source]:
    """(decision, why) for one call to `tool`. `rules`: the agent's tools.rules for the tool's server (tool name or
    "*" -> allow | ask | deny), or None. `granted`: a live hour grant for this agent, server and tool.

    A rule beats the level both ways: `allow` lets even a locked agent make the call (and lists the tool), `deny`
    refuses it (and hides it) even when unrestricted. A grant only turns an ask into an allow, whether the ask came
    from a rule or the level."""
    rule = rule_for(rules, tool)
    decision, source = (rule, "rule") if rule in ("allow", "ask", "deny") else (decide(level, annotations), "level")
    if decision == "ask" and granted:
        return "allow", "grant"
    return decision, source

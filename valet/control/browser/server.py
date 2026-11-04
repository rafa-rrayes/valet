"""`valet.browser`: the browser as one of Valet's own MCP servers in the gateway. Its tools take a `profile`, one of
the agent's `browser.profiles`, and decide each call by the browser's rules (policy.py, through actions.py): the
gateway's annotation rule doesn't apply on top (`decides_calls`), so nothing asks twice, and the tools stay listed
at every level (reading is always allowed)."""
import base64
import json
from typing import Any

import mcp_types as types

from valet.shared.definition import DefinitionError

from ..gateway.upstream import Upstream, UpstreamError, say_tools
from .actions import KEYS, ActionError, Refused
from .policy import BASELINE

PROFILE = {"type": "string", "description": "The browser profile to use: one of yours (see profiles())"}
TAB = {"type": "string", "description": "The tab's id (from tabs() or an earlier result); default: the most recent"}
REF = {"type": "string", "description": "The element's ref from the latest snapshot, like e12"}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _tool(name: str, title: str, description: str, props: dict[str, Any], required: list[str], *,
          read_only: bool = False, destructive: bool = False) -> types.Tool:
    return types.Tool(name=name, title=title, description=description, input_schema=_schema(props, required),
                      annotations=types.ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                                                        open_world_hint=True))


TOOLS = [
    _tool("profiles", "Browser profiles",
          "The browser profiles you may use, each with its site rules (yours first, then the profile's), what your "
          "permission level does where no rule says, and the sites with saved logins.", {}, [], read_only=True),
    _tool("tabs", "List tabs", "The profile's open tabs: id, URL and title.", {"profile": PROFILE}, ["profile"],
          read_only=True),
    _tool("open_tab", "Open a tab",
          "Open a URL in a new tab. Returns the tab's id and the page as an accessibility snapshot, with refs like "
          "[ref=e12] that click, type_text and select_option act on.",
          {"profile": PROFILE, "url": {"type": "string"}}, ["profile", "url"], read_only=True),
    _tool("navigate", "Go to a URL", "Load a URL in an existing tab. Returns the page.",
          {"profile": PROFILE, "url": {"type": "string"}, "tab": TAB}, ["profile", "url"], read_only=True),
    _tool("snapshot", "Read the page",
          "The tab's page as an accessibility snapshot with refs. Take a new one after the page changes (refs "
          "change with it). A long page comes in parts: pass the `start` its truncation note gives.",
          {"profile": PROFILE, "tab": TAB, "start": {"type": "integer", "minimum": 0}}, ["profile"], read_only=True),
    _tool("screenshot", "Screenshot", "A JPEG of the tab (password fields blanked).",
          {"profile": PROFILE, "tab": TAB}, ["profile"], read_only=True),
    _tool("click", "Click", "Click the element with this ref. Clicks that save, send, pay, delete, publish or "
          "submit a form are commits.", {"profile": PROFILE, "ref": REF, "tab": TAB}, ["profile", "ref"],
          destructive=True),
    _tool("type_text", "Type", "Type into a field, replacing what's in it; submit=true presses Enter after (a "
          "commit). Never for passwords: call login().",
          {"profile": PROFILE, "ref": REF, "text": {"type": "string"}, "submit": {"type": "boolean"}, "tab": TAB},
          ["profile", "ref", "text"], destructive=True),
    _tool("select_option", "Choose an option", "Choose option(s) in a select element.",
          {"profile": PROFILE, "ref": REF, "values": {"type": "array", "items": {"type": "string"}}, "tab": TAB},
          ["profile", "ref", "values"]),
    _tool("press_key", "Press a key", "Press a key in the tab (Enter in a form submits it: a commit).",
          {"profile": PROFILE, "key": {"type": "string", "enum": list(KEYS)}, "tab": TAB}, ["profile", "key"],
          destructive=True),
    _tool("scroll", "Scroll", "Scroll the tab up or down. Returns the page.",
          {"profile": PROFILE, "direction": {"type": "string", "enum": ["down", "up"]}, "tab": TAB}, ["profile"],
          read_only=True),
    _tool("back", "Go back", "Go back in the tab's history.", {"profile": PROFILE, "tab": TAB}, ["profile"],
          read_only=True),
    _tool("close_tab", "Close a tab", "Close a tab.", {"profile": PROFILE, "tab": TAB}, ["profile", "tab"]),
    _tool("login", "Sign in",
          "Sign in on the tab's page with the login {owner} saved in Valet's vault for this site: Valet fills the "
          "username, the password and a 2FA code itself. You never see them. `entry`: which saved login, when there "
          "are several (profiles() lists the sites).",
          {"profile": PROFILE, "tab": TAB, "entry": {"type": "string"}}, ["profile"]),
    _tool("request_human", "Ask {owner} to take over",
          "Ask {owner} to take over the tab in Valet's live view, for what only a person can do: a captcha, a code "
          "sent by SMS or email, a passkey or phone prompt. Waits until {owner} says it's done (or can't).",
          {"profile": PROFILE, "reason": {"type": "string", "description": "What {owner} should do, in a sentence"},
           "tab": TAB}, ["profile", "reason"]),
]
KIND_WORDS = {"read": "open pages, read snapshots and screenshots, scroll",
              "interact": "click links, type, choose options",
              "commit": "buttons that save, send, pay, delete, publish; submitting a form",
              "login": "Valet fills a saved login"}


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=error)


def _data(data: Any) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(data, indent=1, ensure_ascii=False))],
                                structured_content=data if isinstance(data, dict) else {"items": data})


def render(r: dict[str, Any]) -> str:
    """An action's result as text: the tab's id, URL, title and note, then the page."""
    lines = [f"{k}: {r[k]}" for k in ("tab", "url", "title", "note") if r.get(k)]
    out = "\n".join(lines)
    if r.get("page"):
        out += "\n\n" + r["page"]
    return out or "done"


def _str(args: dict, key: str, required: bool = True) -> str | None:
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise ActionError(f"`{key}` must be a string")
    return v


class BrowserServer(Upstream):
    name = "valet.browser"
    description = ("{Owner}'s browser: real Chrome with {owner}'s logins, per profile. Valet's site rules decide every call; "
                   "login() fills saved passwords you never see.")
    decides_calls = True

    def __init__(self, control: Any) -> None:
        self.control = control

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        return say_tools(TOOLS)

    def _profiles(self, agent: str) -> dict[str, Any]:
        svc = self.control.browser
        d = self.control.defs.definition(agent)
        vault = self.control.secrets.vault
        out = []
        for name in d.browser.profiles:
            p = svc.profile(name)
            if p is None:
                out.append({"name": name, "error": "this profile isn't in Valet any more"})
                continue
            out.append({"name": name, "title": p.title, "note": p.note, "your_rules": d.browser.rules,
                        "profile_rules": p.sites, "logins": vault.sites(name)})
        level = d.permissions
        return {"agent": agent, "permissions": level, "profiles": out,
                "elsewhere": dict(BASELINE.get(level, BASELINE["ask"])), "kinds": KIND_WORDS,
                "precedence": "your rule for the most specific matching site, then the profile's, then your level; "
                              "ask means {owner} decides, and you wait for the answer"}

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        """`timeout` (the gateway's upstream call limit) doesn't apply: a call may wait for the owner's answer, up to
        the ask timeout."""
        args = arguments or {}
        svc = self.control.browser
        if tool not in {t.name for t in TOOLS}:
            raise UpstreamError(f"valet.browser has no tool {tool!r}")
        try:
            if tool == "profiles":
                return _data(self._profiles(caller))
            profile = _str(args, "profile")
            c = svc.caller(caller, profile)
            a = svc.actions
            tab = _str(args, "tab", required=False)
            if tool == "tabs":
                return _data({"profile": profile, "tabs": await a.tabs(c)})
            if tool == "open_tab":
                return _text(render(await a.open_tab(c, _str(args, "url"))))
            if tool == "navigate":
                return _text(render(await a.navigate(c, tab, _str(args, "url"))))
            if tool == "snapshot":
                start = args.get("start", 0)
                if not isinstance(start, int) or start < 0:
                    raise ActionError("`start` must be a whole number, 0 or more")
                return _text(render(await a.snapshot(c, tab, start)))
            if tool == "screenshot":
                jpeg, info = await a.screenshot(c, tab)
                return types.CallToolResult(content=[
                    types.ImageContent(data=base64.b64encode(jpeg).decode(), mime_type="image/jpeg"),
                    types.TextContent(text=f"tab: {info['tab']}\nurl: {info['url']}")])
            if tool == "click":
                return _text(render(await a.click(c, tab, _str(args, "ref"))))
            if tool == "type_text":
                submit = args.get("submit", False)
                if not isinstance(submit, bool):
                    raise ActionError("`submit` is true or false")
                return _text(render(await a.type_text(c, tab, _str(args, "ref"), _str(args, "text", required=False)
                                                      or "", submit)))
            if tool == "select_option":
                return _text(render(await a.select_option(c, tab, _str(args, "ref"), args.get("values"))))
            if tool == "press_key":
                return _text(render(await a.press_key(c, tab, _str(args, "key"))))
            if tool == "scroll":
                return _text(render(await a.scroll(c, tab, args.get("direction") or "down")))
            if tool == "back":
                return _text(render(await a.back(c, tab)))
            if tool == "close_tab":
                return _data(await a.close_tab(c, _str(args, "tab")))
            if tool == "login":
                return _text(render(await a.login(c, tab, _str(args, "entry", required=False))))
            if tool == "request_human":
                return _text(render(await a.request_human(c, tab, _str(args, "reason"))))
        except (Refused, ActionError, PermissionError) as e:
            return _text(str(e), error=True)
        except (DefinitionError, FileNotFoundError) as e:
            return _text(f"Valet can't read this agent's definition: {e}", error=True)
        raise UpstreamError(f"valet.browser has no tool {tool!r}")

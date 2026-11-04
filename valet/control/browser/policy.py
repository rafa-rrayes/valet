"""The browser's decision: allow, ask or deny an action, from the agent's permission level, its own site rules
(agent.yaml `browser.rules`), the profile's (browser.yaml) and its live hour grants. Pure, so the whole table is
tested in one place (tests/control/test_browser_policy.py), like the gateway's policy.py.

Precedence, for the action's kind on its URL:
1. only http(s) pages and about:blank: anything else is denied;
2. the agent's rule for the most specific of its patterns that matches and sets this kind;
3. else the profile's rule, chosen the same way;
4. else the level's baseline for the kind (DESIGN.md, "Permissions", the gateway / browser column):

   level          read    interact  commit  login
   locked         allow   deny      deny    deny
   ask            allow   ask       ask     ask
   trusted        allow   allow     ask     allow
   unrestricted   allow   allow     allow   allow   (logged, like everything)

and an `ask` with a live hour grant for that host and kind is an `allow`. A rule beats the level both ways: `allow`
lets even a locked agent do it, `deny` stops even an unrestricted one."""
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from valet.shared.browser import best_rule, host_of
from valet.shared.owner import say

Mode = Literal["allow", "ask", "deny"]

BASELINE: dict[str, dict[str, Mode]] = {
    "locked": {"read": "allow", "interact": "deny", "commit": "deny", "login": "deny"},
    "ask": {"read": "allow", "interact": "ask", "commit": "ask", "login": "ask"},
    "trusted": {"read": "allow", "interact": "allow", "commit": "ask", "login": "allow"},
    "unrestricted": {"read": "allow", "interact": "allow", "commit": "allow", "login": "allow"},
}


@dataclass(frozen=True)
class Verdict:
    mode: Mode
    reason: str             # plain words, shown to the agent and in the audit log

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", say(self.reason))      # {owner}: the name in the config


def decide(level: str, agent_rules: Mapping[str, Mapping[str, str]], profile_rules: Mapping[str, Mapping[str, str]],
           kind: str, url: str, grants: Collection[tuple[str, str]] = ()) -> Verdict:
    """`agent_rules`, `profile_rules`: site pattern -> kind -> mode. `grants`: the (host, kind) pairs this agent holds
    a live hour grant for, in this profile. An unknown level is treated as `ask`."""
    if re.match(r"^about:blank([?#]|$)", url):
        return Verdict("allow", "a blank page")
    if not re.match(r"^https?://", url, re.I) or not host_of(url):
        return Verdict("deny", "only http(s) pages are allowed")
    hit = best_rule(dict(agent_rules), url, kind)
    if hit:
        mode, reason = hit[1], f"your rule for {hit[0]}"
    elif hit := best_rule(dict(profile_rules), url, kind):
        mode, reason = hit[1], f"the profile's rule for {hit[0]}"
    else:
        base = BASELINE.get(level, BASELINE["ask"])
        mode, reason = base.get(kind, "ask"), f"your permission level, {level if level in BASELINE else 'ask'}"
    if mode == "ask" and (host_of(url), kind) in set(grants):
        return Verdict("allow", "{Owner} allowed it for an hour")
    return Verdict(mode, reason)


# A click is a commit when it submits a form or its label says it saves, sends, pays, deletes, publishes... (a
# list in English, Portuguese and Spanish).
SENSITIVE = re.compile(
    r"\b(delete|remove|destroy|erase|wipe|pay|payment|purchase|buy|checkout|place order|order now|subscribe|"
    r"upgrade|downgrade|cancel|transfer|withdraw|send|submit|confirm|publish|post|share|invite|approve|accept|"
    r"authorize|grant|revoke|reset|disable|enable|deactivate|save|apply|update|create|add|connect|install|"
    r"uninstall|sign out|log ?out|excluir|apagar|remover|pagar|comprar|enviar|confirmar|publicar|salvar|criar|"
    r"adicionar|aplicar|eliminar|borrar|guardar|agregar|instalar)\b", re.I)


def classify_click(name: str, el_type: str = "", submit: bool = False) -> Literal["interact", "commit"]:
    """`submit`: the element submits a form (a submit button, or a button without a type inside a form)."""
    if submit or (el_type or "").lower() == "submit" or SENSITIVE.search(name or ""):
        return "commit"
    return "interact"

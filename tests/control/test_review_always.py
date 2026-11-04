"""Review of m4: "Always allow" answers write a rule into agent.yaml by reading it, adding the rule and writing it
back. Two such answers at once (two browser asks, or a browser ask and a gateway ask) must both end up in the file."""
import asyncio
from types import SimpleNamespace

from valet.control.browser.service import BrowserService
from valet.control.definitions import DefinitionsRepo
from valet.control.gateway import scopes

AGENT = "name: scout\nharness: claude\npermissions: ask\ntools:\n  mcp: [github, valet.browser]\nbrowser:\n  profiles: [alice]\n"


def _service(tmp_path) -> tuple[BrowserService, DefinitionsRepo]:
    defs = DefinitionsRepo(tmp_path / "defs")
    defs.init()
    defs.create("scout", AGENT)

    async def emit_agent(name):
        pass

    control = SimpleNamespace(defs=defs, hub=SimpleNamespace(emit_agent=emit_agent))
    return BrowserService(control), defs


def test_two_browser_always_answers_at_once_keep_both_rules(tmp_path):
    svc, defs = _service(tmp_path)

    async def both():
        return await asyncio.gather(svc.always("scout", "github.com", "commit"),
                                    svc.always("scout", "gitlab.com", "interact"))

    assert asyncio.run(both()) == [True, True]
    assert defs.definition("scout").browser.rules == {"github.com": {"commit": "allow"},
                                                      "gitlab.com": {"interact": "allow"}}


def test_a_browser_and_a_gateway_always_at_once_keep_both_rules(tmp_path):
    svc, defs = _service(tmp_path)

    async def both():
        return await asyncio.gather(svc.always("scout", "github.com", "commit"),
                                    asyncio.to_thread(scopes.always_allow, defs, "scout", "github", "create_issue"))

    asyncio.run(both())
    d = defs.definition("scout")
    assert d.browser.rules == {"github.com": {"commit": "allow"}}
    assert d.tools.rules == {"github": {"create_issue": "allow"}}

"""Review of m4: where the merged tracks meet (gateway_servers() against tools.mcp, ...)."""
from valet.control import directives
from valet.shared.definition import AgentDefinition
from valet.shared.owner import say


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


def test_the_environment_says_alice_set_rules_for_valet_memory_too():
    """valet.memory is a gateway server without being in tools.mcp (memory: true): the owner's rule for one of its tools
    is one of "some of them" the environment says rules come first for."""
    d = agent(memory=True, tools={"rules": {"valet.memory": {"delete": "deny"}}})
    assert say(directives.RULES_LINE) in directives.environment_text(d)
    assert say(directives.RULES_LINE) not in directives.environment_text(agent(memory=True))

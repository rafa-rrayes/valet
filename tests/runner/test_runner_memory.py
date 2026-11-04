"""An agent with `memory: true` and no tools.mcp still reaches the gateway (valet.memory): both harness configs
let its tools through, like any gateway server's; and its memory layer joins the directives last."""
from pathlib import Path

from valet.runner.harness import claude, deepseek
from valet.runner.launch import CONFIG_MOUNT
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.directives import assemble


def cfg() -> RunnerConfig:
    return RunnerConfig(control_url="ws://x/ws/runner", token="vrun_t", data=Path("/srv/runner"))


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "computer": {"level": "host"}, **kw})


def test_claude_allows_valet_memory():
    def allow(d):
        meta = claude.compile(d, {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
        return meta["managedSettings"].get("permissions", {}).get("allow", [])
    assert allow(agent(memory=True)) == ["mcp__valet"]
    assert allow(agent()) == []


def test_deepseek_lets_valet_memory_through():
    def patch(d):
        return deepseek.compile(d, {}, cfg(), CONFIG_MOUNT).files["dsh.patch.yml"]
    assert '        allowPrefixes: ["mcp__valet__"]\n' in patch(agent(harness="deepseek", memory=True))
    assert "allowPrefixes" not in patch(agent(harness="deepseek"))


def test_the_memory_layer_reaches_the_prompt():
    d = agent(memory=True, directives=["persona.md", "environment", "memory"])
    files = {"persona.md": "You are Scout.", "environment.md": "# Your environment", "memory.md": "# Your memory"}
    assert assemble(d.directives, files) == "You are Scout.\n\n# Your environment\n\n# Your memory"
    text = claude.compile(d, files, cfg(), CONFIG_MOUNT).session_meta["systemPrompt"]
    assert text["append"].endswith("# Your memory")
    # Memory off: its layer is empty and drops out.
    assert assemble(d.directives, {**files, "memory.md": ""}).endswith("# Your environment")

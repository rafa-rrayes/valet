"""The environment layer's M4 sections: the agent's skills (names, one-line descriptions, how its harness loads
them), how its memory works when it has memory, and what the shared space is when it has valet.shared."""
from valet.control import directives
from valet.shared.definition import AgentDefinition

YAML = "name: scout\nharness: {harness}\ncomputer:\n  level: host\npermissions: ask\ndirectives: [environment]\n"


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


def section(text: str, title: str) -> str:
    """One `## title` section's body ('' if there's none)."""
    parts = text.split("\n## ")
    return next((p.split("\n\n", 1)[1].strip() for p in parts if p.startswith(title + "\n")), "")


def test_skills():
    text = directives.environment_text(agent(), skills={"pdf-forms": "Fill in PDF forms.\nUse it for any PDF.",
                                                       "code-word": "x" * 300})
    body = section(text, "Skills")
    assert "Load one with the Skill tool when a task calls for it; its name there is valet:<name>." in body
    assert "- code-word: " + "x" * 199 + "…" in body
    assert "- pdf-forms: Fill in PDF forms. Use it for any PDF." in body
    assert body.index("code-word") < body.index("pdf-forms")
    dsh = directives.environment_text(agent(harness="deepseek"), skills={"pdf-forms": "Fill in PDF forms."})
    assert "Load one with the skill tool when a task calls for it, by its name." in dsh
    assert section(directives.environment_text(agent()), "Skills") == "Valet gives you no skills."
    # In order: tools, skills, then secrets.
    assert text.index("## Tools through Valet") < text.index("## Skills") < text.index("## Secrets")


def test_memory_section():
    assert "## Memory" not in directives.environment_text(agent())
    on = directives.environment_text(agent(memory=True, directives=["persona.md", "environment", "memory"]))
    body = section(on, "Memory")
    assert body.startswith("You have a memory in Valet: Markdown files you keep for yourself")
    assert "Your instructions carry them as they were when you started (\"Your memory\")" in body
    assert "none of these calls waits for approval, at any level" in body
    assert "- valet.memory" in section(on, "Tools through Valet")
    no_layer = directives.environment_text(agent(memory=True, directives=["environment"]))
    assert "Your instructions don't carry them: list and read them when you start on something." in no_layer
    assert not any(w in on.split() for w in ("he", "him", "his", "himself"))


def test_shared_section():
    assert "## The shared space" not in directives.environment_text(agent(tools={"mcp": ["valet.agents"]}))
    text = directives.environment_text(agent(tools={"mcp": ["valet.shared"]}))
    body = section(text, "The shared space")
    assert body.startswith("valet.shared is your shared space with the owner")
    assert "between you and the owner only" in body and "No other agent sees it" in body
    assert "shared_get gives a download URL" in body and "within 15 minutes" in body
    bare = directives.environment_text(agent(computer={"level": "none"}, tools={"mcp": ["valet.shared"]}))
    assert "that's the only way files reach you or leave you" in section(bare, "The shared space")
    assert "download URL" not in section(bare, "The shared space")
    assert not any(w in text.split() for w in ("he", "him", "his"))


def test_the_skills_an_agent_gets_are_listed(client):
    r = client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML.format(harness="claude")})
    assert r.status_code == 201, r.text
    r = client.post("/api/agents/scout/skills", json={"name": "code-word", "description": "Answers the code word.",
                                                      "instructions": "Say BANANA."})
    assert r.status_code == 201, r.text
    env = client.get("/api/agents/scout/directives").json()["text"]
    assert "- code-word: Answers the code word." in section(env, "Skills")
    # A preview of unsaved settings reads the skills from the repo too.
    d = client.get("/api/agents/scout").json()["definition"]
    p = client.post("/api/agents/scout/directives/preview", json={"definition": {**d, "harness": "deepseek"}}).json()
    assert "- code-word: Answers the code word." in p["text"] and "by its name" in p["text"]

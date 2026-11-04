"""Triggers in agent.yaml (shared/definition.py) and how the environment layer tells the agent about them
(control/directives.py, "How you're prompted")."""
import pytest

from valet.control import directives
from valet.shared.definition import (
    TRIGGER_PROMPT_LIMIT, AgentDefinition, DefinitionError, Trigger, parse_agent, to_yaml,
)


def test_a_trigger_prompts_the_main_session_unless_it_asks_for_a_new_one():
    d = parse_agent("name: scout\ntriggers:\n"
                    "  - {schedule: '0  8 * * *', prompt: Morning brief}\n"
                    "  - {webhook: deploy, session: new}\n", "scout")
    brief, deploy = d.triggers
    assert brief.session == "main" and deploy.session == "new"
    assert brief.schedule == "0 8 * * *"                       # spacing is normalized
    assert brief.key == "schedule:0 8 * * *" and deploy.key == "webhook:deploy"
    assert d.webhook("deploy") is deploy and d.webhook("other") is None
    # Written back, the default session isn't spelled out; a new one is.
    text = to_yaml(d)
    assert "session: new" in text and "session: main" not in text
    assert parse_agent(text, "scout") == d


@pytest.mark.parametrize("trigger,needle", [
    ("{schedule: '0 8 * *', prompt: hi}", "five fields"),
    ("{schedule: '61 * * * *', prompt: hi}", "minute"),
    ("{schedule: '0 0 30 2 *', prompt: hi}", "never"),
    ("{schedule: '0 8 * * *'}", "a schedule needs a prompt"),
    ("{schedule: '0 8 * * *', prompt: '  '}", "a schedule needs a prompt"),
    ("{webhook: Deploy}", "a webhook's name is lowercase letters"),
    ("{webhook: -deploy}", "a webhook's name is lowercase letters"),
    ("{webhook: deploy, session: old}", "session"),
    ("{webhook: deploy, schedule: '0 8 * * *', prompt: hi}", "exactly one of schedule or webhook"),
])
def test_trigger_errors(trigger, needle):
    with pytest.raises(DefinitionError) as e:
        parse_agent(f"name: scout\ntriggers:\n  - {trigger}\n", "scout")
    assert needle in str(e.value)


def test_a_trigger_listed_twice_is_an_error():
    with pytest.raises(DefinitionError) as e:
        parse_agent("name: scout\ntriggers:\n  - {webhook: deploy}\n  - {webhook: deploy, prompt: again}\n", "scout")
    assert "the webhook deploy is listed twice" in str(e.value)
    with pytest.raises(DefinitionError) as e:
        parse_agent("name: scout\ntriggers:\n  - {schedule: '0 8 * * *', prompt: a}\n"
                    "  - {schedule: '0  8 * * *', prompt: b}\n", "scout")
    assert "is listed twice: put its prompts in one" in str(e.value)
    # The same time written differently is two triggers (each fires).
    d = parse_agent("name: scout\ntriggers:\n  - {schedule: '0 8 * * *', prompt: a}\n"
                    "  - {schedule: '0 8 * * 0-6', prompt: b}\n", "scout")
    assert len(d.triggers) == 2


def test_a_triggers_prompt_has_a_limit():
    Trigger(webhook="w", prompt="x" * TRIGGER_PROMPT_LIMIT)
    with pytest.raises(ValueError):
        Trigger(webhook="w", prompt="x" * (TRIGGER_PROMPT_LIMIT + 1))


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "directives": ["environment"], **kw})


def section(text: str) -> str:
    return text.split("## How you're prompted\n", 1)[1].split("\n## ", 1)[0]


def test_environment_says_how_the_agent_is_prompted():
    d = agent(tools={"mcp": ["valet.owner", "valet.agents"]}, triggers=[
        {"schedule": "30 9 * * 1-5", "prompt": "Check the\n  overnight builds"},
        {"webhook": "deploy", "session": "new", "prompt": "A deploy finished."},
        {"webhook": "ping"}])
    s = section(directives.environment_text(d, [], timezone="America/Sao_Paulo"))
    assert ('- Weekdays at 09:30 (America/Sao_Paulo time), `30 9 * * 1-5`, in your main session: '
            '"Check the overnight builds"') in s
    assert ("- When webhook `deploy` is called from outside Valet, in a new session, with what the call sent: "
            '"A deploy finished."') in s
    assert "- When webhook `ping` is called from outside Valet, in your main session, with what the call sent\n" in s
    assert "only the latest one catches up" in s
    assert "`Valet: ...`" in s and "UNTRUSTED WEBHOOK DATA" in s
    assert "With valet.owner you reach the owner" in s and "`schedule_wakeup`" in s
    assert "With valet.agents `send_message`" in s


def test_environment_without_triggers_or_valet_owner():
    s = section(directives.environment_text(agent(), []))
    assert "You have no schedules or webhooks" in s
    assert "You don't have valet.owner" in s and "send_message" not in s
    assert "time)" not in s                                  # no zone named without one


def test_environment_quotes_a_long_prompt_shortly():
    d = agent(triggers=[{"schedule": "0 8 * * *", "prompt": "word " * 200}])
    s = section(directives.environment_text(d, []))
    line = next(x for x in s.splitlines() if x.startswith("- Every day at 08:00"))
    assert line.endswith('…"') and len(line) < directives.PROMPT_SHOWN + 80

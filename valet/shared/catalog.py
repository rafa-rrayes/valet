"""What the settings page offers: harnesses and their models, permission levels, computer levels, internet choices
and templates.
Plain data, served by GET /api/catalog; the definition model (shared.definition) stays the source of truth for what
is valid. A model not listed here still works: the form has "Other"."""

HARNESSES = [
    {"id": "claude", "name": "Claude", "by": "Anthropic", "key": "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY",
     "models": [
         {"id": "claude-opus-5-5", "name": "Opus 5.5", "note": "most capable"},
         {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5", "note": "fast and capable"},
         {"id": "claude-haiku-5-5", "name": "Haiku 5.5", "note": "fastest"},
         {"id": "claude-fable-5-1", "name": "Fable 5.1"},
     ]},
    {"id": "deepseek", "name": "DeepSeek", "by": "DeepSeek Harness", "key": "DEEPSEEK_API_KEY",
     "models": [
         {"id": "deepseek-v4-pro", "name": "V4 Pro", "note": "most capable"},
         {"id": "deepseek-v4-flash", "name": "V4 Flash", "note": "fast"},
     ]},
]

PERMISSIONS = [
    {"id": "locked", "name": "Locked",
     "summary": "Reads and answers. It can't edit files or run commands."},
    {"id": "ask", "name": "Ask",
     "summary": "Asks you before it edits files or runs commands."},
    {"id": "trusted", "name": "Trusted",
     "summary": "Does anything inside its own computer without asking.",
     "host": "On this machine it still asks before the risky steps."},
    {"id": "unrestricted", "name": "Unrestricted",
     "summary": "Never asks. Wherever it runs, it can do anything it's able to."},
]

LEVELS = [
    {"id": "container", "name": "Its own computer",
     "summary": "A private Linux container with its own files, kept between restarts."},
    {"id": "none", "name": "No computer",
     "summary": "It talks and uses the tools you give it, nothing more: no files, no commands, no internet."},
    {"id": "host", "name": "This machine",
     "summary": "Runs directly on the runner's machine, as its user: it reaches what that user can."},
]

# What its own computer can reach on the internet (computer.network). Its model is always reachable, and so are
# Valet's tools; `none` in a file means the same as `gateway`. On this machine the agent has the machine's network.
NETWORKS = [
    {"id": "open", "name": "Full internet", "summary": "Any site, like a normal computer."},
    {"id": "gateway", "name": "Only its model and Valet's tools",
     "summary": "Nothing else on the internet, and no web search."},
    {"id": "allowlist", "name": "Only these sites",
     "summary": "Its model, Valet's tools and the sites you list, with their subdomains. No web search."},
]

# Personas use {who}: the agent's display name.
TEMPLATES = [
    {"id": "assistant", "name": "Assistant", "emoji": "🧭", "definition": {"permissions": "ask"},
     "persona": "You are {who}, one of {owner}'s agents: a general assistant.\n\n"
                "- Be brief and direct. Lead with the answer.\n"
                "- When a request is ambiguous, make a sensible choice, say which, and carry on.\n"
                "- When you finish something, say what you did and what's left.\n"},
    {"id": "coder", "name": "Coder", "emoji": "🛠️", "definition": {"permissions": "trusted"},
     "persona": "You are {who}, one of {owner}'s agents: a software engineer.\n\n"
                "- Read the code before you change it. Keep changes small and focused.\n"
                "- Run the tests before you say something works.\n"
                "- Report what you changed, how you checked it, and anything you're unsure about.\n"},
    {"id": "researcher", "name": "Researcher", "emoji": "🔎", "definition": {"permissions": "ask"},
     "persona": "You are {who}, one of {owner}'s agents: a researcher.\n\n"
                "- Look things up instead of guessing, and say where each fact comes from.\n"
                "- Keep what you found apart from what you think.\n"
                "- Lead with the answer, then the evidence. Keep it short.\n"},
    {"id": "blank", "name": "Blank", "emoji": None, "definition": {},
     "persona": "You are {who}, one of {owner}'s agents.\n"},
]


def template(template_id: str) -> dict | None:
    return next((t for t in TEMPLATES if t["id"] == template_id), None)


# The standing authority (authority/<preset>.md in the definitions repo) a new agent from a template starts with, by
# its permission level: the presets Valet ships (valet/control/authority/). The settings page marks the one for the
# agent's level as suggested.
AUTHORITY = {"locked": "strict", "ask": "standard", "trusted": "full", "unrestricted": "full"}

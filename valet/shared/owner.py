"""The owner's name in the texts Valet writes for agents and for people.

The name is one setting, VALET_OWNER_NAME (ControlConfig.owner_name; default "the owner", which reads naturally in
every text: "Waits for the owner's approval"). Texts are written with placeholders and rendered where they're
produced, with say():

    {owner}    the name as it is: "the owner", "Rafa"            ("message {owner}", "{owner}'s inbox")
    {Owner}    the same with its first letter capital: "The owner", "Rafa"   (the start of a sentence)

Nothing else in a text is touched, so braces that aren't these two words stay as they are. The name is process-wide,
set once by the control plane when it starts (configure(), from the config: never at import time, so the
environment is read first); say() takes it when it runs, so a tool description or a preset built at import time
still shows the configured name.
"""
import re
from typing import Any

DEFAULT = "the owner"
PLACEHOLDER = re.compile(r"\{(owner|Owner)\}")

_name = DEFAULT


def configure(name: str | None) -> None:
    """Set the name every say() uses from now on (the default when empty)."""
    global _name
    _name = (name or "").strip() or DEFAULT


def name() -> str:
    return _name


def say(text: str, owner: str | None = None) -> str:
    """`text` with {owner} and {Owner} replaced: by the configured name, or by `owner`."""
    who = (owner if owner is not None else _name)
    return PLACEHOLDER.sub(lambda m: who if m.group(1) == "owner" else who[:1].upper() + who[1:], text)


def say_all(data: Any, owner: str | None = None) -> Any:
    """`data` (strings, lists, dicts, nested) with every string rendered by say(); a copy."""
    if isinstance(data, str):
        return say(data, owner)
    if isinstance(data, list):
        return [say_all(v, owner) for v in data]
    if isinstance(data, dict):
        return {k: say_all(v, owner) for k, v in data.items()}
    return data

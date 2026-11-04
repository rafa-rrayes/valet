"""The settings page's "Working folder" (computer.workdir), under This machine only."""
import copy

from tests.web.test_web_settings import DEFINITION, detail, puts, pytestmark, settings  # noqa: F401


def host(**computer) -> dict:
    d = copy.deepcopy(DEFINITION)
    d["computer"].update(level="host", mounts=[], **computer)
    return d


def test_only_this_machine_has_a_working_folder():
    assert "computer.workdir" not in settings()["controls"]
    r = settings([{"click": "level:host"}])
    assert r["controls"]["computer.workdir"]["value"] == ""
    assert "Working folder" in r["text"] and "its own folder, under the runner's data" in r["html"]


def test_it_shows_and_saves_the_folder():
    d = host(workdir="/Users/rafa/Code/valet")
    r = settings(d=detail(d))
    assert r["controls"]["computer.workdir"]["value"] == "/Users/rafa/Code/valet"
    edited = host(workdir="/srv/project")
    r = settings([{"input": "computer.workdir", "value": " /srv/project "}, {"button": "Save"}], d=detail(d),
                 put={"status": 200, "body": detail(edited)})
    assert puts(r) == [{"definition": edited}]
    # Emptied, it's null: its own folder again.
    r = settings([{"input": "computer.workdir", "value": "  "}, {"button": "Save"}], d=detail(d),
                 put={"status": 200, "body": detail(host())})
    assert puts(r)[0]["definition"]["computer"]["workdir"] is None


def test_its_error_shows_under_it():
    msg = "an absolute path on the runner's machine, like /Users/rafa/Code/project"
    r = settings([{"input": "computer.workdir", "value": "Code/valet"}, {"button": "Save"}], d=detail(host()),
                 put={"status": 422, "body": {"detail": "invalid", "errors": [{"field": "computer.workdir", "msg": msg}]}})
    assert r["errors"]["computer.workdir"] == msg

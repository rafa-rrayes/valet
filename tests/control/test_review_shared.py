"""Review of the shared space (m4): bugs shown by failing tests. Each test says what goes wrong."""
import errno
import logging
from pathlib import Path

from tests.control.test_ctl_shared import call, data, make_agent, start, upload
from valet.control.__main__ import RedactTokens
from valet.control.space import safe_name

BASE = "http://127.0.0.1:8780"


def test_a_replace_ticket_doesnt_land_on_a_new_item_with_the_old_id(client, connect, rpc):
    """`shared.id` is INTEGER PRIMARY KEY without AUTOINCREMENT, so deleting the newest item frees its id for the next
    one, and a transfer ticket only remembers the id. Scout gets a replace URL for its own draft (shared_update with
    upload: true, which Alice approved for that draft); Alice deletes the draft and uploads his own file, which gets the
    same id; scout's PUT then overwrites Alice's file, a change nobody approved."""
    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted")
        draft = data(call(client, token, "shared_add_file", {"name": "draft.txt", "content": "draft"}))
        url = data(call(client, token, "shared_update", {"id": draft["id"], "upload": True}))["upload_url"]
        assert client.delete(f"/api/shared/{draft['id']}").status_code == 200
        [mine] = upload(client, "taxes.txt", b"Alice's own file").json()
        assert mine["id"] != draft["id"]                    # as merged: the id came back
        anon = client.__class__(client.app)
        r = anon.put(url.removeprefix(BASE), content=b"scout's bytes")
        assert r.status_code == 404, r.text                 # the draft it was for is gone
        assert client.get(f"/api/shared/{mine['id']}/file").content == b"Alice's own file"


def test_a_file_name_fits_in_one_path_component():
    """safe_name caps a name at 120 characters, not bytes. On Linux (the control plane's image) a path component is at
    most 255 bytes, so a CJK or emoji name of 86+ characters fails with ENAMETOOLONG in SharedSpace._place: an upload
    is a 500 (an OSError the API, the gateway tool and the transfer route don't catch; the transfer isn't audited)."""
    assert len(safe_name("日" * 200 + ".txt").encode()) <= 255


def test_replacing_a_file_keeps_the_old_one_when_the_new_one_cant_be_placed(client, monkeypatch):
    """SharedSpace._place empties the item's folder before moving the new file in. When the move fails (Linux's
    255-byte name limit before safe_name counted bytes; here any failure, emulated), the old bytes are already gone
    while the row still points at them: Alice's file is lost and the item has no file (404) from then on."""
    make_agent(client)
    [item] = upload(client, "report.txt", b"the only copy").json()
    real = Path.replace

    def failing_replace(self, target):
        if Path(target).name == "new.txt":
            raise OSError(errno.EIO, "Input/output error", str(target))
        return real(self, target)
    monkeypatch.setattr(Path, "replace", failing_replace)
    quiet = client.__class__(client.app, raise_server_exceptions=False, cookies=client.cookies)
    r = quiet.put(f"/api/shared/{item['id']}/file", params={"name": "new.txt"}, content=b"new")
    assert r.status_code >= 400
    assert client.get(f"/api/shared/{item['id']}/file").content == b"the only copy"


def test_transfer_tickets_stay_out_of_the_access_log():
    """A transfer URL's ticket is its credential (a download one works again and again for 15 minutes), but uvicorn's
    access log line carries the path as is: RedactTokens only hides ?token= and ?key=, so anyone who can read the
    control plane's log (docker logs, Dokploy) can fetch the file without any token."""
    ticket = "Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4cXV1eHF1dXg"
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("172.17.0.5:41234", "GET", f"/mcp/agents/scout/shared/{ticket}", "1.1", 200), None)
    RedactTokens().filter(record)
    assert ticket not in record.getMessage()

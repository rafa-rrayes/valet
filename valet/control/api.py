"""REST routes under /api (docs/API.md). Thin: validation, auth, and calls into the db, the definitions repo
and the hub."""
import asyncio
import re
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from valet import __version__
from valet.shared import catalog, skills
from valet.shared.definition import (
    AGENT_FILE, NAME_RE, PERSONA_FILE, AgentDefinition, DefinitionError, read_files, to_yaml, validation_errors,
)
from valet.shared.directives import AUTHORITY, ENVIRONMENT, OWNER, PERSONA
from valet.shared.owner import say, say_all
from valet.shared.protocol import PermissionAnswer, SessionCancel, SessionNew

from . import api_triggers, auth, directives, removal, views
from .attachments import AttachmentError
from .computer import ComputerError
from .gateway import owner as owner_server
from .gateway.registry import RegistryError
from .hub import DURABLE, RunnerError
from .prompts import DeliveryError
from .secrets import SecretError

router = APIRouter(prefix="/api")
owner = APIRouter(dependencies=[Depends(auth.require_owner)])


def _control(request: Request):
    return request.app.state.control


def _id() -> str:
    return uuid.uuid4().hex


def _runner_error(e: RunnerError) -> HTTPException:
    return HTTPException(e.status, str(e))


# ---------- auth ----------
class Login(BaseModel):
    password: str
    client: Literal["web", "native"] = "web"
    device: str | None = None


@router.post("/login")
def login(body: Login, request: Request, response: Response):
    c = _control(request)
    if not c.db.get_setting("owner_password_hash"):
        raise HTTPException(409, "no owner password yet: run `valet-control set-password` on the server")
    ip = auth.client_ip(request, c.cfg.trust_cf_ip)
    attempt = c.logins.begin(ip)
    if attempt is None:
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    if not auth.check_password(c.db, body.password):
        raise HTTPException(401, "wrong password")
    c.logins.succeed(ip, attempt)
    signed = auth.issue_session(c.db)
    max_age = auth.SESSION_DAYS * 86400
    if body.client == "native":
        return {"ok": True, "token": auth.OWNER_PREFIX + signed, "expires_in": max_age}
    response.set_cookie(auth.COOKIE, signed, max_age=max_age, httponly=True, samesite="strict",
                        secure=c.cfg.public_url.startswith("https"))
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(auth.require_same_origin)])
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    c = _control(request)
    try:
        is_owner = True
        auth.require_owner(request)
    except HTTPException:
        is_owner = False
    return {"owner": is_owner, "setup": bool(c.db.get_setting("owner_password_hash")), "api": 1,
            "version": __version__}


# ---------- runners ----------
@owner.get("/runners")
def list_runners(request: Request):
    return views.runners(_control(request))


# ---------- agents ----------
def _agent_or_404(c, name: str) -> None:
    if not NAME_RE.match(name) or not c.defs.exists(name):
        raise HTTPException(404, f"no agent named {name}")


class NewAgent(BaseModel):
    """Raw files (agent_yaml, persona), or built for the owner: a template's or another agent's settings
    (copy_from), with `definition`'s top-level fields replacing the base's (a `computer` given there replaces the
    whole computer). Without any of them, the default template."""
    name: str
    agent_yaml: str | None = None
    persona: str | None = None
    template: str | None = None
    copy_from: str | None = None
    definition: dict[str, Any] | None = None


class FileText(BaseModel):
    text: str


class Invalid(Exception):
    """A definition the settings page sent that doesn't validate: 422 with an error per field."""

    def __init__(self, e: ValidationError | list[dict[str, str]]) -> None:
        self.errors = validation_errors(e) if isinstance(e, ValidationError) else e
        super().__init__("; ".join(f"{x['field']}: {x['msg']}" for x in self.errors))


def _invalid(e: Invalid) -> JSONResponse:
    return JSONResponse({"detail": str(e), "errors": e.errors}, status_code=422)


def _validate(data: dict[str, Any], c=None) -> AgentDefinition:
    """The settings page's definition, validated; with `c`, its authority preset and browser profiles must exist
    too."""
    try:
        d = AgentDefinition.model_validate(data)
    except ValidationError as e:
        raise Invalid(e) from None
    if c is not None:
        _check_shared(c, d)
    return d


def _check_shared(c, d: AgentDefinition) -> None:
    """What a definition names at the repo's root must be there: its authority preset, its browser profiles."""
    errors = []
    if missing := directives.missing_presets(c.defs.root, d):
        errors.append({"field": "directives", "msg": f"authority/{missing[0]}: there's no such authority preset"})
    for p in c.browser.missing_profiles(d):
        errors.append({"field": "browser.profiles", "msg": f"{p}: there's no such browser profile"})
    if errors:
        raise Invalid(errors)


@owner.get("/catalog")
def get_catalog(request: Request):
    """The settings page's choices. A harness is `ready` when a connected runner can run it, false when none of
    the connected ones can, null when no runner is connected."""
    c = _control(request)
    connected = c.hub.connected()
    can = {h for r in connected for h in c.hub.harnesses(r)}
    harnesses = [{**h, "ready": (h["id"] in can) if connected else None} for h in catalog.HARNESSES]
    return {"harnesses": harnesses, "permissions": catalog.PERMISSIONS, "levels": catalog.LEVELS,
            "networks": catalog.NETWORKS, "templates": say_all(catalog.TEMPLATES), "authority": catalog.AUTHORITY}


@owner.get("/agents")
def list_agents(request: Request):
    return views.agents(_control(request))


def _built(c, body: NewAgent) -> tuple[str, str]:
    """(agent.yaml, persona.md) for a new agent, body.definition on top of its base and body.persona, if given,
    replacing its persona. A copy (copy_from; body.template is then ignored) has the original's settings but not
    its display name, and the original's persona introducing the copy instead (_renamed). Otherwise a template
    (blank by default) gives settings, emoji and a persona addressed to the new agent."""
    if body.copy_from:
        _agent_or_404(c, body.copy_from)
        try:
            src, files = c.defs.load(body.copy_from)
        except DefinitionError as e:
            raise HTTPException(422, f"{body.copy_from}: {e}") from None
        base = src.model_dump(mode="json", exclude_defaults=True)
        base.pop("display_name", None)          # it names the original
        d = _validate({**base, **(body.definition or {}), "name": body.name})
        persona = _renamed(files.get(PERSONA_FILE, ""), [src.display_name, src.name], d.display_name or d.name)
    else:
        tpl = catalog.template(body.template or "blank")
        if tpl is None:
            raise HTTPException(422, f"no template named {body.template}")
        base = {**tpl["definition"], **({"emoji": tpl["emoji"]} if tpl["emoji"] else {})}
        d = _validate({**base, **(body.definition or {}), "name": body.name})
        if "directives" not in (body.definition or {}):
            d = _validate({**d.model_dump(mode="json"), "directives": _template_directives(c, d.permissions)})
        persona = say(tpl["persona"].replace("{who}", d.display_name or d.name))
    _check_shared(c, d)
    return to_yaml(d), body.persona if body.persona is not None else persona


def _template_directives(c, level: str) -> list[str]:
    """What a new agent from a template is told: the owner profile, the standing authority for its level
    (catalog.AUTHORITY; left out if the repo no longer has that preset), its persona and its environment."""
    preset = catalog.AUTHORITY.get(level)
    authority = [AUTHORITY + preset] if preset and preset in c.defs.presets() else []
    return [OWNER, *authority, PERSONA, ENVIRONMENT]


def _renamed(persona: str, old: list[str | None], new: str) -> str:
    """A copied persona that opens the way the templates' do, "You are <old>" (the original's display name or its
    name), opens "You are <new>" instead, so the copy doesn't introduce itself as the original. Any other text is
    the owner's own and stays as it is."""
    for name in filter(None, old):
        m = re.match(rf"You are {re.escape(name)}(?!\w)", persona)
        if m:
            return f"You are {new}" + persona[m.end():]
    return persona


@owner.post("/agents", status_code=201)
async def create_agent(body: NewAgent, request: Request):
    c = _control(request)
    if not NAME_RE.match(body.name):
        raise HTTPException(422, "names are lowercase letters, digits and dashes, starting with a letter or "
                                 "digit, at most 31")
    if c.defs.exists(body.name):        # before building: a taken name is a 409 whatever the rest says
        raise HTTPException(409, f"agent {body.name} already exists")
    agent_yaml, persona = body.agent_yaml, body.persona
    if agent_yaml is None and (body.template or body.copy_from or body.definition is not None):
        try:
            agent_yaml, persona = _built(c, body)
        except Invalid as e:
            return _invalid(e)
    try:
        await asyncio.to_thread(c.defs.create, body.name, agent_yaml, persona)
    except FileExistsError:
        raise HTTPException(409, f"agent {body.name} already exists") from None
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None
    if body.copy_from and body.agent_yaml is None:      # a copy gets its own skills too
        await asyncio.to_thread(c.defs.copy_skills, body.copy_from, body.name,
                                f"Copy {body.copy_from}'s skills to {body.name}")
    await c.hub.emit_agent(body.name)
    return views.agent_detail(c, body.name)


@owner.get("/agents/{name}")
def get_agent(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return views.agent_detail(c, name)


@owner.delete("/agents/{name}")
async def delete_agent(name: str, request: Request, volumes: bool = False):
    """Stop it, have its runner forget it (its volumes too with ?volumes=1), remove agents/<name>/ in one commit,
    and forget everything kept under its name (removal.py)."""
    try:
        out = await removal.delete_agent(_control(request), name, volumes)
    except removal.RemovalError as e:
        raise HTTPException(e.status, str(e)) from None
    return {"ok": True, **out}


@owner.put("/agents/{name}/files/{file}")
async def put_file(name: str, file: str, body: FileText, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    if file not in (AGENT_FILE, PERSONA_FILE):
        raise HTTPException(404, f"{file} isn't a definition file")
    try:
        commit = await asyncio.to_thread(c.defs.write, name, {file: body.text}, f"Edit {name}/{file}")
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None
    await c.hub.emit_agent(name)
    return {"commit": commit}


class Settings(BaseModel):
    definition: dict[str, Any]          # the whole definition, as AgentDefinition's JSON
    persona: str | None = None          # persona.md; None leaves it as it is


def _changed(old: AgentDefinition | None, new: AgentDefinition) -> list[str]:
    if old is None:
        return []
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return [k for k in b if a.get(k) != b[k]]


@owner.put("/agents/{name}/definition")
async def put_definition(name: str, body: Settings, request: Request):
    """The settings page's save: agent.yaml written by to_yaml (comments in a hand-edited file don't survive)
    and persona.md, in one commit. 422 {detail, errors: [{field, msg}]} if it doesn't validate."""
    c = _control(request)
    _agent_or_404(c, name)
    if body.definition.get("name", name) != name:
        raise HTTPException(422, "an agent can't be renamed; copy it instead")
    try:
        d = _validate({**body.definition, "name": name}, c)
    except Invalid as e:
        return _invalid(e)
    try:
        old, _ = c.defs.load(name)
    except (DefinitionError, FileNotFoundError):
        old = None          # an agent.yaml this save repairs
    changed = _changed(old, d)
    out = {AGENT_FILE: to_yaml(d)}
    if body.persona is not None:
        out[PERSONA_FILE] = body.persona
        # The file as it is, whether or not agent.yaml validates: an unchanged persona isn't "changed".
        if body.persona != c.defs.raw(name)[PERSONA_FILE]:
            changed.append("persona")
    message = f"{name} settings" + (": " + ", ".join(changed) if changed else "")
    try:
        await asyncio.to_thread(c.defs.write, name, out, message)
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None
    await c.hub.emit_agent(name)
    return views.agent_detail(c, name)


@owner.get("/agents/{name}/history")
def history(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return c.defs.history(name)


class Restore(BaseModel):
    commit: str


@owner.post("/agents/{name}/restore")
async def restore(name: str, body: Restore, request: Request):
    """Write the agent's files as they were at `commit`, as a new commit."""
    c = _control(request)
    _agent_or_404(c, name)
    try:
        files = await asyncio.to_thread(c.defs.at, name, body.commit)
        await asyncio.to_thread(c.defs.write, name, files, f"Restore {name} to {body.commit[:7]}")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    await c.hub.emit_agent(name)
    return views.agent_detail(c, name)


def _secret_error(e: SecretError) -> JSONResponse:
    return JSONResponse({"detail": str(e), "missing_secrets": e.missing}, status_code=422)


@owner.post("/agents/{name}/start")
async def start_agent(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    if name in c.hub.deleting:
        raise HTTPException(409, f"{name} is being deleted")
    try:
        d, _ = c.defs.load(name)
        directives.check(c.defs.root, d)    # an authority preset it lists must exist, and its browser profiles,
        c.browser.check(d)
        c.secrets.resolve(d)        # and a secret that isn't set refuses the start here, before anything changes
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None
    except SecretError as e:
        return _secret_error(e)
    row = views.ensure_agent(c.db, name)
    c.db.execute("UPDATE agents SET desired = 'running', updated_at = ? WHERE name = ?", (time.time(), name))
    connected = c.hub.connected()
    if d.runner:
        target = d.runner if d.runner in connected else None
        missing = f"runner {d.runner} is not connected"
    else:
        target = row["runner"] if row["runner"] in connected else (connected[0] if connected else None)
        missing = "no runner is connected"
    if target is None:
        await c.hub.emit_agent(name)
        raise HTTPException(503, missing + "; the agent starts when one connects")
    try:
        await c.hub.start_agent(name, target)
    except RunnerError as e:
        raise _runner_error(e) from None
    except DefinitionError as e:        # changed since the check above
        raise HTTPException(422, str(e)) from None
    except SecretError as e:
        return _secret_error(e)
    finally:
        await c.hub.emit_agent(name)
    return views.agent_view(c, name)


@owner.post("/agents/{name}/stop")
async def stop_agent(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    row = views.ensure_agent(c.db, name)
    c.db.execute("UPDATE agents SET desired = 'stopped', updated_at = ? WHERE name = ?", (time.time(), name))
    auth.revoke_agent_token(c.db, name)
    try:
        if row["runner"] in c.hub.connected():
            await c.hub.call(row["runner"], {"type": "agent.stop", "agent": name})
    except RunnerError as e:
        raise _runner_error(e) from None
    finally:
        await c.hub.emit_agent(name)
    return views.agent_view(c, name)


# ---------- secrets ----------
class SecretValue(BaseModel):
    value: Any          # checked by SecretStore.set, whose messages never repeat it (pydantic's would)


@owner.get("/secrets")
def list_secrets(request: Request):
    return views.secrets(_control(request))


@owner.put("/secrets/{name}")
async def put_secret(name: str, body: SecretValue, request: Request):
    """Set or replace a secret's value. Running agents that list it say restart_needed."""
    c = _control(request)
    try:
        c.secrets.set(name, body.value)
    except ValueError as e:         # the name or the value
        raise HTTPException(422, str(e)) from None
    except SecretError as e:        # the master key file is unusable
        raise HTTPException(500, str(e)) from None
    s = views.secret(c, name)
    for agent in s["used_by"]:
        await c.hub.emit_agent(agent)
    return s


@owner.delete("/secrets/{name}")
async def delete_secret(name: str, request: Request):
    """Forget a secret. Agents that list it keep it until they stop; starting them again fails until it's set."""
    c = _control(request)
    if not c.secrets.delete(name):
        raise HTTPException(404, f"no secret named {name}")
    for agent in (views.secret(c, name) or {}).get("used_by", []):
        await c.hub.emit_agent(agent)
    return {"ok": True}


# ---------- directives: the owner profile, authority presets, an agent's assembled directives ----------
async def _emit_users(c, entry: str) -> None:
    """Agent frames for the agents that list `entry`: their restart_needed follows the layer's text."""
    for agent in views.directive_users(c, entry):
        await c.hub.emit_agent(agent)


@owner.get("/owner")
def get_owner(request: Request):
    return views.owner(_control(request))


@owner.put("/owner")
async def put_owner(body: FileText, request: Request):
    """owner.md, committed. Running agents that list `owner` then say restart_needed."""
    c = _control(request)
    await asyncio.to_thread(c.defs.write_owner, body.text)
    await _emit_users(c, OWNER)
    return views.owner(c)


def _preset_or_404(c, name: str) -> dict:
    try:
        return views.preset(c, name)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, f"no authority preset named {name}") from None


@owner.get("/authority")
def list_presets(request: Request):
    c = _control(request)
    return [views.preset(c, n) for n in c.defs.presets()]


@owner.get("/authority/{name}")
def get_preset(name: str, request: Request):
    return _preset_or_404(_control(request), name)


@owner.put("/authority/{name}")
async def put_preset(name: str, body: FileText, request: Request):
    """Create or replace authority/<name>.md. Running agents that use it then say restart_needed."""
    c = _control(request)
    try:
        await asyncio.to_thread(c.defs.write_preset, name, body.text)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    await _emit_users(c, AUTHORITY + name)
    return views.preset(c, name)


@owner.delete("/authority/{name}")
async def delete_preset(name: str, request: Request):
    """409 while an agent's definition lists it: change those first."""
    c = _control(request)
    users = _preset_or_404(c, name)["used_by"]
    if users:
        raise HTTPException(409, f"authority/{name} is used by {', '.join(users)}: give "
                                 f"{'them' if len(users) > 1 else 'it'} another one first")
    try:
        await asyncio.to_thread(c.defs.delete_preset, name)
    except FileNotFoundError:
        raise HTTPException(404, f"no authority preset named {name}") from None
    return {"ok": True}


class DirectivesPreview(BaseModel):
    definition: dict[str, Any] | None = None    # unsaved settings (the whole definition); None: the saved one
    persona: str | None = None                  # unsaved persona.md; None: the saved one


@owner.get("/agents/{name}/directives")
async def get_directives(name: str, request: Request):
    """What the agent is told when it starts now, from its saved definition."""
    c = _control(request)
    _agent_or_404(c, name)
    try:
        d, files = c.defs.load(name)
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None
    return await directives.preview(c, d, files)


@owner.post("/agents/{name}/directives/preview")
async def preview_directives(name: str, body: DirectivesPreview, request: Request):
    """The same for unsaved settings: `definition` and `persona` as the settings page would save them."""
    c = _control(request)
    _agent_or_404(c, name)
    if body.definition is not None:
        if body.definition.get("name", name) != name:
            raise HTTPException(422, "an agent can't be renamed; copy it instead")
        try:
            d = _validate({**body.definition, "name": name})
        except Invalid as e:
            return _invalid(e)
    else:
        try:
            d, _ = c.defs.load(name)
        except DefinitionError as e:
            raise HTTPException(422, str(e)) from None
    files = read_files(c.defs.root, name, d.directives)
    if body.persona is not None:
        files[PERSONA_FILE] = body.persona
    return await directives.preview(c, d, files)
# ---------- skills ----------
class NewSkill(BaseModel):
    name: str
    description: str
    instructions: str = ""


class SkillEdit(BaseModel):
    """SKILL.md's description and instructions (its other frontmatter kept), and/or files by path: text to write,
    or null to remove that file. SKILL.md itself in `files` replaces the whole file instead."""
    description: str | None = None
    instructions: str | None = None
    files: dict[str, str | None] | None = None


def _skill_message(verb: str, name: str, agent: str | None) -> str:
    return f"{verb} {agent}'s skill {name}" if agent else f"{verb} skill {name}"


def _skill_users(c, name: str, agent: str | None) -> set[str]:
    """The agents a change to this skill touches: the owner of an agent's own, else whoever gets the shared one."""
    return {agent} if agent else set(views.skill_users(c).get(name, []))


async def _emit_skill_change(c, name: str, agent: str | None, before: set[str]) -> None:
    """A skill changed: the agents that had it or have it now may need a restart (their files changed)."""
    for a in sorted(before | _skill_users(c, name, agent)):
        await c.hub.emit_agent(a)


async def _create_skill(c, body: NewSkill, agent: str | None) -> JSONResponse:
    if err := skills.name_error(body.name):
        raise HTTPException(422, f"name: {err}")
    if body.name in c.defs.skill_names(agent):
        raise HTTPException(409, f"there's already a skill named {body.name}")
    before = _skill_users(c, body.name, agent)
    files = {skills.SKILL_FILE: skills.render(body.name, body.description, body.instructions)}
    try:
        await asyncio.to_thread(c.defs.write_skill, body.name, files, _skill_message("Create", body.name, agent),
                                agent)
    except skills.SkillError as e:
        raise HTTPException(422, str(e)) from None
    await _emit_skill_change(c, body.name, agent, before)
    return JSONResponse(views.skill_detail(c, body.name, agent), status_code=201)


def _skill_or_404(c, name: str, agent: str | None) -> dict:
    try:
        return views.skill_detail(c, name, agent)
    except FileNotFoundError:
        raise HTTPException(404, f"no skill named {name}") from None


async def _edit_skill(c, name: str, body: SkillEdit, agent: str | None) -> dict:
    try:
        files = c.defs.skill(name, agent)
    except FileNotFoundError:
        raise HTTPException(404, f"no skill named {name}") from None
    except skills.SkillError as e:
        raise HTTPException(422, f"{e} (fix it in the definitions repo)") from None
    new = dict(files)
    for path, text in (body.files or {}).items():
        if text is None:
            new.pop(path, None)
        else:
            new[path] = text
    if body.description is not None or body.instructions is not None:
        if body.files and skills.SKILL_FILE in body.files:
            raise HTTPException(422, "send SKILL.md, or its description and instructions, not both")
        try:
            meta, text = skills.split(new.get(skills.SKILL_FILE, ""))
        except skills.SkillError:
            meta, text = {}, ""
        desc = body.description if body.description is not None else meta.get("description")
        new[skills.SKILL_FILE] = skills.render(name, desc if isinstance(desc, str) else "",
                                               body.instructions if body.instructions is not None else text, meta)
    before = _skill_users(c, name, agent)
    try:
        await asyncio.to_thread(c.defs.write_skill, name, new, _skill_message("Edit", name, agent), agent)
    except skills.SkillError as e:
        raise HTTPException(422, str(e)) from None
    await _emit_skill_change(c, name, agent, before)
    return views.skill_detail(c, name, agent)


async def _delete_skill(c, name: str, agent: str | None) -> dict:
    before = _skill_users(c, name, agent)
    try:
        await asyncio.to_thread(c.defs.delete_skill, name, _skill_message("Delete", name, agent), agent)
    except FileNotFoundError:
        raise HTTPException(404, f"no skill named {name}") from None
    await _emit_skill_change(c, name, agent, before)
    return {"ok": True}


@owner.get("/skills")
def list_skills(request: Request):
    return views.skills_list(_control(request))


@owner.post("/skills", status_code=201)
async def create_skill(body: NewSkill, request: Request):
    return await _create_skill(_control(request), body, None)


@owner.get("/skills/{skill}")
def get_skill(skill: str, request: Request):
    return _skill_or_404(_control(request), skill, None)


@owner.put("/skills/{skill}")
async def edit_skill(skill: str, body: SkillEdit, request: Request):
    return await _edit_skill(_control(request), skill, body, None)


@owner.delete("/skills/{skill}")
async def delete_skill(skill: str, request: Request):
    return await _delete_skill(_control(request), skill, None)


@owner.get("/agents/{name}/skills")
def list_agent_skills(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    try:
        return views.agent_skills(c, name)
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None


@owner.post("/agents/{name}/skills", status_code=201)
async def create_agent_skill(name: str, body: NewSkill, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return await _create_skill(c, body, name)


@owner.get("/agents/{name}/skills/{skill}")
def get_agent_skill(name: str, skill: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return _skill_or_404(c, skill, name)


@owner.put("/agents/{name}/skills/{skill}")
async def edit_agent_skill(name: str, skill: str, body: SkillEdit, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return await _edit_skill(c, skill, body, name)


@owner.delete("/agents/{name}/skills/{skill}")
async def delete_agent_skill(name: str, skill: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    return await _delete_skill(c, skill, name)


# ---------- sessions and turns ----------
def _agent_runner(c, name: str) -> str:
    """The connected runner an agent lives on, else 503."""
    row = c.db.one("SELECT runner FROM agents WHERE name = ?", (name,))
    runner = row and row["runner"]
    if not runner:
        raise HTTPException(503, f"{name} is not running")
    if runner not in c.hub.connected():
        raise HTTPException(503, f"runner {runner} is not connected")
    return runner


def _session_or_404(c, name: str, sid: str) -> dict:
    s = views.session(c.db, name, sid)
    if not s:
        raise HTTPException(404, "no such session")
    return s


@owner.get("/agents/{name}/sessions")
def list_sessions(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    rows = c.db.query("SELECT * FROM sessions WHERE agent = ? ORDER BY updated_at DESC", (name,))
    return [views.session_view(c.db, r) for r in rows]


@owner.post("/agents/{name}/sessions", status_code=201)
async def new_session(name: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    runner = _agent_runner(c, name)
    try:
        result = await c.hub.call(runner, SessionNew(id=_id(), agent=name).dump())
    except RunnerError as e:
        raise _runner_error(e) from None
    sid = result.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise HTTPException(502, "the runner didn't return a session id")
    now = time.time()
    c.db.execute("INSERT OR IGNORE INTO sessions (agent, id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                 (name, sid, now, now))
    await c.hub.emit_session(name, sid)
    return views.session(c.db, name, sid)


@owner.get("/agents/{name}/sessions/{sid}")
def get_session(name: str, sid: str, request: Request):
    return _session_or_404(_control(request), name, sid)


@owner.get("/agents/{name}/sessions/{sid}/events")
def list_events(name: str, sid: str, request: Request, after: int = Query(0, ge=0),
                limit: int = Query(500, ge=1, le=2000)):
    c = _control(request)
    _session_or_404(c, name, sid)
    rows = c.db.query("SELECT * FROM events WHERE agent = ? AND session_id = ? AND id > ? ORDER BY id LIMIT ?",
                      (name, sid, after, limit + 1))
    return {"events": [views.event_view(r) for r in rows[:limit]], "more": len(rows) > limit}


class Prompt(BaseModel):
    text: str = ""
    attachments: list[str] = []         # ids from POST /api/agents/{name}/attachments


def _takes_images(c, name: str) -> bool:
    """The harness said at initialize that it takes image blocks (promptCapabilities.image)."""
    row = c.db.one("SELECT capabilities FROM agents WHERE name = ?", (name,))
    caps = views.loads(row["capabilities"]) if row else None
    return bool(isinstance(caps, dict) and (caps.get("promptCapabilities") or {}).get("image"))


@owner.post("/agents/{name}/sessions/{sid}/prompt", status_code=202)
async def prompt(name: str, sid: str, body: Prompt, request: Request):
    """202 {turn_id} when the turn starts now; 202 {queued} when the session is working (or prompts wait ahead):
    it goes when they're done (prompts.py)."""
    c = _control(request)
    _session_or_404(c, name, sid)
    if not body.text.strip() and not body.attachments:
        raise HTTPException(422, "say something")
    rows = []
    if body.attachments:
        # Files go into the agent's computer before the prompt is recorded or queued: if that fails, nothing was
        # sent, and the owner can send it again as it is.
        try:
            rows = c.attachments.pending(name, body.attachments)
            await c.attachments.place(name, rows, _takes_images(c, name))
        except (AttachmentError, ComputerError) as e:
            return JSONResponse({"detail": str(e), "sent": False}, status_code=e.status)
    try:
        d = await c.prompts.submit(name, sid, body.text, attachments=rows)
    except DeliveryError as e:
        raise HTTPException(e.status, str(e)) from None
    return api_triggers.delivery_response(d)


@owner.post("/agents/{name}/sessions/{sid}/cancel")
async def cancel(name: str, sid: str, request: Request):
    c = _control(request)
    s = _session_or_404(c, name, sid)
    if not s["turn_id"]:
        raise HTTPException(409, "no turn is running")
    runner = _agent_runner(c, name)
    try:
        await c.hub.call(runner, SessionCancel(id=_id(), agent=name, session_id=sid).dump())
    except RunnerError as e:
        raise _runner_error(e) from None
    return {"ok": True}


# ---------- inbox ----------
@owner.get("/inbox")
def list_inbox(request: Request, status: Literal["pending", "all"] = "pending"):
    c = _control(request)
    if status == "pending":
        return views.pending_inbox(c.db)
    rows = c.db.query("SELECT * FROM inbox ORDER BY created_at DESC, rowid DESC LIMIT 200")
    return [views.inbox_view(r) for r in rows]


def _item_or_404(c, item_id: str) -> dict:
    item = views.inbox_item(c.db, item_id)
    if not item:
        raise HTTPException(404, "no such inbox item")
    return item


@owner.get("/inbox/{item_id}")
def get_item(item_id: str, request: Request):
    return _item_or_404(_control(request), item_id)


class Answer(BaseModel):
    option_id: str | None = None
    text: str | None = None     # a question's answer in the owner's own words (valet.owner)


@owner.post("/inbox/{item_id}/answer")
async def answer(item_id: str, body: Answer, request: Request):
    c = _control(request)
    item = _item_or_404(c, item_id)
    if item["status"] != "pending":
        raise HTTPException(409, f"this request is already {item['status']}")
    if item["kind"] in DURABLE:         # valet.owner's message or question
        try:
            await owner_server.answer(c, c.db.one("SELECT * FROM inbox WHERE id = ?", (item_id,)), body.option_id,
                                      body.text)
        except owner_server.AnswerError as e:
            raise HTTPException(e.status, str(e)) from None
        return views.inbox_item(c.db, item_id)
    if body.option_id is None:
        raise HTTPException(422, "option_id: pick one of the request's options")
    if body.option_id not in {o.get("optionId") for o in item["options"]}:
        raise HTTPException(422, "not one of this request's options")
    row = c.db.one("SELECT * FROM inbox WHERE id = ?", (item_id,))
    outcome = {"outcome": "selected", "optionId": body.option_id}
    if row["runner"] is None:       # the gateway's: the call waiting on it is in this process
        if c.hub.waiting(item_id) and await c.hub.record_answer(row, outcome):
            await c.hub.emit_agent(item["agent"])
            return views.inbox_item(c.db, item_id)
        if await c.hub.expire_item(row):
            await c.hub.emit_agent(item["agent"])
        item = views.inbox_item(c.db, item_id)
        if item["status"] == "answered":
            return item
        return JSONResponse({"detail": "the agent is no longer waiting for this answer", "item": item},
                            status_code=410)
    try:
        await c.hub.call(row["runner"], PermissionAnswer(id=_id(), agent=item["agent"], request_id=item_id,
                                                         outcome=outcome).dump())
    except RunnerError as e:
        if str(e) != "unknown request":
            # Unreachable, or the link dropped mid-call: the runner still holds the request (or got the answer,
            # and its resent permission.closed records it). Leave it pending; the owner can retry.
            raise _runner_error(e) from None
        if await c.hub.expire_item(row):
            await c.hub.emit_agent(item["agent"])
        item = views.inbox_item(c.db, item_id)
        if item["status"] == "answered":        # an earlier attempt got through; its reply was lost
            return item
        return JSONResponse({"detail": "the agent is no longer waiting for this answer", "item": item},
                            status_code=410)
    if await c.hub.record_answer(row, outcome):
        await c.hub.emit_agent(item["agent"])
    return views.inbox_item(c.db, item_id)


# ---------- the MCP gateway ----------
@owner.get("/agents/{name}/tools")
async def agent_tools(name: str, request: Request,
                      permissions: Literal["locked", "ask", "trusted", "unrestricted"] | None = None):
    """What the agent gets through the gateway: per gateway server, the tools and what a call does.
    `permissions`: what a call would do at that level instead of the saved one (the settings page shows a level
    change before it's saved)."""
    c = _control(request)
    _agent_or_404(c, name)
    try:
        return await c.gateway.agent_tools(name, permissions)
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from None


@owner.get("/gateway/servers")
def gateway_servers(request: Request):
    return _control(request).gateway.servers()


@owner.get("/gateway/registry")
def get_registry(request: Request):
    c = _control(request)
    return {"text": c.defs.registry_text(), "commit": c.defs.head()}


@owner.put("/gateway/registry")
async def put_registry(body: FileText, request: Request):
    """mcp.yaml, validated and committed. 422 if it doesn't validate (nothing written)."""
    c = _control(request)
    try:
        commit = await asyncio.to_thread(c.defs.write_registry, body.text)
    except RegistryError as e:
        raise HTTPException(422, str(e)) from None
    return {"commit": commit, **c.gateway.servers()}


@owner.get("/gateway/calls")
def gateway_calls(request: Request, agent: str | None = None, server: str | None = None,
                  before: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)):
    """The audit log, newest first: `before` (an id) pages back."""
    where, params = [], []
    for col, val in (("agent", agent), ("server", server)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if before is not None:
        where.append("id < ?")
        params.append(before)
    sql = "SELECT * FROM tool_calls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    rows = _control(request).db.query(sql, (*params, limit + 1))
    return {"calls": [views.tool_call_view(r) for r in rows[:limit]], "more": len(rows) > limit}


router.include_router(owner)

"""`valet-control [serve]`, `valet-control set-password`, `valet-control add-runner <name>`,
`valet-control revoke-runner <name>`, `valet-control set-secret <NAME>`, `valet-control delete-secret <NAME>`. Config from the environment (ControlConfig.from_env)."""
import argparse
import copy
import getpass
import logging
import re
import sys

import uvicorn

from valet.shared.config import ControlConfig

from . import auth
from .app import create_app, open_database
from .secrets import SecretError, SecretStore


class RedactTokens(logging.Filter):
    """uvicorn logs request paths, and native clients may open /ws/client?token=vown_..., webhook senders
    /hooks/<agent>/<name>?key=vhk_..., agents fetch /mcp/agents/<agent>/shared/<ticket> (the ticket is the
    credential): keep those out of logs."""
    PATTERN = re.compile(r"(?<![A-Za-z0-9_])((?:token|key)=)[^&\s\"]+")
    TICKET = re.compile(r"(/mcp/agents/[^/\s]+/shared/)[^/?\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.TICKET.sub(r"\1***", self.PATTERN.sub(r"\1***", a)) if isinstance(a, str) else a
                                for a in record.args)
        return True


def log_config() -> dict:
    """uvicorn's default logging, with RedactTokens on every handler."""
    cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    cfg["filters"] = {"redact": {"()": RedactTokens}}
    for handler in cfg["handlers"].values():
        handler["filters"] = ["redact"]
    return cfg


def set_password(cfg: ControlConfig, password: str) -> None:
    """Store the owner password (argon2) and rotate the session secret, which signs everyone out.
    Raises ValueError under 12 characters."""
    db = open_database(cfg)
    try:
        auth.set_password(db, password)
    finally:
        db.close()


def add_runner(cfg: ControlConfig, name: str) -> str:
    """Register a runner (or replace its token) and return the plaintext token, `vrun_` + secret. Only the
    sha256 of the token is stored, so this is the one time it's visible."""
    db = open_database(cfg)
    try:
        return auth.add_runner(db, name)
    finally:
        db.close()


def revoke_runner(cfg: ControlConfig, name: str) -> None:
    """Revoke a runner's token; a connected runner is disconnected (the server rechecks tokens every few
    seconds). KeyError if there's no such runner."""
    db = open_database(cfg)
    try:
        if not db.execute("UPDATE runners SET revoked = 1 WHERE name = ?", (name,)).rowcount:
            raise KeyError(name)
    finally:
        db.close()


def set_secret(cfg: ControlConfig, name: str, value: str) -> None:
    """Store a secret (encrypted; control/secrets.py). ValueError for a bad name or value, SecretError for an
    unusable key file. A running server sees it at once; its open pages on their next fetch."""
    db = open_database(cfg)
    try:
        SecretStore(db, cfg).set(name, value)
    finally:
        db.close()


def delete_secret(cfg: ControlConfig, name: str) -> None:
    """KeyError if there's no such secret."""
    db = open_database(cfg)
    try:
        if not SecretStore(db, cfg).delete(name):
            raise KeyError(name)
    finally:
        db.close()


def read_value(name: str) -> str:
    """The secret's value from the terminal (not echoed), or from stdin when it's a pipe or a file (one trailing
    newline dropped). Never from argv, which other users and shell history can see."""
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name} (not shown): ")
    return sys.stdin.read().removesuffix("\n").removesuffix("\r")


def main(argv: list[str] | None = None) -> None:
    """Parse argv (default sys.argv[1:]); `serve` (the default) runs uvicorn on cfg.host:cfg.port.
    set-password reads the password with getpass (twice), never from argv."""
    parser = argparse.ArgumentParser(prog="valet-control", description="Valet control plane")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the control plane (default)")
    sub.add_parser("set-password", help="set the owner password (signs every client out)")
    p = sub.add_parser("add-runner", help="register a runner, or give it a new token, and print the token")
    p.add_argument("name")
    p = sub.add_parser("revoke-runner", help="revoke a runner's token")
    p.add_argument("name")
    p = sub.add_parser("set-secret", help="set a secret's value, read from the terminal or stdin (never argv)")
    p.add_argument("name", help="an env var name, e.g. GITHUB_TOKEN")
    p = sub.add_parser("delete-secret", help="delete a secret")
    p.add_argument("name")
    args = parser.parse_args(argv)
    try:
        cfg = ControlConfig.from_env()
    except ValueError as e:
        sys.exit(str(e))

    if args.command == "set-password":
        pw = getpass.getpass("New owner password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("passwords don't match")
        try:
            set_password(cfg, pw)
        except ValueError as e:
            sys.exit(str(e))
        print("Password set. Every signed-in client has to sign in again.")
    elif args.command == "add-runner":
        try:
            token = add_runner(cfg, args.name)
        except ValueError as e:
            sys.exit(str(e))
        print(f"Runner {args.name} registered. Its token (shown once):\n\n  {token}\n\n"
              "Start it with VALET_RUNNER_TOKEN set to that value.")
    elif args.command == "revoke-runner":
        try:
            revoke_runner(cfg, args.name)
        except KeyError:
            sys.exit(f"no runner named {args.name}")
        print(f"Runner {args.name} revoked.")
    elif args.command == "set-secret":
        try:
            set_secret(cfg, args.name, read_value(args.name))
        except (ValueError, SecretError) as e:
            sys.exit(str(e))
        print(f"Secret {args.name} set. Agents that list it get it when they next start.")
    elif args.command == "delete-secret":
        try:
            delete_secret(cfg, args.name)
        except KeyError:
            sys.exit(f"no secret named {args.name}")
        print(f"Secret {args.name} deleted.")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        # httpx logs every request's URL at INFO: the MCP gateway's requests to a remote server would put a
        # credential that mcp.yaml puts in a server's url (a ${secret:...} in a query string) in the log, and the
        # owner's webhook URL carries its own (notify.py). The mcp SDK's client is httpx2, which logs as "httpx2".
        for name in ("httpx", "httpx2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=False, log_config=log_config())


if __name__ == "__main__":
    main()

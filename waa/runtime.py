"""Keep smoke sessions away from the developer's live agent sessions."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import socket
import tomllib
from urllib.parse import urlsplit


def network_env(state: Path) -> dict[str, str]:
    return {
        **os.environ,
        "REASON_NETWORK_HOME": str(state.resolve()),
        "REASON_NETWORK_NO_SERVICE": "1",
        "REASON_NETWORK_NO_UPDATE": "1",
        "REASON_NETWORK_NO_TRAY": "1",
        "DO_NOT_TRACK": "1",
    }


def isolated_env(state: Path, *, machine: str) -> dict[str, str]:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = state / "empty-claude"
    config.mkdir(exist_ok=True, mode=0o700)
    env = {
        key: value
        for key, value in network_env(state).items()
        if not key.startswith(("CLAUDE", "CODEX_", "CURSOR_", "GEMINI_", "OPENCODE_", "REASON_NETWORK_"))
    }
    env.update({
        "REASON_NETWORK_HOME": str(state.resolve()),
        "REASON_NETWORK_MACHINE": machine,
        "REASON_NETWORK_NO_SERVICE": "1",
        "REASON_NETWORK_NO_UPDATE": "1",
        "REASON_NETWORK_NO_TRAY": "1",
        "REASON_NETWORK_NO_MDNS": "1",
        "CLAUDE_CONFIG_DIR": str(config.resolve()),
    })
    return env


def relay_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid relay URL.") from None
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Relay URL must contain only a scheme, host, and optional port.")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Use HTTPS for the private relay; HTTP is allowed only on loopback.")
    if host.endswith((".reasonmachines.com", ".iroh.link")) or host in {"reasonmachines.com", "iroh.link"}:
        raise ValueError("Use your team's own relay, rather than an upstream hosted relay.")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}/"


def ticket_relay(ticket: str) -> str:
    """Read only the pinned rn1 routing prefix; the engine validates membership."""
    try:
        encoded = ticket.rsplit("#", 1)[-1]
        prefix = next(prefix for prefix in ("rn1-", "tincan1") if encoded.startswith(prefix))
        encoded = encoded[len(prefix):]
        if len(encoded) > 16384:
            raise ValueError
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        at = 32
        texts = []
        for _ in range(2):
            length = raw[at]
            at += 1
            if length == 255 or at + length > len(raw):
                raise ValueError
            texts.append(raw[at:at + length].decode("utf-8"))
            at += length
        return relay_url(texts[1])
    except (ValueError, IndexError, StopIteration, UnicodeError):
        raise ValueError("Invite must name your own explicit relay; ticket was not used.") from None


def prepare_network(state: Path, relay: str, arguments: list[str]) -> list[str]:
    selected = relay_url(relay)
    if not arguments or arguments[0] in {"setup", "upgrade", "relay", "proof"}:
        raise ValueError("Use explicit network commands; global setup, upgrades, and relay switching are not exposed.")
    command = list(arguments)
    if command[0] == "init":
        if "--relay" in command:
            at = command.index("--relay")
            if at + 1 == len(command) or relay_url(command[at + 1]) != selected:
                raise ValueError("Init relay must match the wrapper's --relay.")
        else:
            command.extend(["--relay", selected])
    if command[0] == "join":
        if len(command) < 2 or command[1].startswith("-") or ticket_relay(command[1]) != selected:
            raise ValueError("Put the invite first after join; it must name the selected private relay.")
    if command[0] in {"init", "join"} and "--no-setup" not in command:
        command.append("--no-setup")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    pin = state / "waa-relay.json"
    if pin.exists() and json.loads(pin.read_text())["relay"] != selected:
        raise ValueError("This state belongs to another relay; choose a fresh --state directory.")
    config = state / "config.toml"
    required = {"share_unmanaged": False, "lan": False, "tray": False, "reason": False, "auto_update": False}
    if config.exists():
        current = tomllib.loads(config.read_text())
        if any(current.get(key) is not value for key, value in required.items()):
            raise ValueError("State config must disable unmanaged discovery, LAN discovery, tray, Reason cloud, and auto updates.")
    else:
        config.write_text("".join(f"{key} = false\n" for key in required))
    pin.write_text(json.dumps({"relay": selected}) + "\n")
    return command


@contextmanager
def private_network_env(state: Path, relay: str):
    # The engine refreshes its default relay list before joining a team. Block
    # those HTTP requests while permitting the operator's explicitly chosen host.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as denied:
        denied.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{denied.getsockname()[1]}"
        env = network_env(state)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        host = urlsplit(relay_url(relay)).hostname
        env["NO_PROXY"] = env["no_proxy"] = f"localhost,127.0.0.1,::1,{host}"
        env["REASON_NETWORK_NO_MDNS"] = "1"
        yield env

#!/usr/bin/env python3
"""
Bridge between the host virtual node and MQTT.

The node binary speaks the protocol on stdout; this owns the radio. Splitting them keeps the
node dependency-free and lets it be driven from a file for deterministic tests, while MQTT —
which is genuinely a network concern — stays in Python where a real client library exists.

Each role gets its **own** MQTT connection, because a Last Will is per-connection and presence
must be per-node. One shared connection could only ever carry one will, so a crash would leave
three of the four nodes looking online forever — exactly the failure MQTT LWT exists to prevent.

Usage:
    python3 tools/virtual-nodes.py --hz 5 --host 127.0.0.1 --port 1883

    The production broker has ``allow_anonymous false``, so against the Pi this needs
    credentials. Each role is a separate broker user, and the file the installer
    writes is already in the right shape:

        python3 tools/virtual-nodes.py --host 192.168.137.114 \\
            --credentials /etc/aqua/mqtt-node-credentials

    ``--username``/``--password`` (or ``AQUA_MQTT_USERNAME``/``AQUA_MQTT_PASSWORD``)
    set one pair for every role, which is enough because the broker applies no
    per-user topic ACL -- but it makes all four nodes share one identity, so presence
    and the last will stop being per-node.
    """

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

try:
    import aiomqtt
except ImportError:  # pragma: no cover - developer feedback
    sys.exit(
        "aiomqtt is not installed in this interpreter; pip install aiomqtt"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BINARY = REPO_ROOT / "firmware" / "virtual_node" / "virtual_node"

ROLES = ("od_sensor", "aerator", "mixer", "pump")
NODE_IDS = {
    "od_sensor": "a0000000-0000-0000-0000-000000000001",
    "aerator": "b0000000-0000-0000-0000-000000000002",
    "mixer": "c0000000-0000-0000-0000-000000000003",
    "pump": "d0000000-0000-0000-0000-000000000004",
}


def read_credentials(path: Path) -> dict[str, tuple[str, str]]:
    """
    Parse a ``role=secret`` file, as `/etc/aqua/mqtt-node-credentials` is written.

    That file is the installer's record of the credentials it generated; reusing it
    here means the harness cannot be tested against credentials the broker does not
    actually have, which is a mistake that otherwise only shows up as
    `[code:135] Not authorized` and sends you looking at the client.
    """
    creds: dict[str, tuple[str, str]] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        role, _, secret = line.partition("=")
        creds[role.strip()] = (role.strip(), secret.strip())
    return creds


class Bridge:
    def __init__(
        self,
        node: subprocess.Popen[bytes],
        host: str,
        port: int,
        verbose: bool,
        auth: dict[str, tuple[str | None, str | None]] | None = None,
        roles: list[str] | None = None,
    ) -> None:
        self.node = node
        self.host = host
        self.port = port
        self.verbose = verbose
        self.auth = auth or {}
        # Which roles to stand in for. Simulating a role that also has real hardware
        # attached is not harmful to the broker, but it does write simulated rows into
        # that node's archive file for the day, where they are indistinguishable from
        # real measurements. So the default is everything and the operator narrows it.
        self.roles = list(roles) if roles else list(ROLES)
        self.published = 0
        self.commands = 0
        self.stopping = asyncio.Event()

    def log(self, message: str) -> None:
        print(message, flush=True)

    # -- node stdout -> publish -------------------------------------------

    def read_node_lines(
        self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop
    ) -> None:
        """Thread body: the node's stdout is a blocking pipe, not an asyncio stream."""
        assert self.node.stdout is not None
        for raw in iter(self.node.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(self.stopping.set)

    async def publish_loop(self, queue: asyncio.Queue[str]) -> None:
        """
        Four roles, two connections each.

        **Publish and subscribe need separate clients.** An `aiomqtt.Client` carries a single
        session and a single reader loop; calling `publish()` from one task while another task
        iterates `client.messages` interleaves their framing and corrupts the payload — which
        presents as the gateway failing to JSON-decode valid telemetry, a very misleading symptom
        for what is really a client-lifetime bug.

        The will goes on the **publish** client, because that is the connection whose death
        should mark the node offline. The subscribe client deliberately has none: if it dropped
        and carried a will, a transient command-subscription failure would publish "offline" for
        a node that is still happily streaming.
        """
        publishers: dict[str, aiomqtt.Client] = {}
        subscribers: dict[str, aiomqtt.Client] = {}
        tasks: list[asyncio.Task] = []

        try:
            for role in self.roles:
                username, password = self.auth.get(role, (None, None))
                publishers[role] = aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    identifier=f"virtual-{role}-pub",
                    clean_session=True,
                    username=username,
                    password=password,
                    will=aiomqtt.Will(
                        topic=f"aqua/presence/{NODE_IDS[role]}",
                        payload=json.dumps(
                            {"online": False, "role": role, "reason": "will"}
                        ).encode(),
                        qos=1,
                        retain=True,
                    ),
                )
                subscribers[role] = aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    identifier=f"virtual-{role}-sub",
                    clean_session=True,
                    username=username,
                    password=password,
                )
                await publishers[role].__aenter__()
                await subscribers[role].__aenter__()
                self.log(
                    f"  {role:10s} pub+sub connected as {username or 'anonymous'}, "
                    f"will -> aqua/presence/{NODE_IDS[role]}"
                )

            for role in self.roles:
                tasks.append(
                    asyncio.create_task(
                        self.command_loop(role, subscribers[role])
                    )
                )

            while not self.stopping.is_set():
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                await self.route(line, publishers)
        finally:
            for task in tasks:
                task.cancel()
            for role in self.roles:
                for client in (subscribers.get(role), publishers.get(role)):
                    if client is None:
                        continue
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - shutdown path
                        pass

    async def route(
        self, line: str, clients: dict[str, aiomqtt.Client]
    ) -> None:
        """`KIND<TAB>topic<TAB>payload` -> the right node's connection."""
        parts = line.split("\t", 2)
        if len(parts) != 3:
            return
        kind, topic, payload = parts
        role = topic.rsplit("/", 1)[-1] if "/" in topic else ""
        if topic.startswith("aqua/presence/"):
            node_id = topic.rsplit("/", 1)[-1]
            role = next(
                (r for r, nid in NODE_IDS.items() if nid == node_id), ""
            )
        client = clients.get(role)
        if client is None:
            return

        retain = kind == "PRESENCE"
        qos = 1
        try:
            await client.publish(
                topic, payload.encode(), qos=qos, retain=retain
            )
            self.published += 1
            if self.verbose and kind != "TEL":
                self.log(f"  pub {kind:8s} {topic} {payload[:120]}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  publish failed on {topic}: {exc}")

    # -- subscribe -> node stdin ------------------------------------------

    async def command_loop(self, role: str, client: aiomqtt.Client) -> None:
        """
        Commands arrive as `{action, target, cmd_id}`; the node gets them on stdin.

        The gateway publishes to `aqua/cmd/<role>` with its own schema (see
        `POST /api/commands`), so the action and target are forwarded as-is and the node decides
        what it understands.
        """
        assert self.node.stdin is not None
        while not self.stopping.is_set():
            try:
                await client.subscribe(f"aqua/cmd/{role}", qos=1)
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)

        try:
            async for message in client.messages:
                topic_role = str(message.topic).rsplit("/", 1)[-1]
                if topic_role != role:
                    continue
                try:
                    body = json.loads(message.payload.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                action = body.get("action", "")
                target = body.get("target") or {}
                cmd_id = body.get("cmd_id") or body.get("id") or ""
                if not action:
                    continue

                forwarded = {"action": action, "cmd_id": cmd_id, **target}
                line = f"CMD\t{role}\t{json.dumps(forwarded)}\n"
                os.write(self.node.stdin.fileno(), line.encode())
                self.commands += 1
                self.log(
                    f"  cmd {role:10s} {action} {target if target else ''}"
                )
        except asyncio.CancelledError:
            return


async def main_async(args: argparse.Namespace) -> int:
    if not NODE_BINARY.exists():
        sys.exit(
            f"{NODE_BINARY} not found — build it first:\n"
            f"  firmware/virtual_node/build.sh"
        )

    node_args = [str(NODE_BINARY), "--hz", str(args.hz)]
    if args.idle:
        node_args.append("--idle")
    if args.seconds:
        node_args += ["--seconds", str(args.seconds)]

    print(
        f"starting {NODE_BINARY.name} ({' '.join(node_args[1:])})", flush=True
    )
    node = subprocess.Popen(
        node_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # the node's diagnostics go straight to the terminal
    )

    auth: dict[str, tuple[str | None, str | None]] = {}
    if args.credentials:
        auth.update(read_credentials(Path(args.credentials)))
    for role in args.role_list:
        if role not in auth and args.username:
            auth[role] = (args.username, args.password)
    unauthenticated = [role for role in args.role_list if role not in auth]
    if unauthenticated:
        print(
            "warning: no credential for "
            f"{', '.join(unauthenticated)}; they will connect anonymously, which a "
            "broker with allow_anonymous false will refuse",
            flush=True,
        )

    bridge = Bridge(
        node, args.host, args.port, args.verbose, auth, roles=args.role_list
    )
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10_000)
    loop = asyncio.get_running_loop()

    import threading

    threading.Thread(
        target=bridge.read_node_lines, args=(queue, loop), daemon=True
    ).start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bridge.stopping.set)
        except NotImplementedError:  # pragma: no cover
            pass

    try:
        await bridge.publish_loop(queue)
    finally:
        try:
            node.terminate()
            node.wait(timeout=5)
        except Exception:  # noqa: BLE001
            node.kill()

    print(
        f"bridge stopped: {bridge.published} messages published, {bridge.commands} commands relayed",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--hz", type=float, default=5.0)
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="0 = run until stopped"
    )
    parser.add_argument(
        "--idle",
        action="store_true",
        help="start with the sensor not monitoring",
    )
    parser.add_argument(
        "--credentials",
        help="file of 'role=password' lines, as written to "
        "/etc/aqua/mqtt-node-credentials on the station",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("AQUA_MQTT_USERNAME"),
        help="one broker user for every role; prefer --credentials",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("AQUA_MQTT_PASSWORD"),
        help="password for --username; prefer --credentials or the env var, "
        "which stay out of the process list",
    )
    parser.add_argument(
        "--roles",
        default=",".join(ROLES),
        help="comma-separated subset to simulate, e.g. od_sensor,mixer,pump. "
        "Leave out any role that has real hardware attached, or its archive file "
        "will hold simulated rows alongside real ones",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    args.role_list = [r.strip() for r in args.roles.split(",") if r.strip()]
    unknown = [r for r in args.role_list if r not in ROLES]
    if unknown:
        parser.error(
            f"unknown role(s): {', '.join(unknown)}; known: {', '.join(ROLES)}"
        )
    if not args.role_list:
        parser.error("--roles selected nothing")
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

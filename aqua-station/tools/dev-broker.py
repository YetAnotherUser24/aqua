#!/usr/bin/env python3
"""
Development MQTT broker.

Runs `amqtt` (a pure-Python MQTT 3.1.1 broker) on loopback for local work. It exists so the
gateway and the virtual nodes can be exercised end to end without installing mosquitto or
touching the Pi.

**Not for deployment.** The Pi runs mosquitto with the config in `deploy/mosquitto/`; this is
plaintext loopback with no authentication, and it allows anonymous connections because every
client here is already on the same machine.

One deviation from stock amqtt is deliberate and is applied at startup: `patch_retained_replay`
stops the broker handing every retained topic to every new connection. See that function for
the defect and the reasoning. mosquitto does not have the bug, so the patch is confined here.

Usage:
    python3 tools/dev-broker.py --port 1883
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

try:
    from amqtt.broker import Broker
except ImportError:  # pragma: no cover - developer feedback
    sys.exit(
        "amqtt is not installed in this interpreter.\n"
        "  pip install amqtt\n"
        "or run the gateway's own venv."
    )


def broker_config(port: int) -> dict:
    """
    amqtt 0.12 validates this dict against a `BrokerConfig` dataclass and rejects unknown keys,
    so it carries listeners and topic-check only. Log level is set through `logging`, not here.
    """
    return {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"127.0.0.1:{port}",
                # Anonymous is acceptable here strictly because the bind is loopback and every
                # client is a process on this machine. It would not be acceptable on the Pi.
                "max_connections": 50,
            }
        },
        "sys_interval": 10,
        "topic-check": {"enabled": False},
    }


def patch_retained_replay() -> None:
    """
    Stop amqtt replaying *every* retained message to *every* new connection.

    `Broker._handle_client_session` walks the broker-wide subscription registry and calls
    `_publish_retained_messages_for_subscription` once per filter, without ever checking that
    the connecting session holds that subscription. The comment directly above the loop even
    says "if this is not a new session, there are subscriptions associated with them" -- the
    condition is described and then never tested.

    The effect here was that a node subscribing to `aqua/cmd/<role>` was handed every retained
    topic in the broker, so `aqua/presence/*` and `aqua/time` arrived on its *command*
    subscription. The firmware routes by topic so nothing broke, but it made a non-existent
    malformed command look real and cost a bench cycle to chase.

    The fix is the missing condition rather than a rewrite: replay only when the session really
    holds the filter. That keeps the case the comment describes -- a returning session with
    persisted subscriptions -- and drops the ones it does not have. The subscribe-time path in
    `_handle_subscription` is already correct and already filtered, so retained delivery itself
    is unaffected.

    Upstream is amqtt 0.12.1, which is the latest release, so there is nothing to upgrade to.
    This is deliberately confined to the development broker: the Pi runs mosquitto, which does
    not have the defect.
    """
    original = Broker._publish_retained_messages_for_subscription

    async def guarded(self, subscription, session):
        topic_filter = subscription[0]
        holders = self._subscriptions.get(topic_filter, [])
        if not any(
            holder.client_id == session.client_id for holder, _ in holders
        ):
            return
        await original(self, subscription, session)

    Broker._publish_retained_messages_for_subscription = guarded


async def run(port: int, verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    patch_retained_replay()
    broker = Broker(broker_config(port))
    await broker.start()
    print(f"dev broker listening on mqtt://127.0.0.1:{port}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - platform dependent
            pass

    try:
        await stop.wait()
    finally:
        await broker.shutdown()
        print("dev broker stopped", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.port, args.verbose))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

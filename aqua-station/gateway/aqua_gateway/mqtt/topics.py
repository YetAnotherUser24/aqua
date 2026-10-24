"""
Topic names and builders.

Centralised so a rename is one edit, and so firmware patches and gateway code
cannot drift apart on string literals.
"""

from __future__ import annotations

#: Telemetry, one topic per role. Payload is the v2 envelope.
TELEMETRY = "aqua/tlm/{role}"

#: Commands to a role. QoS 1, not retained.
COMMAND = "aqua/cmd/{role}"

#: Command acknowledgement from a node.
ACKNOWLEDGEMENT = "aqua/ack/{role}"

#: Presence, one topic per node. QoS 1, RETAINED — so a gateway that restarts
#: learns current state immediately instead of waiting for the next heartbeat.
#: Populated by the node on connect and by the broker's Last Will on disconnect.
PRESENCE = "aqua/presence/{node_id}"

#: Time broadcast from the gateway. Retained so a node joining later gets it.
TIME = "aqua/time"

#: Firmware-originated events (faults, dose completion, watchdog resets).
EVENTS = "aqua/event/{role}"

# Wildcard subscriptions for the gateway.
TELEMETRY_ALL = "aqua/tlm/+"
ACK_ALL = "aqua/ack/+"
PRESENCE_ALL = "aqua/presence/+"
EVENTS_ALL = "aqua/event/+"


def telemetry(role: str) -> str:
    return TELEMETRY.format(role=role)


def command(role: str) -> str:
    return COMMAND.format(role=role)


def acknowledgement(role: str) -> str:
    return ACKNOWLEDGEMENT.format(role=role)


def presence(node_id: str) -> str:
    return PRESENCE.format(node_id=node_id)


def events(role: str) -> str:
    return EVENTS.format(role=role)


def role_from_telemetry_topic(topic: str) -> str | None:
    """
    Extract the role from ``aqua/tlm/<role>``.

    Returning ``None`` for anything unexpected is deliberate: an unknown topic
    becomes a ``protocol_error`` event rather than a silent drop.
    """
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "aqua" and parts[1] == "tlm":
        return parts[2]
    return None


def role_from_ack_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "aqua" and parts[1] == "ack":
        return parts[2]
    return None


def node_id_from_presence_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "aqua" and parts[1] == "presence":
        return parts[2]
    return None

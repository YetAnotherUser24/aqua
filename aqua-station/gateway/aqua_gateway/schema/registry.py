"""
Schema registry — loads ``roles.yaml`` and resolves per-role columns.

The four node roles share almost no fields, so the schema is declared as data
rather than encoded in handlers. Adding a node type is a YAML edit; adding a
field is a YAML edit. Neither requires touching gateway code, which is what keeps
heterogeneity cheap.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("roles.yaml")


class FieldType(str, Enum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"


@dataclass(frozen=True, slots=True)
class Field:
    """One telemetry column."""

    name: str
    type: FieldType
    unit: str | None = None
    precision: int | None = None
    values: tuple[str, ...] = ()
    legacy_source: str | None = None
    legacy_divisor: float | None = None
    legacy_map: Mapping[int, str] | None = None
    authoritative: bool = True
    note: str = ""

    def coerce(self, value: Any) -> Any:
        """
        Coerce a wire value to its declared type.

        Returns ``None`` for absent/empty values rather than fabricating a zero —
        a missing reading and a reading of zero are different claims, and the
        previous schema conflated them in several places.
        """
        if value is None or value == "":
            return None

        if self.type is FieldType.BOOL:
            if isinstance(value, bool):
                return value
            return bool(int(value)) if str(value).strip() not in ("", "0", "false", "False") else False

        if self.type is FieldType.INT:
            return int(value)

        if self.type is FieldType.FLOAT:
            number = float(value)
            return round(number, self.precision) if self.precision is not None else number

        if self.type is FieldType.ENUM:
            text = str(value)
            if text not in self.values:
                raise ValueError(f"{self.name}: {text!r} not in {list(self.values)}")
            return text

        return value


@dataclass(frozen=True, slots=True)
class RoleSchema:
    """Everything the gateway knows about one node role."""

    role: str
    display_name: str
    node_id: str
    topic: str
    fields: tuple[Field, ...]
    authoritative_for: tuple[str, ...] = ()
    expected_rate_hz: Mapping[str, float] | None = None

    @property
    def field_names(self) -> frozenset[str]:
        """
        Membership test only — **never iterate this to order output.**

        A frozenset has no meaningful iteration order, and Python randomises string hashing per
        process, so iterating it produces a different order on every restart. `csv_header` and
        `fields` are the ordered views; this exists for ``name in schema.field_names`` checks.
        """
        return frozenset(f.name for f in self.fields)

    @property
    def csv_header(self) -> list[str]:
        """Column order for the archive file. Registry-driven, so schema changes are local."""
        return ["t_dev_ms", "t_src", "seq", *(f.name for f in self.fields)]

    def project(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        """Coerce a wire sample into the registry's columns. Missing fields become None."""
        return {f.name: f.coerce(sample.get(f.name)) for f in self.fields}


class SchemaRegistry:
    """Loaded, validated view of ``roles.yaml``."""

    def __init__(self, document: Mapping[str, Any], source: Path) -> None:
        self.source = source
        self.version: int = int(document["version"])
        self.max_batch: int = int(document.get("max_batch", 200))
        self.retired_fields: frozenset[str] = frozenset(document.get("retired_fields", ()))
        self.calibration: Mapping[str, Any] = document.get("calibration", {})
        self.roles: dict[str, RoleSchema] = {
            role: self._build_role(role, body) for role, body in document["roles"].items()
        }
        self._by_topic = {schema.topic: schema for schema in self.roles.values()}
        self._by_node_id = {schema.node_id: schema for schema in self.roles.values()}
        self._validate()

    @staticmethod
    def _build_role(role: str, body: Mapping[str, Any]) -> RoleSchema:
        fields = []
        for raw in body["fields"]:
            fields.append(
                Field(
                    name=raw["name"],
                    type=FieldType(raw["type"]),
                    unit=raw.get("unit"),
                    precision=raw.get("precision"),
                    values=tuple(raw.get("values", ())),
                    legacy_source=raw.get("legacy_source"),
                    legacy_divisor=raw.get("legacy_divisor"),
                    legacy_map=raw.get("legacy_map"),
                    authoritative=raw.get("authoritative", True),
                    note=raw.get("note", ""),
                )
            )
        return RoleSchema(
            role=role,
            display_name=body["display_name"],
            node_id=body["node_id"],
            topic=body["topic"],
            fields=tuple(fields),
            authoritative_for=tuple(body.get("authoritative_for", ())),
            expected_rate_hz=body.get("expected_rate_hz"),
        )

    def _validate(self) -> None:
        for schema in self.roles.values():
            # A registry collision would corrupt the archive silently.
            if schema.topic in (s.topic for s in self.roles.values() if s is not schema):
                raise ValueError(f"duplicate topic {schema.topic!r}")
            for f in schema.fields:
                if f.name in self.retired_fields:
                    raise ValueError(f"{schema.role}.{f.name} is retired by schema v{self.version}")
                if f.type is FieldType.ENUM and not f.values:
                    raise ValueError(f"{schema.role}.{f.name} is an enum with no values")
                if f.unit and f.precision is None and f.type is FieldType.FLOAT:
                    raise ValueError(f"{schema.role}.{f.name} has a unit but no precision")

    def for_role(self, role: str) -> RoleSchema:
        try:
            return self.roles[role]
        except KeyError as exc:
            raise KeyError(f"unknown role {role!r}; known: {sorted(self.roles)}") from exc

    def for_topic(self, topic: str) -> RoleSchema:
        try:
            return self._by_topic[topic]
        except KeyError as exc:
            raise KeyError(f"no role registered for topic {topic!r}") from exc

    def role_for_node_id(self, node_id: str) -> str | None:
        """
        The role that owns ``node_id``, or ``None`` when the registry does not know it.

        Presence is addressed by node id, which the topic carries canonically. A node's
        own claim about its role — in the payload — is a claim, not a fact, and one node
        gets it wrong: trusting it marked an unrelated role offline while the node the
        message actually belonged to stayed online forever.
        """
        schema = self._by_node_id.get(node_id)
        return schema.role if schema is not None else None

    def all_roles(self) -> Iterable[RoleSchema]:
        return self.roles.values()

    def authoritative_role_for(self, quantity: str) -> RoleSchema | None:
        """
        Which role owns a quantity.

        Dissolved oxygen is reported by two nodes — ``od_sensor`` authoritatively,
        and echoed on the aerator as ``peer_do_mg_l`` for its own PID loop. Exports
        must never average the two.
        """
        for schema in self.roles.values():
            if quantity in schema.authoritative_for:
                return schema
        return None


@functools.lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> SchemaRegistry:
    """Load and cache the registry. Cached because it is read on every batch."""
    target = path or DEFAULT_REGISTRY_PATH
    with target.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return SchemaRegistry(document, target)

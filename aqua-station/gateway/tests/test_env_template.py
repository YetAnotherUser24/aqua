"""
The environment template must consist only of settings that exist.

`deploy/install.sh` copies `.env.example` to `/etc/aqua/gateway.env` on a fresh
host, and `Settings` forbids unknown keys, so a single misspelled name in the
template is not a cosmetic problem: the gateway refuses to start and the service
exits 1 in a restart loop.

That is exactly what happened. The template carried

    AQUA_ARCHIVE__FSCYNC_ON_ROTATE=true

instead of `FSYNC`. Every fresh install therefore inherited a configuration that
could not boot, and the failure surfaced as:

    pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
    archive.fscync_on_rotate
      Extra inputs are not permitted [type=extra_forbidden]

Only the live install caught it. The unit tests all passed, because none of them
reads the template -- the file is deployed, not imported.

These tests read it, and are the reason a typo like that cannot ship again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aqua_gateway.config import Settings

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.example"


def template_keys() -> list[str]:
    """Every `AQUA_...` assignment in the template, comments and blanks skipped."""
    keys = []
    for line in TEMPLATE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


def test_the_template_exists_and_is_not_empty():
    # If this fails the two tests below would pass vacuously on an empty list.
    assert TEMPLATE.is_file()
    assert len(template_keys()) > 20


def test_every_template_key_is_a_real_setting():
    """
    Walks the nested model rather than just the top level: `AQUA_ARCHIVE__X` maps to
    `settings.archive.x`, and it is the leaf that the typo broke.
    """
    unknown: list[tuple[str, str]] = []

    for key in template_keys():
        if not key.startswith("AQUA_"):
            unknown.append((key, "missing the AQUA_ prefix"))
            continue

        parts = key[len("AQUA_") :].lower().split("__")
        model = Settings
        for index, part in enumerate(parts):
            field = model.model_fields.get(part)
            if field is None:
                unknown.append((key, f"no settings field for {part!r}"))
                break
            if index < len(parts) - 1:
                nested = field.annotation
                if not hasattr(nested, "model_fields"):
                    unknown.append((key, f"{part!r} is not a nested settings group"))
                    break
                model = nested

    assert not unknown, "template keys with no matching setting: " + ", ".join(f"{key} ({why})" for key, why in unknown)


def test_the_fsync_key_is_spelled_correctly():
    """
    Named explicitly as well as covered by the walk above, so the regression has a
    failing test whose name says what went wrong.
    """
    keys = set(template_keys())
    assert "AQUA_ARCHIVE__FSYNC_ON_ROTATE" in keys
    assert "AQUA_ARCHIVE__FSCYNC_ON_ROTATE" not in keys
    assert Settings.model_fields["archive"].annotation.model_fields["fsync_on_rotate"]


def apply_template(monkeypatch) -> None:
    """Put every template assignment into the environment, as systemd would."""
    for line in TEMPLATE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        monkeypatch.setenv(name.strip(), value.strip())


def test_the_template_parses_as_settings(monkeypatch):
    """
    The strongest form: load the template as the environment and construct `Settings`.

    This is what the service does at boot, minus the systemd wrapper -- so it fails for
    the same reason the gateway failed, on the same line.
    """
    apply_template(monkeypatch)

    settings = Settings()

    assert settings.archive.fsync_on_rotate is True
    assert settings.archive.root


@pytest.mark.parametrize("key", ["AQUA_ARCHIVE__FSYNC_ON_ROTATE"])
def test_the_key_actually_takes_effect(key, monkeypatch):
    """
    A correctly spelled name that nothing reads would be its own defect.

    Starts from the template rather than an empty environment: the model enforces
    cross-field rules (backups require storage credentials), so overriding one
    variable on its own would fail for a reason that has nothing to do with this key.
    """
    apply_template(monkeypatch)
    monkeypatch.setenv(key, "false")

    assert Settings().archive.fsync_on_rotate is False

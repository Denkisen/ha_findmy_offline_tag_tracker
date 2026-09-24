"""Config flow tests for the Loshall Tag integration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "custom_components"))

from homeassistant import config_entries  # noqa: E402

from ha_findmy_offline_tag_tracker.const import DOMAIN  # noqa: E402

VALID = {
    "name": "Loshall Tag 1",
    "clock_offset": "17000000.5",
    "curve": "secp160r1",
    "ephemeral_identity_key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}


async def test_user_flow_creates_entry(
    hass, enable_custom_integrations
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID)
    assert result["type"] == "create_entry"
    assert result["title"] == "Loshall Tag 1"
    # The EIK is normalised to lowercase.
    assert result["data"]["ephemeral_identity_key"] == VALID[
        "ephemeral_identity_key"
    ].lower()


async def test_invalid_clock_offset_shows_error(
    hass, enable_custom_integrations
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID, "clock_offset": "not-a-number"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"clock_offset": "invalid_clock_offset"}


async def test_invalid_eik_shows_error(hass, enable_custom_integrations) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # Too short / not valid hex.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID, "ephemeral_identity_key": "zz"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"ephemeral_identity_key": "invalid_eik"}


async def test_empty_name_shows_error(hass, enable_custom_integrations) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID, "name": "   "}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"name": "name_required"}


async def test_duplicate_eik_aborts(hass, enable_custom_integrations) -> None:
    # Pre-seed an existing entry with the same EIK.
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN, title="Tag A", data=VALID, unique_id="existing"
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID)
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_options_flow(hass, enable_custom_integrations) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, title="Loshall Tag 1", data=VALID, unique_id="opt-test"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"presence_timeout": 420}
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {"presence_timeout": 420}

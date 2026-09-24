"""Config flow for the Find My Offline Tag Tracker (Loshall Tag) integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow

from .const import DEFAULT_PRESENCE_TIMEOUT, DOMAIN
from .ec import _CURVES

_LOGGER = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_KNOWN_CURVES = sorted(_CURVES)


def _is_valid_hex(value: str) -> bool:
    return bool(_HEX_RE.match(value or "")) and len(value) % 2 == 0


def _is_valid_eik(value: str) -> bool:
    if not _is_valid_hex(value):
        return False
    length = len(bytes.fromhex(value))
    return length in (20, 32)  # secp160r1 -> 20 bytes, secp256r1 -> 32 bytes


def _validate_clock_offset(value: str) -> bool:
    try:
        float(value)
    except (ValueError, TypeError):
        return False
    return True


USER_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("clock_offset"): str,
        vol.Required("curve", default="secp160r1"): vol.In(_KNOWN_CURVES),
        vol.Required("ephemeral_identity_key"): str,
    }
)


class ConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Loshall Tag integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            errors: dict[str, str] = {}

            name = user_input["name"].strip()
            if not name:
                errors["name"] = "name_required"

            clock_offset = user_input["clock_offset"]
            if not _validate_clock_offset(clock_offset):
                errors["clock_offset"] = "invalid_clock_offset"

            curve = user_input["curve"]
            eik = user_input["ephemeral_identity_key"].strip()
            if not _is_valid_eik(eik):
                errors["ephemeral_identity_key"] = "invalid_eik"

            if not errors:
                # De-duplicate: one entry per ephemeral identity key.
                existing_eik = eik.lower()
                for existing in self._async_current_entries():
                    stored = existing.data.get("ephemeral_identity_key")
                    if stored is not None and stored.lower() == existing_eik:
                        raise AbortFlow("already_configured")

                return self.async_create_entry(
                    title=name,
                    data={
                        "name": name,
                        "clock_offset": clock_offset,
                        "curve": curve,
                        "ephemeral_identity_key": eik.lower(),
                    },
                )

            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA, errors=errors
            )

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this config entry."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handle options for the Loshall Tag integration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # `OptionsFlow.config_entry` is a read-only property; store a copy.
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    "presence_timeout",
                    default=self._entry.options.get(
                        "presence_timeout", DEFAULT_PRESENCE_TIMEOUT
                    ),
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

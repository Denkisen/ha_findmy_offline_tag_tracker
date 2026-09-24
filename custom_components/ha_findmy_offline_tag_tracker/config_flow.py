"""Config flow for FHN Tracker integration."""

import voluptuous as vol
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .fhn_codec import FHNBeacon, SECP160R1

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({
    vol.Required("shared_secret"): str,
    vol.Required("account_key"): str,
    vol.Required("ephemeral_identity_key"): str,
    vol.Required("model_id"): str,
    vol.Optional("original_mac", default=""): str,
    vol.Required("clock_offset"): str,
})


def validate_fhn_keys(user_input: dict) -> None:
    """Validate FHN provisioning keys are properly formatted."""
    bytes.fromhex(user_input["ephemeral_identity_key"])
    bytes.fromhex(user_input["account_key"])
    bytes.fromhex(user_input["shared_secret"])
    float(user_input["clock_offset"])

    if not user_input["model_id"].startswith("0x"):
        raise ValueError("Model ID must start with 0x")
    int(user_input["model_id"], 16)


class FHNTrackerFlowHandler(config_entries.ConfigFlow, domain="fhn_tracker"):
    """Handle a config flow for FHN Tracker."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validate_fhn_keys(user_input)

                beacon = FHNBeacon(
                    clock_offset=float(user_input["clock_offset"]),
                    curve=SECP160R1,
                    ephemeral_identity_key=bytes.fromhex(
                        user_input["ephemeral_identity_key"]
                    ),
                    name="Test Beacon",
                    model_id=user_input["model_id"],
                    original_mac=user_input.get("original_mac", ""),
                )

                from datetime import datetime
                test_eid = beacon.generate_eid(datetime.now())
                if len(test_eid) != beacon.eid_length:
                    raise ValueError("EID generation failed")

                return self.async_create_entry(
                    title=f"Tag ({user_input['model_id'][-6:]})",
                    data=user_input,
                )
            except ValueError as err:
                errors["base"] = str(err)
            except Exception as err:
                errors["base"] = "invalid_keys"
                _LOGGER.exception(f"Unexpected error: {err}")

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "instructions": (
                    "**From fhn_provision.py output:**\n\n"
                    "- `Shared Secret:` paste value\n"
                    "- `Account Key:` paste value\n"
                    "- `Ephemeral Identity Key:` paste value\n"
                    "- `Model ID:` keep 0x prefix\n"
                    "- `Original MAC:` optional\n"
                    "- `Clock Offset:` numeric value\n\n"
                    "**Note:** Ensure no spaces in key values."
                ),
            },
        )
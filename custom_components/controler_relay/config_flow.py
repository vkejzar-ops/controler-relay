"""Config flow for Controler Relay."""
from __future__ import annotations

import logging
from typing import Any

import serial_asyncio_fast as serial_asyncio
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BAUD_RATE,
    CONF_PANEL_COUNT,
    DEFAULT_BAUD_RATE,
    DEFAULT_PANEL_COUNT,
    DOMAIN,
    MAX_PANELS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default="/dev/ttyAMA0"): str,
        vol.Required(CONF_BAUD_RATE, default=DEFAULT_BAUD_RATE): int,
        vol.Required(CONF_PANEL_COUNT, default=DEFAULT_PANEL_COUNT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_PANELS)
        ),
    }
)


async def _async_validate_port(port: str, baud_rate: int) -> None:
    """Raise if the serial port can't be opened."""
    _, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud_rate)
    writer.close()


class ControlerRelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Controler Relay."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_PORT])
            self._abort_if_unique_id_configured()

            try:
                await _async_validate_port(user_input[CONF_PORT], user_input[CONF_BAUD_RATE])
            except Exception:  # noqa: BLE001 - any open failure means "cannot_connect"
                _LOGGER.exception("Unable to open serial port %s", user_input[CONF_PORT])
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Controler Relay ({user_input[CONF_PORT]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

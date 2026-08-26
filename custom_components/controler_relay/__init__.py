"""The Controler Relay integration (ESP32 bus bridge over UART)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_BAUD_RATE, DOMAIN
from .hub import ControlerRelayHub

PLATFORMS: list[Platform] = [Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Controler Relay from a config entry."""
    hub = ControlerRelayHub(
        hass.loop, entry.data[CONF_PORT], entry.data[CONF_BAUD_RATE]
    )
    await hub.async_connect()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hub: ControlerRelayHub = hass.data[DOMAIN].pop(entry.entry_id)
        await hub.async_close()
    return unload_ok

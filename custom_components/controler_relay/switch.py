"""Switch platform for Controler Relay: 12 buttons + 1 master."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NUM_BUTTONS
from .hub import ControlerRelayHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up switch entities for a config entry."""
    hub: ControlerRelayHub = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [ControlerRelayMasterSwitch(hub, entry)]
    entities.extend(
        ControlerRelayButtonSwitch(hub, entry, button)
        for button in range(1, NUM_BUTTONS + 1)
    )
    async_add_entities(entities)


class ControlerRelayEntity(SwitchEntity):
    """Shared setup for entities backed by the hub's push updates."""

    _attr_should_poll = False

    def __init__(self, hub: ControlerRelayHub, entry: ConfigEntry) -> None:
        self._hub = hub
        self._unsubscribe = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Controler Relay Panel",
            manufacturer="DIY",
            model="ESP32 Bus Bridge",
        )

    async def async_added_to_hass(self) -> None:
        self._unsubscribe = self._hub.add_listener(self._handle_hub_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _handle_hub_update(self) -> None:
        self.async_write_ha_state()


class ControlerRelayMasterSwitch(ControlerRelayEntity):
    """The panel's master on/off switch."""

    _attr_translation_key = "master"

    def __init__(self, hub: ControlerRelayHub, entry: ConfigEntry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.entry_id}_master"
        self._attr_name = "Master"

    @property
    def is_on(self) -> bool | None:
        return self._hub.master_state

    @property
    def available(self) -> bool:
        return self._hub.available and self._hub.master_state is not None

    async def async_turn_on(self, **kwargs) -> None:
        await self._hub.async_set_master(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._hub.async_set_master(False)


class ControlerRelayButtonSwitch(ControlerRelayEntity):
    """One of the panel's 12 buttons."""

    _attr_translation_key = "button"

    def __init__(self, hub: ControlerRelayHub, entry: ConfigEntry, button: int) -> None:
        super().__init__(hub, entry)
        self._button = button
        self._attr_unique_id = f"{entry.entry_id}_button_{button}"
        self._attr_name = f"Button {button}"

    @property
    def is_on(self) -> bool | None:
        return self._hub.is_button_on(self._button)

    @property
    def available(self) -> bool:
        return self._hub.available and self._hub.master_state is not None

    async def async_turn_on(self, **kwargs) -> None:
        await self._hub.async_set_button(self._button, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._hub.async_set_button(self._button, False)

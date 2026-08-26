"""Serial (UART) client for the ESP32 bus bridge.

Speaks the newline-delimited JSON protocol documented in PROTOCOL.md at
the repo root. Owns the connection lifecycle (connect, read loop,
reconnect-with-backoff) and exposes the last-known panel state plus a
simple listener callback for entities to subscribe to updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import serial_asyncio_fast as serial_asyncio

from .const import (
    MSG_TYPE_GET_STATE,
    MSG_TYPE_SET_BUTTON,
    MSG_TYPE_SET_MASTER,
    MSG_TYPE_STATE,
    NUM_SLOTS,
    RECONNECT_INITIAL_DELAY,
    RECONNECT_MAX_DELAY,
    VALID_SLOT_STATES,
)

_LOGGER = logging.getLogger(__name__)


class ControlerRelayHub:
    """Manages the UART connection to the ESP32 bridge."""

    def __init__(self, loop: asyncio.AbstractEventLoop, port: str, baud_rate: int) -> None:
        self._loop = loop
        self._port = port
        self._baud_rate = baud_rate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._closing = False

        self.available = False
        self.master_state: bool | None = None
        self.slot_states: list[str] = [VALID_SLOT_STATES[0]] * NUM_SLOTS

        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked whenever state changes. Returns an unsubscribe function."""
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def is_button_on(self, button: int) -> bool | None:
        """Return the current on/off state for a 1-12 button, or None if unknown."""
        if self.master_state is None:
            return None
        slot_index = (button - 1) // 2
        is_odd_button = (button - 1) % 2 == 0
        state = self.slot_states[slot_index]
        if is_odd_button:
            return state in ("odd", "held")
        return state in ("even", "held")

    async def async_connect(self) -> None:
        """Open the serial connection and start the read loop."""
        self._closing = False
        await self._open()
        self._read_task = self._loop.create_task(self._read_loop())

    async def _open(self) -> None:
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud_rate
        )
        self.available = True
        await self._async_send({"t": MSG_TYPE_GET_STATE})
        self._notify()

    async def async_close(self) -> None:
        """Close the connection and stop reconnect attempts."""
        self._closing = True
        if self._read_task is not None:
            self._read_task.cancel()
            self._read_task = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None
        self.available = False

    async def _read_loop(self) -> None:
        delay = RECONNECT_INITIAL_DELAY
        while not self._closing:
            try:
                assert self._reader is not None
                line = await self._reader.readline()
                if not line:
                    raise ConnectionError("serial connection closed by peer")
                delay = RECONNECT_INITIAL_DELAY
                self._handle_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any I/O/parse error should trigger reconnect
                if self._closing:
                    return
                _LOGGER.warning("Controler Relay serial link lost (%s), reconnecting", err)
                self.available = False
                self._notify()
                await self._reconnect_with_backoff(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _reconnect_with_backoff(self, delay: float) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self._open()
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Controler Relay reconnect attempt failed: %s", err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    def _handle_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="ignore").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.debug("Ignoring non-JSON line from ESP32: %r", text)
            return

        if msg.get("t") != MSG_TYPE_STATE:
            return

        slots = msg.get("slots")
        master = msg.get("master")
        if (
            not isinstance(slots, list)
            or len(slots) != NUM_SLOTS
            or any(s not in VALID_SLOT_STATES for s in slots)
            or not isinstance(master, bool)
        ):
            _LOGGER.warning("Ignoring malformed state message from ESP32: %r", msg)
            return

        self.master_state = master
        self.slot_states = list(slots)
        self.available = True
        self._notify()

    async def _async_send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("serial link not connected")
        self._writer.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def async_set_button(self, button: int, on: bool) -> None:
        await self._async_send({"t": MSG_TYPE_SET_BUTTON, "button": button, "on": on})

    async def async_set_master(self, on: bool) -> None:
        await self._async_send({"t": MSG_TYPE_SET_MASTER, "on": on})

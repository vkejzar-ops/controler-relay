#!/usr/bin/env python3
"""Mock ESP32 bridge firmware for testing the Home Assistant integration
without real hardware.

Creates a pty pair, prints the path of the "device" side (point the HA
config flow's serial port at it), and speaks the protocol documented in
PROTOCOL.md: tracks 6 slot states + master state in memory, responds to
get_state/set_button/set_master, and pushes a state update after every
change -- exactly what the real ESP32 firmware is expected to do.

Usage:
    python3 tools/mock_esp32.py [--link /tmp/controler_relay_mock_tty]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pty

_LOGGER = logging.getLogger("mock_esp32")

NUM_SLOTS = 6


class MockPanel:
    def __init__(self) -> None:
        self.master_state = True
        self.slot_states = ["neutral"] * NUM_SLOTS

    def state_message(self) -> dict:
        return {"t": "state", "master": self.master_state, "slots": list(self.slot_states)}

    def set_button(self, button: int, on: bool) -> None:
        if not 1 <= button <= 12:
            _LOGGER.warning("Ignoring set_button for out-of-range button %s", button)
            return
        slot_index = (button - 1) // 2
        is_odd_button = (button - 1) % 2 == 0
        state = self.slot_states[slot_index]
        odd_on = state in ("odd", "held")
        even_on = state in ("even", "held")
        if is_odd_button:
            odd_on = on
        else:
            even_on = on
        if odd_on and even_on:
            new_state = "held"
        elif odd_on:
            new_state = "odd"
        elif even_on:
            new_state = "even"
        else:
            new_state = "neutral"
        self.slot_states[slot_index] = new_state

    def set_master(self, on: bool) -> None:
        self.master_state = on


async def handle_line(panel: MockPanel, line: str, write) -> None:
    line = line.strip()
    if not line:
        return
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        _LOGGER.warning("Ignoring non-JSON line from host: %r", line)
        return

    msg_type = msg.get("t")
    _LOGGER.info("<- %s", msg)

    if msg_type == "get_state":
        write(panel.state_message())
    elif msg_type == "set_button":
        panel.set_button(msg.get("button"), bool(msg.get("on")))
        write(panel.state_message())
    elif msg_type == "set_master":
        panel.set_master(bool(msg.get("on")))
        write(panel.state_message())
    else:
        _LOGGER.warning("Unknown message type from host: %r", msg_type)


async def async_main(link_path: str | None) -> None:
    master_fd, slave_fd = pty.openpty()
    device_path = os.ttyname(slave_fd)
    os.set_blocking(master_fd, False)

    if link_path:
        if os.path.islink(link_path) or os.path.exists(link_path):
            os.remove(link_path)
        os.symlink(device_path, link_path)
        print(f"Mock ESP32 listening. Point Home Assistant's serial port at: {link_path}")
    else:
        print(f"Mock ESP32 listening. Point Home Assistant's serial port at: {device_path}")

    panel = MockPanel()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb", 0))

    def write(message: dict) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        os.write(master_fd, data)
        _LOGGER.info("-> %s", message)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            await handle_line(panel, line.decode("utf-8", errors="ignore"), write)
    finally:
        transport.close()
        os.close(slave_fd)
        if link_path and os.path.islink(link_path):
            os.remove(link_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--link",
        default="/tmp/controler_relay_mock_tty",
        help="Stable symlink path to create pointing at the mock pty (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(async_main(args.link))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

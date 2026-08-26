#!/usr/bin/env python3
"""Standalone smoke test: drives hub.ControlerRelayHub against
tools/mock_esp32.py's protocol logic over a real pty pair, without
needing a full Home Assistant install.

Run: python3 tools/smoke_test_hub.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pty
import sys
import types

# hub.py uses a relative "from .const import ..." import, so load it as a
# submodule of a synthetic "controler_relay" package instead of importing
# custom_components/controler_relay/__init__.py directly -- that __init__
# pulls in homeassistant, which isn't a dependency of this standalone test.
_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "controler_relay"
)


def _load_submodule(pkg_name: str, mod_name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.{mod_name}", os.path.join(_COMPONENT_DIR, file_name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.{mod_name}"] = module
    spec.loader.exec_module(module)
    return module


_pkg = types.ModuleType("controler_relay")
_pkg.__path__ = [_COMPONENT_DIR]
sys.modules["controler_relay"] = _pkg
_load_submodule("controler_relay", "const", "const.py")
hub_module = _load_submodule("controler_relay", "hub", "hub.py")
ControlerRelayHub = hub_module.ControlerRelayHub

sys.path.insert(0, os.path.dirname(__file__))
from mock_esp32 import MockPanel, handle_line  # noqa: E402


async def run_mock_device(master_fd: int, panel: MockPanel) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb", 0))

    def write(message: dict) -> None:
        import json

        os.write(master_fd, (json.dumps(message) + "\n").encode("utf-8"))

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            await handle_line(panel, line.decode("utf-8", errors="ignore"), write)
    except asyncio.CancelledError:
        pass
    finally:
        transport.close()


async def main() -> None:
    master_fd, slave_fd = pty.openpty()
    device_path = os.ttyname(slave_fd)
    os.set_blocking(master_fd, False)

    panel = MockPanel()
    device_task = asyncio.get_running_loop().create_task(run_mock_device(master_fd, panel))

    hub = ControlerRelayHub(asyncio.get_running_loop(), device_path, 115200)
    await hub.async_connect()

    # Let the initial get_state/state round trip settle.
    await asyncio.sleep(0.2)
    assert hub.master_state is True, f"expected initial master True, got {hub.master_state}"
    assert hub.slot_states == ["neutral"] * 6, hub.slot_states
    assert hub.is_button_on(1) is False
    print("PASS: initial state received")

    # Turn button 7 on (odd button of slot 4) -> slot should become "odd".
    await hub.async_set_button(7, True)
    await asyncio.sleep(0.2)
    assert hub.slot_states[3] == "odd", hub.slot_states
    assert hub.is_button_on(7) is True
    print("PASS: set_button(7, True) -> slot 4 = odd")

    # Turn button 8 on too (even button of slot 4) -> slot should become "held".
    await hub.async_set_button(8, True)
    await asyncio.sleep(0.2)
    assert hub.slot_states[3] == "held", hub.slot_states
    assert hub.is_button_on(7) is True
    assert hub.is_button_on(8) is True
    print("PASS: set_button(8, True) -> slot 4 = held")

    # Turn button 7 back off -> slot should become "even" (only 8 held).
    await hub.async_set_button(7, False)
    await asyncio.sleep(0.2)
    assert hub.slot_states[3] == "even", hub.slot_states
    print("PASS: set_button(7, False) -> slot 4 = even")

    # Master off/on round trip.
    await hub.async_set_master(False)
    await asyncio.sleep(0.2)
    assert hub.master_state is False
    print("PASS: set_master(False)")

    await hub.async_set_master(True)
    await asyncio.sleep(0.2)
    assert hub.master_state is True
    print("PASS: set_master(True)")

    await hub.async_close()
    device_task.cancel()
    os.close(slave_fd)

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())

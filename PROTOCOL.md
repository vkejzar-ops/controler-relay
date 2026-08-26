# UART Protocol — Home Assistant <-> ESP32 Bridge

The ESP32 connects to the Home Assistant host over a **wired UART** link
(the ESP32's TX/RX pins directly to the host's RX/TX pins — no WiFi, no
MQTT, no USB adapter assumed). This document is the contract both sides
implement: the Home Assistant custom integration in
`custom_components/controler_relay/` implements the HA side today; the
ESP32 firmware (`esp32_bus_bridge.ino`) will implement the device side
once the HA side is validated.

## Framing

- **Baud rate:** 115200, 8N1 (matches `Serial.begin(115200)` already in
  the sketches).
- **Message framing:** one JSON object per line, terminated by `\n`.
  No length prefixes, no binary framing — easy to emit from an Arduino
  `Serial.println()` and easy to parse with `StreamReader.readline()`
  on the HA side.
- Any line that isn't valid JSON (e.g. boot-time garbage from the ESP32's
  bootloader) is silently ignored by the HA side rather than treated as
  a fatal error.
- Every message has a `"t"` field naming its type.

## Device -> Host messages

### `state` — full panel state

Sent by the device whenever button/master state changes, and once in
response to a `get_state` request (e.g. right after the host (re)connects).

```json
{"t": "state", "master": true, "slots": ["neutral", "odd", "even", "held", "neutral", "neutral"]}
```

- `master`: boolean, current master on/off state.
- `slots`: array of exactly 6 strings, one per slot (slot 1 first),
  each one of `"neutral"`, `"odd"`, `"even"`, `"held"` — matching the
  `SlotState` enum in `esp32_bus_bridge.ino`.

The host derives each of the 12 per-button booleans from `slots`:

```
slot index si (0..5) -> buttons (2*si + 1, 2*si + 2)
odd  button on  <=>  slots[si] in {"odd", "held"}
even button on  <=>  slots[si] in {"even", "held"}
```

## Host -> Device messages

### `get_state` — request a full state dump

Sent once right after the serial connection opens (and after any
reconnect), so the host doesn't have to guess initial entity state.

```json
{"t": "get_state"}
```

Device responds with a `state` message.

### `set_button` — set one button's on/off state

```json
{"t": "set_button", "button": 7, "on": true}
```

- `button`: integer 1-12.
- `on`: desired boolean state for that button.

The device is responsible for combining the two buttons of a slot
(odd/even) into the correct `SlotState` (`neutral`/`odd`/`even`/`held`)
and injecting the corresponding frame on the bus, then pushing an
updated `state` message back once the new state is confirmed.

### `set_master` — set the master on/off state

```json
{"t": "set_master", "on": true}
```

Device injects the master-on or master-off frame accordingly, then
pushes an updated `state` message back.

## Reconnection

If the serial link drops (device reset, cable unplugged), the host
marks all entities unavailable and retries opening the port with
backoff. On successful reconnect it immediately sends `get_state`
again.

## Testing without hardware

`tools/mock_esp32.py` emulates the device side of this protocol over a
pty pair, so the Home Assistant integration can be developed and
exercised end-to-end before the real ESP32 firmware exists.

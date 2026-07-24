// ============================================================
// ESP32 bus-protocol bridge: passive relay + Home Assistant inject
// Panel --(pulses)--> ESP32 --(relayed pulses)--> Receiver
// ESP32 also listens for HA commands and can inject its own frames
// ============================================================

#include <Arduino.h>

// ---------- Pin assignments ----------
#define PIN_PANEL_IN   4   // signal FROM the button panel (input, interrupt)
#define PIN_RX_OUT     5   // signal TO the receiver (output, relay/inject)

// ---------- Protocol constants ----------
const uint32_t SYMBOL_US = 416;       // 1 symbol unit = 416 microseconds
const uint32_t GAP_THRESH_US = 50000; // >50ms idle = frame boundary

const uint8_t HEADER_NORMAL[16] = {2,1,1,2,1,1,1,1,9,1,1,9,2,1,6,1};
const uint8_t HEADER_MASTER[16] = {2,1,1,2,1,1,1,1,9,1,1,9,1,2,6,1};

// ---------- Capture state (touched by ISR) ----------
volatile uint32_t lastEdgeTime = 0;
volatile uint32_t capturedSymbols[128];
volatile int numCaptured = 0;
volatile bool frameReady = false;
volatile bool lastLevel = HIGH;

// ---------- ISR: just timestamp + push raw duration, do NO decoding here ----------
void IRAM_ATTR onPanelEdge() {
  uint32_t now = micros();
  uint32_t duration = now - lastEdgeTime;
  lastEdgeTime = now;

  if (duration > GAP_THRESH_US) {
    // idle gap seen -> previous pulses (if any) form a completed frame
    if (numCaptured > 0) {
      frameReady = true; // main loop will pick this up
    }
    numCaptured = 0; // start fresh after the gap
    return;
  }

  if (numCaptured < 128) {
    // round duration to nearest symbol unit
    uint32_t sym = (duration + SYMBOL_US / 2) / SYMBOL_US;
    capturedSymbols[numCaptured++] = sym;
  }
}

// ---------- Relay: bit-bang a symbol array out to the receiver ----------
// Runs in the MAIN LOOP, never inside the ISR.
void relayFrame(uint32_t *symbols, int count) {
  bool level = HIGH;
  digitalWrite(PIN_RX_OUT, level);
  for (int i = 0; i < count; i++) {
    uint32_t us = symbols[i] * SYMBOL_US;
    delayMicroseconds(us);
    level = !level;
    digitalWrite(PIN_RX_OUT, level);
  }
}

// ---------- Decode helpers ----------
// (mirrors the Python decoder we built earlier: header check, 6 slots, tail)
bool matchesHeader(uint32_t *symbols, const uint8_t *header) {
  for (int i = 0; i < 16; i++) {
    if (symbols[i] != header[i]) return false;
  }
  return true;
}

// ---------- Main loop ----------
void setup() {
  pinMode(PIN_PANEL_IN, INPUT_PULLUP);
  pinMode(PIN_RX_OUT, OUTPUT);
  digitalWrite(PIN_RX_OUT, HIGH);

  attachInterrupt(digitalPinToInterrupt(PIN_PANEL_IN), onPanelEdge, CHANGE);

  Serial.begin(115200);
}

void loop() {
  if (frameReady) {
    // copy out of volatile buffer quickly, then clear the flag
    noInterrupts();
    int count = numCaptured;
    uint32_t localSymbols[128];
    for (int i = 0; i < count; i++) localSymbols[i] = capturedSymbols[i];
    frameReady = false;
    interrupts();

    // 1. Passive relay: forward the frame to the receiver, unchanged,
    //    UNLESS we're actively injecting our own frame right now.
    relayFrame(localSymbols, count);

    // 2. Decode for our own bookkeeping / HA state sync (optional).
    //    Use the same logic as the Python decode_frame() we already built.
  }

  // 3. Check for pending Home-Assistant-triggered command (e.g. via MQTT
  //    subscription, or a simple HTTP endpoint you poll/receive here).
  //    When one arrives, build the symbol array for the desired button
  //    state (using our body + tail lookup table) and call relayFrame()
  //    with that array instead of a captured one.
}

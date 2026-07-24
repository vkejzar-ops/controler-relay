/*
  ESP32 Wired-Bus Protocol Bridge
  --------------------------------
  Sits between the 12-button (+1 master) control panel and the receiver.
  - Passively relays every pulse from panel -> receiver in real time.
  - Decodes each frame in the background to track full button/master state.
  - Can inject a synthetic frame (as if a button/master were pressed) on
    the bus line to the receiver, e.g. triggered from Home Assistant.

  Protocol reference (from the reverse-engineering sessions):
    symbol = round(pulse_duration_us / 416)
    frame boundary = idle gap > 50000 us (0.05 s)
    HEADER_NORMAL = [2,1,1,2,1,1,1,1,9,1,1,9,2,1,6,1]
    HEADER_MASTER = [2,1,1,2,1,1,1,1,9,1,1,9,1,2,6,1]  (positions 12-13 flipped)
    slot shapes:   neutral=[9,1] odd=[5,1,3,1] even=[1,1,7,1] held=[1,1,3,1,3,1]
    tail: looked up by (count_odd, count_held) among the 6 slots -- see TAIL_TABLE.

  HARDWARE ASSUMPTIONS (adjust to your actual wiring):
    - Bus idles HIGH; symbols are the durations between consecutive edges.
    - PIN_BUS_IN  : connected to the panel's bus line (input, pulled up).
    - PIN_BUS_OUT : connected to the receiver's bus line (output).
    - Normally PIN_BUS_OUT mirrors PIN_BUS_IN with minimal latency (passive
      relay). During injectFrame(), the panel side should be electrically
      isolated (e.g. via a mosfet/analog switch on PIN_RELAY_ISOLATE) so the
      ESP32 can drive PIN_BUS_OUT on its own without contention from the
      panel. Wire that isolation switch to suit your actual hardware --
      this sketch only raises/lowers the control pin, it doesn't assume a
      specific switch part.
*/

#include <Arduino.h>

// ---------- Pin configuration ----------
static const uint8_t PIN_BUS_IN        = 4;   // from panel
static const uint8_t PIN_BUS_OUT       = 5;   // to receiver
static const uint8_t PIN_RELAY_ISOLATE = 6;   // drives isolation switch; HIGH = panel disconnected from PIN_BUS_OUT

// ---------- Timing constants ----------
static const uint32_t SYMBOL_US      = 416;     // 1 symbol unit
static const uint32_t GAP_THRESHOLD_US = 50000; // frame boundary gap

// ---------- Protocol constants ----------
static const uint8_t HEADER_NORMAL[16] = {2,1,1,2,1,1,1,1,9,1,1,9,2,1,6,1};
static const uint8_t HEADER_MASTER[16] = {2,1,1,2,1,1,1,1,9,1,1,9,1,2,6,1};

enum SlotState : uint8_t { SLOT_NEUTRAL, SLOT_ODD, SLOT_EVEN, SLOT_HELD, SLOT_INVALID };

// Slot symbol shapes (as symbol-unit sequences)
static const uint8_t SHAPE_NEUTRAL[] = {9,1};
static const uint8_t SHAPE_ODD[]     = {5,1,3,1};
static const uint8_t SHAPE_EVEN[]    = {1,1,7,1};
static const uint8_t SHAPE_HELD[]    = {1,1,3,1,3,1};

// Tail lookup: index = odd*7 + held  (odd,held each 0..6, odd+held<=6)
// Stored as {length, sym0..sym6} with unused entries left as length 0.
struct TailEntry { uint8_t len; uint8_t syms[7]; };
static const TailEntry TAIL_TABLE[7][7] = {
  /* held->        0                         1                      2                    3                 4              5           6        */
  /*odd 0*/ {{7,{1,2,1,2,1,1,1}}, {5,{3,2,1,2,1}},     {5,{1,1,1,5,1}},   {3,{2,3,3}},     {3,{1,5,2}},   {3,{5,2,1}}, {3,{1,1,5}}},
  /*odd 1*/ {{7,{1,2,1,1,1,2,1}}, {3,{3,5,1}},         {5,{1,1,1,2,3}},   {3,{2,4,2}},     {5,{1,4,1,1,1}}, {1,{7}},   {0,{0}}},
  /*odd 2*/ {{5,{1,2,1,4,1}},     {3,{3,2,3}},         {5,{1,1,1,3,2}},   {5,{2,3,1,1,1}}, {3,{1,6,1}},   {0,{0}},     {0,{0}}},
  /*odd 3*/ {{5,{1,2,1,1,3}},     {3,{3,3,2}},         {5,{1,1,1,4,1}},   {3,{2,5,1}},     {0,{0}},       {0,{0}},     {0,{0}}},
  /*odd 4*/ {{5,{1,2,1,2,2}},     {5,{3,2,1,1,1}},     {3,{3,4,1}},       {0,{0}},         {0,{0}},       {0,{0}},     {0,{0}}},
  /*odd 5*/ {{7,{1,2,1,1,1,1,1}}, {5,{1,1,1,2,1,1,1}}, {0,{0}},           {0,{0}},         {0,{0}},       {0,{0}},     {0,{0}}},
  /*odd 6*/ {{5,{1,2,1,3,1}},     {0,{0}},             {0,{0}},           {0,{0}},         {0,{0}},       {0,{0}},     {0,{0}}},
};

// ---------- Edge capture ring buffer (filled from ISR) ----------
static const uint16_t RING_SIZE = 512;
volatile uint32_t ringDurationsUs[RING_SIZE];
volatile uint16_t ringHead = 0, ringTail = 0;
volatile uint32_t lastEdgeUs = 0;
volatile bool passiveRelayEnabled = true;

void IRAM_ATTR busInISR() {
  uint32_t now = micros();
  uint32_t dur = now - lastEdgeUs;
  lastEdgeUs = now;

  uint16_t nextHead = (ringHead + 1) % RING_SIZE;
  if (nextHead != ringTail) {
    ringDurationsUs[ringHead] = dur;
    ringHead = nextHead;
  } // else: buffer full, drop -- decoding loop needs to keep up

  if (passiveRelayEnabled) {
    // Mirror the edge straight through to the receiver with minimal delay.
    digitalWrite(PIN_BUS_OUT, digitalRead(PIN_BUS_IN));
  }
}

// ---------- Frame decode state ----------
struct DecodedFrame {
  bool valid;
  bool isMasterEvent;
  SlotState slots[6];
  uint8_t tail[7];
  uint8_t tailLen;
};

// Current known state of the whole panel, updated as frames decode.
struct PanelState {
  SlotState slots[6] = {SLOT_NEUTRAL, SLOT_NEUTRAL, SLOT_NEUTRAL,
                         SLOT_NEUTRAL, SLOT_NEUTRAL, SLOT_NEUTRAL};
  bool masterOn = false;
} panelState;

static bool matchShape(const uint8_t* symbols, uint16_t len, uint16_t pos,
                        const uint8_t* shape, uint8_t shapeLen) {
  if (pos + shapeLen > len) return false;
  for (uint8_t i = 0; i < shapeLen; i++) {
    if (symbols[pos + i] != shape[i]) return false;
  }
  return true;
}

static SlotState parseField(const uint8_t* symbols, uint16_t len, uint16_t &pos) {
  if (matchShape(symbols, len, pos, SHAPE_ODD, 4))     { pos += 4; return SLOT_ODD; }
  if (matchShape(symbols, len, pos, SHAPE_EVEN, 4))    { pos += 4; return SLOT_EVEN; }
  if (matchShape(symbols, len, pos, SHAPE_HELD, 6))    { pos += 6; return SLOT_HELD; }
  if (matchShape(symbols, len, pos, SHAPE_NEUTRAL, 2)) { pos += 2; return SLOT_NEUTRAL; }
  return SLOT_INVALID;
}

// Decodes one already-boundary-split frame of normalized symbols.
DecodedFrame decodeFrame(const uint8_t* symbols, uint16_t len) {
  DecodedFrame f{};
  f.valid = false;
  f.isMasterEvent = false;

  bool isNormal = (len >= 16) && (memcmp(symbols, HEADER_NORMAL, 16) == 0);
  bool isMaster = (len >= 16) && (memcmp(symbols, HEADER_MASTER, 16) == 0);
  if (!isNormal && !isMaster) return f;

  uint16_t pos = 16;
  SlotState masterField = SLOT_NEUTRAL;

  if (isMaster) {
    masterField = parseField(symbols, len, pos);
    if (masterField == SLOT_INVALID) return f;
    f.isMasterEvent = true;
  }

  for (uint8_t s = 0; s < 6; s++) {
    SlotState st = parseField(symbols, len, pos);
    if (st == SLOT_INVALID) return f;
    f.slots[s] = st;
  }

  uint8_t tailLen = len - pos;
  if (tailLen > 7) tailLen = 7; // guard, shouldn't happen on well-formed frames
  for (uint8_t i = 0; i < tailLen; i++) f.tail[i] = symbols[pos + i];
  f.tailLen = tailLen;
  f.valid = true;

  // ---- Update persistent panel state ----
  if (f.isMasterEvent) {
    // Master-OFF is a fixed constant frame: master field neutral, all
    // slots neutral, tail == [3,3,1,1,1]. Master-ON reports the real,
    // restored slot states (master field reads as SLOT_EVEN in both
    // samples seen so far).
    bool looksLikeOff = (masterField == SLOT_NEUTRAL) && (tailLen == 5) &&
                         symbols[pos]==3 && symbols[pos+1]==3 && symbols[pos+2]==1 &&
                         symbols[pos+3]==1 && symbols[pos+4]==1;
    if (looksLikeOff) {
      panelState.masterOn = false;
      // NOTE: real slot states are not zeroed out here in our tracked
      // PanelState -- the physical panel remembers them (confirmed: turning
      // master back on restores prior button state), so we only flip the
      // masterOn flag and leave panelState.slots as they were.
    } else {
      panelState.masterOn = true;
      for (uint8_t s = 0; s < 6; s++) panelState.slots[s] = f.slots[s];
    }
  } else {
    for (uint8_t s = 0; s < 6; s++) panelState.slots[s] = f.slots[s];
  }

  return f;
}

// ---------- Main decode loop (called from loop(), not from the ISR) ----------
static uint8_t frameBuf[64];
static uint16_t frameBufLen = 0;

void processRing() {
  while (ringTail != ringHead) {
    uint32_t dur;
    noInterrupts();
    dur = ringDurationsUs[ringTail];
    ringTail = (ringTail + 1) % RING_SIZE;
    interrupts();

    if (dur > GAP_THRESHOLD_US) {
      // Frame boundary: decode whatever we've accumulated, then reset.
      if (frameBufLen >= 16) {
        DecodedFrame f = decodeFrame(frameBuf, frameBufLen);
        if (f.valid) {
          onFrameDecoded(f); // user hook, see below
        }
      }
      frameBufLen = 0;
      continue;
    }

    if (frameBufLen < sizeof(frameBuf)) {
      uint8_t sym = (uint8_t) ((dur + SYMBOL_US / 2) / SYMBOL_US); // round
      frameBuf[frameBufLen++] = sym;
    }
  }
}

// Hook: called once per successfully decoded frame. Wire this up to your
// Home Assistant / MQTT reporting layer.
void onFrameDecoded(const DecodedFrame &f) {
  // Example: print a compact summary over serial for debugging.
  Serial.print(f.isMasterEvent ? "[MASTER] " : "[NORMAL] ");
  Serial.print("masterOn="); Serial.print(panelState.masterOn);
  Serial.print(" slots=");
  const char* names[] = {"neutral","odd","even","held"};
  for (uint8_t s = 0; s < 6; s++) {
    Serial.print(names[panelState.slots[s]]);
    if (s < 5) Serial.print(",");
  }
  Serial.println();
}

// ---------- Injection: build and drive a synthetic frame ----------

// Appends a slot's symbol shape into buf, returns new length.
static uint16_t appendSlot(uint8_t* buf, uint16_t len, SlotState st) {
  const uint8_t* shape; uint8_t shapeLen;
  switch (st) {
    case SLOT_ODD:  shape = SHAPE_ODD;  shapeLen = 4; break;
    case SLOT_EVEN: shape = SHAPE_EVEN; shapeLen = 4; break;
    case SLOT_HELD: shape = SHAPE_HELD; shapeLen = 6; break;
    default:         shape = SHAPE_NEUTRAL; shapeLen = 2; break;
  }
  for (uint8_t i = 0; i < shapeLen; i++) buf[len++] = shape[i];
  return len;
}

// Builds a full NORMAL frame for the given 6 slot states using the
// verified tail table. Returns total symbol count, or 0 if the
// (odd,held) combination isn't in range (shouldn't happen, all 28 covered).
uint16_t buildNormalFrame(const SlotState slots[6], uint8_t* out) {
  uint16_t len = 0;
  for (uint8_t i = 0; i < 16; i++) out[len++] = HEADER_NORMAL[i];
  uint8_t countOdd = 0, countHeld = 0;
  for (uint8_t s = 0; s < 6; s++) {
    len = appendSlot(out, len, slots[s]);
    if (slots[s] == SLOT_ODD) countOdd++;
    if (slots[s] == SLOT_HELD) countHeld++;
  }
  const TailEntry &te = TAIL_TABLE[countOdd][countHeld];
  if (te.len == 0) return 0; // out of range / unsupported combo
  for (uint8_t i = 0; i < te.len; i++) out[len++] = te.syms[i];
  return len;
}

// Builds the fixed master-OFF frame (constant, per confirmed captures).
uint16_t buildMasterOffFrame(uint8_t* out) {
  uint16_t len = 0;
  for (uint8_t i = 0; i < 16; i++) out[len++] = HEADER_MASTER[i];
  len = appendSlot(out, len, SLOT_NEUTRAL); // master field
  for (uint8_t s = 0; s < 6; s++) len = appendSlot(out, len, SLOT_NEUTRAL);
  const uint8_t offTail[5] = {3,3,1,1,1};
  for (uint8_t i = 0; i < 5; i++) out[len++] = offTail[i];
  return len;
}

// Builds a master-ON frame restoring the given slot states.
// NOTE: only verified for the sum-9 tail bucket (odd+held <= 2), where the
// transform is "-1 on tail symbol[1], +1 on tail symbol[3]". Outside that
// bucket this is a best-effort placeholder (falls back to the normal tail
// unmodified) until the master-on tail table is fully characterized --
// see the open item flagged in bus_protocol_master_table.md.
uint16_t buildMasterOnFrame(const SlotState slots[6], uint8_t* out) {
  uint16_t len = 0;
  for (uint8_t i = 0; i < 16; i++) out[len++] = HEADER_MASTER[i];
  len = appendSlot(out, len, SLOT_EVEN); // master field, "on" shape
  uint8_t countOdd = 0, countHeld = 0;
  for (uint8_t s = 0; s < 6; s++) {
    len = appendSlot(out, len, slots[s]);
    if (slots[s] == SLOT_ODD) countOdd++;
    if (slots[s] == SLOT_HELD) countHeld++;
  }
  TailEntry te = TAIL_TABLE[countOdd][countHeld];
  if (te.len == 0) return 0;
  if (countOdd + countHeld <= 2 && te.len >= 4) {
    // Verified transform, sum-9 bucket only.
    te.syms[1] -= 1;
    te.syms[3] += 1;
  }
  // else: unverified bucket -- using normal tail as a best-effort stand-in.
  for (uint8_t i = 0; i < te.len; i++) out[len++] = te.syms[i];
  return len;
}

// Drives a normalized symbol buffer out on PIN_BUS_OUT, isolating the
// panel side first so there's no contention on the shared line.
void injectFrame(const uint8_t* symbols, uint16_t len) {
  noInterrupts();
  passiveRelayEnabled = false;
  interrupts();

  digitalWrite(PIN_RELAY_ISOLATE, HIGH); // disconnect panel from PIN_BUS_OUT
  delayMicroseconds(10);

  bool level = HIGH; // bus idles high
  digitalWrite(PIN_BUS_OUT, level);
  for (uint16_t i = 0; i < len; i++) {
    delayMicroseconds(symbols[i] * SYMBOL_US);
    level = !level;
    digitalWrite(PIN_BUS_OUT, level);
  }

  digitalWrite(PIN_RELAY_ISOLATE, LOW); // reconnect panel
  noInterrupts();
  passiveRelayEnabled = true;
  interrupts();
}

// Convenience wrappers for Home Assistant-triggered actions.
void injectMasterOn()  { uint8_t buf[64]; uint16_t n = buildMasterOnFrame(panelState.slots, buf);  if (n) injectFrame(buf, n); }
void injectMasterOff() { uint8_t buf[64]; uint16_t n = buildMasterOffFrame(buf);                    if (n) injectFrame(buf, n); }

void injectSetSlot(uint8_t slotIndex, SlotState newState) {
  if (slotIndex >= 6) return;
  SlotState desired[6];
  for (uint8_t i = 0; i < 6; i++) desired[i] = panelState.slots[i];
  desired[slotIndex] = newState;
  uint8_t buf[64];
  uint16_t n = buildNormalFrame(desired, buf);
  if (n) injectFrame(buf, n);
}

// ---------- Arduino setup/loop ----------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUS_IN, INPUT_PULLUP);
  pinMode(PIN_BUS_OUT, OUTPUT);
  pinMode(PIN_RELAY_ISOLATE, OUTPUT);
  digitalWrite(PIN_BUS_OUT, HIGH);
  digitalWrite(PIN_RELAY_ISOLATE, LOW);

  lastEdgeUs = micros();
  attachInterrupt(digitalPinToInterrupt(PIN_BUS_IN), busInISR, CHANGE);
}

void loop() {
  processRing();
  // Home Assistant integration (MQTT/HTTP polling/etc.) goes here, calling
  // injectMasterOn() / injectMasterOff() / injectSetSlot() as needed, and
  // reading panelState for current status reporting.
}

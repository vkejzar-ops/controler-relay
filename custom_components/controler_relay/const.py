"""Constants for the Controler Relay integration."""

DOMAIN = "controler_relay"

CONF_BAUD_RATE = "baud_rate"
DEFAULT_BAUD_RATE = 115200

NUM_BUTTONS = 12
NUM_SLOTS = 6

MSG_TYPE_STATE = "state"
MSG_TYPE_SET_BUTTON = "set_button"
MSG_TYPE_SET_MASTER = "set_master"
MSG_TYPE_GET_STATE = "get_state"

SLOT_NEUTRAL = "neutral"
SLOT_ODD = "odd"
SLOT_EVEN = "even"
SLOT_HELD = "held"

VALID_SLOT_STATES = (SLOT_NEUTRAL, SLOT_ODD, SLOT_EVEN, SLOT_HELD)

RECONNECT_INITIAL_DELAY = 1
RECONNECT_MAX_DELAY = 30

'''
BLE multi-profile support for KMK.

Allows switching between up to N bonded BLE host connections.
Each slot uses a distinct BLE identity (address + name) so hosts treat each
slot as a separate device.  Only one slot is active at a time — this mirrors
ZMK profile-switching behaviour.

Usage in your code.py / kb.py
------------------------------
    from kmk.modules.ble_profiles import BLEProfiles

    keyboard.modules.append(BLEProfiles(n_slots=3))

    # In your keymap use:
    #   KC.BT_SEL_0  – switch to / advertise as slot 0
    #   KC.BT_SEL_1  – switch to / advertise as slot 1
    #   KC.BT_SEL_2  – switch to / advertise as slot 2
    #   KC.BT_NXT    – cycle forward through slots
    #   KC.BT_PRV    – cycle backward through slots
    #   KC.BT_CLR    – rotate address of current slot (forces re-pair on host)
    #   KC.BT_CLR_ALL – erase ALL bonds and reset MCU

Hardware note
-------------
Designed for the SuperMini nRF52840 (nice!nano v1 clone) running
CircuitPython 9.x.  Requires adafruit_ble and adafruit_hid in /lib.

NVM layout (microcontroller.nvm)
---------------------------------
    nvm[0]       : active slot index (0 … n_slots-1)
    nvm[1]       : slot-0 address-rotation seed (incremented on BT_CLR)
    nvm[2]       : slot-1 address-rotation seed
    nvm[3]       : slot-2 address-rotation seed
    nvm[4..7]    : reserved
'''

import microcontroller

from kmk.keys import make_key
from kmk.modules import Module
from kmk.utils import Debug

debug = Debug(__name__)

# Maximum slots this module will ever handle (limits NVM usage)
_MAX_SLOTS = 8


class BLEProfiles(Module):
    def __init__(self, n_slots=3):
        if n_slots < 1 or n_slots > _MAX_SLOTS:
            raise ValueError('BLEProfiles: n_slots must be 1–8')

        self._n_slots = n_slots

        # Register slot-select keys BT_SEL_0 … BT_SEL_N-1
        for i in range(n_slots):
            make_key(
                names=(f'BT_SEL_{i}',),
                on_press=self._make_select_handler(i),
            )

        make_key(names=('BT_NXT',), on_press=self._bt_nxt)
        make_key(names=('BT_PRV',), on_press=self._bt_prv)
        make_key(names=('BT_CLR',), on_press=self._bt_clr)
        make_key(names=('BT_CLR_ALL',), on_press=self._bt_clr_all)

    # ------------------------------------------------------------------
    # Key handlers
    # ------------------------------------------------------------------

    def _make_select_handler(self, slot):
        def _handler(key, keyboard, *args, **kwargs):
            self._select_slot(keyboard, slot)

        return _handler

    def _bt_nxt(self, key, keyboard, *args, **kwargs):
        hid = keyboard._hid_helper
        if not hasattr(hid, '_active_slot'):
            return
        next_slot = (hid._active_slot + 1) % self._n_slots
        self._select_slot(keyboard, next_slot)

    def _bt_prv(self, key, keyboard, *args, **kwargs):
        hid = keyboard._hid_helper
        if not hasattr(hid, '_active_slot'):
            return
        prev_slot = (hid._active_slot - 1) % self._n_slots
        self._select_slot(keyboard, prev_slot)

    def _bt_clr(self, key, keyboard, *args, **kwargs):
        hid = keyboard._hid_helper
        if not hasattr(hid, 'clear_profile'):
            return
        slot = hid._active_slot
        hid.clear_profile(slot)
        self._blink_slot(slot)

    def _bt_clr_all(self, key, keyboard, *args, **kwargs):
        try:
            import _bleio

            _bleio.adapter.erase_bonding()
        except Exception as e:
            if debug.enabled:
                debug('BT_CLR_ALL erase_bonding error:', e)

        # Reset all per-slot seeds so next pairings get fresh addresses
        for i in range(self._n_slots):
            try:
                microcontroller.nvm[1 + i] = 0
            except Exception:
                pass

        microcontroller.reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_slot(self, keyboard, slot):
        hid = keyboard._hid_helper
        if not hasattr(hid, 'switch_to_slot'):
            if debug.enabled:
                debug('BLEProfiles: hid does not support switch_to_slot')
            return
        hid.switch_to_slot(slot)
        self._blink_slot(slot)

    def _blink_slot(self, slot):
        '''Blink the board LED (slot+1) times to indicate the active slot.'''
        try:
            import board
            import digitalio
            import time

            led = digitalio.DigitalInOut(board.LED)
            led.direction = digitalio.Direction.OUTPUT
            led.value = False
            for _ in range(slot + 1):
                led.value = True
                time.sleep(0.12)
                led.value = False
                time.sleep(0.18)
            led.deinit()
        except Exception:
            pass  # Board may not have LED or time may be unavailable

    # ------------------------------------------------------------------
    # Module lifecycle
    # ------------------------------------------------------------------

    def during_bootup(self, keyboard):
        '''
        Configure the BLEHID helper for multi-slot operation.

        This runs after _init_hid() so keyboard._hid_helper already exists.
        ble_monitor fires after 1 s, giving us a clear window to set the
        adapter address before any advertising begins.
        '''
        hid = keyboard._hid_helper

        # Silently skip if we are not running in BLE mode
        if not hasattr(hid, 'switch_to_slot'):
            if debug.enabled:
                debug('BLEProfiles: not a BLEHID, skipping setup')
            return

        # Tell BLEHID how many slots we have
        hid._n_slots = self._n_slots

        # Read the persisted slot; clamp to valid range
        try:
            slot = microcontroller.nvm[0]
            if not isinstance(slot, int) or slot >= self._n_slots:
                slot = 0
                microcontroller.nvm[0] = 0
        except Exception:
            slot = 0

        # Apply slot identity (address + name) and start advertising
        hid._apply_slot_identity(slot)
        hid.start_advertising()

        if debug.enabled:
            debug('BLEProfiles: bootup on slot', slot)

        self._blink_slot(slot)

    def before_matrix_scan(self, keyboard):
        pass

    def after_matrix_scan(self, keyboard):
        pass

    def before_hid_send(self, keyboard):
        pass

    def after_hid_send(self, keyboard):
        pass

    def on_powersave_enable(self, keyboard):
        pass

    def on_powersave_disable(self, keyboard):
        pass

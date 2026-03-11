# KMK Multi-BT Profile Implementation Plan
### Target Hardware: SuperMini nRF52840 (nice!nano v1 clone)

---

## 0. Toolchain & Deployment

### Key concept: no build step
KMK on CircuitPython is **interpreted Python at runtime**. There is no compiler or build system. You copy `.py` files to the board's USB flash drive and CircuitPython runs them directly. CircuitPython watches for file changes and **auto-restarts** on every save.

`.mpy` files (pre-compiled bytecode) are only needed if you hit `MemoryError` at runtime. Develop with plain `.py` first.

### What you need on your PC
- **Git** — clone your fork
- **VS Code** — with the **CircuitPython extension** (by Joedevivo) for serial console access
- **Python 3** + `pip install mpy-cross circup` — only needed later if RAM is tight
- No compiler, no Docker, no Zephyr toolchain

### File layout

```
Your PC (VS Code fork)         SuperMini (CIRCUITPY drive)
──────────────────────         ───────────────────────────
kmk/                      →    lib/kmk/
kmk/modules/              →    lib/kmk/modules/
kmk/extensions/           →    lib/kmk/extensions/
code.py                   →    code.py          ← main entry point (CP looks here first)
kb.py                     →    kb.py            ← your keyboard definition
                               boot.py          ← optional USB/BLE boot config
                               lib/adafruit_ble/
                               lib/adafruit_hid/
```

### One-time setup

1. **Flash CircuitPython** — double-tap RST→GND to enter UF2 bootloader (drive appears as `NRF52BOOT`). Drag the `.uf2` from `circuitpython.org/board/supermini_nrf52840` onto it. It reboots as `CIRCUITPY`.

2. **Install Adafruit libraries** — download the CircuitPython library bundle from `circuitpython.org/libraries` matching your CP version. Copy into `CIRCUITPY/lib/`:
   - `adafruit_ble/` (folder)
   - `adafruit_hid/` (folder)

3. **Fork KMK on GitHub**, clone your fork locally in VS Code.

4. **Copy KMK source** — the `kmk/` folder from your local clone maps directly to `CIRCUITPY/lib/kmk/`.

5. **Copy your keyboard files** — `code.py` and `kb.py` to the root of `CIRCUITPY/`.

### Development iteration loop

```
Edit file in VS Code
      ↓
Copy changed file(s) to CIRCUITPY
      ↓
CircuitPython auto-restarts (visible in serial console)
      ↓
Read serial output for errors / debug prints
      ↓
Test on keyboard
```

Use **VS Code → Ctrl+Shift+P → "CircuitPython: Open Serial Console"** to see `print()` output and tracebacks. This is your primary debugging tool.

### Sync script (recommended for active development)

Instead of manually copying files, use a sync script. Run it after each save.

**Linux/macOS** (`sync.sh`):
```bash
#!/bin/bash
rsync -av --delete ./kmk/ /media/$USER/CIRCUITPY/lib/kmk/
cp code.py kb.py /media/$USER/CIRCUITPY/
```

**Windows** (PowerShell):
```powershell
robocopy .\kmk\ D:\lib\kmk\ /MIR /NFL /NDL
Copy-Item code.py, kb.py D:\
```
Replace `D:\` with the actual drive letter of your CIRCUITPY drive.

### If you hit MemoryError (optional, later step)
```bash
pip install mpy-cross
mpy-cross kmk/modules/ble_profiles.py   # → ble_profiles.mpy
# Copy the .mpy instead of the .py to the board
```

---

## 1. Project Status & Key Findings

**Current KMK BLE state (as of 2025-2026):**
- KMK is **on limited life support** — officially unmaintained; PRs/issues not being addressed. Any new feature is a fork/personal-branch effort.
- KMK supports a **single BLE HID connection** via `adafruit_ble` + `adafruit_hid`. The `BLEHID` class in `kmk/hid.py` creates one `HIDService`, one `ProvideServicesAdvertisement`, and one `BLERadio`.
- Keys `KC.BT_PRV`, `KC.BT_NXT`, `KC.BT_CLR` were **never implemented**; only `KC.BLE_REFRESH` (restart advertising) and `KC.BLE_DISCONNECT` exist.
- A critical upstream blocker was documented in **circuitpython issue #4639**: `HIDService` notifications are broadcast to **all** connected centrals simultaneously, with no per-connection routing at the `adafruit_ble` layer. This is the core architectural problem.
- The nRF52840 SoC *itself* supports up to **20 simultaneous BLE connections** in hardware (Nordic SoftDevice S140), but CircuitPython's `_bleio` exposes limited bonding/switching APIs.

**SuperMini compatibility:**
- The SuperMini has its own CircuitPython UF2 (`circuitpython.org/board/supermini_nrf52840`).
- It ships with the Adafruit nRF52 UF2 bootloader and SoftDevice S140 (same as nice!nano).
- Pin-compatible with nice!nano v1/v2 for all Pro Micro-footprint pins (minor exceptions: P1.01/02/07).
- All `adafruit_ble`, `adafruit_hid`, `_bleio` modules are available natively in the CircuitPython firmware.

---

## 1. The Core Problem in Detail

### 1a. How KMK's current BLE works

```
BLERadio (singleton)
  └─ HIDService (singleton, one set of GATT characteristics)
       └─ ProvideServicesAdvertisement (advertises the service)
            └─ adafruit_hid.Keyboard / Mouse / ConsumerControl
```

When connected, `hid.devices` sends HID reports via **notify** on the HID report characteristics. The SoftDevice broadcasts the notify to every subscribed central — there is no "send to connection X only" at this layer in CircuitPython.

### 1b. The ZMK approach (for reference)

ZMK uses Zephyr RTOS and the Zephyr BLE stack directly. Each profile slot is a full **separate identity** with its own IRK/CSRK bond keys stored in the flash NVS, its own advertising identity address, and its own GATT subscription state. Switching profiles involves:
1. Disconnecting the current central.
2. Loading the target profile's bond keys and identity address.
3. Re-advertising under the target identity.

Only one central is active at a time ("profile switching," not true simultaneous multi-host). This is important — **ZMK does not run simultaneous multi-host HID either**; it switches between bonded hosts one at a time.

### 1c. What the plan targets

The same behavior ZMK offers: **N bonded profiles** (default 3), all stored in flash, switchable via keypress. Only **one profile is active at a time**. When switching, the previous connection is cleanly dropped and the keyboard re-advertises under the new identity.

---

## 2. Hardware Prerequisites

| Item | Status |
|---|---|
| SuperMini nRF52840 | ✅ nRF52840 SoC with S140 SoftDevice, BLE 5.0 |
| CircuitPython for SuperMini | ✅ Official build at circuitpython.org/board/supermini_nrf52840 — use **9.x stable** |
| `_bleio` native module | ✅ Bundled in the SuperMini CP build |
| `adafruit_ble` library | ✅ Must be present in `/lib` (or frozen) |
| `adafruit_hid` library | ✅ Must be present in `/lib` (or frozen) |
| `nvm` module (flash storage) | ✅ Built into CircuitPython — used for profile slot persistence |
| Battery + VCC_OFF pin | Optional — P0.13 controls the MOSFET to cut peripheral power |

---

## 3. Library Analysis

### Libraries needed (already available in CP bundle)

| Library | Purpose | Already in KMK? |
|---|---|---|
| `adafruit_ble` | `BLERadio`, `BLEConnection`, `ProvideServicesAdvertisement` | Yes (imported in `hid.py`) |
| `adafruit_ble.services.standard.hid` | `HIDService` | Yes |
| `adafruit_hid` | `Keyboard`, `Mouse`, `ConsumerControl` | Yes |
| `_bleio` | Low-level: `_bleio.adapter`, `.Address`, `.Adapter.erase_bonding()` | Used indirectly |
| `nvm` / `microcontroller.nvm` | Persist active slot index across power cycles | No — needs adding |

### No new external libraries are required.
The key is using **existing APIs differently**, specifically:
- `_bleio.adapter.address = _bleio.Address(...)` — change the advertising MAC per slot.
- `_bleio.adapter.erase_bonding()` — wipe all bonds (for "forget" action).
- `microcontroller.nvm` — 256 bytes of non-volatile RAM on the nRF52840, perfect for storing the active slot index (1 byte) and slot names.
- `ble.connections` tuple — inspect and disconnect active connections.

---

## 4. Implementation Plan

### Phase 1 — Understand and audit existing `kmk/hid.py`

**File:** `kmk/hid.py`

Key class to study: `BLEHID`

```python
# Current structure (simplified):
class BLEHID(AbstractHID):
    def __init__(self, ble_name="KMK Keyboard", ...):
        self._ble = BLERadio()
        self._ble.name = ble_name
        self._hid_service = HIDService()
        self._advertisement = ProvideServicesAdvertisement(self._hid_service)
        self._advertisement.appearance = _BLE_APPEARANCE_HID_KEYBOARD
        self._keyboard = Keyboard(self._hid_service.devices)
        self._mouse = Mouse(self._hid_service.devices)
        self._consumer = ConsumerControl(self._hid_service.devices)
```

**What to note:**
- `HIDService` is a singleton bound at init. Its GATT characteristics are registered globally in the SoftDevice.
- `BLERadio.name` is what the host OS shows during pairing.
- Bond data is stored by the SoftDevice S140 in the bootloader's flash region — it uses the advertising address as the bond key. Changing the address effectively presents as a new device to hosts.

---

### Phase 2 — Design the `BLEProfileManager` module

Create a new file: `kmk/modules/ble_profiles.py`

**Responsibilities:**
- Track `N_PROFILES` slots (default: 3).
- Persist the active slot index in `microcontroller.nvm[0]`.
- On slot switch: disconnect → change `BLERadio.name` and `_bleio.adapter.address` → start advertising.
- Expose keycodes: `BT_SEL_0`, `BT_SEL_1`, `BT_SEL_2`, `BT_CLR`, `BT_NXT`, `BT_PRV`.

**Slot identity strategy:**
Each slot needs a unique, stable advertising address. Since the nRF52840 uses random static addresses, derive each slot's address from the chip's factory UID:

```python
import microcontroller
import _bleio

def _slot_address(slot_index: int) -> _bleio.Address:
    uid = microcontroller.cpu.uid  # 8-byte unique ID
    addr = bytearray(uid[:6])
    addr[5] = (addr[5] & 0x3F) | 0xC0  # set top 2 bits: random static
    addr[0] = (addr[0] & 0xF0) | (slot_index & 0x0F)  # vary low nibble per slot
    return _bleio.Address(bytes(addr), _bleio.Address.RANDOM_STATIC)
```

**Slot name strategy:**
```python
SLOT_NAMES = ["KMK BT 1", "KMK BT 2", "KMK BT 3"]
```

**Profile switch logic:**
```python
def switch_to_slot(self, slot: int):
    if slot == self._active_slot:
        return
    # 1. Disconnect current connection
    self._ble.stop_advertising()
    for conn in self._ble.connections:
        conn.disconnect()
    # 2. Change identity
    self._ble._adapter.address = _slot_address(slot)
    self._ble.name = SLOT_NAMES[slot]
    # 3. Persist
    microcontroller.nvm[0] = slot
    self._active_slot = slot
    # 4. Re-advertise
    self._ble.start_advertising(self._advertisement)
```

---

### Phase 3 — Modify `kmk/hid.py` — `BLEHID` class

**Changes needed:**

1. **Accept slot info at init:**
```python
class BLEHID(AbstractHID):
    def __init__(self, ble_name=None, active_slot=0, n_slots=3, ...):
        self._n_slots = n_slots
        self._active_slot = active_slot
        ...
```

2. **Read persisted slot from NVM at startup:**
```python
import microcontroller
_stored_slot = microcontroller.nvm[0]
if _stored_slot >= n_slots:
    _stored_slot = 0
self._active_slot = _stored_slot
```

3. **Set initial address from slot:**
```python
self._ble._adapter.address = _slot_address(self._active_slot)
self._ble.name = SLOT_NAMES[self._active_slot]
```

4. **Add `switch_profile(slot)` method** (delegates to `BLEProfileManager`).

5. **Add `clear_profile(slot)` method:**
   CircuitPython doesn't support erasing individual bond records (only `_bleio.adapter.erase_bonding()` which wipes all). For selective clear, change the slot's address to a new random address — the old bond on the host becomes stale:
```python
def clear_profile(self, slot: int):
    # Rotate the slot's address variation byte to invalidate old bond
    import os
    seed = int.from_bytes(os.urandom(1), 'little')
    # store the new seed in nvm[1 + slot] for persistence
    microcontroller.nvm[1 + slot] = seed
    self.switch_to_slot(slot)  # re-advertise under new address
```

---

### Phase 4 — Add BT keycodes to `kmk/keys.py`

```python
# BLE profile keys
make_key(names=('BT_SEL_0',), constructor=BLESelectKey, slot=0)
make_key(names=('BT_SEL_1',), constructor=BLESelectKey, slot=1)
make_key(names=('BT_SEL_2',), constructor=BLESelectKey, slot=2)
make_key(names=('BT_NXT',),   constructor=BLENextKey)
make_key(names=('BT_PRV',),   constructor=BLEPrevKey)
make_key(names=('BT_CLR',),   constructor=BLEClearKey)   # clear current slot
make_key(names=('BT_CLR_ALL',), constructor=BLEClearAllKey)  # erase_bonding()
```

Key handler (in `ble_profiles.py` extension):
```python
def on_press(self, key, keyboard, *args, **kwargs):
    if isinstance(key, BLESelectKey):
        keyboard.hid_pending = True
        self._hid.switch_to_slot(key.slot)
    elif isinstance(key, BLENextKey):
        next_slot = (self._hid._active_slot + 1) % self._hid._n_slots
        self._hid.switch_to_slot(next_slot)
    elif isinstance(key, BLEClearKey):
        self._hid.clear_profile(self._hid._active_slot)
    elif isinstance(key, BLEClearAllKey):
        import _bleio
        _bleio.adapter.erase_bonding()
        microcontroller.reset()
```

---

### Phase 5 — Visual feedback via the SuperMini LED

The SuperMini has a blue user LED (`board.LED`, `P0.15`) and a red charge LED. Use the user LED to signal the active slot:

```python
# In BLEProfileManager.switch_to_slot():
import digitalio, board, time

def _blink_slot(slot):
    led = digitalio.DigitalInOut(board.LED)
    led.direction = digitalio.Direction.OUTPUT
    for _ in range(slot + 1):
        led.value = True;  time.sleep(0.1)
        led.value = False; time.sleep(0.1)
    led.deinit()
```

For boards with NeoPixels, slot color could be: slot 0 = blue, slot 1 = green, slot 2 = red.

---

### Phase 6 — `kmk_keyboard.py` integration

The `BLEProfileManager` should register as a KMK **extension** (not a module), since it doesn't need to scan keys:

```python
# In user's main code.py:
from kmk.modules.ble_profiles import BLEProfiles

keyboard = KMKKeyboard()
ble_profiles = BLEProfiles(n_slots=3)
keyboard.extensions.append(ble_profiles)

keyboard.keymap = [
    [KC.BT_SEL_0, KC.BT_SEL_1, KC.BT_SEL_2, KC.BT_CLR, ...]
]
```

The extension hooks into `keyboard.go()` via the standard `during_bootup`, `before_matrix_scan`, and `after_hid_send` extension lifecycle methods.

---

### Phase 7 — NVM layout

Use `microcontroller.nvm` (256 bytes available on the nRF52840):

| NVM Byte | Content |
|---|---|
| `nvm[0]` | Active slot index (0–N) |
| `nvm[1]` | Slot 0 address seed (random, rotated on `BT_CLR`) |
| `nvm[2]` | Slot 1 address seed |
| `nvm[3]` | Slot 2 address seed |
| `nvm[4–7]` | Reserved |

---

## 5. Known Limitations & Mitigations

| Limitation | Detail | Mitigation |
|---|---|---|
| No per-bond erase | `_bleio.adapter.erase_bonding()` erases ALL bonds. | Rotate the slot's advertising address on `BT_CLR` — host's old bond becomes stale; host will prompt to re-pair. |
| HIDService broadcast bug (#4639) | If somehow two hosts connect simultaneously, both get keystrokes. | In this design only one is active at a time (sequential switching), so this is avoided by design. |
| Pairing on macOS/iOS | Apple requires HID connection interval to be a multiple of 15ms. Already handled by `adafruit_ble`. | No action needed. |
| Slow re-advertising | Disconnect + re-advertise takes ~0.5–1s. During this window, keystrokes are lost. | Set `hid_pending = True` and hold the current key state before switching. Drain the key queue first. |
| NVM byte 0 corruption | If NVM is 0xFF (factory state), default to slot 0. | `if nvm[0] > n_slots - 1: nvm[0] = 0` |
| KMK maintenance status | KMK is effectively unmaintained as of 2025. | Maintain as a personal fork; keep changes isolated to `kmk/modules/ble_profiles.py` and minimal edits to `hid.py`. |

---

## 6. File Change Summary

| File | Change Type | Description |
|---|---|---|
| `kmk/hid.py` | Modify | Add slot-aware init, `switch_to_slot()`, `clear_profile()`, NVM read on boot |
| `kmk/keys.py` | Add | Register `BT_SEL_0/1/2`, `BT_NXT`, `BT_PRV`, `BT_CLR`, `BT_CLR_ALL` keycodes |
| `kmk/modules/ble_profiles.py` | **New** | `BLEProfiles` extension: address derivation, slot switching, LED feedback, key handlers |
| `code.py` (user keyboard config) | Modify | Append `BLEProfiles()` to `keyboard.extensions`, add BT keys to keymap |
| No new library files needed | — | All required Adafruit libs are already in the CircuitPython bundle |

---

## 7. Testing Checklist

- [ ] Flash latest CircuitPython stable (9.x) to SuperMini via double-reset → UF2 bootloader
- [ ] Verify `adafruit_ble` and `adafruit_hid` are in `/lib`
- [ ] Confirm `microcontroller.nvm` is writable (should be by default)
- [ ] Pair slot 0 to PC, type test string ✓
- [ ] Press `BT_SEL_1`, verify advertising appears as new device on second host ✓
- [ ] Pair slot 1 to phone, type test string ✓
- [ ] Press `BT_SEL_0`, verify reconnection to PC ✓
- [ ] Press `BT_CLR`, verify host prompts re-pair (old bond invalidated) ✓
- [ ] Power cycle board, verify it reconnects to last active slot ✓
- [ ] Press `BT_CLR_ALL`, verify all hosts lose pairing ✓

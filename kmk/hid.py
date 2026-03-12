import supervisor
import usb_hid
from micropython import const

from struct import pack, pack_into

from kmk.keys import (
    Axis,
    ConsumerKey,
    KeyboardKey,
    ModifierKey,
    MouseKey,
    SixAxis,
    SpacemouseKey,
)
from kmk.scheduler import cancel_task, create_task
from kmk.utils import Debug, clamp

_BLE_AVAILABLE = False
try:
    import microcontroller

    from adafruit_ble import BLERadio
    from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
    from adafruit_ble.services.standard.hid import HIDService
    from storage import getmount

    _BLE_APPEARANCE_HID_KEYBOARD = const(961)
    _BLE_AVAILABLE = True
except ImportError:
    # BLE not supported on this platform
    pass


debug = Debug(__name__)


class HIDModes:
    NOOP = 0  # currently unused; for testing?
    USB = 1
    BLE = 2


_USAGE_PAGE_CONSUMER = const(0x0C)
_USAGE_PAGE_KEYBOARD = const(0x01)
_USAGE_PAGE_MOUSE = const(0x01)
_USAGE_PAGE_SIXAXIS = const(0x01)
_USAGE_PAGE_SYSCONTROL = const(0x01)

_USAGE_CONSUMER = const(0x01)
_USAGE_KEYBOARD = const(0x06)
_USAGE_MOUSE = const(0x02)
_USAGE_SIXAXIS = const(0x08)
_USAGE_SYSCONTROL = const(0x80)

_REPORT_SIZE_CONSUMER = const(2)
_REPORT_SIZE_KEYBOARD = const(8)
_REPORT_SIZE_KEYBOARD_NKRO = const(16)
_REPORT_SIZE_MOUSE = const(4)
_REPORT_SIZE_MOUSE_HSCROLL = const(5)
_REPORT_SIZE_SIXAXIS = const(12)
_REPORT_SIZE_SIXAXIS_BUTTON = const(2)
_REPORT_SIZE_SYSCONTROL = const(8)


def find_device(devices, usage_page, usage):
    for device in devices:
        if (
            device.usage_page == usage_page
            and device.usage == usage
            and hasattr(device, 'send_report')
        ):
            return device


class Report:
    def __init__(self, size):
        self.buffer = bytearray(size)
        self.pending = False

    def clear(self):
        for k, v in enumerate(self.buffer):
            if v:
                self.buffer[k] = 0x00
                self.pending = True

    def get_action_map(self):
        return {}


class KeyboardReport(Report):
    def __init__(self, size=_REPORT_SIZE_KEYBOARD):
        self.buffer = bytearray(size)
        self.prev_buffer = bytearray(size)

    @property
    def pending(self):
        return self.buffer != self.prev_buffer

    @pending.setter
    def pending(self, v):
        if v is False:
            self.prev_buffer[:] = self.buffer[:]

    def clear(self):
        for idx in range(len(self.buffer)):
            self.buffer[idx] = 0x00

    def add_key(self, key):
        # Find the first empty slot in the key report, and fill it; drop key if
        # report is full.
        idx = self.buffer.find(b'\x00', 2)

        if 0 < idx < _REPORT_SIZE_KEYBOARD:
            self.buffer[idx] = key.code

    def remove_key(self, key):
        idx = self.buffer.find(pack('B', key.code), 2)
        if 0 < idx:
            self.buffer[idx] = 0x00

    def add_modifier(self, modifier):
        self.buffer[0] |= modifier.code

    def remove_modifier(self, modifier):
        self.buffer[0] &= ~modifier.code

    def get_action_map(self):
        return {KeyboardKey: self.add_key, ModifierKey: self.add_modifier}


class NKROKeyboardReport(KeyboardReport):
    def __init__(self):
        super().__init__(_REPORT_SIZE_KEYBOARD_NKRO)

    def add_key(self, key):
        self.buffer[(key.code >> 3) + 1] |= 1 << (key.code & 0x07)

    def remove_key(self, key):
        self.buffer[(key.code >> 3) + 1] &= ~(1 << (key.code & 0x07))


class ConsumerControlReport(Report):
    def __init__(self):
        super().__init__(_REPORT_SIZE_CONSUMER)

    def add_cc(self, cc):
        pack_into('<H', self.buffer, 0, cc.code)
        self.pending = True

    def remove_cc(self):
        if self.buffer != b'\x00\x00':
            self.buffer = b'\x00\x00'
            self.pending = True

    def get_action_map(self):
        return {ConsumerKey: self.add_cc}


class PointingDeviceReport(Report):
    def __init__(self, size=_REPORT_SIZE_MOUSE):
        super().__init__(size)

    def add_button(self, key):
        self.buffer[0] |= key.code
        self.pending = True

    def remove_button(self, key):
        self.buffer[0] &= ~key.code
        self.pending = True

    def move_axis(self, axis):
        delta = clamp(axis.delta, -127, 127)
        axis.delta -= delta
        try:
            self.buffer[axis.code + 1] = 0xFF & delta
            self.pending = True
        except IndexError:
            if debug.enabled:
                debug(axis, ' not supported')

    def get_action_map(self):
        return {Axis: self.move_axis, MouseKey: self.add_button}


class HSPointingDeviceReport(PointingDeviceReport):
    def __init__(self):
        super().__init__(_REPORT_SIZE_MOUSE_HSCROLL)


class SixAxisDeviceReport(Report):
    def __init__(self, size=_REPORT_SIZE_SIXAXIS):
        super().__init__(size)

    def move_six_axis(self, axis):
        delta = clamp(axis.delta, -500, 500)
        axis.delta -= delta
        index = 2 * axis.code
        try:
            self.buffer[index] = 0xFF & delta
            self.buffer[index + 1] = 0xFF & (delta >> 8)
            self.pending = True
        except IndexError:
            if debug.enabled:
                debug(axis, ' not supported')

    def get_action_map(self):
        return {SixAxis: self.move_six_axis}


class SixAxisDeviceButtonReport(Report):
    def __init__(self, size=_REPORT_SIZE_SIXAXIS_BUTTON):
        super().__init__(size)

    def add_six_axis_button(self, key):
        self.buffer[0] |= key.code
        self.pending = True

    def remove_six_axis_button(self, key):
        self.buffer[0] &= ~key.code
        self.pending = True

    def get_action_map(self):
        return {SpacemouseKey: self.add_six_axis_button}


class IdentifiedDevice:
    def __init__(self, device, report_id):
        self.device = device
        self.report_id = report_id

    def send_report(self, buffer):
        self.device.send_report(buffer, self.report_id)


class AbstractHID:
    def __init__(self, **kwargs):
        self.report_map = {}
        self.device_map = {}
        self._setup_task = create_task(self.setup, period_ms=100)

    def __repr__(self):
        return self.__class__.__name__

    def create_report(self, keys):
        for report in self.device_map.keys():
            report.clear()

        for key in keys:
            if action := self.report_map.get(type(key)):
                action(key)

    def send(self):
        for report in self.device_map.keys():
            if report.pending:
                self.device_map[report].send_report(report.buffer)
                report.pending = False

    def setup(self):
        if not self.connected:
            return

        try:
            self.setup_keyboard_hid()
            self.setup_consumer_control()
            self.setup_mouse_hid()
            self.setup_sixaxis_hid()

            cancel_task(self._setup_task)
            self._setup_task = None
            if debug.enabled:
                self.show_debug()

        except OSError as e:
            if debug.enabled:
                debug(type(e), ':', e)

    def setup_keyboard_hid(self):
        if device := find_device(self.devices, _USAGE_PAGE_KEYBOARD, _USAGE_KEYBOARD):
            # bodgy NKRO autodetect
            try:
                report = KeyboardReport()
                device.send_report(report.buffer)
            except ValueError:
                report = NKROKeyboardReport()

            self.report_map.update(report.get_action_map())
            self.device_map[report] = device

    def setup_consumer_control(self):
        if device := find_device(self.devices, _USAGE_PAGE_CONSUMER, _USAGE_CONSUMER):
            report = ConsumerControlReport()
            self.report_map.update(report.get_action_map())
            self.device_map[report] = device

    def setup_mouse_hid(self):
        if device := find_device(self.devices, _USAGE_PAGE_MOUSE, _USAGE_MOUSE):
            # bodgy pointing device panning autodetect
            try:
                report = PointingDeviceReport()
                device.send_report(report.buffer)
            except ValueError:
                report = HSPointingDeviceReport()

            self.report_map.update(report.get_action_map())
            self.device_map[report] = device

    def setup_sixaxis_hid(self):
        if device := find_device(self.devices, _USAGE_PAGE_SIXAXIS, _USAGE_SIXAXIS):
            report = SixAxisDeviceReport()
            self.report_map.update(report.get_action_map())
            self.device_map[report] = IdentifiedDevice(device, 1)
            report = SixAxisDeviceButtonReport()
            self.report_map.update(report.get_action_map())
            self.device_map[report] = IdentifiedDevice(device, 3)

    def show_debug(self):
        for report in self.device_map.keys():
            debug('use ', report.__class__.__name__)


class USBHID(AbstractHID):
    @property
    def connected(self):
        return supervisor.runtime.usb_connected

    @property
    def devices(self):
        return usb_hid.devices


class BLEHID(AbstractHID):
    # Number of NVM bytes reserved for multi-profile slot seeds.
    # nvm[0] = active slot; nvm[1..n] = per-slot address-rotation seeds.
    _NVM_SLOT_BASE = 1

    def __init__(self, ble_name=None, **kwargs):
        if not _BLE_AVAILABLE:
            raise ImportError('adafruit_ble not available; install full library in /lib')
        super().__init__(**kwargs)

        # Multi-profile state.  BLEProfiles module overwrites _n_slots and
        # calls _apply_slot_identity() during its during_bootup() hook.
        self._n_slots = 1
        self._active_slot = 0
        self._ble_name_base = ble_name  # user-supplied base name (may be None)

        self.ble = BLERadio()
        self.ble.name = ble_name if ble_name else getmount('/').label
        self.ble_connected = False

        self.hid = HIDService()
        self.hid.protocol_mode = 0  # Boot protocol

        create_task(self.ble_monitor, period_ms=1000)

    @property
    def connected(self):
        return self.ble.connected

    @property
    def devices(self):
        return self.hid.devices

    def ble_monitor(self):
        if self.ble_connected != self.connected:
            self.ble_connected = self.connected
            if debug.enabled:
                if self.connected:
                    debug('BLE connected')
                else:
                    debug('BLE disconnected')

        if not self.connected:
            # Security-wise this is not right. While you're away someone turns
            # on your keyboard and they can pair with it nice and clean and then
            # listen to keystrokes.
            # On the other hand we don't have LESC so it's like shouting your
            # keystrokes in the air
            self.start_advertising()

    def clear_bonds(self):
        import _bleio

        _bleio.adapter.erase_bonding()

    def start_advertising(self):
        if not self.ble.advertising:
            advertisement = ProvideServicesAdvertisement(self.hid)
            advertisement.appearance = _BLE_APPEARANCE_HID_KEYBOARD

            self.ble.start_advertising(advertisement)

    def stop_advertising(self):
        self.ble.stop_advertising()

    # ------------------------------------------------------------------
    # Multi-profile helpers
    # ------------------------------------------------------------------

    def _slot_name(self, slot):
        '''Return the BLE advertised name for the given slot.'''
        base = self._ble_name_base if self._ble_name_base else getmount('/').label
        if self._n_slots > 1:
            return f'{base} {slot + 1}'
        return base

    def _slot_address(self, slot):
        '''
        Derive a stable random-static BLE address for *slot* from the chip UID
        and a per-slot rotation seed stored in NVM.

        Address byte layout (6 bytes, little-endian on the wire):
            addr[5] bits 7-6 = 0b11  → random static address type
            addr[0] bits 5-3 = slot index  (supports up to 8 slots)
            addr[0] bits 2-0 = rotation seed low 3 bits  (8 rotations per slot)
            remaining bits   = chip UID bytes (stable per device)
        '''
        uid = microcontroller.cpu.uid  # bytes, length ≥ 6 on nRF52840

        addr = bytearray(uid[:6])

        # Read the per-slot seed; default 0 on first use (0xFF → clamp to 0)
        try:
            seed = microcontroller.nvm[self._NVM_SLOT_BASE + slot]
            if seed == 0xFF:
                seed = 0
        except Exception:
            seed = 0

        # Encode slot index and seed into the least-significant byte
        addr[0] = (addr[0] & 0xC0) | ((slot & 0x07) << 3) | (seed & 0x07)
        # Force top two bits of MSB to 0b11 (random static address)
        addr[5] = (addr[5] & 0x3F) | 0xC0

        try:
            import _bleio

            return _bleio.Address(bytes(addr), _bleio.Address.RANDOM_STATIC)
        except Exception:
            return None

    def _apply_slot_identity(self, slot):
        '''
        Set the BLE adapter address and advertisement name for *slot*.

        Called by BLEProfiles.during_bootup() before advertising starts,
        and by switch_to_slot() when changing profiles.
        '''
        self._active_slot = slot

        # Change adapter address only when running in multi-slot mode
        if self._n_slots > 1:
            addr = self._slot_address(slot)
            if addr is not None:
                try:
                    self.ble._adapter.address = addr
                except Exception as e:
                    if debug.enabled:
                        debug('BLE address change failed:', e)

        self.ble.name = self._slot_name(slot)

        if debug.enabled:
            debug('BLE slot', slot, 'name', self.ble.name)

    def switch_to_slot(self, slot):
        '''
        Disconnect from the current host, change identity, and re-advertise
        so the keyboard can be paired with / reconnect to the *slot* host.
        '''
        if slot < 0 or slot >= self._n_slots:
            return

        if debug.enabled:
            debug('BLE switch_to_slot', slot)

        self.ble.stop_advertising()

        # Disconnect all active connections
        for conn in self.ble.connections:
            try:
                conn.disconnect()
            except Exception:
                pass

        # Persist the new active slot
        try:
            microcontroller.nvm[0] = slot
        except Exception:
            pass

        # Reset HID report state so setup() re-probes on new connection
        self.report_map.clear()
        self.device_map.clear()
        if self._setup_task is None:
            self._setup_task = create_task(self.setup, period_ms=100)

        self._apply_slot_identity(slot)
        self.start_advertising()

    def clear_profile(self, slot):
        '''
        Rotate the advertising address for *slot*, making any existing host
        bond stale.  The host will treat the keyboard as a new device and
        prompt to re-pair.

        CircuitPython does not support erasing individual bond records
        (_bleio.adapter.erase_bonding() wipes ALL bonds), so rotating the
        address is the practical workaround.
        '''
        if slot < 0 or slot >= self._n_slots:
            return

        # Increment the per-slot seed (wraps at 8; avoid 0xFF sentinel)
        try:
            current = microcontroller.nvm[self._NVM_SLOT_BASE + slot]
            if current == 0xFF:
                current = 0
            new_seed = (current + 1) & 0x07
            microcontroller.nvm[self._NVM_SLOT_BASE + slot] = new_seed
        except Exception:
            pass

        # Re-advertise under the rotated address (disconnects current host)
        self.switch_to_slot(slot)

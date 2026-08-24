# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Every keyword here is a thin wrapper over seine.tui.target's own action
# functions -- the same code '/target' and its AI tools already call, not
# a reimplementation for headless use. What is added is only the RF
# surface: keyword names a test author writes, and duration strings
# ('120s', '2 minutes') parsed the same way Robot's own WHILE 'limit' is,
# via robot.utils.timestr_to_secs -- one time syntax across the whole
# suite rather than a seine-specific one here and Robot's own elsewhere.

from robot.api.deco import keyword, library
from robot.utils import timestr_to_secs

def _seconds(value):
    return None if value is None else timestr_to_secs(value)

@library
class TargetLibrary:
    def __init__(self, context):
        self.context = context

    @keyword("Connect Target")
    def connect_target(self, agent=None):
        """Connects to the mtda agent 'agent' names, or the local config's own."""
        from seine.tui import target
        try:
            target.connect(self.context, agent or None)
        except target.Unavailable as e:
            raise RuntimeError(str(e))

    @keyword("Disconnect Target")
    def disconnect_target(self):
        from seine.tui import target
        target.disconnect(self.context)

    def _power(self, state):
        from seine.tui import target
        try:
            target.power(self.context, state)
        except target.Unavailable as e:
            raise RuntimeError(str(e))

    @keyword("Power On")
    def power_on(self):
        self._power("on")

    @keyword("Power Off")
    def power_off(self):
        self._power("off")

    @keyword("Power Toggle")
    def power_toggle(self):
        self._power("toggle")

    # No single mtda RPC for this -- off, a pause for the target to
    # actually lose power, then on. 'settle' is a duration string
    # ('2s' default), not a fixed sleep hidden from the test author: a
    # board with capacitors that hold power longer names its own.
    @keyword("Power Cycle")
    def power_cycle(self, settle="2s"):
        import time
        self._power("off")
        time.sleep(_seconds(settle))
        self._power("on")

    @keyword("USB Port")
    def usb_port(self, port, state):
        from seine.tui import target
        try:
            target.usb(self.context, port, state)
        except target.Unavailable as e:
            raise RuntimeError(str(e))

    # 'key' is mtda's own key-name syntax (X11 keysym names, e.g. 'Return',
    # 'Tab', 'a') -- passed through as-is rather than seine inventing a
    # second naming scheme for the same keys.
    @keyword("Keyboard Press")
    def keyboard_press(self, key, repeat=1, ctrl=False, shift=False, alt=False, meta=False):
        from seine.tui import target
        target.keyboard_press(self.context, key, repeat=int(repeat),
                              ctrl=ctrl, shift=shift, alt=alt, meta=meta)

    @keyword("Keyboard Write")
    def keyboard_write(self, text):
        """Types 'text' one key at a time -- for a login prompt with no console, say."""
        from seine.tui import target
        target.keyboard_write(self.context, text)

    # 'x'/'y' are absolute HID coordinates (0-32767, mtda.constants.MOUSE's
    # own MAX_X/MAX_Y), not screen pixels -- mtda's HID mouse reports
    # position this way regardless of the target's actual resolution, so
    # a test names a fraction of the screen (e.g. centre: 16384, 16384)
    # rather than a resolution it may not know.
    @keyword("Mouse Move")
    def mouse_move(self, x, y, buttons=0):
        """Moves the pointer to '(x, y)', 'buttons' held down (a bitmask, 0 for none)."""
        from seine.tui import target
        target.mouse_move(self.context, int(x), int(y), int(buttons))

    # A single mouse_move() only reports where the pointer is and which
    # buttons are held *right now* -- a real click is two HID reports,
    # button down then up, exactly like a physical mouse; nothing seen
    # in between is a "click" to whatever is listening on the target.
    # Bit values are the standard USB HID mouse report (button 1 = bit
    # 0, button 2 = bit 1, button 3 = bit 2) -- mtda passes 'buttons'
    # through as-is and names no constant of its own for them.
    _BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}

    @keyword("Mouse Click")
    def mouse_click(self, x, y, button="left", hold="100ms"):
        """Moves to '(x, y)' and clicks 'button' ('left'/'right'/'middle'), held for 'hold'."""
        import time
        from seine.tui import target
        bit = self._BUTTON_BITS.get(button)
        if bit is None:
            raise ValueError("'button' must be one of %s" % ", ".join(self._BUTTON_BITS))
        x, y = int(x), int(y)
        target.mouse_move(self.context, x, y, bit)
        time.sleep(_seconds(hold))
        target.mouse_move(self.context, x, y, 0)

    @keyword("Console Send")
    def console_send(self, data):
        """Sends 'data' to the console as-is (raw bytes/text, no escaping)."""
        from seine.tui import target
        target.console_send(self.context, data, raw=True)

    # Waits for mtda's configured prompt to know a command finished --
    # its default ('=> ') rarely matches a real shell. 'Console Prompt'
    # below fixes that; 'Console Send' + 'Console Wait' never depend on
    # it at all.
    @keyword("Console Run")
    def console_run(self, command):
        """Runs 'command' on the console and returns what it printed."""
        from seine.tui import target
        return target.console_run(self.context, command)

    @keyword("Console Prompt")
    def console_prompt(self, new_prompt=None):
        """Sets (or, with no argument, reads back) the prompt 'Console Run' waits for."""
        from seine.tui import target
        return target.console_prompt(self.context, new_prompt)

    # Truthy once 'pattern' appears, falsy on timeout -- not the matched
    # text (mtda's own protocol, not a seine choice; use 'Capture Screen'/
    # 'Console Tail' to read what is actually on the console).
    @keyword("Console Wait")
    def console_wait(self, pattern, timeout=None):
        """Waits for 'pattern' to appear; truthy if it did, falsy on timeout."""
        from seine.tui import target
        return target.console_wait(self.context, pattern, timeout=_seconds(timeout))

    @keyword("Console Dump")
    def console_dump(self):
        from seine.tui import target
        return target.console_dump(self.context)

    @keyword("Console Tail")
    def console_tail(self):
        from seine.tui import target
        return target.console_tail(self.context)

    @keyword("Target Status")
    def target_status(self):
        """Returns a dict: power/uptime/storage/usb, mtda-cli's own status shape."""
        from seine.tui import target
        return target.status(self.context)

    @keyword("Storage To Host")
    def storage_to_host(self):
        from seine.tui import target
        target.storage_to_host(self.context)

    @keyword("Storage To Target")
    def storage_to_target(self):
        from seine.tui import target
        target.storage_to_target(self.context)

    @keyword("Write Image To Target")
    def write_image_to_target(self, path):
        from seine.tui import target
        target.write_image(self.context, path)

    @keyword("Storage Snapshot")
    def storage_snapshot(self):
        from seine.tui import target
        target.snapshot(self.context)

    @keyword("Storage Rollback")
    def storage_rollback(self):
        from seine.tui import target
        target.rollback(self.context)

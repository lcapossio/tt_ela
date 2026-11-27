# SPDX-FileCopyrightText: © 2026 Leonardo Capossio
# SPDX-License-Identifier: Apache-2.0
"""
Hardware test for tt_um_fcapz_ela_tile on the Tiny Tapeout demo board.

Runs in MicroPython on the demo board's RP2040 with the Tiny Tapeout SDK
(tt-micropython-firmware). It repeats the cocotb tests from test/test.py
on the real chip.

Usage, from the board's REPL after copying this file to the board:

    >>> import fcapz_ela_board
    >>> fcapz_ela_board.run()

The RP2040 drives the project clock one edge at a time
(tt.clock_project_once), so every sample the analyzer takes lines up with
one value the script puts on ui_in[3:0]. No extra wiring is needed: the
RP2040 drives the probe inputs, the SPI pins and TRIG_IN itself.

ELA is also usable on its own, for example to capture real signals:

    >>> ela = fcapz_ela_board.setup()
    >>> hex(ela.read(0x0000))     # VERSION: 0x44c41
"""

PROJECT = "tt_um_fcapz_ela_tile"

# uio bits: the RP2040 drives SCLK, MOSI, CS_N and TRIG_IN, and reads MISO
# and BUSY. The tile sets uio_oe = 0x30, so the RP2040 side mirrors it.
SCLK, MOSI, CS_N, TRIG_IN = 0, 1, 2, 3
MISO, BUSY = 4, 5
UIO_OE_PICO = 0x0F

# uo_out bits
ARMED, TRIG_OUT = 4, 7

# SPI half period in project clocks. The tile needs SCLK <= clk/8.
HALF = 4

# ELA registers (fpgacapZero 0.4 register map subset)
VERSION = 0x0000
CTRL = 0x0004
STATUS = 0x0008
SAMPLE_W = 0x000C
DEPTH = 0x0010
PRETRIG = 0x0014
POSTTRIG = 0x0018
CAPTURE_LEN = 0x001C
TRIG_MODE = 0x0020
TRIG_VALUE = 0x0024
TRIG_MASK = 0x0028
BURST_PTR = 0x002C
FEATURES = 0x003C
NUM_CHAN = 0x00A4
TRIG_EXT = 0x00B4
NUM_SEGMENTS = 0x00B8
SEG_STATUS = 0x00BC
STARTUP_ARM = 0x00D8
COMPARE_CAPS = 0x00E0
DATA = 0x0100

CTRL_ARM, CTRL_RESET = 0x1, 0x2
ST_ARMED, ST_TRIGGERED, ST_DONE = 0x1, 0x2, 0x4

MODE_VALUE, MODE_CHANGE = 0x1, 0x2


class ELA:
    """Register access to the tile over bit-banged SPI, with manual clocking.

    tt is the DemoBoard object. Only these members are used: ui_in, uio_in,
    uo_out, uio_out (each through .value), clock_project_once() and
    reset_project().
    """

    def __init__(self, tt):
        self.tt = tt
        self.uio = 1 << CS_N
        self.probe = 0
        self.trig_out_seen = 0
        tt.uio_in.value = self.uio
        tt.ui_in.value = 0

    # ---- pins and clock ----

    def tick(self, n=1):
        """n project clock cycles. Counts TRIG_OUT pulses seen after each."""
        for _ in range(n):
            self.tt.clock_project_once()
            if (int(self.tt.uo_out.value) >> TRIG_OUT) & 1:
                self.trig_out_seen += 1

    def set_probe(self, value):
        self.probe = value & 0xF
        self.tt.ui_in.value = self.probe

    def _set(self, bit, val):
        if val:
            self.uio |= 1 << bit
        else:
            self.uio &= ~(1 << bit)
        self.tt.uio_in.value = self.uio

    def set_trig_in(self, val):
        self._set(TRIG_IN, val)

    def uo(self):
        return int(self.tt.uo_out.value)

    def reset(self):
        self.tt.reset_project(True)
        self.tick(10)
        self.tt.reset_project(False)
        self.tick(10)

    # ---- SPI frames ----

    def _frame(self, bits):
        self._set(CS_N, 0)
        self.tick(HALF)
        miso = 0
        for i, b in enumerate(bits):
            self._set(MOSI, b)
            self.tick(HALF)
            if i >= 17:  # MISO is only defined in the data phase
                miso = (miso << 1) | ((int(self.tt.uio_out.value) >> MISO) & 1)
            self._set(SCLK, 1)
            self.tick(HALF)
            self._set(SCLK, 0)
        self.tick(HALF)
        self._set(CS_N, 1)
        self.tick(HALF)
        return miso

    @staticmethod
    def _bits(value, width):
        return [(value >> i) & 1 for i in range(width - 1, -1, -1)]

    def write(self, addr, data):
        self._frame([1] + self._bits(addr, 16) + self._bits(data, 32))

    def read(self, addr):
        return self._frame([0] + self._bits(addr, 16) + [0] * 32)

    # ---- capture helpers ----

    def configure(self, pretrig, posttrig, mode, value, mask, ext=0):
        """Stop the analyzer and set up a capture. Arm it with CTRL = 1."""
        if not (0 <= pretrig and 0 <= posttrig and pretrig + posttrig <= 15):
            raise ValueError("need PRETRIG + POSTTRIG <= 15")
        # STARTUP_ARM = 0 first, so the CTRL reset leaves the analyzer idle
        # and a half-written trigger cannot fire while it is configured.
        self.write(STARTUP_ARM, 0)
        self.write(CTRL, CTRL_RESET)
        self.write(PRETRIG, pretrig)
        self.write(POSTTRIG, posttrig)
        self.write(TRIG_MODE, mode)
        self.write(TRIG_VALUE, value)
        self.write(TRIG_MASK, mask)
        self.write(TRIG_EXT, ext)

    def ramp(self, cycles, start=0):
        """4-bit counter on the probe, one step per clock."""
        count = start
        for _ in range(cycles):
            self.set_probe(count)
            self.tick(1)
            count = (count + 1) & 0xF

    def wait_done(self, polls=20):
        status = 0
        for _ in range(polls):
            status = self.read(STATUS)
            if status & ST_DONE:
                return status
        raise AssertionError("capture never completed, STATUS=0x%x" % status)

    def read_capture(self):
        n = self.read(CAPTURE_LEN)
        if not 1 <= n <= 16:
            raise AssertionError("CAPTURE_LEN %d out of range" % n)
        self.write(BURST_PTR, 0)  # what full-core host code does; no effect here
        return [self.read(DATA + 4 * i) for i in range(n)]


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _hexlist(samples):
    return " ".join("%x" % s for s in samples)


def _contiguous(samples):
    for i in range(1, len(samples)):
        _check(samples[i] == (samples[i - 1] + 1) & 0xF,
               "gap at sample %d: %s" % (i, _hexlist(samples)))


# ---- tests (same checks as test/test.py) ----

def test_registers(ela):
    ela.reset()
    _check(ela.read(SAMPLE_W) == 4, "SAMPLE_W != 4")
    _check(ela.read(DEPTH) == 16, "DEPTH != 16")
    v = ela.read(VERSION)
    _check(v & 0xFFFF == 0x4C41, "VERSION 0x%08x, core id not 0x4C41" % v)

    _check(ela.read(STARTUP_ARM) == 1, "STARTUP_ARM reset value != 1")
    ela.write(STARTUP_ARM, 0)
    _check(ela.read(STARTUP_ARM) == 0, "STARTUP_ARM write 0 failed")
    ela.write(STARTUP_ARM, 1)
    _check(ela.read(STARTUP_ARM) == 1, "STARTUP_ARM write 1 failed")

    ela.write(TRIG_MASK, 0xFFFFFFFF)
    _check(ela.read(TRIG_MASK) & 0xF == 0xF, "TRIG_MASK readback")
    ela.write(TRIG_VALUE, 0xFFFFFFF5)
    _check(ela.read(TRIG_VALUE) & 0xF == 0x5, "TRIG_VALUE readback")
    ela.write(PRETRIG, 3)
    ela.write(POSTTRIG, 9)
    _check(ela.read(PRETRIG) == 3, "PRETRIG readback")
    _check(ela.read(POSTTRIG) == 9, "POSTTRIG readback")

    ela.set_probe(0x5)
    ela.tick(2)
    _check(ela.uo() & 0xF == 0x5, "probe mirror on uo_out[3:0]")
    _check((int(ela.tt.uio_out.value) >> BUSY) & 1 == 0, "BUSY high while idle")


def test_id_registers(ela):
    ela.reset()
    expected = (
        (VERSION, 0x00044C41),
        (FEATURES, 0x00010141),
        (NUM_CHAN, 1),
        (NUM_SEGMENTS, 1),
        (SEG_STATUS, 0x80000000),
        (COMPARE_CAPS, 0x000A01C3),
    )
    for addr, value in expected:
        got = ela.read(addr)
        _check(got == value, "0x%04x: 0x%08x != 0x%08x" % (addr, got, value))


def test_startup_arm(ela):
    # Reset defaults: armed, PRETRIG = POSTTRIG = 0, value 0 under a full
    # mask. With the probe held at 0 the first sample triggers.
    ela.set_probe(0)
    ela.reset()
    status = ela.wait_done()
    _check(status & 0x7 == ST_TRIGGERED | ST_DONE, "STATUS=0x%x" % status)
    _check(ela.read(CAPTURE_LEN) == 1, "CAPTURE_LEN != 1")


def test_reset_probe_high(ela):
    # With the probe at 0xF through reset, the default trigger (value 0) must
    # wait for the probe to really read 0, and capture that 0.
    ela.set_probe(0xF)
    ela.trig_out_seen = 0
    ela.reset()
    ela.tick(20)
    status = ela.read(STATUS)
    _check(status & 0x7 == ST_ARMED, "STATUS=0x%x with probe 0xF" % status)
    _check(ela.trig_out_seen == 0, "TRIG_OUT pulsed with probe 0xF")
    ela.set_probe(0)
    ela.wait_done()
    _check(ela.trig_out_seen == 1, "TRIG_OUT high for %d clocks" % ela.trig_out_seen)
    samples = ela.read_capture()
    _check(samples == [0], "capture %s, expected 0" % _hexlist(samples))


def test_capture(ela):
    ela.reset()
    pretrig, posttrig, trig_value = 4, 7, 0xA
    ela.configure(pretrig, posttrig, MODE_VALUE, trig_value, 0xF)
    ela.write(CTRL, CTRL_ARM)
    _check(ela.uo() >> ARMED & 1, "ARMED low after arm")
    ela.ramp(40)
    ela.wait_done()
    samples = ela.read_capture()
    print("  samples:", _hexlist(samples))
    _check(len(samples) == pretrig + posttrig + 1, "CAPTURE_LEN %d" % len(samples))
    _check(samples[pretrig] == trig_value, "trigger sample %x" % samples[pretrig])
    _contiguous(samples)


def test_change_trigger(ela):
    ela.reset()
    pretrig, posttrig = 3, 4
    ela.configure(pretrig, posttrig, MODE_CHANGE, 0, 0x8)  # change on bit 3
    ela.write(CTRL, CTRL_ARM)
    ela.ramp(40)
    ela.wait_done()
    samples = ela.read_capture()
    print("  samples:", _hexlist(samples))
    _check(len(samples) == pretrig + posttrig + 1, "CAPTURE_LEN %d" % len(samples))
    _check((samples[pretrig] ^ samples[pretrig - 1]) & 0x8,
           "trigger sample did not change bit 3")
    _contiguous(samples)


def test_ext_trigger(ela):
    ela.reset()
    pretrig, posttrig = 2, 3
    # The value compare never matches the constant probe, so only TRIG_IN
    # (OR mode) can fire the trigger.
    ela.set_probe(0x3)
    ela.configure(pretrig, posttrig, MODE_VALUE, 0xC, 0xF, ext=1)
    ela.trig_out_seen = 0
    ela.write(CTRL, CTRL_ARM)
    ela.tick(50)
    status = ela.read(STATUS)
    _check(status & ST_ARMED and not status & ST_DONE, "STATUS=0x%x" % status)
    _check(ela.trig_out_seen == 0, "TRIG_OUT pulsed before TRIG_IN")

    ela.set_trig_in(1)
    ela.tick(4)
    ela.set_trig_in(0)
    ela.wait_done()
    _check(ela.trig_out_seen == 1, "TRIG_OUT high for %d clocks, expected 1"
           % ela.trig_out_seen)
    samples = ela.read_capture()
    print("  samples:", _hexlist(samples))
    _check(len(samples) == pretrig + posttrig + 1, "CAPTURE_LEN %d" % len(samples))
    _check(all(s == 0x3 for s in samples), "samples not all 3")


def test_ext_and(ela):
    # TRIG_EXT = AND: probe match and TRIG_IN on the same single clock.
    ela.reset()
    pretrig, posttrig, match, idle = 2, 3, 0xC, 0x3
    ela.set_probe(idle)
    ela.configure(pretrig, posttrig, MODE_VALUE, match, 0xF, ext=2)
    ela.write(CTRL, CTRL_ARM)
    ela.tick(10)
    ela.set_probe(match)
    ela.set_trig_in(1)
    ela.tick(1)
    ela.set_probe(idle)
    ela.set_trig_in(0)
    ela.wait_done()
    samples = ela.read_capture()
    print("  samples:", _hexlist(samples))
    _check(samples[pretrig] == match, "trigger sample %x" % samples[pretrig])
    _check(samples.count(match) == 1, "match captured more than once")


TESTS = (
    test_registers,
    test_id_registers,
    test_startup_arm,
    test_reset_probe_high,
    test_capture,
    test_change_trigger,
    test_ext_trigger,
    test_ext_and,
)


def run_tests(ela):
    """Run every test on an ELA object. Returns the number of failures."""
    failed = 0
    for test in TESTS:
        try:
            test(ela)
            print("PASS", test.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", test.__name__, "-", e)
    print("%d/%d tests passed" % (len(TESTS) - failed, len(TESTS)))
    return failed


def prepare(tt):
    """Set the RP2040 pin directions, idle the serial pins and return an ELA.

    Clocks the project a few times so the tile sees CS_N high before the
    first frame. Does not reset the project, so an earlier capture survives.
    """
    tt.uio_oe_pico.value = UIO_OE_PICO
    ela = ELA(tt)
    ela.tick(8)
    return ela


def setup():
    """Select the project on the demo board and return an ELA object."""
    from ttboard.demoboard import DemoBoard
    from ttboard.mode import RPMode

    tt = DemoBoard.get()
    if not tt.shuttle.has(PROJECT):
        raise RuntimeError(PROJECT + " is not on this chip (check the shuttle)")
    if getattr(tt.shuttle, PROJECT).enable() is False:
        raise RuntimeError("could not select " + PROJECT)
    tt.mode = RPMode.ASIC_RP_CONTROL
    tt.clock_project_stop()
    return prepare(tt)


def run():
    """Select the project and run all tests. Returns True if all pass."""
    return run_tests(setup()) == 0

# SPDX-FileCopyrightText: © 2026 Leonardo Capossio
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

# uio_in bits
SCLK, MOSI, CS_N, TRIG_IN = 0, 1, 2, 3
# uio_out bits
MISO = 4

# SCLK half period in clk cycles (SCLK = clk/16; the tile needs <= clk/8)
HALF = 8

# ELA registers (upstream register map)
ADDR_VERSION = 0x0000
ADDR_CTRL = 0x0004
ADDR_STATUS = 0x0008
ADDR_SAMPLE_W = 0x000C
ADDR_DEPTH = 0x0010
ADDR_PRETRIG = 0x0014
ADDR_POSTTRIG = 0x0018
ADDR_CAPTURE_LEN = 0x001C
ADDR_TRIG_MODE = 0x0020
ADDR_TRIG_VALUE = 0x0024
ADDR_TRIG_MASK = 0x0028
ADDR_BURST_PTR = 0x002C
ADDR_TRIG_EXT = 0x00B4
ADDR_STARTUP_ARM = 0x00D8
ADDR_DATA_OFFSET = 0x00F4
ADDR_CAPTURE_GEN = 0x00F8
ADDR_DATA_BASE = 0x0100

CTRL_ARM = 0x1
CTRL_RESET = 0x2
STATUS_ARMED = 0x1
STATUS_TRIGGERED = 0x2
STATUS_DONE = 0x4


class Host:
    """Bit-banged SPI mode 0 host on uio_in[2:0], MISO on uio_out[4]."""

    def __init__(self, dut):
        self.dut = dut
        self.uio = 1 << CS_N
        dut.uio_in.value = self.uio

    def _set(self, bit, val):
        if val:
            self.uio |= 1 << bit
        else:
            self.uio &= ~(1 << bit)
        self.dut.uio_in.value = self.uio

    async def _frame(self, bits):
        clk = self.dut.clk
        self._set(CS_N, 0)
        await ClockCycles(clk, HALF)
        miso = []
        for i, b in enumerate(bits):
            self._set(MOSI, b)
            await ClockCycles(clk, HALF)
            if i >= 17:  # MISO is only defined in the data phase
                miso.append((int(self.dut.uio_out.value) >> MISO) & 1)
            self._set(SCLK, 1)
            await ClockCycles(clk, HALF)
            self._set(SCLK, 0)
        await ClockCycles(clk, HALF)
        self._set(CS_N, 1)
        await ClockCycles(clk, HALF)
        return miso

    @staticmethod
    def _bits(value, width):
        return [(value >> i) & 1 for i in range(width - 1, -1, -1)]

    async def write(self, addr, data):
        await self._frame([1] + self._bits(addr, 16) + self._bits(data, 32))

    async def read(self, addr):
        miso = await self._frame([0] + self._bits(addr, 16) + [0] * 32)
        value = 0
        for b in miso:
            value = (value << 1) | b
        return value


async def reset(dut):
    clock = Clock(dut.clk, 20, unit="ns")  # 50 MHz
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    host = Host(dut)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 10)
    return host


@cocotb.test()
async def test_registers(dut):
    host = await reset(dut)

    # Back-to-back reads must each return their own register.
    assert await host.read(ADDR_SAMPLE_W) == 4
    assert await host.read(ADDR_DEPTH) == 16
    version = await host.read(ADDR_VERSION)
    dut._log.info(f"VERSION = 0x{version:08x}")
    assert version & 0xFFFF == 0x4C41  # ELA core id "LA"

    # Write / readback
    assert await host.read(ADDR_STARTUP_ARM) == 1
    await host.write(ADDR_STARTUP_ARM, 0)
    assert await host.read(ADDR_STARTUP_ARM) == 0
    await host.write(ADDR_STARTUP_ARM, 1)
    assert await host.read(ADDR_STARTUP_ARM) == 1

    # Writes keep the low bits of a 32-bit word (MSB-first frame)
    await host.write(ADDR_TRIG_MASK, 0xFFFFFFFF)
    assert await host.read(ADDR_TRIG_MASK) & 0xF == 0xF
    await host.write(ADDR_TRIG_VALUE, 0xFFFFFFF5)
    assert await host.read(ADDR_TRIG_VALUE) & 0xF == 0x5
    await host.write(ADDR_PRETRIG, 3)
    await host.write(ADDR_POSTTRIG, 9)
    assert await host.read(ADDR_PRETRIG) == 3
    assert await host.read(ADDR_POSTTRIG) == 9

    # Probe mirror and busy idle
    dut.ui_in.value = 0x5
    await ClockCycles(dut.clk, 2)
    assert int(dut.uo_out.value) & 0xF == 0x5
    assert (int(dut.uio_out.value) >> 5) & 1 == 0
    assert int(dut.uio_oe.value) == 0x30


async def drive_ramp(dut):
    """4-bit counter on probe_in, one step per clk."""
    count = 0
    while True:
        await FallingEdge(dut.clk)
        dut.ui_in.value = count
        count = (count + 1) & 0xF


async def configure(host, pretrig, posttrig, mode, value, mask, ext=0):
    await host.write(ADDR_CTRL, CTRL_RESET)
    await host.write(ADDR_PRETRIG, pretrig)
    await host.write(ADDR_POSTTRIG, posttrig)
    await host.write(ADDR_TRIG_MODE, mode)
    await host.write(ADDR_TRIG_VALUE, value)
    await host.write(ADDR_TRIG_MASK, mask)
    await host.write(ADDR_TRIG_EXT, ext)


async def wait_done(host, polls=20):
    for _ in range(polls):
        status = await host.read(ADDR_STATUS)
        if status & STATUS_DONE:
            return status
    assert False, f"capture never completed, STATUS=0x{status:x}"


async def read_capture(dut, host, expected_len):
    cap_len = await host.read(ADDR_CAPTURE_LEN)
    assert cap_len == expected_len
    await host.write(ADDR_BURST_PTR, 0)  # latch start pointer of the capture
    samples = [await host.read(ADDR_DATA_BASE + 4 * i) for i in range(cap_len)]
    dut._log.info("samples: " + " ".join(f"{s:x}" for s in samples))
    return samples


def assert_contiguous(samples):
    for i in range(1, len(samples)):
        assert samples[i] == (samples[i - 1] + 1) & 0xF, f"gap at sample {i}"


@cocotb.test()
async def test_capture(dut):
    host = await reset(dut)
    pretrig, posttrig, trig_value = 4, 7, 0xA

    await configure(host, pretrig, posttrig, 0x1, trig_value, 0xF)
    ramp = cocotb.start_soon(drive_ramp(dut))
    await host.write(ADDR_CTRL, CTRL_ARM)
    await wait_done(host)
    ramp.cancel()

    samples = await read_capture(dut, host, pretrig + posttrig + 1)
    assert samples[pretrig] == trig_value
    assert_contiguous(samples)


@cocotb.test()
async def test_startup_arm(dut):
    # Reset defaults: armed, PRETRIG = POSTTRIG = 0, value 0 under a full
    # mask. With the probe held at 0 the first sample triggers.
    host = await reset(dut)
    status = await wait_done(host)
    assert status & (STATUS_ARMED | STATUS_TRIGGERED | STATUS_DONE) == (
        STATUS_TRIGGERED | STATUS_DONE)
    assert await host.read(ADDR_CAPTURE_LEN) == 1


@cocotb.test()
async def test_change_trigger(dut):
    host = await reset(dut)
    pretrig, posttrig = 3, 4

    await configure(host, pretrig, posttrig, 0x2, 0, 0x8)  # change on bit 3
    ramp = cocotb.start_soon(drive_ramp(dut))
    await host.write(ADDR_CTRL, CTRL_ARM)
    await wait_done(host)
    ramp.cancel()

    samples = await read_capture(dut, host, pretrig + posttrig + 1)
    assert (samples[pretrig] ^ samples[pretrig - 1]) & 0x8, "trigger sample did not change bit 3"
    assert_contiguous(samples)


@cocotb.test()
async def test_ext_trigger(dut):
    host = await reset(dut)
    pretrig, posttrig = 2, 3

    # The value compare never matches the constant probe, so only TRIG_IN
    # (OR mode) can fire the trigger.
    dut.ui_in.value = 0x3
    await configure(host, pretrig, posttrig, 0x1, 0xC, 0xF, ext=1)

    pulses = 0

    async def count_trigger_out():
        nonlocal pulses
        prev = 0
        while True:
            await FallingEdge(dut.clk)
            cur = (int(dut.uo_out.value) >> 7) & 1
            pulses += cur & ~prev & 1
            prev = cur

    counter = cocotb.start_soon(count_trigger_out())
    await host.write(ADDR_CTRL, CTRL_ARM)
    await ClockCycles(dut.clk, 50)
    status = await host.read(ADDR_STATUS)
    assert status & STATUS_ARMED and not status & STATUS_DONE, f"STATUS=0x{status:x}"
    assert pulses == 0

    host._set(TRIG_IN, 1)
    await ClockCycles(dut.clk, 4)
    host._set(TRIG_IN, 0)
    await wait_done(host)
    counter.cancel()

    assert pulses == 1
    samples = await read_capture(dut, host, pretrig + posttrig + 1)
    assert all(s == 0x3 for s in samples)


@cocotb.test()
async def test_id_registers(dut):
    # Constant words a host uses to discover the core's capabilities.
    host = await reset(dut)
    expected = {
        ADDR_VERSION: 0x00054C41,  # fpgacapZero 0.5, core id "LA"
        0x003C: 0x00010141,  # FEATURES: 1 stage, ext trigger, 1 chan, 1 seg
        0x00A4: 1,           # NUM_CHAN
        0x00B8: 1,           # NUM_SEGMENTS
        0x00BC: 0x80000000,  # SEG_STATUS without segments
        0x00C4: 0,           # TIMESTAMP_W
        0x00D0: 0,           # PROBE_MUX_W
        0x00CC: 0,           # RLE_W: no run-length capture
        0x00E0: 0x007A01C3,  # COMPARE_CAPS: + DATA_OFFSET, CAPTURE_GEN, SQ_MODE
    }
    for addr, value in expected.items():
        got = await host.read(addr)
        assert got == value, f"0x{addr:04x}: 0x{got:08x} != 0x{value:08x}"


async def count_pulses(dut, bit, counter):
    """Count rising edges of uo_out[bit], sampled at every falling clk edge."""
    prev = 0
    while True:
        await FallingEdge(dut.clk)
        cur = (int(dut.uo_out.value) >> bit) & 1
        if cur and not prev:
            counter[0] += 1
        prev = cur


@cocotb.test()
async def test_reset_probe_high(dut):
    # Startup capture must not see a reset value: with the probe held at 0xF
    # through reset, the default trigger (value 0, mask 0xF) must not fire
    # until the probe really is 0.
    clock = Clock(dut.clk, 20, unit="ns")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0xF
    host = Host(dut)
    pulses = [0]
    counter = cocotb.start_soon(count_pulses(dut, 7, pulses))
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 20)

    status = await host.read(ADDR_STATUS)
    assert status & 0x7 == STATUS_ARMED, f"STATUS=0x{status:x}"
    assert pulses[0] == 0, "TRIG_OUT pulsed with the probe at 0xF"

    dut.ui_in.value = 0x0
    status = await wait_done(host)
    assert pulses[0] == 1
    assert await host.read(ADDR_CAPTURE_LEN) == 1
    assert await host.read(ADDR_DATA_BASE) == 0x0
    counter.cancel()


@cocotb.test()
async def test_ext_and_coincident(dut):
    # TRIG_EXT = AND: a probe match and a TRIG_IN pulse presented on the same
    # single clock must trigger, and mark that sample.
    host = await reset(dut)
    pretrig, posttrig, match, idle = 2, 3, 0xC, 0x3
    dut.ui_in.value = idle
    await configure(host, pretrig, posttrig, 0x1, match, 0xF, ext=2)
    await host.write(ADDR_CTRL, CTRL_ARM)
    await ClockCycles(dut.clk, 10)

    await FallingEdge(dut.clk)
    dut.ui_in.value = match
    host._set(TRIG_IN, 1)
    await FallingEdge(dut.clk)
    dut.ui_in.value = idle
    host._set(TRIG_IN, 0)

    await wait_done(host)
    samples = await read_capture(dut, host, pretrig + posttrig + 1)
    assert samples[pretrig] == match, f"trigger sample {samples[pretrig]:x}"
    assert samples.count(match) == 1


async def max_high_width(dut, bit, result):
    """Longest run of clocks with uo_out[bit] high, sampled at falling edges."""
    run = 0
    while True:
        await FallingEdge(dut.clk)
        run = run + 1 if (int(dut.uo_out.value) >> bit) & 1 else 0
        result[0] = max(result[0], run)


@cocotb.test()
async def test_trig_out_one_clock(dut):
    # TRIG_IN goes high and stays high at a point swept across the clock
    # where a CTRL re-arm commits. TRIG_OUT must never be high for more than
    # one clock, and the re-armed capture must complete.
    host = await reset(dut)
    for offset in range(0, 20):
        host._set(TRIG_IN, 0)
        dut.ui_in.value = 0x3
        await configure(host, 0, 3, 0x1, 0xC, 0xF, ext=1)  # only TRIG_IN fires
        await host.write(ADDR_CTRL, CTRL_ARM)
        await ClockCycles(dut.clk, 10)

        width = [0]
        monitor = cocotb.start_soon(max_high_width(dut, 7, width))

        async def raise_trig_in():
            await ClockCycles(dut.clk, offset)
            await FallingEdge(dut.clk)
            host._set(TRIG_IN, 1)

        # Re-arm frame; start the delay when the last bit is presented.
        bits = [1] + Host._bits(ADDR_CTRL, 16) + Host._bits(CTRL_ARM, 32)
        clk = dut.clk
        host._set(CS_N, 0)
        await ClockCycles(clk, HALF)
        for i, b in enumerate(bits):
            host._set(MOSI, b)
            if i == len(bits) - 1:
                cocotb.start_soon(raise_trig_in())
            await ClockCycles(clk, HALF)
            host._set(SCLK, 1)
            await ClockCycles(clk, HALF)
            host._set(SCLK, 0)
        await ClockCycles(clk, HALF)
        host._set(CS_N, 1)
        await ClockCycles(clk, 30)

        status = await host.read(ADDR_STATUS)
        monitor.cancel()
        assert width[0] == 1, f"offset {offset}: TRIG_OUT high for {width[0]} clocks"
        assert status & STATUS_DONE, f"offset {offset}: STATUS=0x{status:x}"
    host._set(TRIG_IN, 0)


async def drive_mod15(dut):
    """Probe counts 0..14 and wraps, so a 16-sample age error shows."""
    count = 0
    while True:
        await FallingEdge(dut.clk)
        dut.ui_in.value = count
        count = (count + 1) % 15


@cocotb.test()
async def test_full_depth(dut):
    host = await reset(dut)
    for pretrig, posttrig in ((15, 0), (0, 15), (7, 8)):
        trig_value = 0x9
        await configure(host, pretrig, posttrig, 0x1, trig_value, 0xF)
        ramp = cocotb.start_soon(drive_mod15(dut))
        await host.write(ADDR_CTRL, CTRL_ARM)
        await wait_done(host)
        ramp.cancel()
        samples = await read_capture(dut, host, 16)
        assert samples[pretrig] == trig_value, f"({pretrig},{posttrig}) trigger sample"
        for i in range(1, 16):
            assert samples[i] == (samples[i - 1] + 1) % 15, (
                f"({pretrig},{posttrig}) gap at {i}: {samples}")


@cocotb.test()
async def test_trig_mode_both(dut):
    """TRIG_MODE 3 fires on a value match OR a change (fpgacapZero 0.5.2)."""
    host = await reset(dut)
    await host.write(ADDR_STARTUP_ARM, 0)

    # Steady probe equal to TRIG_VALUE: mode 2 (change) must not fire,
    # mode 3 must.
    dut.ui_in.value = 0x6
    await configure(host, 0, 2, 0x2, 0x6, 0xF)
    await host.write(ADDR_CTRL, CTRL_ARM)
    await ClockCycles(dut.clk, 50)
    status = await host.read(ADDR_STATUS)
    assert status & STATUS_ARMED and not status & STATUS_TRIGGERED, f"STATUS=0x{status:x}"

    await configure(host, 0, 2, 0x3, 0x6, 0xF)
    await host.write(ADDR_CTRL, CTRL_ARM)
    await wait_done(host)
    samples = await read_capture(dut, host, 3)
    assert all(s == 0x6 for s in samples)

    # Probe never equal to TRIG_VALUE: mode 3 still fires on a change.
    dut.ui_in.value = 0x1
    await configure(host, 1, 1, 0x3, 0xF, 0xF)
    await host.write(ADDR_CTRL, CTRL_ARM)
    await ClockCycles(dut.clk, 50)
    status = await host.read(ADDR_STATUS)
    assert status & STATUS_ARMED and not status & STATUS_TRIGGERED, f"STATUS=0x{status:x}"
    dut.ui_in.value = 0x2
    await wait_done(host)
    samples = await read_capture(dut, host, 3)
    assert samples == [0x1, 0x2, 0x2], samples


@cocotb.test()
async def test_data_offset(dut):
    """DATA_OFFSET shifts DATA reads by whole samples (fpgacapZero 0.5)."""
    host = await reset(dut)
    pretrig, posttrig = 4, 7
    n = pretrig + posttrig + 1

    await configure(host, pretrig, posttrig, 0x1, 0xA, 0xF)
    ramp = cocotb.start_soon(drive_ramp(dut))
    await host.write(ADDR_CTRL, CTRL_ARM)
    await wait_done(host)
    ramp.cancel()
    samples = await read_capture(dut, host, n)
    assert await host.read(ADDR_DATA_OFFSET) == 0

    for offset in (1, 5, 11, 15):
        await host.write(ADDR_DATA_OFFSET, offset)
        assert await host.read(ADDR_DATA_OFFSET) == offset
        for i in range(16):
            got = await host.read(ADDR_DATA_BASE + 4 * i)
            want = samples[i + offset] if i + offset < n else 0
            assert got == want, f"offset {offset} DATA[{i}] = {got:x}, want {want:x}"

    # Offsets of 16 or more point past the buffer: every DATA read is 0.
    for value in (16, 0x13, 0xFFFFFFF0, 0x80000000):
        await host.write(ADDR_DATA_OFFSET, value)
        assert await host.read(ADDR_DATA_OFFSET) == 16
        assert await host.read(ADDR_DATA_BASE) == 0

    # A CTRL write without arm or reset keeps it; arm or reset clears it.
    await host.write(ADDR_DATA_OFFSET, 3)
    await host.write(ADDR_CTRL, 0)
    assert await host.read(ADDR_DATA_OFFSET) == 3
    assert await host.read(ADDR_DATA_BASE) == samples[3]
    await host.write(ADDR_CTRL, CTRL_RESET)
    assert await host.read(ADDR_DATA_OFFSET) == 0
    await host.write(ADDR_DATA_OFFSET, 3)
    await host.write(ADDR_CTRL, CTRL_ARM)
    assert await host.read(ADDR_DATA_OFFSET) == 0


@cocotb.test()
async def test_capture_gen(dut):
    """CAPTURE_GEN: +1 per CTRL arm/reset write and per reset release."""
    host = await reset(dut)
    assert await host.read(ADDR_CAPTURE_GEN) == 1  # the reset release

    steps = [
        (ADDR_CTRL, CTRL_ARM, 1),
        (ADDR_CTRL, CTRL_RESET, 1),
        (ADDR_CTRL, CTRL_ARM | CTRL_RESET, 1),  # one write counts once
        (ADDR_CTRL, 0, 0),
        (ADDR_CTRL, 0xFFFFFFFC, 0),             # no arm or reset bit
        (ADDR_STARTUP_ARM, 1, 0),
        (ADDR_CAPTURE_GEN, 0x1234, 0),          # read only
    ]
    gen = 1
    for addr, data, inc in steps:
        await host.write(addr, data)
        gen += inc
        got = await host.read(ADDR_CAPTURE_GEN)
        assert got == gen, f"after write 0x{data:x} to 0x{addr:04x}: {got} != {gen}"
        assert await host.read(ADDR_CAPTURE_GEN) == gen  # reads have no effect

    # Carry into bits [15:8], which MISO takes from the live read mux.
    while gen < 0x100:
        await host.write(ADDR_CTRL, CTRL_ARM)
        gen += 1
    assert await host.read(ADDR_CAPTURE_GEN) == 0x100

    # A chip reset clears it, then its release counts one.
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 10)
    assert await host.read(ADDR_CAPTURE_GEN) == 1

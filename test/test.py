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
        ADDR_VERSION: 0x00044C41,
        0x003C: 0x00010141,  # FEATURES: 1 stage, ext trigger, 1 chan, 1 seg
        0x00A4: 1,           # NUM_CHAN
        0x00B8: 1,           # NUM_SEGMENTS
        0x00BC: 0x80000000,  # SEG_STATUS without segments
        0x00C4: 0,           # TIMESTAMP_W
        0x00D0: 0,           # PROBE_MUX_W
        0x00E0: 0x000A01C3,  # COMPARE_CAPS
    }
    for addr, value in expected.items():
        got = await host.read(addr)
        assert got == value, f"0x{addr:04x}: 0x{got:08x} != 0x{value:08x}"

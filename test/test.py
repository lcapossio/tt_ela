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
ADDR_STARTUP_ARM = 0x00D8
ADDR_DATA_BASE = 0x0100

CTRL_ARM = 0x1
CTRL_RESET = 0x2
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


@cocotb.test()
async def test_capture(dut):
    host = await reset(dut)
    pretrig, posttrig, trig_value = 4, 7, 0xA

    await host.write(ADDR_CTRL, CTRL_RESET)
    await host.write(ADDR_PRETRIG, pretrig)
    await host.write(ADDR_POSTTRIG, posttrig)
    await host.write(ADDR_TRIG_MODE, 0x1)  # value match
    await host.write(ADDR_TRIG_VALUE, trig_value)
    await host.write(ADDR_TRIG_MASK, 0xF)
    ramp = cocotb.start_soon(drive_ramp(dut))
    await host.write(ADDR_CTRL, CTRL_ARM)

    for _ in range(20):
        status = await host.read(ADDR_STATUS)
        if status & STATUS_DONE:
            break
    else:
        assert False, f"capture never completed, STATUS=0x{status:x}"
    ramp.cancel()

    cap_len = await host.read(ADDR_CAPTURE_LEN)
    assert cap_len == pretrig + posttrig + 1

    await host.write(ADDR_BURST_PTR, 0)  # latch start pointer of the capture
    samples = [await host.read(ADDR_DATA_BASE + 4 * i) for i in range(cap_len)]
    dut._log.info("samples: " + " ".join(f"{s:x}" for s in samples))

    assert samples[pretrig] == trig_value
    for i in range(1, cap_len):
        assert samples[i] == (samples[i - 1] + 1) & 0xF, f"gap at sample {i}"

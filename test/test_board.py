# SPDX-FileCopyrightText: © 2026 Leonardo Capossio
# SPDX-License-Identifier: Apache-2.0
"""
Runs the demo board script (board/fcapz_ela_board.py) against the RTL.

The script is synchronous MicroPython. Here it runs in a thread via
cocotb.bridge, and a stand-in for the DemoBoard object turns each
clock_project_once() into one simulated clock cycle, the way the RP2040
clocks the real chip.
"""

import os
import sys

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "board"))
import fcapz_ela_board as board  # noqa: E402

try:  # cocotb >= 2.1 exports these at top level
    from cocotb import bridge, resume
except ImportError:
    from cocotb.task import bridge, resume


class _Port:
    def __init__(self, value=0):
        self.value = value


class FakeDemoBoard:
    """The parts of ttboard's DemoBoard that fcapz_ela_board uses."""

    def __init__(self, dut):
        self.dut = dut
        self.ui_in = _Port()
        self.uio_in = _Port()
        self.uo_out = _Port()
        self.uio_out = _Port()
        self.uio_oe_pico = _Port(0)  # board.prepare() must set it
        # Held in reset until the script's first reset_project(False): the
        # flops have no defined power-up value, and simulation shows that as X.
        self.rst_n = 0

        @resume
        async def cycle():
            # Inputs change while clk is low, like the RP2040 between ticks.
            dut.ui_in.value = self.ui_in.value
            pico_oe = self.uio_oe_pico.value
            dut.uio_in.value = self.uio_in.value & pico_oe
            dut.rst_n.value = self.rst_n
            await Timer(10, unit="ns")
            dut.clk.value = 1
            await Timer(10, unit="ns")
            dut.clk.value = 0
            await Timer(1, unit="ns")
            self.uo_out.value = int(dut.uo_out.value)
            # Resolve the shared uio pins: each bit is driven by the RP2040
            # or by the chip, never both.
            chip_oe = int(dut.uio_oe.value)
            assert pico_oe & chip_oe == 0, (
                f"uio driven by both sides: pico 0x{pico_oe:02x}, chip 0x{chip_oe:02x}")
            self.uio_out.value = ((self.uio_in.value & pico_oe)
                                  | (int(dut.uio_out.value) & chip_oe))

        self._cycle = cycle

    def clock_project_once(self):
        self._cycle()

    def reset_project(self, in_reset):
        self.rst_n = 0 if in_reset else 1


@cocotb.test()
async def test_board_script(dut):
    dut.ena.value = 1
    dut.clk.value = 0
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await Timer(10, unit="ns")

    tt = FakeDemoBoard(dut)

    @bridge
    def run():
        return board.run_tests(board.prepare(tt))

    failed = await run()
    assert failed == 0, f"{failed} board test(s) failed"

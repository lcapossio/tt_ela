<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This is the embedded logic analyzer (ELA) core from [fpgacapZero](https://github.com/lcapossio/fpgacapZero), built for a 4-bit probe and a 16-sample capture buffer. It samples `ui_in[3:0]` on every clock into a ring buffer. A trigger can fire on a value match (masked compare) or on any change of the masked bits, optionally combined (OR or AND) with the external `TRIG_IN` pin. The buffer keeps a configurable number of samples from before the trigger, then fills the rest after it.

A host reads and writes the analyzer's 32-bit registers over a SPI-style serial interface on the bidirectional pins. The register map is the upstream fpgacapZero ELA map. After reset the analyzer arms itself (`STARTUP_ARM=1`), and `ARMED` shows when it is waiting for a trigger. `TRIG_OUT` pulses for one clock when the trigger fires.

## How to test

### Serial protocol

SPI mode 0, MSB first. The host changes `MOSI` while `SCLK` is low, and both sides sample on the rising edge of `SCLK`. Every input goes through a 2-flop synchronizer, so **`SCLK` must be at most clk/8** (6.25 MHz at a 50 MHz clock).

Pull `CS_N` low, clock 49 bits, then raise `CS_N`:

| Bits  | Content |
|-------|---------|
| 0     | 1 = write, 0 = read |
| 1-16  | register address |
| 17-48 | write: data on `MOSI`. Read: data on `MISO` |

Clocks after bit 48 are ignored until `CS_N` rises. `BUSY` is high while a frame is in progress.

### Main registers

| Address | Name | Notes |
|---------|------|-------|
| 0x0000 | VERSION | `[15:0]` = 0x4C41 ("LA"), `[31:16]` = core version |
| 0x0004 | CTRL | write bit 0 = arm, bit 1 = reset capture |
| 0x0008 | STATUS | bit 0 armed, bit 1 triggered, bit 2 done, bit 3 overflow |
| 0x000C / 0x0010 | SAMPLE_W / DEPTH | read 4 and 16 |
| 0x0014 / 0x0018 | PRETRIG / POSTTRIG | samples kept before / after the trigger sample |
| 0x001C | CAPTURE_LEN | samples in the finished capture (PRETRIG + POSTTRIG + 1) |
| 0x0020 | TRIG_MODE | bit 0 value match, bit 1 change detect |
| 0x0024 / 0x0028 | TRIG_VALUE / TRIG_MASK | compare value and mask |
| 0x002C | BURST_PTR | write any value to latch the finished capture for readout |
| 0x00B4 | TRIG_EXT | `TRIG_IN` use: 0 = ignored (default), 1 = OR with internal trigger, 2 = AND |
| 0x00D8 | STARTUP_ARM | bit 0: arm automatically after reset |
| 0x0100 + 4*i | DATA | sample i of the capture, oldest first |

### Capturing

1. Write CTRL = 0x2 to reset, then set PRETRIG, POSTTRIG, TRIG_MODE, TRIG_VALUE and TRIG_MASK (for example PRETRIG = 4, POSTTRIG = 7, TRIG_MODE = 1, TRIG_VALUE = 0xA, TRIG_MASK = 0xF).
2. Write CTRL = 0x1 to arm, and drive the signals to capture on `ui_in[3:0]`.
3. Poll STATUS until bit 2 (done) is set.
4. Read CAPTURE_LEN, write BURST_PTR, then read DATA at 0x0100 + 4*i for i = 0 .. CAPTURE_LEN-1. Write BURST_PTR only after done, or the readout uses a stale start point. The trigger sample is at index PRETRIG.

## External hardware

None required. Any microcontroller with GPIO can act as the host, including the RP2040 on the Tiny Tapeout demo board. It needs four pins: `SCLK`, `MOSI` and `CS_N` as outputs, `MISO` as an input.

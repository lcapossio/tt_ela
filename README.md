![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# fcapz ELA tile

The [fpgacapZero](https://github.com/lcapossio/fpgacapZero) embedded logic analyzer on Tiny Tapeout: a 4-bit probe, a 16-sample capture buffer, value-match or change triggers, and register access over a SPI-style serial interface.

- [Project documentation](docs/info.md)

## Layout

This is the 1x1 version, which is the one submitted. The full upstream core needs a 2x2 tile. That version is kept at the `full-2x2` tag.

- `src/tt_um_fcapz_ela_tile.v`: Tiny Tapeout top level and SPI-to-register bridge.
- `src/fcapz_ela_mini.v`: compact reimplementation of the ELA for one tile. It follows a subset of the fpgacapZero 0.4 register map with the same capture behavior. See [docs/info.md](docs/info.md) for what is left out.
- `upstream/`: fpgacapZero as a git submodule, for reference. The build only uses `src/`.
- `test/`: cocotb testbench (registers, value, change and external triggers, startup arm). The same tests pass on the full core at the `full-2x2` tag. See [test/README.md](test/README.md).
- `test/board/fcapz_ela_board.py`: MicroPython self-test for the chip on the Tiny Tapeout demo board. See "How to test" in [docs/info.md](docs/info.md). CI also runs it against the RTL.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Resources

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Learn how semiconductors work](https://tinytapeout.com/siliwiz/)
- [Join the community](https://tinytapeout.com/discord)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)

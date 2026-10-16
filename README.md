![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# fcapz ELA tile

The [fpgacapZero](https://github.com/lcapossio/fpgacapZero) embedded logic analyzer on Tiny Tapeout: a 4-bit probe, a 16-sample capture buffer, value-match or change triggers, and register access over a SPI-style serial interface.

- [Project documentation](docs/info.md)

## Layout

- `src/tt_um_fcapz_ela_tile.v`: Tiny Tapeout top level and SPI-to-register bridge.
- `src/fcapz_ela.v`, `src/trig_compare.v`, `src/dpram.v`, `src/fcapz_version.vh`: ELA core, unmodified copies of `upstream/rtl`.
- `upstream/`: fpgacapZero as a git submodule, pinned to the commit `src/` was copied from. The build only uses `src/`.
- `test/`: cocotb testbench (register access and a full capture with readback). See [test/README.md](test/README.md).

To update the core, move the `upstream/` submodule to the new commit and copy those four files from `upstream/rtl` into `src/`.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Resources

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Learn how semiconductors work](https://tinytapeout.com/siliwiz/)
- [Join the community](https://tinytapeout.com/discord)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)

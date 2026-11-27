// SPDX-License-Identifier: Apache-2.0
// Tiny Tapeout 1x1 tile: compact fpgacapZero-compatible ELA (fcapz_ela_mini)
// behind a SPI-style register interface on uio[].
//
// Pinout:
//   ui_in [3:0] = probe_in[3:0]   (signals being captured)
//   ui_in [7:4] = unused
//   uo_out[3:0] = live mirror of probe_in[3:0]
//   uo_out[4]   = armed
//   uo_out[6:5] = unused (0)
//   uo_out[7]   = trigger_out (one-clock pulse when the trigger fires)
//   uio_in[0]   = SCLK  (host shift clock, synchronised into clk)
//   uio_in[1]   = MOSI
//   uio_in[2]   = CS_N  (frame, active low)
//   uio_in[3]   = external trigger_in
//   uio_out[4]  = MISO
//   uio_out[5]  = busy  (frame in progress)
//
// Frame (SPI mode 0, MSB first, host samples MISO and the tile samples MOSI
// on SCLK rising edges). SCLK must be at most clk/8 because every input passes
// through a 2-FF synchroniser:
//   bit  0     : 1 = write, 0 = read
//   bits 1..16 : register address
//   bits 17..48: write data (write) or read data on MISO (read)
// Bits beyond 48 are ignored until CS_N rises.
//
// Register map: subset of the fpgacapZero 0.5 ELA map (see fcapz_ela_mini.v).
// Captured samples are at 0x0100 + 4*i (i = 0 .. CAPTURE_LEN-1, oldest
// first) once STATUS.done is set.

`default_nettype none
`timescale 1ns/1ps

module tt_um_fcapz_ela_tile (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

    wire rst = ~rst_n;

    // ---------------- host bridge ----------------
    // SCLK, MOSI and CS_N are asynchronous to clk. All three go through
    // equal-depth 2-FF synchronisers so MOSI stays aligned with the detected
    // SCLK edge.
    reg [1:0] sclk_s, mosi_s, cs_s;
    reg       sclk_d;
    always @(posedge clk) begin
        sclk_s <= {sclk_s[0], uio_in[0]};
        mosi_s <= {mosi_s[0], uio_in[1]};
        cs_s   <= {cs_s[0],   uio_in[2]};
        sclk_d <= sclk_s[1];
    end
    wire sclk_rise = sclk_s[1] & ~sclk_d;
    wire mosi      = mosi_s[1];
    wire cs_n      = cs_s[1];

    reg [5:0] bitcnt;      // saturates at 49 (end of frame)
    reg       rw;
    reg       addr_hi;     // any of address bits [15:9] set
    reg [8:0] addr;
    reg [3:0] wdata;       // last 4 data bits = write data [3:0] (MSB-first frame)
    reg       wdata_hi;    // any write data bit [31:4] set
    reg [7:0] rd_snap;     // read data [7:0], frozen for the frame
    reg       wr_pulse, rd_pulse;
    reg       busy;

    wire [31:0] core_rdata;

    always @(posedge clk) begin
        wr_pulse <= 1'b0;
        rd_pulse <= 1'b0;

        if (rst) begin
            bitcnt <= 0; rw <= 0; addr_hi <= 0; addr <= 0; wdata <= 0;
            wdata_hi <= 0;
            rd_snap <= 0; busy <= 0;
        end else if (cs_n) begin
            bitcnt <= 0;
            busy   <= 0;
        end else if (sclk_rise && bitcnt != 6'd49) begin
            if (bitcnt == 0) begin
                rw      <= mosi;
                addr_hi <= 1'b0;   // the old address would shift into addr_hi
                addr    <= 9'd0;
                wdata_hi <= 1'b0;
            end else if (bitcnt <= 16) begin
                {addr_hi, addr} <= {addr_hi | addr[8], addr[7:0], mosi};
            end else begin
                wdata <= {wdata[2:0], mosi};
                // bits 17..44 carry data bits [31:4]
                if (bitcnt <= 44) wdata_hi <= wdata_hi | mosi;
            end
            bitcnt <= bitcnt + 1'b1;

            if (rw && bitcnt == 48)  wr_pulse <= 1'b1;
            if (!rw && bitcnt == 16) rd_pulse <= 1'b1;
            busy <= 1'b1;
        end

        // The address is complete when rd_pulse is high; the core's read
        // mux is combinational. Bits [31:8] cannot change during a read
        // frame (they depend on the address and on CAPTURE_GEN, which moves
        // only on a register write or a reset), so only [7:0] is frozen.
        if (rd_pulse) rd_snap <= core_rdata[7:0];
    end

    // Bit 31 is presented after the bit-16 edge, bit 0 after the bit-47 edge.
    wire       data_phase = !cs_n && bitcnt >= 6'd17 && bitcnt <= 6'd48;
    wire [4:0] rd_idx     = 5'd16 - bitcnt[4:0];   // 48 - bitcnt, mod 32
    wire       miso       = data_phase && (rd_idx[4:3] != 2'b00 ? core_rdata[rd_idx]
                                                                : rd_snap[rd_idx[2:0]]);

    // ---------------- ELA core ----------------
    wire armed_w, trigger_out_w;

    fcapz_ela_mini u_ela (
        .clk         (clk),
        .rst         (rst),
        .probe_in    (ui_in[3:0]),
        .trigger_in  (uio_in[3]),       // synchronised inside core
        .trigger_out (trigger_out_w),
        .armed_out   (armed_w),
        .reg_wr      (wr_pulse),
        .reg_addr_hi (addr_hi),
        .reg_addr    (addr),
        .reg_wdata   (wdata),
        .reg_wdata_hi(wdata_hi),
        .reg_rdata   (core_rdata)
    );

    // ---------------- pin mapping ----------------
    assign uo_out  = { trigger_out_w, 2'b00, armed_w, ui_in[3:0] };
    assign uio_out = { 2'b00, busy, miso, 4'b0000 };
    assign uio_oe  = 8'b0011_0000;  // miso(4), busy(5) drive out; rest in

    // tie-offs to silence lint
    wire _unused = &{ena, ui_in[7:4], uio_in[7:4], 1'b0};

endmodule

`default_nettype wire

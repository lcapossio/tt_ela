// SPDX-License-Identifier: Apache-2.0
// Tiny Tapeout wrapper around the fpgacapZero ELA core (fcapz_ela).
//
// Instantiates upstream rtl/fcapz_ela.v (copied unmodified into src/) at a
// small parameter set and bridges a SPI-style serial interface on uio[] to the
// core's register bus. No vendor TAP, no BRAM; jtag_clk is tied to clk.
//
// Pinout:
//   ui_in [3:0] = probe_in[3:0]   (signals being captured)
//   ui_in [7:4] = unused
//   uo_out[3:0] = live mirror of probe_in[3:0]
//   uo_out[4]   = armed
//   uo_out[6:5] = unused (0)
//   uo_out[7]   = trigger_out (from core)
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
// Register map is the upstream ELA map. Captured samples are read from the
// DATA window at 0x0100 + 4*i (i = 0 .. CAPTURE_LEN-1, oldest first) after a
// write to BURST_PTR (0x002C) latches the start pointer of the finished capture.
// The window is served here through the core's burst read port, so the core's
// own USER1 data window is compiled out.

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

    localparam SAMPLE_W = 4;
    localparam DEPTH    = 16;
    localparam AW       = $clog2(DEPTH);

    localparam [15:0] ADDR_DATA_BASE = 16'h0100;

    wire rst = ~rst_n;

    // ---------------- probe + ext trigger ----------------
    wire [SAMPLE_W-1:0] probe_in    = ui_in[3:0];
    wire                trigger_in  = uio_in[3];   // synchronised inside core
    wire                trigger_out_w;

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

    reg [5:0]  bitcnt;      // saturates at 49 (end of frame)
    reg        rw;
    reg [15:0] addr_sr;
    reg [31:0] shift_sr;    // write data in, read data out
    reg        wr_pulse, rd_pulse, rd_pulse_d;
    reg        busy;

    wire [31:0]         core_rdata;
    wire [SAMPLE_W-1:0] burst_rd_data;
    wire                in_datawin = (addr_sr >= ADDR_DATA_BASE);
    wire [31:0]         rd_word    = in_datawin
                                   ? {{(32-SAMPLE_W){1'b0}}, burst_rd_data}
                                   : core_rdata;

    always @(posedge clk) begin
        wr_pulse   <= 1'b0;
        rd_pulse   <= 1'b0;
        rd_pulse_d <= rd_pulse;

        if (rst) begin
            bitcnt <= 0; rw <= 0; addr_sr <= 0; shift_sr <= 0; busy <= 0;
        end else if (cs_n) begin
            bitcnt <= 0;
            busy   <= 0;
        end else if (sclk_rise && bitcnt != 6'd49) begin
            if (bitcnt == 0)         rw       <= mosi;
            else if (bitcnt <= 16)   addr_sr  <= {addr_sr[14:0], mosi};
            else                     shift_sr <= {shift_sr[30:0], mosi};
            bitcnt <= bitcnt + 1'b1;

            if (rw && bitcnt == 48)  wr_pulse <= 1'b1;
            if (!rw && bitcnt == 16) rd_pulse <= 1'b1;
            busy <= 1'b1;
        end

        // jtag_rdata and the burst read data are registered in the core, so
        // they are valid one cycle after rd_pulse.
        if (rd_pulse_d) shift_sr <= rd_word;
    end
    wire miso = shift_sr[31];

    // ---------------- ELA core ----------------
    // DATA window sample i lives at ring address start_ptr + i (mod DEPTH);
    // ADDR_DATA_BASE is DEPTH*4-aligned, so addr_sr[AW+1:2] is i mod DEPTH.
    wire [AW-1:0]       burst_start_ptr;
    wire [AW-1:0]       burst_rd_addr = burst_start_ptr + addr_sr[AW+1:2];
    wire [0:0]          burst_rd_ts_data;   // TIMESTAMP_W=0 -> width 1 stub
    wire                burst_start;
    wire                burst_timestamp;
    wire                armed_w;

    fcapz_ela #(
        .SAMPLE_W      (SAMPLE_W),
        .DEPTH         (DEPTH),
        .TRIG_STAGES   (1),
        .STOR_QUAL     (0),
        .NUM_CHANNELS  (1),
        .INPUT_PIPE    (0),
        .DECIM_EN      (0),
        .EXT_TRIG_EN   (1),
        .TIMESTAMP_W   (0),
        .NUM_SEGMENTS  (1),
        .PROBE_MUX_W   (0),
        .STARTUP_ARM   (1),   // auto-arm after reset (no host needed)
        .DEFAULT_TRIG_EXT(0),
        .REL_COMPARE   (0),   // ==/!= only
        .DUAL_COMPARE  (0),   // single comparator A
        .USER1_DATA_EN (0)    // DATA window served by the bridge via burst port
    ) u_ela (
        .sample_clk (clk),
        .sample_rst (rst),
        .probe_in   (probe_in),
        .trigger_in (trigger_in),
        .trigger_out(trigger_out_w),
        .armed_out  (armed_w),
        .jtag_clk   (clk),
        .jtag_rst   (rst),
        .jtag_wr_en (wr_pulse),
        .jtag_rd_en (rd_pulse),
        .jtag_addr  (addr_sr),
        .jtag_wdata (shift_sr),
        .jtag_rdata (core_rdata),
        .burst_rd_active (in_datawin),
        .burst_rd_addr   (burst_rd_addr),
        .burst_rd_data   (burst_rd_data),
        .burst_rd_ts_data(burst_rd_ts_data),
        .burst_start     (burst_start),
        .burst_timestamp (burst_timestamp),
        .burst_start_ptr (burst_start_ptr)
    );

    // ---------------- pin mapping ----------------
    assign uo_out  = { trigger_out_w, 2'b00, armed_w, probe_in };
    assign uio_out = { 2'b00, busy, miso, 4'b0000 };
    assign uio_oe  = 8'b0011_0000;  // miso(4), busy(5) drive out; rest in

    // tie-offs to silence lint
    wire _unused = &{ena, ui_in[7:4], uio_in[7:4], burst_rd_ts_data,
                     burst_start, burst_timestamp, 1'b0};

endmodule

`default_nettype wire

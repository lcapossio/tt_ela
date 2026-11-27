// SPDX-License-Identifier: Apache-2.0
// Compact single-clock reimplementation of the fpgacapZero ELA for one
// Tiny Tapeout tile: 4-bit probe, 16-sample buffer, one comparator.
//
// It implements a subset of the fpgacapZero 0.5 ELA register map with the
// same capture semantics as rtl/fcapz_ela.v (0.5.2) at SAMPLE_W=4, DEPTH=16,
// TRIG_STAGES=1, DUAL_COMPARE=0, EXT_TRIG_EN=1:
//   - the trigger is accepted once PRETRIG samples are stored after arming,
//   - the trigger sample sits at index PRETRIG of the DATA window,
//   - CAPTURE_LEN = PRETRIG + POSTTRIG + 1 (keep it <= 16),
//   - TRIG_MODE 3 = value match OR change, 2 = change, 0/1 = value match,
//   - CTRL bit 1 (reset) re-arms when STARTUP_ARM is set,
//   - DATA_OFFSET (0xF4) adds a sample offset to DATA reads; CAPTURE_GEN
//     (0xF8) counts CTRL arm/reset writes and reset releases.
// Registers hold only the bits a 4-bit probe needs. Unlisted registers,
// CTRL, TRIG_DELAY and TRIG_HOLDOFF read as 0 and ignore writes.
//
// Register bus: reg_addr is the byte address with bits [15:9] reduced to
// reg_addr_hi (set if any of them is 1). reg_wdata_hi is set if any write
// data bit [31:4] is 1. reg_rdata is combinational; bits [31:8] depend only
// on the address and on state that changes only on a register write or a
// reset (CAPTURE_GEN), so they are stable during a read frame.

`default_nettype none
`timescale 1ns/1ps

module fcapz_ela_mini (
    input  wire        clk,
    input  wire        rst,
    input  wire [3:0]  probe_in,
    input  wire        trigger_in,
    output reg         trigger_out,
    output wire        armed_out,

    input  wire        reg_wr,
    input  wire        reg_addr_hi,
    input  wire [8:0]  reg_addr,
    input  wire [3:0]  reg_wdata,   // write data [3:0]
    input  wire        reg_wdata_hi, // any write data bit [31:4] set
    output reg  [31:0] reg_rdata
);

    // fpgacapZero 0.5 register map, core id "LA"
    localparam [31:0] VERSION = 32'h0005_4C41;
    // Constant ID words, equal to rtl/fcapz_ela.v at these parameters:
    // FEATURES = {TIMESTAMP_W 0, NUM_SEGMENTS 1, NUM_CHANNELS 1,
    //             HAS_EXT_TRIG, TRIG_STAGES 1}
    localparam [31:0] FEATURES     = 32'h0001_0141;
    // COMPARE_CAPS = modes 0x1C3 | bit17 | bit19 (timestamp base, full width)
    //   | bit20 DATA_OFFSET | bit21 CAPTURE_GEN | bit22 SQ_MODE encoding
    localparam [31:0] COMPARE_CAPS = 32'h007A_01C3;

    localparam [7:0] ADDR_VERSION     = 8'h00;
    localparam [7:0] ADDR_CTRL        = 8'h04;
    localparam [7:0] ADDR_STATUS      = 8'h08;
    localparam [7:0] ADDR_SAMPLE_W    = 8'h0C;
    localparam [7:0] ADDR_DEPTH       = 8'h10;
    localparam [7:0] ADDR_PRETRIG     = 8'h14;
    localparam [7:0] ADDR_POSTTRIG    = 8'h18;
    localparam [7:0] ADDR_CAPTURE_LEN = 8'h1C;
    localparam [7:0] ADDR_TRIG_MODE   = 8'h20;
    localparam [7:0] ADDR_TRIG_VALUE  = 8'h24;
    localparam [7:0] ADDR_TRIG_MASK   = 8'h28;
    localparam [7:0] ADDR_FEATURES    = 8'h3C;
    localparam [7:0] ADDR_NUM_CHAN    = 8'hA4;
    localparam [7:0] ADDR_TRIG_EXT    = 8'hB4;
    localparam [7:0] ADDR_NUM_SEGMENTS = 8'hB8;
    localparam [7:0] ADDR_SEG_STATUS  = 8'hBC;
    localparam [7:0] ADDR_SEG_START   = 8'hC8;
    localparam [7:0] ADDR_STARTUP_ARM = 8'hD8;
    localparam [7:0] ADDR_COMPARE_CAPS = 8'hE0;
    localparam [7:0] ADDR_DATA_OFFSET = 8'hF4;
    localparam [7:0] ADDR_CAPTURE_GEN = 8'hF8;
    // DATA window: 0x0100 + 4*i, i = 0..15

    // ---------------- configuration ----------------
    reg [3:0] pretrig, posttrig, trig_value, trig_mask;
    reg [1:0] trig_mode, trig_ext;
    reg       startup_arm;

    // ---------------- capture state ----------------
    reg       armed, triggered, done;
    reg [3:0] wr_ptr, pre_count, post_left, start_ptr;
    reg [4:0] cap_len;
    reg [4:0] data_offset;  // DATA read offset in samples, 16 = past the buffer
    reg [15:0] capture_gen;
    reg        rst_d;
    // Input pipeline, no reset: probe_s/probe_r form a 2-FF synchroniser that
    // matches trig_in_s, so TRIG_IN and the probe reach the trigger decision
    // on the same clock. Without a reset they hold real pin samples taken
    // while rst_n is low, so the startup capture never sees a fake value.
    reg [3:0] probe_s, probe_r, probe_prev;
    reg [1:0] trig_in_s;
    reg [3:0] mem [0:15];   // no reset: only read back after it is written

    assign armed_out = armed;

    wire reg_cmd    = reg_wr && !reg_addr_hi && !reg_addr[8];
    wire ctrl_wr    = reg_cmd && reg_addr[7:0] == ADDR_CTRL;
    wire soft_reset = ctrl_wr && reg_wdata[1];
    wire arm        = ctrl_wr && (reg_wdata[0] || (reg_wdata[1] && startup_arm));

    wire [3:0] m_probe = probe_r    & trig_mask;
    wire [3:0] m_prev  = probe_prev & trig_mask;
    wire [3:0] m_value = trig_value & trig_mask;
    wire hit_eq  = m_probe == m_value;
    wire hit_chg = m_probe != m_prev;
    wire hit_int = (trig_mode == 2'd3) ? (hit_eq | hit_chg)
                 : trig_mode[1]        ? hit_chg : hit_eq;
    reg  hit;
    always @(*) begin
        case (trig_ext)
            2'd1:    hit = hit_int | trig_in_s[1];
            2'd2:    hit = hit_int & trig_in_s[1];
            default: hit = hit_int;
        endcase
    end
    wire fire = armed && !triggered && pre_count >= pretrig && hit;

    always @(posedge clk) begin
        if (armed)
            mem[wr_ptr] <= probe_r;
    end

    always @(posedge clk) begin
        probe_s    <= probe_in;
        probe_r    <= probe_s;
        probe_prev <= probe_r;
        trig_in_s  <= {trig_in_s[0], trigger_in};
    end

    // CAPTURE_GEN: +1 for every CTRL write with the arm or reset bit set and
    // +1 when rst is released (each can start a capture that overwrites the
    // buffer). Reset to 0 so it never powers up unknown.
    wire gen_write   = ctrl_wr && (reg_wdata[0] || reg_wdata[1]);
    wire gen_release = rst_d && !rst;
    always @(posedge clk) begin
        rst_d <= rst;
        if (rst)
            capture_gen <= 16'd0;
        else
            capture_gen <= capture_gen + {15'd0, gen_write} + {15'd0, gen_release};
    end

    always @(posedge clk) begin
        // A CTRL write on the same clock discards the trigger, so it must not
        // reach TRIG_OUT either (and cannot stretch it to two clocks).
        trigger_out <= fire && !(soft_reset || arm);

        if (rst) begin
            pretrig <= 0; posttrig <= 0; trig_mode <= 2'd1;
            trig_value <= 0; trig_mask <= 4'hF; trig_ext <= 0;
            startup_arm <= 1'b1;
            armed <= 1'b1; triggered <= 0; done <= 0;     // STARTUP_ARM=1
            wr_ptr <= 0; pre_count <= 0; post_left <= 0; start_ptr <= 0;
            cap_len <= 0; data_offset <= 0;
            trigger_out <= 0;
        end else begin
            if (reg_cmd) begin
                case (reg_addr[7:0])
                    ADDR_PRETRIG:     pretrig     <= reg_wdata[3:0];
                    ADDR_POSTTRIG:    posttrig    <= reg_wdata[3:0];
                    ADDR_TRIG_MODE:   trig_mode   <= reg_wdata[1:0];
                    ADDR_TRIG_VALUE:  trig_value  <= reg_wdata[3:0];
                    ADDR_TRIG_MASK:   trig_mask   <= reg_wdata[3:0];
                    ADDR_TRIG_EXT:    trig_ext    <= reg_wdata[1:0];
                    ADDR_STARTUP_ARM: startup_arm <= reg_wdata[0];
                    ADDR_DATA_OFFSET: data_offset <= reg_wdata_hi ? 5'd16
                                                                  : {1'b0, reg_wdata};
                    default: ;
                endcase
            end

            // Every CTRL arm or reset clears DATA_OFFSET (as upstream), so an
            // offset left behind cannot shift the next capture's reads.
            if (gen_write) data_offset <= 0;

            if (soft_reset || arm) begin
                armed <= arm; triggered <= 0; done <= 0;
                pre_count <= 0;
            end else if (armed) begin
                wr_ptr <= wr_ptr + 1'b1;
                if (!triggered) begin
                    if (pre_count != 4'hF) pre_count <= pre_count + 1'b1;
                    if (fire) begin
                        triggered <= 1'b1;
                        start_ptr <= wr_ptr - pretrig;
                        cap_len   <= pretrig + posttrig + 5'd1;
                        post_left <= posttrig;
                        if (posttrig == 0) begin done <= 1'b1; armed <= 1'b0; end
                    end
                end else begin
                    post_left <= post_left - 1'b1;
                    if (post_left == 4'd1) begin done <= 1'b1; armed <= 1'b0; end
                end
            end
        end
    end

    // ---------------- register read ----------------
    wire [3:0] data_idx  = reg_addr[5:2];
    wire       data_addr = reg_addr[8] && reg_addr[7:6] == 2'b00 && reg_addr[1:0] == 2'b00;
    wire [5:0] data_sidx = {2'b00, data_idx} + {1'b0, data_offset};  // sample index
    wire [3:0] data_ptr  = start_ptr + data_sidx[3:0];
    wire       data_ok   = done && data_sidx < {1'b0, cap_len};

    always @(*) begin
        reg_rdata = 32'h0;
        if (!reg_addr_hi) begin
            if (reg_addr[8]) begin
                if (data_addr && data_ok) reg_rdata[3:0] = mem[data_ptr];
            end else begin
                case (reg_addr[7:0])
                    ADDR_VERSION:      reg_rdata = VERSION;
                    ADDR_STATUS:       reg_rdata[3:0] = {1'b0, done, triggered, armed};
                    ADDR_SAMPLE_W:     reg_rdata[7:0] = 8'd4;
                    ADDR_DEPTH:        reg_rdata[7:0] = 8'd16;
                    ADDR_PRETRIG:      reg_rdata[3:0] = pretrig;
                    ADDR_POSTTRIG:     reg_rdata[3:0] = posttrig;
                    ADDR_CAPTURE_LEN:  reg_rdata[4:0] = cap_len;
                    ADDR_TRIG_MODE:    reg_rdata[1:0] = trig_mode;
                    ADDR_TRIG_VALUE:   reg_rdata[3:0] = trig_value;
                    ADDR_TRIG_MASK:    reg_rdata[3:0] = trig_mask;
                    ADDR_FEATURES:     reg_rdata = FEATURES;
                    ADDR_NUM_CHAN:     reg_rdata[0]   = 1'b1;
                    ADDR_TRIG_EXT:     reg_rdata[1:0] = trig_ext;
                    ADDR_NUM_SEGMENTS: reg_rdata[0]   = 1'b1;
                    ADDR_SEG_STATUS:   reg_rdata[31]  = 1'b1;     // no segments
                    ADDR_SEG_START:    reg_rdata[3:0] = start_ptr;
                    ADDR_COMPARE_CAPS: reg_rdata = COMPARE_CAPS;
                    ADDR_STARTUP_ARM:  reg_rdata[0]   = startup_arm;
                    ADDR_DATA_OFFSET:  reg_rdata[4:0] = data_offset;
                    ADDR_CAPTURE_GEN:  reg_rdata[15:0] = capture_gen;
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire

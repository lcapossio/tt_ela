// SPDX-License-Identifier: Apache-2.0
// Compact single-clock reimplementation of the fpgacapZero ELA for one
// Tiny Tapeout tile: 4-bit probe, 16-sample buffer, one comparator.
//
// It implements a subset of the fpgacapZero 0.4 ELA register map with the
// same capture semantics as rtl/fcapz_ela.v at SAMPLE_W=4, DEPTH=16,
// TRIG_STAGES=1, DUAL_COMPARE=0, EXT_TRIG_EN=1:
//   - the trigger is accepted once PRETRIG samples are stored after arming,
//   - the trigger sample sits at index PRETRIG of the DATA window,
//   - CAPTURE_LEN = PRETRIG + POSTTRIG + 1 (keep it <= 16),
//   - TRIG_MODE[1] selects change detect, otherwise masked equality,
//   - CTRL bit 1 (reset) re-arms when STARTUP_ARM is set.
// Registers hold only the bits a 4-bit probe needs. Unlisted registers,
// CTRL, TRIG_DELAY and TRIG_HOLDOFF read as 0 and ignore writes.
//
// Register bus: reg_addr is the byte address with bits [15:9] reduced to
// reg_addr_hi (set if any of them is 1). reg_rdata is combinational;
// bits [31:8] depend only on the address.

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
    input  wire [3:0]  reg_wdata,   // write data [3:0]; upper bits ignored
    output reg  [31:0] reg_rdata
);

    // fpgacapZero 0.4 register map, core id "LA"
    localparam [31:0] VERSION = 32'h0004_4C41;

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
    localparam [7:0] ADDR_STARTUP_ARM = 8'hD8;
    // DATA window: 0x0100 + 4*i, i = 0..15

    // ---------------- configuration ----------------
    reg [3:0] pretrig, posttrig, trig_value, trig_mask;
    reg [1:0] trig_mode, trig_ext;
    reg       startup_arm;

    // ---------------- capture state ----------------
    reg       armed, triggered, done;
    reg [3:0] wr_ptr, pre_count, post_left, start_ptr;
    reg [4:0] cap_len;
    reg [3:0] probe_r, probe_prev;
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
    wire hit_int = trig_mode[1] ? (m_probe != m_prev) : (m_probe == m_value);
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
        probe_r     <= probe_in;
        probe_prev  <= probe_r;
        trig_in_s   <= {trig_in_s[0], trigger_in};
        trigger_out <= fire;

        if (rst) begin
            pretrig <= 0; posttrig <= 0; trig_mode <= 2'd1;
            trig_value <= 0; trig_mask <= 4'hF; trig_ext <= 0;
            startup_arm <= 1'b1;
            armed <= 1'b1; triggered <= 0; done <= 0;     // STARTUP_ARM=1
            wr_ptr <= 0; pre_count <= 0; post_left <= 0; start_ptr <= 0;
            cap_len <= 0; probe_r <= 0; probe_prev <= 0; trig_in_s <= 0;
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
                    default: ;
                endcase
            end

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
    wire [3:0] data_ptr  = start_ptr + data_idx;
    wire       data_ok   = done && {1'b0, data_idx} < cap_len;

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
                    ADDR_FEATURES:     reg_rdata[3:0] = 4'd1;   // TRIG_STAGES = 1
                    ADDR_NUM_CHAN:     reg_rdata[0]   = 1'b1;
                    ADDR_TRIG_EXT:     reg_rdata[1:0] = trig_ext;
                    ADDR_NUM_SEGMENTS: reg_rdata[0]   = 1'b1;
                    ADDR_STARTUP_ARM:  reg_rdata[0]   = startup_arm;
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire

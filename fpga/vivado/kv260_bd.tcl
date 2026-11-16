# KV260 block design: Zynq MPSoC PS -> AXI DMA (MM2S/S2MM) <-> quote_engine (AXI-Stream).
# Usage (Vivado 2022.2+ with Kria board files installed):
#   vivado -mode batch -source kv260_bd.tcl
# Outputs quote_engine.bit + quote_engine.hwh in ./overlay for the PYNQ driver.
# UNTESTED without Vivado here: board-part version strings and IP versions may need updating.
set root [file normalize [file dirname [info script]]]
set ip_repo [file normalize $root/../hls/quote_engine_prj/sol1/impl/ip]
create_project -force kv260_qe $root/kv260_qe -part xck26-sfvc784-2LV-c
set bp [lindex [get_board_parts -quiet -latest_file_version *kv260_som*] 0]
if {$bp ne ""} { set_property board_part $bp [current_project] }
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog

create_bd_design qe
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e zynq_ultra_ps_e_0]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset 1} $ps
# one HP slave port for the DMA, PL clock 0 at 300 MHz (falls back to what the PS can derive)
set_property -dict [list CONFIG.PSU__USE__S_AXI_GP2 {1} CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {300}] $ps

set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma axi_dma_0]
set_property -dict [list CONFIG.c_include_sg {0} CONFIG.c_sg_length_width {26} \
    CONFIG.c_m_axis_mm2s_tdata_width {256} CONFIG.c_mm2s_burst_size {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {128} CONFIG.c_s2mm_burst_size {64} \
    CONFIG.c_m_axi_mm2s_data_width {128} CONFIG.c_m_axi_s2mm_data_width {128}] $dma
set qe [create_bd_cell -type ip -vlnv hftmodel:hls:quote_engine quote_engine_0]

connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXIS_MM2S] [get_bd_intf_pins quote_engine_0/s_in]
connect_bd_intf_net [get_bd_intf_pins quote_engine_0/s_out] [get_bd_intf_pins axi_dma_0/S_AXIS_S2MM]

apply_bd_automation -rule xilinx.com:bd_rule:axi4 -config {Master /zynq_ultra_ps_e_0/M_AXI_HPM0_FPD Clk_master Auto Clk_slave Auto Clk_xbar Auto} [get_bd_intf_pins axi_dma_0/S_AXI_LITE]
apply_bd_automation -rule xilinx.com:bd_rule:axi4 -config {Slave /zynq_ultra_ps_e_0/S_AXI_HP0_FPD Clk_master Auto Clk_slave Auto Clk_xbar Auto} [get_bd_intf_pins axi_dma_0/M_AXI_MM2S]
apply_bd_automation -rule xilinx.com:bd_rule:axi4 -config {Slave /zynq_ultra_ps_e_0/S_AXI_HP0_FPD Clk_master Auto Clk_slave Auto Clk_xbar Auto} [get_bd_intf_pins axi_dma_0/M_AXI_S2MM]
apply_bd_automation -rule xilinx.com:bd_rule:clkrst -config {Clk /zynq_ultra_ps_e_0/pl_clk0} [get_bd_pins quote_engine_0/ap_clk]

validate_bd_design
save_bd_design
make_wrapper -files [get_files qe.bd] -top
add_files -norecurse [glob $root/kv260_qe/kv260_qe.gen/sources_1/bd/qe/hdl/qe_wrapper.v]
set_property top qe_wrapper [current_fileset]
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1

file mkdir $root/overlay
file copy -force [glob $root/kv260_qe/kv260_qe.runs/impl_1/*.bit] $root/overlay/quote_engine.bit
file copy -force [glob $root/kv260_qe/kv260_qe.gen/sources_1/bd/qe/hw_handoff/qe.hwh] $root/overlay/quote_engine.hwh
puts "overlay written to $root/overlay"

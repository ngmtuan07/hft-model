# Vitis HLS flow for the Kria K26 SOM (KV260). Run: VEC=/abs/path/to/vectors vitis_hls -f run_hls.tcl
# Produces an IP-XACT core in quote_engine_prj/sol1/impl/ip for Vivado.
set vec $::env(VEC)
open_project -reset quote_engine_prj
set_top quote_engine
add_files quote_engine.cpp -cflags "-std=c++14"
add_files -tb tb_quote_engine.cpp -cflags "-std=c++14 -Wno-unknown-pragmas"
open_solution -reset sol1 -flow_target vivado
set_part xck26-sfvc784-2LV-c
create_clock -period 3.333 -name default   ;# 300 MHz target
csim_design -argv $vec
csynth_design
# cosim replays every vector through the RTL; slow for 100k vectors, so export fewer for it.
if {[info exists ::env(COSIM)]} { cosim_design -argv $vec -trace_level none }
export_design -format ip_catalog -vendor hftmodel -library hls -version 1.0 -ipname quote_engine
exit

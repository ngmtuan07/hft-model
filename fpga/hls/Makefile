# Local C-simulation with a plain C++ compiler (no Vitis needed).
#   make csim VEC=../../output/fpga_vectors
# Vitis HLS (csim + synthesis + IP export for the KV260's XCK26):
#   make hls VEC=$(abspath ../../output/fpga_vectors)
CXX ?= c++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wno-unknown-pragmas
VEC ?= ../../output/fpga_vectors

tb: tb_quote_engine.cpp quote_engine.cpp quote_engine.hpp hls_compat.hpp
	$(CXX) $(CXXFLAGS) -o $@ tb_quote_engine.cpp quote_engine.cpp

csim: tb
	./tb $(VEC)

hls:
	VEC=$(VEC) vitis_hls -f run_hls.tcl

clean:
	rm -rf tb quote_engine_prj *.log
.PHONY: csim hls clean

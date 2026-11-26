# C++ exchange box + reference CPU contestant.
#   make            build everything into build/
#   make test       matching-engine unit tests
#   make verify     replay a LOBSTER day with no contestants and compare against LOBSTER's own book
#   make bench      matching-engine throughput / per-message latency
#   make race       exchange + two CPU contestants (one handicapped) on localhost
#   make linux      Linux build + unit tests in Docker (output in build-linux/)
#   B=dir           build directory (default build/)
CXX ?= c++
ARCH := $(shell uname -m)
ifneq ($(filter arm64 aarch64,$(ARCH)),)
  TUNE := -mcpu=native
else
  TUNE := -march=native
endif
CXXFLAGS ?= -O3 -std=c++17 $(TUNE) -Wall -Wextra -Wno-unused-parameter
LDFLAGS ?= -pthread
INC := -Iinclude
B ?= build
HDRS := $(wildcard include/*.hpp)
MSG ?= ../data/lobster/INTC_2012-06-21_34200000_57600000_message_10.csv

all: $(B)/exchange $(B)/trader $(B)/test_book $(B)/udp_pingpong

$(B)/%: src/%.cpp $(HDRS) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $< $(LDFLAGS)

$(B)/test_book: tests/test_book.cpp $(HDRS) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $< $(LDFLAGS)

$(B)/udp_pingpong: tools/udp_pingpong.cpp $(HDRS) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $< $(LDFLAGS)

$(B):
	mkdir -p $(B)

test: $(B)/test_book
	./$(B)/test_book

verify: $(B)/exchange
	./$(B)/exchange verify --messages $(MSG)

bench: $(B)/exchange
	./$(B)/exchange bench --messages $(MSG)

race: all
	./scripts/race.sh

# Linux build + tests inside Docker (verifies the Linux code paths from a Mac)
LINUX_IMAGE ?= gcc:14
linux:
	docker run --rm --pull=never -v $(abspath ..):/w -w /w/exchange $(LINUX_IMAGE) make B=build-linux all test

clean:
	rm -rf build build-linux

.PHONY: all test verify bench race clean

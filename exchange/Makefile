# C++ exchange box + reference CPU contestant.
#   make            build everything into build/
#   make test       matching-engine unit tests
#   make verify     replay a LOBSTER day with no contestants and compare against LOBSTER's own book
#   make bench      matching-engine throughput / per-message latency
#   make race       exchange + two CPU contestants (one handicapped) on localhost
CXX ?= c++
ARCH := $(shell uname -m)
ifeq ($(ARCH),arm64)
  TUNE := -mcpu=native
else
  TUNE := -march=native
endif
CXXFLAGS ?= -O3 -std=c++17 $(TUNE) -Wall -Wextra -Wno-unused-parameter
INC := -Iinclude
HDRS := $(wildcard include/*.hpp)
MSG ?= ../data/lobster/INTC_2012-06-21_34200000_57600000_message_10.csv

all: build/exchange build/trader build/test_book

build/%: src/%.cpp $(HDRS) | build
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $<

build/test_book: tests/test_book.cpp $(HDRS) | build
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $<

build:
	mkdir -p build

test: build/test_book
	./build/test_book

verify: build/exchange
	./build/exchange verify --messages $(MSG)

bench: build/exchange
	./build/exchange bench --messages $(MSG)

race: all
	./scripts/race.sh

clean:
	rm -rf build

.PHONY: all test verify bench race clean

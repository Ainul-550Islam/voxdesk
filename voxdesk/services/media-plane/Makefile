# Media-plane (C++17) — build + tests
#
# Two self-contained test binaries share the one static library:
#   media_tests  — foundation (jitter buffer + consistent-hash router)
#   audio_tests  — audio/video processing (FFT, spectral denoiser, G.711, VAD)

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Werror
INCLUDES := -Iinclude

BUILD_DIR := build
SRC_OBJS := $(BUILD_DIR)/src/jitter_buffer.o \
            $(BUILD_DIR)/src/consistent_hash_router.o \
            $(BUILD_DIR)/src/fft.o \
            $(BUILD_DIR)/src/spectral_denoise.o \
            $(BUILD_DIR)/src/g711.o \
            $(BUILD_DIR)/src/vad.o
LIB := libvoxdesk_media.a
FOUNDATION_BIN := $(BUILD_DIR)/media_tests
AUDIO_BIN := $(BUILD_DIR)/audio_tests

.PHONY: all test clean

all: $(LIB) $(FOUNDATION_BIN) $(AUDIO_BIN)

test: $(FOUNDATION_BIN) $(AUDIO_BIN)
	./$(FOUNDATION_BIN)
	./$(AUDIO_BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/src/%.o: src/%.cpp | $(BUILD_DIR)
	@mkdir -p $(BUILD_DIR)/src
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD_DIR)/tests/%.o: tests/%.cpp | $(BUILD_DIR)
	@mkdir -p $(BUILD_DIR)/tests
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

$(LIB): $(SRC_OBJS)
	ar rcs $@ $^

$(FOUNDATION_BIN): $(BUILD_DIR)/tests/test_main.o $(LIB)
	$(CXX) $(CXXFLAGS) $(BUILD_DIR)/tests/test_main.o -L. -lvoxdesk_media -o $@

$(AUDIO_BIN): $(BUILD_DIR)/tests/test_audio.o $(LIB)
	$(CXX) $(CXXFLAGS) $(BUILD_DIR)/tests/test_audio.o -L. -lvoxdesk_media -o $@

clean:
	rm -rf $(BUILD_DIR) $(LIB)

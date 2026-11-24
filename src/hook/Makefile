CC = gcc
CFLAGS = -Wall -Wextra -fPIC -g
LDFLAGS = -shared -ldl -lpthread

BUILD_DIR = ../../build
TARGET64 = $(BUILD_DIR)/libjanus64.so
TARGET32 = $(BUILD_DIR)/libjanus32.so
GAMESCOPE_SHIM = $(BUILD_DIR)/libjanus_gamescope.so
TRACER64 = $(BUILD_DIR)/libsdl_tracer64.so
TRACER32 = $(BUILD_DIR)/libsdl_tracer32.so
SRC = janus_hook.c
TRACER_SRC = sdl_tracer.c

.PHONY: all clean 32bit 64bit gamescope-shim tracer tracer64 tracer32

all: 64bit 32bit gamescope-shim

gamescope-shim: $(GAMESCOPE_SHIM)

64bit: $(TARGET64)

32bit: $(TARGET32)

tracer: tracer64 tracer32

tracer64: $(TRACER64)

tracer32: $(TRACER32)

$(TARGET64): $(SRC)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $(TARGET64)"

$(TARGET32): $(SRC)
	@mkdir -p $(BUILD_DIR)
	$(CC) -m32 $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $(TARGET32)"

$(GAMESCOPE_SHIM): gamescope_shim.c
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $(GAMESCOPE_SHIM)"

$(TRACER64): $(TRACER_SRC)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $(TRACER64)"

$(TRACER32): $(TRACER_SRC)
	@mkdir -p $(BUILD_DIR)
	$(CC) -m32 $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $(TRACER32)"

clean:
	rm -f $(TARGET64) $(TARGET32) $(GAMESCOPE_SHIM) $(TRACER64) $(TRACER32)

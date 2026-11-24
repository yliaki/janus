# Ticket proxy DLL (Game.goldbergRealTicket) — see proxy.c.
#
# The output is COMMITTED at vendor/ticketproxy/steam_api64_ticket.dll so
# launching never needs a Windows cross-compiler. Rebuild (and commit the
# result) after changing proxy.c / gen_stubs.py or updating the Goldberg DLLs
# in vendor/goldberg/. Needs: sudo apt install gcc-mingw-w64-x86-64
#
# --no-insert-timestamp keeps the build reproducible: rebuilding unchanged
# sources with the same compiler gives byte-identical output (no git noise).
CC = x86_64-w64-mingw32-gcc
CFLAGS = -Wall -Wextra -O2
LDFLAGS = -shared -s -static-libgcc -Wl,--enable-stdcall-fixup -Wl,--no-insert-timestamp
GEN_DIR = ../../build/ticketproxy
OUT_DIR = ../../vendor/ticketproxy
GOLDBERG = $(wildcard ../../vendor/goldberg/*/steam_api64.dll)
OVERRIDES = SteamAPI_ISteamUser_GetAuthSessionTicket
TARGET = $(OUT_DIR)/steam_api64_ticket.dll

.PHONY: all clean

all: $(TARGET)

# Jump stubs + .def generated from Goldberg's export NAMES (no Goldberg code).
$(GEN_DIR)/ticketproxy_stubs.c $(GEN_DIR)/ticketproxy.def &: gen_stubs.py $(GOLDBERG)
	@mkdir -p $(GEN_DIR)
	python3 gen_stubs.py $(GEN_DIR)/ticketproxy_stubs.c $(GEN_DIR)/ticketproxy.def \
		$(OVERRIDES) $(GOLDBERG)

$(TARGET): proxy.c $(GEN_DIR)/ticketproxy_stubs.c $(GEN_DIR)/ticketproxy.def
	@mkdir -p $(OUT_DIR)
	$(CC) $(CFLAGS) -I$(GEN_DIR) -o $@ proxy.c $(GEN_DIR)/ticketproxy.def $(LDFLAGS)
	@echo "Built: $(TARGET)"

# Removes only the intermediates; the committed DLL stays.
clean:
	rm -rf $(GEN_DIR)

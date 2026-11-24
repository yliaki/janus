#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdarg.h>
#include <errno.h>
#include <linux/joystick.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <pthread.h>
#include <poll.h>
#include <ctype.h>
#include <limits.h>
#include <sys/sysmacros.h>
#include <sys/epoll.h>
#include <sys/inotify.h>
#include <sys/select.h>
#include <sys/syscall.h>
#include <signal.h>

// =============================================================================
// Configuration
// =============================================================================

static int assigned_controller = -1;  // -1 means no filtering
static char assigned_controller_name[256] = {0};
static unsigned short assigned_vid = 0;   // VID of assigned controller (for re-matching after replug)
static unsigned short assigned_pid = 0;   // PID of assigned controller (for re-matching after replug)
static int hook_initialized = 0;
static int evdev_initialized = 0;  // Separate flag for evdev hooks (must init early)

// Debug logging
static int verbose_logging = 0;

static int trace_open_enabled = 0;             // JANUS_TRACE_OPEN: log write-opens (find what needs isolating)

// Optional per-process log file (JANUS_LOG_FILE). Opened in the constructor as
// "<JANUS_LOG_FILE>.<pid>" so concurrent instances/processes don't interleave.
// Survives gamescope/pressure-vessel swallowing stderr — essential for catching
// intermittent controller-filtering races in a real multi-instance session.
static FILE* g_log_fp = NULL;
#define LOG(fmt, ...) do { \
    fprintf(stderr, "[janus] " fmt "\n", ##__VA_ARGS__); \
    if (g_log_fp) { fprintf(g_log_fp, "[janus] " fmt "\n", ##__VA_ARGS__); fflush(g_log_fp); } \
} while(0)
#define VLOG(fmt, ...) do { if (verbose_logging) LOG(fmt, ##__VA_ARGS__); } while(0)

// Forward declarations
static int steam_filter_on(void);

// Native game reading pads through SDL: in Steam Input mode it gets its pad as
// the Steam virtual pad (not under Proton, nor with the vtable filter).
static int sdl_reads_steam_virtual_pad(void) {
    return !getenv("STEAM_COMPAT_DATA_PATH") && !steam_filter_on();
}

// dlsym/dlopen hooks
static void* (*real_dlsym)(void* handle, const char* symbol) = NULL;
static void* (*real_dlopen)(const char* filename, int flags) = NULL;

// dlvsym, not dlsym: dlsym is our own hook. Also called from the dlopen hook
// itself, which can run BEFORE our constructor: libraries the executable links
// directly are initialised before an LD_PRELOAD lib, and sdl2-compat's
// constructor dlopens libSDL3 (gamescope links SDL2: on Arch every launch
// died with "Failed loading SDL3 library.").
static void resolve_real_dlopen(void) {
    if (real_dlopen) return;
    real_dlopen = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.1");    // i386
    if (!real_dlopen)
        real_dlopen = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.2.5");  // x86_64
    if (!real_dlopen)
        real_dlopen = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.0");
    if (!real_dlopen)
        real_dlopen = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.34");  // glibc 2.34+
}

// Goldberg Steam Emulator: redirect dlopen of libsteam_api.so
static const char* goldberg_lib_path = NULL;
// Path of the game's ORIGINAL Valve libsteam_api.so, captured in the dlopen hook
// at the moment we redirect it to Goldberg. Fallback source for the hybrid
// (JANUS_STEAM_INPUT_REAL_LIB) when steam.py could not find the lib on disk —
// e.g. Necesse, whose steamworks4j jar extracts it to /tmp/lwjgl_*/x64/ at runtime.
static char captured_valve_steam_api[4096] = {0};

// SDL_SetHint wrapper: block steamclient.so from overriding virtual gamepad hint
static int (*real_SDL_SetHint_fn)(const char*, const char*) = NULL;
static int (*real_SDL_SetHintWithPriority_fn)(const char*, const char*, int) = NULL;

// ISteamInput blocking: force games to fall back to SDL2 for controller input
static void* (*real_SteamInternal_FindOrCreateUserInterface)(int, const char*) = NULL;
static void* (*real_SteamInternal_CreateInterface)(const char*) = NULL;

// =============================================================================
// evdev Hooks - Function Pointers (must be loaded first)
// =============================================================================

static int (*real_open)(const char*, int, ...) = NULL;
static int (*real_open64)(const char*, int, ...) = NULL;
static int (*real_openat)(int, const char*, int, ...) = NULL;
static int (*real_openat64)(int, const char*, int, ...) = NULL;
static int (*real_close)(int) = NULL;

// Initialize evdev hooks early (before anything else uses open())
static void ensure_evdev_init(void) {
    if (evdev_initialized) return;
    evdev_initialized = 1;

    real_open = dlsym(RTLD_NEXT, "open");
    real_open64 = dlsym(RTLD_NEXT, "open64");
    real_openat = dlsym(RTLD_NEXT, "openat");
    real_openat64 = dlsym(RTLD_NEXT, "openat64");
    real_close = dlsym(RTLD_NEXT, "close");
}

// =============================================================================
// Kernel-level controller enumeration (canonical ordering)
// =============================================================================

// Must comfortably hold every physical controller PLUS the Steam Input virtual
// gamepad it creates for each (Steam Input roughly doubles the device count), or
// the later js* devices — including the virtual pad assigned to an instance —
// get dropped from the table and that instance's filtering breaks (it then sees
// every pad). 8 was too small for a 2-player Steam Input split (5 physical + 5
// virtual = 10 js nodes).
#define MAX_CONTROLLERS 32
#define VALVE_VENDOR_ID 0x28de

typedef struct {
    int valid;
    char name[256];
    char device_path[64];  // e.g., "/dev/input/js0"
    int js_index;          // the js* number
    int event_index;       // corresponding event* number (-1 if unknown)
    unsigned short vendor_id;
    unsigned short product_id;
    char uniq[64];         // unique ID (Bluetooth MAC or empty)
    char phys[128];        // physical path (USB path)
} KernelController;

// =============================================================================
// Steam Input Virtual Controller Support
// =============================================================================

typedef struct {
    int valid;
    int slot;              // Steam slot number (from virtualgamepadinfo.txt)
    char name[256];        // e.g., "PS4 Controller"
    unsigned short vid;
    unsigned short pid;
    char handle[32];       // handle from Steam (MAC without colons)
    char type[32];         // e.g., "ps4"
    int js_index;          // corresponding /dev/input/js* index (-1 if not found)
    int physical_index;    // index into kernel_controllers (-1 if not matched)
} SteamVirtualController;

static SteamVirtualController steam_virtual_controllers[MAX_CONTROLLERS];
static int num_steam_virtual = 0;
static int steam_input_active = 0;
static int steam_input_checked = 0;
static int assigned_virtual_js = -1;  // js index of virtual controller for assigned physical
static time_t vgi_mtime = 0;          // mtime of virtualgamepadinfo.txt when parsed

static KernelController kernel_controllers[MAX_CONTROLLERS];
static int num_kernel_controllers = 0;
// Serializes publishing the controller list and whole rescans (enumerate +
// re-match + Steam Input re-detect) across the game's threads. Recursive:
// a rescan re-enters enumerate_kernel_controllers().
static pthread_mutex_t g_enum_lock = PTHREAD_RECURSIVE_MUTEX_INITIALIZER_NP;

// The real ioctl(). The hook's own code must use this: our exported ioctl()
// (below) rewrites virtual-pad names, which would break slot detection.
static int (*real_ioctl_fn)(int, unsigned long, ...) = NULL;
static int sys_ioctl(int fd, unsigned long request, void* arg) {
    if (!real_ioctl_fn)
        real_ioctl_fn = (int (*)(int, unsigned long, ...))dlsym(RTLD_NEXT, "ioctl");
    return real_ioctl_fn ? real_ioctl_fn(fd, request, arg) : -1;
}

// Safe string copy that always null-terminates
static void safe_strcpy(char* dest, const char* src, size_t dest_size) {
    if (dest_size == 0) return;
    strncpy(dest, src, dest_size - 1);
    dest[dest_size - 1] = '\0';
}

static unsigned short read_sysfs_hex(const char* path) {
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    unsigned int val = 0;
    if (fscanf(f, "%x", &val) != 1) val = 0;
    fclose(f);
    return (unsigned short)val;
}

static void read_sysfs_string(const char* path, char* buf, size_t bufsize) {
    buf[0] = '\0';
    FILE* f = fopen(path, "r");
    if (!f) return;
    if (fgets(buf, bufsize, f)) {
        // Remove trailing newline
        size_t len = strlen(buf);
        if (len > 0 && buf[len-1] == '\n') {
            buf[len-1] = '\0';
        }
    }
    fclose(f);
}

// Find the event* index corresponding to a js* device
static int find_event_for_js(int js_index) {
    char sysfs_path[256];
    char resolved[256];

    // /sys/class/input/js0/device points to the parent input device
    snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device", js_index);

    // Resolve the symlink
    ssize_t len = readlink(sysfs_path, resolved, sizeof(resolved) - 1);
    if (len < 0) return -1;
    resolved[len] = '\0';

    // Now scan that directory for event* entries
    // The resolved path is relative, like "../../../input5"
    // We need to find event* in /sys/class/input/js0/device/
    DIR* dir = opendir(sysfs_path);
    if (!dir) return -1;

    struct dirent* entry;
    int event_idx = -1;
    while ((entry = readdir(dir)) != NULL) {
        if (strncmp(entry->d_name, "event", 5) == 0) {
            event_idx = atoi(entry->d_name + 5);
            break;
        }
    }
    closedir(dir);

    return event_idx;
}

// Bit b of a sysfs capability bitmap ("hex longs, most significant first";
// bit b lives in word b/BITS_PER_LONG counting from the END).
static int sysfs_bitmap_has(const char* path, int b) {
    char buf[1024];
    read_sysfs_string(path, buf, sizeof(buf));
    char* words[64];
    int nw = 0;
    char* save = NULL;
    for (char* tok = strtok_r(buf, " \n", &save); tok && nw < 64;
         tok = strtok_r(NULL, " \n", &save))
        words[nw++] = tok;
    const int bpl = (int)(sizeof(long) * 8);
    int w = b / bpl;
    if (w >= nw) return 0;
    unsigned long v = strtoul(words[nw - 1 - w], NULL, 16);
    return (v & (1UL << (b % bpl))) != 0;
}

// eventN has gamepad/joystick buttons (BTN_JOYSTICK..BTN_THUMBR, 0x120-0x13f —
// the same test udev uses for ID_INPUT_JOYSTICK).
static int event_has_gamepad_buttons(int idx) {
    char path[96];
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/capabilities/key", idx);
    for (int b = 0x120; b <= 0x13f; b++)
        if (sysfs_bitmap_has(path, b)) return 1;
    return 0;
}

static int event_has_js_sibling(int idx) {
    char dir_path[64];
    snprintf(dir_path, sizeof(dir_path), "/sys/class/input/event%d/device", idx);
    DIR* dir = opendir(dir_path);
    if (!dir) return 0;
    int found = 0;
    struct dirent* e;
    while ((e = readdir(dir)) != NULL)
        if (strncmp(e->d_name, "js", 2) == 0) { found = 1; break; }
    closedir(dir);
    return found;
}

// A gamepad the kernel gives no js node (Nintendo Pro Controller over
// Bluetooth via hid-nintendo: event node only). Gamepad buttons + an X axis,
// not an accelerometer (its IMU node), not Steam's virtual pad (those get js
// nodes; right after creation the js sibling may just not exist yet).
static int is_evdev_only_gamepad(int idx) {
    char path[96];
    if (event_has_js_sibling(idx) || !event_has_gamepad_buttons(idx)) return 0;
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/capabilities/abs", idx);
    if (!sysfs_bitmap_has(path, 0 /* ABS_X */)) return 0;
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/properties", idx);
    if (sysfs_bitmap_has(path, 6 /* INPUT_PROP_ACCELEROMETER */)) return 0;
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/id/vendor", idx);
    return read_sysfs_hex(path) != VALVE_VENDOR_ID;
}

static void enumerate_kernel_controllers(void) {
    // Build into a private list and publish it under g_enum_lock. Appending to the
    // global in place let two threads (a hot-plug rescan + an open() refresh) both
    // append, doubling every entry (2026-10-07: 11 entries for 6 pads), which broke
    // index-based matching.
    KernelController tmp[MAX_CONTROLLERS];
    int n = 0;
    memset(tmp, 0, sizeof(tmp));

    // Make sure we have the real open() before using it
    ensure_evdev_init();

    // Enumerate /dev/input/js* devices
    for (int i = 0; i < MAX_CONTROLLERS; i++) {
        char path[64];
        snprintf(path, sizeof(path), "/dev/input/js%d", i);

        // Use real_open to avoid recursion
        int fd = real_open(path, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;

        char name[256] = "Unknown";
        if (sys_ioctl(fd, JSIOCGNAME(sizeof(name)), name) < 0) {
            strcpy(name, "Unknown");
        }
        real_close(fd);

        // Read vendor/product IDs from sysfs
        char sysfs_path[256];
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device/id/vendor", i);
        unsigned short vendor = read_sysfs_hex(sysfs_path);
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device/id/product", i);
        unsigned short product = read_sysfs_hex(sysfs_path);

        tmp[n].valid = 1;
        tmp[n].js_index = i;
        safe_strcpy(tmp[n].name, name, sizeof(tmp[n].name));
        safe_strcpy(tmp[n].device_path, path, sizeof(tmp[n].device_path));
        tmp[n].vendor_id = vendor;
        tmp[n].product_id = product;

        // Find corresponding event device
        tmp[n].event_index = find_event_for_js(i);

        // Read unique ID (Bluetooth MAC) and physical path (USB)
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device/uniq", i);
        read_sysfs_string(sysfs_path, tmp[n].uniq,
                          sizeof(tmp[n].uniq));
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device/phys", i);
        read_sysfs_string(sysfs_path, tmp[n].phys,
                          sizeof(tmp[n].phys));

        LOG("Kernel controller %d: %s (js%d, event%d) [%04x:%04x] uniq='%s'",
            n, name, i,
            tmp[n].event_index,
            vendor, product,
            tmp[n].uniq);
        n++;
    }

    // Gamepads without a js node (event node only), after the js pads, same
    // order as controllers.py lists them. js_index -1: no js node to filter.
    for (int ev = 0; ev < 256 && n < MAX_CONTROLLERS; ev++) {
        char sysfs_path[256];
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d", ev);
        struct stat st;
        if (stat(sysfs_path, &st) != 0 || !is_evdev_only_gamepad(ev)) continue;
        tmp[n].valid = 1;
        tmp[n].js_index = -1;
        tmp[n].event_index = ev;
        snprintf(tmp[n].device_path, sizeof(tmp[n].device_path), "/dev/input/event%d", ev);
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d/device/name", ev);
        read_sysfs_string(sysfs_path, tmp[n].name, sizeof(tmp[n].name));
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d/device/id/vendor", ev);
        tmp[n].vendor_id = read_sysfs_hex(sysfs_path);
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d/device/id/product", ev);
        tmp[n].product_id = read_sysfs_hex(sysfs_path);
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d/device/uniq", ev);
        read_sysfs_string(sysfs_path, tmp[n].uniq, sizeof(tmp[n].uniq));
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/event%d/device/phys", ev);
        read_sysfs_string(sysfs_path, tmp[n].phys, sizeof(tmp[n].phys));
        LOG("Kernel controller %d: %s (no js node, event%d) [%04x:%04x] uniq='%s'",
            n, tmp[n].name, ev, tmp[n].vendor_id, tmp[n].product_id, tmp[n].uniq);
        n++;
    }

    pthread_mutex_lock(&g_enum_lock);
    memcpy(kernel_controllers, tmp, sizeof(tmp));
    num_kernel_controllers = n;
    pthread_mutex_unlock(&g_enum_lock);
}

static int find_kernel_index_by_name(const char* name) {
    for (int i = 0; i < num_kernel_controllers; i++) {
        if (kernel_controllers[i].valid && strstr(kernel_controllers[i].name, name)) {
            return i;
        }
    }
    return -1;
}

// Re-find the assigned controller after re-enumerating kernel controllers (hot-plug support)
static char assigned_phys[128];   // phys of the assigned pad, for re-matching
// Identity of the assigned pad beyond VID:PID, for re-matching after the
// kernel list changes. Kernel indices shift whenever ANY pad appears or
// disappears, so they're never used as identity. Two complications:
//  - identical pads: the remaining one belongs to another player, so a pad
//    that's gone must not be replaced by "any pad with that VID:PID";
//  - multi-pad adapters ("Twin USB Joystick" 0810:0001: one USB device, two
//    PS1 pads) share VID:PID AND phys, so only their order tells them apart.
static int assigned_event = -1;        // event node of the assigned pad while it exists
static int assigned_rank_id = 0;       // rank among same VID:PID, by event order
static int assigned_rank_phys = 0;     // rank among same VID:PID + phys
static int assigned_model_count = 0;   // pads with its VID:PID at assignment
static int assigned_gone = 0;          // logged "gone" for the current outage
// Steam handle of this instance's pad (SteamFilter), defined further down;
// dropped when the pad changes mode (a new device = a new Steam handle).
static unsigned long long g_assigned_steam_handle;

static int same_identity(int i, int use_phys) {
    return kernel_controllers[i].valid &&
           kernel_controllers[i].vendor_id == assigned_vid &&
           kernel_controllers[i].product_id == assigned_pid &&
           (!use_phys || strcmp(kernel_controllers[i].phys, assigned_phys) == 0);
}

// Position of pad i among the pads sharing its identity, in event-node order
// (= connection order, the order Steam opens them in too).
static int identity_rank(int i, int use_phys) {
    int r = 0;
    for (int j = 0; j < num_kernel_controllers; j++)
        if (j != i && same_identity(j, use_phys) &&
            kernel_controllers[j].event_index < kernel_controllers[i].event_index)
            r++;
    return r;
}

static int identity_count(int use_phys) {
    int n = 0;
    for (int j = 0; j < num_kernel_controllers; j++)
        if (same_identity(j, use_phys)) n++;
    return n;
}

// Record the assigned pad's port so re-enumeration can find the same device.
static void remember_assigned_phys(void) {
    if (assigned_controller >= 0 && assigned_controller < num_kernel_controllers &&
        kernel_controllers[assigned_controller].valid &&
        kernel_controllers[assigned_controller].phys[0])
        safe_strcpy(assigned_phys, kernel_controllers[assigned_controller].phys,
                    sizeof(assigned_phys));
}

// Called once VID:PID is known (identity from env or from the assigned pad).
static void remember_assigned_ordinals(void) {
    if (assigned_controller < 0 || assigned_controller >= num_kernel_controllers) return;
    assigned_event = kernel_controllers[assigned_controller].event_index;
    assigned_rank_id = identity_rank(assigned_controller, 0);
    assigned_rank_phys = assigned_phys[0] ? identity_rank(assigned_controller, 1) : 0;
    assigned_model_count = identity_count(0);
}

// The pad with the remembered rank among those matching the identity (the
// last if fewer remain); -1 if none match.
static int pick_identity_match(int use_phys) {
    int want = use_phys ? assigned_rank_phys : assigned_rank_id;
    int last = -1;
    for (int i = 0; i < num_kernel_controllers; i++) {
        if (!same_identity(i, use_phys)) continue;
        if (identity_rank(i, use_phys) == want) return i;
        last = i;
    }
    return last;
}

static void set_rematched(int i, int old_assigned, const char* how) {
    assigned_controller = i;
    assigned_event = kernel_controllers[i].event_index;
    assigned_gone = 0;
    if (i != old_assigned)
        LOG("Hot-plug: re-matched controller %s -> kernel index %d (js%d, was index %d)",
            how, i, kernel_controllers[i].js_index, old_assigned);
}

static void re_match_assigned_controller(void) {
    if (assigned_vid == 0 && assigned_pid == 0 && assigned_controller_name[0] == '\0') {
        return;  // No identity info to match with
    }

    int old_assigned = assigned_controller;

    if (assigned_vid != 0 && assigned_pid != 0) {
        // Still connected: its event node is unique while the device exists.
        for (int i = 0; i < num_kernel_controllers; i++)
            if (assigned_event >= 0 && kernel_controllers[i].event_index == assigned_event &&
                same_identity(i, assigned_phys[0] != 0)) {
                set_rematched(i, old_assigned, "by event node");
                return;
            }

        // Replugged into the same USB/BT port (same rank for adapter pads).
        if (assigned_phys[0]) {
            int i = pick_identity_match(1);
            if (i >= 0) {
                set_rematched(i, old_assigned, "by port");
                return;
            }
        }

        // Same USB port, another model: the pad re-enumerated in another mode
        // on a replug (MACHENIKE G3V2 36e8:3001 comes back as an Xbox 360 pad
        // 045e:028e). Only one such pad on the port, and only USB: a Bluetooth
        // phys is the adapter's address, the same for every pad.
        if (strncmp(assigned_phys, "usb-", 4) == 0) {
            int found = -1, n = 0;
            for (int i = 0; i < num_kernel_controllers; i++) {
                KernelController* k = &kernel_controllers[i];
                if (!k->valid || k->vendor_id == VALVE_VENDOR_ID) continue;
                if (strcmp(k->phys, assigned_phys) != 0) continue;
                if (k->vendor_id == assigned_vid && k->product_id == assigned_pid) continue;
                found = i;
                n++;
            }
            if (n == 1) {
                LOG("Hot-plug: same USB port %s, now %04x:%04x '%s' (was %04x:%04x): "
                    "the pad changed mode", assigned_phys, kernel_controllers[found].vendor_id,
                    kernel_controllers[found].product_id, kernel_controllers[found].name,
                    assigned_vid, assigned_pid);
                assigned_vid = kernel_controllers[found].vendor_id;
                assigned_pid = kernel_controllers[found].product_id;
                safe_strcpy(assigned_controller_name, kernel_controllers[found].name,
                            sizeof(assigned_controller_name));
                g_assigned_steam_handle = 0;
                set_rematched(found, old_assigned, "by USB port (pad changed mode)");
                remember_assigned_ordinals();
                return;
            }
        }

        // Any pad of this model — but only when no port is known, or no pad of
        // the model is missing (then ours moved port). Otherwise the missing
        // one is ours and the others belong to other players.
        if (!assigned_phys[0] || identity_count(0) >= assigned_model_count) {
            int i = pick_identity_match(0);
            if (i >= 0) {
                set_rematched(i, old_assigned, "by VID:PID");
                return;
            }
        }
    } else if (assigned_controller_name[0] != '\0') {
        // No VID:PID known: match by name
        int idx = find_kernel_index_by_name(assigned_controller_name);
        if (idx >= 0) {
            set_rematched(idx, old_assigned, "by name");
            return;
        }
    }

    // Gone: point at an empty entry, so every pad is blocked (nobody else's pad
    // drives this instance) and refresh_kernel_state_if_needed() keeps
    // re-enumerating until it's back.
    if (num_kernel_controllers < MAX_CONTROLLERS)
        assigned_controller = num_kernel_controllers;
    if (!assigned_gone) {
        assigned_gone = 1;
        LOG("Hot-plug: assigned controller (VID:PID %04x:%04x, %s) not connected — "
            "this instance has no pad until it is back", assigned_vid, assigned_pid,
            assigned_phys[0] ? assigned_phys : assigned_controller_name);
    }
}

// =============================================================================
// Steam Input Detection and Mapping
// =============================================================================

// Convert MAC address "aa:bb:cc:dd:ee:ff" to Steam handle format "0x0000aabbccddeeff"
static void mac_to_steam_handle(const char* mac, char* handle, size_t handle_size) {
    handle[0] = '\0';
    if (!mac || strlen(mac) < 17) return;

    // Parse MAC and convert to handle
    unsigned int b[6];
    if (sscanf(mac, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) == 6) {
        snprintf(handle, handle_size, "0x0000%02x%02x%02x%02x%02x%02x",
                 b[0], b[1], b[2], b[3], b[4], b[5]);
    }
}

// Path of Steam's virtualgamepadinfo.txt; 0 if it can't be determined.
// Under instance isolation the container's $HOME is a fresh dir with no
// ~/.steam, so steam.py bind-mounts the real Steam config and hands us its
// path via JANUS_VGI_PATH. Prefer that; otherwise use the $HOME-relative one.
static int vgi_path(char* path, size_t size) {
    const char* vgi = getenv("JANUS_VGI_PATH");
    if (vgi && vgi[0]) {
        snprintf(path, size, "%s", vgi);
        return 1;
    }
    const char* home = getenv("HOME");
    if (!home) return 0;
    snprintf(path, size, "%s/.steam/steam/config/virtualgamepadinfo.txt", home);
    return 1;
}

// Parse ~/.steam/steam/config/virtualgamepadinfo.txt
static void parse_virtualgamepadinfo(void) {
    num_steam_virtual = 0;

    char path[512];
    if (!vgi_path(path, sizeof(path))) return;

    FILE* f = fopen(path, "r");
    if (!f) {
        VLOG("Steam Input: virtualgamepadinfo.txt not found");
        return;
    }

    VLOG("Steam Input: Parsing %s", path);
    struct stat vst;
    vgi_mtime = (fstat(fileno(f), &vst) == 0) ? vst.st_mtime : 0;

    char line[256];
    SteamVirtualController* current = NULL;

    while (fgets(line, sizeof(line), f)) {
        // Remove trailing newline
        size_t len = strlen(line);
        if (len > 0 && line[len-1] == '\n') line[len-1] = '\0';

        // Check for slot header [slot N]
        int slot;
        if (sscanf(line, "[slot %d]", &slot) == 1) {
            if (num_steam_virtual < MAX_CONTROLLERS) {
                current = &steam_virtual_controllers[num_steam_virtual++];
                memset(current, 0, sizeof(*current));
                current->valid = 1;
                current->slot = slot;
                current->js_index = -1;
                current->physical_index = -1;
            }
            continue;
        }

        if (!current) continue;

        // Parse key=value pairs
        char* eq = strchr(line, '=');
        if (!eq) continue;
        *eq = '\0';
        char* key = line;
        char* value = eq + 1;

        if (strcmp(key, "name") == 0) {
            safe_strcpy(current->name, value, sizeof(current->name));
        } else if (strcmp(key, "VID") == 0) {
            current->vid = (unsigned short)strtol(value, NULL, 16);
        } else if (strcmp(key, "PID") == 0) {
            current->pid = (unsigned short)strtol(value, NULL, 16);
        } else if (strcmp(key, "handle") == 0) {
            safe_strcpy(current->handle, value, sizeof(current->handle));
        } else if (strcmp(key, "type") == 0) {
            safe_strcpy(current->type, value, sizeof(current->type));
        }
    }

    fclose(f);

    VLOG("Steam Input: Found %d virtual controller slots", num_steam_virtual);
    for (int i = 0; i < num_steam_virtual; i++) {
        VLOG("  Slot %d: %s [%04x:%04x] handle=%s type=%s",
            steam_virtual_controllers[i].slot,
            steam_virtual_controllers[i].name,
            steam_virtual_controllers[i].vid,
            steam_virtual_controllers[i].pid,
            steam_virtual_controllers[i].handle,
            steam_virtual_controllers[i].type);
    }
}

// Find Steam virtual controller js* devices (vendor 28de)
static void find_steam_virtual_js_devices(void) {
    for (int i = 0; i < num_steam_virtual; i++) {
        steam_virtual_controllers[i].js_index = -1;
    }

    // Collect all Valve virtual js devices in order
    int virtual_js_devices[MAX_CONTROLLERS];
    char virtual_names[MAX_CONTROLLERS][256];
    int num_virtual_found = 0;

    // Scan js devices for Valve virtual controllers
    for (int js = 0; js < 16 && num_virtual_found < MAX_CONTROLLERS; js++) {
        char sysfs_path[256];
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d/device/id/vendor", js);
        unsigned short vendor = read_sysfs_hex(sysfs_path);

        if (vendor != VALVE_VENDOR_ID) continue;

        // Get the device name
        char path[64];
        snprintf(path, sizeof(path), "/dev/input/js%d", js);
        int fd = real_open(path, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;

        char name[256] = "";
        sys_ioctl(fd, JSIOCGNAME(sizeof(name)), name);
        real_close(fd);

        LOG("Steam Input: Found virtual device js%d: '%s'", js, name);
        virtual_js_devices[num_virtual_found] = js;
        strncpy(virtual_names[num_virtual_found], name, sizeof(virtual_names[0]) - 1);
        virtual_names[num_virtual_found][sizeof(virtual_names[0]) - 1] = '\0';
        num_virtual_found++;
    }

    // Match virtual devices to slots by "pad N" suffix in device name
    // e.g. "Microsoft X-Box 360 pad 1" -> slot 1
    for (int i = 0; i < num_virtual_found; i++) {
        int js = virtual_js_devices[i];
        const char *pad_str = strstr(virtual_names[i], "pad ");
        if (!pad_str) continue;
        int pad_num = atoi(pad_str + 4);
        for (int s = 0; s < num_steam_virtual; s++) {
            if (steam_virtual_controllers[s].slot == pad_num &&
                steam_virtual_controllers[s].js_index < 0) {
                steam_virtual_controllers[s].js_index = js;
                LOG("Steam Input: Matched slot %d -> js%d (by pad name)", pad_num, js);
                break;
            }
        }
    }

    // Fallback: assign remaining unmatched devices by order
    int fallback_slot = 0;
    for (int i = 0; i < num_virtual_found; i++) {
        int js = virtual_js_devices[i];
        int already_matched = 0;
        for (int s = 0; s < num_steam_virtual; s++) {
            if (steam_virtual_controllers[s].js_index == js) { already_matched = 1; break; }
        }
        if (already_matched) continue;
        while (fallback_slot < num_steam_virtual &&
               steam_virtual_controllers[fallback_slot].js_index >= 0) {
            fallback_slot++;
        }
        if (fallback_slot < num_steam_virtual) {
            steam_virtual_controllers[fallback_slot].js_index = js;
            LOG("Steam Input: Matched slot %d -> js%d (by device order fallback)",
                steam_virtual_controllers[fallback_slot].slot, js);
            fallback_slot++;
        }
    }
}

// Match Steam virtual controllers to physical controllers.
//
// Pass 1: MAC-based. Steam's handle for some controllers (e.g. PS4/PS5 over
// Bluetooth) encodes the device MAC as "0x0000<mac>", which uniquely
// identifies the physical device.
//
// Pass 2: VID:PID match when there is exactly one physical of that
// VID:PID. Safe disambiguation by elimination.
//
// When neither succeeds (e.g. two identical Xbox Wireless Controllers
// over Bluetooth — Steam's handle for those embeds VID:PID + a *shared*
// serial, so the two virtuals are indistinguishable from the file
// alone), leave `physical_index = -1` and let the caller fall back to
// the pass-through path: allow the assigned physical's evdev node, block
// all virtuals. Ordinal-by-slot guessing is unreliable because Steam's
// slot order does not always match kernel enumeration order, and a wrong
// guess causes the assigned virtual to carry the *other* physical's
// input — instance receives both controllers.
static void match_virtual_to_physical(void) {
    for (int v = 0; v < num_steam_virtual; v++) {
        steam_virtual_controllers[v].physical_index = -1;
    }

    for (int v = 0; v < num_steam_virtual; v++) {
        SteamVirtualController* virt = &steam_virtual_controllers[v];
        if (!virt->valid || strlen(virt->handle) == 0) continue;

        // Pass 1: MAC-based
        for (int p = 0; p < num_kernel_controllers; p++) {
            KernelController* phys = &kernel_controllers[p];
            if (!phys->valid) continue;
            if (phys->vendor_id == VALVE_VENDOR_ID) continue;
            if (strlen(phys->uniq) == 0) continue;

            char phys_handle[32];
            mac_to_steam_handle(phys->uniq, phys_handle, sizeof(phys_handle));
            if (strcmp(virt->handle, phys_handle) == 0) {
                virt->physical_index = p;
                LOG("Steam Input: Virtual slot %d (js%d) matches physical %d (%s) via MAC",
                    virt->slot, virt->js_index, p, phys->name);
                break;
            }
        }
        if (virt->physical_index >= 0) continue;

        // Pass 2: VID:PID when exactly one physical of that type
        int count = 0;
        int last_p = -1;
        for (int p = 0; p < num_kernel_controllers; p++) {
            KernelController* phys = &kernel_controllers[p];
            if (!phys->valid) continue;
            if (phys->vendor_id == VALVE_VENDOR_ID) continue;
            if (phys->vendor_id == virt->vid && phys->product_id == virt->pid) {
                count++;
                last_p = p;
            }
        }
        if (count == 1) {
            virt->physical_index = last_p;
            LOG("Steam Input: Virtual slot %d (js%d) matches physical %d via VID:PID (unique)",
                virt->slot, virt->js_index, last_p);
        } else if (count > 1) {
            LOG("Steam Input: Virtual slot %d (js%d) [%04x:%04x] has %d physical candidates — "
                "cannot disambiguate, will fall through to physical evdev pass-through",
                virt->slot, virt->js_index, virt->vid, virt->pid, count);
        }
    }
}

// Pads whose kernel VID:PID matches no Steam slot (a third-party "Lic Pro
// Controller" over Bluetooth is 0000:0000 to the kernel, 057e:2009 to Steam)
// used to fall back to EVERY slot and pick by instance rank, i.e. often
// another pad's virtual: "one controller drives two instances" (2026-10-25).
// Drop the slots that provably belong to another pad: matched to a different
// physical, or with a serial/MAC handle (one that doesn't embed its own
// "<vid><pid>" the way Steam's serial-less handles do) that isn't ours (pass 1
// of match_virtual_to_physical would have caught ours). Keeps `cand` when
// nothing would be left. Returns the new count.
static int drop_other_pads_slots(int* cand, int nc) {
    const char* uniq = (assigned_controller >= 0 &&
                        assigned_controller < num_kernel_controllers)
                       ? kernel_controllers[assigned_controller].uniq : "";
    char mine[32];
    mac_to_steam_handle(uniq, mine, sizeof(mine));
    int keep[MAX_CONTROLLERS], nk = 0;
    for (int c = 0; c < nc; c++) {
        SteamVirtualController* v = &steam_virtual_controllers[cand[c]];
        if (v->physical_index >= 0 && v->physical_index != assigned_controller) continue;
        char key[16];
        snprintf(key, sizeof(key), "%x%x", v->vid, v->pid);
        if (v->handle[0] && !strstr(v->handle, key) && strcmp(v->handle, mine) != 0) continue;
        keep[nk++] = cand[c];
    }
    // Every slot belongs to another pad: Steam made none for this one (e.g.
    // it lost the pad's hidraw). Taking one anyway handed this instance the
    // other pad's virtual — one pad drove both (2026-10-25, Pro Controller
    // without a slot took the Xbox's). No candidates -> pass-through.
    if (nk == 0) {
        LOG("Steam Input: all %d candidate slots belong to other pads, none for this one", nc);
        return 0;
    }
    if (nk < nc)
        LOG("Steam Input: %d of %d candidate slots belong to other pads, dropped", nc - nk, nc);
    memcpy(cand, keep, nk * sizeof(int));
    return nk;
}

// Kernel input-device number (the N of /sys/class/input/inputN) behind
// /dev/input/eventE. Assigned in registration order, so it orders pads by
// when they were connected.
static int input_number_of_event(int event_idx) {
    char link[128], real[PATH_MAX];
    snprintf(link, sizeof(link), "/sys/class/input/event%d/device", event_idx);
    if (!realpath(link, real)) return -1;
    const char* base = strrchr(real, '/');
    return (base && strncmp(base + 1, "input", 5) == 0) ? atoi(base + 6) : -1;
}

// Steam rewrites virtualgamepadinfo.txt only once a Steam Input game is running
// (~15-20 s after launch), so right after a pad is (re)connected the hooks read
// the PREVIOUS slot order: every instance then gets the other pad's virtual
// (2026-10-09, Palworld after replugging the Xbox pad). Steam numbers slots in
// connection order (slot 0 = earliest-connected pad), so when the file predates
// the newest physical pad, map slot k -> k-th pad by kernel input number.
static void fix_stale_vgi_slots(void) {
    int phys[MAX_CONTROLLERS], num[MAX_CONTROLLERS], n = 0;
    time_t newest = 0;
    for (int p = 0; p < num_kernel_controllers && n < MAX_CONTROLLERS; p++) {
        KernelController* k = &kernel_controllers[p];
        if (!k->valid || k->vendor_id == VALVE_VENDOR_ID || k->event_index < 0) continue;
        char node[64];
        struct stat st;
        snprintf(node, sizeof(node), "/dev/input/event%d", k->event_index);
        if (stat(node, &st) == 0 && st.st_ctime > newest) newest = st.st_ctime;
        int in = input_number_of_event(k->event_index);
        if (in < 0) return;  // can't order reliably
        // insertion sort by input number
        int j = n++;
        while (j > 0 && num[j - 1] > in) { phys[j] = phys[j - 1]; num[j] = num[j - 1]; j--; }
        phys[j] = p; num[j] = in;
    }
    if (n == 0 || !vgi_mtime || vgi_mtime >= newest) return;  // file is current
    LOG("Steam Input: virtualgamepadinfo.txt (mtime %ld) predates the newest pad "
        "connection (%ld) — mapping slots by connection order instead",
        (long)vgi_mtime, (long)newest);
    for (int i = 0; i < num_steam_virtual; i++) {
        SteamVirtualController* v = &steam_virtual_controllers[i];
        if (v->slot >= 0 && v->slot < n) {
            KernelController* k = &kernel_controllers[phys[v->slot]];
            v->physical_index = phys[v->slot];
            v->vid = k->vendor_id;
            v->pid = k->product_id;
            v->handle[0] = '\0';
            LOG("Steam Input: slot %d (js%d) -> physical %d %s [%04x:%04x] (connection order)",
                v->slot, v->js_index, phys[v->slot], k->name, v->vid, v->pid);
        } else {
            v->physical_index = -1;
        }
    }
}

// Steam handle for this instance's pad from the session's pad matcher
// (JANUS_PAD_MAP file, "handle=0x..."). 0 if there's none (yet).
static int read_pad_map_handle(char* out, size_t size) {
    const char* path = getenv("JANUS_PAD_MAP");
    if (!path || !path[0]) return 0;
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    char line[128];
    int ok = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "handle=", 7) == 0) {
            line[strcspn(line, "\r\n")] = '\0';
            snprintf(out, size, "%s", line + 7);
            ok = out[0] != '\0';
            break;
        }
    }
    fclose(f);
    return ok;
}

// Check if Steam Input is active and set up mappings
static void detect_steam_input(void) {
    if (steam_input_checked) return;

    // Check if user wants to bypass Steam Input detection
    const char* no_steam = getenv("JANUS_NO_STEAM_INPUT");
    if (no_steam && strcmp(no_steam, "0") != 0) {
        LOG("Steam Input: Bypassed by JANUS_NO_STEAM_INPUT");
        steam_input_checked = 1;
        steam_input_active = 0;
        return;
    }

    VLOG("detect_steam_input: assigned_controller=%d, num_kernel_controllers=%d",
        assigned_controller, num_kernel_controllers);

    // Parse virtualgamepadinfo.txt
    parse_virtualgamepadinfo();

    if (num_steam_virtual == 0) {
        VLOG("Steam Input: No virtual controllers configured");
        steam_input_checked = 1;
        return;
    }

    // Find the actual js devices for virtual controllers
    find_steam_virtual_js_devices();

    // Check if any virtual devices exist
    int found_virtual = 0;
    for (int i = 0; i < num_steam_virtual; i++) {
        if (steam_virtual_controllers[i].js_index >= 0) {
            found_virtual = 1;
            break;
        }
    }

    if (!found_virtual) {
        VLOG("Steam Input: Virtual devices not created yet");
        // Don't mark as checked - we'll retry later
        return;
    }

    steam_input_active = 1;
    steam_input_checked = 1;

    LOG("Steam Input: ACTIVE - virtual controllers detected, using Steam Input mode");
    LOG("Steam Input: Will attempt per-instance virtual controller filtering");

    // Match virtual to physical
    match_virtual_to_physical();
    fix_stale_vgi_slots();

    // Find the virtual js device for our assigned physical controller
    if (assigned_controller >= 0) {
        for (int i = 0; i < num_steam_virtual; i++) {
            if (steam_virtual_controllers[i].physical_index == assigned_controller &&
                steam_virtual_controllers[i].js_index >= 0) {
                assigned_virtual_js = steam_virtual_controllers[i].js_index;
                LOG("Steam Input: Assigned physical %d maps to virtual js%d",
                    assigned_controller, assigned_virtual_js);
                break;
            }
        }

        // match_virtual_to_physical() couldn't uniquely bind this physical. Two cases:
        //  (1) genuinely NOT virtualized (no virtual slot for this pad) -> pass through;
        //  (2) AMBIGUOUS: identical MAC-less pads, virtual slots exist but can't be told
        //      apart by metadata. Old code wrongly concluded (1) and let instances collide
        //      onto the same virtual node (the "one pad drives both" leak). Instead pick a
        //      DISTINCT virtual by per-instance RANK (JANUS_INSTANCE_RANK), so instances
        //      never collide (worst case swapped — swap the pads to fix).
        // Identical pads: the session's PadMatcher (pad_matcher.py) matched this
        // pad to its Steam handle by watching which virtual pad reacts to its
        // presses, keeping the whole group one-to-one. Its answer wins.
        if (assigned_virtual_js < 0) {
            char want[64];
            if (read_pad_map_handle(want, sizeof(want))) {
                for (int i = 0; i < num_steam_virtual; i++) {
                    if (steam_virtual_controllers[i].js_index >= 0 &&
                        strcmp(steam_virtual_controllers[i].handle, want) == 0) {
                        assigned_virtual_js = steam_virtual_controllers[i].js_index;
                        LOG("Steam Input: Assigned physical %d -> virtual js%d via the "
                            "session's pad matcher (slot %d, handle %s)", assigned_controller,
                            assigned_virtual_js, steam_virtual_controllers[i].slot, want);
                        return;
                    }
                }
                LOG("Steam Input: pad matcher handle %s not in virtualgamepadinfo.txt yet", want);
            }
        }

        if (assigned_virtual_js < 0) {
            unsigned short pv = 0, pp = 0;
            if (assigned_controller < num_kernel_controllers) {
                pv = kernel_controllers[assigned_controller].vendor_id;
                pp = kernel_controllers[assigned_controller].product_id;
            }
            // Candidates: virtual slots matching the physical VID:PID; if Steam recorded a
            // generic VID (none match), fall back to ALL virtual slots.
            int cand[MAX_CONTROLLERS], nc = 0;
            for (int i = 0; i < num_steam_virtual; i++)
                if (steam_virtual_controllers[i].js_index >= 0 &&
                    steam_virtual_controllers[i].vid == pv &&
                    steam_virtual_controllers[i].pid == pp)
                    cand[nc++] = i;
            if (nc == 0) {
                for (int i = 0; i < num_steam_virtual; i++)
                    if (steam_virtual_controllers[i].js_index >= 0)
                        cand[nc++] = i;
                nc = drop_other_pads_slots(cand, nc);
            }
            // Consistent order across instances: sort candidates by Steam slot.
            for (int a = 1; a < nc; a++) {
                int key = cand[a], b = a - 1;
                while (b >= 0 && steam_virtual_controllers[cand[b]].slot >
                                 steam_virtual_controllers[key].slot) {
                    cand[b + 1] = cand[b]; b--;
                }
                cand[b + 1] = key;
            }
            // Identity first: a serial-less pad's handle embeds Steam's controller
            // INDEX ("device opened for index N" in logs/controller.txt) as the hex
            // digit just before "<vid><pid>": MACHENIKE 0x0|36e83001|d48d305,
            // 0x1|36e83001|.., Xbox 0x00|2|45eb12|... It is NOT the Steam slot (slot
            // 1 carried 1, slot 2 carried 0). Steam opens pads in kernel event order
            // and keeps a pad's index while it stays connected, but the kernel list
            // shifts when any pad comes or goes (2026-10-17: a MACHENIKE dropped and
            // the PS1 instance took the other adapter port's handle). So compare
            // ranks within the model: the assigned pad's rank among same-VID:PID pads
            // by event node <-> the same rank among that model's handles by Steam
            // index. Rank alone picked by instance number, which is wrong whenever an
            // identical pad is plugged in but unassigned (2026-10-07). See NOTES.md.
            if (nc > 0 && assigned_controller < num_kernel_controllers && pv) {
                int my_ev = kernel_controllers[assigned_controller].event_index;
                int rank = 0;
                for (int p = 0; p < num_kernel_controllers; p++)
                    if (p != assigned_controller && kernel_controllers[p].valid &&
                        kernel_controllers[p].vendor_id == pv &&
                        kernel_controllers[p].product_id == pp &&
                        kernel_controllers[p].event_index < my_ev)
                        rank++;
                int hcand[MAX_CONTROLLERS], hidx[MAX_CONTROLLERS], nh = 0;
                for (int c = 0; c < nc; c++) {
                    SteamVirtualController* v = &steam_virtual_controllers[cand[c]];
                    if (v->vid != pv || v->pid != pp) continue;
                    // "<vid><pid>" are unpadded lowercase hex, as in Steam's
                    // '45e-b12-d48d305' serial; a '0'/'x' before them means 0.
                    char key[16];
                    snprintf(key, sizeof(key), "%x%x", pv, pp);
                    const char* at = strstr(v->handle, key);
                    if (!at) continue;
                    int vo = 0;
                    if (at > v->handle && isxdigit((unsigned char)at[-1]))
                        vo = (int)strtol((char[]){at[-1], 0}, NULL, 16);
                    // insertion-sort by Steam index
                    int k = nh++;
                    while (k > 0 && hidx[k - 1] > vo) {
                        hidx[k] = hidx[k - 1]; hcand[k] = hcand[k - 1]; k--;
                    }
                    hidx[k] = vo; hcand[k] = cand[c];
                }
                int dup = 0;
                for (int k = 1; k < nh; k++) if (hidx[k] == hidx[k - 1]) dup = 1;
                if (rank < nh && !dup) {
                    int hit = hcand[rank];
                    assigned_virtual_js = steam_virtual_controllers[hit].js_index;
                    LOG("Steam Input: Assigned physical %d -> virtual js%d via handle "
                        "rank %d of %d (Steam index %d, slot %d, handle %s)",
                        assigned_controller, assigned_virtual_js, rank, nh, hidx[rank],
                        steam_virtual_controllers[hit].slot,
                        steam_virtual_controllers[hit].handle);
                    return;
                }
                LOG("Steam Input: handle rank %d of %d (duplicate indices: %d) — falling back to rank",
                    rank, nh, dup);
            }
            if (nc > 0) {
                const char* rank_env = getenv("JANUS_INSTANCE_RANK");
                int rank = rank_env ? atoi(rank_env) : 0;
                if (rank < 0) rank = 0;
                int pick = (rank < nc) ? rank : (rank % nc);
                assigned_virtual_js = steam_virtual_controllers[cand[pick]].js_index;
                if (nc == 1)
                    LOG("Steam Input: Assigned physical %d -> virtual js%d (the only slot "
                        "left, slot %d)", assigned_controller, assigned_virtual_js,
                        steam_virtual_controllers[cand[0]].slot);
                else
                    LOG("Steam Input: Assigned physical %d -> virtual js%d via RANK %d "
                        "(distinct fallback; %d candidates). Matcher will refine.",
                        assigned_controller, assigned_virtual_js, rank, nc);
            } else {
                LOG("Steam Input: Assigned physical %d passes through directly (not virtualized)",
                    assigned_controller);
            }
        }
    }
}

// Check if a js device should be blocked
// No redirection — js redirection (js0→jsN) corrupts SDL2's button mapping because
// SDL2 reads sysfs metadata for the original path but gets a different device's fd.
// Instead, simply block/allow like event devices.
static int should_block_js_device(int js_idx) {
    if (assigned_controller < 0) return 0;  // No filtering

    // Filter mode: the game reads controllers via Steam Input (vtable-filtered to the
    // assigned pad), so block EVERY physical controller js node — otherwise the game
    // also reads the pad via SDL/evdev (double input / cross-talk). Keyboard/mouse are
    // not js devices, so unaffected.
    if (steam_filter_on()) {
        for (int i = 0; i < num_kernel_controllers; i++)
            if (kernel_controllers[i].js_index == js_idx)
                return 1;
    }

    // Try Steam Input detection if not done
    if (!steam_input_checked) {
        detect_steam_input();
    }

    // In Steam Input mode
    if (steam_input_active) {
        // Allow our assigned virtual device
        if (assigned_virtual_js >= 0 && js_idx == assigned_virtual_js) {
            return 0;  // Don't block
        }

        // Allow our assigned physical device if it passes through directly (not virtualized)
        if (assigned_virtual_js < 0 && assigned_controller < num_kernel_controllers) {
            if (js_idx == kernel_controllers[assigned_controller].js_index) {
                return 0;  // Don't block
            }
        }

        // Block everything else (other virtual devices, other physical controllers)
        return 1;
    }

    // Direct mode - block if not our assigned controller
    for (int i = 0; i < num_kernel_controllers; i++) {
        if (kernel_controllers[i].js_index == js_idx) {
            return i != assigned_controller;
        }
    }

    return 0;  // Not a known controller, allow
}

// =============================================================================
// SDL2 Types and Function Pointers
// =============================================================================

typedef struct _SDL_Joystick SDL_Joystick;
typedef struct _SDL_GameController SDL_GameController;
typedef struct { unsigned char data[16]; } SDL_JoystickGUID;

static int (*real_SDL_NumJoysticks)(void) = NULL;
static const char* (*real_SDL_JoystickNameForIndex)(int) = NULL;
static SDL_JoystickGUID (*real_SDL_JoystickGetDeviceGUID)(int) = NULL;
static SDL_Joystick* (*real_SDL_JoystickOpen)(int) = NULL;
static SDL_GameController* (*real_SDL_GameControllerOpen)(int) = NULL;
static int (*real_SDL_IsGameController)(int) = NULL;
static unsigned short (*real_SDL_JoystickGetDeviceVendor)(int) = NULL;
static unsigned short (*real_SDL_JoystickGetDeviceProduct)(int) = NULL;

// =============================================================================
// SDL3 Types and Function Pointers
// =============================================================================

typedef unsigned int SDL_JoystickID;  // SDL3 uses Uint32 for instance IDs
typedef struct _SDL_Gamepad SDL_Gamepad;

// SDL3 function pointers
static SDL_JoystickID* (*real_SDL_GetJoysticks)(int*) = NULL;
static SDL_JoystickID* (*real_SDL_GetGamepads)(int*) = NULL;
static SDL_Joystick* (*real_SDL_OpenJoystick)(SDL_JoystickID) = NULL;
static SDL_Gamepad* (*real_SDL_OpenGamepad)(SDL_JoystickID) = NULL;
static const char* (*real_SDL_GetJoystickNameForID)(SDL_JoystickID) = NULL;
static unsigned short (*real_SDL_GetJoystickVendorForID)(SDL_JoystickID) = NULL;
static unsigned short (*real_SDL_GetJoystickProductForID)(SDL_JoystickID) = NULL;
static void (*real_SDL_free)(void*) = NULL;

// SDL3 mapping state
static SDL_JoystickID sdl3_assigned_instance_id = 0;
static int sdl3_mapping_done = 0;

// =============================================================================
// SDL Index Mapping
// =============================================================================

static int sdl_assigned_index = -1;  // The SDL index that corresponds to our assigned controller
static int sdl_mapping_done = 0;
static int sdl_last_count = 0;  // Track if joystick count changes

static void build_sdl_mapping(void) {
    if (!real_SDL_NumJoysticks) return;

    int num_sdl = real_SDL_NumJoysticks();

    // Skip if no joysticks yet, or if count hasn't changed and we already mapped
    if (num_sdl == 0) return;
    if (sdl_mapping_done && num_sdl == sdl_last_count) return;

    // Re-enumerate kernel controllers on hot-plug (count changed since last mapping)
    if (sdl_mapping_done) {
        LOG("Hot-plug: SDL joystick count changed (%d -> %d), re-enumerating kernel controllers",
            sdl_last_count, num_sdl);
        enumerate_kernel_controllers();
        re_match_assigned_controller();
    }

    sdl_mapping_done = 1;
    sdl_last_count = num_sdl;
    sdl_assigned_index = -1;  // Reset in case of re-mapping

    LOG("Building SDL index mapping (%d joysticks)...", num_sdl);

    // Track which kernel controllers have been matched
    int kernel_matched[MAX_CONTROLLERS] = {0};

    // For each SDL joystick, find matching kernel controller
    for (int sdl_idx = 0; sdl_idx < num_sdl; sdl_idx++) {
        const char* sdl_name = real_SDL_JoystickNameForIndex ?
                               real_SDL_JoystickNameForIndex(sdl_idx) : "Unknown";

        // Get SDL vendor/product IDs if available
        unsigned short sdl_vendor = 0, sdl_product = 0;
        if (real_SDL_JoystickGetDeviceVendor && real_SDL_JoystickGetDeviceProduct) {
            sdl_vendor = real_SDL_JoystickGetDeviceVendor(sdl_idx);
            sdl_product = real_SDL_JoystickGetDeviceProduct(sdl_idx);
        }

        LOG("  SDL joystick %d: %s [%04x:%04x]", sdl_idx, sdl_name ? sdl_name : "Unknown",
            sdl_vendor, sdl_product);

        // Try matching by vendor/product ID first (most reliable)
        int matched_kernel = -1;

        if (sdl_vendor != 0 && sdl_product != 0) {
            for (int k = 0; k < num_kernel_controllers; k++) {
                if (kernel_controllers[k].valid && !kernel_matched[k] &&
                    kernel_controllers[k].vendor_id == sdl_vendor &&
                    kernel_controllers[k].product_id == sdl_product) {
                    matched_kernel = k;
                    kernel_matched[k] = 1;
                    LOG("    -> matches kernel %d by VID:PID [%04x:%04x]",
                        k, sdl_vendor, sdl_product);
                    break;
                }
            }
        }

        // Fallback to name matching if VID:PID not available or didn't match
        if (matched_kernel < 0 && sdl_name) {
            for (int k = 0; k < num_kernel_controllers; k++) {
                if (kernel_controllers[k].valid && !kernel_matched[k]) {
                    if (strstr(kernel_controllers[k].name, sdl_name) ||
                        strstr(sdl_name, kernel_controllers[k].name) ||
                        (strstr(kernel_controllers[k].name, "Xbox") && strstr(sdl_name, "Xbox")) ||
                        (strstr(kernel_controllers[k].name, "Sony") && strstr(sdl_name, "PS")) ||
                        (strstr(kernel_controllers[k].name, "Wireless Controller") && strstr(sdl_name, "PS"))) {
                        matched_kernel = k;
                        kernel_matched[k] = 1;
                        LOG("    -> matches kernel %d by name fallback (%s)",
                            k, kernel_controllers[k].name);
                        break;
                    }
                }
            }
        }

        if (matched_kernel >= 0 && matched_kernel == assigned_controller) {
            sdl_assigned_index = sdl_idx;
            LOG("Assigned controller (kernel %d) maps to SDL index %d", matched_kernel, sdl_idx);
        }
    }
}

// =============================================================================
// Initialization
// =============================================================================

static void ensure_init(void) {
    if (hook_initialized) return;
    hook_initialized = 1;

    if (getenv("JANUS_VERBOSE")) {
        verbose_logging = 1;
    }

    // First, enumerate kernel controllers (canonical ordering)
    enumerate_kernel_controllers();

    // Read configuration
    const char* env = getenv("JANUS_CONTROLLER");
    const char* env_name = getenv("JANUS_CONTROLLER_NAME");
    const char* env_vid = getenv("JANUS_CONTROLLER_VID");
    const char* env_pid = getenv("JANUS_CONTROLLER_PID");
    // VID/PID are passed as decimal (the GUI stores them that way); kernel
    // vendor/product ids are the same numeric value (sysfs hex "045e" == 1118).
    unsigned short want_vid = env_vid ? (unsigned short)atoi(env_vid) : 0;
    unsigned short want_pid = env_pid ? (unsigned short)atoi(env_pid) : 0;
    int index_hint = env ? atoi(env) : -1;
    int identity_from_env = 0;

    if (want_vid != 0 && want_pid != 0) {
        // Identity-based assignment — robust to kernel index shifts when a
        // wireless pad disconnects/renumbers (js0,js1,js2,js3 -> js0,js2,js3
        // moves index 1 onto a different pad). Record the INTENDED identity up
        // front so hot-plug re-matching always targets the right hardware even
        // if the pad isn't connected at launch.
        identity_from_env = 1;
        assigned_vid = want_vid;
        assigned_pid = want_pid;
        if (env_name && strlen(env_name) > 0)
            safe_strcpy(assigned_controller_name, env_name, sizeof(assigned_controller_name));

        int matches[MAX_CONTROLLERS], n_match = 0;
        for (int i = 0; i < num_kernel_controllers; i++) {
            if (kernel_controllers[i].valid &&
                kernel_controllers[i].vendor_id == want_vid &&
                kernel_controllers[i].product_id == want_pid) {
                matches[n_match++] = i;
            }
        }
        if (n_match == 1) {
            assigned_controller = matches[0];
            LOG("Controller filter by VID:PID %04x:%04x -> kernel index %d",
                want_vid, want_pid, assigned_controller);
        } else if (n_match > 1) {
            // Duplicate VID:PID (e.g. two identical pads): disambiguate with the
            // index hint if it points at one of them, else take the first match.
            assigned_controller = matches[0];
            for (int k = 0; k < n_match; k++) {
                if (matches[k] == index_hint) { assigned_controller = index_hint; break; }
            }
            LOG("Controller filter by VID:PID %04x:%04x (%d candidates) -> kernel index %d (hint %d)",
                want_vid, want_pid, n_match, assigned_controller, index_hint);
        } else {
            // Not connected at launch — keep the identity so hot-plug binds it
            // when it appears. Fall back to the index hint so we still filter
            // *something* meanwhile (better than the no-filter default, which
            // would expose every pad to this instance).
            assigned_controller = (index_hint >= 0 && index_hint < num_kernel_controllers)
                                  ? index_hint : -1;
            LOG("Controller VID:PID %04x:%04x not connected at launch; will bind on "
                "hot-plug (fallback index %d)", want_vid, want_pid, assigned_controller);
        }
    } else if (env_name && strlen(env_name) > 0) {
        // Match by name
        safe_strcpy(assigned_controller_name, env_name, sizeof(assigned_controller_name));
        assigned_controller = find_kernel_index_by_name(env_name);
        if (assigned_controller >= 0) {
            LOG("Controller filter by name '%s' -> kernel index %d", env_name, assigned_controller);
        } else {
            LOG("WARNING: Controller name '%s' not found!", env_name);
        }
    } else if (env) {
        // Match by kernel index
        assigned_controller = atoi(env);
        LOG("Controller filter by kernel index: %d", assigned_controller);
        if (assigned_controller < num_kernel_controllers && kernel_controllers[assigned_controller].valid) {
            safe_strcpy(assigned_controller_name, kernel_controllers[assigned_controller].name,
                        sizeof(assigned_controller_name));
            LOG("  -> controller name: %s", assigned_controller_name);
        }
    } else {
        LOG("No JANUS_CONTROLLER set, logging only (no filtering)");
    }

    remember_assigned_phys();

    // Save VID:PID for hot-plug re-matching. Skip when identity came from env —
    // we already recorded the intended VID:PID (which may differ from whatever
    // is at the fallback index right now).
    if (!identity_from_env && assigned_controller >= 0 && assigned_controller < num_kernel_controllers &&
        kernel_controllers[assigned_controller].valid) {
        assigned_vid = kernel_controllers[assigned_controller].vendor_id;
        assigned_pid = kernel_controllers[assigned_controller].product_id;
        LOG("  -> VID:PID for hot-plug: %04x:%04x", assigned_vid, assigned_pid);
    }
    remember_assigned_ordinals();

    // Load SDL2 functions. Keep any the dlsym hook already found: it can run
    // first (sdl2-compat resolves SDL3 from its RTLD_LOCAL handle in its own
    // constructor), and RTLD_NEXT finds nothing in a library loaded that way.
    if (!real_SDL_NumJoysticks) real_SDL_NumJoysticks = dlsym(RTLD_NEXT, "SDL_NumJoysticks");
    if (!real_SDL_JoystickNameForIndex) real_SDL_JoystickNameForIndex = dlsym(RTLD_NEXT, "SDL_JoystickNameForIndex");
    if (!real_SDL_JoystickGetDeviceGUID) real_SDL_JoystickGetDeviceGUID = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceGUID");
    if (!real_SDL_JoystickOpen) real_SDL_JoystickOpen = dlsym(RTLD_NEXT, "SDL_JoystickOpen");
    if (!real_SDL_GameControllerOpen) real_SDL_GameControllerOpen = dlsym(RTLD_NEXT, "SDL_GameControllerOpen");
    if (!real_SDL_IsGameController) real_SDL_IsGameController = dlsym(RTLD_NEXT, "SDL_IsGameController");
    if (!real_SDL_JoystickGetDeviceVendor) real_SDL_JoystickGetDeviceVendor = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceVendor");
    if (!real_SDL_JoystickGetDeviceProduct) real_SDL_JoystickGetDeviceProduct = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceProduct");

    // Load SDL3 functions
    if (!real_SDL_GetJoysticks) real_SDL_GetJoysticks = dlsym(RTLD_NEXT, "SDL_GetJoysticks");
    if (!real_SDL_GetGamepads) real_SDL_GetGamepads = dlsym(RTLD_NEXT, "SDL_GetGamepads");
    if (!real_SDL_OpenJoystick) real_SDL_OpenJoystick = dlsym(RTLD_NEXT, "SDL_OpenJoystick");
    if (!real_SDL_OpenGamepad) real_SDL_OpenGamepad = dlsym(RTLD_NEXT, "SDL_OpenGamepad");
    if (!real_SDL_GetJoystickNameForID) real_SDL_GetJoystickNameForID = dlsym(RTLD_NEXT, "SDL_GetJoystickNameForID");
    if (!real_SDL_GetJoystickVendorForID) real_SDL_GetJoystickVendorForID = dlsym(RTLD_NEXT, "SDL_GetJoystickVendorForID");
    if (!real_SDL_GetJoystickProductForID) real_SDL_GetJoystickProductForID = dlsym(RTLD_NEXT, "SDL_GetJoystickProductForID");
    if (!real_SDL_free) real_SDL_free = dlsym(RTLD_NEXT, "SDL_free");

    // File-open tracing: log write-intent opens to find the minimal set of
    // paths two instances would conflict on (so we can isolate just those
    // instead of the whole $HOME). See trace_file_open().
    const char* trace_env = getenv("JANUS_TRACE_OPEN");
    trace_open_enabled = (trace_env && strcmp(trace_env, "0") != 0);
    if (trace_open_enabled) {
        LOG("File-open tracing ENABLED (logging write-opens outside /dev,/proc,/sys,/tmp)");
    }

    LOG("Hook initialized");
    LOG("  SDL2: NumJoysticks=%p", (void*)real_SDL_NumJoysticks);
    LOG("  SDL3: GetJoysticks=%p, OpenJoystick=%p", (void*)real_SDL_GetJoysticks, (void*)real_SDL_OpenJoystick);

    // Build the SDL2 index mapping
    if (real_SDL_NumJoysticks) {
        build_sdl_mapping();
    }
}

// Per-process env overrides keyed on argv (JANUS_ARGV_ENV_MATCH / JANUS_ARGV_ENV).
// A game may spawn a helper from its own environment that must differ from it:
// Terraria's "Host & Play" starts a server process (vanilla TerrariaServer, or
// tModLoader re-running its own dotnet with -server) that inherits the client's
// Goldberg settings and so its Steam ID. If any argv element equals
// JANUS_ARGV_ENV_MATCH, every "KEY=VALUE" line of JANUS_ARGV_ENV is setenv()'d.
static void apply_argv_env(void) {
    const char* match = getenv("JANUS_ARGV_ENV_MATCH");
    const char* spec = getenv("JANUS_ARGV_ENV");
    if (!match || !match[0] || !spec || !spec[0])
        return;
    ensure_evdev_init();  // real_open: our open() wrapper isn't ready yet
    int fd = real_open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    static char buf[16384];
    ssize_t n, len = 0;
    while (len < (ssize_t)sizeof(buf) - 1 &&
           (n = read(fd, buf + len, sizeof(buf) - 1 - len)) > 0)
        len += n;
    close(fd);
    buf[len] = '\0';
    bool hit = false;
    for (char* p = buf; p < buf + len; p += strlen(p) + 1)
        if (strcmp(p, match) == 0) { hit = true; break; }
    if (!hit)
        return;
    char* copy = strdup(spec);
    if (!copy)
        return;
    char* save = NULL;
    for (char* line = strtok_r(copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char* eq = strchr(line, '=');
        if (!eq || eq == line)
            continue;
        *eq = '\0';
        setenv(line, eq + 1, 1);
        LOG("ArgvEnv: argv has '%s' -> %s=%s", match, line, eq + 1);
    }
    free(copy);
}

__attribute__((constructor))
static void hook_constructor(void) {
    // Open the optional per-process log file before the first LOG() so the
    // banner (and everything after) is captured. Best-effort; ignore failures.
    const char* log_path = getenv("JANUS_LOG_FILE");
    if (log_path && log_path[0]) {
        char full[512];
        snprintf(full, sizeof(full), "%s.%d", log_path, (int)getpid());
        g_log_fp = fopen(full, "a");
    }

    LOG("=== Janus hook loaded === pid=%d %d-bit HOME=%s JANUS_CONTROLLER=%s "
        "JANUS_VGI_PATH=%s PROTON_DISABLE_LSTEAMCLIENT=%s",
        getpid(), (int)(sizeof(void*) * 8),
        getenv("HOME") ? getenv("HOME") : "(null)",
        getenv("JANUS_CONTROLLER") ? getenv("JANUS_CONTROLLER") : "(null)",
        getenv("JANUS_VGI_PATH") ? getenv("JANUS_VGI_PATH") : "(null)",
        getenv("PROTON_DISABLE_LSTEAMCLIENT") ? getenv("PROTON_DISABLE_LSTEAMCLIENT") : "(null)");

    // Before anything reads the environment (Goldberg reads GseAppPath at
    // SteamAPI_Init, long after this).
    apply_argv_env();

    // Goldberg: check for steam_api redirect path (arch-specific)
    if (sizeof(void*) == 8)
        goldberg_lib_path = getenv("JANUS_GOLDBERG_LIB");
    else
        goldberg_lib_path = getenv("JANUS_GOLDBERG_LIB32");
    if (goldberg_lib_path)
        LOG("Goldberg: will redirect libsteam_api.so to %s", goldberg_lib_path);

    // Steam's virtual gamepads and SDL. In Steam Input mode the evdev filter
    // leaves a native game only its own Steam virtual pad (physical ones are
    // blocked), so SDL must be allowed to open it, or the game has no controller
    // at all (and the virtual pad is what carries Steam's remapping). Kept off
    // (a) under Proton, unchanged from before (winebus reads the virtual pads
    // itself), and (b) with the vtable filter, where input comes through
    // ISteamInput and an SDL-visible virtual pad would double it.
    // STEAM_COMPAT_DATA_PATH marks a Proton launch for the whole process tree.
    if (getenv("JANUS_CONTROLLER")) {
        int allow = sdl_reads_steam_virtual_pad();
        unsetenv("SteamVirtualGamepadInfo");
        setenv("SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", allow ? "1" : "0", 1);
        LOG("Steam Input: %s virtual gamepads in SDL (unset SteamVirtualGamepadInfo, set hint=%d)",
            allow ? "allowed" : "disabled", allow);
    }

    // Initialize real_dlsym early (needed by focus-watch thread)
    if (!real_dlsym) {
        real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");  // x86_64 baseline
        if (!real_dlsym)
            real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");  // i386 baseline
        if (!real_dlsym)
            real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");  // glibc 2.34+
    }

    // Initialize real_dlopen early (must not call dl* from within dlopen hook)
    resolve_real_dlopen();
}

// =============================================================================
// SDL2 Hooked Functions (use sdl_assigned_index)
// =============================================================================

// Lazy-resolve SDL2 function pointers.
// SDL2 may not be loaded when ensure_init() runs (evdev hooks trigger init first).
// This re-resolves NULL pointers when SDL2 hooks are actually called.
static void ensure_sdl2_resolved(void) {
    if (real_SDL_NumJoysticks) return;  // Already resolved

    real_SDL_NumJoysticks = dlsym(RTLD_NEXT, "SDL_NumJoysticks");
    if (!real_SDL_NumJoysticks) return;  // SDL2 still not loaded

    LOG("SDL2 late-resolve: NumJoysticks=%p (SDL2 loaded after init)", (void*)real_SDL_NumJoysticks);

    // Resolve all other SDL2 functions too
    if (!real_SDL_JoystickNameForIndex)
        real_SDL_JoystickNameForIndex = dlsym(RTLD_NEXT, "SDL_JoystickNameForIndex");
    if (!real_SDL_JoystickGetDeviceGUID)
        real_SDL_JoystickGetDeviceGUID = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceGUID");
    if (!real_SDL_JoystickOpen)
        real_SDL_JoystickOpen = dlsym(RTLD_NEXT, "SDL_JoystickOpen");
    if (!real_SDL_GameControllerOpen)
        real_SDL_GameControllerOpen = dlsym(RTLD_NEXT, "SDL_GameControllerOpen");
    if (!real_SDL_IsGameController)
        real_SDL_IsGameController = dlsym(RTLD_NEXT, "SDL_IsGameController");
    if (!real_SDL_JoystickGetDeviceVendor)
        real_SDL_JoystickGetDeviceVendor = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceVendor");
    if (!real_SDL_JoystickGetDeviceProduct)
        real_SDL_JoystickGetDeviceProduct = dlsym(RTLD_NEXT, "SDL_JoystickGetDeviceProduct");

    // Now build the SDL mapping since we have the function pointers
    build_sdl_mapping();
}

// SDL2/SDL3 exports must be hidden - exporting them via LD_PRELOAD causes the dynamic
// linker to resolve games' SDL2 imports to OUR functions instead of the real SDL2.
// This breaks games like L4D2 that link SDL2 via ELF NEEDED (not dlopen/dlsym).
// Games that load SDL dynamically still get intercepted through the dlsym hook.
#define SDL2_HIDDEN __attribute__((visibility("hidden")))

SDL2_HIDDEN int SDL_NumJoysticks(void) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_NumJoysticks) return 0;

    int real_count = real_SDL_NumJoysticks();

    // Rebuild SDL mapping if needed (SDL may have initialized more joysticks)
    if (assigned_controller >= 0 && real_count > 0) {
        build_sdl_mapping();
    }

    if (assigned_controller >= 0) {
        if (sdl_assigned_index >= 0) {
            return (sdl_assigned_index < real_count) ? 1 : 0;
        }
        // Even without a mapping, return 1 if filtering is active
        // The actual filtering will happen in SDL_JoystickOpen/SDL_GameControllerOpen
        return (real_count > 0) ? 1 : 0;
    }

    return real_count;
}

SDL2_HIDDEN const char* SDL_JoystickNameForIndex(int device_index) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_JoystickNameForIndex) return "Unknown";

    if (assigned_controller >= 0 && device_index == 0 && sdl_assigned_index >= 0) {
        return real_SDL_JoystickNameForIndex(sdl_assigned_index);
    }

    return real_SDL_JoystickNameForIndex(device_index);
}

SDL2_HIDDEN SDL_JoystickGUID SDL_JoystickGetDeviceGUID(int device_index) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_JoystickGetDeviceGUID) {
        SDL_JoystickGUID empty = {0};
        return empty;
    }

    if (assigned_controller >= 0 && device_index == 0 && sdl_assigned_index >= 0) {
        return real_SDL_JoystickGetDeviceGUID(sdl_assigned_index);
    }

    return real_SDL_JoystickGetDeviceGUID(device_index);
}

// Check if an SDL device matches our assigned kernel controller by VID:PID
static int sdl_device_matches_assigned(int sdl_idx) {
    if (assigned_controller < 0 || assigned_controller >= num_kernel_controllers) return 0;
    if (!real_SDL_JoystickGetDeviceVendor || !real_SDL_JoystickGetDeviceProduct) return 0;

    unsigned short sdl_vid = real_SDL_JoystickGetDeviceVendor(sdl_idx);
    unsigned short sdl_pid = real_SDL_JoystickGetDeviceProduct(sdl_idx);
    unsigned short kern_vid = kernel_controllers[assigned_controller].vendor_id;
    unsigned short kern_pid = kernel_controllers[assigned_controller].product_id;

    return (sdl_vid == kern_vid && sdl_pid == kern_pid);
}

SDL2_HIDDEN SDL_Joystick* SDL_JoystickOpen(int device_index) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_JoystickOpen) return NULL;

    // Try to rebuild mapping if we haven't found our controller yet
    if (assigned_controller >= 0 && sdl_assigned_index < 0) {
        build_sdl_mapping();
    }

    if (assigned_controller >= 0) {
        if (sdl_assigned_index >= 0) {
            // We know exactly which SDL index is our controller
            if (device_index == 0) {
                LOG("SDL_JoystickOpen(%d) -> remapped to SDL %d (kernel %d: %s)",
                    device_index, sdl_assigned_index, assigned_controller, assigned_controller_name);
                return real_SDL_JoystickOpen(sdl_assigned_index);
            } else {
                LOG("SDL_JoystickOpen(%d) -> BLOCKED", device_index);
                return NULL;
            }
        } else {
            // Fallback: check by VID:PID if this device matches our assigned controller
            if (sdl_device_matches_assigned(device_index)) {
                LOG("SDL_JoystickOpen(%d) -> ALLOWED (VID:PID match)", device_index);
                return real_SDL_JoystickOpen(device_index);
            } else if (!real_SDL_JoystickGetDeviceVendor || !real_SDL_JoystickGetDeviceProduct) {
                // Can't check VID:PID — trust evdev layer which already filters devices.
                // This happens when SDL2 is loaded via dlopen(RTLD_LOCAL) and we couldn't
                // resolve all helper functions (e.g., Wine's winebus in Proton).
                LOG("SDL_JoystickOpen(%d) -> ALLOWED (trusting evdev filter, no SDL2 VID:PID functions)", device_index);
                return real_SDL_JoystickOpen(device_index);
            } else {
                LOG("SDL_JoystickOpen(%d) -> BLOCKED (no mapping, VID:PID mismatch)", device_index);
                return NULL;
            }
        }
    }

    return real_SDL_JoystickOpen(device_index);
}

SDL2_HIDDEN SDL_GameController* SDL_GameControllerOpen(int joystick_index) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_GameControllerOpen) return NULL;

    // Try to rebuild mapping if we haven't found our controller yet
    if (assigned_controller >= 0 && sdl_assigned_index < 0) {
        build_sdl_mapping();
    }

    if (assigned_controller >= 0) {
        if (sdl_assigned_index >= 0) {
            if (joystick_index == 0) {
                LOG("SDL_GameControllerOpen(%d) -> remapped to SDL %d", joystick_index, sdl_assigned_index);
                return real_SDL_GameControllerOpen(sdl_assigned_index);
            } else {
                LOG("SDL_GameControllerOpen(%d) -> BLOCKED", joystick_index);
                return NULL;
            }
        } else {
            // Fallback: check by VID:PID
            if (sdl_device_matches_assigned(joystick_index)) {
                LOG("SDL_GameControllerOpen(%d) -> ALLOWED (VID:PID match)", joystick_index);
                return real_SDL_GameControllerOpen(joystick_index);
            } else if (!real_SDL_JoystickGetDeviceVendor || !real_SDL_JoystickGetDeviceProduct) {
                // Can't check VID:PID — trust evdev layer (see SDL_JoystickOpen comment)
                LOG("SDL_GameControllerOpen(%d) -> ALLOWED (trusting evdev filter, no SDL2 VID:PID functions)", joystick_index);
                return real_SDL_GameControllerOpen(joystick_index);
            } else {
                LOG("SDL_GameControllerOpen(%d) -> BLOCKED (no mapping, VID:PID mismatch)", joystick_index);
                return NULL;
            }
        }
    }

    return real_SDL_GameControllerOpen(joystick_index);
}

SDL2_HIDDEN int SDL_IsGameController(int joystick_index) {
    ensure_init();
    ensure_sdl2_resolved();
    if (!real_SDL_IsGameController) return 0;

    if (assigned_controller >= 0 && joystick_index == 0 && sdl_assigned_index >= 0) {
        return real_SDL_IsGameController(sdl_assigned_index);
    }

    return real_SDL_IsGameController(joystick_index);
}

// =============================================================================
// SDL3 Hooked Functions
// =============================================================================

// Build mapping from SDL3 instance IDs to kernel controllers
static void build_sdl3_mapping(SDL_JoystickID* joysticks, int count) {
    if (sdl3_mapping_done || assigned_controller < 0) return;
    if (!real_SDL_GetJoystickNameForID) return;

    // Make sure Steam Input detection has run
    detect_steam_input();

    LOG("Building SDL3 instance ID mapping (%d joysticks, steam_input=%d)...", count, steam_input_active);

    // Determine what we're looking for
    const char* target_name = NULL;
    unsigned short target_vid = 0, target_pid = 0;

    if (steam_input_active && assigned_virtual_js >= 0) {
        // Steam Input mode: SDL3 reports virtual controllers by their Steam slot name
        // (e.g., "Xbox Series X Controller"), NOT the kernel evdev name
        // (e.g., "Microsoft X-Box 360 pad 1"). Look up the Steam slot name.
        for (int i = 0; i < num_steam_virtual; i++) {
            if (steam_virtual_controllers[i].valid &&
                steam_virtual_controllers[i].js_index == assigned_virtual_js) {
                target_name = steam_virtual_controllers[i].name;
                target_vid = steam_virtual_controllers[i].vid;
                target_pid = steam_virtual_controllers[i].pid;
                LOG("  Steam Input: looking for virtual js%d with Steam slot name: '%s' [%04x:%04x]",
                    assigned_virtual_js, target_name, target_vid, target_pid);
                break;
            }
        }
    } else {
        // Normal mode: match the physical controller by VID:PID
        target_vid = kernel_controllers[assigned_controller].vendor_id;
        target_pid = kernel_controllers[assigned_controller].product_id;
        target_name = kernel_controllers[assigned_controller].name;
        LOG("  Normal mode: looking for '%s' [%04x:%04x]", target_name, target_vid, target_pid);
    }

    for (int i = 0; i < count; i++) {
        SDL_JoystickID id = joysticks[i];
        const char* name = real_SDL_GetJoystickNameForID(id);
        unsigned short vid = real_SDL_GetJoystickVendorForID ? real_SDL_GetJoystickVendorForID(id) : 0;
        unsigned short pid = real_SDL_GetJoystickProductForID ? real_SDL_GetJoystickProductForID(id) : 0;

        LOG("  SDL3 joystick instance %u: %s [%04x:%04x]", id, name ? name : "Unknown", vid, pid);

        // Try to match by name first (required for Steam Input since all virtuals have same VID:PID)
        if (target_name && name && strcmp(name, target_name) == 0) {
            sdl3_assigned_instance_id = id;
            sdl3_mapping_done = 1;
            LOG("  -> Matched by name to SDL3 instance ID %u", id);
            return;
        }

        // Fallback: match by VID:PID (only works when VID:PID is unique)
        if (!steam_input_active && target_vid != 0 && vid == target_vid && pid == target_pid) {
            sdl3_assigned_instance_id = id;
            sdl3_mapping_done = 1;
            LOG("  -> Matched by VID:PID to SDL3 instance ID %u", id);
            return;
        }
    }

    LOG("  WARNING: No SDL3 match found for assigned controller");
}

// Hook SDL_GetJoysticks - returns filtered array with only assigned controller
SDL2_HIDDEN SDL_JoystickID* SDL_GetJoysticks(int* count) {
    ensure_init();
    VLOG("SDL_GetJoysticks called, real_SDL_GetJoysticks=%p", (void*)real_SDL_GetJoysticks);
    if (!real_SDL_GetJoysticks) {
        if (count) *count = 0;
        return NULL;
    }

    int real_count = 0;
    SDL_JoystickID* real_joysticks = real_SDL_GetJoysticks(&real_count);
    VLOG("SDL_GetJoysticks: real returned %d joysticks", real_count);

    if (!real_joysticks || real_count == 0) {
        if (count) *count = 0;
        return real_joysticks;
    }

    // Build or rebuild mapping
    if (assigned_controller >= 0) {
        if (!sdl3_mapping_done) {
            build_sdl3_mapping(real_joysticks, real_count);
        } else if (sdl3_assigned_instance_id != 0) {
            // Hot-plug: check if our assigned instance ID is still valid
            int still_valid = 0;
            for (int i = 0; i < real_count; i++) {
                if (real_joysticks[i] == sdl3_assigned_instance_id) {
                    still_valid = 1;
                    break;
                }
            }
            if (!still_valid) {
                LOG("Hot-plug: SDL3 instance ID %u gone, re-enumerating", sdl3_assigned_instance_id);
                sdl3_mapping_done = 0;
                sdl3_assigned_instance_id = 0;
                enumerate_kernel_controllers();
                re_match_assigned_controller();
                build_sdl3_mapping(real_joysticks, real_count);
            }
        }
    }

    // If not filtering, return original array
    if (assigned_controller < 0) {
        if (count) *count = real_count;
        return real_joysticks;
    }

    // Filter: return array with only the assigned controller
    if (sdl3_assigned_instance_id != 0) {
        // Verify the assigned ID is still in the list
        int found = 0;
        for (int i = 0; i < real_count; i++) {
            if (real_joysticks[i] == sdl3_assigned_instance_id) {
                found = 1;
                break;
            }
        }

        if (found) {
            // Allocate new array with just our controller
            // SDL3 expects caller to free with SDL_free
            SDL_JoystickID* filtered = malloc(2 * sizeof(SDL_JoystickID));
            if (filtered) {
                filtered[0] = sdl3_assigned_instance_id;
                filtered[1] = 0;  // NULL terminator
                if (count) *count = 1;

                // Free the original array
                if (real_SDL_free) real_SDL_free(real_joysticks);

                VLOG("SDL_GetJoysticks: filtered %d -> 1 (instance %u)", real_count, sdl3_assigned_instance_id);
                return filtered;
            }
        }
    }

    // Fallback: return original (shouldn't happen if mapping worked)
    VLOG("SDL_GetJoysticks: no mapping, returning all %d", real_count);
    if (count) *count = real_count;
    return real_joysticks;
}

// Hook SDL_GetGamepads - same logic as SDL_GetJoysticks
SDL2_HIDDEN SDL_JoystickID* SDL_GetGamepads(int* count) {
    ensure_init();
    VLOG("SDL_GetGamepads called, real_SDL_GetGamepads=%p", (void*)real_SDL_GetGamepads);
    if (!real_SDL_GetGamepads) {
        if (count) *count = 0;
        return NULL;
    }

    int real_count = 0;
    SDL_JoystickID* real_gamepads = real_SDL_GetGamepads(&real_count);
    VLOG("SDL_GetGamepads: real returned %d gamepads", real_count);

    if (!real_gamepads || real_count == 0) {
        if (count) *count = 0;
        return real_gamepads;
    }

    // Build or rebuild mapping
    if (assigned_controller >= 0 && real_SDL_GetJoystickVendorForID) {
        if (!sdl3_mapping_done) {
            build_sdl3_mapping(real_gamepads, real_count);
        } else if (sdl3_assigned_instance_id != 0) {
            // Hot-plug: check if our assigned instance ID is still valid
            int still_valid = 0;
            for (int i = 0; i < real_count; i++) {
                if (real_gamepads[i] == sdl3_assigned_instance_id) {
                    still_valid = 1;
                    break;
                }
            }
            if (!still_valid) {
                LOG("Hot-plug: SDL3 gamepad instance ID %u gone, re-enumerating", sdl3_assigned_instance_id);
                sdl3_mapping_done = 0;
                sdl3_assigned_instance_id = 0;
                enumerate_kernel_controllers();
                re_match_assigned_controller();
                build_sdl3_mapping(real_gamepads, real_count);
            }
        }
    }

    // If not filtering, return original array
    if (assigned_controller < 0) {
        if (count) *count = real_count;
        return real_gamepads;
    }

    // Filter: return array with only the assigned controller
    if (sdl3_assigned_instance_id != 0) {
        int found = 0;
        for (int i = 0; i < real_count; i++) {
            if (real_gamepads[i] == sdl3_assigned_instance_id) {
                found = 1;
                break;
            }
        }

        if (found) {
            SDL_JoystickID* filtered = malloc(2 * sizeof(SDL_JoystickID));
            if (filtered) {
                filtered[0] = sdl3_assigned_instance_id;
                filtered[1] = 0;
                if (count) *count = 1;
                if (real_SDL_free) real_SDL_free(real_gamepads);
                VLOG("SDL_GetGamepads: filtered %d -> 1 (instance %u)", real_count, sdl3_assigned_instance_id);
                return filtered;
            }
        }
    }

    VLOG("SDL_GetGamepads: no mapping, returning all %d", real_count);
    if (count) *count = real_count;
    return real_gamepads;
}

// Hook SDL_OpenJoystick - only allow opening the assigned controller
SDL2_HIDDEN SDL_Joystick* SDL_OpenJoystick(SDL_JoystickID instance_id) {
    ensure_init();
    if (!real_SDL_OpenJoystick) return NULL;

    if (assigned_controller < 0) {
        return real_SDL_OpenJoystick(instance_id);
    }

    // Check if this is our assigned controller (already mapped)
    if (sdl3_assigned_instance_id != 0 && instance_id == sdl3_assigned_instance_id) {
        LOG("SDL_OpenJoystick(%u) -> ALLOWED (assigned controller)", instance_id);
        return real_SDL_OpenJoystick(instance_id);
    }

    // Hot-plug: if mapping is done but the requested ID doesn't match,
    // the old ID may be stale. Reset and try to re-map.
    if (sdl3_mapping_done && sdl3_assigned_instance_id != 0 && instance_id != sdl3_assigned_instance_id) {
        // Check if our mapped ID still exists
        if (real_SDL_GetJoysticks) {
            int count = 0;
            SDL_JoystickID* joysticks = real_SDL_GetJoysticks(&count);
            int old_valid = 0;
            if (joysticks) {
                for (int i = 0; i < count; i++) {
                    if (joysticks[i] == sdl3_assigned_instance_id) { old_valid = 1; break; }
                }
                if (real_SDL_free) real_SDL_free(joysticks);
            }
            if (!old_valid) {
                LOG("Hot-plug: SDL3 instance ID %u stale in OpenJoystick, resetting", sdl3_assigned_instance_id);
                sdl3_mapping_done = 0;
                sdl3_assigned_instance_id = 0;
                enumerate_kernel_controllers();
                re_match_assigned_controller();
            }
        }
    }

    // If mapping not done yet, try to identify the controller
    if (!sdl3_mapping_done && real_SDL_GetJoystickNameForID) {
        detect_steam_input();

        const char* sdl_name = real_SDL_GetJoystickNameForID(instance_id);
        unsigned short vid = real_SDL_GetJoystickVendorForID ? real_SDL_GetJoystickVendorForID(instance_id) : 0;
        unsigned short pid = real_SDL_GetJoystickProductForID ? real_SDL_GetJoystickProductForID(instance_id) : 0;

        if (steam_input_active && assigned_virtual_js >= 0 && real_SDL_GetJoysticks) {
            // Steam Input mode: check how many joysticks SDL3 can see
            int count = 0;
            SDL_JoystickID* joysticks = real_SDL_GetJoysticks(&count);

            // If SDL3 only sees 1 joystick, trust our evdev filter
            if (joysticks && count == 1) {
                SDL_JoystickID only_id = joysticks[0];
                if (real_SDL_free) real_SDL_free(joysticks);
                sdl3_assigned_instance_id = only_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenJoystick(%u) -> ALLOWED (Steam Input: only 1 device visible, trusting evdev filter)",
                    instance_id);
                return real_SDL_OpenJoystick(only_id);
            }
            if (joysticks && real_SDL_free) real_SDL_free(joysticks);

            // Multiple joysticks - try name matching
            const char* target_name = NULL;
            for (int i = 0; i < num_steam_virtual; i++) {
                if (steam_virtual_controllers[i].valid &&
                    steam_virtual_controllers[i].js_index == assigned_virtual_js) {
                    target_name = steam_virtual_controllers[i].name;
                    break;
                }
            }
            if (target_name && sdl_name && strcmp(sdl_name, target_name) == 0) {
                sdl3_assigned_instance_id = instance_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenJoystick(%u) -> ALLOWED (Steam Input name match: '%s')", instance_id, sdl_name);
                return real_SDL_OpenJoystick(instance_id);
            }
        } else if (!steam_input_active) {
            // Normal mode: match by VID:PID
            unsigned short assigned_vid = kernel_controllers[assigned_controller].vendor_id;
            unsigned short assigned_pid = kernel_controllers[assigned_controller].product_id;

            if (vid == assigned_vid && pid == assigned_pid) {
                sdl3_assigned_instance_id = instance_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenJoystick(%u) -> ALLOWED (VID:PID match)", instance_id);
                return real_SDL_OpenJoystick(instance_id);
            }
        }
    }

    LOG("SDL_OpenJoystick(%u) -> BLOCKED", instance_id);
    return NULL;
}

// Hook SDL_OpenGamepad - only allow opening the assigned controller
SDL2_HIDDEN SDL_Gamepad* SDL_OpenGamepad(SDL_JoystickID instance_id) {
    ensure_init();
    if (!real_SDL_OpenGamepad) return NULL;

    if (assigned_controller < 0) {
        LOG("SDL_OpenGamepad(%u) -> pass through (no filter)", instance_id);
        return real_SDL_OpenGamepad(instance_id);
    }

    // Check if this is our assigned controller (already mapped)
    if (sdl3_assigned_instance_id != 0 && instance_id == sdl3_assigned_instance_id) {
        LOG("SDL_OpenGamepad(%u) -> ALLOWED (assigned controller)", instance_id);
        return real_SDL_OpenGamepad(instance_id);
    }

    // Hot-plug: if mapping is done but the requested ID doesn't match,
    // the old ID may be stale. Reset and try to re-map.
    if (sdl3_mapping_done && sdl3_assigned_instance_id != 0 && instance_id != sdl3_assigned_instance_id) {
        if (real_SDL_GetJoysticks) {
            int count = 0;
            SDL_JoystickID* joysticks = real_SDL_GetJoysticks(&count);
            int old_valid = 0;
            if (joysticks) {
                for (int i = 0; i < count; i++) {
                    if (joysticks[i] == sdl3_assigned_instance_id) { old_valid = 1; break; }
                }
                if (real_SDL_free) real_SDL_free(joysticks);
            }
            if (!old_valid) {
                LOG("Hot-plug: SDL3 instance ID %u stale in OpenGamepad, resetting", sdl3_assigned_instance_id);
                sdl3_mapping_done = 0;
                sdl3_assigned_instance_id = 0;
                enumerate_kernel_controllers();
                re_match_assigned_controller();
            }
        }
    }

    // If mapping not done yet, try to identify the controller
    if (!sdl3_mapping_done && real_SDL_GetJoystickNameForID) {
        detect_steam_input();

        const char* sdl_name = real_SDL_GetJoystickNameForID(instance_id);
        unsigned short vid = real_SDL_GetJoystickVendorForID ? real_SDL_GetJoystickVendorForID(instance_id) : 0;
        unsigned short pid = real_SDL_GetJoystickProductForID ? real_SDL_GetJoystickProductForID(instance_id) : 0;

        if (steam_input_active && assigned_virtual_js >= 0) {
            // Steam Input mode: match by name from virtualgamepadinfo.txt
            // SDL3 uses the Steam slot name (e.g., "Xbox Series X Controller"), not the kernel name
            const char* target_name = NULL;
            for (int i = 0; i < num_steam_virtual; i++) {
                if (steam_virtual_controllers[i].valid &&
                    steam_virtual_controllers[i].js_index == assigned_virtual_js) {
                    target_name = steam_virtual_controllers[i].name;
                    break;
                }
            }
            LOG("SDL_OpenGamepad(%u): comparing SDL name '%s' vs Steam slot name '%s'",
                instance_id, sdl_name ? sdl_name : "(null)", target_name ? target_name : "(null)");
            if (target_name && sdl_name && strcmp(sdl_name, target_name) == 0) {
                sdl3_assigned_instance_id = instance_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenGamepad(%u) -> ALLOWED (Steam Input name match: '%s')", instance_id, sdl_name);
                return real_SDL_OpenGamepad(instance_id);
            }
        } else {
            // Normal mode: match by VID:PID
            unsigned short assigned_vid = kernel_controllers[assigned_controller].vendor_id;
            unsigned short assigned_pid = kernel_controllers[assigned_controller].product_id;

            if (vid == assigned_vid && pid == assigned_pid) {
                sdl3_assigned_instance_id = instance_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenGamepad(%u) -> ALLOWED (VID:PID match)", instance_id);
                return real_SDL_OpenGamepad(instance_id);
            }
        }
    }

    // In Steam Input mode, try to find and open the correct controller
    LOG("SDL_OpenGamepad: steam_input_active=%d, real_SDL_GetJoysticks=%p, real_SDL_GetJoystickNameForID=%p",
        steam_input_active, (void*)real_SDL_GetJoysticks, (void*)real_SDL_GetJoystickNameForID);
    if (steam_input_active && real_SDL_GetJoysticks && real_SDL_GetJoystickNameForID) {
        // Find our assigned controller's Steam slot name
        const char* target_name = NULL;
        for (int i = 0; i < num_steam_virtual; i++) {
            if (steam_virtual_controllers[i].valid &&
                steam_virtual_controllers[i].js_index == assigned_virtual_js) {
                target_name = steam_virtual_controllers[i].name;
                break;
            }
        }

        LOG("SDL_OpenGamepad: assigned_virtual_js=%d", assigned_virtual_js);

        // Get all joysticks SDL3 can see
        int count = 0;
        SDL_JoystickID* joysticks = real_SDL_GetJoysticks(&count);
        LOG("SDL_OpenGamepad: real_SDL_GetJoysticks returned %d joysticks", count);

        if (joysticks && count > 0) {
            // Log all visible joysticks
            for (int i = 0; i < count; i++) {
                const char* name = real_SDL_GetJoystickNameForID(joysticks[i]);
                LOG("SDL_OpenGamepad: joystick[%d] = instance %u, name='%s'",
                    i, joysticks[i], name ? name : "(null)");
            }

            // If SDL3 only sees 1 joystick, it must be the one we allowed at evdev level
            // Trust our evdev filtering and allow it regardless of name mismatch
            if (count == 1) {
                SDL_JoystickID only_id = joysticks[0];
                const char* name = real_SDL_GetJoystickNameForID(only_id);
                if (real_SDL_free) real_SDL_free(joysticks);
                sdl3_assigned_instance_id = only_id;
                sdl3_mapping_done = 1;
                LOG("SDL_OpenGamepad(%u) -> ALLOWED (Steam Input: only 1 device visible, trusting evdev filter, name='%s')",
                    instance_id, name ? name : "(null)");
                return real_SDL_OpenGamepad(only_id);
            }

            // Multiple joysticks visible - try name matching
            if (target_name) {
                LOG("SDL_OpenGamepad: searching for target_name='%s'", target_name);
                for (int i = 0; i < count; i++) {
                    const char* name = real_SDL_GetJoystickNameForID(joysticks[i]);
                    if (name && strcmp(name, target_name) == 0) {
                        SDL_JoystickID correct_id = joysticks[i];
                        if (real_SDL_free) real_SDL_free(joysticks);
                        sdl3_assigned_instance_id = correct_id;
                        sdl3_mapping_done = 1;
                        LOG("SDL_OpenGamepad(%u) -> redirecting to instance %u ('%s')",
                            instance_id, correct_id, target_name);
                        return real_SDL_OpenGamepad(correct_id);
                    }
                }
            }
            if (real_SDL_free) real_SDL_free(joysticks);
        }
    }

    LOG("SDL_OpenGamepad(%u) -> BLOCKED", instance_id);
    return NULL;
}

// =============================================================================
// evdev Hooked Functions (for Proton/Wine and raw device access)
// =============================================================================

// Re-enumerate kernel controllers if assigned device is gone (hot-plug support for evdev games)
static time_t last_evdev_refresh_time = 0;

static void refresh_kernel_state_if_needed(void) {
    if (assigned_controller < 0) return;

    // Fast path: check if assigned controller's js device still exists in sysfs
    if (assigned_controller < num_kernel_controllers && kernel_controllers[assigned_controller].valid) {
        char sysfs_path[64];
        snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/input/js%d",
                 kernel_controllers[assigned_controller].js_index);
        struct stat st;
        if (stat(sysfs_path, &st) == 0) {
            return;  // Device still exists, state is fresh
        }
    }

    // Throttle re-enumeration to once per second (avoid hammering during sustained disconnect)
    time_t now = time(NULL);
    if (now - last_evdev_refresh_time < 1) return;
    last_evdev_refresh_time = now;

    pthread_mutex_lock(&g_enum_lock);
    LOG("Hot-plug: assigned controller device gone, re-enumerating...");
    enumerate_kernel_controllers();
    re_match_assigned_controller();
    pthread_mutex_unlock(&g_enum_lock);

    // Reset SDL mappings since kernel state changed
    sdl_mapping_done = 0;
    sdl3_mapping_done = 0;
    sdl3_assigned_instance_id = 0;
}

// Result of checking an input device path
typedef enum {
    INPUT_PASS_THROUGH,  // Not an input device or not a joystick - pass through
    INPUT_ALLOW,         // Allow this device
    INPUT_BLOCK,         // Block this device
    INPUT_REDIRECT       // Redirect to a different device (check redirect_path)
} InputDeviceAction;

// Parse /dev/input/jsN path and return N, or -1 if not a js device
static int parse_js_device_index(const char* path) {
    if (!path) return -1;

    // Match /dev/input/jsN
    if (strncmp(path, "/dev/input/js", 13) == 0) {
        const char* num = path + 13;
        if (*num >= '0' && *num <= '9') {
            char* end;
            long idx = strtol(num, &end, 10);
            if (*end == '\0' && idx >= 0 && idx < 64) {  // generous; covers virtual pads + churn
                return (int)idx;
            }
        }
    }
    return -1;
}

// Parse /dev/input/eventN path and return N, or -1 if not an event device
static int parse_event_device_index(const char* path) {
    if (!path) return -1;

    // Match /dev/input/eventN
    if (strncmp(path, "/dev/input/event", 16) == 0) {
        const char* num = path + 16;
        if (*num >= '0' && *num <= '9') {
            char* end;
            long idx = strtol(num, &end, 10);
            // No upper bound: the kernel's evdev minor counter climbs without
            // resetting (controller hot-plug churn pushed Steam's virtual pads to
            // event256+), and the old `idx < 256` cap silently rejected those —
            // so they slipped past filtering and leaked to every instance. idx is
            // only ever compared, never used as an array index, so any value is safe.
            if (*end == '\0' && idx >= 0) {
                return (int)idx;
            }
        }
    }
    return -1;
}

// Check if an event index belongs to a joystick, and if so which kernel controller
// Returns: kernel controller index if it's a joystick, -1 if not a joystick
static int event_to_kernel_controller(int event_idx) {
    for (int i = 0; i < num_kernel_controllers; i++) {
        if (kernel_controllers[i].valid && kernel_controllers[i].event_index == event_idx) {
            return i;
        }
    }
    return -1;
}

// Find the event device index for our assigned virtual controller (Steam Input mode)
static int get_assigned_virtual_event(void) {
    if (!steam_input_active || assigned_virtual_js < 0) return -1;

    // Find the kernel controller entry for the virtual js device
    for (int i = 0; i < num_kernel_controllers; i++) {
        if (kernel_controllers[i].valid &&
            kernel_controllers[i].js_index == assigned_virtual_js) {
            return kernel_controllers[i].event_index;
        }
    }
    return -1;
}

// Check if an event device should be allowed in Steam Input mode
// Returns: 1 if allowed, 0 if blocked, -1 if not applicable (not a joystick or no Steam Input)
static int check_event_steam_input_mode(int event_idx) {
    int controller_idx = event_to_kernel_controller(event_idx);
    if (controller_idx < 0) return -1;  // Not a joystick

    // Note: Steam Input grabs hidraw, NOT evdev. So even when Steam Input is configured,
    // the physical evdev device is still usable. Only block physical device if Steam Input
    // actually created a virtual evdev device (steam_input_active=1).
    if (!steam_input_active) {
        VLOG("check_event_steam_input_mode(%d): steam_input_active=0, returning -1", event_idx);
        return -1;  // Not in Steam Input mode - use direct matching
    }

    // In Steam Input mode, we want to:
    // - ALLOW the virtual controller's event device
    // - BLOCK the physical controller (Steam is handling it)
    // - BLOCK other controllers

    int virtual_event = get_assigned_virtual_event();
    VLOG("check_event_steam_input_mode(%d): virtual_event=%d, assigned_virtual_js=%d, controller_idx=%d",
         event_idx, virtual_event, assigned_virtual_js, controller_idx);
    if (virtual_event >= 0 && event_idx == virtual_event) {
        return 1;  // This is our assigned virtual controller - ALLOW
    }

    // Pass-through case: assigned physical isn't (or couldn't be) mapped to
    // a virtual slot. Allow the assigned physical's event device so the game
    // can still read input, and keep blocking everything else. Mirrors the
    // pass-through branch in should_block_js_device().
    if (assigned_virtual_js < 0 &&
        assigned_controller >= 0 &&
        controller_idx == assigned_controller) {
        return 1;
    }

    // Block everything else (other physical controllers, other virtual controllers)
    return 0;
}

// Check if a hidraw device belongs to the assigned controller
// Returns: INPUT_ALLOW if it's the assigned controller, INPUT_BLOCK if not, INPUT_PASS_THROUGH if not a controller
// Janus input mode (JANUS_ALLOW_DEVICES="/dev/input/eventA:/dev/input/jsA"): the
// session's input daemon (src/host/padd.py) made this instance its own virtual
// Xbox pad. A private /dev (src/host/input_ns.py) normally hides every other
// device already; this is the fallback where user namespaces are unavailable:
// the listed nodes open, every other pad and every hidraw node is refused,
// keyboards/mice pass through. No assigned controller (JANUS_CONTROLLER unset),
// so none of the Steam Input logic runs.
#define MAX_ALLOWED_DEVICES 8
static int allow_mode_state = -1;      // -1 not parsed yet, 0 off, 1 on
static char allowed_devices[MAX_ALLOWED_DEVICES][64];
static int allowed_device_count = 0;

static int allow_mode(void) {
    if (allow_mode_state >= 0) return allow_mode_state;
    const char* env = getenv("JANUS_ALLOW_DEVICES");
    int n = 0;
    if (env && *env) {
        const char* p = env;
        while (*p && n < MAX_ALLOWED_DEVICES) {
            const char* end = strchr(p, ':');
            size_t len = end ? (size_t)(end - p) : strlen(p);
            if (len > 0 && len < sizeof(allowed_devices[0])) {
                memcpy(allowed_devices[n], p, len);
                allowed_devices[n][len] = '\0';
                n++;
            }
            if (!end) break;
            p = end + 1;
        }
    }
    allowed_device_count = n;
    allow_mode_state = n > 0;
    if (n > 0) LOG("Janus input: allowlist of %d device(s), first %s", n, allowed_devices[0]);
    return allow_mode_state;
}

static int device_allowed(const char* path) {
    for (int i = 0; i < allowed_device_count; i++)
        if (strcmp(path, allowed_devices[i]) == 0) return 1;
    return 0;
}

static InputDeviceAction check_allowlisted_input(const char* pathname) {
    int js = parse_js_device_index(pathname);
    int ev = parse_event_device_index(pathname);
    if (js < 0 && ev < 0) return INPUT_PASS_THROUGH;
    if (device_allowed(pathname)) {
        VLOG("evdev: %s -> ALLOWED (Janus input allowlist)", pathname);
        return INPUT_ALLOW;
    }
    if (js >= 0 || event_has_gamepad_buttons(ev) || event_has_js_sibling(ev)) {
        LOG("evdev: %s -> BLOCKED (not in the Janus input allowlist)", pathname);
        return INPUT_BLOCK;
    }
    return INPUT_PASS_THROUGH;
}

static InputDeviceAction check_hidraw_device(const char* pathname) {
    if (!pathname || strncmp(pathname, "/dev/hidraw", 11) != 0) {
        return INPUT_PASS_THROUGH;
    }
    if (allow_mode()) {
        LOG("hidraw: %s -> BLOCKED (Janus input mode)", pathname);
        return INPUT_BLOCK;
    }

    ensure_init();
    refresh_kernel_state_if_needed();

    if (assigned_controller < 0) {
        return INPUT_PASS_THROUGH;  // No filtering
    }

    // Extract hidraw number
    int hidraw_num = atoi(pathname + 11);

    // Read VID:PID from sysfs
    char sysfs_path[128];
    snprintf(sysfs_path, sizeof(sysfs_path), "/sys/class/hidraw/hidraw%d/device/uevent", hidraw_num);

    int fd = real_open(sysfs_path, O_RDONLY);
    if (fd < 0) {
        return INPUT_PASS_THROUGH;  // Can't read sysfs, allow by default
    }

    char buf[512];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    real_close(fd);

    if (n <= 0) {
        return INPUT_PASS_THROUGH;
    }
    buf[n] = '\0';

    // Parse HID_ID line to get VID:PID
    // Format: HID_ID=0003:0000054C:000009CC
    unsigned int vid = 0, pid = 0;
    char* hid_id = strstr(buf, "HID_ID=");
    if (hid_id) {
        unsigned int bus;
        if (sscanf(hid_id, "HID_ID=%x:%x:%x", &bus, &vid, &pid) != 3) {
            vid = pid = 0;
        }
    }

    if (vid == 0 || pid == 0) {
        return INPUT_PASS_THROUGH;  // Not a HID device or can't parse
    }

    // Check if this VID:PID matches our assigned controller
    if (assigned_controller < num_kernel_controllers) {
        KernelController* ctrl = &kernel_controllers[assigned_controller];
        if (ctrl->valid && ctrl->vendor_id == vid && ctrl->product_id == pid) {
            // Steam Input mode, native: the game reads this pad through its
            // Steam virtual pad (see the SDL hint in the constructor), so SDL's
            // HIDAPI must not read the physical one as well (double input, and
            // Steam's remapping bypassed). Steam hides it from its own games the
            // same way (SDL_GAMECONTROLLER_IGNORE_DEVICES).
            if (!steam_input_checked) detect_steam_input();
            if (steam_input_active && assigned_virtual_js >= 0 && sdl_reads_steam_virtual_pad()) {
                LOG("hidraw: %s -> BLOCKED (assigned controller %04x:%04x is read via its Steam virtual pad js%d)",
                    pathname, vid, pid, assigned_virtual_js);
                return INPUT_BLOCK;
            }
            LOG("hidraw: %s -> ALLOWED (assigned controller VID:PID %04x:%04x)", pathname, vid, pid);
            return INPUT_ALLOW;
        }
    }

    // Check if this VID:PID matches any known controller
    for (int i = 0; i < num_kernel_controllers; i++) {
        KernelController* ctrl = &kernel_controllers[i];
        if (ctrl->valid && ctrl->vendor_id == vid && ctrl->product_id == pid) {
            // It's a controller, but not the assigned one - block it
            LOG("hidraw: %s -> BLOCKED (controller %d VID:PID %04x:%04x, want controller %d)",
                pathname, i, vid, pid, assigned_controller);
            return INPUT_BLOCK;
        }
    }

    // Not a known controller hidraw device
    VLOG("hidraw: %s -> pass through (VID:PID %04x:%04x not a known controller)", pathname, vid, pid);
    return INPUT_PASS_THROUGH;
}

// Is /dev/input/eventN (or jsN when is_js) a joystick the hook hasn't enumerated?
static int is_unknown_joystick_node(int idx, int is_js) {
    for (int i = 0; i < num_kernel_controllers; i++)
        if (kernel_controllers[i].valid &&
            (is_js ? kernel_controllers[i].js_index : kernel_controllers[i].event_index) == idx)
            return 0;
    if (is_js) {
        // Only nodes that exist: games probe js0..jsN blindly at startup.
        char sys_path[64];
        struct stat st;
        snprintf(sys_path, sizeof(sys_path), "/sys/class/input/js%d", idx);
        return stat(sys_path, &st) == 0;
    }
    // An event node is a joystick if its input device has gamepad/joystick
    // buttons. Checked first because Steam's freshly created virtual pads get
    // opened before their js sibling shows up in sysfs.
    if (event_has_gamepad_buttons(idx)) return 1;
    return event_has_js_sibling(idx);
}

// A joystick appeared after init — typically Steam's virtual pads, which Steam
// creates only once the game is running. Under GUI auto-join the host instance
// starts ALONE, so its hook enumerated before they existed; event_to_kernel_
// controller() then returned -1 for them and they were passed through as "not a
// joystick" -> every pad drove the host (2026-10-07). Re-enumerate, keep the
// same physical pad, and redo Steam Input detection against the new set.
static void rescan_for_new_joystick(const char* why) {
    pthread_mutex_lock(&g_enum_lock);
    LOG("Hot-plug: new joystick node %s, re-enumerating controllers", why);
    // Steam creates the event node a moment before the js node; give the js
    // handler (which enumeration keys on) a brief chance to appear.
    for (int tries = 0; tries < 20; tries++) {
        char d[64];
        int ev = parse_event_device_index(why);
        if (ev < 0) break;
        snprintf(d, sizeof(d), "/sys/class/input/event%d/device", ev);
        DIR* dir = opendir(d);
        int has_js = 0;
        if (dir) {
            struct dirent* e;
            while ((e = readdir(dir)) != NULL)
                if (strncmp(e->d_name, "js", 2) == 0) { has_js = 1; break; }
            closedir(dir);
        }
        if (has_js || !dir) break;
        usleep(25000);
    }
    enumerate_kernel_controllers();
    re_match_assigned_controller();
    steam_input_checked = 0;
    steam_input_active = 0;
    assigned_virtual_js = -1;
    detect_steam_input();
    sdl_mapping_done = 0;
    sdl3_mapping_done = 0;
    sdl3_assigned_instance_id = 0;
    pthread_mutex_unlock(&g_enum_lock);
}

// =============================================================================
// Live correction of the Steam virtual pad (virtualgamepadinfo.txt watcher)
// =============================================================================
// The virtual pad is chosen when the game opens it, from virtualgamepadinfo.txt
// as it is at that moment. Steam can rewrite that file with a different slot
// order a few seconds later: launched from Big Picture, its first write after
// creating the virtual pads used connection order (slot 0 = MACHENIKE) and ~10 s
// later it rewrote it in Big Picture's player order (slot 0 = Xbox), which is
// what "pad 0"/"pad 1" really carry (2026-10-11). Every instance had already
// opened the other pad's virtual: one pad drove both instances.
//
// So once a process holds a Steam virtual pad, a thread watches the file. When
// it changes and resolves this instance to a different virtual pad, every fd
// open on the old pad's js/event node is pointed at the new one with dup3():
// same fd number, so the game (Wine's SDL backend) keeps its device and simply
// reads the right pad. Steam's virtual pads are identical X-Box 360 devices, so
// nothing else about the device changes. An fd registered in an epoll is
// re-registered, since epoll tracks the old file, not the fd number.

static pid_t vgi_watcher_pid = 0;   // process that owns the watcher thread

// Re-register fd in every epoll instance of this process that watches it.
// Call with phase 0 before replacing fd (removes it, remembering the
// registrations) and phase 1 after (adds them back).
#define MAX_EPOLL_REGS 8
typedef struct { int epfd; unsigned int events; unsigned long long data; } EpollReg;

static int epoll_regs_of(int fd, EpollReg* regs) {
    int n = 0;
    DIR* d = opendir("/proc/self/fd");
    if (!d) return 0;
    struct dirent* e;
    while ((e = readdir(d)) != NULL && n < MAX_EPOLL_REGS) {
        if (!isdigit((unsigned char)e->d_name[0])) continue;
        char link[64], target[64];
        int epfd = atoi(e->d_name);
        snprintf(link, sizeof(link), "/proc/self/fd/%d", epfd);
        ssize_t len = readlink(link, target, sizeof(target) - 1);
        if (len <= 0) continue;
        target[len] = '\0';
        if (strcmp(target, "anon_inode:[eventpoll]") != 0) continue;
        snprintf(link, sizeof(link), "/proc/self/fdinfo/%d", epfd);
        FILE* f = fopen(link, "r");
        if (!f) continue;
        char line[256];
        while (fgets(line, sizeof(line), f) && n < MAX_EPOLL_REGS) {
            int tfd;
            unsigned int ev;
            unsigned long long data;
            if (sscanf(line, "tfd: %d events: %x data: %llx", &tfd, &ev, &data) == 3 &&
                tfd == fd) {
                regs[n].epfd = epfd;
                regs[n].events = ev;
                regs[n].data = data;
                n++;
            }
        }
        fclose(f);
    }
    closedir(d);
    return n;
}

// A thread already blocked in poll()/select() on fd keeps waiting on the OLD
// file after dup3() — the new pad's events never wake it. Wine's winebus reads
// pads that way (one thread in poll(fds, 3, -1)), so after a slot rewrite the
// game got no input at all (Gunfire Reborn, 2026-10-18). Interrupt every
// thread blocked on fd with a private signal (no-op handler): poll never
// restarts, returns EINTR, and the caller polls again — on the new file.
#define WAKE_SIGNAL (SIGRTMAX - 3)

static void wake_noop(int sig) { (void)sig; }

static int wake_signal_ready(void) {
    static int ready = -1;
    if (ready >= 0) return ready;
    struct sigaction old;
    if (sigaction(WAKE_SIGNAL, NULL, &old) == 0 && old.sa_handler == SIG_DFL) {
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = wake_noop;      // no SA_RESTART
        sigemptyset(&sa.sa_mask);
        ready = sigaction(WAKE_SIGNAL, &sa, NULL) == 0;
    } else {
        ready = old.sa_handler == wake_noop;
    }
    if (!ready) LOG("Steam Input: signal %d is taken, can't wake pollers", WAKE_SIGNAL);
    return ready;
}

#if defined(__x86_64__)
#define NR_POLL 7
#define NR_PPOLL 271
#define NR_SELECT 23
#define NR_PSELECT 270
#elif defined(__i386__)
#define NR_POLL 168
#define NR_PPOLL 309
#define NR_SELECT 142
#define NR_PSELECT 308
#endif

// Is thread tid blocked in poll/ppoll/select/pselect with fd in its set?
static int blocked_on_fd(pid_t tid, int fd) {
#ifdef NR_POLL
    char path[64], line[256];
    snprintf(path, sizeof(path), "/proc/self/task/%d/syscall", (int)tid);
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    char* ok = fgets(line, sizeof(line), f);
    fclose(f);
    if (!ok) return 0;
    long nr;
    unsigned long a0, a1, a2;
    if (sscanf(line, "%ld %lx %lx %lx", &nr, &a0, &a1, &a2) != 4) return 0;
    if (nr == NR_POLL || nr == NR_PPOLL) {
        struct pollfd* p = (struct pollfd*)(uintptr_t)a0;
        if (!p || a1 > 4096) return 0;
        for (unsigned long i = 0; i < a1; i++) if (p[i].fd == fd) return 1;
    } else if (nr == NR_SELECT || nr == NR_PSELECT) {
        fd_set* r = (fd_set*)(uintptr_t)a1;
        if (r && fd < (int)a0 && fd < FD_SETSIZE && FD_ISSET(fd, r)) return 1;
    }
#endif
    return 0;
}

static int signal_blocked_in(pid_t tid, int sig) {
    char path[64], line[256];
    snprintf(path, sizeof(path), "/proc/self/task/%d/status", (int)tid);
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    unsigned long long blk = 0;
    while (fgets(line, sizeof(line), f))
        if (sscanf(line, "SigBlk: %llx", &blk) == 1) break;
    fclose(f);
    return (blk >> (sig - 1)) & 1;
}

static void wake_pollers(int fd) {
    if (!wake_signal_ready()) return;
    pid_t me = (pid_t)syscall(SYS_gettid);
    DIR* d = opendir("/proc/self/task");
    if (!d) return;
    struct dirent* e;
    while ((e = readdir(d)) != NULL) {
        if (!isdigit((unsigned char)e->d_name[0])) continue;
        pid_t tid = (pid_t)atoi(e->d_name);
        if (tid == me || !blocked_on_fd(tid, fd)) continue;
        if (signal_blocked_in(tid, WAKE_SIGNAL)) {
            LOG("Steam Input: thread %d polls fd %d but blocks signal %d — can't wake it",
                (int)tid, fd, WAKE_SIGNAL);
            continue;
        }
        if (syscall(SYS_tgkill, getpid(), tid, WAKE_SIGNAL) == 0)
            LOG("Steam Input: woke thread %d polling fd %d", (int)tid, fd);
    }
    closedir(d);
}

// Point every fd open on Steam virtual pad old_js/old_ev at new_js/new_ev.
static int swap_virtual_pad_fds(int old_js, int old_ev, int new_js, int new_ev) {
    int swapped = 0;
    DIR* d = opendir("/proc/self/fd");
    if (!d) return 0;
    int self_fd = dirfd(d);
    struct dirent* e;
    while ((e = readdir(d)) != NULL) {
        if (!isdigit((unsigned char)e->d_name[0])) continue;
        int fd = atoi(e->d_name);
        if (fd == self_fd) continue;
        char link[64], target[64];
        snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
        ssize_t len = readlink(link, target, sizeof(target) - 1);
        if (len <= 0) continue;
        target[len] = '\0';
        char newpath[64];
        int js = parse_js_device_index(target);
        int ev = parse_event_device_index(target);
        if (js >= 0 && js == old_js && new_js >= 0)
            snprintf(newpath, sizeof(newpath), "/dev/input/js%d", new_js);
        else if (ev >= 0 && ev == old_ev && new_ev >= 0)
            snprintf(newpath, sizeof(newpath), "/dev/input/event%d", new_ev);
        else
            continue;

        int fl = fcntl(fd, F_GETFL);
        int fdfl = fcntl(fd, F_GETFD);
        if (fl < 0 || fdfl < 0) continue;
        int nfd = real_open(newpath, (fl & (O_ACCMODE | O_NONBLOCK)) | O_CLOEXEC);
        if (nfd < 0) {
            LOG("Steam Input: can't open %s to replace fd %d (%s): %s",
                newpath, fd, target, strerror(errno));
            continue;
        }
        EpollReg regs[MAX_EPOLL_REGS];
        int nregs = epoll_regs_of(fd, regs);
        for (int i = 0; i < nregs; i++)
            epoll_ctl(regs[i].epfd, EPOLL_CTL_DEL, fd, NULL);
        if (dup3(nfd, fd, (fdfl & FD_CLOEXEC) ? O_CLOEXEC : 0) < 0) {
            LOG("Steam Input: dup3 onto fd %d failed: %s", fd, strerror(errno));
        } else {
            swapped++;
            LOG("Steam Input: fd %d %s -> %s (%d epoll registration%s moved)",
                fd, target, newpath, nregs, nregs == 1 ? "" : "s");
            wake_pollers(fd);
        }
        for (int i = 0; i < nregs; i++) {
            struct epoll_event pe = { .events = regs[i].events };
            pe.data.u64 = regs[i].data;
            if (epoll_ctl(regs[i].epfd, EPOLL_CTL_ADD, fd, &pe) < 0)
                LOG("Steam Input: re-adding fd %d to epoll %d failed: %s",
                    fd, regs[i].epfd, strerror(errno));
        }
        real_close(nfd);
    }
    closedir(d);
    return swapped;
}

// virtualgamepadinfo.txt (or the pad-matcher file) changed: resolve the assignment again and, if this
// instance now belongs on another virtual pad, move its open fds there. A file
// caught mid-write, or one naming pads that don't exist (game exit), resolves
// to nothing usable; the current assignment is then kept.
static void reresolve_virtual_pad(void) {
    pthread_mutex_lock(&g_enum_lock);
    int old_js = assigned_virtual_js;
    int old_ev = get_assigned_virtual_event();
    SteamVirtualController saved[MAX_CONTROLLERS];
    memcpy(saved, steam_virtual_controllers, sizeof(saved));
    int saved_num = num_steam_virtual;
    int saved_active = steam_input_active;
    time_t saved_mtime = vgi_mtime;

    enumerate_kernel_controllers();
    re_match_assigned_controller();
    steam_input_checked = 0;
    steam_input_active = 0;
    assigned_virtual_js = -1;
    detect_steam_input();

    int complete = steam_input_active && num_steam_virtual > 0;
    for (int i = 0; complete && i < num_steam_virtual; i++)
        if (steam_virtual_controllers[i].js_index < 0) complete = 0;
    if (!complete || assigned_virtual_js < 0) {
        LOG("Steam Input: virtualgamepadinfo.txt changed but doesn't resolve to a "
            "virtual pad (%d slots) — keeping js%d", num_steam_virtual, old_js);
        memcpy(steam_virtual_controllers, saved, sizeof(saved));
        num_steam_virtual = saved_num;
        steam_input_active = saved_active;
        steam_input_checked = 1;
        assigned_virtual_js = old_js;
        vgi_mtime = saved_mtime;
    } else if (assigned_virtual_js != old_js) {
        int new_ev = get_assigned_virtual_event();
        LOG("Steam Input: virtualgamepadinfo.txt changed: assigned virtual pad "
            "js%d/event%d -> js%d/event%d", old_js, old_ev, assigned_virtual_js, new_ev);
        int n = swap_virtual_pad_fds(old_js, old_ev, assigned_virtual_js, new_ev);
        LOG("Steam Input: moved %d open fd%s to the new virtual pad", n, n == 1 ? "" : "s");
    } else {
        VLOG("Steam Input: virtualgamepadinfo.txt changed, assignment unchanged (js%d)",
             old_js);
    }
    pthread_mutex_unlock(&g_enum_lock);
}

static void* vgi_watcher_main(void* arg) {
    (void)arg;
    char path[512];
    if (!vgi_path(path, sizeof(path))) return NULL;
    const char* base = strrchr(path, '/');
    if (!base) return NULL;
    base++;
    char dir[512];
    snprintf(dir, sizeof(dir), "%.*s", (int)(base - 1 - path), path);

    int ifd = inotify_init1(IN_CLOEXEC);
    if (ifd < 0) {
        LOG("Steam Input: inotify_init1 failed: %s", strerror(errno));
        return NULL;
    }
    // Watch the directory: also catches the file being replaced by a rename.
    if (inotify_add_watch(ifd, dir, IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_MODIFY) < 0) {
        LOG("Steam Input: can't watch %s: %s", dir, strerror(errno));
        real_close(ifd);
        return NULL;
    }
    LOG("Steam Input: watching %s for slot changes", path);

    // And the session's pad-matcher answer for this instance (JANUS_PAD_MAP).
    const char* map = getenv("JANUS_PAD_MAP");
    const char* map_base = map ? strrchr(map, '/') : NULL;
    if (map_base) {
        char map_dir[512];
        snprintf(map_dir, sizeof(map_dir), "%.*s", (int)(map_base - map), map);
        map_base++;
        if (inotify_add_watch(ifd, map_dir, IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE) < 0)
            LOG("Steam Input: can't watch %s: %s", map_dir, strerror(errno));
    }

    char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
    for (;;) {
        ssize_t len = read(ifd, buf, sizeof(buf));
        if (len <= 0) {
            if (len < 0 && errno == EINTR) continue;
            break;
        }
        int ours = 0;
        for (char* p = buf; p < buf + len; ) {
            struct inotify_event* ev = (struct inotify_event*)p;
            if (ev->len && (strcmp(ev->name, base) == 0 ||
                            (map_base && strcmp(ev->name, map_base) == 0))) ours = 1;
            p += sizeof(*ev) + ev->len;
        }
        if (!ours) continue;
        // Debounce: act once the file has been quiet for 500 ms, so a
        // truncate-then-write is read whole.
        struct pollfd pfd = { .fd = ifd, .events = POLLIN };
        while (poll(&pfd, 1, 500) > 0) {
            if (read(ifd, buf, sizeof(buf)) <= 0) break;
        }
        reresolve_virtual_pad();
    }
    real_close(ifd);
    return NULL;
}

// Start the watcher in this process (once; again in a forked child, which
// doesn't inherit threads). Only processes that open a Steam virtual pad call
// this, so gamescope, the container tools etc. never get a thread.
static void start_vgi_watcher(void) {
    pid_t me = getpid();
    pid_t seen = __atomic_load_n(&vgi_watcher_pid, __ATOMIC_ACQUIRE);
    if (seen == me ||
        !__atomic_compare_exchange_n(&vgi_watcher_pid, &seen, me, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return;
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&t, &attr, vgi_watcher_main, NULL) != 0)
        LOG("Steam Input: can't start virtualgamepadinfo.txt watcher");
    pthread_attr_destroy(&attr);
}

// Unified handler for input device paths
// Returns the action to take and fills redirect_path if action is INPUT_REDIRECT
static InputDeviceAction check_input_device(const char* pathname, char* redirect_path, size_t redirect_size) {
    if (!pathname || strncmp(pathname, "/dev/input/", 11) != 0) {
        return INPUT_PASS_THROUGH;
    }
    if (allow_mode()) return check_allowlisted_input(pathname);

    // Initialize if not done yet (we need the controller mapping)
    ensure_init();

    // Re-enumerate if assigned controller device is gone (hot-plug support)
    refresh_kernel_state_if_needed();

    // Check if this is a joystick device (js*)
    // No redirection — just block/allow like event devices to preserve sysfs metadata correlation
    int js_idx = parse_js_device_index(pathname);
    if (js_idx >= 0 && assigned_controller >= 0) {
        if (is_unknown_joystick_node(js_idx, 1)) rescan_for_new_joystick(pathname);
        detect_steam_input();

        if (should_block_js_device(js_idx)) {
            LOG("evdev: %s -> BLOCKED", pathname);
            return INPUT_BLOCK;
        }

        VLOG("evdev: %s -> ALLOWED", pathname);
        if (steam_input_active && js_idx == assigned_virtual_js) start_vgi_watcher();
        return INPUT_ALLOW;
    }

    // Check if this is an event device (event*)
    int event_idx = parse_event_device_index(pathname);
    if (event_idx >= 0 && assigned_controller >= 0) {
        if (is_unknown_joystick_node(event_idx, 0)) rescan_for_new_joystick(pathname);
        detect_steam_input();

        // Check Steam Input mode
        int steam_result = check_event_steam_input_mode(event_idx);
        if (steam_result == 1) {
            LOG("evdev: %s -> ALLOWED (Steam Input virtual controller)", pathname);
            start_vgi_watcher();
            return INPUT_ALLOW;
        } else if (steam_result == 0) {
            int controller_idx = event_to_kernel_controller(event_idx);
            LOG("evdev: %s -> BLOCKED (Steam Input mode, controller %d)", pathname, controller_idx);
            return INPUT_BLOCK;
        }

        // Not in Steam Input mode - use direct controller matching
        int controller_idx = event_to_kernel_controller(event_idx);
        if (controller_idx >= 0) {
            // Filter mode: game reads controllers via Steam Input (vtable-filtered), so
            // block EVERY physical controller event node — else it's also read via
            // SDL/evdev (the "duplicate input"). Only controller event nodes reach here.
            if (steam_filter_on()) {
                LOG("evdev: %s -> BLOCKED (filter mode: input via Steam Input, not evdev)", pathname);
                return INPUT_BLOCK;
            }
            if (controller_idx == assigned_controller) {
                LOG("evdev: %s -> ALLOWED (assigned controller)", pathname);
                return INPUT_ALLOW;
            } else {
                LOG("evdev: %s -> BLOCKED (controller %d, want %d)",
                    pathname, controller_idx, assigned_controller);
                return INPUT_BLOCK;
            }
        }
        // A gamepad we still can't place (no js node yet even after the rescan):
        // block it rather than hand an unidentified pad to every instance.
        if (is_unknown_joystick_node(event_idx, 0)) {
            LOG("evdev: %s -> BLOCKED (unidentified gamepad)", pathname);
            return INPUT_BLOCK;
        }
        // Not a joystick event device - allow (keyboard, mouse, etc.)
        VLOG("evdev: %s -> pass through (not a joystick)", pathname);
    }

    return INPUT_PASS_THROUGH;
}

// Log write-intent opens to absolute paths (skipping devices/kernel/tmp) so we
// can see exactly which mutable files the game touches — the set two instances
// would clash on, hence the minimum that actually needs per-instance isolation.
static void trace_file_open(const char* pathname, int flags) {
    if (!trace_open_enabled || !pathname) return;
    if (!(flags & (O_WRONLY | O_RDWR | O_CREAT))) return;   // reads don't conflict
    if (pathname[0] != '/') return;
    if (strncmp(pathname, "/dev/", 5) == 0 ||
        strncmp(pathname, "/proc/", 6) == 0 ||
        strncmp(pathname, "/sys/", 5) == 0 ||
        strncmp(pathname, "/tmp/", 5) == 0) return;
    LOG("TRACE_OPEN flags=0x%x %s", flags, pathname);
}

int open(const char* pathname, int flags, ...) {
    ensure_evdev_init();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    trace_file_open(pathname, flags);

    // Check hidraw devices first
    InputDeviceAction hidraw_action = check_hidraw_device(pathname);
    if (hidraw_action == INPUT_BLOCK) {
        errno = ENOENT;
        return -1;
    }

    char redirect_path[64];
    InputDeviceAction action = check_input_device(pathname, redirect_path, sizeof(redirect_path));

    switch (action) {
        case INPUT_REDIRECT:
            LOG("open: redirecting %s -> %s", pathname, redirect_path);
            return real_open(redirect_path, flags, mode);
        case INPUT_BLOCK:
            LOG("open: blocking %s", pathname);
            errno = ENOENT;
            return -1;
        case INPUT_ALLOW:
        case INPUT_PASS_THROUGH:
        default:
            return real_open(pathname, flags, mode);
    }
}

int openat(int dirfd, const char* pathname, int flags, ...) {
    ensure_evdev_init();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    trace_file_open(pathname, flags);

    // Check hidraw devices first (absolute paths)
    if (pathname && pathname[0] == '/') {
        InputDeviceAction hidraw_action = check_hidraw_device(pathname);
        if (hidraw_action == INPUT_BLOCK) {
            errno = ENOENT;
            return -1;
        }
    }

    // Check absolute paths for input devices
    if (pathname && pathname[0] == '/') {
        char redirect_path[64];
        InputDeviceAction action = check_input_device(pathname, redirect_path, sizeof(redirect_path));

        switch (action) {
            case INPUT_REDIRECT:
                return real_openat(dirfd, redirect_path, flags, mode);
            case INPUT_BLOCK:
                errno = ENOENT;
                return -1;
            case INPUT_ALLOW:
            case INPUT_PASS_THROUGH:
            default:
                break;
        }
    }

    // Check relative paths that might be input devices (e.g., SDL3 opens "event11" relative to /dev/input)
    if (pathname && pathname[0] != '/' &&
        (strncmp(pathname, "event", 5) == 0 || strncmp(pathname, "js", 2) == 0)) {
        // Construct full path and check
        char full_path[64];
        snprintf(full_path, sizeof(full_path), "/dev/input/%s", pathname);
        char redirect_path[64];
        InputDeviceAction action = check_input_device(full_path, redirect_path, sizeof(redirect_path));

        switch (action) {
            case INPUT_REDIRECT:
                LOG("openat: redirecting relative path '%s' -> '%s'", pathname, redirect_path + strlen("/dev/input/"));
                return real_openat(dirfd, redirect_path + strlen("/dev/input/"), flags, mode);
            case INPUT_BLOCK:
                LOG("openat: blocking relative path '%s' (full: %s)", pathname, full_path);
                errno = ENOENT;
                return -1;
            case INPUT_ALLOW:
                LOG("openat: allowing relative path '%s' (full: %s)", pathname, full_path);
                break;
            case INPUT_PASS_THROUGH:
            default:
                break;
        }
    }

    return real_openat(dirfd, pathname, flags, mode);
}

int open64(const char* pathname, int flags, ...) {
    ensure_evdev_init();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    trace_file_open(pathname, flags);

    // Check hidraw devices first
    InputDeviceAction hidraw_action = check_hidraw_device(pathname);
    if (hidraw_action == INPUT_BLOCK) {
        errno = ENOENT;
        return -1;
    }

    char redirect_path[64];
    InputDeviceAction action = check_input_device(pathname, redirect_path, sizeof(redirect_path));

    switch (action) {
        case INPUT_REDIRECT:
            LOG("open64: redirecting %s -> %s", pathname, redirect_path);
            return real_open64(redirect_path, flags, mode);
        case INPUT_BLOCK:
            LOG("open64: blocking %s", pathname);
            errno = ENOENT;
            return -1;
        case INPUT_ALLOW:
        case INPUT_PASS_THROUGH:
        default:
            return real_open64(pathname, flags, mode);
    }
}

int openat64(int dirfd, const char* pathname, int flags, ...) {
    ensure_evdev_init();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    trace_file_open(pathname, flags);

    // Check hidraw devices first (absolute paths)
    if (pathname && pathname[0] == '/') {
        InputDeviceAction hidraw_action = check_hidraw_device(pathname);
        if (hidraw_action == INPUT_BLOCK) {
            errno = ENOENT;
            return -1;
        }
    }

    // Check absolute paths for input devices
    if (pathname && pathname[0] == '/') {
        char redirect_path[64];
        InputDeviceAction action = check_input_device(pathname, redirect_path, sizeof(redirect_path));

        switch (action) {
            case INPUT_REDIRECT:
                return real_openat64(dirfd, redirect_path, flags, mode);
            case INPUT_BLOCK:
                errno = ENOENT;
                return -1;
            case INPUT_ALLOW:
            case INPUT_PASS_THROUGH:
            default:
                break;
        }
    }

    // Check relative paths that might be input devices
    if (pathname && pathname[0] != '/' &&
        (strncmp(pathname, "event", 5) == 0 || strncmp(pathname, "js", 2) == 0)) {
        char full_path[64];
        snprintf(full_path, sizeof(full_path), "/dev/input/%s", pathname);
        char redirect_path[64];
        InputDeviceAction action = check_input_device(full_path, redirect_path, sizeof(redirect_path));

        switch (action) {
            case INPUT_REDIRECT:
                LOG("openat64: redirecting relative path '%s' -> '%s'", pathname, redirect_path + strlen("/dev/input/"));
                return real_openat64(dirfd, redirect_path + strlen("/dev/input/"), flags, mode);
            case INPUT_BLOCK:
                LOG("openat64: blocking relative path '%s' (full: %s)", pathname, full_path);
                errno = ENOENT;
                return -1;
            case INPUT_ALLOW:
                LOG("openat64: allowing relative path '%s' (full: %s)", pathname, full_path);
                break;
            case INPUT_PASS_THROUGH:
            default:
                break;
        }
    }

    return real_openat64(dirfd, pathname, flags, mode);
}

// =============================================================================
// ioctl: present this instance's Steam virtual pad as "pad 0"
// =============================================================================
// Steam names its virtual pads "Microsoft X-Box 360 pad <slot>", and Proton's
// winebus parses that number into the XInput user index. Each instance only
// sees its own virtual pad, so every instance but the one holding slot 0 got a
// lone pad at XInput player 2+ — and games that read only player 1 (Palworld's
// menus) ignored it. Rewriting the name of the ASSIGNED virtual pad, as read
// through EVIOCGNAME / JSIOCGNAME, makes it player 1 in its own instance.
// JANUS_VIRTUAL_PAD_RENAME=0 disables.

static int virtual_pad_rename_enabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char* v = getenv("JANUS_VIRTUAL_PAD_RENAME");
        cached = !(v && strcmp(v, "0") == 0);
    }
    return cached;
}

// Is fd the assigned virtual pad (its js or event node)? Both nodes hang off
// the same /sys/class/input/inputN device, so compare those directories.
static int fd_is_assigned_virtual_pad(int fd) {
    if (assigned_virtual_js < 0) return 0;
    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISCHR(st.st_mode)) return 0;
    char link[96], mine[PATH_MAX], want[PATH_MAX];
    snprintf(link, sizeof(link), "/sys/dev/char/%u:%u/device",
             major(st.st_rdev), minor(st.st_rdev));
    if (!realpath(link, mine)) return 0;
    snprintf(link, sizeof(link), "/sys/class/input/js%d/device", assigned_virtual_js);
    if (!realpath(link, want)) return 0;
    return strcmp(mine, want) == 0;
}

int ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void* arg = va_arg(ap, void*);
    va_end(ap);
    int r = sys_ioctl(fd, request, arg);
    if (r <= 0 || !arg || !steam_input_active || !virtual_pad_rename_enabled())
        return r;
    // EVIOCGNAME(len) = _IOC(READ,'E',0x06,len); JSIOCGNAME(len) = _IOC(READ,'j',0x13,len)
    int is_name = (_IOC_DIR(request) == _IOC_READ) &&
        ((_IOC_TYPE(request) == 'E' && _IOC_NR(request) == 0x06) ||
         (_IOC_TYPE(request) == 'j' && _IOC_NR(request) == 0x13));
    if (!is_name) return r;
    char* name = (char*)arg;
    size_t cap = _IOC_SIZE(request);
    if (cap == 0 || !memchr(name, '\0', cap)) return r;
    char* pad = strstr(name, "X-Box 360 pad ");
    if (!pad) return r;
    char* digits = pad + strlen("X-Box 360 pad ");
    if (!isdigit((unsigned char)digits[0]) || strcmp(digits, "0") == 0) return r;
    if (!fd_is_assigned_virtual_pad(fd)) return r;
    char old[64];
    safe_strcpy(old, digits, sizeof(old));
    digits[0] = '0';
    digits[1] = '\0';
    VLOG("ioctl: virtual pad name 'pad %s' -> 'pad 0' (assigned js%d)", old, assigned_virtual_js);
    // EVIOCGNAME returns the copied length incl. NUL; JSIOCGNAME the same.
    return (int)strlen(name) + 1;
}

// =============================================================================
// Real dlsym, bypassing our own dlsym hook
// =============================================================================
static void* raw_dlsym(void* handle, const char* symbol) {
    if (!real_dlsym) {
        real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");   // x86_64 baseline
        if (!real_dlsym) real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");   // i386
        if (!real_dlsym) real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    }
    return real_dlsym ? real_dlsym(handle, symbol) : NULL;
}

// =============================================================================
// dlopen Hook - Strip RTLD_DEEPBIND to preserve LD_PRELOAD overrides
// =============================================================================

// Source Engine (and some other games) load shared libraries with RTLD_DEEPBIND,
// which causes the library's own dependencies to be searched before the global scope.
// This bypasses our LD_PRELOAD hooks (evdev open(), etc.). We strip the flag.

#ifndef RTLD_DEEPBIND
#define RTLD_DEEPBIND 0x00008
#endif

// Remember the path the game asked for before we swap in Goldberg, so the hybrid
// (Goldberg for multiplayer + REAL Steam Input) has a real Valve lib to load even
// when steam.py found none in the install dir. Games that ship the lib inside a jar
// (steamworks4j/LWJGL: Necesse) only materialize it at runtime under /tmp — this is
// the only place its path is ever visible.
//
// Skip candidates that ARE Goldberg: the mirror holds a byte-copy of
// goldberg_lib_path, so an equal file size identifies it (gbe_fork is 6MB/75MB vs
// Valve's ~380KB — a collision is not a realistic concern).
static void capture_valve_steam_api(const char* path) {
    if (captured_valve_steam_api[0] || !path || !path[0]) return;
    struct stat sc, sg;
    if (stat(path, &sc) != 0) return;
    if (goldberg_lib_path && stat(goldberg_lib_path, &sg) == 0 &&
        (sc.st_size == sg.st_size || (sc.st_dev == sg.st_dev && sc.st_ino == sg.st_ino))) {
        LOG("HybridSteamInput: dlopen('%s') is the Goldberg lib itself — not capturing", path);
        return;
    }
    snprintf(captured_valve_steam_api, sizeof(captured_valve_steam_api), "%s", path);
    LOG("HybridSteamInput: captured original Valve libsteam_api.so at '%s'", path);
}

void* dlopen(const char* filename, int flags) {
    resolve_real_dlopen();
    if (!real_dlopen) return NULL;

    if (flags & RTLD_DEEPBIND) {
        LOG("dlopen: stripping RTLD_DEEPBIND from '%s' (flags=0x%x)", filename ? filename : "(null)", flags);
        flags &= ~RTLD_DEEPBIND;
    }

    // Goldberg: redirect libsteam_api.so to the emulator version
    if (goldberg_lib_path && filename) {
        // Match "libsteam_api.so" anywhere in the path (bare name or full path)
        const char* base = strrchr(filename, '/');
        base = base ? base + 1 : filename;
        if (strcmp(base, "libsteam_api.so") == 0) {
            capture_valve_steam_api(filename);
            LOG("Goldberg: redirecting dlopen('%s') -> '%s'", filename, goldberg_lib_path);
            return real_dlopen(goldberg_lib_path, flags);
        }
    }

    return real_dlopen(filename, flags);
}

// =============================================================================
// SDL_SetHint Exports - Block steamclient.so from enabling virtual gamepads
// (Proton / vtable filter only; see sdl_reads_steam_virtual_pad())
// =============================================================================
// steamclient.so links SDL3 directly (not via dlsym), so dlsym interception
// doesn't work. Instead, we EXPORT SDL_SetHint via LD_PRELOAD so the dynamic
// linker resolves steamclient.so's SDL_SetHint calls to our wrapper.
//
// Unlike the joystick SDL functions (which must be hidden to avoid breaking
// games), SDL_SetHint is a simple hint setter — our wrapper only blocks one
// specific hint ("STEAM_VIRTUAL_GAMEPAD"=1) and passes everything else through.
// This is safe even when games call SDL_SetHint internally.

static void ensure_real_SDL_SetHint(void) {
    if (!real_SDL_SetHint_fn) {
        // Use dlvsym to get the real dlsym first (in case real_dlsym isn't set yet)
        void* (*get_dlsym)(void*, const char*) = real_dlsym;
        if (!get_dlsym) {
            get_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
            if (!get_dlsym)
                get_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");
        }
        if (get_dlsym) {
            real_SDL_SetHint_fn = get_dlsym(RTLD_NEXT, "SDL_SetHint");
            real_SDL_SetHintWithPriority_fn = get_dlsym(RTLD_NEXT, "SDL_SetHintWithPriority");
        }
    }
}

// Visible export — overrides SDL_SetHint for ALL libraries loaded via LD_PRELOAD
int SDL_SetHint(const char* name, const char* value) {
    ensure_real_SDL_SetHint();
    if (getenv("JANUS_CONTROLLER") && !sdl_reads_steam_virtual_pad() &&
        name && strstr(name, "STEAM_VIRTUAL_GAMEPAD") &&
        value && strcmp(value, "1") == 0) {
        LOG("Steam Input: BLOCKED SDL_SetHint(\"%s\", \"%s\")", name, value);
        return 0;  // SDL_FALSE
    }
    if (real_SDL_SetHint_fn)
        return real_SDL_SetHint_fn(name, value);
    return 0;
}

int SDL_SetHintWithPriority(const char* name, const char* value, int priority) {
    ensure_real_SDL_SetHint();
    if (getenv("JANUS_CONTROLLER") && !sdl_reads_steam_virtual_pad() &&
        name && strstr(name, "STEAM_VIRTUAL_GAMEPAD") &&
        value && strcmp(value, "1") == 0) {
        LOG("Steam Input: BLOCKED SDL_SetHintWithPriority(\"%s\", \"%s\", %d)", name, value, priority);
        return 0;
    }
    if (real_SDL_SetHintWithPriority_fn)
        return real_SDL_SetHintWithPriority_fn(name, value, priority);
    return 0;
}

// =============================================================================
// ISteamInput Blocking - Force SDL2 fallback for controller input
// =============================================================================
// When Steam Input is active, steamclient.so delivers controller input via IPC
// (shared memory), bypassing our evdev hooks entirely. Games that use
// ISteamInput/ISteamController (via vtable) get ALL controllers' input.
//
// By returning NULL for SteamInput/SteamController interfaces, we force the
// game to fall back to SDL2 for controller input, where our evdev hooks filter
// correctly. L4D2 and other Source Engine games handle NULL gracefully since
// they predated Steam Input.

static int is_steam_input_interface(const char* version) {
    if (!version) return 0;
    return (strncmp(version, "SteamInput", 10) == 0 ||
            strncmp(version, "SteamController", 15) == 0 ||
            strncmp(version, "STEAMCONTROLLER_", 16) == 0 ||
            strncmp(version, "STEAMINPUT_", 11) == 0);
}

// Resolve real Steam internal functions (lazy — retries until libsteam_api.so is loaded)
static void* steam_api_handle = NULL;
static void* (*glibc_dlsym_fn)(void*, const char*) = NULL;
static void* (*glibc_dlopen_fn)(const char*, int) = NULL;

static void ensure_glibc_dl(void) {
    if (glibc_dlsym_fn) return;
    glibc_dlsym_fn = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    if (!glibc_dlsym_fn)
        glibc_dlsym_fn = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");
    if (!glibc_dlsym_fn)
        glibc_dlsym_fn = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    // Also get real dlopen for RTLD_NOLOAD
    glibc_dlopen_fn = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.1");
    if (!glibc_dlopen_fn)
        glibc_dlopen_fn = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.2.5");
    if (!glibc_dlopen_fn)
        glibc_dlopen_fn = dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.0");
}

static void try_resolve_steam_api(void) {
    if (steam_api_handle) return;
    ensure_glibc_dl();
    if (!glibc_dlopen_fn || !glibc_dlsym_fn) return;

    // Try to get handle of already-loaded libsteam_api.so
    steam_api_handle = glibc_dlopen_fn("libsteam_api.so", RTLD_NOLOAD | RTLD_NOW);
    if (!steam_api_handle) return;

    real_SteamInternal_FindOrCreateUserInterface =
        glibc_dlsym_fn(steam_api_handle, "SteamInternal_FindOrCreateUserInterface");
    real_SteamInternal_CreateInterface =
        glibc_dlsym_fn(steam_api_handle, "SteamInternal_CreateInterface");

    LOG("Steam Input: found libsteam_api.so handle=%p, FindOrCreate=%p, Create=%p",
        steam_api_handle,
        (void*)real_SteamInternal_FindOrCreateUserInterface,
        (void*)real_SteamInternal_CreateInterface);
}

// =============================================================================
// Steam Input vtable FILTER (JANUS_STEAM_INPUT_FILTER=1)
// =============================================================================
// Instead of nulling ISteamInput/ISteamController (which forces the SDL2/evdev
// path and breaks controllers that ONLY work via Steam Input — e.g. generic
// PS3-style adapters that have no SDL gamecontroller mapping), pass the REAL
// interface through but install a shadow vtable that filters
// GetConnectedControllers to ONLY this instance's assigned controller handle.
// The handle comes from virtualgamepadinfo.txt (physical VID:PID -> Steam handle),
// matched by VID:PID + JANUS_INSTANCE_RANK (identical-pad tie-break) — the same
// assignment the evdev path uses. Slots recovered from the game's own
// libsteam_api.so flat wrappers (32-bit): SteamController008
// GetConnectedControllers = slot 3, SteamInput006 = slot 6. See NOTES.md.

static unsigned long long g_assigned_steam_handle = 0;   // (declared above)

static int steam_filter_on(void) {
    const char* e = getenv("JANUS_STEAM_INPUT_FILTER");
    return e && e[0] != '0';
}

// Resolve the assigned controller's Steam handle from virtualgamepadinfo.txt.
// Steam writes that file a few seconds AFTER game start, so this is called lazily
// (every GetConnectedControllers) and does NOT cache an empty result — it keeps
// retrying (re-reading the file) until Steam populates it, then caches the handle.
static void compute_assigned_steam_handle(void) {
    if (g_assigned_steam_handle) return;   // already resolved
    parse_virtualgamepadinfo();
    if (num_steam_virtual == 0) return;    // not written yet -> retry on the next call
    // Assigned VID:PID: the live identity (set from JANUS_CONTROLLER_VID/PID at
    // init, updated when the pad changes mode on a replug), else the env.
    unsigned short pv = assigned_vid, pp = assigned_pid;
    const char* ev = getenv("JANUS_CONTROLLER_VID");
    const char* ep = getenv("JANUS_CONTROLLER_PID");
    if (!pv && ev) pv = (unsigned short)atoi(ev);
    if (!pp && ep) pp = (unsigned short)atoi(ep);
    // Candidates matching VID:PID (or all slots if none match), sorted by slot.
    int cand[MAX_CONTROLLERS], nc = 0;
    for (int i = 0; i < num_steam_virtual; i++)
        if (steam_virtual_controllers[i].vid == pv && steam_virtual_controllers[i].pid == pp)
            cand[nc++] = i;
    if (nc == 0) {
        for (int i = 0; i < num_steam_virtual; i++) cand[nc++] = i;
        match_virtual_to_physical();
        nc = drop_other_pads_slots(cand, nc);
    }
    for (int a = 1; a < nc; a++) {   // insertion sort by Steam slot
        int key = cand[a], b = a - 1;
        while (b >= 0 && steam_virtual_controllers[cand[b]].slot >
                         steam_virtual_controllers[key].slot) { cand[b+1] = cand[b]; b--; }
        cand[b+1] = key;
    }
    const char* rank_env = getenv("JANUS_INSTANCE_RANK");
    int rank = rank_env ? atoi(rank_env) : 0;
    if (rank < 0) rank = 0;
    int pick = (rank < nc) ? rank : (rank % nc);
    g_assigned_steam_handle = strtoull(steam_virtual_controllers[cand[pick]].handle, NULL, 16);
    LOG("SteamFilter: assigned handle=0x%llx (slot %d '%s' %04x:%04x, rank %d, %d cand)",
        g_assigned_steam_handle, steam_virtual_controllers[cand[pick]].slot,
        steam_virtual_controllers[cand[pick]].name,
        steam_virtual_controllers[cand[pick]].vid, steam_virtual_controllers[cand[pick]].pid,
        rank, nc);
}

// Trim a GetConnectedControllers result to this instance's ONE controller.
// Primary: keep the handle matched by identity (virtualgamepadinfo VID:PID+rank).
// Fallback (file not written yet, or handle not in the list): keep the rank-th
// handle in sorted order, so instances still get DISJOINT controllers — never
// pass through unfiltered (that leaks: all pads drive all games). Worst case the
// two are swapped, not cross-talking.
static int steam_filter_handles(unsigned long long* out, int n) {
    if (n <= 0) return n;
    compute_assigned_steam_handle();   // lazy; uses virtualgamepadinfo if Steam populates it
    if (g_assigned_steam_handle) {
        for (int i = 0; i < n; i++)
            if (out[i] == g_assigned_steam_handle) { out[0] = out[i]; return 1; }
    }
    // virtualgamepadinfo.txt is EMPTY for native ISteamController games, so match by the
    // VID:PID that Steam packs into the handle. Layout: [upper 36 bits: type+VID+PID]
    // [lower 28 bits: Steam account id]. `up = handle >> 28` isolates the identity; the
    // VID/PID fields are packed either 16-bit+16-bit (0x36e83001 -> 36e8:3001) or
    // 12-bit+12-bit (0x0345e28e -> 45e:28e), depending on controller type. Match both.
    unsigned short vid = assigned_vid, pid = assigned_pid;   // live identity (see above)
    const char* ev = getenv("JANUS_CONTROLLER_VID");
    const char* ep = getenv("JANUS_CONTROLLER_PID");
    if (!vid && ev) vid = (unsigned short)atoi(ev);
    if (!pid && ep) pid = (unsigned short)atoi(ep);
    unsigned long long cand[16]; int nc = 0;
    if (vid && pid) {
        for (int i = 0; i < n && nc < 16; i++) {
            unsigned int u = (unsigned int)(out[i] >> 28);
            int m16 = (u == (((unsigned)vid << 16) | pid));
            int m12 = (((u >> 12) & 0xFFF) == (unsigned)(vid & 0xFFF)) &&
                      ((u & 0xFFF) == (unsigned)(pid & 0xFFF));
            if (m16 || m12) cand[nc++] = out[i];
        }
    }
    if (nc == 0) for (int i = 0; i < n && nc < 16; i++) cand[nc++] = out[i];
    for (int a = 1; a < nc; a++) {   // sort ascending for a consistent order across instances
        unsigned long long k = cand[a]; int b = a - 1;
        while (b >= 0 && cand[b] > k) { cand[b+1] = cand[b]; b--; }
        cand[b+1] = k;
    }
    const char* rank_env = getenv("JANUS_INSTANCE_RANK");
    int rank = rank_env ? atoi(rank_env) : 0;
    if (rank < 0) rank = 0;
    out[0] = cand[(rank < nc) ? rank : (rank % nc)];
    return 1;
}

typedef int (*getconn_fn_t)(void*, unsigned long long*);
static getconn_fn_t orig_controller_getconn = NULL;
static getconn_fn_t orig_input_getconn = NULL;

static int thunk_controller_getconn(void* self, unsigned long long* out) {
    int n = orig_controller_getconn ? orig_controller_getconn(self, out) : 0;
    return steam_filter_handles(out, n);
}
static int thunk_input_getconn(void* self, unsigned long long* out) {
    int n = orig_input_getconn ? orig_input_getconn(self, out) : 0;
    return steam_filter_handles(out, n);
}

#define STEAM_VT_SLOTS 128
static void* shadow_vt_controller[STEAM_VT_SLOTS];
static void* shadow_vt_input[STEAM_VT_SLOTS];

// Recover a method's vtable slot by reading the matching flat wrapper in
// libsteam_api.so — robust across interface versions (SteamInput005/006/007,
// SteamController005..008) instead of hardcoding. The wrapper loads the vtable
// then does an indirect call through it; we find the first such call/jmp:
//   FF /2 or FF /4, mod=01 (disp8) or mod=10 (disp32), through a base reg.
// slot = disp / sizeof(void*). Returns `fallback` if no clean pattern is found.
static void* g_real_steam_handle;   // tentative decl; defined with the hybrid below
static int recover_getconnected_slot(int is_input, int fallback) {
    // Read the wrapper from the lib that actually produced the interface object:
    // under the hybrid that's the REAL Valve lib, not Goldberg's emulator.
    void* h = g_real_steam_handle ? g_real_steam_handle : steam_api_handle;
    if (!h || !glibc_dlsym_fn) return fallback;
    const char* sym = is_input
        ? "SteamAPI_ISteamInput_GetConnectedControllers"
        : "SteamAPI_ISteamController_GetConnectedControllers";
    unsigned char* p = (unsigned char*)glibc_dlsym_fn(h, sym);
    if (!p) return fallback;
    for (int i = 0; i < 48; i++) {
        if (p[i] != 0xFF) continue;
        unsigned char modrm = p[i + 1];
        unsigned char reg = (modrm >> 3) & 7;   // /2 = call, /4 = jmp
        unsigned char mod = (modrm >> 6) & 3;
        unsigned char rm  = modrm & 7;
        if (reg != 2 && reg != 4) continue;
        if (rm == 4 || rm == 5) continue;       // skip SIB / rip-relative forms
        int disp = -1;
        if (mod == 1) disp = (signed char)p[i + 2];                       // disp8
        else if (mod == 2) disp = p[i+2] | (p[i+3]<<8) | (p[i+4]<<16) | (p[i+5]<<24);
        if (disp >= 0 && disp < STEAM_VT_SLOTS * (int)sizeof(void*) &&
            (disp % (int)sizeof(void*)) == 0)
            return disp / (int)sizeof(void*);
    }
    return fallback;
}

// Install a shadow vtable on the interface object that filters GetConnectedControllers.
static void install_steam_filter(void* iface, const char* version) {
    if (!iface) return;
    compute_assigned_steam_handle();   // may be deferred; the thunk retries + rank-fallbacks
    int is_input = (strncmp(version, "SteamInput", 10) == 0);
    int slot = recover_getconnected_slot(is_input, is_input ? 6 : 3);  // robust across versions
    void** shadow = is_input ? shadow_vt_input : shadow_vt_controller;
    void** vt = *(void***)iface;                // real vtable
    if (vt == shadow) return;                   // already installed on this object
    for (int i = 0; i < STEAM_VT_SLOTS; i++) shadow[i] = vt[i];
    if (is_input) { orig_input_getconn = (getconn_fn_t)vt[slot]; shadow[slot] = (void*)thunk_input_getconn; }
    else          { orig_controller_getconn = (getconn_fn_t)vt[slot]; shadow[slot] = (void*)thunk_controller_getconn; }
    *(void***)iface = shadow;
    if (g_assigned_steam_handle)
        LOG("SteamFilter: shadow vtable on %s (slot %d), keep handle 0x%llx",
            version, slot, g_assigned_steam_handle);
    else
        LOG("SteamFilter: shadow vtable on %s (slot %d), handle deferred "
            "(virtualgamepadinfo not written yet — will resolve, rank fallback meanwhile)", version, slot);
}

// =============================================================================
// Steam ID override (JANUS_STEAM_ID_OVERRIDE) — two local instances, one account
// =============================================================================
// For games whose multiplayer needs a genuine Steam login (Palworld: Epic Online
// Services logs in with a Steam ticket, so Goldberg greys multiplayer out), both
// instances must run on the user's real account. A LOCAL dedicated server
// (bUseAuth=False, 127.0.0.1) then rejects the second as "already logged in",
// because it keys players by the reported steam_<SteamID64>. This override
// changes only ISteamUser::GetSteamID in this one process so the user's own two
// instances are distinct players; every other Steam call stays real. Off unless
// the env var is set (the handler sets it for instance 2+ only).
//
// Path: game -> steam_api(64) -> Proton lsteamclient -> Linux steamclient.so,
// dlopen'd into this process. lsteamclient resolves
// dlsym(steamclient, "CreateInterface"); we hand back a wrapper that installs
// shadow vtables: ISteamClient::GetISteamUser (slot 5) -> on the returned
// ISteamUser, GetSteamID (slot 2) returns the override. Both slots are stable
// across SteamClient017..023 / SteamUser019..023. CSteamID is trivially
// copyable and 8 bytes, so the SysV ABI returns it in RAX.
typedef void* (*CreateInterface_fn)(const char*, int*);
static CreateInterface_fn real_steamclient_CreateInterface;
static unsigned long long g_steamid_override;   // 0 = off
static int g_steamid_override_checked;

static unsigned long long steamid_override(void) {
    if (!g_steamid_override_checked) {
        const char* e = getenv("JANUS_STEAM_ID_OVERRIDE");
        g_steamid_override = (e && *e) ? strtoull(e, NULL, 10) : 0;
        g_steamid_override_checked = 1;
        if (g_steamid_override)
            LOG("SteamID override: GetSteamID -> %llu", g_steamid_override);
    }
    return g_steamid_override;
}

#define SID_SLOTS 128
#define SID_MAX_VT 8
typedef void* (*GetISteamUser_fn)(void*, int, int, const char*);
// One shadow per distinct real vtable (different interface versions).
static struct { void** real_vt; void* shadow[SID_SLOTS]; void* orig; }
    sid_client_vt[SID_MAX_VT], sid_user_vt[SID_MAX_VT];
static pthread_mutex_t sid_lock = PTHREAD_MUTEX_INITIALIZER;

static unsigned long long thunk_GetSteamID(void* self) {
    (void)self;
    return g_steamid_override;
}

// Point obj's vptr at a shadow copy of its vtable with `slot` replaced.
static void sid_install(void* obj, int slot, void* thunk,
                        typeof(sid_client_vt) table, const char* what) {
    if (!obj) return;
    pthread_mutex_lock(&sid_lock);
    void** vt = *(void***)obj;
    for (int i = 0; i < SID_MAX_VT; i++)
        if (vt == table[i].shadow) { pthread_mutex_unlock(&sid_lock); return; }
    int k = -1;
    for (int i = 0; i < SID_MAX_VT; i++) {
        if (table[i].real_vt == vt) { k = i; break; }
        if (!table[i].real_vt && k < 0) k = i;
    }
    if (k >= 0) {
        if (table[k].real_vt != vt) {
            table[k].real_vt = vt;
            for (int i = 0; i < SID_SLOTS; i++) table[k].shadow[i] = vt[i];
            table[k].orig = vt[slot];
            table[k].shadow[slot] = thunk;
        }
        *(void***)obj = table[k].shadow;
        LOG("SteamID override: %s %p patched", what, obj);
    }
    pthread_mutex_unlock(&sid_lock);
}

static void* thunk_GetISteamUser(void* self, int hUser, int hPipe, const char* ver) {
    GetISteamUser_fn orig = NULL;
    void** vt = *(void***)self;
    for (int i = 0; i < SID_MAX_VT; i++)
        if (vt == sid_client_vt[i].shadow) { orig = (GetISteamUser_fn)sid_client_vt[i].orig; break; }
    void* user = orig ? orig(self, hUser, hPipe, ver) : NULL;
    sid_install(user, 2, (void*)thunk_GetSteamID, sid_user_vt, ver ? ver : "ISteamUser");
    return user;
}

static void* janus_steamclient_CreateInterface(const char* ver, int* err) {
    void* r = real_steamclient_CreateInterface ? real_steamclient_CreateInterface(ver, err) : NULL;
    if (r && ver) {
        if (strncmp(ver, "SteamClient", 11) == 0)
            sid_install(r, 5, (void*)thunk_GetISteamUser, sid_client_vt, ver);
        else if (strncmp(ver, "SteamUser", 9) == 0)
            sid_install(r, 2, (void*)thunk_GetSteamID, sid_user_vt, ver);
    }
    return r;
}

// =============================================================================
// Hybrid: Goldberg for multiplayer + REAL Steam Input (JANUS_STEAM_INPUT_REAL_LIB)
// =============================================================================
// Goldberg replaces libsteam_api.so, and its ISteamInput/ISteamController is an
// emulated stub — so native games lose real Steam Input. When both Goldberg and
// the filter are on, we load the REAL Valve libsteam_api.so separately (its path
// in JANUS_STEAM_INPUT_REAL_LIB), init it so its steamclient IPC is live, and route
// only the SteamInput/SteamController interface requests to it (everything else —
// matchmaking, networking, user — still goes to Goldberg). The vtable filter then
// restricts the real interface per-instance. Requires the real Steam client running.
static void* g_real_steam_handle = NULL;
static void* (*g_real_findorcreate)(int, const char*) = NULL;
static void* (*g_real_createinterface)(const char*) = NULL;
static int g_real_steam_ready = -1;   // -1 unknown, 0 no, 1 yes

static int ensure_real_steam_input(void) {
    if (g_real_steam_ready >= 0) return g_real_steam_ready;
    const char* real_path = getenv("JANUS_STEAM_INPUT_REAL_LIB");
    // Fallback: the path the dlopen hook saw before redirecting to Goldberg.
    // Needed for games that ship libsteam_api.so inside a jar (Necesse), where
    // steam.py's install-dir scan finds nothing and the env var is unset.
    if ((!real_path || !real_path[0]) && captured_valve_steam_api[0]) {
        real_path = captured_valve_steam_api;
        LOG("HybridSteamInput: JANUS_STEAM_INPUT_REAL_LIB unset — using captured '%s'", real_path);
    }
    // Not a failure yet: the game may not have dlopen'd libsteam_api.so so far.
    // Stay unresolved so the next interface request retries.
    if (!real_path || !real_path[0]) return 0;
    ensure_glibc_dl();
    if (!glibc_dlopen_fn || !glibc_dlsym_fn) { g_real_steam_ready = 0; return 0; }
    // Full path via glibc dlopen bypasses our Goldberg dlopen redirect; RTLD_LOCAL
    // keeps Valve's symbols out of the global scope so they don't clash with the
    // already-loaded Goldberg libsteam_api.so.
    void* h = glibc_dlopen_fn(real_path, RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        LOG("HybridSteamInput: dlopen('%s') failed", real_path);
        g_real_steam_ready = 0; return 0;
    }
    int (*p_InitFlat)(char*) = (int(*)(char*))glibc_dlsym_fn(h, "SteamAPI_InitFlat");
    unsigned char (*p_Init)(void) = (unsigned char(*)(void))glibc_dlsym_fn(h, "SteamAPI_Init");
    char errbuf[1024]; errbuf[0] = 0; int rc = -1;
    if (p_InitFlat) rc = p_InitFlat(errbuf);
    else if (p_Init) rc = p_Init() ? 0 : 2;
    g_real_findorcreate  = (void*(*)(int,const char*))glibc_dlsym_fn(h, "SteamInternal_FindOrCreateUserInterface");
    g_real_createinterface = (void*(*)(const char*))glibc_dlsym_fn(h, "SteamInternal_CreateInterface");
    g_real_steam_handle = h;
    g_real_steam_ready = (g_real_findorcreate != NULL) ? 1 : 0;
    LOG("HybridSteamInput: real lib '%s' Init rc=%d '%s' FindOrCreate=%p -> %s",
        real_path, rc, errbuf, (void*)g_real_findorcreate,
        g_real_steam_ready ? "ROUTING Steam Input to REAL Steam" : "FAILED (no FindOrCreate)");
    return g_real_steam_ready;
}

// Visible export: overrides SteamInternal_FindOrCreateUserInterface from libsteam_api.so
void* SteamInternal_FindOrCreateUserInterface(int hSteamUser, const char* pszVersion) {
    try_resolve_steam_api();

    if (getenv("JANUS_CONTROLLER") && is_steam_input_interface(pszVersion)) {
        if (steam_filter_on()) {
            void* iface = NULL;
            if (ensure_real_steam_input() && g_real_findorcreate)
                iface = g_real_findorcreate(hSteamUser, pszVersion);   // REAL Steam (hybrid w/ Goldberg)
            else if (real_SteamInternal_FindOrCreateUserInterface)
                iface = real_SteamInternal_FindOrCreateUserInterface(hSteamUser, pszVersion);
            if (iface) install_steam_filter(iface, pszVersion);
            return iface;
        }
        LOG("Steam Input: returning NULL for %s (forcing SDL2 controller fallback)", pszVersion);
        return NULL;
    }

    if (real_SteamInternal_FindOrCreateUserInterface)
        return real_SteamInternal_FindOrCreateUserInterface(hSteamUser, pszVersion);

    LOG("Steam Input: WARNING - FindOrCreateUserInterface(\"%s\") but libsteam_api.so not found",
        pszVersion ? pszVersion : "(null)");
    return NULL;
}

// Also hook SteamInternal_CreateInterface (some games use this instead)
void* SteamInternal_CreateInterface(const char* pszVersion) {
    try_resolve_steam_api();

    if (getenv("JANUS_CONTROLLER") && is_steam_input_interface(pszVersion)) {
        if (steam_filter_on()) {
            void* iface = NULL;
            if (ensure_real_steam_input() && g_real_createinterface)
                iface = g_real_createinterface(pszVersion);   // REAL Steam (hybrid w/ Goldberg)
            else if (real_SteamInternal_CreateInterface)
                iface = real_SteamInternal_CreateInterface(pszVersion);
            if (iface) install_steam_filter(iface, pszVersion);
            return iface;
        }
        LOG("Steam Input: returning NULL for %s via CreateInterface", pszVersion);
        return NULL;
    }

    if (real_SteamInternal_CreateInterface)
        return real_SteamInternal_CreateInterface(pszVersion);

    LOG("Steam Input: WARNING - CreateInterface(\"%s\") but libsteam_api.so not found",
        pszVersion ? pszVersion : "(null)");
    return NULL;
}

// =============================================================================
// dlsym Hook - Intercept dynamically loaded SDL2/SDL3 functions
// =============================================================================

// Games (Source Engine, FNA, etc.) load SDL2/SDL3 via dlopen/dlsym,
// bypassing LD_PRELOAD. We hook dlsym to return our hook functions instead.

// =============================================================================
// Goldberg invite fix (JANUS_GOLDBERG_INVITE_FIX=1, native games only)
// =============================================================================
// gbe_fork's ActivateGameOverlayInviteDialog (dll/steam_friends.cpp) sends the
// auto_send_invite.txt lobby invite only to friends with
//     fr.lobby_id() != settings->get_lobby()
// meant as "skip friends already in MY lobby". When the inviter itself is in
// no lobby, both sides are 0 and EVERY friend not in some lobby is skipped.
// Terraria/tModLoader hit exactly that: the Host & Play server process owns
// the lobby and the host's client never joins it, so "Invite Friends" only
// ever reached the host's own server (NOTES.md "Terraria / tModLoader").
//
// We keep the vendored Goldberg byte-identical to the upstream release and
// fix it here instead: the dlsym hook hands out this replacement for the flat
// export SteamAPI_ISteamFriends_ActivateGameOverlayInviteDialog (Steamworks.NET
// and other flat-API wrappers resolve it by name through dlsym). It does what
// gbe's auto_send was meant to do, through gbe's own public flat API: invite
// every friend playing the same appid who isn't already a member of the lobby.
// The real function is NOT called (it would re-invite the friends it doesn't
// skip, and only opens gbe's overlay otherwise).
//
// Version dependence: only on the Steamworks flat API names below (stable
// across gbe_fork releases; checked against the vendored regular + debug
// builds). If a future gbe_fork fixes the condition, this stays correct.
// Not applied to Proton (the PE steam_api64.dll is resolved by Wine's loader,
// not dlsym) or to games calling the C++ interface vtable directly.
typedef int      (*gbe_GetFriendCount_fn)(void* self, int flags);
typedef uint64_t (*gbe_GetFriendByIndex_fn)(void* self, int idx, int flags);
typedef bool     (*gbe_GetFriendGamePlayed_fn)(void* self, uint64_t friend_id, void* info);
typedef void*    (*gbe_accessor_fn)(void);
typedef bool     (*gbe_InviteUserToLobby_fn)(void* self, uint64_t lobby, uint64_t user);
typedef int      (*gbe_GetNumLobbyMembers_fn)(void* self, uint64_t lobby);
typedef uint64_t (*gbe_GetLobbyMemberByIndex_fn)(void* self, uint64_t lobby, int idx);
typedef uint32_t (*gbe_GetAppID_fn)(void* self);

static void* gbe_invite_handle = NULL;   // the Goldberg lib the game looked us up in

static bool goldberg_invite_fix_enabled(void) {
    const char* v = getenv("JANUS_GOLDBERG_INVITE_FIX");
    return v && strcmp(v, "0") != 0;
}

static void janus_ActivateGameOverlayInviteDialog(void* friends, uint64_t lobby) {
    void* h = gbe_invite_handle;
    #define GBE(type, name) type name = (type)raw_dlsym(h, "SteamAPI_" #name)
    GBE(gbe_GetFriendCount_fn, ISteamFriends_GetFriendCount);
    GBE(gbe_GetFriendByIndex_fn, ISteamFriends_GetFriendByIndex);
    GBE(gbe_GetFriendGamePlayed_fn, ISteamFriends_GetFriendGamePlayed);
    GBE(gbe_accessor_fn, SteamMatchmaking_v009);
    GBE(gbe_InviteUserToLobby_fn, ISteamMatchmaking_InviteUserToLobby);
    GBE(gbe_GetNumLobbyMembers_fn, ISteamMatchmaking_GetNumLobbyMembers);
    GBE(gbe_GetLobbyMemberByIndex_fn, ISteamMatchmaking_GetLobbyMemberByIndex);
    GBE(gbe_accessor_fn, SteamUtils_v010);
    GBE(gbe_accessor_fn, SteamUtils_v009);
    GBE(gbe_GetAppID_fn, ISteamUtils_GetAppID);
    #undef GBE
    if (!ISteamFriends_GetFriendCount || !ISteamFriends_GetFriendByIndex ||
        !ISteamFriends_GetFriendGamePlayed || !SteamMatchmaking_v009 ||
        !ISteamMatchmaking_InviteUserToLobby) {
        LOG("GoldbergInvite: flat API missing in this steam_api build; no invites sent");
        return;
    }
    void* mm = SteamMatchmaking_v009();
    gbe_accessor_fn utils_fn = SteamUtils_v010 ? SteamUtils_v010 : SteamUtils_v009;
    void* utils = utils_fn ? utils_fn() : NULL;
    uint32_t appid = (utils && ISteamUtils_GetAppID) ? ISteamUtils_GetAppID(utils) : 0;
    if (!mm || !lobby) {
        LOG("GoldbergInvite: no matchmaking interface or no lobby (lobby=%llu)",
            (unsigned long long)lobby);
        return;
    }

    const int k_EFriendFlagImmediate = 0x04;
    int n = ISteamFriends_GetFriendCount(friends, k_EFriendFlagImmediate);
    int members = ISteamMatchmaking_GetNumLobbyMembers
                ? ISteamMatchmaking_GetNumLobbyMembers(mm, lobby) : 0;
    int sent = 0;
    for (int i = 0; i < n; i++) {
        uint64_t id = ISteamFriends_GetFriendByIndex(friends, i, k_EFriendFlagImmediate);
        // FriendGameInfo_t starts with CGameID m_gameID (appid = low 24 bits);
        // the rest of its layout depends on packing, so only that is read.
        uint64_t info[8] = {0};
        if (!ISteamFriends_GetFriendGamePlayed(friends, id, info))
            continue;
        if (appid && (uint32_t)(info[0] & 0xFFFFFF) != appid)
            continue;
        bool in_lobby = false;
        for (int m = 0; m < members && ISteamMatchmaking_GetLobbyMemberByIndex; m++)
            if (ISteamMatchmaking_GetLobbyMemberByIndex(mm, lobby, m) == id) { in_lobby = true; break; }
        if (in_lobby)
            continue;
        bool ok = ISteamMatchmaking_InviteUserToLobby(mm, lobby, id);
        LOG("GoldbergInvite: lobby %llu -> friend %llu: %s", (unsigned long long)lobby,
            (unsigned long long)id, ok ? "sent" : "FAILED (lobby unknown to this process?)");
        sent += ok;
    }
    LOG("GoldbergInvite: %d invite(s) sent (%d friend(s), %d lobby member(s), appid %u)",
        sent, n, members, appid);
}

// The exported dlsym is an assembly stub. For RTLD_NEXT it jumps (no call, so
// no stack frame of ours) straight into glibc's dlsym, which then resolves
// "next" relative to the REAL caller's return address. Calling the real dlsym
// from C instead made every RTLD_NEXT lookup search after *this* library: a
// library preloaded after us that wraps a function got its own wrapper back
// and recursed forever (Steam's gameoverlayrenderer.so, which svencoop.sh
// preloads, in open() and XOpenDisplay(): Sven Co-op segfaulted at start).
// Other handles go to janus_dlsym_impl. janus_next_override() keeps the
// SDL_SetHint wrapper for RTLD_NEXT lookups.
__attribute__((visibility("hidden"))) void* janus_dlsym_impl(void* handle, const char* symbol);
__attribute__((visibility("hidden"))) void* janus_next_override(const char* symbol);
__attribute__((visibility("hidden"))) void* janus_real_dlsym_ptr(void);

static void resolve_real_dlsym(void) {
    if (real_dlsym) return;
    real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");  // x86_64 baseline
    if (!real_dlsym)
        real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");  // i386 baseline
    if (!real_dlsym)
        real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");  // glibc 2.34+
}

void* janus_real_dlsym_ptr(void) {
    resolve_real_dlsym();
    return (void*)real_dlsym;
}

void* janus_next_override(const char* symbol) {
    if (!symbol || !getenv("JANUS_CONTROLLER")) return NULL;
    if (strcmp(symbol, "SDL_SetHint") == 0) {
        ensure_real_SDL_SetHint();
        VLOG("dlsym: returning SDL_SetHint wrapper");
        return (void*)SDL_SetHint;
    }
    if (strcmp(symbol, "SDL_SetHintWithPriority") == 0) {
        ensure_real_SDL_SetHint();
        VLOG("dlsym: returning SDL_SetHintWithPriority wrapper");
        return (void*)SDL_SetHintWithPriority;
    }
    return NULL;
}

#if defined(__x86_64__)
__asm__(
    ".text\n"
    ".globl dlsym\n"
    ".type dlsym, @function\n"
    "dlsym:\n"
    "    cmpq $-1, %rdi\n"                 // RTLD_NEXT
    "    jne janus_dlsym_impl\n"
    "    pushq %rdi\n"
    "    pushq %rsi\n"
    "    subq $8, %rsp\n"                  // 16-byte alignment for the calls
    "    movq %rsi, %rdi\n"
    "    call janus_next_override\n"
    "    testq %rax, %rax\n"
    "    jnz 1f\n"
    "    call janus_real_dlsym_ptr\n"
    "    movq %rax, %r11\n"
    "    addq $8, %rsp\n"
    "    popq %rsi\n"
    "    popq %rdi\n"
    "    testq %r11, %r11\n"
    "    jz 2f\n"
    "    jmp *%r11\n"
    "1:  addq $8, %rsp\n"
    "    popq %rsi\n"
    "    popq %rdi\n"
    "    ret\n"
    "2:  xorl %eax, %eax\n"
    "    ret\n"
    ".size dlsym, .-dlsym\n");
#elif defined(__i386__)
__asm__(
    ".text\n"
    ".globl dlsym\n"
    ".type dlsym, @function\n"
    "dlsym:\n"
    "    cmpl $-1, 4(%esp)\n"              // RTLD_NEXT
    "    jne janus_dlsym_impl\n"
    "    pushl 8(%esp)\n"                  // symbol
    "    call janus_next_override\n"
    "    addl $4, %esp\n"
    "    testl %eax, %eax\n"
    "    jnz 1f\n"
    "    call janus_real_dlsym_ptr\n"
    "    testl %eax, %eax\n"
    "    jz 1f\n"
    "    jmp *%eax\n"
    "1:  ret\n"
    ".size dlsym, .-dlsym\n");
#else
#error "dlsym stub: unsupported architecture"
#endif

void* janus_dlsym_impl(void* handle, const char* symbol) {
    resolve_real_dlsym();
    if (!real_dlsym) {
        return NULL;
    }

    // RTLD_NEXT never gets here (the dlsym stub above sends it to glibc).
    // Don't intercept RTLD_DEFAULT - used by steamclient.so. Intercepting SDL joystick functions
    // for RTLD_DEFAULT causes crashes. SDL_SetHint is handled via visible exports
    // (not dlsym interception) since steamclient.so links SDL directly.
    if (handle == RTLD_DEFAULT) {
        // For dlsym lookups of SDL_SetHint, return our visible export so games
        // that use dlsym also get the wrapper (belt + suspenders with the export)
        if (symbol && getenv("JANUS_CONTROLLER")) {
            if (strcmp(symbol, "SDL_SetHint") == 0) {
                ensure_real_SDL_SetHint();
                VLOG("dlsym: returning SDL_SetHint wrapper");
                return (void*)SDL_SetHint;
            }
            if (strcmp(symbol, "SDL_SetHintWithPriority") == 0) {
                ensure_real_SDL_SetHint();
                VLOG("dlsym: returning SDL_SetHintWithPriority wrapper");
                return (void*)SDL_SetHintWithPriority;
            }
        }
        return real_dlsym(handle, symbol);
    }

    // Steam ID override: wrap steamclient.so's CreateInterface (see above).
    if (symbol && strcmp(symbol, "CreateInterface") == 0 && steamid_override()) {
        void* fn = real_dlsym(handle, symbol);
        Dl_info di;
        if (fn && dladdr(fn, &di) && di.dli_fname && strstr(di.dli_fname, "steamclient")) {
            real_steamclient_CreateInterface = (CreateInterface_fn)fn;
            LOG("SteamID override: wrapping CreateInterface from %s", di.dli_fname);
            return (void*)janus_steamclient_CreateInterface;
        }
        return fn;
    }

    // Goldberg invite fix (see janus_ActivateGameOverlayInviteDialog).
    if (symbol && strcmp(symbol, "SteamAPI_ISteamFriends_ActivateGameOverlayInviteDialog") == 0 &&
        goldberg_invite_fix_enabled()) {
        void* fn = real_dlsym(handle, symbol);
        if (fn) {
            gbe_invite_handle = handle;
            LOG("GoldbergInvite: replacing %s", symbol);
            return (void*)janus_ActivateGameOverlayInviteDialog;
        }
        return fn;
    }

    // Intercept SDL_SetHint for any handle (steamclient.so dlopen's SDL3 then resolves via handle)
    if (symbol && getenv("JANUS_CONTROLLER")) {
        if (strcmp(symbol, "SDL_SetHint") == 0) {
            if (!real_SDL_SetHint_fn)
                real_SDL_SetHint_fn = real_dlsym(handle, symbol);
            VLOG("dlsym: intercepting SDL_SetHint (handle=%p)", handle);
            return (void*)SDL_SetHint;
        }
        if (strcmp(symbol, "SDL_SetHintWithPriority") == 0) {
            if (!real_SDL_SetHintWithPriority_fn)
                real_SDL_SetHintWithPriority_fn = real_dlsym(handle, symbol);
            VLOG("dlsym: intercepting SDL_SetHintWithPriority (handle=%p)", handle);
            return (void*)SDL_SetHintWithPriority;
        }
    }

    // Intercept SDL2/SDL3 joystick/gamepad functions (Source Engine, FNA load SDL dynamically)
    if (symbol) {
        // Debug: log all SDL symbol lookups
        if (strncmp(symbol, "SDL_", 4) == 0) {
            VLOG("dlsym: SDL lookup '%s' handle=%p", symbol, handle);
        }

        // SDL2 joystick/gamepad functions
        // steamclient.so resolves these via RTLD_DEFAULT to inject virtual gamepads
        if (strcmp(symbol, "SDL_NumJoysticks") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_NumJoysticks) {
                real_SDL_NumJoysticks = real_fn;
                LOG("dlsym: intercepted SDL_NumJoysticks -> %p", real_fn);
            }
            return (void*)SDL_NumJoysticks;
        }
        if (strcmp(symbol, "SDL_JoystickOpen") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_JoystickOpen) {
                real_SDL_JoystickOpen = real_fn;
                LOG("dlsym: intercepted SDL_JoystickOpen -> %p", real_fn);
                // Proactively resolve all SDL2 helpers from this handle.
                // Callers like Wine's winebus may not look up all functions we need
                // (e.g., SDL_NumJoysticks, SDL_JoystickGetDeviceVendor) via dlsym,
                // so we resolve them here to enable build_sdl_mapping().
                #define RESOLVE_SDL2(fn_name, var) do { \
                    if (!var) { \
                        void* _fn = real_dlsym(handle, fn_name); \
                        if (_fn) { var = _fn; LOG("dlsym: proactive resolve %s -> %p", fn_name, _fn); } \
                    } \
                } while(0)
                RESOLVE_SDL2("SDL_NumJoysticks", real_SDL_NumJoysticks);
                RESOLVE_SDL2("SDL_JoystickNameForIndex", real_SDL_JoystickNameForIndex);
                RESOLVE_SDL2("SDL_JoystickGetDeviceGUID", real_SDL_JoystickGetDeviceGUID);
                RESOLVE_SDL2("SDL_JoystickGetDeviceVendor", real_SDL_JoystickGetDeviceVendor);
                RESOLVE_SDL2("SDL_JoystickGetDeviceProduct", real_SDL_JoystickGetDeviceProduct);
                RESOLVE_SDL2("SDL_GameControllerOpen", real_SDL_GameControllerOpen);
                RESOLVE_SDL2("SDL_IsGameController", real_SDL_IsGameController);
                #undef RESOLVE_SDL2
            }
            return (void*)SDL_JoystickOpen;
        }
        if (strcmp(symbol, "SDL_GameControllerOpen") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_GameControllerOpen) {
                real_SDL_GameControllerOpen = real_fn;
                LOG("dlsym: intercepted SDL_GameControllerOpen -> %p", real_fn);
            }
            return (void*)SDL_GameControllerOpen;
        }
        if (strcmp(symbol, "SDL_IsGameController") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_IsGameController) {
                real_SDL_IsGameController = real_fn;
                LOG("dlsym: intercepted SDL_IsGameController -> %p", real_fn);
            }
            return (void*)SDL_IsGameController;
        }
        if (strcmp(symbol, "SDL_JoystickNameForIndex") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_JoystickNameForIndex) {
                real_SDL_JoystickNameForIndex = real_fn;
            }
            return (void*)SDL_JoystickNameForIndex;
        }
        if (strcmp(symbol, "SDL_JoystickGetDeviceGUID") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_JoystickGetDeviceGUID) {
                real_SDL_JoystickGetDeviceGUID = real_fn;
            }
            return (void*)SDL_JoystickGetDeviceGUID;
        }
        // SDL2 helper functions - capture real pointers (needed for VID:PID mapping)
        if (strcmp(symbol, "SDL_JoystickGetDeviceVendor") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_JoystickGetDeviceVendor) {
                real_SDL_JoystickGetDeviceVendor = real_fn;
            }
            return real_fn;  // Don't hook, just capture
        }
        if (strcmp(symbol, "SDL_JoystickGetDeviceProduct") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_JoystickGetDeviceProduct) {
                real_SDL_JoystickGetDeviceProduct = real_fn;
            }
            return real_fn;  // Don't hook, just capture
        }

        // SDL3 joystick/gamepad functions (FNA loads SDL3 dynamically)
        if (strcmp(symbol, "SDL_GetJoysticks") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn) {
                if (!real_SDL_GetJoysticks) real_SDL_GetJoysticks = real_fn;
                LOG("dlsym: intercepted SDL_GetJoysticks -> %p", real_fn);
                // The SDL3 wrappers need these too, and a caller may never look
                // them up itself (sdl2-compat) or not through this handle.
                #define RESOLVE_SDL3(fn_name, var) do { \
                    if (!var) { \
                        void* _fn = real_dlsym(handle, fn_name); \
                        if (_fn) { var = _fn; LOG("dlsym: proactive resolve %s -> %p", fn_name, _fn); } \
                    } \
                } while(0)
                RESOLVE_SDL3("SDL_GetGamepads", real_SDL_GetGamepads);
                RESOLVE_SDL3("SDL_OpenJoystick", real_SDL_OpenJoystick);
                RESOLVE_SDL3("SDL_OpenGamepad", real_SDL_OpenGamepad);
                RESOLVE_SDL3("SDL_GetJoystickNameForID", real_SDL_GetJoystickNameForID);
                RESOLVE_SDL3("SDL_GetJoystickVendorForID", real_SDL_GetJoystickVendorForID);
                RESOLVE_SDL3("SDL_GetJoystickProductForID", real_SDL_GetJoystickProductForID);
                RESOLVE_SDL3("SDL_free", real_SDL_free);
                #undef RESOLVE_SDL3
            }
            return (void*)SDL_GetJoysticks;
        }
        if (strcmp(symbol, "SDL_GetGamepads") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn) {
                if (!real_SDL_GetGamepads) real_SDL_GetGamepads = real_fn;
                LOG("dlsym: intercepted SDL_GetGamepads -> %p", real_fn);
            }
            return (void*)SDL_GetGamepads;
        }
        if (strcmp(symbol, "SDL_OpenJoystick") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_OpenJoystick) {
                real_SDL_OpenJoystick = real_fn;
                LOG("dlsym: intercepted SDL_OpenJoystick -> %p", real_fn);
            }
            return (void*)SDL_OpenJoystick;
        }
        if (strcmp(symbol, "SDL_OpenGamepad") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_OpenGamepad) {
                real_SDL_OpenGamepad = real_fn;
                LOG("dlsym: intercepted SDL_OpenGamepad -> %p", real_fn);
            }
            return (void*)SDL_OpenGamepad;
        }
        // Also intercept helper functions needed for mapping
        if (strcmp(symbol, "SDL_GetJoystickNameForID") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_GetJoystickNameForID) {
                real_SDL_GetJoystickNameForID = real_fn;
            }
            return real_fn;  // Don't hook, just capture
        }
        if (strcmp(symbol, "SDL_GetJoystickVendorForID") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_GetJoystickVendorForID) {
                real_SDL_GetJoystickVendorForID = real_fn;
            }
            return real_fn;
        }
        if (strcmp(symbol, "SDL_GetJoystickProductForID") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_GetJoystickProductForID) {
                real_SDL_GetJoystickProductForID = real_fn;
            }
            return real_fn;
        }
        if (strcmp(symbol, "SDL_free") == 0) {
            void* real_fn = real_dlsym(handle, symbol);
            if (real_fn && !real_SDL_free) {
                real_SDL_free = real_fn;
            }
            return real_fn;
        }
    }

    return real_dlsym(handle, symbol);
}

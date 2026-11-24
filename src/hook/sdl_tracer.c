// SDL2 + evdev diagnostic tracer
// Standalone LD_PRELOAD library that logs all SDL2 device-index function calls
// and evdev device opens. For diagnosing controller mapping issues.
//
// Build: cd src/hook && make tracer
// Usage: LD_PRELOAD=build/libsdl_tracer32.so <game>

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdarg.h>
#include <errno.h>
#include <linux/joystick.h>
#include <linux/input.h>
#include <sys/ioctl.h>

#define TRACE(fmt, ...) fprintf(stderr, "[SDL_TRACE] " fmt "\n", ##__VA_ARGS__)

// =============================================================================
// Real function pointers
// =============================================================================

// evdev
static int (*real_open)(const char*, int, ...) = NULL;
static int (*real_openat)(int, const char*, int, ...) = NULL;

// dlsym
static void* (*real_dlsym)(void*, const char*) = NULL;

// SDL2 types
typedef struct _SDL_Joystick SDL_Joystick;
typedef struct _SDL_GameController SDL_GameController;
typedef struct { unsigned char data[16]; } SDL_JoystickGUID;
typedef enum {
    SDL_JOYSTICK_TYPE_UNKNOWN,
    SDL_JOYSTICK_TYPE_GAMECONTROLLER,
    SDL_JOYSTICK_TYPE_WHEEL,
    SDL_JOYSTICK_TYPE_ARCADE_STICK,
    SDL_JOYSTICK_TYPE_FLIGHT_STICK,
    SDL_JOYSTICK_TYPE_DANCE_PAD,
    SDL_JOYSTICK_TYPE_GUITAR,
    SDL_JOYSTICK_TYPE_DRUM_KIT,
    SDL_JOYSTICK_TYPE_ARCADE_PAD,
    SDL_JOYSTICK_TYPE_THROTTLE
} SDL_JoystickType;
typedef enum {
    SDL_CONTROLLER_TYPE_UNKNOWN = 0,
    SDL_CONTROLLER_TYPE_XBOX360,
    SDL_CONTROLLER_TYPE_XBOXONE,
    SDL_CONTROLLER_TYPE_PS3,
    SDL_CONTROLLER_TYPE_PS4,
    SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_PRO,
    SDL_CONTROLLER_TYPE_VIRTUAL,
    SDL_CONTROLLER_TYPE_PS5,
    SDL_CONTROLLER_TYPE_AMAZON_LUNA,
    SDL_CONTROLLER_TYPE_GOOGLE_STADIA,
    SDL_CONTROLLER_TYPE_NVIDIA_SHIELD,
    SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_LEFT,
    SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_RIGHT,
    SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_PAIR
} SDL_GameControllerType;

// SDL2 function pointers (resolved lazily from real SDL2)
static int (*sdl_NumJoysticks)(void) = NULL;
static SDL_Joystick* (*sdl_JoystickOpen)(int) = NULL;
static SDL_GameController* (*sdl_GameControllerOpen)(int) = NULL;
static int (*sdl_IsGameController)(int) = NULL;
static const char* (*sdl_JoystickNameForIndex)(int) = NULL;
static SDL_JoystickGUID (*sdl_JoystickGetDeviceGUID)(int) = NULL;
static unsigned short (*sdl_JoystickGetDeviceVendor)(int) = NULL;
static unsigned short (*sdl_JoystickGetDeviceProduct)(int) = NULL;
static SDL_JoystickType (*sdl_JoystickGetDeviceType)(int) = NULL;
static int (*sdl_JoystickGetDevicePlayerIndex)(int) = NULL;
static unsigned short (*sdl_JoystickGetDeviceProductVersion)(int) = NULL;
static SDL_GameControllerType (*sdl_GameControllerTypeForIndex)(int) = NULL;
static char* (*sdl_GameControllerMappingForDeviceIndex)(int) = NULL;
static const char* (*sdl_GameControllerNameForIndex)(int) = NULL;
static SDL_GameControllerType (*sdl_GameControllerGetType)(SDL_GameController*) = NULL;
static char* (*sdl_GameControllerMapping)(SDL_GameController*) = NULL;
static SDL_JoystickGUID (*sdl_JoystickGetGUID)(SDL_Joystick*) = NULL;
static const char* (*sdl_JoystickName)(SDL_Joystick*) = NULL;
static int (*sdl_JoystickNumAxes)(SDL_Joystick*) = NULL;
static int (*sdl_JoystickNumButtons)(SDL_Joystick*) = NULL;
static int (*sdl_JoystickNumHats)(SDL_Joystick*) = NULL;
static void (*sdl_JoystickGetGUIDString)(SDL_JoystickGUID, char*, int) = NULL;
static int (*sdl_SetHint)(const char*, const char*) = NULL;
static const char* (*sdl_GetHint)(const char*) = NULL;
static void (*sdl_free)(void*) = NULL;
static void (*sdl_JoystickClose)(SDL_Joystick*) = NULL;
static void (*sdl_GameControllerClose)(SDL_GameController*) = NULL;

static int sdl2_resolved = 0;

static const char* controller_type_name(SDL_GameControllerType t) {
    switch (t) {
        case SDL_CONTROLLER_TYPE_UNKNOWN: return "Unknown";
        case SDL_CONTROLLER_TYPE_XBOX360: return "Xbox360";
        case SDL_CONTROLLER_TYPE_XBOXONE: return "XboxOne";
        case SDL_CONTROLLER_TYPE_PS3: return "PS3";
        case SDL_CONTROLLER_TYPE_PS4: return "PS4";
        case SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_PRO: return "NintendoSwitchPro";
        case SDL_CONTROLLER_TYPE_VIRTUAL: return "Virtual";
        case SDL_CONTROLLER_TYPE_PS5: return "PS5";
        case SDL_CONTROLLER_TYPE_AMAZON_LUNA: return "AmazonLuna";
        case SDL_CONTROLLER_TYPE_GOOGLE_STADIA: return "GoogleStadia";
        case SDL_CONTROLLER_TYPE_NVIDIA_SHIELD: return "NvidiaShield";
        case SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_LEFT: return "JoyConLeft";
        case SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_RIGHT: return "JoyConRight";
        case SDL_CONTROLLER_TYPE_NINTENDO_SWITCH_JOYCON_PAIR: return "JoyConPair";
        default: return "?";
    }
}

static const char* joystick_type_name(SDL_JoystickType t) {
    switch (t) {
        case SDL_JOYSTICK_TYPE_UNKNOWN: return "Unknown";
        case SDL_JOYSTICK_TYPE_GAMECONTROLLER: return "GameController";
        case SDL_JOYSTICK_TYPE_WHEEL: return "Wheel";
        case SDL_JOYSTICK_TYPE_ARCADE_STICK: return "ArcadeStick";
        case SDL_JOYSTICK_TYPE_FLIGHT_STICK: return "FlightStick";
        case SDL_JOYSTICK_TYPE_DANCE_PAD: return "DancePad";
        case SDL_JOYSTICK_TYPE_GUITAR: return "Guitar";
        case SDL_JOYSTICK_TYPE_DRUM_KIT: return "DrumKit";
        case SDL_JOYSTICK_TYPE_ARCADE_PAD: return "ArcadePad";
        case SDL_JOYSTICK_TYPE_THROTTLE: return "Throttle";
        default: return "?";
    }
}

// =============================================================================
// Resolve real functions
// =============================================================================

static void resolve_sdl2(void) {
    if (sdl2_resolved) return;

    void* sdl = dlopen("libSDL2-2.0.so.0", RTLD_NOLOAD | RTLD_NOW);
    if (!sdl) {
        // Try without NOLOAD — maybe SDL2 hasn't been loaded yet
        return;
    }

    TRACE("Resolving SDL2 functions from %p", sdl);

    #define RESOLVE(name) do { \
        sdl_##name = dlsym(sdl, "SDL_" #name); \
        if (sdl_##name) TRACE("  SDL_" #name " = %p", (void*)sdl_##name); \
    } while(0)

    // Can't use RESOLVE macro for names that differ
    sdl_NumJoysticks = dlsym(sdl, "SDL_NumJoysticks");
    sdl_JoystickOpen = dlsym(sdl, "SDL_JoystickOpen");
    sdl_GameControllerOpen = dlsym(sdl, "SDL_GameControllerOpen");
    sdl_IsGameController = dlsym(sdl, "SDL_IsGameController");
    sdl_JoystickNameForIndex = dlsym(sdl, "SDL_JoystickNameForIndex");
    sdl_JoystickGetDeviceGUID = dlsym(sdl, "SDL_JoystickGetDeviceGUID");
    sdl_JoystickGetDeviceVendor = dlsym(sdl, "SDL_JoystickGetDeviceVendor");
    sdl_JoystickGetDeviceProduct = dlsym(sdl, "SDL_JoystickGetDeviceProduct");
    sdl_JoystickGetDeviceType = dlsym(sdl, "SDL_JoystickGetDeviceType");
    sdl_JoystickGetDevicePlayerIndex = dlsym(sdl, "SDL_JoystickGetDevicePlayerIndex");
    sdl_JoystickGetDeviceProductVersion = dlsym(sdl, "SDL_JoystickGetDeviceProductVersion");
    sdl_GameControllerTypeForIndex = dlsym(sdl, "SDL_GameControllerTypeForIndex");
    sdl_GameControllerMappingForDeviceIndex = dlsym(sdl, "SDL_GameControllerMappingForDeviceIndex");
    sdl_GameControllerNameForIndex = dlsym(sdl, "SDL_GameControllerNameForIndex");
    sdl_GameControllerGetType = dlsym(sdl, "SDL_GameControllerGetType");
    sdl_GameControllerMapping = dlsym(sdl, "SDL_GameControllerMapping");
    sdl_JoystickGetGUID = dlsym(sdl, "SDL_JoystickGetGUID");
    sdl_JoystickName = dlsym(sdl, "SDL_JoystickName");
    sdl_JoystickNumAxes = dlsym(sdl, "SDL_JoystickNumAxes");
    sdl_JoystickNumButtons = dlsym(sdl, "SDL_JoystickNumButtons");
    sdl_JoystickNumHats = dlsym(sdl, "SDL_JoystickNumHats");
    sdl_JoystickGetGUIDString = dlsym(sdl, "SDL_JoystickGetGUIDString");
    sdl_SetHint = dlsym(sdl, "SDL_SetHint");
    sdl_GetHint = dlsym(sdl, "SDL_GetHint");
    sdl_free = dlsym(sdl, "SDL_free");
    sdl_JoystickClose = dlsym(sdl, "SDL_JoystickClose");
    sdl_GameControllerClose = dlsym(sdl, "SDL_GameControllerClose");

    #undef RESOLVE

    sdl2_resolved = 1;
    TRACE("SDL2 resolved: NumJoysticks=%p, JoystickOpen=%p, GameControllerOpen=%p",
          (void*)sdl_NumJoysticks, (void*)sdl_JoystickOpen, (void*)sdl_GameControllerOpen);

    dlclose(sdl);
}

// =============================================================================
// Log device metadata from fd
// =============================================================================

static void log_js_metadata(const char* path, int fd) {
    char name[256] = {0};
    if (ioctl(fd, JSIOCGNAME(sizeof(name)), name) >= 0) {
        TRACE("  js metadata: name='%s'", name);
    }
    unsigned char axes = 0, buttons = 0;
    if (ioctl(fd, JSIOCGAXES, &axes) >= 0 && ioctl(fd, JSIOCGBUTTONS, &buttons) >= 0) {
        TRACE("  js metadata: axes=%d buttons=%d", axes, buttons);
    }
    (void)path;
}

static void log_event_metadata(const char* path, int fd) {
    char name[256] = {0};
    if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0) {
        TRACE("  event metadata: name='%s'", name);
    }
    struct input_id id;
    if (ioctl(fd, EVIOCGID, &id) >= 0) {
        TRACE("  event metadata: bus=%04x vendor=%04x product=%04x version=%04x",
              id.bustype, id.vendor, id.product, id.version);
    }
    (void)path;
}

// =============================================================================
// evdev open hooks
// =============================================================================

static void ensure_real_open(void) {
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (!real_openat) real_openat = dlsym(RTLD_NEXT, "openat");
}

int open(const char* pathname, int flags, ...) {
    ensure_real_open();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    int fd = real_open(pathname, flags, mode);

    if (pathname && strncmp(pathname, "/dev/input/", 11) == 0) {
        TRACE("open('%s', 0x%x) = %d%s", pathname, flags, fd,
              fd < 0 ? " (FAILED)" : "");

        if (fd >= 0) {
            if (strncmp(pathname + 11, "js", 2) == 0) {
                log_js_metadata(pathname, fd);
            } else if (strncmp(pathname + 11, "event", 5) == 0) {
                log_event_metadata(pathname, fd);
            }
        }
    }

    if (pathname && strncmp(pathname, "/dev/hidraw", 11) == 0) {
        TRACE("open('%s', 0x%x) = %d%s", pathname, flags, fd,
              fd < 0 ? " (FAILED)" : "");
    }

    return fd;
}

int open64(const char* pathname, int flags, ...) {
    ensure_real_open();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    // open64 is the same as open with O_LARGEFILE on modern systems
    return open(pathname, flags | O_LARGEFILE, mode);
}

int openat(int dirfd, const char* pathname, int flags, ...) {
    ensure_real_open();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    int fd = real_openat(dirfd, pathname, flags, mode);

    if (pathname && strncmp(pathname, "/dev/input/", 11) == 0) {
        TRACE("openat(%d, '%s', 0x%x) = %d%s", dirfd, pathname, flags, fd,
              fd < 0 ? " (FAILED)" : "");

        if (fd >= 0) {
            if (strncmp(pathname + 11, "js", 2) == 0) {
                log_js_metadata(pathname, fd);
            } else if (strncmp(pathname + 11, "event", 5) == 0) {
                log_event_metadata(pathname, fd);
            }
        }
    }

    return fd;
}

int openat64(int dirfd, const char* pathname, int flags, ...) {
    ensure_real_open();

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }

    return openat(dirfd, pathname, flags | O_LARGEFILE, mode);
}

// =============================================================================
// SDL2 wrapper hooks (visible exports + dlsym interception)
// =============================================================================

int SDL_NumJoysticks(void) {
    resolve_sdl2();
    if (!sdl_NumJoysticks) { TRACE("SDL_NumJoysticks() -> (no real func)"); return 0; }

    int result = sdl_NumJoysticks();
    TRACE("SDL_NumJoysticks() = %d", result);

    // Log details for each joystick
    for (int i = 0; i < result; i++) {
        const char* name = sdl_JoystickNameForIndex ? sdl_JoystickNameForIndex(i) : NULL;
        unsigned short vid = sdl_JoystickGetDeviceVendor ? sdl_JoystickGetDeviceVendor(i) : 0;
        unsigned short pid = sdl_JoystickGetDeviceProduct ? sdl_JoystickGetDeviceProduct(i) : 0;
        int is_gc = sdl_IsGameController ? sdl_IsGameController(i) : -1;

        TRACE("  [%d] name='%s' VID:PID=%04x:%04x isGameController=%d",
              i, name ? name : "(null)", vid, pid, is_gc);

        if (sdl_JoystickGetDeviceGUID && sdl_JoystickGetGUIDString) {
            SDL_JoystickGUID guid = sdl_JoystickGetDeviceGUID(i);
            char guid_str[33];
            sdl_JoystickGetGUIDString(guid, guid_str, sizeof(guid_str));
            TRACE("  [%d] GUID=%s", i, guid_str);
        }

        if (sdl_GameControllerTypeForIndex) {
            SDL_GameControllerType type = sdl_GameControllerTypeForIndex(i);
            TRACE("  [%d] controllerType=%d (%s)", i, type, controller_type_name(type));
        }

        if (sdl_JoystickGetDeviceType) {
            SDL_JoystickType type = sdl_JoystickGetDeviceType(i);
            TRACE("  [%d] joystickType=%d (%s)", i, type, joystick_type_name(type));
        }

        if (sdl_GameControllerMappingForDeviceIndex) {
            char* mapping = sdl_GameControllerMappingForDeviceIndex(i);
            if (mapping) {
                TRACE("  [%d] mapping='%.200s%s'", i, mapping,
                      strlen(mapping) > 200 ? "..." : "");
                if (sdl_free) sdl_free(mapping);
            }
        }

        if (sdl_GameControllerNameForIndex) {
            const char* gc_name = sdl_GameControllerNameForIndex(i);
            TRACE("  [%d] gcName='%s'", i, gc_name ? gc_name : "(null)");
        }
    }

    return result;
}

SDL_Joystick* SDL_JoystickOpen(int device_index) {
    resolve_sdl2();
    if (!sdl_JoystickOpen) { TRACE("SDL_JoystickOpen(%d) -> (no real func)", device_index); return NULL; }

    SDL_Joystick* result = sdl_JoystickOpen(device_index);
    TRACE("SDL_JoystickOpen(%d) = %p", device_index, (void*)result);

    if (result) {
        const char* name = sdl_JoystickName ? sdl_JoystickName(result) : NULL;
        TRACE("  opened name='%s'", name ? name : "(null)");

        if (sdl_JoystickNumAxes && sdl_JoystickNumButtons && sdl_JoystickNumHats) {
            TRACE("  axes=%d buttons=%d hats=%d",
                  sdl_JoystickNumAxes(result), sdl_JoystickNumButtons(result),
                  sdl_JoystickNumHats(result));
        }

        if (sdl_JoystickGetGUID && sdl_JoystickGetGUIDString) {
            SDL_JoystickGUID guid = sdl_JoystickGetGUID(result);
            char guid_str[33];
            sdl_JoystickGetGUIDString(guid, guid_str, sizeof(guid_str));
            TRACE("  instance GUID=%s", guid_str);
        }
    }

    return result;
}

SDL_GameController* SDL_GameControllerOpen(int joystick_index) {
    resolve_sdl2();
    if (!sdl_GameControllerOpen) { TRACE("SDL_GameControllerOpen(%d) -> (no real func)", joystick_index); return NULL; }

    SDL_GameController* result = sdl_GameControllerOpen(joystick_index);
    TRACE("SDL_GameControllerOpen(%d) = %p", joystick_index, (void*)result);

    if (result) {
        if (sdl_GameControllerGetType) {
            SDL_GameControllerType type = sdl_GameControllerGetType(result);
            TRACE("  type=%d (%s)", type, controller_type_name(type));
        }

        if (sdl_GameControllerMapping) {
            char* mapping = sdl_GameControllerMapping(result);
            if (mapping) {
                TRACE("  mapping='%.200s%s'", mapping,
                      strlen(mapping) > 200 ? "..." : "");
                if (sdl_free) sdl_free(mapping);
            }
        }
    }

    return result;
}

int SDL_IsGameController(int joystick_index) {
    resolve_sdl2();
    if (!sdl_IsGameController) { TRACE("SDL_IsGameController(%d) -> (no real func)", joystick_index); return 0; }

    int result = sdl_IsGameController(joystick_index);
    TRACE("SDL_IsGameController(%d) = %d", joystick_index, result);
    return result;
}

int SDL_SetHint(const char* name, const char* value) {
    resolve_sdl2();
    TRACE("SDL_SetHint('%s', '%s')", name ? name : "(null)", value ? value : "(null)");
    if (!sdl_SetHint) return 0;
    return sdl_SetHint(name, value);
}

// =============================================================================
// dlsym hook - intercept SDL2 lookups
// =============================================================================

// Declare glibc internal functions
extern void* __libc_dlsym(void*, const char*);
extern void* __libc_dlopen_mode(const char*, int);

static void ensure_real_dlsym(void) {
    if (!real_dlsym) {
        // Bootstrap: use libc's internal dlsym
        real_dlsym = __libc_dlsym(__libc_dlopen_mode("libdl.so.2", RTLD_LAZY | RTLD_NOLOAD), "dlsym");
        if (!real_dlsym) {
            // Fallback for newer glibc where libdl is integrated into libc
            real_dlsym = __libc_dlsym(__libc_dlopen_mode("libc.so.6", RTLD_LAZY | RTLD_NOLOAD), "dlsym");
        }
    }
}

void* dlsym(void* handle, const char* symbol) {
    ensure_real_dlsym();

    if (!symbol) return real_dlsym(handle, symbol);

    // Log SDL symbol lookups
    if (strncmp(symbol, "SDL_", 4) == 0) {
        void* real_fn = real_dlsym(handle, symbol);
        TRACE("dlsym(%p, '%s') = %p", handle, symbol, real_fn);

        // Intercept device-index functions to trace them
        if (strcmp(symbol, "SDL_NumJoysticks") == 0) {
            if (real_fn) sdl_NumJoysticks = real_fn;
            return (void*)SDL_NumJoysticks;
        }
        if (strcmp(symbol, "SDL_JoystickOpen") == 0) {
            if (real_fn) sdl_JoystickOpen = real_fn;
            return (void*)SDL_JoystickOpen;
        }
        if (strcmp(symbol, "SDL_GameControllerOpen") == 0) {
            if (real_fn) sdl_GameControllerOpen = real_fn;
            return (void*)SDL_GameControllerOpen;
        }
        if (strcmp(symbol, "SDL_IsGameController") == 0) {
            if (real_fn) sdl_IsGameController = real_fn;
            return (void*)SDL_IsGameController;
        }
        if (strcmp(symbol, "SDL_SetHint") == 0) {
            if (real_fn) sdl_SetHint = real_fn;
            return (void*)SDL_SetHint;
        }

        // Capture but pass through other SDL2 functions
        #define CAPTURE(func_name, var_name) \
            if (strcmp(symbol, "SDL_" #func_name) == 0) { \
                if (real_fn) sdl_##var_name = real_fn; \
                return real_fn; \
            }

        CAPTURE(JoystickNameForIndex, JoystickNameForIndex)
        CAPTURE(JoystickGetDeviceGUID, JoystickGetDeviceGUID)
        CAPTURE(JoystickGetDeviceVendor, JoystickGetDeviceVendor)
        CAPTURE(JoystickGetDeviceProduct, JoystickGetDeviceProduct)
        CAPTURE(JoystickGetDeviceType, JoystickGetDeviceType)
        CAPTURE(JoystickGetDevicePlayerIndex, JoystickGetDevicePlayerIndex)
        CAPTURE(JoystickGetDeviceProductVersion, JoystickGetDeviceProductVersion)
        CAPTURE(GameControllerTypeForIndex, GameControllerTypeForIndex)
        CAPTURE(GameControllerMappingForDeviceIndex, GameControllerMappingForDeviceIndex)
        CAPTURE(GameControllerNameForIndex, GameControllerNameForIndex)
        CAPTURE(GameControllerGetType, GameControllerGetType)
        CAPTURE(GameControllerMapping, GameControllerMapping)
        CAPTURE(JoystickGetGUID, JoystickGetGUID)
        CAPTURE(JoystickName, JoystickName)
        CAPTURE(JoystickNumAxes, JoystickNumAxes)
        CAPTURE(JoystickNumButtons, JoystickNumButtons)
        CAPTURE(JoystickNumHats, JoystickNumHats)
        CAPTURE(JoystickGetGUIDString, JoystickGetGUIDString)
        CAPTURE(GetHint, GetHint)
        CAPTURE(free, free)
        CAPTURE(JoystickClose, JoystickClose)
        CAPTURE(GameControllerClose, GameControllerClose)

        #undef CAPTURE

        return real_fn;
    }

    return real_dlsym(handle, symbol);
}

// =============================================================================
// Constructor
// =============================================================================

__attribute__((constructor))
static void tracer_init(void) {
    ensure_real_open();
    ensure_real_dlsym();
    TRACE("=== SDL/evdev tracer loaded (pid=%d) ===", getpid());
}

// Preloaded into gamescope (not the game): show the desktop arrow over a tile
// that doesn't have the mouse.
//
// gamescope's SDL backend draws the nested game's cursor on its host window
// (SDL_CreateColorCursor + SDL_SetCursor) and switches to relative mouse mode
// while the game hides its cursor (mouse-look). A game in mouse-look sets an
// invisible cursor, so with one mouse shared by several tiles the pointer
// vanished over every tile that didn't have it, and relative mode could keep
// a tile holding the pointer. Here, while gamescope's window doesn't have
// keyboard focus, relative mode stays off and an invisible game cursor is
// shown as the system arrow; on focus gain gamescope's own requests apply
// again. gamescope's code is untouched.
//
// The library removes itself from LD_PRELOAD in its constructor, so the game,
// the runtime and gamescope's other children never load it: exporting SDL
// functions into games breaks those that link SDL directly.
// See NOTES.md "Invisible cursor over tiles without the mouse".
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// The few SDL2 types used (no SDL headers needed). Layouts from SDL2's
// SDL_pixels.h / SDL_surface.h / SDL_events.h; stable across SDL 2.x.
typedef struct SDL_Cursor SDL_Cursor;
typedef struct {
    uint32_t format;
    void *palette;
    uint8_t BitsPerPixel, BytesPerPixel, padding[2];
    uint32_t Rmask, Gmask, Bmask, Amask;
} PixelFormat;
typedef struct {
    uint32_t flags;
    PixelFormat *format;
    int w, h, pitch;
    void *pixels;
} Surface;
typedef struct {
    uint32_t type, timestamp, windowID;
    uint8_t event;
} WindowEvent;

typedef struct {
    uint32_t type, timestamp, windowID, which;
    uint8_t button, state, clicks, padding1;
    int32_t x, y;
} MouseButtonEvent;

#define SDL_WINDOWEVENT 0x200
#define SDL_MOUSEBUTTONDOWN 0x401
#define SDL_WINDOWEVENT_FOCUS_GAINED 12
#define SDL_WINDOWEVENT_FOCUS_LOST 13
#define SDL_SYSTEM_CURSOR_ARROW 0

static SDL_Cursor *(*real_CreateColorCursor)(Surface *, int, int);
static void (*real_SetCursor)(SDL_Cursor *);
static void (*real_FreeCursor)(SDL_Cursor *);
static int (*real_SetRelativeMouseMode)(int);
static int (*real_WaitEvent)(void *);

static int active;              // this process is gamescope
static int verbose;
static int focused = -1;        // -1 = not known yet
static int want_relative;       // what gamescope asked for
static SDL_Cursor *game_cursor; // what gamescope last set
static SDL_Cursor *arrow;
static int unfocused_since_click = 1;  // next click gives the tile the mouse

// Invisible cursors created by gamescope (it keeps only a few alive).
#define MAX_BLANK 32
static SDL_Cursor *blank[MAX_BLANK];

#define LOG(...) do { if (verbose) { fprintf(stderr, "[janus-gamescope] " __VA_ARGS__); fputc('\n', stderr); } } while (0)

static void *sdl_sym(const char *name)
{
    void *p = dlsym(RTLD_NEXT, name);
    return p ? p : dlsym(RTLD_DEFAULT, name);
}

static void resolve(void)
{
    if (real_SetCursor) return;
    real_CreateColorCursor = sdl_sym("SDL_CreateColorCursor");
    real_SetCursor = sdl_sym("SDL_SetCursor");
    real_FreeCursor = sdl_sym("SDL_FreeCursor");
    real_SetRelativeMouseMode = sdl_sym("SDL_SetRelativeMouseMode");
    real_WaitEvent = sdl_sym("SDL_WaitEvent");
}

static int is_blank(SDL_Cursor *c)
{
    for (int i = 0; i < MAX_BLANK; i++)
        if (c && blank[i] == c) return 1;
    return 0;
}

static int surface_invisible(const Surface *s)
{
    if (!s || !s->pixels || !s->format) return 0;
    if (s->w <= 1 && s->h <= 1 && s->format->BytesPerPixel != 4) return 1;
    if (s->format->BytesPerPixel != 4 || !s->format->Amask) return 0;
    for (int y = 0; y < s->h; y++) {
        const uint32_t *row = (const uint32_t *)((const char *)s->pixels + (size_t)y * s->pitch);
        for (int x = 0; x < s->w; x++)
            if (row[x] & s->format->Amask) return 0;
    }
    return 1;
}

static void apply(void)
{
    if (!real_SetCursor || !real_SetRelativeMouseMode) return;
    if (focused) {
        real_SetRelativeMouseMode(want_relative);
        if (game_cursor) real_SetCursor(game_cursor);
        return;
    }
    real_SetRelativeMouseMode(0);
    if (!arrow) {
        SDL_Cursor *(*create_system)(int) = sdl_sym("SDL_CreateSystemCursor");
        if (create_system) arrow = create_system(SDL_SYSTEM_CURSOR_ARROW);
    }
    if (arrow && (want_relative || !game_cursor || is_blank(game_cursor)))
        real_SetCursor(arrow);
    else if (game_cursor)
        real_SetCursor(game_cursor);
}

static int focus_watch(void *ud, void *event)
{
    (void)ud;
    const WindowEvent *e = event;
    if (e->type != SDL_WINDOWEVENT) return 0;
    if (e->event == SDL_WINDOWEVENT_FOCUS_GAINED || e->event == SDL_WINDOWEVENT_FOCUS_LOST) {
        focused = e->event == SDL_WINDOWEVENT_FOCUS_GAINED;
        if (!focused) unfocused_since_click = 1;
        LOG("focus %s", focused ? "gained" : "lost");
        apply();
    }
    return 0;
}

// First call from gamescope's SDL thread: install the focus watch there.
static void init_once(void)
{
    static int done;
    if (done) return;
    done = 1;
    resolve();
    void (*add_watch)(int (*)(void *, void *), void *) = sdl_sym("SDL_AddEventWatch");
    void *(*kb_focus)(void) = sdl_sym("SDL_GetKeyboardFocus");
    if (add_watch) add_watch(focus_watch, NULL);
    focused = kb_focus ? kb_focus() != NULL : 1;
    LOG("active, focused=%d", focused);
}

SDL_Cursor *SDL_CreateColorCursor(Surface *surface, int hot_x, int hot_y)
{
    resolve();
    SDL_Cursor *c = real_CreateColorCursor ? real_CreateColorCursor(surface, hot_x, hot_y) : NULL;
    if (active && c && surface_invisible(surface)) {
        for (int i = 0; i < MAX_BLANK; i++)
            if (!blank[i]) { blank[i] = c; break; }
    }
    return c;
}

void SDL_FreeCursor(SDL_Cursor *cursor)
{
    resolve();
    for (int i = 0; i < MAX_BLANK; i++)
        if (blank[i] == cursor) blank[i] = NULL;
    if (cursor && cursor == game_cursor) game_cursor = NULL;
    if (real_FreeCursor) real_FreeCursor(cursor);
}

void SDL_SetCursor(SDL_Cursor *cursor)
{
    resolve();
    if (!active) { if (real_SetCursor) real_SetCursor(cursor); return; }
    init_once();
    if (cursor) game_cursor = cursor;
    if (focused || !cursor) { real_SetCursor(cursor); return; }
    apply();
}

int SDL_SetRelativeMouseMode(int enabled)
{
    resolve();
    if (!active) return real_SetRelativeMouseMode ? real_SetRelativeMouseMode(enabled) : -1;
    init_once();
    want_relative = enabled;
    if (focused) return real_SetRelativeMouseMode(enabled);
    apply();
    return 0;
}

// --- Click into a tile that didn't have the mouse --------------------------
// While unfocused, gamescope doesn't pass hover motion to a game that hides its
// cursor, so the game's pointer stays where it was left: the click that gives
// the tile the mouse landed there, not under the arrow. Before gamescope sees
// that click, warp the game's pointer (in gamescope's nested X display, named
// in gamescope's DISPLAY once it has set it for its child) to the click.

typedef struct _XDisplay XDisplay;
static XDisplay *nested;
static XDisplay *(*x_open)(const char *);
static int (*x_warp)(XDisplay *, unsigned long, unsigned long, int, int,
                     unsigned, unsigned, int, int);
static unsigned long (*x_root)(XDisplay *);
static int (*x_default_screen)(XDisplay *);
static int (*x_width)(XDisplay *, int), (*x_height)(XDisplay *, int);
static int (*x_sync)(XDisplay *, int);

static XDisplay *nested_display(void)
{
    static int tried;
    if (nested || tried) return nested;
    const char *name = getenv("DISPLAY");
    if (!name || !*name) return NULL;
    tried = 1;
    void *x11 = dlopen("libX11.so.6", RTLD_NOW | RTLD_LOCAL);
    if (!x11) return NULL;
    x_open = dlsym(x11, "XOpenDisplay");
    x_warp = dlsym(x11, "XWarpPointer");
    x_root = dlsym(x11, "XDefaultRootWindow");
    x_default_screen = dlsym(x11, "XDefaultScreen");
    x_width = dlsym(x11, "XDisplayWidth");
    x_height = dlsym(x11, "XDisplayHeight");
    x_sync = dlsym(x11, "XSync");
    if (!x_open || !x_warp || !x_root || !x_default_screen || !x_width || !x_height || !x_sync)
        return NULL;
    nested = x_open(name);
    LOG("nested display %s %s", name, nested ? "opened" : "failed");
    return nested;
}

static void warp_nested_to(uint32_t window_id, int x, int y)
{
    XDisplay *d = nested_display();
    if (!d) return;
    void *(*get_window)(uint32_t) = sdl_sym("SDL_GetWindowFromID");
    void (*get_size)(void *, int *, int *) = sdl_sym("SDL_GetWindowSize");
    int ww = 0, wh = 0;
    void *win = get_window ? get_window(window_id) : NULL;
    if (win && get_size) get_size(win, &ww, &wh);
    int scr = x_default_screen(d);
    int nw = x_width(d, scr), nh = x_height(d, scr);
    if (ww > 0 && wh > 0) {
        x = (int)((long)x * nw / ww);
        y = (int)((long)y * nh / wh);
    }
    x_warp(d, 0, x_root(d), 0, 0, 0, 0, x, y);
    x_sync(d, 0);
    LOG("click into tile: game pointer -> %d,%d", x, y);
}


int SDL_WaitEvent(void *event)
{
    resolve();
    int r = real_WaitEvent ? real_WaitEvent(event) : 0;
    if (!active || !r || !event) return r;
    const MouseButtonEvent *b = event;
    if (getenv("JANUS_GS_DEBUG") && (b->type & 0xff00) == 0x400) LOG("event 0x%x focused=%d pending=%d", b->type, focused, unfocused_since_click);
    if (b->type == SDL_MOUSEBUTTONDOWN && unfocused_since_click) {
        unfocused_since_click = 0;
        warp_nested_to(b->windowID, b->x, b->y);
    }
    return r;
}

// Drop this library from LD_PRELOAD so gamescope's children don't load it.
static void strip_self_from_preload(void)
{
    Dl_info info;
    const char *pre = getenv("LD_PRELOAD");
    if (!pre || !dladdr((void *)strip_self_from_preload, &info) || !info.dli_fname) return;
    const char *self = strrchr(info.dli_fname, '/');
    self = self ? self + 1 : info.dli_fname;
    char *copy = strdup(pre), *out = calloc(1, strlen(pre) + 1), *save = NULL;
    if (!copy || !out) { free(copy); free(out); return; }
    for (char *tok = strtok_r(copy, ": ", &save); tok; tok = strtok_r(NULL, ": ", &save)) {
        const char *base = strrchr(tok, '/');
        if (strcmp(base ? base + 1 : tok, self) == 0) continue;
        if (*out) strcat(out, ":");
        strcat(out, tok);
    }
    setenv("LD_PRELOAD", out, 1);
    free(copy);
    free(out);
}

__attribute__((constructor)) static void janus_gamescope_init(void)
{
    char exe[4096];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n > 0) {
        exe[n] = 0;
        const char *base = strrchr(exe, '/');
        active = strncmp(base ? base + 1 : exe, "gamescope", 9) == 0;
    }
    verbose = getenv("JANUS_VERBOSE") && *getenv("JANUS_VERBOSE") == '1';
    strip_self_from_preload();
}

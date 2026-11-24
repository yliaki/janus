// UI scale for IL2CPP Unity games (Proton), injected as a DLL (e.g. by
// gbe_fork's ColdClientLoader, DllsToInjectFolder). The native counterpart of
// src/unity_uiscale/JanusUiScale.cs, which needs BepInEx 5 (Mono only).
//
// No metadata file or generated bindings: everything is looked up by name
// through the il2cpp runtime API that GameAssembly.dll exports, so it works
// with games that embed/encrypt global-metadata.dat (Gunfire Reborn).
//
// Every root CanvasScaler in ScaleWithScreenSize mode gets its reference
// resolution divided by the factor (JANUS_UI_SCALE). When a canvas's menu
// changes (set of active children), the largest factor up to the setting at
// which no visible graphic is pushed off screen (or further off than at the
// game's own size) is found by binary search, all within one frame. Refit
// once the menu has settled, and whenever overflow persists.
//
// Main-thread hook: CanvasScaler.Update's MethodInfo->methodPointer is
// swapped (Unity invokes MonoBehaviour messages through the MethodInfo).
//
// Env: JANUS_UI_SCALE (1.0-4.0), JANUS_UI_LOG (log path; default
// janus_uiscale.log beside the DLL), JANUS_UI_VERBOSE=1.
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

// --- il2cpp API -----------------------------------------------------------

#define API(ret, name, args) typedef ret (*t_##name) args; static t_##name name;
API(void *, il2cpp_domain_get, (void))
API(void **, il2cpp_domain_get_assemblies, (void *, size_t *))
API(void *, il2cpp_assembly_get_image, (void *))
API(void *, il2cpp_class_from_name, (void *, const char *, const char *))
API(void *, il2cpp_class_get_methods, (void *, void **))
API(const char *, il2cpp_method_get_name, (void *))
API(uint32_t, il2cpp_method_get_param_count, (void *))
API(void *, il2cpp_method_get_param, (void *, uint32_t))
API(int, il2cpp_method_is_generic, (void *))
API(char *, il2cpp_type_get_name, (void *))
API(void *, il2cpp_class_get_field_from_name, (void *, const char *))
API(void, il2cpp_field_get_value, (void *, void *, void *))
API(void, il2cpp_field_set_value, (void *, void *, void *))
API(void *, il2cpp_thread_attach, (void *))
API(void *, il2cpp_class_get_type, (void *))
API(void *, il2cpp_type_get_object, (void *))
API(void *, il2cpp_array_new, (void *, uintptr_t))
API(uint32_t, il2cpp_gchandle_new, (void *, int))
API(void, il2cpp_gchandle_free, (uint32_t))
API(void, il2cpp_free, (void *))
API(void *, il2cpp_class_get_parent, (void *))
API(void *, il2cpp_object_get_class, (void *))
API(uint8_t, il2cpp_class_is_subclass_of, (void *, void *, uint8_t))
API(void *, il2cpp_resolve_icall, (const char *))

// --- logging --------------------------------------------------------------

static FILE *logfile;
static int verbose;

static void LOG(const char *fmt, ...) {
    if (!logfile) return;
    va_list ap; va_start(ap, fmt);
    SYSTEMTIME t; GetLocalTime(&t);
    fprintf(logfile, "%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    vfprintf(logfile, fmt, ap); fputc('\n', logfile); fflush(logfile);
    va_end(ap);
}

// --- managed layout (64-bit) ------------------------------------------------

typedef struct { float x, y; } Vec2;
typedef struct { float x, y, z; } Vec3;
typedef struct { float r, g, b, a; } Color;

#define ARR_LEN(a) (*(uintptr_t *)((char *)(a) + 0x18))
#define ARR_PTR(a, i) (((void **)((char *)(a) + 0x20))[i])
#define ARR_VEC3(a) ((Vec3 *)((char *)(a) + 0x20))
// UnityEngine.Object.m_CachedPtr: 0 once the native object is destroyed.
#define UNITY_ALIVE(o) ((o) && *(void **)((char *)(o) + 0x10) != NULL)

// A method's code through its MethodInfo (methodPointer is the first member;
// instance methods: (this, args..., MethodInfo*); static: (args..., MethodInfo*)).
#define FN(mi) (*(void **)(mi))

typedef uint8_t (*f_bool)(void *, void *);
typedef int32_t (*f_int)(void *, void *);
typedef float (*f_float)(void *, void *);
typedef void *(*f_obj)(void *, void *);
typedef void *(*f_obj_i)(void *, int32_t, void *);
typedef void *(*f_obj_o)(void *, void *, void *);
typedef void (*f_void)(void *, void *);
typedef void (*f_void_o)(void *, void *, void *);
typedef Color (*f_color)(void *, void *);
typedef int32_t (*f_sint)(void *);
typedef void (*f_svoid)(void *);

static void *k_scaler, *k_canvas, *k_graphic, *k_vec3, *k_text, *k_tmp;
static void *fld_mode, *fld_ref, *fld_canvas;
static void *m_update, *m_handle;
static void *m_isroot, *m_rendermode, *m_worldcam, *m_force_update;
static void *m_targettex;
static void *m_get_transform, *m_get_gameobject, *m_gcic;
static void *m_childcount, *m_getchild;
static void *m_activeself;
static void *m_enabled;
static void *m_get_cr, *m_cull, *m_inh_alpha, *m_color, *m_get_rt;
static void *m_corners;
static void *m_scr_w, *m_scr_h;
static void *m_name, *m_parent;
static void *graphic_type;           // System.Type object of UnityEngine.UI.Graphic
static void *corners;                // Vector3[4], pinned by a GC handle

static float factor = 1.0f;
static int want_w, want_h;           // JANUS_SCREEN_SIZE
// JANUS_SIZE_FILE (dynamic players): "WxH", re-read every 2 s; Janus rewrites
// it when the tiles re-flow. gamescope's screen is then monitor-sized, so the
// game must be windowed (mode 3): a full-screen window would cover it all.
static char size_path[MAX_PATH];
static int screen_mode = 1;          // FullScreenWindow, or 3 = Windowed

static void read_size_file(void) {
    if (!size_path[0]) return;
    FILE *f = fopen(size_path, "r");
    if (!f) return;
    int w = 0, h = 0;
    if (fscanf(f, "%dx%d", &w, &h) == 2 && w > 0 && h > 0 && (w != want_w || h != want_h)) {
        LOG("size file: %dx%d", w, h);
        want_w = w; want_h = h;
    }
    fclose(f);
}
typedef void (*fn_setres_icall)(int32_t, int32_t, int32_t, int32_t);
static fn_setres_icall setres_icall;

// --- method lookup ---------------------------------------------------------

static void *find_class(const char *ns, const char *name) {
    size_t n = 0;
    void **as = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n);
    for (size_t i = 0; i < n; i++) {
        void *k = il2cpp_class_from_name(il2cpp_assembly_get_image(as[i]), ns, name);
        if (k) return k;
    }
    return NULL;
}

// Non-generic method by name and parameter count, optionally by the type name
// of its first parameter; searched up the class hierarchy.
static void *find_method(void *klass, const char *name, int nparams, const char *param0) {
    for (void *k = klass; k; k = il2cpp_class_get_parent(k)) {
        void *iter = NULL, *m;
        while ((m = il2cpp_class_get_methods(k, &iter))) {
            if (strcmp(il2cpp_method_get_name(m), name) != 0) continue;
            if ((int)il2cpp_method_get_param_count(m) != nparams) continue;
            if (il2cpp_method_is_generic(m)) continue;
            if (param0) {
                char *tn = il2cpp_type_get_name(il2cpp_method_get_param(m, 0));
                int ok = tn && strcmp(tn, param0) == 0;
                if (tn) il2cpp_free(tn);
                if (!ok) continue;
            }
            if (!FN(m)) continue;    // stripped body
            return m;
        }
    }
    return NULL;
}

// --- helpers ---------------------------------------------------------------

static void name_of(void *obj, char *out, int n) {
    out[0] = 0;
    if (!m_name || !UNITY_ALIVE(obj)) return;
    void *s = ((f_obj)FN(m_name))(obj, m_name);
    if (!s) return;
    int len = *(int32_t *)((char *)s + 0x10);
    uint16_t *w = (uint16_t *)((char *)s + 0x14);
    int i; for (i = 0; i < len && i < n - 1; i++) out[i] = w[i] < 128 ? (char)w[i] : '?';
    out[i] = 0;
}

static void path_of(void *component, char *out, int n) {
    // name of the GameObject and up to 3 parents (diagnostics only)
    out[0] = 0;
    void *t = ((f_obj)FN(m_get_transform))(component, m_get_transform);
    char parts[4][64]; int np = 0;
    while (t && np < 4) {
        name_of(t, parts[np++], 64);
        t = m_parent ? ((f_obj)FN(m_parent))(t, m_parent) : NULL;
    }
    for (int i = np - 1; i >= 0; i--) {
        strncat(out, parts[i], n - strlen(out) - 1);
        if (i) strncat(out, "/", n - strlen(out) - 1);
    }
}

static int screen_w(void) { return ((f_sint)FN(m_scr_w))(m_scr_w); }
static int screen_h(void) { return ((f_sint)FN(m_scr_h))(m_scr_h); }

// --- per canvas state ------------------------------------------------------

#define MAXCOV 8
// Baseline (factor 1) state of one graphic: how far off screen, how much of
// it is on screen, and (text) which later-drawn graphics already cover it.
typedef struct { void *g; float over, vis; uint8_t onscreen, ncov; void *cov[MAXCOV]; } BEntry;
typedef struct Baseline { uint32_t sig; int n; BEntry *e; struct Baseline *next; } Baseline;
typedef struct { uint32_t sig; float f; } Learned;

#define MAXCANVAS 64
#define MAXLEARN 64
typedef struct {
    void *scaler, *canvas, *root;    // root = the canvas's RectTransform
    uint32_t h_scaler, h_canvas, h_root;
    char name[64];
    Vec2 orig_ref;
    float current;
    uint32_t sig;
    ULONGLONG refit_at, next_check;
    int streak;
    Learned learned[MAXLEARN]; int nlearned;
    Baseline *baselines;
} Managed;

static Managed canv[MAXCANVAS];
static int ncanv;
static void *rejected[256]; static int nrejected;

#define SETTLE_MS 600
#define CHECK_MS 250
#define PERSIST 3
#define STEPS 7

static void apply(Managed *m, float f) {
    m->current = f;
    Vec2 r = { m->orig_ref.x / f, m->orig_ref.y / f };
    il2cpp_field_set_value(m->scaler, fld_ref, &r);
}

static void apply_now(Managed *m, float f) {
    apply(m, f);
    if (m_handle) ((f_void)FN(m_handle))(m->scaler, m_handle);
    if (m_force_update) { ((f_svoid)FN(m_force_update))(m_force_update); ((f_svoid)FN(m_force_update))(m_force_update); }
}

// Menu-state signature: active children down to depth 3 (by object identity).
static void walk(void *t, int depth, uint32_t *h) {
    if (depth == 3) return;
    int n = ((f_int)FN(m_childcount))(t, m_childcount);
    for (int i = 0; i < n; i++) {
        void *c = ((f_obj_i)FN(m_getchild))(t, i, m_getchild);
        if (!c) continue;
        void *go = ((f_obj)FN(m_get_gameobject))(c, m_get_gameobject);
        if (!go || !((f_bool)FN(m_activeself))(go, m_activeself)) continue;
        *h = *h * 31u + (uint32_t)((uintptr_t)c >> 4) + (uint32_t)i;
        walk(c, depth + 1, h);
    }
}

static uint32_t signature(Managed *m) {
    uint32_t h = 17;
    walk(m->root, 0, &h);
    return h;
}

static int cmp_bentry(const void *a, const void *b) {
    uintptr_t x = (uintptr_t)((const BEntry *)a)->g, y = (uintptr_t)((const BEntry *)b)->g;
    return x < y ? -1 : x > y;
}

static BEntry *bfind(Baseline *b, void *g) {
    if (!b) return NULL;
    BEntry key = { .g = g };
    return bsearch(&key, b->e, b->n, sizeof(BEntry), cmp_bentry);
}

static Baseline *baseline_for(Managed *m, uint32_t sig) {
    for (Baseline *b = m->baselines; b; b = b->next) if (b->sig == sig) return b;
    return NULL;
}

static int is_text(void *g) {
    void *k = il2cpp_object_get_class(g);
    return (k_text && il2cpp_class_is_subclass_of(k, k_text, 0))
        || (k_tmp && il2cpp_class_is_subclass_of(k, k_tmp, 0));
}

// One visible graphic, in draw (hierarchy) order.
typedef struct { void *g; float x0, y0, x1, y1, alpha; uint8_t text; } Item;
static Item *items; static int nitems, capitems;

// Fraction of a's area covered by b.
static float covered(const Item *a, const Item *b) {
    float w = fminf(a->x1, b->x1) - fmaxf(a->x0, b->x0), h = fminf(a->y1, b->y1) - fmaxf(a->y0, b->y0);
    float area = (a->x1 - a->x0) * (a->y1 - a->y0);
    return (w > 0 && h > 0 && area > 0) ? w * h / area : 0;
}

// Graphics that can hide text: not larger than half the screen either way
// (full-screen flashes/vignettes, glow halos: Gunfire Reborn's skill glow is
// 1239x1239 in a 540 px tall tile, mostly transparent).
static int coverer(const Item *o, float sw, float sh) {
    return o->x1 - o->x0 <= sw * 0.5f && o->y1 - o->y0 <= sh * 0.5f;
}

#define COVER_BASE 0.05f     // covering at the game's size: allowed to stay
#define COVER_NEW 0.25f      // newly covering this much of a text: too big

// Measure every visible graphic of the canvas. With rec: record the baseline
// (factor 1). With base: return 0 at the first graphic that is newly off
// screen, text further off screen, or text newly covered by a graphic drawn
// over it; report=1 logs every culprit.
static int measure(Managed *m, Baseline *base, Baseline *rec, int report) {
    float sw = (float)screen_w(), sh = (float)screen_h();
    ((f_void_o)FN(m_corners))(m->root, corners, m_corners);
    Vec3 *c = ARR_VEC3(corners);
    float cx0 = c[0].x, cy0 = c[0].y, cw = c[2].x - c[0].x, ch = c[2].y - c[0].y;
    if (fabsf(cw) < 1e-6f || fabsf(ch) < 1e-6f) return 1;
    void *arr = ((f_obj_o)FN(m_gcic))(m->canvas, graphic_type, m_gcic);
    if (!arr) return 1;
    int n = (int)ARR_LEN(arr), ok = 1;
    // Pass 1: screen rects of the visible graphics.
    nitems = 0;
    for (int i = 0; i < n; i++) {
        void *g = ARR_PTR(arr, i);
        if (!g || !((f_bool)FN(m_enabled))(g, m_enabled)) continue;
        Color col = ((f_color)FN(m_color))(g, m_color);
        if (col.a <= 0.01f) continue;
        void *cr = ((f_obj)FN(m_get_cr))(g, m_get_cr);
        if (cr) {
            if (m_cull && ((f_bool)FN(m_cull))(cr, m_cull)) continue;
            if (m_inh_alpha && ((f_float)FN(m_inh_alpha))(cr, m_inh_alpha) * col.a <= 0.01f) continue;
        }
        void *rt = ((f_obj)FN(m_get_rt))(g, m_get_rt);
        if (!rt) continue;
        ((f_void_o)FN(m_corners))(rt, corners, m_corners);
        Item it = { g, 1e9f, 1e9f, -1e9f, -1e9f, col.a, 0 };
        for (int k = 0; k < 4; k++) {
            float x = (c[k].x - cx0) / cw * sw, y = (c[k].y - cy0) / ch * sh;
            it.x0 = fminf(it.x0, x); it.x1 = fmaxf(it.x1, x); it.y0 = fminf(it.y0, y); it.y1 = fmaxf(it.y1, y);
        }
        it.text = (uint8_t)is_text(g);
        if (nitems == capitems) { capitems = capitems ? capitems * 2 : 256; items = realloc(items, sizeof(Item) * capitems); }
        items[nitems++] = it;
    }
    if (rec) { rec->n = 0; rec->e = realloc(rec->e, sizeof(BEntry) * (nitems ? nitems : 1)); }
    // Pass 2: judge each graphic.
    for (int i = 0; i < nitems; i++) {
        Item *it = &items[i];
        float x0 = it->x0, y0 = it->y0, x1 = it->x1, y1 = it->y1, rw = x1 - x0, rh = y1 - y0;
        // Fraction of the element's area on screen.
        float vis = (rw > 0 && rh > 0)
            ? fmaxf(0, fminf(x1, sw) - fmaxf(x0, 0)) * fmaxf(0, fminf(y1, sh) - fmaxf(y0, 0)) / (rw * rh) : 0;
        int off = x1 <= 0 || x0 >= sw || y1 <= 0 || y0 >= sh;
        float over = 0; int big = 0;
        if (rw > 1 && rh > 1 && !off) {
            int spanx = x0 <= 1 && x1 >= sw - 1, spany = y0 <= 1 && y1 >= sh - 1;
            float ox = spanx ? 0 : fmaxf(-x0, x1 - sw), oy = spany ? 0 : fmaxf(-y0, y1 - sh);
            over = fmaxf(ox / (sw * 0.5f), oy / (sh * 0.5f));
            // Corner pieces hugging the edge (transparent padding on HUD
            // art) aren't overflow: 2 % of the shorter side, at least 3 px.
            // Text gets 3 px.
            big = fmaxf(ox, oy) > (it->text ? 3.0f : fmaxf(3.0f, 0.02f * fminf(sw, sh)));
        }
        // Text drawn under a later graphic (see coverer()).
        int textcheck = it->text && !off && vis >= 0.5f && rw > 1 && rh > 1;
        if (rec) {
            BEntry e = { .g = it->g, .over = big ? over : 0, .vis = vis,
                         .onscreen = (uint8_t)(rw > 1 && rh > 1 && !off) };
            for (int j = i + 1; textcheck && j < nitems && e.ncov < MAXCOV; j++) {
                Item *o = &items[j];
                if (!coverer(o, sw, sh)) continue;
                if (covered(it, o) > COVER_BASE) e.cov[e.ncov++] = o->g;
            }
            rec->e[rec->n++] = e;
            continue;
        }
        const char *why = NULL;
        void *by = NULL; Item byi = {0};
        BEntry *b = bfind(base, it->g);
        if (off && b && b->onscreen) why = "pushed off screen";
        else if (big) {
            if (!b || (b->over <= 0 && b->onscreen)) why = "off screen";
            // Decorations designed to hang off screen (ink, frames) may hang
            // off further; text may not. Text mostly off screen at the
            // game's size is parked there.
            else if (b->over > 0 && b->vis >= 0.5f && over > b->over + 0.01f && it->text)
                why = "text further off screen";
        }
        if (!why && textcheck && b && b->vis >= 0.5f) {
            for (int j = i + 1; j < nitems; j++) {
                Item *o = &items[j];
                if (!coverer(o, sw, sh)) continue;
                if (covered(it, o) <= COVER_NEW) continue;
                int known = 0;
                for (int k = 0; k < b->ncov; k++) if (b->cov[k] == o->g) { known = 1; break; }
                if (known || (b->ncov == MAXCOV)) continue;
                why = "text covered"; by = o->g; byi = *o; break;
            }
        }
        if (!why) continue;
        ok = 0;
        if (!report) return 0;
        char p[200], q[200] = ""; path_of(it->g, p, sizeof p);
        if (by) path_of(by, q, sizeof q);
        if (by)
            LOG("    '%s' %.0f,%.0f %.0fx%.0f: %s by '%s' %.0f,%.0f %.0fx%.0f alpha %.2f", p, x0, y0, rw, rh, why,
                q, byi.x0, byi.y0, byi.x1 - byi.x0, byi.y1 - byi.y0, byi.alpha);
        else LOG("    '%s' %.0f,%.0f %.0fx%.0f: %s", p, x0, y0, rw, rh, why);
    }
    if (rec) qsort(rec->e, rec->n, sizeof(BEntry), cmp_bentry);
    return ok;
}

static void fit(Managed *m, const char *why) {
    float before = m->current;
    Baseline *b = baseline_for(m, m->sig);
    if (!b) { b = calloc(1, sizeof *b); b->sig = m->sig; b->next = m->baselines; m->baselines = b; }
    apply_now(m, 1.0f);
    measure(m, NULL, b, 0);
    float lo = 1.0f, hi = factor;
    apply_now(m, hi);
    if (!measure(m, b, NULL, 0)) {
        for (int i = 0; i < STEPS; i++) {
            float mid = (lo + hi) * 0.5f;
            apply_now(m, mid);
            if (measure(m, b, NULL, 0)) lo = mid; else hi = mid;
        }
        hi = lo;
        apply_now(m, hi);
    }
    int i;
    for (i = 0; i < m->nlearned && m->learned[i].sig != m->sig; i++) ;
    if (i == m->nlearned && m->nlearned < MAXLEARN) m->nlearned++;
    if (i < MAXLEARN) { m->learned[i].sig = m->sig; m->learned[i].f = hi; }
    m->streak = 0;
    if (fabsf(hi - before) > 0.001f || strcmp(why, "opened") == 0 || verbose) {
        LOG("fit '%s' (%s): factor %.3f (%d graphics)", m->name, why, hi, b->n);
        if (hi < factor - 0.001f && verbose) {
            apply_now(m, fminf(factor, hi * 1.05f));
            LOG("  bigger would break:");
            measure(m, b, NULL, 1);
            apply_now(m, hi);
        }
    }
}

static void menu_changed(Managed *m, uint32_t sig) {
    m->sig = sig;
    m->streak = 0;
    for (int i = 0; i < m->nlearned; i++)
        if (m->learned[i].sig == sig) {
            apply_now(m, m->learned[i].f);
            m->refit_at = GetTickCount64() + SETTLE_MS;
            return;
        }
    fit(m, "opened");
    m->refit_at = GetTickCount64() + SETTLE_MS;
}

static void check(Managed *m) {
    Baseline *b = baseline_for(m, m->sig);
    if (!b || measure(m, b, NULL, 0)) { m->streak = 0; return; }
    if (++m->streak < PERSIST) return;
    if (m->current > 1.001f) {
        LOG("overflow on '%s' at factor %.3f:", m->name, m->current);
        measure(m, b, NULL, 1);
        fit(m, "overflow");
    }
    m->streak = 0;
}

static void forget(Managed *m) {
    il2cpp_gchandle_free(m->h_scaler); il2cpp_gchandle_free(m->h_canvas); il2cpp_gchandle_free(m->h_root);
    for (Baseline *b = m->baselines, *nx; b; b = nx) { nx = b->next; free(b->e); free(b); }
    *m = canv[--ncanv];
}

static Managed *find_managed(void *scaler) {
    for (int i = 0; i < ncanv; i++) if (canv[i].scaler == scaler) return &canv[i];
    return NULL;
}

static int is_rejected(void *s) {
    for (int i = 0; i < nrejected; i++) if (rejected[i] == s) return 1;
    return 0;
}

static Managed *register_scaler(void *s) {
    if (is_rejected(s)) return NULL;
    void *canvas = NULL; il2cpp_field_get_value(s, fld_canvas, &canvas);
    int mode = 0; il2cpp_field_get_value(s, fld_mode, &mode);
    if (!UNITY_ALIVE(canvas)) return NULL;     // not initialised yet: retry next frame
    char nm[64]; name_of(s, nm, sizeof nm);
    const char *skip = NULL;
    if (mode != 1) skip = "not ScaleWithScreenSize";
    else if (m_isroot && !((f_bool)FN(m_isroot))(canvas, m_isroot)) skip = "not a root canvas";
    else {
        int rm = m_rendermode ? ((f_int)FN(m_rendermode))(canvas, m_rendermode) : 0;
        if (rm == 2) skip = "world space";
        else if (rm == 1 && m_worldcam && m_targettex) {
            void *cam = ((f_obj)FN(m_worldcam))(canvas, m_worldcam);
            if (cam && ((f_obj)FN(m_targettex))(cam, m_targettex)) skip = "renders into a texture";
        }
    }
    if (skip || ncanv == MAXCANVAS) {
        if (nrejected < 256) rejected[nrejected++] = s;
        LOG("canvas '%s': left alone (%s)", nm, skip ? skip : "too many canvases");
        return NULL;
    }
    Managed *m = &canv[ncanv++];
    memset(m, 0, sizeof *m);
    m->scaler = s; m->canvas = canvas;
    m->root = ((f_obj)FN(m_get_transform))(canvas, m_get_transform);
    m->h_scaler = il2cpp_gchandle_new(s, 0);
    m->h_canvas = il2cpp_gchandle_new(canvas, 0);
    m->h_root = il2cpp_gchandle_new(m->root, 0);
    strcpy(m->name, nm);
    il2cpp_field_get_value(s, fld_ref, &m->orig_ref);
    m->current = 1.0f;
    LOG("canvas '%s': reference %.0fx%.0f -> managed", nm, m->orig_ref.x, m->orig_ref.y);
    return m;
}

// --- the hook ---------------------------------------------------------------

typedef void (*fn_update)(void *, void *);
static fn_update orig_update;
static int last_sw, last_sh;

static ULONGLONG next_res_fix;

static void hook_update(void *self, void *method) {
    orig_update(self, method);
    // Main thread: undo a resolution the game set before our SetResolution
    // hooks were in (Gunfire Reborn's aspect clamp at start-up).
    if (want_w > 0 && setres_icall && GetTickCount64() >= next_res_fix) {
        next_res_fix = GetTickCount64() + 2000;
        read_size_file();
        int sw = screen_w(), sh = screen_h();
        if (sw != want_w || sh != want_h) {
            LOG("screen %dx%d, want %dx%d: setting it", sw, sh, want_w, want_h);
            setres_icall(want_w, want_h, screen_mode, 0);
        }
    }
    if (factor <= 1.0f) return;
    Managed *m = find_managed(self);
    if (!m) { m = register_scaler(self); if (!m) return; }
    if (!UNITY_ALIVE(m->scaler) || !UNITY_ALIVE(m->canvas) || !UNITY_ALIVE(m->root)) { forget(m); return; }
    int sw = screen_w(), sh = screen_h();
    if (sw != last_sw || sh != last_sh) {
        if (last_sw) LOG("screen %dx%d -> %dx%d: re-fitting", last_sw, last_sh, sw, sh);
        else LOG("screen %dx%d", sw, sh);
        last_sw = sw; last_sh = sh;
        for (int i = 0; i < ncanv; i++) { canv[i].nlearned = 0; canv[i].sig = 0; }
    }
    ULONGLONG now = GetTickCount64();
    uint32_t sig = signature(m);
    if (sig != m->sig) menu_changed(m, sig);
    else if (m->refit_at && now >= m->refit_at) { m->refit_at = 0; fit(m, "settled"); }
    else if (now >= m->next_check) { m->next_check = now + CHECK_MS; check(m); }
}

// --- screen size ------------------------------------------------------------
// JANUS_SCREEN_SIZE=WxH: every Screen.SetResolution the game makes is applied
// at that size (the tile). Gunfire Reborn clamps its aspect to >= 32:27, so in
// a 960x1080 tile it shrank itself to 960x810, letterboxed. The C# wrappers
// are called directly by compiled code, so their entry is patched with a jump
// to ours, which calls the engine's icall.


static void setres(int w, int h, int mode, int hz, const char *via) {
    LOG("SetResolution(%d, %d, mode %d, %d Hz) via %s -> %dx%d", w, h, mode, hz, via, want_w, want_h);
    setres_icall(want_w, want_h, size_path[0] ? screen_mode : mode, hz);
}
static void hk_mode_hz(int32_t w, int32_t h, int32_t mode, int32_t hz, void *mi) { setres(w, h, mode, hz, "(w,h,mode,hz)"); }
static void hk_mode(int32_t w, int32_t h, int32_t mode, void *mi) { setres(w, h, mode, 0, "(w,h,mode)"); }
static void hk_bool_hz(int32_t w, int32_t h, uint8_t fs, int32_t hz, void *mi) { setres(w, h, fs ? 1 : 3, hz, "(w,h,bool,hz)"); }
static void hk_bool(int32_t w, int32_t h, uint8_t fs, void *mi) { setres(w, h, fs ? 1 : 3, 0, "(w,h,bool)"); }

static int patch_jump(void *at, void *to) {
    uint8_t code[12] = { 0x48, 0xB8, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xE0 };  // mov rax, imm64; jmp rax
    memcpy(code + 2, &to, 8);
    DWORD old;
    if (!VirtualProtect(at, sizeof code, PAGE_EXECUTE_READWRITE, &old)) return 0;
    memcpy(at, code, sizeof code);
    VirtualProtect(at, sizeof code, old, &old);
    FlushInstructionCache(GetCurrentProcess(), at, sizeof code);
    return 1;
}

static void install_screen_size(void *k_scr) {
    static const char *icalls[] = {
        "UnityEngine.Screen::SetResolution(System.Int32,System.Int32,UnityEngine.FullScreenMode,System.Int32)",
        "UnityEngine.Screen::SetResolution",
    };
    for (unsigned i = 0; i < sizeof icalls / sizeof *icalls && !setres_icall; i++)
        setres_icall = (fn_setres_icall)il2cpp_resolve_icall(icalls[i]);
    if (!setres_icall) { LOG("screen size: SetResolution icall not found"); return; }
    struct { int n; const char *p2; void *hook; const char *desc; } ov[] = {
        { 4, "UnityEngine.FullScreenMode", (void *)hk_mode_hz, "(w,h,mode,hz)" },
        { 3, "UnityEngine.FullScreenMode", (void *)hk_mode, "(w,h,mode)" },
        { 4, "System.Boolean", (void *)hk_bool_hz, "(w,h,bool,hz)" },
        { 3, "System.Boolean", (void *)hk_bool, "(w,h,bool)" },
    };
    int done = 0;
    for (unsigned i = 0; i < sizeof ov / sizeof *ov; i++) {
        void *iter = NULL, *m;
        while ((m = il2cpp_class_get_methods(k_scr, &iter))) {
            if (strcmp(il2cpp_method_get_name(m), "SetResolution") != 0) continue;
            if ((int)il2cpp_method_get_param_count(m) != ov[i].n || !FN(m)) continue;
            char *tn = il2cpp_type_get_name(il2cpp_method_get_param(m, 2));
            int match = tn && strcmp(tn, ov[i].p2) == 0;
            if (tn) il2cpp_free(tn);
            if (!match) continue;
            if (patch_jump(FN(m), ov[i].hook)) { done++; LOG("screen size: hooked SetResolution%s", ov[i].desc); }
        }
    }
    LOG("screen size: forcing %dx%d (%d overloads hooked)", want_w, want_h, done);
}

// --- setup -----------------------------------------------------------------

#define NEED(x) do { if (!(x)) { LOG("missing: %s", #x); return 0; } } while (0)

static int resolve(void) {
    k_scaler = find_class("UnityEngine.UI", "CanvasScaler");
    k_canvas = find_class("UnityEngine", "Canvas");
    k_graphic = find_class("UnityEngine.UI", "Graphic");
    k_vec3 = find_class("UnityEngine", "Vector3");
    k_text = find_class("UnityEngine.UI", "Text");
    k_tmp = find_class("TMPro", "TMP_Text");
    void *k_comp = find_class("UnityEngine", "Component");
    void *k_tr = find_class("UnityEngine", "Transform");
    void *k_rt = find_class("UnityEngine", "RectTransform");
    void *k_go = find_class("UnityEngine", "GameObject");
    void *k_beh = find_class("UnityEngine", "Behaviour");
    void *k_cr = find_class("UnityEngine", "CanvasRenderer");
    void *k_cam = find_class("UnityEngine", "Camera");
    void *k_scr = find_class("UnityEngine", "Screen");
    void *k_obj = find_class("UnityEngine", "Object");
    NEED(k_scaler); NEED(k_canvas); NEED(k_graphic); NEED(k_vec3); NEED(k_comp); NEED(k_tr);
    NEED(k_rt); NEED(k_go); NEED(k_beh); NEED(k_cr); NEED(k_scr); NEED(k_obj);
    fld_mode = il2cpp_class_get_field_from_name(k_scaler, "m_UiScaleMode");
    fld_ref = il2cpp_class_get_field_from_name(k_scaler, "m_ReferenceResolution");
    fld_canvas = il2cpp_class_get_field_from_name(k_scaler, "m_Canvas");
    NEED(fld_mode); NEED(fld_ref); NEED(fld_canvas);
    m_update = find_method(k_scaler, "Update", 0, NULL);
    m_handle = find_method(k_scaler, "Handle", 0, NULL);
    m_isroot = find_method(k_canvas, "get_isRootCanvas", 0, NULL);
    m_rendermode = find_method(k_canvas, "get_renderMode", 0, NULL);
    m_worldcam = find_method(k_canvas, "get_worldCamera", 0, NULL);
    m_force_update = find_method(k_canvas, "ForceUpdateCanvases", 0, NULL);
    m_targettex = k_cam ? find_method(k_cam, "get_targetTexture", 0, NULL) : NULL;
    m_get_transform = find_method(k_comp, "get_transform", 0, NULL);
    m_get_gameobject = find_method(k_comp, "get_gameObject", 0, NULL);
    m_gcic = find_method(k_comp, "GetComponentsInChildren", 1, "System.Type");  // active only
    m_childcount = find_method(k_tr, "get_childCount", 0, NULL);
    m_getchild = find_method(k_tr, "GetChild", 1, NULL);
    m_activeself = find_method(k_go, "get_activeSelf", 0, NULL);
    m_enabled = find_method(k_beh, "get_enabled", 0, NULL);
    m_get_cr = find_method(k_graphic, "get_canvasRenderer", 0, NULL);
    m_cull = find_method(k_cr, "get_cull", 0, NULL);
    m_inh_alpha = find_method(k_cr, "GetInheritedAlpha", 0, NULL);
    m_color = find_method(k_graphic, "get_color", 0, NULL);
    m_get_rt = find_method(k_graphic, "get_rectTransform", 0, NULL);
    m_corners = find_method(k_rt, "GetWorldCorners", 1, NULL);
    m_scr_w = find_method(k_scr, "get_width", 0, NULL);
    m_scr_h = find_method(k_scr, "get_height", 0, NULL);
    m_name = find_method(k_obj, "get_name", 0, NULL);
    m_parent = find_method(k_tr, "get_parent", 0, NULL);
    LOG("methods: handle=%p force=%p isRoot=%p cull=%p inhAlpha=%p targetTex=%p",
        m_handle, m_force_update, m_isroot, m_cull, m_inh_alpha, m_targettex);
    NEED(m_update); NEED(m_handle); NEED(m_get_transform); NEED(m_get_gameobject); NEED(m_gcic);
    NEED(m_childcount); NEED(m_getchild); NEED(m_activeself); NEED(m_enabled); NEED(m_get_cr);
    NEED(m_color); NEED(m_get_rt); NEED(m_corners); NEED(m_scr_w); NEED(m_scr_h);
    if (want_w > 0 && want_h > 0) install_screen_size(k_scr);
    if (factor <= 1.0f) return 1;
    graphic_type = il2cpp_type_get_object(il2cpp_class_get_type(k_graphic));
    il2cpp_gchandle_new(graphic_type, 0);
    corners = il2cpp_array_new(k_vec3, 4);
    il2cpp_gchandle_new(corners, 1);
    return 1;
}

#define GA(name) do { name = (t_##name)GetProcAddress(ga, #name); if (!name) { LOG("GameAssembly lacks " #name); return 0; } } while (0)

static DWORD WINAPI worker(LPVOID arg) {
    (void)arg;
    HMODULE ga = NULL;
    for (int i = 0; i < 1200 && !(ga = GetModuleHandleA("GameAssembly.dll")); i++) Sleep(100);
    if (!ga) { LOG("GameAssembly.dll never loaded; giving up"); return 0; }
    GA(il2cpp_domain_get); GA(il2cpp_domain_get_assemblies); GA(il2cpp_assembly_get_image);
    GA(il2cpp_class_from_name); GA(il2cpp_class_get_methods); GA(il2cpp_method_get_name);
    GA(il2cpp_method_get_param_count); GA(il2cpp_method_get_param); GA(il2cpp_method_is_generic);
    GA(il2cpp_type_get_name); GA(il2cpp_class_get_field_from_name); GA(il2cpp_field_get_value);
    GA(il2cpp_field_set_value); GA(il2cpp_thread_attach); GA(il2cpp_class_get_type);
    GA(il2cpp_type_get_object); GA(il2cpp_array_new); GA(il2cpp_gchandle_new);
    GA(il2cpp_gchandle_free); GA(il2cpp_free); GA(il2cpp_class_get_parent);
    GA(il2cpp_object_get_class); GA(il2cpp_class_is_subclass_of); GA(il2cpp_resolve_icall);
    // Wait until the runtime has loaded its assemblies and UnityEngine.UI.
    size_t n = 0, last = 0; int stable = 0;
    for (int i = 0; i < 2400; i++) {
        Sleep(250);
        if (!il2cpp_domain_get()) continue;
        il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n);
        if (n > 5 && n == last) { if (++stable >= 8) break; } else stable = 0;
        last = n;
    }
    il2cpp_thread_attach(il2cpp_domain_get());
    if (!resolve()) { LOG("UI scale not installed"); return 0; }
    orig_update = (fn_update)FN(m_update);
    *(void **)m_update = (void *)hook_update;
    LOG("installed (factor %.2f, %zu assemblies)", factor, n);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID r) {
    (void)r;
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    char path[MAX_PATH], buf[64];
    if (!GetEnvironmentVariableA("JANUS_UI_LOG", path, MAX_PATH)) {
        GetModuleFileNameA(h, path, MAX_PATH);
        char *s = strrchr(path, '\\'); if (s) strcpy(s + 1, "janus_uiscale.log");
    }
    logfile = fopen(path, "a");
    if (GetEnvironmentVariableA("JANUS_UI_SCALE", buf, sizeof buf)) factor = (float)atof(buf);
    if (!(factor >= 1.0f)) factor = 1.0f;
    if (factor > 4.0f) factor = 4.0f;
    verbose = GetEnvironmentVariableA("JANUS_UI_VERBOSE", buf, sizeof buf) && buf[0] == '1';
    if (GetEnvironmentVariableA("JANUS_SCREEN_SIZE", buf, sizeof buf)) sscanf(buf, "%dx%d", &want_w, &want_h);
    if (GetEnvironmentVariableA("JANUS_SIZE_FILE", path, MAX_PATH) && path[0] == '/' && want_w > 0
            && GetEnvironmentVariableA("JANUS_RESIZE", buf, sizeof buf) && buf[0] == '1') {
        // A Unix path: drive Z: under Wine.
        snprintf(size_path, sizeof size_path, "Z:%s", path);
        for (char *c = size_path; *c; c++) if (*c == '/') *c = '\\';
        screen_mode = 3;
        read_size_file();
    }
    LOG("janus il2cpp uiscale loaded into pid %lu, factor %.2f, screen size %dx%d",
        GetCurrentProcessId(), factor, want_w, want_h);
    if (factor > 1.0f || want_w > 0) CreateThread(NULL, 0, worker, NULL, 0, NULL);
    return TRUE;
}

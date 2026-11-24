/*
 * Ticket proxy: a steam_api64.dll that sits in front of Goldberg
 * (renamed steam_api64_gbe.dll next to it) and forwards every export to it,
 * except SteamAPI_ISteamUser_GetAuthSessionTicket. That one still calls
 * Goldberg (so the handle, CancelAuthTicket and Goldberg's bookkeeping stay
 * consistent) but then overwrites the ticket bytes with a REAL Steam session
 * ticket, read from janus_ticket.bin beside this DLL. The file is written by
 * src/host/steam_ticket.py, a native helper that holds the ticket open
 * against the user's running Steam client.
 *
 * Why: games whose backend validates the ticket with Valve (Content Warning's
 * Photon app) reject Goldberg's fake tickets, while everything else
 * (lobbies, invites, distinct SteamIDs per instance) must stay on Goldberg.
 * The Proton-side counterpart of the native JANUS_STEAM_INPUT_REAL_LIB
 * hybrid. LD_PRELOAD can't reach this: steam_api64.dll is PE code in Wine.
 *
 * Log: janus_ticket_proxy.log beside this DLL.
 */
#include <windows.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>

#include "ticketproxy_stubs.c"   /* generated into build/ticketproxy/: janus_tbl, janus_names, jmp stubs */

static WCHAR g_dir[MAX_PATH];    /* this DLL's directory, with trailing '\' */
static HMODULE g_real;
static FILE *g_log;

typedef uint32_t (*get_ticket_fn)(void *, void *, int, uint32_t *, const void *);
static get_ticket_fn g_real_get_ticket;

static void plog(const char *fmt, ...)
{
    if (!g_log)
        return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(g_log, "%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond,
            t.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

static void path_in_dir(WCHAR *out, const WCHAR *name)
{
    lstrcpyW(out, g_dir);
    lstrcatW(out, name);
}

/* Target of exports the loaded Goldberg build doesn't have. */
static uintptr_t missing_export(void)
{
    plog("called an export the real DLL lacks; returning 0");
    return 0;
}

static void load_real(HMODULE self)
{
    GetModuleFileNameW(self, g_dir, MAX_PATH);
    WCHAR *slash = wcsrchr(g_dir, L'\\');
    if (slash)
        slash[1] = 0;

    WCHAR p[MAX_PATH];
    path_in_dir(p, L"janus_ticket_proxy.log");
    g_log = _wfopen(p, L"a");

    path_in_dir(p, L"steam_api64_gbe.dll");
    g_real = LoadLibraryW(p);
    if (!g_real) {
        plog("FATAL: can't load steam_api64_gbe.dll (error %lu)", GetLastError());
        for (int i = 0; i < JANUS_NSTUBS; i++)
            janus_tbl[i] = (void *)missing_export;
        return;
    }
    int missing = 0;
    for (int i = 0; i < JANUS_NSTUBS; i++) {
        janus_tbl[i] = (void *)GetProcAddress(g_real, janus_names[i]);
        if (!janus_tbl[i]) {
            janus_tbl[i] = (void *)missing_export;
            missing++;
        }
    }
    g_real_get_ticket = (get_ticket_fn)(void *)GetProcAddress(
        g_real, "SteamAPI_ISteamUser_GetAuthSessionTicket");
    plog("loaded steam_api64_gbe.dll: %d exports forwarded, %d absent",
         JANUS_NSTUBS - missing, missing);
}

/* Read janus_ticket.bin. The helper starts alongside the game and may not
 * have written it yet, so wait a little before giving up. */
static int read_ticket(uint8_t *buf, int max)
{
    WCHAR p[MAX_PATH];
    path_in_dir(p, L"janus_ticket.bin");
    for (int tries = 0; tries < 100; tries++) {   /* up to ~20 s */
        FILE *f = _wfopen(p, L"rb");
        if (f) {
            int n = (int)fread(buf, 1, max, f);
            int more = fgetc(f) != EOF;
            fclose(f);
            if (n > 0 && !more)
                return n;
            plog("janus_ticket.bin unusable (%d bytes%s)", n,
                 more ? ", larger than the game's buffer" : "");
            return -1;
        }
        Sleep(200);
    }
    plog("janus_ticket.bin never appeared; is steam_ticket.py running?");
    return -1;
}

__declspec(dllexport) uint32_t SteamAPI_ISteamUser_GetAuthSessionTicket(
    void *self, void *ticket, int max, uint32_t *len, const void *identity)
{
    uint32_t h = g_real_get_ticket
        ? g_real_get_ticket(self, ticket, max, len, identity) : 0;
    int n = read_ticket(ticket, max);
    if (n > 0) {
        *len = (uint32_t)n;
        plog("GetAuthSessionTicket: handle %u, real ticket (%d bytes)", h, n);
    } else {
        plog("GetAuthSessionTicket: handle %u, kept Goldberg's ticket", h);
    }
    return h;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        load_real(inst);
    }
    return TRUE;
}

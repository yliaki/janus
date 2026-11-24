// xitest.exe SECONDS: print which XInput users are connected and every state change.
#include <windows.h>
#include <xinput.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    int secs = argc > 1 ? atoi(argv[1]) : 10;
    if (argc > 2) freopen(argv[2], "w", stdout);
    XINPUT_STATE last[4]; DWORD conn[4];
    memset(last, 0, sizeof last);
    for (int u = 0; u < 4; u++) conn[u] = 99;
    DWORD end = GetTickCount() + secs * 1000;
    while (GetTickCount() < end) {
        for (int u = 0; u < 4; u++) {
            XINPUT_STATE s; memset(&s, 0, sizeof s);
            DWORD r = XInputGetState(u, &s);
            if (r != conn[u]) { printf("user %d %s\n", u, r == ERROR_SUCCESS ? "CONNECTED" : "absent"); conn[u] = r; }
            if (r == ERROR_SUCCESS && memcmp(&s.Gamepad, &last[u].Gamepad, sizeof s.Gamepad)) {
                XINPUT_GAMEPAD *g = &s.Gamepad;
                printf("user %d buttons=%04x LT=%d RT=%d LX=%d LY=%d RX=%d RY=%d\n", u, g->wButtons,
                       g->bLeftTrigger, g->bRightTrigger, g->sThumbLX, g->sThumbLY, g->sThumbRX, g->sThumbRY);
                last[u] = s;
            }
        }
        fflush(stdout);
        Sleep(5);
    }
    return 0;
}

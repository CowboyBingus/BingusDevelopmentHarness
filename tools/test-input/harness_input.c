/* The harness's test input: a HellForge code mod (deployed by the harness as mods/zz-harness-input) that presses keys,
 * mouse buttons and moves the mouse inside the engine, so a test can walk, look and shoot while the game runs in the
 * background. Never part of a release.
 *
 * Build 25480438's input controllers (helldivers2.exe): each device is an object whose vtable's slot 5 is its update
 * (keyboard exe+0x5AD320, mouse exe+0x5A8C10) and slot 1 the frame's reset. The update writes each button's value, a
 * float, at [device+0x38][index] (1.0 down) and sets bit `index` in the pressed words at [device+0xD0] or the released
 * words at [device+0xE8]; the reset clears those words. The mouse's axes are Vector3s at [device+0x138]; axis 0 is the
 * motion ("mouse"). This mod hooks both updates and, after the engine's own, applies what the harness asked for: a held
 * button's value 1.0 (its pressed bit on the first frame, its released bit after the last), and the mouse motion added
 * to axis 0. Devices are told apart by their vtables (keyboard exe+0x168A828, mouse exe+0x168A740).
 *
 * The harness calls harness_input(command) from its in-game Lua (LuaJIT's ffi), on the game thread:
 *   key <index> <seconds>             hold a keyboard button (stingray.Keyboard.button_index(name) gives the index)
 *   mouse <index> <seconds>           hold a mouse button (0 left, 1 right; stingray.Mouse.button_index)
 *   look <dx/s> <dy/s> <seconds>      move the mouse at that rate (the engine's motion units) for that long
 *   clear                             release everything now
 * Several commands can be given at once, separated by ';'. Returns the number of commands taken, -1 on a bad one.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "hellforge_mod.h"

#define KEYBOARD_UPDATE 0x5AD320u
#define MOUSE_UPDATE 0x5A8C10u
#define KEYBOARD_VTABLE 0x168A828u
#define MOUSE_VTABLE 0x168A740u
#define MAX_HOLDS 32

typedef void (*UpdateFn)(uintptr_t device);

typedef struct Hold
{
    int used, mouse, started;
    uint32_t index;
    double until;
} Hold;

static const HfApi* g_api;
static HfMod* g_mod;
static uintptr_t g_exe;
static UpdateFn g_keyboardUpdate, g_mouseUpdate;
static SRWLOCK g_lock = SRWLOCK_INIT;
static Hold g_holds[MAX_HOLDS];
static double g_lookX, g_lookY, g_lookUntil, g_lastMouse;
static int g_logged;

static double Now(void)
{
    static LARGE_INTEGER frequency;
    LARGE_INTEGER now;
    if (frequency.QuadPart == 0) QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)frequency.QuadPart;
}

static int Read(uintptr_t at, void* out, size_t size) { return g_api->read(at, out, size) == 0; }

/* Sets a button's value and, when given, its edge bit. */
static void Button(uintptr_t device, uint32_t index, float value, uint32_t edgeOffset)
{
    uintptr_t values = 0, edges = 0;
    uint32_t count = 0;
    if (!Read(device + 0x30, &count, sizeof count) || index >= count || !Read(device + 0x38, &values, sizeof values) || !values) return;
    ((float*)values)[index] = value;
    if (edgeOffset && Read(device + edgeOffset, &edges, sizeof edges) && edges) ((uint32_t*)edges)[index >> 5] |= 1u << (index & 31);
}

static void Apply(uintptr_t device, int mouse)
{
    double now = Now();
    AcquireSRWLockExclusive(&g_lock);
    for (int i = 0; i < MAX_HOLDS; i++)
    {
        Hold* h = &g_holds[i];
        if (!h->used || h->mouse != mouse) continue;
        if (now < h->until)
        {
            Button(device, h->index, 1.0f, h->started ? 0 : 0xD0);
            h->started = 1;
        }
        else
        {
            Button(device, h->index, 0.0f, h->started ? 0xE8 : 0);
            h->used = 0;
        }
    }
    if (mouse)
    {
        double dt = g_lastMouse > 0 ? now - g_lastMouse : 0;
        g_lastMouse = now;
        if (dt > 0.1) dt = 0.1;
        uintptr_t axes = 0;
        if (now < g_lookUntil && Read(device + 0x138, &axes, sizeof axes) && axes)
        {
            float* motion = (float*)axes;
            motion[0] += (float)(g_lookX * dt);
            motion[1] += (float)(g_lookY * dt);
        }
    }
    ReleaseSRWLockExclusive(&g_lock);
}

static uintptr_t VTable(uintptr_t device)
{
    uintptr_t table = 0;
    return Read(device, &table, sizeof table) ? table : 0;
}

static void KeyboardDetour(uintptr_t device)
{
    g_keyboardUpdate(device);
    if (VTable(device) == g_exe + KEYBOARD_VTABLE) Apply(device, 0);
}

static void MouseDetour(uintptr_t device)
{
    g_mouseUpdate(device);
    if (VTable(device) == g_exe + MOUSE_VTABLE) Apply(device, 1);
}

static int Take(const char* command, double now)
{
    char verb[16];
    double a = 0, b = 0, c = 0;
    int n = sscanf(command, " %15s %lf %lf %lf", verb, &a, &b, &c);
    if (n < 1) return 0;
    if (!strcmp(verb, "clear"))
    {
        for (int i = 0; i < MAX_HOLDS; i++) g_holds[i].until = 0;
        g_lookUntil = 0;
        return 1;
    }
    if (!strcmp(verb, "look") && n == 4 && c > 0 && c < 600)
    {
        g_lookX = a;
        g_lookY = b;
        g_lookUntil = now + c;
        return 1;
    }
    int mouse = !strcmp(verb, "mouse");
    if ((mouse || !strcmp(verb, "key")) && n == 3 && a >= 0 && a < 512 && b > 0 && b < 600)
        for (int i = 0; i < MAX_HOLDS; i++)
        {
            Hold* h = &g_holds[i];
            if (h->used && (h->mouse != mouse || h->index != (uint32_t)a)) continue;
            if (!h->used) { h->used = 1; h->started = 0; h->mouse = mouse; h->index = (uint32_t)a; }
            h->until = now + b;
            return 1;
        }
    return -1;
}

__declspec(dllexport) int harness_input(const char* commands)
{
    if (commands == NULL || g_keyboardUpdate == NULL || g_mouseUpdate == NULL) return -1;
    char copy[512];
    snprintf(copy, sizeof copy, "%s", commands);
    double now = Now();
    int taken = 0;
    AcquireSRWLockExclusive(&g_lock);
    for (char* part = strtok(copy, ";"); part != NULL; part = strtok(NULL, ";"))
    {
        int r = Take(part, now);
        if (r < 0) { taken = -1; break; }
        taken += r;
    }
    ReleaseSRWLockExclusive(&g_lock);
    if (g_logged++ < 200) g_api->log(g_mod, "input: %s -> %d", commands, taken);
    return taken;
}

static void OnGameLoaded(HfMod* mod, HfEvent event, void* user)
{
    size_t size = 0;
    g_exe = g_api->module_base("helldivers2.exe", &size);
    if (!g_api->code_matches(g_exe + KEYBOARD_UPDATE, "40 53 48 83 EC 20 4C 8D 81 38 02 00 00 C6 81 F9 00 00 00 01") ||
        !g_api->code_matches(g_exe + MOUSE_UPDATE, "40 53 56 48 83 EC 38 48 8B D9 48 89 6C 24 60 48 81 C1 48 02 00 00"))
    {
        g_api->log(mod, "input: the controllers' updates do not match build 25480438; no test input");
        return;
    }
    if (g_api->hook_ex(mod, g_exe + KEYBOARD_UPDATE, (void*)KeyboardDetour, (void**)&g_keyboardUpdate, 0) ||
        g_api->hook_ex(mod, g_exe + MOUSE_UPDATE, (void*)MouseDetour, (void**)&g_mouseUpdate, 0))
    {
        g_keyboardUpdate = g_mouseUpdate = NULL;
        g_api->log(mod, "input: could not hook the controllers' updates");
        return;
    }
    g_api->log(mod, "input: ready (harness_input)");
}

HF_MOD_EXPORT int hellforge_mod_init(const HfApi* api, HfMod* mod)
{
    if (api == NULL || mod == NULL || api->version < 13u) return 1;
    g_api = api;
    g_mod = mod;
    if (api->on(mod, HF_EVENT_GAME_LOADED, OnGameLoaded, NULL) != 0) api->log(mod, "input: no GAME_LOADED");
    return 0;
}

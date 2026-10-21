// In-game check of HORUS Script Hook APIs that need the running game (installed only with
// `scripts/koko/gta_mods.sh install --api-probe`; a plain install removes it again). Loaded by the
// ASI loader like any plugin, it registers one script through ScriptHookV.dll's exports and writes
// horus_api_probe.log next to itself, ending with "PASS" or "FAIL".
//
// worldGetAllPickups: 15 s after story mode starts, CREATE_PICKUP places a health pickup 3 m from
// the player. Once GET_PICKUP_OBJECT names its object, the probe checks that worldGetAllPickups
// lists that handle (the pickup pool holds pickup objects, CPickup). It then removes the pickup and
// checks that the handle is gone from the list.
//
// createTexture: writes a 37x23 RGBA PNG (horus_api_probe.png, independent encoder in test_png.hpp)
// and loads it twice, expecting consecutive ids >= 0, then a missing file, expecting -1. The hook's
// own log shows each texture's size when decoded and again when uploaded to the game's device.
//
// drawTexture: writes a 32x32 PNG (horus_api_probe_draw.png: magenta, top-left quadrant yellow) and
// draws it every frame for 20 s as instance 0 with time 3000 ms, size 0.2 x 0.2 with the screen's
// aspect ratio as height scale (a square 20% of the screen width), centred at (0.25, 0.30), then
// stops calling it for 20 s. The log gives the first and last call to the millisecond; whether the
// texture is on screen, where, and when it goes is read from captured frames (gta/hook/README.md).

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "hook_core.hpp"
#include "test_png.hpp"

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

using ScriptRegisterFn = void (*)(HMODULE, void (*)());
using ScriptWaitFn = void (*)(DWORD);
using NativeInitFn = void (*)(uint64_t);
using NativePush64Fn = void (*)(uint64_t);
using NativeCallFn = uint64_t* (*)();
using WorldGetAllFn = int (*)(int*, int);
using CreateTextureFn = int (*)(const char*);
using DrawTextureFn = void (*)(int, int, int, int, float, float, float, float, float, float, float, float, float,
                               float, float, float);

struct Api {
    ScriptRegisterFn script_register;
    ScriptWaitFn wait;
    NativeInitFn native_init;
    NativePush64Fn push;
    NativeCallFn call;
    WorldGetAllFn get_all_pickups;
    WorldGetAllFn get_all_objects;
    CreateTextureFn create_texture;
    DrawTextureFn draw_texture;
} api;

HMODULE g_module = nullptr;
FILE* g_log = nullptr;
std::string g_dir;  // this plugin's folder (the game folder), with a trailing backslash

void log(const char* fmt, ...) {
    if (!g_log) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    std::fprintf(g_log, "[%02u:%02u:%02u.%03u] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list args;
    va_start(args, fmt);
    std::vfprintf(g_log, fmt, args);
    va_end(args);
    std::fputc('\n', g_log);
    std::fflush(g_log);
}

// Natives by their original (Legacy) hashes; the hook translates them through its crossmap.
constexpr uint64_t PLAYER_PED_ID = 0xD80958FC74E988A6;
constexpr uint64_t GET_ENTITY_COORDS = 0x3FEF770D40960D5A;
constexpr uint64_t DOES_ENTITY_EXIST = 0x7239B21A38F536BA;
constexpr uint64_t CREATE_PICKUP = 0xFBA08C503DD5FA58;
constexpr uint64_t GET_PICKUP_OBJECT = 0x5099BC55630B25AE;
constexpr uint64_t DOES_PICKUP_EXIST = 0xAFC1CA75AD4074D1;
constexpr uint64_t REMOVE_PICKUP = 0x3288D8ACAECD2AB2;
constexpr uint64_t GET_ENTITY_MODEL = 0x9F47B058362C84B5;
constexpr uint64_t IS_ENTITY_AN_OBJECT = 0x8D68C8FD0FACA94E;
constexpr uint64_t GET_ASPECT_RATIO = 0xF1307EF624A80D87;
constexpr uint64_t GET_FRAME_COUNT = 0xFC8202EFC642E6F2;

uint64_t arg(int value) { return static_cast<uint64_t>(static_cast<uint32_t>(value)); }
uint64_t arg(float value) {
    uint32_t bits;
    std::memcpy(&bits, &value, 4);
    return bits;
}

template <typename... Args>
uint64_t* invoke(uint64_t hash, Args... args) {
    api.native_init(hash);
    (api.push(arg(args)), ...);
    return api.call();
}

int as_int(const uint64_t* result) { return static_cast<int>(static_cast<uint32_t>(result[0])); }
float as_float(const uint64_t* slot) {
    float value;
    std::memcpy(&value, slot, 4);
    return value;
}

std::vector<int> all(WorldGetAllFn get_all) {
    std::vector<int> handles(2048);
    handles.resize(static_cast<size_t>(std::max(0, get_all(handles.data(), static_cast<int>(handles.size())))));
    return handles;
}

bool contains(const std::vector<int>& handles, int handle) {
    return std::find(handles.begin(), handles.end(), handle) != handles.end();
}

// Logs each handle (existence, object or not, model, position); true when every one is an existing
// object, i.e. the list holds live pickup objects and no garbage from a misread pool.
bool describe(const char* what, const std::vector<int>& handles) {
    log("%s: %zu handle(s)", what, handles.size());
    bool all_objects = true;
    for (size_t i = 0; i < handles.size(); ++i) {
        const int h = handles[i];
        const bool exists = as_int(invoke(DOES_ENTITY_EXIST, h)) != 0;
        const bool object = exists && as_int(invoke(IS_ENTITY_AN_OBJECT, h)) != 0;
        all_objects &= object;
        if (i >= 16) continue;
        const uint64_t* c = exists ? invoke(GET_ENTITY_COORDS, h, 0) : nullptr;
        log("  %d: exists %d, object %d, model 0x%08X, at (%.2f, %.2f, %.2f)", h, exists, object,
            exists ? static_cast<uint32_t>(*invoke(GET_ENTITY_MODEL, h)) : 0u, c ? as_float(c) : 0.0f,
            c ? as_float(c + 1) : 0.0f, c ? as_float(c + 2) : 0.0f);
    }
    return all_objects;
}

bool check_pickups() {
    const int ped = as_int(invoke(PLAYER_PED_ID));
    const uint64_t* c = invoke(GET_ENTITY_COORDS, ped, 1);
    const float x = as_float(c), y = as_float(c + 1), z = as_float(c + 2);
    log("player ped %d at (%.2f, %.2f, %.2f)", ped, x, y, z);

    const std::vector<int> before = all(api.get_all_pickups);
    const bool before_ok = describe("worldGetAllPickups before", before);

    const int type = static_cast<int>(horus::hook::joaat("PICKUP_HEALTH_STANDARD"));
    const int pickup = as_int(invoke(CREATE_PICKUP, type, x + 3.0f, y, z, 0, 0, 1, 0));
    log("CREATE_PICKUP(PICKUP_HEALTH_STANDARD) -> pickup %d, DOES_PICKUP_EXIST %d", pickup,
        as_int(invoke(DOES_PICKUP_EXIST, pickup)));
    if (!pickup) return false;

    int object = 0;
    for (int i = 0; i < 100 && !object; ++i) {
        const int candidate = as_int(invoke(GET_PICKUP_OBJECT, pickup));
        if (candidate && as_int(invoke(DOES_ENTITY_EXIST, candidate))) object = candidate;
        else api.wait(100);
    }
    log("GET_PICKUP_OBJECT -> %d", object);

    const std::vector<int> with = all(api.get_all_pickups);
    const std::vector<int> objects = all(api.get_all_objects);
    const bool with_ok = describe("worldGetAllPickups with the pickup", with);
    log("  contains the pickup object %d: %s; contains the CREATE_PICKUP handle %d: %s; object also in "
        "worldGetAllObjects: %s",
        object, contains(with, object) ? "yes" : "no", pickup, contains(with, pickup) ? "yes" : "no",
        contains(objects, object) ? "yes" : "no");

    invoke(REMOVE_PICKUP, pickup);
    api.wait(2000);
    const std::vector<int> after = all(api.get_all_pickups);
    log("REMOVE_PICKUP, 2 s later: DOES_PICKUP_EXIST %d, contains %d: %s", as_int(invoke(DOES_PICKUP_EXIST, pickup)),
        object, contains(after, object) ? "yes" : "no");
    const bool after_ok = describe("worldGetAllPickups after", after);
    return object != 0 && contains(with, object) && !contains(before, object) && !contains(after, object) &&
           before_ok && with_ok && after_ok;
}

bool check_textures() {
    const std::string png = g_dir + "horus_api_probe.png";
    if (!horus::hook::test::write_png(png, 37, 23, horus::hook::test::pattern_rgba(37, 23))) {
        log("cannot write %s", png.c_str());
        return false;
    }
    const int first = api.create_texture(png.c_str());
    const int second = api.create_texture(png.c_str());
    const std::string missing = g_dir + "horus_api_probe_missing.png";
    const int bad = api.create_texture(missing.c_str());
    log("createTexture(%s) -> %d, again -> %d; createTexture(%s) -> %d", png.c_str(), first, second, missing.c_str(),
        bad);
    api.wait(2000);  // a few Presents: the hook uploads both textures (see ScriptHookV.log)
    return first >= 0 && second == first + 1 && bad == -1;
}

bool check_draw() {
    const std::string png = g_dir + "horus_api_probe_draw.png";
    std::vector<uint8_t> rgba(32 * 32 * 4);
    for (int y = 0; y < 32; ++y) {
        for (int x = 0; x < 32; ++x) {
            const bool yellow = x < 16 && y < 16;
            uint8_t* p = &rgba[(y * 32 + x) * 4];
            p[0] = 255, p[1] = yellow ? 255 : 0, p[2] = yellow ? 0 : 255, p[3] = 255;
        }
    }
    if (!horus::hook::test::write_png(png, 32, 32, rgba)) {
        log("cannot write %s", png.c_str());
        return false;
    }
    const int id = api.create_texture(png.c_str());
    if (id < 0) return false;
    api.wait(500);
    float aspect;
    const uint64_t* result = invoke(GET_ASPECT_RATIO, 0);
    std::memcpy(&aspect, result, 4);
    log("drawTexture: texture %d, aspect ratio %.4f; drawing at (0.25, 0.30), size 0.2, time 3000 ms for 20 s", id,
        aspect);
    const int first_frame = as_int(invoke(GET_FRAME_COUNT));
    log("drawTexture: first call (frame %d)", first_frame);
    const ULONGLONG start = GetTickCount64();
    int calls = 0;
    while (GetTickCount64() - start < 20000) {
        api.draw_texture(id, 0, 0, 3000, 0.2f, 0.2f, 0.5f, 0.5f, 0.25f, 0.30f, 0.0f, aspect, 1.0f, 1.0f, 1.0f, 1.0f);
        ++calls;
        api.wait(0);
    }
    log("drawTexture: last call (frame %d), %d calls; not calling for 20 s", as_int(invoke(GET_FRAME_COUNT)), calls);
    api.wait(20000);
    log("drawTexture: 20 s without calls are over");
    return calls > 0;
}

void probe_main() {
    log("probe script started; waiting 15 s for the world");
    api.wait(15000);
    log("createTexture: %s", check_textures() ? "PASS" : "FAIL");
    log("worldGetAllPickups: %s", check_pickups() ? "PASS" : "FAIL");
    log("drawTexture: calls %s (placement and expiry: captured frames)", check_draw() ? "done" : "FAILED");
    log("done");
}

bool resolve() {
    // A plugin that imports ScriptHookV.dll gets it loaded first; LoadLibrary does the same here.
    HMODULE shv = LoadLibraryA("ScriptHookV.dll");
    if (!shv) return false;
    auto get = [&](const char* name) { return reinterpret_cast<void*>(GetProcAddress(shv, name)); };
    api.script_register = reinterpret_cast<ScriptRegisterFn>(get("?scriptRegister@@YAXPEAUHINSTANCE__@@P6AXXZ@Z"));
    api.wait = reinterpret_cast<ScriptWaitFn>(get("?scriptWait@@YAXK@Z"));
    api.native_init = reinterpret_cast<NativeInitFn>(get("?nativeInit@@YAX_K@Z"));
    api.push = reinterpret_cast<NativePush64Fn>(get("?nativePush64@@YAX_K@Z"));
    api.call = reinterpret_cast<NativeCallFn>(get("?nativeCall@@YAPEA_KXZ"));
    api.get_all_pickups = reinterpret_cast<WorldGetAllFn>(get("?worldGetAllPickups@@YAHPEAHH@Z"));
    api.get_all_objects = reinterpret_cast<WorldGetAllFn>(get("?worldGetAllObjects@@YAHPEAHH@Z"));
    api.create_texture = reinterpret_cast<CreateTextureFn>(get("?createTexture@@YAHPEBD@Z"));
    api.draw_texture = reinterpret_cast<DrawTextureFn>(get("?drawTexture@@YAXHHHHMMMMMMMMMMMM@Z"));
    return api.script_register && api.wait && api.native_init && api.push && api.call && api.get_all_pickups &&
           api.get_all_objects && api.create_texture && api.draw_texture;
}

}  // namespace

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    g_module = instance;
    char path[MAX_PATH];
    const DWORD n = GetModuleFileNameA(instance, path, MAX_PATH);
    if (n && n < MAX_PATH) {
        char* slash = std::strrchr(path, '\\');
        if (slash) slash[1] = '\0';
        g_dir = slash ? path : "";
        g_log = std::fopen((g_dir + "horus_api_probe.log").c_str(), "w");
    }
    if (!resolve()) {
        log("FAIL: ScriptHookV.dll or one of its exports is missing");
        return TRUE;
    }
    api.script_register(g_module, probe_main);
    log("registered");
    return TRUE;
}

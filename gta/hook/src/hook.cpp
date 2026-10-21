// HORUS Script Hook: an open implementation of the Script Hook V plugin API for GTA V Enhanced.
//
// Built as ScriptHookV.dll, so ASI plugins written against the Script Hook V SDK (Native Trainer,
// ScriptHookVDotNet Enhanced and the .NET scripts it hosts) load it unchanged. It works from runtime
// memory patterns instead of a per-build table, so a game hotfix only breaks it when one of the
// patterns below stops matching.
//
// How it works:
//   * Natives: Enhanced uses the original native hashes. A hash becomes a handler by passing a
//     one-entry fake rage::scrProgram through the game's own InitNativeTables, which rewrites the
//     program's native table from hashes to handler addresses.
//   * Scripts: every registered script main runs in its own fiber. After the game's
//     RunScriptThreads, the hook makes the persistent story-mode script thread ("main_persistent")
//     the current script thread in the game's TLS context and switches to each script fiber whose
//     wait has expired; scriptWait switches back. Natives therefore run inside a script context.
//   * Online: if a network session starts, the game is closed, as Script Hook V does.
//
// Patterns and structure layouts for Enhanced follow YimMenuV2 (github.com/YimMenu/YimMenuV2,
// GPL-2.0); no code is taken from it. Checked against GTA V Enhanced 1.0.1158.16.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <intrin.h>
#include <MinHook.h>

#include "dxgi_probe.hpp"
#include "hook_core.hpp"
#include "swapchain_queues.hpp"
#include "texture.hpp"
#include "texture_draw.hpp"

#include <algorithm>
#include <atomic>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

using horus::hook::joaat;
using horus::hook::rip;

// ---------------------------------------------------------------------------------------------
// Logging (ScriptHookV.log next to the DLL, like Script Hook V)

std::mutex g_log_mutex;
FILE* g_log = nullptr;
std::wstring g_module_dir;  // with trailing separator

void log(const char* fmt, ...) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    if (!g_log) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    std::fprintf(g_log, "[%02u:%02u:%02u] ", t.wHour, t.wMinute, t.wSecond);
    va_list args;
    va_start(args, fmt);
    std::vfprintf(g_log, fmt, args);
    va_end(args);
    std::fputc('\n', g_log);
    std::fflush(g_log);
}

// Log a message only the first time `key` is seen.
void log_once(const std::string& key, const char* message) {
    static std::mutex mutex;
    static std::unordered_set<std::string> seen;
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (!seen.insert(key).second) return;
    }
    log("%s", message);
}

// ---------------------------------------------------------------------------------------------
// Pattern scanning over the game image (only committed, readable pages: parts of the image are
// unpacked at runtime)

bool readable(DWORD protect) {
    if (protect & (PAGE_GUARD | PAGE_NOACCESS)) return false;
    return protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                      PAGE_EXECUTE_WRITECOPY);
}

// Address of the single match of `text` in the image, or nullptr when it has zero or several matches.
uint8_t* find_unique(const char* text) {
    const horus::hook::Pattern p = horus::hook::parse_pattern(text);
    auto* base = reinterpret_cast<uint8_t*>(GetModuleHandleW(nullptr));
    const auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    const auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    uint8_t* const end = base + nt->OptionalHeader.SizeOfImage;
    const uint8_t* found = nullptr;
    int count = 0;
    for (uint8_t* region = base; region < end;) {
        MEMORY_BASIC_INFORMATION info;
        if (!VirtualQuery(region, &info, sizeof(info))) break;
        uint8_t* const region_end = std::min(end, static_cast<uint8_t*>(info.BaseAddress) + info.RegionSize);
        if (info.State == MEM_COMMIT && readable(info.Protect)) count += horus::hook::find_pattern(region, region_end, p, &found);
        region = region_end;
    }
    return count == 1 ? const_cast<uint8_t*>(found) : nullptr;
}

// ---------------------------------------------------------------------------------------------
// Game structures (Enhanced layouts)

struct alignas(16) Vector3Source {  // rage::vector3
    float x, y, z, w;
};
struct ScrVector {  // rage::scrVector: three floats, each padded to 8 bytes
    float x;
    uint32_t pad_x;
    float y;
    uint32_t pad_y;
    float z;
    uint32_t pad_z;
};

struct NativeContext {  // rage::scrNativeCallContext
    uint64_t* return_value;
    uint32_t arg_count;
    uint64_t* args;
    int32_t vector_ref_count;
    ScrVector* vector_ref_targets[4];
    Vector3Source vector_ref_sources[4];
};
static_assert(sizeof(NativeContext) == 0x80, "scrNativeCallContext is 0x80 bytes");
static_assert(offsetof(NativeContext, args) == 0x10, "args at 0x10");
static_assert(offsetof(NativeContext, vector_ref_sources) == 0x40, "vector sources at 0x40");

using NativeHandler = void (*)(NativeContext*);

constexpr size_t kProgramSize = 0x80;            // rage::scrProgram
constexpr size_t kProgramNativeCount = 0x2C;     // uint32
constexpr size_t kProgramNativeEntries = 0x40;   // NativeHandler* (hashes before InitNativeTables)
constexpr size_t kThreadId = 0x08;               // scrThread::m_Context.m_ThreadId
constexpr size_t kThreadScriptHash = 0x150;      // scrThread::m_ScriptHash (joaat of the name)
constexpr size_t kThreadScriptName = 0x154;      // scrThread::m_ScriptName, char[64]
constexpr size_t kTlsCurrentThread = 0x7A0;      // rage::tlsContext::m_CurrentScriptThread
constexpr size_t kTlsThreadActive = 0x7A8;       // rage::tlsContext::m_ScriptThreadActive

struct ThreadArray {  // rage::atArray<scrThread*>
    uint8_t** data;
    uint16_t size;
    uint16_t capacity;
};

struct PoolEncryption {
    bool is_set;
    uint64_t first;
    uint64_t second;
};

struct BasePool {  // rage::fwBasePool
    void* vtable;
    uintptr_t entries;
    uint8_t* flags;
    uint32_t size;
    uint32_t item_size;
};

struct VehiclePool {  // rage::fwVehiclePool
    void* vtable;
    void** items;
    uint32_t size;
    char pad[36];
    uint32_t* bits;
};

template <typename T>
T field(uint8_t* object, size_t offset) {
    T value;
    std::memcpy(&value, object + offset, sizeof(T));
    return value;
}

// The game's rage::tlsContext: slot 0 of the TLS array the TEB points to at gs:0x58.
uint8_t* tls_context() {
    uint64_t tls_array;
    asm volatile("movq %%gs:0x58, %0" : "=r"(tls_array));
    return *reinterpret_cast<uint8_t**>(tls_array);
}

// GetCurrentFiber() without MinGW's inline __readgsqword (GCC flags it with a false -Warray-bounds).
LPVOID current_fiber() {
    LPVOID fiber;
    asm volatile("movq %%gs:0x20, %0" : "=r"(fiber));
    return fiber;
}

// ---------------------------------------------------------------------------------------------
// Game pointers

// Rewrites a program's native table from hashes to handlers; false when a hash is unknown (its entry
// then points at the game's "unregistered native" handler).
using InitNativeTablesFn = bool (*)(void* program);
using RunScriptThreadsFn = bool (*)(uint32_t ops);
using HandleToPtrFn = void* (*)(int handle);
using PtrToHandleFn = int (*)(void* pointer);

struct GamePointers {
    const char* version = nullptr;
    ThreadArray* script_threads = nullptr;
    InitNativeTablesFn init_native_tables = nullptr;
    void* run_script_threads = nullptr;
    bool* is_session_started = nullptr;
    int64_t** script_globals = nullptr;
    HandleToPtrFn handle_to_ptr = nullptr;
    PtrToHandleFn ptr_to_handle = nullptr;
    PoolEncryption* ped_pool = nullptr;
    PoolEncryption* object_pool = nullptr;
    VehiclePool*** vehicle_pool = nullptr;
    horus::hook::EncryptedPoolRef pickup_pool;  // rage::fwBasePool of CPickup (pickup objects)
};

GamePointers g;

// Scan once; true when every required pointer was found. Optional ones only disable their API.
bool find_pointers(bool log_misses) {
    static bool s_log = false;  // read by the PickupPool apply below (a captureless lambda)
    s_log = log_misses;
    struct Entry {
        const char* name;
        const char* pattern;
        bool required;
        void (*apply)(uint8_t*);
    };
    static const Entry entries[] = {
        {"Version", "4C 8D 0D ? ? ? ? 48 8D 5C 24 ? 48 89 D9 48 89 FA", false,
         [](uint8_t* m) { g.version = reinterpret_cast<const char*>(rip(m + 3)); }},
        {"ScriptThreads", "48 8B 05 ? ? ? ? 48 89 34 F8 48 FF C7 48 39 FB 75 97", true,
         [](uint8_t* m) { g.script_threads = reinterpret_cast<ThreadArray*>(rip(m + 3)); }},
        {"InitNativeTables", "EB 2A 0F 1F 40 00 48 8B 54 17 10", true,
         [](uint8_t* m) { g.init_native_tables = reinterpret_cast<InitNativeTablesFn>(m - 0x2A); }},
        {"RunScriptThreads", "BE 40 5D C6 00", true, [](uint8_t* m) { g.run_script_threads = m - 0xA; }},
        {"IsSessionStarted", "0F B6 05 ? ? ? ? 0A 05 ? ? ? ? 75 2A", true,
         [](uint8_t* m) { g.is_session_started = reinterpret_cast<bool*>(rip(m + 3)); }},
        {"ScriptGlobals", "48 8B 8E B8 00 00 00 48 8D 15 ? ? ? ? 49 89 D8", false,
         [](uint8_t* m) { g.script_globals = reinterpret_cast<int64_t**>(rip(m + 10)); }},
        {"HandlesAndPtrs", "0F 1F 84 00 00 00 00 00 89 F8 0F 28 FE 41", false,
         [](uint8_t* m) {
             g.handle_to_ptr = reinterpret_cast<HandleToPtrFn>(rip(m + 0x22));
             g.ptr_to_handle = reinterpret_cast<PtrToHandleFn>(rip(m - 0xB + 1));
         }},
        {"PedPool", "80 79 4B 00 0F 84 F5 00 00 00 48 89 F1", false,
         [](uint8_t* m) { g.ped_pool = reinterpret_cast<PoolEncryption*>(rip(m + 0x18 + 3)); }},
        {"VehiclePool", "48 83 78 18 0D", false,
         [](uint8_t* m) { g.vehicle_pool = reinterpret_cast<VehiclePool***>(rip(m - 0xA + 3)); }},
        {"ObjectPool", "48 8B 04 0A C3 0F B6 05", false,
         [](uint8_t* m) { g.object_pool = reinterpret_cast<PoolEncryption*>(rip(m + 5 + 3)); }},
        {"PickupPool", horus::hook::kPickupPoolPattern, false,
         [](uint8_t* m) {
             g.pickup_pool = horus::hook::pickup_pool_ref(m);
             const horus::hook::PoolKey& k = g.pickup_pool.key;
             if (s_log) {
                 log("pickup pool decoder: rol %u, mask 0x%02X, add %u, rol %u", k.rotate_second, k.mask, k.add,
                     k.rotate_final);
             }
         }},
    };
    bool all_required = true;
    for (const Entry& entry : entries) {
        uint8_t* match = find_unique(entry.pattern);
        if (match) {
            entry.apply(match);
        } else {
            all_required &= !entry.required;
            if (log_misses) log("%s pattern %s not found", entry.required ? "FATAL:" : "WARNING:", entry.name);
        }
    }
    return all_required;
}

// ---------------------------------------------------------------------------------------------
// Natives

// Plugins call natives by their original (Legacy) hashes; Enhanced registers them under different
// hashes. ScriptHookV.crossmap.txt ("0xORIGINAL,0xENHANCED" per line, parsed in hook_core.hpp)
// translates; a hash without an entry is looked up unchanged.
std::unordered_map<uint64_t, uint64_t> g_crossmap;

bool load_crossmap(const std::wstring& path) {
    FILE* file = _wfopen(path.c_str(), L"rb");
    if (!file) return false;
    std::string text;
    char buffer[65536];
    for (size_t n; (n = std::fread(buffer, 1, sizeof(buffer), file)) > 0;) text.append(buffer, n);
    std::fclose(file);
    horus::hook::Crossmap map = horus::hook::parse_crossmap(text);
    if (!map.malformed_lines.empty()) {
        std::string lines;
        for (size_t i = 0; i < map.malformed_lines.size() && i < 10; ++i) {
            lines += (i ? ", " : "") + std::to_string(map.malformed_lines[i]);
        }
        log("WARNING: crossmap: skipped %zu malformed line(s): %s%s", map.malformed_lines.size(), lines.c_str(),
            map.malformed_lines.size() > 10 ? ", ..." : "");
    }
    g_crossmap = std::move(map.entries);
    return !g_crossmap.empty();
}

// The game's handler for an Enhanced hash, or nullptr if this build does not register it.
NativeHandler lookup_handler(uint64_t enhanced_hash) {
    alignas(16) uint8_t program[kProgramSize] = {};
    uint64_t entry = enhanced_hash;
    const uint32_t count = 1;
    uint64_t* entries = &entry;
    std::memcpy(program + kProgramNativeCount, &count, sizeof(count));
    std::memcpy(program + kProgramNativeEntries, &entries, sizeof(entries));
    return g.init_native_tables(program) ? reinterpret_cast<NativeHandler>(entry) : nullptr;
}

std::mutex g_native_mutex;
std::unordered_map<uint64_t, NativeHandler> g_handlers;  // by original hash

NativeHandler resolve_native(uint64_t hash) {
    std::lock_guard<std::mutex> lock(g_native_mutex);
    auto it = g_handlers.find(hash);
    if (it != g_handlers.end()) return it->second;
    auto mapped = g_crossmap.find(hash);
    NativeHandler handler = lookup_handler(mapped != g_crossmap.end() ? mapped->second : hash);
    g_handlers.emplace(hash, handler);
    return handler;
}

// Once, on the game thread: how much of the crossmap this build registers, and which entries it
// does not (original hash -> Enhanced hash, sorted by original hash; at most 64 listed).
void log_crossmap_coverage() {
    std::vector<std::pair<uint64_t, uint64_t>> missing;
    for (const auto& entry : g_crossmap) {
        if (!lookup_handler(entry.second)) missing.emplace_back(entry.first, entry.second);
    }
    log("crossmap: %zu of %zu natives registered in this build", g_crossmap.size() - missing.size(),
        g_crossmap.size());
    if (missing.empty()) return;
    std::sort(missing.begin(), missing.end());
    constexpr size_t kMaxListed = 64;
    std::string list;
    char item[48];
    for (size_t i = 0; i < missing.size() && i < kMaxListed; ++i) {
        std::snprintf(item, sizeof(item), "%s0x%016llX->0x%016llX", i ? ", " : "",
                      static_cast<unsigned long long>(missing[i].first),
                      static_cast<unsigned long long>(missing[i].second));
        list += item;
    }
    log("crossmap: %zu not registered (original->enhanced): %s%s", missing.size(), list.c_str(),
        missing.size() > kMaxListed ? ", ..." : "");
}

struct CallState {
    NativeContext context{};
    uint64_t args[32] = {};
    uint64_t result[4] = {};
    uint64_t hash = 0;
};

thread_local CallState t_call;

// ---------------------------------------------------------------------------------------------
// Scripts

struct Script {
    HMODULE module;
    void (*entry)();
    LPVOID fiber = nullptr;
    ULONGLONG wake_at = 0;
    bool finished = false;
    bool removed = false;
    uint64_t switches = 0;  // fiber entries since (re)start
};

std::mutex g_scripts_mutex;
std::vector<std::shared_ptr<Script>> g_scripts;
LPVOID g_main_fiber = nullptr;
std::atomic<Script*> g_running{nullptr};
std::atomic<ULONGLONG> g_running_since{0};
HANDLE g_main_thread = nullptr;  // the game thread that runs script fibers, for the watchdog
std::atomic<DWORD> g_main_thread_id{0};
uint8_t* g_host_thread = nullptr;
uint32_t g_host_thread_id = 0;
std::atomic<bool> g_ready{false};
// Script ticks (RunScriptThreads calls), for the heartbeat and the no-tick watchdog.
std::atomic<ULONGLONG> g_last_tick{0};
constexpr ULONGLONG kHeartbeatMs = 60000;
constexpr ULONGLONG kNoTickMs = 10000;

void WINAPI script_fiber(LPVOID parameter) {
    auto* script = static_cast<Script*>(parameter);
    script->entry();
    script->finished = true;
    log("script main %p returned", reinterpret_cast<void*>(script->entry));
    for (;;) SwitchToFiber(g_main_fiber);  // a fiber must never return
}

void stop_fiber(Script& script) {
    if (script.fiber) DeleteFiber(script.fiber);
    script.fiber = nullptr;
    script.finished = false;
    script.wake_at = 0;
    script.switches = 0;
}

uint8_t* find_script_thread(uint32_t hash) {
    ThreadArray* threads = g.script_threads;
    for (uint16_t i = 0; threads && threads->data && i < threads->size; ++i) {
        uint8_t* thread = threads->data[i];
        if (thread && field<uint32_t>(thread, kThreadId) != 0 && field<uint32_t>(thread, kThreadScriptHash) == hash) {
            return thread;
        }
    }
    return nullptr;
}

void log_script_threads() {
    std::string names;
    ThreadArray* threads = g.script_threads;
    for (uint16_t i = 0; threads && threads->data && i < threads->size; ++i) {
        uint8_t* thread = threads->data[i];
        if (!thread || field<uint32_t>(thread, kThreadId) == 0) continue;
        char name[65] = {};
        std::memcpy(name, thread + kThreadScriptName, 64);
        if (!names.empty()) names += ", ";
        names += name;
    }
    log("running script threads: %s", names.empty() ? "(none)" : names.c_str());
}

// Keyboard: the game window's WndProc is replaced once it exists.

using KeyboardHandler = void (*)(DWORD, WORD, BYTE, BOOL, BOOL, BOOL, BOOL);
std::mutex g_keyboard_mutex;
std::vector<KeyboardHandler> g_keyboard_handlers;
WNDPROC g_original_wndproc = nullptr;
HWND g_window = nullptr;

LRESULT CALLBACK game_wndproc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_KEYDOWN || message == WM_KEYUP || message == WM_SYSKEYDOWN || message == WM_SYSKEYUP) {
        std::vector<KeyboardHandler> handlers;
        {
            std::lock_guard<std::mutex> lock(g_keyboard_mutex);
            handlers = g_keyboard_handlers;
        }
        const BOOL up = message == WM_KEYUP || message == WM_SYSKEYUP;
        for (KeyboardHandler handler : handlers) {
            handler(static_cast<DWORD>(wparam), LOWORD(lparam), static_cast<BYTE>((lparam >> 16) & 0xFF),
                    (lparam >> 24) & 1, (lparam >> 29) & 1, (lparam >> 30) & 1, up);
        }
    }
    return CallWindowProcW(g_original_wndproc, window, message, wparam, lparam);
}

BOOL CALLBACK find_game_window(HWND window, LPARAM result) {
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    wchar_t class_name[64] = {};
    GetClassNameW(window, class_name, 64);
    if (pid == GetCurrentProcessId() && IsWindowVisible(window) && !std::wcscmp(class_name, L"sgaWindow")) {
        *reinterpret_cast<HWND*>(result) = window;
        return FALSE;
    }
    return TRUE;
}

void hook_game_window() {
    if (g_window) return;
    HWND window = nullptr;
    EnumWindows(find_game_window, reinterpret_cast<LPARAM>(&window));
    if (!window) return;
    g_original_wndproc = reinterpret_cast<WNDPROC>(
        SetWindowLongPtrW(window, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(&game_wndproc)));
    g_window = window;
    log("keyboard: hooked game window %p", static_cast<void*>(window));
}

// Every 60 s on the game thread: ticks in the period, the host thread and the scripts' states. A
// gap of more than 10 s since the previous tick is logged when ticks resume.
void heartbeat(ULONGLONG now) {
    static ULONGLONG period_start = 0;
    static uint64_t ticks = 0;
    const ULONGLONG previous = g_last_tick.exchange(now);
    if (previous && now - previous > kNoTickMs) {
        log("script ticks resumed after %llu ms without a tick", static_cast<unsigned long long>(now - previous));
    }
    ++ticks;
    if (!period_start) period_start = now;
    if (now - period_start < kHeartbeatMs) return;
    int running = 0, waiting = 0, finished = 0;
    {
        std::lock_guard<std::mutex> lock(g_scripts_mutex);
        for (const auto& script : g_scripts) {
            if (script->finished) {
                ++finished;
            } else if (script->fiber) {
                ++running;
            } else {
                ++waiting;
            }
        }
    }
    log("heartbeat: %llu ticks in %llu ms, host thread id %u, scripts %d running, %d waiting, %d finished",
        static_cast<unsigned long long>(ticks), static_cast<unsigned long long>(now - period_start), g_host_thread_id,
        running, waiting, finished);
    period_start = now;
    ticks = 0;
}

void drop_textures();

// Runs on the game's main thread right after the game's own script threads.
void tick_scripts() {
    if (*g.is_session_started) {
        log("FATAL: a GTA Online session started; closing the game (mods are story mode only)");
        ExitProcess(0);
    }
    heartbeat(GetTickCount64());
    hook_game_window();

    uint8_t* host = find_script_thread(joaat("main_persistent"));
    const uint32_t host_id = host ? field<uint32_t>(host, kThreadId) : 0;
    if (host != g_host_thread || host_id != g_host_thread_id) {
        log_script_threads();
        std::lock_guard<std::mutex> lock(g_scripts_mutex);
        for (auto& script : g_scripts) stop_fiber(*script);
        drop_textures();  // Script Hook V drops textures when scripts are reloaded
        g_host_thread = host;
        g_host_thread_id = host_id;
        log(host ? "host script thread main_persistent (id %u): starting scripts"
                 : "no main_persistent script thread: scripts paused",
            host_id);
        static bool coverage_logged = false;
        if (host && !coverage_logged) {
            log_crossmap_coverage();
            coverage_logged = true;
        }
    }
    if (!host) return;

    if (!g_main_thread) {
        DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &g_main_thread,
                        THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, 0);
        g_main_thread_id.store(GetCurrentThreadId());
    }
    if (!g_main_fiber) g_main_fiber = IsThreadAFiber() ? current_fiber() : ConvertThreadToFiber(nullptr);
    if (!g_main_fiber) {
        log_once("main-fiber", "FATAL: cannot convert the game thread to a fiber");
        return;
    }

    std::vector<std::shared_ptr<Script>> scripts;
    {
        std::lock_guard<std::mutex> lock(g_scripts_mutex);
        g_scripts.erase(std::remove_if(g_scripts.begin(), g_scripts.end(),
                                       [](const std::shared_ptr<Script>& s) {
                                           if (s->removed) stop_fiber(*s);
                                           return s->removed;
                                       }),
                        g_scripts.end());
        for (auto& script : g_scripts) {
            if (!script->fiber) {
                script->fiber = CreateFiber(0, script_fiber, script.get());
                log("started script main %p", reinterpret_cast<void*>(script->entry));
            }
        }
        scripts = g_scripts;
    }

    uint8_t* tls = tls_context();
    uint8_t* const previous_thread = field<uint8_t*>(tls, kTlsCurrentThread);
    const bool previous_active = field<bool>(tls, kTlsThreadActive);
    std::memcpy(tls + kTlsCurrentThread, &host, sizeof(host));
    tls[kTlsThreadActive] = 1;
    const ULONGLONG now = GetTickCount64();
    for (auto& script : scripts) {
        if (!script->fiber || script->finished || script->removed || now < script->wake_at) continue;
        const bool first = script->switches++ == 0;
        if (first) log("script main %p: first run", reinterpret_cast<void*>(script->entry));
        const ULONGLONG started = GetTickCount64();
        g_running_since.store(started);
        g_running.store(script.get());
        SwitchToFiber(script->fiber);
        g_running.store(nullptr);
        const ULONGLONG ran = GetTickCount64() - started;
        if (first || ran > 1000) {
            log("script main %p: yielded after %llu ms", reinterpret_cast<void*>(script->entry),
                static_cast<unsigned long long>(ran));
        }
    }
    std::memcpy(tls + kTlsCurrentThread, &previous_thread, sizeof(previous_thread));
    tls[kTlsThreadActive] = previous_active;
}

RunScriptThreadsFn g_run_script_threads = nullptr;

bool hook_run_script_threads(uint32_t ops) {
    const bool result = g_run_script_threads(ops);
    if (g_ready.load(std::memory_order_acquire)) tick_scripts();
    return result;
}

// ---------------------------------------------------------------------------------------------
// Textures: createTexture decodes on the calling (script) thread and the next Present uploads to
// the game's D3D12 device; drawTexture records instances that each Present draws into the back
// buffer on the presenting queue, before the frame is presented (and before horus_capture copies it,
// whose Present hook this one wraps).

struct Texture {
    int id;
    horus::hook::Image image;  // pixels until uploaded
    ID3D12Resource* resource = nullptr;
    bool failed = false;
    int srv_slot = -1;  // in g_renderer's heap, once drawn
};

struct DroppedTexture {
    ID3D12Resource* resource;
    int srv_slot;
};

std::mutex g_textures_mutex;  // guards g_textures, g_textures_dropped, g_next_texture_id, g_instances
std::vector<Texture> g_textures;
std::vector<DroppedTexture> g_textures_dropped;  // released at a Present once the GPU is done with them
int g_next_texture_id = 0;
horus::hook::TextureInstances g_instances;
std::atomic<bool> g_textures_pending{false};
horus::hook::TextureUploader g_uploader;  // Present thread only
horus::hook::QuadRenderer g_renderer;     // Present thread only
horus::SwapchainQueues g_queues;          // gta/common/swapchain_queues.hpp

void drop_textures() {
    std::lock_guard<std::mutex> lock(g_textures_mutex);
    g_instances.clear();
    if (g_textures.empty()) return;
    for (Texture& texture : g_textures) {
        if (texture.resource) g_textures_dropped.push_back({texture.resource, texture.srv_slot});
    }
    log("scripts restarted: %zu texture(s) dropped", g_textures.size());
    g_textures.clear();
    g_textures_pending.store(true);
}

// Present thread: release dropped textures and upload new ones.
void process_textures(IDXGISwapChain* swapchain) {
    if (!g_textures_pending.exchange(false)) return;
    ID3D12Resource* buffer = nullptr;
    ID3D12Device* device = nullptr;
    if (FAILED(swapchain->GetBuffer(0, IID_PPV_ARGS(&buffer))) || FAILED(buffer->GetDevice(IID_PPV_ARGS(&device)))) {
        if (buffer) buffer->Release();
        log_once("texture-device", "ERROR: textures: the swap chain has no D3D12 device; textures not uploaded");
        return;
    }
    buffer->Release();
    std::string error;
    const bool ready = g_uploader.device() == device || g_uploader.init(device, error);
    device->Release();
    if (!ready) {
        log("ERROR: textures: %s; textures not uploaded", error.c_str());
        return;
    }
    std::vector<DroppedTexture> dropped;
    std::vector<std::pair<int, horus::hook::Image>> work;
    {
        std::lock_guard<std::mutex> lock(g_textures_mutex);
        dropped.swap(g_textures_dropped);
        for (Texture& texture : g_textures) {
            if (!texture.resource && !texture.failed) work.emplace_back(texture.id, std::move(texture.image));
        }
    }
    if (!dropped.empty()) {
        if (!g_renderer.wait_idle()) {  // the overlay may still read them; try again at the next Present
            std::lock_guard<std::mutex> lock(g_textures_mutex);
            g_textures_dropped.insert(g_textures_dropped.end(), dropped.begin(), dropped.end());
            g_textures_pending.store(true);
            dropped.clear();
        }
        for (const DroppedTexture& texture : dropped) {
            g_renderer.free_srv(texture.srv_slot);
            texture.resource->Release();
        }
    }
    for (auto& [id, image] : work) {
        ID3D12Resource* resource = g_uploader.upload(image, error);
        std::lock_guard<std::mutex> lock(g_textures_mutex);
        auto it = std::find_if(g_textures.begin(), g_textures.end(), [id = id](const Texture& t) { return t.id == id; });
        if (it == g_textures.end()) {  // dropped while uploading
            if (resource) resource->Release();
        } else if (resource) {
            it->resource = resource;
            log("texture %d: %ux%u uploaded to the game's D3D12 device", id, image.width, image.height);
        } else {
            it->failed = true;
            log("ERROR: texture %d: %s", id, error.c_str());
        }
    }
}

// Present thread: draw the live drawTexture instances into the back buffer about to be presented.
// Uploads finished on the CPU side before (TextureUploader waits for its fence), and the overlay
// goes on the presenting queue, so it lands after the game's frame and before the Present.
void draw_textures(IDXGISwapChain* swapchain) {
    {
        std::lock_guard<std::mutex> lock(g_textures_mutex);
        if (g_instances.empty()) return;
    }
    IDXGISwapChain3* swapchain3 = nullptr;
    if (FAILED(swapchain->QueryInterface(IID_PPV_ARGS(&swapchain3)))) {
        log_once("overlay-swapchain", "ERROR: drawTexture: the swap chain is not an IDXGISwapChain3; not drawn");
        return;
    }
    IUnknown* identity = nullptr;
    swapchain->QueryInterface(IID_PPV_ARGS(&identity));
    bool fallback = false;
    ID3D12CommandQueue* queue = g_queues.find(swapchain3, identity, &fallback);
    if (identity) identity->Release();
    ID3D12Resource* back_buffer = nullptr;
    ID3D12Device* device = nullptr;
    if (!queue || FAILED(swapchain3->GetBuffer(swapchain3->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&back_buffer))) ||
        FAILED(back_buffer->GetDevice(IID_PPV_ARGS(&device)))) {
        if (!queue) log_once("overlay-queue", "WARNING: drawTexture: no presenting queue known yet; not drawn");
        if (back_buffer) back_buffer->Release();
        swapchain3->Release();
        return;
    }
    const D3D12_RESOURCE_DESC desc = back_buffer->GetDesc();
    const DXGI_FORMAT format = horus::hook::render_target_format(desc.Format);
    std::string error;
    bool ready = g_renderer.device() == device && g_renderer.format() == format;
    if (!ready) {
        ready = g_renderer.init(device, format, error);  // waits for the old one; its SRVs are gone
        std::lock_guard<std::mutex> lock(g_textures_mutex);
        for (Texture& texture : g_textures) texture.srv_slot = -1;
        if (ready) {
            log("drawTexture: overlay on queue %p%s, back buffer %llux%u format %d", static_cast<void*>(queue),
                fallback ? " (last direct queue of ExecuteCommandLists)" : "",
                static_cast<unsigned long long>(desc.Width), desc.Height, static_cast<int>(format));
        } else {
            log("ERROR: drawTexture: %s; not drawn", error.c_str());
        }
    }
    device->Release();
    std::vector<horus::hook::QuadDraw> quads;
    if (ready) {
        std::lock_guard<std::mutex> lock(g_textures_mutex);
        const uint64_t now = GetTickCount64();
        auto find = [](int id) {
            return std::find_if(g_textures.begin(), g_textures.end(), [id](const Texture& t) { return t.id == id; });
        };
        g_instances.prune(now, [&](int id) { return find(id) != g_textures.end(); });
        for (const horus::hook::TextureInstance& instance : g_instances.draw_order(now)) {
            auto texture = find(instance.id);
            if (!texture->resource) continue;  // not uploaded yet
            if (texture->srv_slot < 0) texture->srv_slot = g_renderer.create_srv(texture->resource);
            if (texture->srv_slot < 0) continue;
            horus::hook::QuadDraw quad{texture->resource, texture->srv_slot, {}, {}};
            horus::hook::texture_quad(instance.layout, static_cast<float>(desc.Width), static_cast<float>(desc.Height),
                                      quad.corners);
            std::memcpy(quad.color, instance.color, sizeof(quad.color));
            quads.push_back(quad);
        }
    }
    if (!quads.empty() && !g_renderer.draw(queue, back_buffer, quads, error)) {
        log("ERROR: drawTexture: %s", error.c_str());
    }
    back_buffer->Release();
    swapchain3->Release();
}

// ---------------------------------------------------------------------------------------------
// IDXGISwapChain::Present callbacks

using PresentCallback = void (*)(void*);
std::mutex g_present_mutex;
std::vector<PresentCallback> g_present_callbacks;

using PresentFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT);
using Present1Fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain1*, UINT, UINT, const DXGI_PRESENT_PARAMETERS*);
PresentFn g_present = nullptr;
Present1Fn g_present1 = nullptr;
thread_local bool t_in_present = false;  // Present may forward to Present1 internally

void run_present_callbacks(void* swapchain, UINT flags) {
    if (t_in_present || (flags & DXGI_PRESENT_TEST)) return;
    process_textures(static_cast<IDXGISwapChain*>(swapchain));
    draw_textures(static_cast<IDXGISwapChain*>(swapchain));
    std::vector<PresentCallback> callbacks;
    {
        std::lock_guard<std::mutex> lock(g_present_mutex);
        callbacks = g_present_callbacks;
    }
    for (PresentCallback callback : callbacks) callback(swapchain);
}

HRESULT STDMETHODCALLTYPE hook_present(IDXGISwapChain* swapchain, UINT sync, UINT flags) {
    run_present_callbacks(swapchain, flags);
    const bool outer = !t_in_present;
    t_in_present = true;
    const HRESULT hr = g_present(swapchain, sync, flags);
    if (outer) t_in_present = false;
    return hr;
}

HRESULT STDMETHODCALLTYPE hook_present1(IDXGISwapChain1* swapchain, UINT sync, UINT flags,
                                        const DXGI_PRESENT_PARAMETERS* params) {
    run_present_callbacks(swapchain, flags);
    const bool outer = !t_in_present;
    t_in_present = true;
    const HRESULT hr = g_present1(swapchain, sync, flags, params);
    if (outer) t_in_present = false;
    return hr;
}

// The swap chain's queue (gta/common/swapchain_queues.hpp) and resizing, which needs the overlay idle.
using ResizeBuffersFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);
using ResizeBuffers1Fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain3*, UINT, UINT, UINT, DXGI_FORMAT, UINT,
                                                     const UINT*, IUnknown* const*);
using CreateSwapChainFn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory*, IUnknown*, DXGI_SWAP_CHAIN_DESC*,
                                                      IDXGISwapChain**);
using CreateSwapChainForHwndFn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory2*, IUnknown*, HWND,
                                                             const DXGI_SWAP_CHAIN_DESC1*,
                                                             const DXGI_SWAP_CHAIN_FULLSCREEN_DESC*, IDXGIOutput*,
                                                             IDXGISwapChain1**);
using ExecuteCommandListsFn = void(STDMETHODCALLTYPE*)(ID3D12CommandQueue*, UINT, ID3D12CommandList* const*);
ResizeBuffersFn g_resize_buffers = nullptr;
ResizeBuffers1Fn g_resize_buffers1 = nullptr;
CreateSwapChainFn g_create_swapchain = nullptr;
CreateSwapChainForHwndFn g_create_swapchain_for_hwnd = nullptr;
ExecuteCommandListsFn g_execute_command_lists = nullptr;

HRESULT STDMETHODCALLTYPE hook_resize_buffers(IDXGISwapChain* swapchain, UINT count, UINT width, UINT height,
                                              DXGI_FORMAT format, UINT flags) {
    g_renderer.wait_idle();
    return g_resize_buffers(swapchain, count, width, height, format, flags);
}

HRESULT STDMETHODCALLTYPE hook_resize_buffers1(IDXGISwapChain3* swapchain, UINT count, UINT width, UINT height,
                                               DXGI_FORMAT format, UINT flags, const UINT* node_masks,
                                               IUnknown* const* queues) {
    g_renderer.wait_idle();
    return g_resize_buffers1(swapchain, count, width, height, format, flags, node_masks, queues);
}

void remember_swapchain(IUnknown* swapchain, IUnknown* device) {
    IUnknown* identity = nullptr;
    if (swapchain && device && SUCCEEDED(swapchain->QueryInterface(IID_PPV_ARGS(&identity)))) {
        if (ID3D12CommandQueue* queue = g_queues.remember(identity, device)) {
            log("swap chain %p presents on queue %p", static_cast<void*>(identity), static_cast<void*>(queue));
        }
        identity->Release();
    }
}

HRESULT STDMETHODCALLTYPE hook_create_swapchain(IDXGIFactory* factory, IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc,
                                                IDXGISwapChain** swapchain) {
    const HRESULT hr = g_create_swapchain(factory, device, desc, swapchain);
    if (SUCCEEDED(hr) && swapchain) remember_swapchain(*swapchain, device);
    return hr;
}

HRESULT STDMETHODCALLTYPE hook_create_swapchain_for_hwnd(IDXGIFactory2* factory, IUnknown* device, HWND hwnd,
                                                         const DXGI_SWAP_CHAIN_DESC1* desc,
                                                         const DXGI_SWAP_CHAIN_FULLSCREEN_DESC* fullscreen,
                                                         IDXGIOutput* output, IDXGISwapChain1** swapchain) {
    const HRESULT hr = g_create_swapchain_for_hwnd(factory, device, hwnd, desc, fullscreen, output, swapchain);
    if (SUCCEEDED(hr) && swapchain) remember_swapchain(*swapchain, device);
    return hr;
}

void STDMETHODCALLTYPE hook_execute_command_lists(ID3D12CommandQueue* queue, UINT count,
                                                  ID3D12CommandList* const* lists) {
    g_queues.note_execute(queue);
    g_execute_command_lists(queue, count, lists);
}

// ---------------------------------------------------------------------------------------------
// Pools

BasePool* decrypt_pool(PoolEncryption* encryption, int rotate_extra) {
    if (!encryption || !encryption->is_set) return nullptr;
    const horus::hook::PoolKey key{30, 0x1F, static_cast<uint8_t>(rotate_extra), 32};
    return reinterpret_cast<BasePool*>(horus::hook::decrypt_pool_pointer(encryption->first, encryption->second, key));
}

BasePool* decrypt_pool(const horus::hook::EncryptedPoolRef& ref) {
    if (!ref.flag || !(*ref.flag & 1)) return nullptr;
    return reinterpret_cast<BasePool*>(horus::hook::decrypt_pool_pointer(*ref.first, *ref.second, ref.key));
}

int collect_base_pool(BasePool* pool, int* out, int capacity) {
    if (!pool || !g.ptr_to_handle || !out || capacity <= 0) return 0;
    int count = 0;
    for (uint32_t i = 0; i < pool->size && count < capacity; ++i) {
        if (!pool->flags[i] || (pool->flags[i] & 0x80)) continue;
        uint8_t* object = reinterpret_cast<uint8_t*>(pool->entries + static_cast<uintptr_t>(i) * pool->item_size);
        if (!field<void*>(object, 0x10)) continue;
        if (const int handle = g.ptr_to_handle(object)) out[count++] = handle;
    }
    return count;
}

// ---------------------------------------------------------------------------------------------
// Initialisation

// Diagnostics: module+offset per unwound frame, so a stuck or crashing call can be identified.
void log_frame(int index, DWORD64 pc) {
    HMODULE module = nullptr;
    char name[MAX_PATH] = "?";
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCSTR>(pc), &module)) {
        GetModuleFileNameA(module, name, MAX_PATH);
    }
    const char* base = std::strrchr(name, '\\');
    log("  #%02d %s+0x%llx", index, base ? base + 1 : name,
        static_cast<unsigned long long>(pc - reinterpret_cast<DWORD64>(module)));
}

void log_stack(CONTEXT context) {
    for (int i = 0; i < 24 && context.Rip; ++i) {
        log_frame(i, context.Rip);
        DWORD64 image_base = 0;
        PRUNTIME_FUNCTION function = RtlLookupFunctionEntry(context.Rip, &image_base, nullptr);
        if (!function) {  // leaf function: return address is at [rsp]
            context.Rip = *reinterpret_cast<DWORD64*>(context.Rsp);
            context.Rsp += 8;
            continue;
        }
        void* handler_data = nullptr;
        DWORD64 establisher = 0;
        RtlVirtualUnwind(UNW_FLAG_NHANDLER, image_base, context.Rip, function, &context, &handler_data, &establisher,
                         nullptr);
    }
}

// Watchdog thread: (1) a script that does not yield for 5 s blocks the game; (2) no script tick for
// 10 s means the game thread stopped running scripts (pause, hang, starvation). Each is logged once
// per occurrence with the game thread's stack.
void log_game_thread_stack() {
    if (!g_main_thread || SuspendThread(g_main_thread) == static_cast<DWORD>(-1)) return;
    CONTEXT context{};
    context.ContextFlags = CONTEXT_FULL;
    if (GetThreadContext(g_main_thread, &context)) log_stack(context);
    ResumeThread(g_main_thread);
}

DWORD WINAPI watchdog(LPVOID) {
    Script* reported = nullptr;
    ULONGLONG reported_since = 0;
    ULONGLONG reported_tick = 0;
    for (;;) {
        Sleep(1000);
        const ULONGLONG now = GetTickCount64();
        const ULONGLONG last_tick = g_last_tick.load();
        if (last_tick && now - last_tick >= kNoTickMs && last_tick != reported_tick) {
            reported_tick = last_tick;
            log("WARNING: no script tick for %llu ms (game thread not running scripts); game thread stack:",
                static_cast<unsigned long long>(now - last_tick));
            log_game_thread_stack();
        }
        Script* script = g_running.load();
        const ULONGLONG since = g_running_since.load();
        if (!script || now - since < 5000) continue;
        if (script == reported && since == reported_since) continue;
        reported = script;
        reported_since = since;
        log("WARNING: script main %p has not yielded for %llu ms; game thread stack:",
            reinterpret_cast<void*>(script->entry), static_cast<unsigned long long>(now - since));
        log_game_thread_stack();
    }
}

// Faults raised on the game thread while a script fiber runs: log the first few with their stack
// before the game's own handler decides the process's fate. (The .NET thread of ScriptHookVDotNet
// raises first-chance access violations for managed null references; those are not logged.)
LONG CALLBACK log_script_fault(EXCEPTION_POINTERS* info) {
    static std::atomic<int> logged{0};
    const DWORD code = info->ExceptionRecord->ExceptionCode;
    const bool fault = code == EXCEPTION_ACCESS_VIOLATION || code == EXCEPTION_ILLEGAL_INSTRUCTION ||
                       code == EXCEPTION_STACK_OVERFLOW || code == EXCEPTION_INT_DIVIDE_BY_ZERO ||
                       code == EXCEPTION_PRIV_INSTRUCTION;
    Script* script = g_running.load();
    if (fault && script && GetCurrentThreadId() == g_main_thread_id.load() && logged.fetch_add(1) < 5) {
        log("ERROR: exception 0x%08lX in script main %p, address:", code, reinterpret_cast<void*>(script->entry));
        log_stack(*info->ContextRecord);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

DWORD WINAPI initialise(LPVOID) {
    log("HORUS Script Hook starting (Script Hook V API for GTA V Enhanced)");
    const MH_STATUS mh = MH_Initialize();
    if (mh != MH_OK) {
        log("FATAL: MinHook: %s", MH_StatusToString(mh));
        return 0;
    }
    // The executable unpacks its code after start and the unpacker changes page protections, so
    // scanning only starts once the game window exists; then retry until every pattern is readable.
    for (int i = 0; i < 1200 && !FindWindowW(L"sgaWindow", nullptr); ++i) Sleep(500);
    bool found = false;
    for (int attempt = 0; attempt < 600 && !(found = find_pointers(false)); ++attempt) Sleep(1000);
    if (!found) {
        find_pointers(true);
        log("FATAL: required game patterns not found; scripts disabled");
        return 0;
    }
    find_pointers(true);  // final pass: log optional misses, take any pointer that appeared late
    log("game version %s", g.version ? g.version : "(unknown)");
    if (!load_crossmap(g_module_dir + L"ScriptHookV.crossmap.txt")) {
        log("FATAL: ScriptHookV.crossmap.txt missing or empty; natives cannot be translated; scripts disabled");
        return 0;
    }
    log("crossmap: %zu entries loaded", g_crossmap.size());

    MH_STATUS status = MH_CreateHook(g.run_script_threads, reinterpret_cast<void*>(&hook_run_script_threads),
                                     reinterpret_cast<void**>(&g_run_script_threads));
    if (status != MH_OK) {
        log("FATAL: hooking RunScriptThreads: %s", MH_StatusToString(status));
        return 0;
    }
    horus::DxgiVtables vt;
    if (const char* error = horus::probe_dxgi_vtables(vt, L"horus_hook_probe")) {
        log("WARNING: %s failed; Present callbacks and textures disabled", error);
    } else {
        struct DxgiHook {
            void* target;
            void* detour;
            void** original;
            const char* name;
        };
        const DxgiHook hooks[] = {
            {vt.present, reinterpret_cast<void*>(&hook_present), reinterpret_cast<void**>(&g_present), "Present"},
            {vt.present1, reinterpret_cast<void*>(&hook_present1), reinterpret_cast<void**>(&g_present1), "Present1"},
            {vt.resize_buffers, reinterpret_cast<void*>(&hook_resize_buffers),
             reinterpret_cast<void**>(&g_resize_buffers), "ResizeBuffers"},
            {vt.resize_buffers1, reinterpret_cast<void*>(&hook_resize_buffers1),
             reinterpret_cast<void**>(&g_resize_buffers1), "ResizeBuffers1"},
            {vt.create_swapchain, reinterpret_cast<void*>(&hook_create_swapchain),
             reinterpret_cast<void**>(&g_create_swapchain), "CreateSwapChain"},
            {vt.create_swapchain_for_hwnd, reinterpret_cast<void*>(&hook_create_swapchain_for_hwnd),
             reinterpret_cast<void**>(&g_create_swapchain_for_hwnd), "CreateSwapChainForHwnd"},
            {vt.execute_command_lists, reinterpret_cast<void*>(&hook_execute_command_lists),
             reinterpret_cast<void**>(&g_execute_command_lists), "ExecuteCommandLists"},
        };
        for (const DxgiHook& h : hooks) {
            const MH_STATUS created = MH_CreateHook(h.target, h.detour, h.original);
            if (created != MH_OK) log("WARNING: hooking %s: %s", h.name, MH_StatusToString(created));
        }
    }
    status = MH_EnableHook(MH_ALL_HOOKS);
    if (status != MH_OK) {
        log("FATAL: enabling hooks: %s", MH_StatusToString(status));
        return 0;
    }
    if (HANDLE thread = CreateThread(nullptr, 0, watchdog, nullptr, 0, nullptr)) CloseHandle(thread);
    AddVectoredExceptionHandler(1, log_script_fault);
    g_ready.store(true, std::memory_order_release);
    log("INIT: success, waiting for story mode");
    return 0;
}

// The game executable's file version from its VS_VERSIONINFO resource, read once (all zero if it
// has none). The resource is mapped from process start and reading it loads no library, so
// plugins may ask before the patterns are scanned and from their DllMain.
const horus::hook::GameVersionInfo& game_file_version() {
    static const horus::hook::GameVersionInfo version = [] {
        horus::hook::GameVersionInfo file = {};
        HMODULE game = GetModuleHandleW(nullptr);
        HRSRC info = FindResourceW(game, MAKEINTRESOURCEW(VS_VERSION_INFO), MAKEINTRESOURCEW(16));  // RT_VERSION
        HGLOBAL loaded = info ? LoadResource(game, info) : nullptr;
        const void* data = loaded ? LockResource(loaded) : nullptr;
        if (!data || !horus::hook::fixed_file_version(static_cast<const uint8_t*>(data), SizeofResource(game, info), file)) {
            log("game file version: the game executable has no version resource; using 0.0.0.0");
        }
        return file;
    }();
    return version;
}

}  // namespace

// ---------------------------------------------------------------------------------------------
// Script Hook V API (exported under the MSVC-mangled names in exports.def)

extern "C" {

void shv_nativeInit(uint64_t hash) {
    CallState& call = t_call;
    call.hash = hash;
    call.context.return_value = call.result;
    call.context.args = call.args;
    call.context.arg_count = 0;
    call.context.vector_ref_count = 0;
    std::memset(call.result, 0, sizeof(call.result));
}

void shv_nativePush64(uint64_t value) {
    CallState& call = t_call;
    if (call.context.arg_count < sizeof(call.args) / sizeof(call.args[0])) call.args[call.context.arg_count++] = value;
}

uint64_t* shv_nativeCall() {
    CallState& call = t_call;
    if (!g_ready.load(std::memory_order_acquire)) return call.result;
    NativeHandler handler = resolve_native(call.hash);
    if (!handler) {
        char message[96];
        std::snprintf(message, sizeof(message), "ERROR: native 0x%016llX is not registered in this game build",
                      static_cast<unsigned long long>(call.hash));
        log_once(message, message);
        return call.result;
    }
    handler(&call.context);
    for (int i = 0; i < call.context.vector_ref_count && i < 4; ++i) {  // copy out Vector3 results
        ScrVector* target = call.context.vector_ref_targets[i];
        const Vector3Source& source = call.context.vector_ref_sources[i];
        target->x = source.x;
        target->y = source.y;
        target->z = source.z;
    }
    call.context.vector_ref_count = 0;
    return call.result;
}

bool shv_nativeCanExecuteInThisContext() { return g_running.load() != nullptr; }

void shv_scriptWait(DWORD milliseconds) {
    Script* script = g_running.load();
    if (!script) {
        log_once("wait-outside", "ERROR: scriptWait called outside a script fiber");
        Sleep(milliseconds == MAXDWORD ? 0 : milliseconds);
        return;
    }
    script->wake_at = milliseconds == MAXDWORD ? ~0ULL : GetTickCount64() + milliseconds;
    SwitchToFiber(g_main_fiber);
}

void shv_scriptRegister(HMODULE module, void (*entry)()) {
    std::lock_guard<std::mutex> lock(g_scripts_mutex);
    auto script = std::make_shared<Script>();
    script->module = module;
    script->entry = entry;
    g_scripts.push_back(std::move(script));
    log("registered script main %p", reinterpret_cast<void*>(entry));
}

void shv_scriptRegisterAdditionalThread(HMODULE module, void (*entry)()) { shv_scriptRegister(module, entry); }

void shv_scriptUnregisterModule(HMODULE module) {
    std::lock_guard<std::mutex> lock(g_scripts_mutex);
    for (auto& script : g_scripts) {
        if (script->module == module) script->removed = true;
    }
}

void shv_scriptUnregisterFunction(void (*entry)()) {
    std::lock_guard<std::mutex> lock(g_scripts_mutex);
    for (auto& script : g_scripts) {
        if (script->entry == entry) script->removed = true;
    }
}

bool shv_scriptsAreLaunchedUsingReloading() { return false; }

void shv_keyboardHandlerRegister(KeyboardHandler handler) {
    std::lock_guard<std::mutex> lock(g_keyboard_mutex);
    g_keyboard_handlers.push_back(handler);
}

void shv_keyboardHandlerUnregister(KeyboardHandler handler) {
    std::lock_guard<std::mutex> lock(g_keyboard_mutex);
    g_keyboard_handlers.erase(std::remove(g_keyboard_handlers.begin(), g_keyboard_handlers.end(), handler),
                              g_keyboard_handlers.end());
}

void shv_presentCallbackRegister(PresentCallback callback) {
    std::lock_guard<std::mutex> lock(g_present_mutex);
    g_present_callbacks.push_back(callback);
}

void shv_presentCallbackUnregister(PresentCallback callback) {
    std::lock_guard<std::mutex> lock(g_present_mutex);
    g_present_callbacks.erase(std::remove(g_present_callbacks.begin(), g_present_callbacks.end(), callback),
                              g_present_callbacks.end());
}

uint64_t* shv_getGlobalPtr(int global_id) {
    if (!g.script_globals) return nullptr;
    int64_t* block = g.script_globals[(global_id >> 18) & 0x3F];
    return block ? reinterpret_cast<uint64_t*>(&block[global_id & 0x3FFFF]) : nullptr;
}

BYTE* shv_getScriptHandleBaseAddress(int handle) {
    return g.handle_to_ptr ? static_cast<BYTE*>(g.handle_to_ptr(handle)) : nullptr;
}

int shv_worldGetAllPeds(int* out, int capacity) { return collect_base_pool(decrypt_pool(g.ped_pool, 2), out, capacity); }

int shv_worldGetAllObjects(int* out, int capacity) {
    return collect_base_pool(decrypt_pool(g.object_pool, 3), out, capacity);
}

int shv_worldGetAllVehicles(int* out, int capacity) {
    if (!g.vehicle_pool || !*g.vehicle_pool || !g.ptr_to_handle || !out || capacity <= 0) return 0;
    VehiclePool* pool = **g.vehicle_pool;
    if (!pool) return 0;
    int count = 0;
    for (uint32_t i = 0; i < pool->size && count < capacity; ++i) {
        if (!((pool->bits[i >> 5] >> (i & 0x1F)) & 1) || !pool->items[i]) continue;
        if (const int handle = g.ptr_to_handle(pool->items[i])) out[count++] = handle;
    }
    return count;
}

// Pickup objects (CPickup, the entity a pickup spawns as; GET_PICKUP_OBJECT returns the same handle).
int shv_worldGetAllPickups(int* out, int capacity) { return collect_base_pool(decrypt_pool(g.pickup_pool), out, capacity); }

int shv_createTexture(const char* file) {
    if (!file) return -1;
    const int length = MultiByteToWideChar(CP_ACP, 0, file, -1, nullptr, 0);
    std::wstring path(length > 0 ? length - 1 : 0, L'\0');
    if (length > 1) MultiByteToWideChar(CP_ACP, 0, file, -1, path.data(), length);
    horus::hook::Image image;
    std::string error;
    if (!horus::hook::decode_image(path, image, error)) {
        log("ERROR: createTexture(\"%s\"): %s; returning -1", file, error.c_str());
        return -1;
    }
    std::lock_guard<std::mutex> lock(g_textures_mutex);
    const int id = g_next_texture_id++;
    log("createTexture(\"%s\"): id %d, %ux%u", file, id, image.width, image.height);
    g_textures.push_back(Texture{id, std::move(image)});
    g_textures_pending.store(true);
    return id;
}

// Sets instance `index` of texture `id` for `time` ms (Script Hook V's parameters; see
// hook_core.hpp TextureLayout). Each Present draws the live instances, lowest level first.
void shv_drawTexture(int id, int index, int level, int time, float size_x, float size_y, float center_x,
                     float center_y, float pos_x, float pos_y, float rotation, float height_scale, float r, float g,
                     float b, float a) {
    horus::hook::TextureInstance instance;
    instance.id = id;
    instance.index = index;
    instance.level = level;
    instance.layout = {size_x, size_y, center_x, center_y, pos_x, pos_y, rotation, height_scale};
    const float color[4] = {r, g, b, a};
    std::memcpy(instance.color, color, sizeof(color));
    std::lock_guard<std::mutex> lock(g_textures_mutex);
    if (std::none_of(g_textures.begin(), g_textures.end(), [id](const Texture& t) { return t.id == id; })) {
        log_once("draw-texture-id", "ERROR: drawTexture: unknown texture id (not from createTexture, or dropped); ignored");
        return;
    }
    if (!g_instances.set(instance, time, GetTickCount64())) {
        log_once("draw-texture-index", "ERROR: drawTexture: index outside 0..63; ignored");
    }
}

// Script Hook V's eGameVersion for the running build (hook_core.hpp kShvEnhancedVersions).
int shv_getGameVersion() {
    static const int value = [] {
        const horus::hook::GameVersionInfo& file = game_file_version();
        const horus::hook::ShvGameVersion* known = horus::hook::shv_game_version(file);
        if (!known) {
            log("getGameVersion: game %d.%d.%d.%d is not in Script Hook V 3889.0's list; returning VER_UNK (-1)",
                file.major, file.minor, file.build, file.revision);
            return -1;
        }
        log("getGameVersion: game %d.%d.%d.%d is %s; returning %d", file.major, file.minor, file.build, file.revision,
            known->name, known->value);
        return known->value;
    }();
    return value;
}

// Script Hook V's getGameVersionInfo: the game executable's file version (all zero if unknown, as
// in Script Hook V), returned by value in RAX.
horus::hook::GameVersionInfo shv_getGameVersionInfo() {
    const horus::hook::GameVersionInfo& file = game_file_version();
    char message[96];
    std::snprintf(message, sizeof(message), "getGameVersionInfo: returning %d.%d.%d.%d", file.major, file.minor,
                  file.build, file.revision);
    log_once("version-info", message);
    return file;
}

}  // extern "C"

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        wchar_t path[MAX_PATH];
        const DWORD length = GetModuleFileNameW(instance, path, MAX_PATH);
        const std::wstring module_path(path, length);
        g_module_dir = module_path.substr(0, module_path.find_last_of(L"\\/") + 1);
        g_log = _wfopen((g_module_dir + L"ScriptHookV.log").c_str(), L"w");
        if (HANDLE thread = CreateThread(nullptr, 0, initialise, nullptr, 0, nullptr)) CloseHandle(thread);
    }
    return TRUE;
}

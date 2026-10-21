// Unit tests for the HORUS Script Hook (no game needed). Built by gta/build.sh; run under wine:
//   wine gta/build/horus_hook_tests.exe gta/build/ScriptHookV.dll gta/hook/src/exports.def
// Exit 0 when every check passes, 1 otherwise (each failure is printed).

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "hook_core.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <set>
#include <string>
#include <vector>

namespace {

int g_failures = 0;

#define CHECK(condition)                                                          \
    do {                                                                          \
        if (!(condition)) {                                                       \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);      \
            ++g_failures;                                                         \
        }                                                                         \
    } while (0)

using namespace horus::hook;

void test_parse_pattern() {
    const Pattern p = parse_pattern("48 8b ?? 05 ? FF");
    CHECK((p.bytes == std::vector<uint8_t>{0x48, 0x8B, 0x00, 0x05, 0x00, 0xFF}));
    CHECK((p.mask == std::vector<bool>{true, true, false, true, false, true}));
    const Pattern leading = parse_pattern("  ? 8B  ");
    CHECK((leading.bytes == std::vector<uint8_t>{0x00, 0x8B}));
    CHECK((leading.mask == std::vector<bool>{false, true}));
    // Each fixed byte is exactly two hex digits; anything else rejects the whole pattern.
    CHECK(parse_pattern("488B").bytes.empty());
    CHECK(parse_pattern("4 8B").bytes.empty());
    CHECK(parse_pattern("48 G1").bytes.empty());
    CHECK(parse_pattern("48 ???").bytes.empty());
    CHECK(parse_pattern("").bytes.empty());
}

void test_find_pattern() {
    uint8_t buffer[64] = {};
    const uint8_t code[] = {0x48, 0x8B, 0x05, 0x11, 0x22, 0x33, 0x44, 0x48, 0x85, 0xC0};
    std::copy(code, code + sizeof(code), buffer + 20);
    const uint8_t* begin = buffer;
    const uint8_t* end = buffer + sizeof(buffer);
    const Pattern p = parse_pattern("48 8B 05 ? ? ? ? 48 85 C0");

    const uint8_t* found = nullptr;
    CHECK(find_pattern(begin, end, p, &found) == 1);
    CHECK(found == buffer + 20);

    // A second copy makes the pattern ambiguous (find_unique then returns nullptr).
    std::copy(code, code + sizeof(code), buffer + 40);
    buffer[43] = 0x99;  // wildcard bytes may differ
    CHECK(find_pattern(begin, end, p, &found) == 2);

    // A mismatch in a fixed byte breaks the match; a match must lie wholly inside the range.
    buffer[49] = 0xC1;
    CHECK(find_pattern(begin, end, p, &found) == 1);
    CHECK(found == buffer + 20);
    CHECK(find_pattern(begin, buffer + 29, p, &found) == 0);
    CHECK(find_pattern(begin, buffer + 30, p, &found) == 1);
    CHECK(find_pattern(buffer + 21, end, p, &found) == 0);

    // Leading wildcard, pattern at the very end, empty pattern.
    found = nullptr;
    CHECK(find_pattern(begin, end, parse_pattern("? 85 C0 00"), &found) == 1);
    CHECK(found == buffer + 27);
    buffer[62] = 0xAB;
    buffer[63] = 0xCD;
    CHECK(find_pattern(begin, end, parse_pattern("AB CD"), &found) == 1);
    CHECK(found == buffer + 62);
    CHECK(find_pattern(begin, end, Pattern{}, &found) == 0);
    CHECK(find_pattern(begin, end, parse_pattern("488B"), &found) == 0);
}

void test_rip() {
    uint8_t buffer[64] = {};
    const int32_t forward = 0x10;
    std::memcpy(buffer + 8, &forward, 4);
    CHECK(rip(buffer + 8) == buffer + 8 + 4 + 0x10);
    const int32_t backward = -12;
    std::memcpy(buffer + 40, &backward, 4);
    CHECK(rip(buffer + 40) == buffer + 32);
}

void test_joaat() {
    // The hook finds the story-mode host thread by this hash (scrThread::m_ScriptHash).
    CHECK(joaat("main_persistent") == 0x5700179Cu);
    CHECK(joaat("adder") == 0xB779A091u);  // the Adder's model hash, VehicleHash.Adder in SHVDN
    CHECK(joaat("ADDER") == 0xB779A091u);  // case-insensitive
    CHECK(joaat("") == 0u);
}

// --- encrypted pool pointers --------------------------------------------------------------------

// ScriptHookVDotNet Enhanced 1.1.0.6's pickup pool decoder, step by step (NativeMemory.cs).
uint64_t shvdne_pickup_pointer(uint64_t first, uint64_t second, uint8_t rol1, uint8_t mask, uint8_t add, uint8_t rol2) {
    auto rol = [](uint64_t v, int n) { return (v << n) | (v >> (64 - n)); };
    uint64_t rcx = rol(second, rol1);
    uint64_t rax = first ^ rcx;
    uint8_t cl = static_cast<uint8_t>(rcx & 0xFF);
    cl = static_cast<uint8_t>(cl & mask);
    cl = static_cast<uint8_t>(cl + add);
    rax = rol(rax, cl);
    rax = rol(rax, rol2);
    return ~rax;
}

void test_pool_decrypt() {
    uint64_t state = 0x243F6A8885A308D3ull;
    auto next = [&] { return state = state * 6364136223846793005ull + 1442695040888963407ull; };
    for (int i = 0; i < 1000; ++i) {
        const uint64_t first = next(), second = next();
        // The pickup decoder as 1158.16 encodes it (rol 31, mask 0x1F, add 5, rol 32).
        CHECK(decrypt_pool_pointer(first, second, {31, 0x1F, 5, 32}) ==
              shvdne_pickup_pointer(first, second, 31, 0x1F, 5, 32));
        // The ped (add 2) and object (add 3) decoders as hook.cpp wrote them before PoolKey.
        for (uint8_t extra : {2, 3}) {
            const uint64_t x = _rotl64(second, 30);
            const uint64_t before = ~_rotl64(_rotl64(x ^ first, 32), (static_cast<uint8_t>(x) & 0x1F) + extra);
            CHECK(decrypt_pool_pointer(first, second, {30, 0x1F, extra, 32}) == before);
        }
    }
    CHECK(rotl64(0x8000000000000001ull, 1) == 3);
    CHECK(rotl64(0x1234, 0) == 0x1234 && rotl64(0x1234, 64) == 0x1234);
}

void test_pickup_pool_pattern() {
    // The decoder prologue laid out as kPickupPoolPattern describes, followed by its three globals.
    uint8_t buffer[128] = {};
    const uint8_t code[] = {
        0x48, 0x83, 0xEC, 0x28,              // +0  sub rsp, 0x28
        0x89, 0xCE,                          // +4  mov esi, ecx
        0x0F, 0xB6, 0x05, 0, 0, 0, 0,        // +6  movzx eax, byte [rip+flag]
        0x48, 0x8B, 0x0D, 0, 0, 0, 0,        // +13 mov rcx, [rip+second]
        0x48, 0xC1, 0xC1, 0x1F,              // +20 rol rcx, 31
        0x48, 0x8B, 0x05, 0, 0, 0, 0,        // +24 mov rax, [rip+first]
        0x48, 0x31, 0xC8,                    // +31 xor rax, rcx
        0x80, 0xE1, 0x1F,                    // +34 and cl, 0x1F
        0x80, 0xC1, 0x05,                    // +37 add cl, 5
        0x48, 0xD3, 0xC0, 0x90, 0x90, 0x90,  // +40 rol rax, cl (+ filler)
        0x48, 0xC1, 0xC0, 0x20,              // +46 rol rax, 32
    };
    uint8_t* const at = buffer + 16;
    std::memcpy(at, code, sizeof(code));
    uint8_t* const flag = buffer + 80;
    uint8_t* const second = buffer + 88;
    uint8_t* const first = buffer + 96;
    auto displace = [&](int disp_at, int end, uint8_t* target) {
        const int32_t d = static_cast<int32_t>(target - (at + end));
        std::memcpy(at + disp_at, &d, 4);
    };
    displace(9, 13, flag);
    displace(16, 20, second);
    displace(27, 31, first);
    const Pattern p = parse_pattern(kPickupPoolPattern);
    CHECK(!p.bytes.empty() && p.bytes.size() == 50);
    const uint8_t* found = nullptr;
    CHECK(find_pattern(buffer, buffer + sizeof(buffer), p, &found) == 1 && found == at);
    const EncryptedPoolRef ref = pickup_pool_ref(at);
    CHECK(ref.flag == flag);
    CHECK(reinterpret_cast<const uint8_t*>(ref.second) == second);
    CHECK(reinterpret_cast<const uint8_t*>(ref.first) == first);
    CHECK(ref.key.rotate_second == 31 && ref.key.mask == 0x1F && ref.key.add == 5 && ref.key.rotate_final == 32);
    // Another instruction where the pattern fixes one (or cl instead of and cl) is not a match.
    at[35] = 0xC9;
    CHECK(find_pattern(buffer, buffer + sizeof(buffer), p, &found) == 0);
}

// --- drawTexture: quad geometry and the instance store ------------------------------------------

bool close_to(float a, float b) { return std::fabs(a - b) < 1e-3f; }

// Corner `i` (texture corners (0,0), (1,0), (0,1), (1,1)) of `q` in pixels on a width x height target.
void corner_px(const float q[8], int i, float width, float height, float& x, float& y) {
    x = (q[i * 2] + 1.0f) * 0.5f * width;
    y = (1.0f - q[i * 2 + 1]) * 0.5f * height;
}

void test_texture_quad() {
    float q[8];
    float x, y;
    // Centred, unrotated, 20% of a 1920x1080 screen wide, height scale = aspect: a 384 px square.
    texture_quad({0.2f, 0.2f, 0.5f, 0.5f, 0.5f, 0.5f, 0.0f, 1920.0f / 1080.0f}, 1920, 1080, q);
    corner_px(q, 0, 1920, 1080, x, y);
    CHECK(close_to(x, 960 - 192) && close_to(y, 540 - 192));
    corner_px(q, 3, 1920, 1080, x, y);
    CHECK(close_to(x, 960 + 192) && close_to(y, 540 + 192));
    // Clip space: x right, y up.
    CHECK(close_to(q[0], (768.0f / 1920) * 2 - 1) && close_to(q[1], 1 - (348.0f / 1080) * 2));
    // Pivot at the texture's top-left corner: that corner sits on the position.
    texture_quad({0.1f, 0.1f, 0.0f, 0.0f, 0.25f, 0.75f, 0.0f, 1.0f}, 1000, 500, q);
    corner_px(q, 0, 1000, 500, x, y);
    CHECK(close_to(x, 250) && close_to(y, 375));
    corner_px(q, 3, 1000, 500, x, y);
    CHECK(close_to(x, 350) && close_to(y, 425));  // 100 px wide, 0.1 * 1.0 * 500 = 50 px high
    // A quarter turn clockwise about the centre: the image's top-left corner goes to the top right.
    texture_quad({0.2f, 0.4f, 0.5f, 0.5f, 0.5f, 0.5f, 0.25f, 1.0f}, 1000, 1000, q);
    corner_px(q, 0, 1000, 1000, x, y);  // was (400, 300): offset (-100, -200) -> (200, -100)
    CHECK(close_to(x, 700) && close_to(y, 400));
    corner_px(q, 1, 1000, 1000, x, y);  // was (600, 300): offset (100, -200) -> (200, 100)
    CHECK(close_to(x, 700) && close_to(y, 600));
}

void test_texture_instances() {
    TextureInstances store;
    auto make = [](int id, int index, int level) {
        TextureInstance t;
        t.id = id;
        t.index = index;
        t.level = level;
        return t;
    };
    CHECK(store.set(make(1, 0, 5), 100, 1000));
    CHECK(store.set(make(2, 3, -1), 300, 1000));
    CHECK(store.set(make(1, 63, 5), 50, 1000));
    CHECK(!store.set(make(1, 64, 0), 100, 1000));  // Script Hook V's 64 instances per texture
    CHECK(!store.set(make(1, -1, 0), 100, 1000));
    // Lowest level first; equal levels by id, then index.
    auto order = store.draw_order(1000);
    CHECK(order.size() == 3 && order[0].id == 2 && order[1].index == 0 && order[2].index == 63);
    // `time` ms after the call the instance is gone: 1049 still has (1,63), 1050 not.
    CHECK(store.draw_order(1049).size() == 3);
    CHECK(store.draw_order(1050).size() == 2);
    // A new call for the same (id, index) replaces the instance and restarts its time.
    CHECK(store.set(make(1, 0, -5), 100, 1090));
    order = store.draw_order(1150);
    CHECK(order.size() == 2 && order[0].id == 1 && order[0].level == -5 && order[1].id == 2);
    CHECK(store.draw_order(1190).size() == 1);
    // prune drops expired instances and those whose texture is gone.
    store.prune(1150, [](int id) { return id != 2; });
    CHECK(store.draw_order(1150).size() == 1 && !store.empty());
    store.prune(1190, [](int) { return true; });
    CHECK(store.empty());
}

void test_crossmap() {
    const Crossmap map = parse_crossmap(
        "0x4EDE34FBADD967A6,0x4EDE34FBADD967A6\n"  // 1
        "0xe81651ad79516e48 , 0x1\r\n"             // 2: lower case, spaces, CRLF
        "\n"                                       // 3: blank, ignored
        "0x1234\n"                                 // 4: one hash
        "garbage\n"                                // 5
        "0x2,0x3,0x4\n"                            // 6: trailing field
        "0x,0x5\n"                                 // 7: no digits
        "0x10000000000000000,0x6\n"                // 8: 17 digits
        "# comment\n"                              // 9
        "   \t\r\n"                                // 10: blank, ignored
        "0x7, 0x8");                               // 11: last line without LF
    CHECK(map.entries.size() == 3);
    CHECK(map.entries.count(0x4EDE34FBADD967A6ull) && map.entries.at(0x4EDE34FBADD967A6ull) == 0x4EDE34FBADD967A6ull);
    CHECK(map.entries.count(0xE81651AD79516E48ull) && map.entries.at(0xE81651AD79516E48ull) == 0x1);
    CHECK(map.entries.count(0x7) && map.entries.at(0x7) == 0x8);  // entries after malformed lines still load
    CHECK((map.malformed_lines == std::vector<size_t>{4, 5, 6, 7, 8, 9}));
    CHECK(parse_crossmap("").entries.empty());
}

// --- game version: Script Hook V's eGameVersion numbering and the version resource parser -------

void test_game_version() {
    const ShvGameVersion* latest = shv_game_version({1, 0, 1158, 13});
    CHECK(latest && latest->value == 1012 && std::strcmp(latest->name, "VER_EN_1_0_1158_13") == 0);
    CHECK(shv_game_version({1, 0, 811, 8}) && shv_game_version({1, 0, 811, 8})->value == 1001);
    CHECK(shv_game_version({1, 0, 1013, 34}) && shv_game_version({1, 0, 1013, 34})->value == 1011);
    // Unlisted builds are VER_UNK: the current 1158.16, a Legacy build, another major version.
    CHECK(shv_game_version({1, 0, 1158, 16}) == nullptr);
    CHECK(shv_game_version({1, 0, 3889, 0}) == nullptr);
    CHECK(shv_game_version({2, 0, 1158, 13}) == nullptr);
    // Values run 1001.. in table order and each name spells its build.
    int expected = 1001;
    for (const ShvGameVersion& v : kShvEnhancedVersions) {
        char name[32];
        std::snprintf(name, sizeof(name), "VER_EN_1_0_%d_%d", v.build, v.revision);
        CHECK(v.value == expected++);
        CHECK(std::strcmp(v.name, name) == 0);
    }
    CHECK(expected == 1013);
}

void test_fixed_file_version() {
    // Signature, struct version, file version MS/LS (1.0.1158.16) after a 40-byte header.
    uint8_t resource[64] = {};
    const uint32_t fixed[4] = {0xFEEF04BDu, 0x00010000u, 0x00010000u, (1158u << 16) | 16u};
    std::memcpy(resource + 40, fixed, sizeof(fixed));
    GameVersionInfo version = {};
    CHECK(fixed_file_version(resource, sizeof(resource), version));
    CHECK(version.major == 1 && version.minor == 0 && version.build == 1158 && version.revision == 16);
    CHECK(!fixed_file_version(resource, 50, version));  // truncated before the version words
    uint8_t misaligned[64] = {};
    std::memcpy(misaligned + 42, fixed, sizeof(fixed));
    CHECK(!fixed_file_version(misaligned, sizeof(misaligned), version));

    // A real version resource read the way hook.cpp reads the game's, against Windows' own parser.
    HMODULE module = GetModuleHandleW(L"kernel32.dll");
    HRSRC info = FindResourceW(module, MAKEINTRESOURCEW(VS_VERSION_INFO), MAKEINTRESOURCEW(16));
    const void* data = info ? LockResource(LoadResource(module, info)) : nullptr;
    CHECK(data && fixed_file_version(static_cast<const uint8_t*>(data), SizeofResource(module, info), version));
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(module, path, MAX_PATH);
    std::vector<uint8_t> block(GetFileVersionInfoSizeW(path, nullptr));
    VS_FIXEDFILEINFO* expected = nullptr;
    UINT length = 0;
    CHECK(!block.empty() && GetFileVersionInfoW(path, 0, static_cast<DWORD>(block.size()), block.data()) &&
          VerQueryValueW(block.data(), L"\\", reinterpret_cast<void**>(&expected), &length) && expected);
    if (expected) {
        CHECK(version.major == HIWORD(expected->dwFileVersionMS) && version.minor == LOWORD(expected->dwFileVersionMS));
        CHECK(version.build == HIWORD(expected->dwFileVersionLS) && version.revision == LOWORD(expected->dwFileVersionLS));
    }
}

// The DLL's version exports, called the way plugins call them. This executable stands in for the
// game: hook_tests.rc gives it file version 1.0.1158.16, the build on Koko, which Script Hook V
// does not list.
void test_version_exports(const char* dll_path) {
    char full[MAX_PATH];
    if (!GetFullPathNameA(dll_path, MAX_PATH, full, nullptr)) {
        CHECK(!"GetFullPathNameA");
        return;
    }
    std::replace(full, full + std::strlen(full), '/', '\\');
    HMODULE dll = LoadLibraryA(full);
    CHECK(dll != nullptr);
    if (!dll) return;
    auto get_version = reinterpret_cast<int (*)()>(
        reinterpret_cast<void*>(GetProcAddress(dll, "?getGameVersion@@YA?AW4eGameVersion@@XZ")));
    void* info_export = reinterpret_cast<void*>(GetProcAddress(dll, "?getGameVersionInfo@@YA?AUGameVersionInfo@@XZ"));
    CHECK(get_version && info_export);
    if (!get_version || !info_export) return;
    CHECK(get_version() == -1);  // VER_UNK
    const GameVersionInfo info = reinterpret_cast<GameVersionInfo (*)()>(info_export)();
    CHECK(info.major == 1 && info.minor == 0 && info.build == 1158 && info.revision == 16);
    // An MSVC-built plugin reads the 8-byte struct from RAX, little-endian: major in the low word.
    const uint64_t rax = reinterpret_cast<uint64_t (*)()>(info_export)();
    CHECK(rax == (1ull | (0ull << 16) | (1158ull << 32) | (16ull << 48)));
}

// --- exports: ScriptHookV.dll against exports.def and Script Hook V's own export list -----------

// Every export of Alexander Blade's ScriptHookV.dll 3889.0 (objdump -p, 2026-10-20).
const char* const kScriptHookVExports[] = {
    "?createTexture@@YAHPEBD@Z",
    "?drawTexture@@YAXHHHHMMMMMMMMMMMM@Z",
    "?getGameVersion@@YA?AW4eGameVersion@@XZ",
    "?getGameVersionInfo@@YA?AUGameVersionInfo@@XZ",
    "?getGlobalPtr@@YAPEA_KH@Z",
    "?getScriptHandleBaseAddress@@YAPEAEH@Z",
    "?keyboardHandlerRegister@@YAXP6AXKGEHHHH@Z@Z",
    "?keyboardHandlerUnregister@@YAXP6AXKGEHHHH@Z@Z",
    "?nativeCall@@YAPEA_KXZ",
    "?nativeCanExecuteInThisContext@@YA_NXZ",
    "?nativeInit@@YAX_K@Z",
    "?nativePush64@@YAX_K@Z",
    "?presentCallbackRegister@@YAXP6AXPEAX@Z@Z",
    "?presentCallbackUnregister@@YAXP6AXPEAX@Z@Z",
    "?scriptRegister@@YAXPEAUHINSTANCE__@@P6AXXZ@Z",
    "?scriptRegisterAdditionalThread@@YAXPEAUHINSTANCE__@@P6AXXZ@Z",
    "?scriptUnregister@@YAXP6AXXZ@Z",
    "?scriptUnregister@@YAXPEAUHINSTANCE__@@@Z",
    "?scriptWait@@YAXK@Z",
    "?scriptsAreLaunchedUsingReloading@@YA_NXZ",
    "?worldGetAllObjects@@YAHPEAHH@Z",
    "?worldGetAllPeds@@YAHPEAHH@Z",
    "?worldGetAllPickups@@YAHPEAHH@Z",
    "?worldGetAllVehicles@@YAHPEAHH@Z",
};

bool read_file(const char* path, std::string& out) {
    FILE* file = std::fopen(path, "rb");
    if (!file) return false;
    char buffer[65536];
    for (size_t n; (n = std::fread(buffer, 1, sizeof(buffer), file)) > 0;) out.append(buffer, n);
    std::fclose(file);
    return true;
}

// Names in the PE export directory of a 64-bit DLL file; false if the file is not one.
bool pe_export_names(const std::string& image, std::set<std::string>& names) {
    auto in = [&](size_t offset, size_t size) { return offset <= image.size() && size <= image.size() - offset; };
    if (!in(0, sizeof(IMAGE_DOS_HEADER))) return false;
    IMAGE_DOS_HEADER dos;
    std::memcpy(&dos, image.data(), sizeof(dos));
    if (dos.e_magic != IMAGE_DOS_SIGNATURE || !in(dos.e_lfanew, sizeof(IMAGE_NT_HEADERS64))) return false;
    IMAGE_NT_HEADERS64 nt;
    std::memcpy(&nt, image.data() + dos.e_lfanew, sizeof(nt));
    if (nt.Signature != IMAGE_NT_SIGNATURE || nt.OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return false;
    const size_t sections = dos.e_lfanew + offsetof(IMAGE_NT_HEADERS64, OptionalHeader) + nt.FileHeader.SizeOfOptionalHeader;
    auto file_offset = [&](uint32_t rva, size_t& offset) {
        for (unsigned i = 0; i < nt.FileHeader.NumberOfSections; ++i) {
            IMAGE_SECTION_HEADER s;
            if (!in(sections + i * sizeof(s), sizeof(s))) return false;
            std::memcpy(&s, image.data() + sections + i * sizeof(s), sizeof(s));
            if (rva >= s.VirtualAddress && rva < s.VirtualAddress + std::max(s.Misc.VirtualSize, s.SizeOfRawData)) {
                offset = rva - s.VirtualAddress + s.PointerToRawData;
                return true;
            }
        }
        return false;
    };
    const IMAGE_DATA_DIRECTORY dir = nt.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
    size_t offset = 0;
    if (dir.Size == 0 || !file_offset(dir.VirtualAddress, offset) || !in(offset, sizeof(IMAGE_EXPORT_DIRECTORY))) return false;
    IMAGE_EXPORT_DIRECTORY exports;
    std::memcpy(&exports, image.data() + offset, sizeof(exports));
    size_t names_offset = 0;
    if (!file_offset(exports.AddressOfNames, names_offset) || !in(names_offset, exports.NumberOfNames * 4ull)) return false;
    for (DWORD i = 0; i < exports.NumberOfNames; ++i) {
        uint32_t name_rva;
        std::memcpy(&name_rva, image.data() + names_offset + i * 4, 4);
        size_t name_offset = 0;
        if (!file_offset(name_rva, name_offset) || !in(name_offset, 1)) return false;
        const size_t end = image.find('\0', name_offset);
        if (end == std::string::npos) return false;
        names.insert(image.substr(name_offset, end - name_offset));
    }
    return true;
}

// Quoted export names in a .def file: `"name" = symbol` lines.
std::set<std::string> def_export_names(const std::string& def) {
    std::set<std::string> names;
    for (size_t open = def.find('"'); open != std::string::npos; open = def.find('"', open)) {
        const size_t close = def.find('"', open + 1);
        if (close == std::string::npos) break;
        names.insert(def.substr(open + 1, close - open - 1));
        open = close + 1;
    }
    return names;
}

void report_difference(const char* what, const std::set<std::string>& a, const std::set<std::string>& b) {
    for (const auto& name : a) {
        if (!b.count(name)) std::printf("  %s: %s\n", what, name.c_str());
    }
}

void test_exports(const char* dll_path, const char* def_path) {
    std::string dll;
    std::string def;
    CHECK(read_file(dll_path, dll));
    CHECK(read_file(def_path, def));
    std::set<std::string> exported;
    CHECK(pe_export_names(dll, exported));
    const std::set<std::string> declared = def_export_names(def);

    const std::set<std::string> expected(std::begin(kScriptHookVExports), std::end(kScriptHookVExports));

    report_difference("in the DLL, not in exports.def", exported, declared);
    report_difference("in exports.def, not in the DLL", declared, exported);
    CHECK(exported == declared);
    report_difference("Script Hook V export missing from exports.def", expected, declared);
    report_difference("in exports.def, not a Script Hook V export", declared, expected);
    CHECK(declared == expected);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "usage: %s ScriptHookV.dll exports.def\n", argv[0]);
        return 2;
    }
    test_parse_pattern();
    test_find_pattern();
    test_rip();
    test_joaat();
    test_pool_decrypt();
    test_pickup_pool_pattern();
    test_texture_quad();
    test_texture_instances();
    test_crossmap();
    test_game_version();
    test_fixed_file_version();
    test_version_exports(argv[1]);
    test_exports(argv[1], argv[2]);
    if (g_failures) {
        std::printf("%d check(s) FAILED\n", g_failures);
        return 1;
    }
    std::printf("all hook tests passed\n");
    return 0;
}

// Pure parts of the HORUS Script Hook (no game, no Windows API), shared by hook.cpp and the unit
// tests in gta/hook/tests: byte patterns, RIP-relative targets, joaat, the crossmap format and
// Script Hook V's game version numbering.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <map>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace horus::hook {

// A byte pattern such as "48 8B 05 ? ? ? ? 48 85 C0": two hex digits per fixed byte, "?" or "??" per
// wildcard, separated by spaces. Malformed text gives an empty pattern, which matches nothing.
struct Pattern {
    std::vector<uint8_t> bytes;
    std::vector<bool> mask;  // true = must match
};

inline int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

inline Pattern parse_pattern(std::string_view text) {
    Pattern p;
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == ' ') {
            ++i;
            continue;
        }
        size_t end = i;
        while (end < text.size() && text[end] != ' ') ++end;
        const std::string_view token = text.substr(i, end - i);
        if (token == "?" || token == "??") {
            p.bytes.push_back(0);
            p.mask.push_back(false);
        } else if (token.size() == 2 && hex_digit(token[0]) >= 0 && hex_digit(token[1]) >= 0) {
            p.bytes.push_back(static_cast<uint8_t>(hex_digit(token[0]) << 4 | hex_digit(token[1])));
            p.mask.push_back(true);
        } else {
            return {};
        }
        i = end;
    }
    return p;
}

// Matches of `p` lying entirely inside [begin, end): the count, and the last match in `found`.
inline int find_pattern(const uint8_t* begin, const uint8_t* end, const Pattern& p, const uint8_t** found) {
    const size_t size = p.bytes.size();
    if (size == 0 || end - begin < static_cast<ptrdiff_t>(size)) return 0;
    int count = 0;
    for (const uint8_t* at = begin; at + size <= end; ++at) {
        if (p.mask[0] && at[0] != p.bytes[0]) continue;
        size_t i = 1;
        while (i < size && (!p.mask[i] || at[i] == p.bytes[i])) ++i;
        if (i == size) {
            *found = at;
            ++count;
        }
    }
    return count;
}

// Target of a RIP-relative 32-bit displacement stored at `at`.
inline uint8_t* rip(uint8_t* at) {
    int32_t displacement;
    std::memcpy(&displacement, at, 4);
    return at + 4 + displacement;
}

inline uint64_t rotl64(uint64_t value, unsigned count) {
    count &= 63;
    return count ? (value << count) | (value >> (64 - count)) : value;
}

// Enhanced keeps some pool pointers as two encrypted qwords. The game decodes them with
//   x = rol(second, rotate_second); pointer = ~rol(rol(first ^ x, (x & mask) + add), rotate_final)
// The two final rotations commute, so this covers the ped/object decoders (rol 30, then rol 32 and
// rol (x & 0x1F) + 2 or 3) and the pickup decoder (rol 31, rol (x & 0x1F) + 5, then rol 32).
struct PoolKey {
    uint8_t rotate_second;
    uint8_t mask;
    uint8_t add;
    uint8_t rotate_final;
};

inline uint64_t decrypt_pool_pointer(uint64_t first, uint64_t second, PoolKey key) {
    const uint64_t x = rotl64(second, key.rotate_second);
    return ~rotl64(rotl64(first ^ x, (static_cast<uint8_t>(x) & key.mask) + key.add), key.rotate_final);
}

// The pickup pool's decoder, a function prologue in 1158.16 (pattern from ScriptHookVDotNet
// Enhanced 1.1.0.6, NativeMemory.cs, with the instruction bytes it implies written out):
//   +0  sub rsp, imm8            +4  mov esi, ecx
//   +6  movzx eax, byte [rip+d]  (the "pool is set" flag, bit 0)
//   +13 mov rcx, [rip+d]         (second)       +20 rol rcx, imm8      (rotate_second)
//   +24 mov rax, [rip+d]         (first)        +31 xor rax, rcx
//   +34 and cl, imm8 (mask)      +37 add cl, imm8 (add)                +40 rol rax, cl
//   +46 rol rax, imm8            (rotate_final)
constexpr const char* kPickupPoolPattern =
    "48 83 EC ? 89 CE 0F B6 05 ? ? ? ? 48 8B 0D ? ? ? ? 48 C1 C1 ? 48 8B 05 ? ? ? ? ? ? ? 80 E1 ? 80 C1 ? "
    "? ? ? ? ? ? 48 C1 C0 ?";

struct EncryptedPoolRef {
    const uint8_t* flag = nullptr;
    const uint64_t* first = nullptr;
    const uint64_t* second = nullptr;
    PoolKey key{};
};

inline EncryptedPoolRef pickup_pool_ref(uint8_t* match) {
    EncryptedPoolRef ref;
    ref.flag = rip(match + 9);
    ref.second = reinterpret_cast<const uint64_t*>(rip(match + 16));
    ref.first = reinterpret_cast<const uint64_t*>(rip(match + 27));
    ref.key = {match[23], match[36], match[39], match[49]};
    return ref;
}

// Jenkins one-at-a-time hash over the lower-cased text, as the game hashes names.
inline uint32_t joaat(std::string_view text) {
    uint32_t hash = 0;
    for (const char c : text) {
        hash += static_cast<uint8_t>(c >= 'A' && c <= 'Z' ? c + 32 : c);
        hash += hash << 10;
        hash ^= hash >> 6;
    }
    hash += hash << 3;
    hash ^= hash >> 11;
    hash += hash << 15;
    return hash;
}

// ScriptHookV.crossmap.txt: one "0xORIGINAL,0xENHANCED" pair per line (spaces around the comma and
// a CR before the LF allowed, last line may lack its LF). Blank lines are ignored; any other line
// that is not exactly such a pair is skipped and its 1-based number recorded.
struct Crossmap {
    std::unordered_map<uint64_t, uint64_t> entries;
    std::vector<size_t> malformed_lines;
};

namespace detail {

inline void skip_spaces(std::string_view s, size_t& i) {
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
}

// "0x" followed by 1-16 hex digits.
inline bool parse_hash(std::string_view s, size_t& i, uint64_t& value) {
    if (i + 2 > s.size() || s[i] != '0' || (s[i + 1] != 'x' && s[i + 1] != 'X')) return false;
    i += 2;
    const size_t start = i;
    value = 0;
    while (i < s.size() && hex_digit(s[i]) >= 0) value = value << 4 | static_cast<uint64_t>(hex_digit(s[i++]));
    return i > start && i - start <= 16;
}

inline bool parse_crossmap_line(std::string_view line, uint64_t& original, uint64_t& enhanced) {
    size_t i = 0;
    skip_spaces(line, i);
    if (!parse_hash(line, i, original)) return false;
    skip_spaces(line, i);
    if (i == line.size() || line[i++] != ',') return false;
    skip_spaces(line, i);
    if (!parse_hash(line, i, enhanced)) return false;
    skip_spaces(line, i);
    return i == line.size();
}

}  // namespace detail

inline Crossmap parse_crossmap(std::string_view text) {
    Crossmap map;
    size_t number = 0;
    for (size_t start = 0; start < text.size();) {
        size_t end = text.find('\n', start);
        if (end == std::string_view::npos) end = text.size();
        std::string_view line = text.substr(start, end - start);
        start = end + 1;
        ++number;
        if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
        if (line.find_first_not_of(" \t") == std::string_view::npos) continue;
        uint64_t original = 0;
        uint64_t enhanced = 0;
        if (detail::parse_crossmap_line(line, original, enhanced)) {
            map.entries[original] = enhanced;
        } else {
            map.malformed_lines.push_back(number);
        }
    }
    return map;
}

// Script Hook V's GameVersionInfo (`getGameVersionInfo`): the game executable's file version as
// four 16-bit fields, 8 bytes, returned by value in RAX. Layout from Alexander Blade's
// ScriptHookV.dll 3889.0; see gta/hook/README.md "Game version".
struct GameVersionInfo {
    uint16_t major;
    uint16_t minor;
    uint16_t build;
    uint16_t revision;
};
static_assert(sizeof(GameVersionInfo) == 8, "Script Hook V returns GameVersionInfo in RAX");

// Script Hook V's eGameVersion values for GTA V Enhanced, taken from Alexander Blade's
// ScriptHookV.dll 3889.0 (1158.13); see gta/hook/README.md "Game version". It numbers Enhanced
// builds VER_EN_* = 1001 upward in release order (Legacy builds are 0..102, which this hook never
// runs on). Builds it does not list are VER_UNK (-1).
struct ShvGameVersion {
    uint16_t build;
    uint16_t revision;
    int value;
    const char* name;
};

inline constexpr ShvGameVersion kShvEnhancedVersions[] = {
    {811, 8, 1001, "VER_EN_1_0_811_8"},     {812, 8, 1002, "VER_EN_1_0_812_8"},
    {813, 11, 1003, "VER_EN_1_0_813_11"},   {814, 9, 1004, "VER_EN_1_0_814_9"},
    {889, 15, 1005, "VER_EN_1_0_889_15"},   {889, 19, 1006, "VER_EN_1_0_889_19"},
    {889, 22, 1007, "VER_EN_1_0_889_22"},   {1013, 17, 1008, "VER_EN_1_0_1013_17"},
    {1013, 29, 1009, "VER_EN_1_0_1013_29"}, {1013, 33, 1010, "VER_EN_1_0_1013_33"},
    {1013, 34, 1011, "VER_EN_1_0_1013_34"}, {1158, 13, 1012, "VER_EN_1_0_1158_13"},
};

// The listed entry for the executable's file version (Enhanced builds are 1.0.<build>.<revision>),
// or nullptr for VER_UNK.
inline const ShvGameVersion* shv_game_version(const GameVersionInfo& version) {
    if (version.major != 1 || version.minor != 0) return nullptr;
    for (const ShvGameVersion& v : kShvEnhancedVersions) {
        if (v.build == version.build && v.revision == version.revision) return &v;
    }
    return nullptr;
}

// The file version in a VS_VERSIONINFO resource: the VS_FIXEDFILEINFO that starts with signature
// 0xFEEF04BD on a 32-bit boundary. False if absent.
inline bool fixed_file_version(const uint8_t* data, size_t size, GameVersionInfo& version) {
    for (size_t i = 0; i + 16 <= size; i += 4) {
        uint32_t word[4];
        std::memcpy(word, data + i, sizeof(word));
        if (word[0] != 0xFEEF04BDu) continue;
        version.major = static_cast<uint16_t>(word[2] >> 16);
        version.minor = static_cast<uint16_t>(word[2] & 0xFFFF);
        version.build = static_cast<uint16_t>(word[3] >> 16);
        version.revision = static_cast<uint16_t>(word[3] & 0xFFFF);
        return true;
    }
    return false;
}

// --- drawTexture ---------------------------------------------------------------------------------

// One drawTexture call's layout, as Script Hook V defines it: size and position are fractions of the
// screen; (center_x, center_y) is the point of the texture (0..1) placed at (pos_x, pos_y) and
// rotated about; rotation is in turns, clockwise. The drawn width is size_x of the screen width and
// the height size_y * height_scale of the screen height (ScriptHookVDotNet passes the screen aspect
// ratio as height_scale and size_y already divided by it, so equal sizes give a square).
struct TextureLayout {
    float size_x, size_y, center_x, center_y, pos_x, pos_y, rotation, height_scale;
};

// The quad's corners in clip space (x right, y up, -1..1) for a width x height pixel target, in the
// order of the texture corners (0,0), (1,0), (0,1), (1,1) (top left, top right, bottom left, bottom
// right of the image).
inline void texture_quad(const TextureLayout& l, float width, float height, float out[8]) {
    const float w = l.size_x * width;
    const float h = l.size_y * l.height_scale * height;
    const float angle = l.rotation * 6.28318530718f;
    const float c = std::cos(angle), s = std::sin(angle);
    for (int corner = 0; corner < 4; ++corner) {
        const float x = (static_cast<float>(corner & 1) - l.center_x) * w;  // pixels, y down
        const float y = (static_cast<float>(corner >> 1) - l.center_y) * h;
        const float px = l.pos_x * width + x * c - y * s;  // clockwise on screen (y down)
        const float py = l.pos_y * height + x * s + y * c;
        out[corner * 2] = px / width * 2.0f - 1.0f;
        out[corner * 2 + 1] = 1.0f - py / height * 2.0f;
    }
}

struct TextureInstance {
    int id = 0;
    int index = 0;
    int level = 0;
    uint64_t expires_at = 0;  // ms
    TextureLayout layout{};
    float color[4] = {};
};

// The texture instances on screen: drawTexture(id, index, ...) sets instance (id, index) until
// `time` ms from now; a later call for the same pair replaces it. Script Hook V allows 64 instances
// per texture.
class TextureInstances {
public:
    static constexpr int kMaxIndex = 64;

    // False (nothing stored) for an index outside 0..63.
    bool set(const TextureInstance& instance, int time_ms, uint64_t now_ms) {
        if (instance.index < 0 || instance.index >= kMaxIndex) return false;
        TextureInstance stored = instance;
        stored.expires_at = now_ms + static_cast<uint64_t>(std::max(time_ms, 0));
        instances_[{instance.id, instance.index}] = stored;
        return true;
    }

    // Removes expired instances and those of textures that no longer exist.
    template <typename Exists>
    void prune(uint64_t now_ms, Exists exists) {
        for (auto it = instances_.begin(); it != instances_.end();) {
            it = (now_ms >= it->second.expires_at || !exists(it->first.first)) ? instances_.erase(it) : std::next(it);
        }
    }

    // The live instances in draw order: lowest level first, then by texture id and index.
    std::vector<TextureInstance> draw_order(uint64_t now_ms) const {
        std::vector<TextureInstance> out;
        for (const auto& entry : instances_) {
            if (now_ms < entry.second.expires_at) out.push_back(entry.second);
        }
        std::stable_sort(out.begin(), out.end(),
                         [](const TextureInstance& a, const TextureInstance& b) { return a.level < b.level; });
        return out;
    }

    bool empty() const { return instances_.empty(); }
    void clear() { instances_.clear(); }

private:
    std::map<std::pair<int, int>, TextureInstance> instances_;  // ordered by (id, index)
};

}  // namespace horus::hook

// Test images for createTexture: a deterministic RGBA pattern and a minimal PNG writer (stored
// zlib blocks, no compression), independent of the WIC decoder under test.
#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace horus::hook::test {

// width * height RGBA8 pixels whose every channel depends on x and y, alpha included.
inline std::vector<uint8_t> pattern_rgba(uint32_t width, uint32_t height) {
    std::vector<uint8_t> rgba(static_cast<size_t>(width) * height * 4);
    for (uint32_t y = 0; y < height; ++y) {
        for (uint32_t x = 0; x < width; ++x) {
            uint8_t* p = &rgba[(static_cast<size_t>(y) * width + x) * 4];
            p[0] = static_cast<uint8_t>(x * 7 + y);
            p[1] = static_cast<uint8_t>(y * 11 + 3);
            p[2] = static_cast<uint8_t>((x ^ y) * 5);
            p[3] = static_cast<uint8_t>(255 - ((x + 2 * y) * 3) % 256);
        }
    }
    return rgba;
}

inline uint32_t png_crc(const uint8_t* data, size_t size, uint32_t crc = 0xFFFFFFFFu) {
    for (size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (int k = 0; k < 8; ++k) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return crc;
}

// Writes an 8-bit RGBA PNG; false if the file cannot be written.
inline bool write_png(const std::string& path, uint32_t width, uint32_t height, const std::vector<uint8_t>& rgba) {
    auto be32 = [](std::vector<uint8_t>& out, uint32_t v) {
        for (int shift = 24; shift >= 0; shift -= 8) out.push_back(static_cast<uint8_t>(v >> shift));
    };
    std::vector<uint8_t> raw;  // each row: filter type 0, then the pixels
    for (uint32_t y = 0; y < height; ++y) {
        raw.push_back(0);
        raw.insert(raw.end(), rgba.begin() + static_cast<size_t>(y) * width * 4,
                   rgba.begin() + static_cast<size_t>(y + 1) * width * 4);
    }
    std::vector<uint8_t> zlib = {0x78, 0x01};
    for (size_t at = 0; at < raw.size() || at == 0;) {
        const size_t n = std::min<size_t>(65535, raw.size() - at);
        zlib.push_back(at + n == raw.size() ? 1 : 0);
        zlib.push_back(static_cast<uint8_t>(n));
        zlib.push_back(static_cast<uint8_t>(n >> 8));
        zlib.push_back(static_cast<uint8_t>(~n));
        zlib.push_back(static_cast<uint8_t>(~n >> 8));
        zlib.insert(zlib.end(), raw.begin() + at, raw.begin() + at + n);
        at += n;
        if (n == 0) break;
    }
    uint32_t a = 1, b = 0;
    for (uint8_t byte : raw) {
        a = (a + byte) % 65521;
        b = (b + a) % 65521;
    }
    be32(zlib, (b << 16) | a);

    std::vector<uint8_t> png = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
    auto chunk = [&](const char* type, const std::vector<uint8_t>& data) {
        be32(png, static_cast<uint32_t>(data.size()));
        const size_t start = png.size();
        png.insert(png.end(), type, type + 4);
        png.insert(png.end(), data.begin(), data.end());
        be32(png, png_crc(&png[start], png.size() - start) ^ 0xFFFFFFFFu);
    };
    std::vector<uint8_t> ihdr;
    be32(ihdr, width);
    be32(ihdr, height);
    ihdr.insert(ihdr.end(), {8, 6, 0, 0, 0});  // 8 bits, RGBA, deflate, adaptive filtering, no interlace
    chunk("IHDR", ihdr);
    chunk("IDAT", zlib);
    chunk("IEND", {});

    FILE* file = std::fopen(path.c_str(), "wb");
    if (!file) return false;
    const bool ok = std::fwrite(png.data(), 1, png.size(), file) == png.size();
    return std::fclose(file) == 0 && ok;
}

}  // namespace horus::hook::test

// Pure parts of horus_capture's shared view (gta/capture/README.md "Shared view"), shared by
// capture.cpp, compositor.hpp and gta/capture/tests/capture_tests.cpp.
//
// HorusGta marks every frame in shared view with an 8x8 px rectangle in the top-left corner: black
// on drone ticks, white on gameplay-camera ticks. A frame is a drone frame iff texel (2, 2) has
// max(R, G, B) <= 16/255; anything else (including no marker at all) is a player frame. The capture
// worker classifies the raw readback bytes with frame_is_drone(); the compositor's pixel shader
// (composite.hlsl) compares the loaded texel with drone_marker_threshold(), which sits between the
// last drone and the first player value of each format so both decide identically.
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <dxgiformat.h>

#include <cstdint>
#include <cstring>

namespace horus::capture {

constexpr uint32_t kMarkerTexel = 2;           // x and y of the classified texel
constexpr uint32_t kDroneMax8 = 16;            // 8-bit channels: <= 16/255
constexpr uint32_t kDroneMax10 = 64;           // 10-bit channels: <= 16/255 * 1023 = 64.2
constexpr float kDroneMaxFloat = 0.0627f;      // float channels
constexpr double kPublishSlackSeconds = 0.002;  // publish throttle tolerance for frame-time jitter

inline float half_to_float(uint16_t h) {
    const uint32_t sign = (h & 0x8000u) << 16;
    uint32_t exponent = (h >> 10) & 0x1fu;
    uint32_t mantissa = h & 0x3ffu;
    uint32_t bits;
    if (exponent == 0) {
        if (mantissa == 0) {
            bits = sign;
        } else {  // subnormal: renormalise
            exponent = 127 - 15 + 1;
            while ((mantissa & 0x400u) == 0) {
                mantissa <<= 1;
                --exponent;
            }
            bits = sign | (exponent << 23) | ((mantissa & 0x3ffu) << 13);
        }
    } else if (exponent == 31) {
        bits = sign | 0x7f800000u | (mantissa << 13);
    } else {
        bits = sign | ((exponent - 15 + 127) << 23) | (mantissa << 13);
    }
    float value;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

// Bytes per texel of the formats the capture reads back, 0 for any other.
inline uint32_t texel_bytes(DXGI_FORMAT format) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
        case DXGI_FORMAT_R10G10B10A2_UNORM:
            return 4;
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
            return 8;
        default:
            return 0;
    }
}

// One texel of `format` (raw bytes) classified by the marker rule.
inline bool texel_is_drone(DXGI_FORMAT format, const uint8_t* texel) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:  // raw (encoded) bytes, as the compositor's UNORM views
            return texel[0] <= kDroneMax8 && texel[1] <= kDroneMax8 && texel[2] <= kDroneMax8;
        case DXGI_FORMAT_R10G10B10A2_UNORM: {
            uint32_t v;
            std::memcpy(&v, texel, 4);
            return (v & 1023u) <= kDroneMax10 && ((v >> 10) & 1023u) <= kDroneMax10 &&
                   ((v >> 20) & 1023u) <= kDroneMax10;
        }
        case DXGI_FORMAT_R16G16B16A16_FLOAT: {
            uint16_t rgb[3];
            std::memcpy(rgb, texel, sizeof(rgb));
            // NaN compares false: a player frame.
            return half_to_float(rgb[0]) <= kDroneMaxFloat && half_to_float(rgb[1]) <= kDroneMaxFloat &&
                   half_to_float(rgb[2]) <= kDroneMaxFloat;
        }
        default:
            return false;
    }
}

// A whole frame (row-major, `row_pitch` bytes per row) classified by its marker texel. Frames too
// small to hold the texel, and unknown formats, are player frames.
inline bool frame_is_drone(DXGI_FORMAT format, const uint8_t* pixels, uint32_t row_pitch, uint32_t width,
                           uint32_t height) {
    const uint32_t bytes = texel_bytes(format);
    if (bytes == 0 || width <= kMarkerTexel || height <= kMarkerTexel) return false;
    return texel_is_drone(format, pixels + static_cast<size_t>(kMarkerTexel) * row_pitch + kMarkerTexel * bytes);
}

// The compositor's shader-side threshold for a view of `format` (typed, non-sRGB): the loaded
// texel is a drone marker iff max(r, g, b) <= this. For UNORM formats it lies halfway between the
// largest drone value and the next code, so the GPU's UNORM-to-float rounding cannot flip a
// decision; 0 for formats the compositor does not handle.
inline float drone_marker_threshold(DXGI_FORMAT format) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
            return (kDroneMax8 + 0.5f) / 255.0f;
        case DXGI_FORMAT_R10G10B10A2_UNORM:
            return (kDroneMax10 + 0.5f) / 1023.0f;
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
            return kDroneMaxFloat;
        default:
            return 0.0f;
    }
}

// Worker-side rate limit for shared view: drone frames are published at most max_hz times per
// second, measured between the Presents of the published frames, with kPublishSlackSeconds of
// tolerance so frame-time jitter does not drop a frame that is due.
class PublishThrottle {
public:
    explicit PublishThrottle(double max_hz) : period_(1.0 / max_hz - kPublishSlackSeconds) {}

    // `present_s`: when the frame was presented. True (and remembered) if it may be published.
    bool admit(double present_s) {
        if (published_ && present_s - last_ < period_) return false;
        published_ = true;
        last_ = present_s;
        return true;
    }

private:
    double period_;
    double last_ = 0.0;
    bool published_ = false;
};

}  // namespace horus::capture

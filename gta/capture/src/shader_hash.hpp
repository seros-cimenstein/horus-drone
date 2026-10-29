// FNV-1a (32-bit) of a shader source, recorded in composite_shaders.hpp by tools/shader_gen.cpp and
// checked by tests/capture_tests.cpp, so a shader edit without regenerating the header is caught.
#pragma once

#include <cstdint>
#include <string>

namespace horus::capture {

inline uint32_t fnv1a(const std::string& data) {
    uint32_t hash = 2166136261u;
    for (const char c : data) {
        hash ^= static_cast<unsigned char>(c);
        hash *= 16777619u;
    }
    return hash;
}

}  // namespace horus::capture

// Shared-memory block `Local\HorusGtaCamera` through which HorusGta.dll (gta/horus_mod, C#,
// `CameraBlock.cs`) tells horus_capture.asi whether the drone camera is on screen. The script
// updates it on every tick; the capture plugin reads it at every Present and stamps it into the
// frame header (gta/capture/README.md "Protocol").
//
// Layout (64 bytes, little-endian), version 1:
//   0 u32 magic "HGCM"   4 u32 version   8 u64 sequence (seqlock: odd while the script writes,
//   +2 per tick)   16 u32 active (1 while HorusGta drives the camera)   20 u32 reserved
//   24 u64 pose_seq (seq of the last applied pose packet)   32 u64 game_frame (GET_FRAME_COUNT)
//   40 u32 view_mode (kViewModeDrone / kViewModeShared; zero in blocks of older scripts)
//   44..63 reserved
//
// Scripts do not tick while the game is paused, so the block then keeps saying active=1. The
// reader therefore counts presents since the sequence last changed and reports the camera active
// only while the script ticked within kMaxScriptAgePresents presents. The same holds for the
// shared view: a stale block reads as kViewModeDrone, so a paused game is never composited.
#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <atomic>
#include <cstdint>
#include <cstring>

namespace horus {

constexpr wchar_t kCameraBlockName[] = L"Local\\HorusGtaCamera";
constexpr uint32_t kCameraBlockSize = 64;
constexpr uint32_t kCameraBlockMagic = 0x4D434748u;  // "HGCM"
constexpr uint32_t kCameraBlockVersion = 1;
// Scripts tick once per game frame, a frame ahead of or behind its Present; 2 allows that jitter.
// The cost: the first one or two presents after the game pauses may still count as active.
constexpr uint64_t kMaxScriptAgePresents = 2;
constexpr uint32_t kScriptAgeNever = 0xFFFFFFFFu;
// view_mode: the drone camera is on screen (HorusGta's default, and what older scripts leave there).
constexpr uint32_t kViewModeDrone = 0;
// view_mode: HorusGta alternates drone and gameplay camera ticks and marks each frame in its top-left
// corner; horus_capture keeps the drone frames off the screen (gta/capture/README.md "Shared view").
constexpr uint32_t kViewModeShared = 1;

struct CameraFields {
    uint64_t sequence = 0;
    uint32_t active = 0;
    uint64_t pose_seq = 0;
    uint64_t game_frame = 0;
    uint32_t view_mode = kViewModeDrone;
};

// Consistent snapshot of the block (seqlock read); false if it is not a version-1 block or the
// writer was mid-update on every attempt.
inline bool read_camera_block(const volatile uint8_t* base, CameraFields& out) {
    auto u32 = [base](size_t offset) {
        uint32_t v;
        std::memcpy(&v, const_cast<const uint8_t*>(base) + offset, sizeof(v));
        return v;
    };
    auto u64 = [base](size_t offset) {
        uint64_t v;
        std::memcpy(&v, const_cast<const uint8_t*>(base) + offset, sizeof(v));
        return v;
    };
    for (int attempt = 0; attempt < 4; ++attempt) {
        std::atomic_thread_fence(std::memory_order_acquire);
        if (u32(0) != kCameraBlockMagic || u32(4) != kCameraBlockVersion) return false;
        const uint64_t before = u64(8);
        if (before & 1) continue;
        std::atomic_thread_fence(std::memory_order_acquire);
        CameraFields fields;
        fields.active = u32(16);
        fields.pose_seq = u64(24);
        fields.game_frame = u64(32);
        fields.view_mode = u32(40);
        std::atomic_thread_fence(std::memory_order_acquire);
        if (u64(8) != before) continue;
        fields.sequence = before;
        out = fields;
        return true;
    }
    return false;
}

// What a frame presented now carries in its header.
struct CameraStamp {
    uint32_t active = 0;                       // block active and script ticked recently
    uint32_t script_age = kScriptAgeNever;     // presents since the script last ticked
    uint64_t pose_seq = 0;
    uint64_t game_frame = 0;
    uint32_t view_mode = kViewModeDrone;     // block's view mode while the script ticks recently
};

// Render-thread state: call observe() once per Present with that Present's index.
class CameraTracker {
public:
    // `read` is the block snapshot at this Present, or nullptr when none could be read.
    CameraStamp observe(uint64_t present_index, const CameraFields* read) {
        if (read && (!seen_ || read->sequence != last_.sequence)) {
            last_ = *read;
            last_change_present_ = present_index;
            seen_ = true;
        }
        CameraStamp stamp;
        if (!seen_) return stamp;
        const uint64_t age = present_index - last_change_present_;
        stamp.script_age = age >= kScriptAgeNever ? kScriptAgeNever - 1 : static_cast<uint32_t>(age);
        const bool fresh = age <= kMaxScriptAgePresents;
        stamp.active = (last_.active == 1 && fresh) ? 1u : 0u;
        stamp.pose_seq = last_.pose_seq;
        stamp.game_frame = last_.game_frame;
        stamp.view_mode = fresh ? last_.view_mode : kViewModeDrone;
        return stamp;
    }

private:
    CameraFields last_;
    uint64_t last_change_present_ = 0;
    bool seen_ = false;
};

// Writer side, for the capture selftest standing in for HorusGta (the script writes the same way
// from C#): one tick's update under the seqlock.
inline void write_camera_block(volatile uint8_t* base, uint32_t active, uint64_t pose_seq, uint64_t game_frame,
                               uint32_t view_mode = kViewModeDrone) {
    uint8_t* p = const_cast<uint8_t*>(base);
    const uint32_t header[2] = {kCameraBlockMagic, kCameraBlockVersion};
    std::memcpy(p, header, sizeof(header));
    uint64_t sequence;
    std::memcpy(&sequence, p + 8, sizeof(sequence));
    sequence |= 1;  // odd: writing
    std::memcpy(p + 8, &sequence, sizeof(sequence));
    std::atomic_thread_fence(std::memory_order_release);
    std::memcpy(p + 16, &active, sizeof(active));
    std::memcpy(p + 24, &pose_seq, sizeof(pose_seq));
    std::memcpy(p + 32, &game_frame, sizeof(game_frame));
    std::memcpy(p + 40, &view_mode, sizeof(view_mode));
    std::atomic_thread_fence(std::memory_order_release);
    ++sequence;  // even: done
    std::memcpy(p + 8, &sequence, sizeof(sequence));
}

// Create or open the block; nullptr on failure. The mapping lives for the process.
inline volatile uint8_t* open_camera_block() {
    HANDLE mapping =
        CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, kCameraBlockSize, kCameraBlockName);
    if (!mapping) return nullptr;
    void* view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, kCameraBlockSize);
    return static_cast<volatile uint8_t*>(view);
}

}  // namespace horus

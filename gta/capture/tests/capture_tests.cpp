// Unit tests of horus_capture's shared view (no game needed). Built by gta/build.sh; run under wine:
//   WINEDEBUG=-all wine gta/build/horus_capture_tests.exe gta/capture/src/composite.hlsl
// Pure parts: the marker classification of every supported format (and that the compositor's
// shader threshold decides identically), the worker's publish throttle, the camera block's
// view_mode, and that composite_shaders.hpp was generated from the current composite.hlsl. With a
// D3D12 device (wine's vkd3d on the laptop; Proton's vkd3d-proton on Koko) it also runs the
// compositor on off-screen "back buffers" and compares every pixel with what must reach the screen.
// Exit 0 when every check passes, 1 otherwise (each failure is printed).

#define WIDL_EXPLICIT_AGGREGATE_RETURNS  // MSVC ABI for methods returning structs (GetDesc).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>

#include "camera_block.hpp"
#include "compositor.hpp"
#include "shader_hash.hpp"
#include "shared_view.hpp"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {

int g_checks = 0;
int g_failures = 0;

#define CHECK(condition)                                                          \
    do {                                                                          \
        ++g_checks;                                                               \
        if (!(condition)) {                                                       \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);      \
            ++g_failures;                                                         \
        }                                                                         \
    } while (0)

using namespace horus::capture;

// Counts mismatches instead of one CHECK per value, so exhaustive loops stay readable.
void check_all(const char* what, int mismatches) {
    if (mismatches) std::printf("FAIL %s: %d mismatching value(s)\n", what, mismatches);
    ++g_checks;
    g_failures += mismatches ? 1 : 0;
}

void test_texel_8bit() {
    const DXGI_FORMAT formats[] = {DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM,
                                   DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB};
    int cpu = 0;
    int gpu = 0;
    for (const DXGI_FORMAT format : formats) {
        const float threshold = drone_marker_threshold(
            format == DXGI_FORMAT_B8G8R8A8_UNORM || format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB
                ? DXGI_FORMAT_B8G8R8A8_UNORM
                : DXGI_FORMAT_R8G8B8A8_UNORM);
        for (int channel = 0; channel < 3; ++channel) {
            for (int v = 0; v < 256; ++v) {
                uint8_t texel[8] = {0, 0, 0, 255};  // 8: room for any format's texel
                texel[channel] = static_cast<uint8_t>(v);
                const bool expected = v <= 16;
                cpu += texel_is_drone(format, texel) != expected;
                gpu += (static_cast<float>(v) / 255.0f <= threshold) != expected;
            }
        }
    }
    check_all("8-bit texel classification", cpu);
    check_all("8-bit shader threshold", gpu);
    const uint8_t transparent_black[4] = {0, 0, 0, 0};
    const uint8_t marker_white[4] = {255, 255, 255, 255};
    const uint8_t grey[4] = {16, 16, 16, 255};
    const uint8_t dark_red[4] = {17, 0, 0, 255};
    CHECK(texel_is_drone(DXGI_FORMAT_B8G8R8A8_UNORM, transparent_black));  // alpha is ignored
    CHECK(!texel_is_drone(DXGI_FORMAT_B8G8R8A8_UNORM, marker_white));
    CHECK(texel_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, grey));
    CHECK(!texel_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, dark_red));
}

void test_texel_10bit() {
    const float threshold = drone_marker_threshold(DXGI_FORMAT_R10G10B10A2_UNORM);
    int cpu = 0;
    int gpu = 0;
    for (int channel = 0; channel < 3; ++channel) {
        for (uint32_t v = 0; v < 1024; ++v) {
            const uint32_t packed = (3u << 30) | (v << (10 * channel));  // alpha bits set: ignored
            uint8_t texel[4];
            std::memcpy(texel, &packed, 4);
            const bool expected = v * 255u <= 16u * 1023u;  // v / 1023 <= 16 / 255
            cpu += texel_is_drone(DXGI_FORMAT_R10G10B10A2_UNORM, texel) != expected;
            gpu += (static_cast<float>(v) / 1023.0f <= threshold) != expected;
        }
    }
    check_all("10-bit texel classification", cpu);
    check_all("10-bit shader threshold", gpu);
    CHECK(kDroneMax10 == 64);
}

void test_texel_half() {
    CHECK(drone_marker_threshold(DXGI_FORMAT_R16G16B16A16_FLOAT) == kDroneMaxFloat);
    int cpu = 0;
    for (int channel = 0; channel < 3; ++channel) {
        for (uint32_t h = 0; h < 65536; ++h) {
            uint16_t texel[4] = {0, 0, 0, 0x3C00};
            texel[channel] = static_cast<uint16_t>(h);
            const float value = half_to_float(static_cast<uint16_t>(h));
            const bool expected = value <= 0.0627f;  // false for NaN
            cpu += texel_is_drone(DXGI_FORMAT_R16G16B16A16_FLOAT, reinterpret_cast<const uint8_t*>(texel)) != expected;
        }
    }
    check_all("half texel classification", cpu);
    auto drone = [](uint16_t r, uint16_t g, uint16_t b) {
        const uint16_t texel[4] = {r, g, b, 0x3C00};
        return texel_is_drone(DXGI_FORMAT_R16G16B16A16_FLOAT, reinterpret_cast<const uint8_t*>(texel));
    };
    CHECK(drone(0x0000, 0x0000, 0x0000));    // black marker
    CHECK(drone(0x2C03, 0x0000, 0x8000));    // 0.062683 (largest half <= 0.0627), -0
    CHECK(!drone(0x2C04, 0x0000, 0x0000));   // 0.062744
    CHECK(drone(0xBC00, 0xC000, 0x0000));    // negative scRGB values
    CHECK(!drone(0x3C00, 0x3C00, 0x3C00));   // white marker
    CHECK(!drone(0x7E00, 0x0000, 0x0000));   // NaN: player
    CHECK(!drone(0x0000, 0x7C00, 0x0000));   // +inf
    CHECK(half_to_float(0x2C00) == 0.0625f);
    CHECK(half_to_float(0x0001) == std::ldexp(1.0f, -24));  // smallest subnormal
}

void test_frame_is_drone() {
    // 6x4 BGRA8 frame with 32-byte rows (8 bytes of padding): the texel is (2, 2), not byte 2*6*4+8.
    std::vector<uint8_t> frame(32 * 4, 255);
    uint8_t* texel = frame.data() + 2 * 32 + 2 * 4;
    std::memset(texel, 0, 3);
    CHECK(frame_is_drone(DXGI_FORMAT_B8G8R8A8_UNORM, frame.data(), 32, 6, 4));
    CHECK(!frame_is_drone(DXGI_FORMAT_B8G8R8A8_UNORM, frame.data(), 24, 6, 4));  // wrong pitch: other texel
    std::fill(frame.begin(), frame.end(), 0);
    texel[1] = 200;
    CHECK(!frame_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, frame.data(), 32, 6, 4));
    // Too small to hold texel (2, 2), and formats the capture does not read: player.
    std::fill(frame.begin(), frame.end(), 0);
    CHECK(frame_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, frame.data(), 32, 3, 3));
    CHECK(!frame_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, frame.data(), 32, 2, 4));
    CHECK(!frame_is_drone(DXGI_FORMAT_R8G8B8A8_UNORM, frame.data(), 32, 6, 2));
    CHECK(!frame_is_drone(DXGI_FORMAT_R32G32B32A32_FLOAT, frame.data(), 32, 6, 4));
    // RGBA16F: 8 bytes per texel.
    std::vector<uint16_t> wide(16 * 4, 0x3C00);  // 4x4 texels, 32-byte rows
    CHECK(!frame_is_drone(DXGI_FORMAT_R16G16B16A16_FLOAT, reinterpret_cast<const uint8_t*>(wide.data()), 32, 4, 4));
    for (int c = 0; c < 3; ++c) wide[2 * 16 + 2 * 4 + c] = 0;
    CHECK(frame_is_drone(DXGI_FORMAT_R16G16B16A16_FLOAT, reinterpret_cast<const uint8_t*>(wide.data()), 32, 4, 4));
    // RGB10A2: white everywhere but the texel.
    std::vector<uint32_t> packed(4 * 4, 0xFFFFFFFFu);
    CHECK(!frame_is_drone(DXGI_FORMAT_R10G10B10A2_UNORM, reinterpret_cast<const uint8_t*>(packed.data()), 16, 4, 4));
    packed[2 * 4 + 2] = (3u << 30) | (64u << 20) | (64u << 10) | 64u;
    CHECK(frame_is_drone(DXGI_FORMAT_R10G10B10A2_UNORM, reinterpret_cast<const uint8_t*>(packed.data()), 16, 4, 4));
}

void test_publish_throttle() {
    PublishThrottle throttle(15.0);  // period 1/15 s - 2 ms = 64.67 ms
    CHECK(throttle.admit(100.0));    // the first frame always goes
    CHECK(!throttle.admit(100.050));
    CHECK(!throttle.admit(100.0646));
    CHECK(throttle.admit(100.0647));
    CHECK(!throttle.admit(100.100));
    CHECK(throttle.admit(100.1300));

    // Drone frames at 15 Hz with +-0.9 ms jitter: every one is published.
    PublishThrottle at_rate(15.0);
    int published = 0;
    for (int i = 0; i < 150; ++i) {
        const double jitter = (i % 2 ? 0.0009 : -0.0009);
        published += at_rate.admit(i / 15.0 + jitter) ? 1 : 0;
    }
    CHECK(published == 150);

    // Drone frames at 30 Hz (60 fps game, drone every 2nd tick): every other one, 15 Hz.
    PublishThrottle fast(15.0);
    published = 0;
    for (int i = 0; i < 300; ++i) published += fast.admit(i / 30.0) ? 1 : 0;
    CHECK(published == 150);

    // Drone frames at 20 Hz against max_hz 15: 50 ms apart, so only every other one passes (10 Hz).
    PublishThrottle twenty(15.0);
    published = 0;
    for (int i = 0; i < 200; ++i) published += twenty.admit(i / 20.0) ? 1 : 0;
    CHECK(published == 100);
}

void test_camera_block() {
    alignas(8) uint8_t block[horus::kCameraBlockSize] = {};
    volatile uint8_t* base = block;
    horus::CameraFields fields;
    CHECK(!horus::read_camera_block(base, fields));  // no magic yet

    // A block written by an older script: offset 40 stays zero -> drone view.
    horus::write_camera_block(base, 1, 5, 6);
    CHECK(horus::read_camera_block(base, fields));
    CHECK(fields.view_mode == horus::kViewModeDrone);
    CHECK(fields.active == 1 && fields.pose_seq == 5 && fields.game_frame == 6);

    horus::write_camera_block(base, 1, 7, 8, horus::kViewModeShared);
    CHECK(horus::read_camera_block(base, fields));
    CHECK(fields.view_mode == horus::kViewModeShared);
    uint32_t raw = 0;
    std::memcpy(&raw, block + 40, 4);
    CHECK(raw == 1);
    bool reserved_zero = true;
    for (uint32_t i = 44; i < horus::kCameraBlockSize; ++i) reserved_zero = reserved_zero && block[i] == 0;
    CHECK(reserved_zero);
    CHECK(fields.sequence % 2 == 0);

    // Mid-write (odd sequence) never reads.
    uint64_t sequence;
    std::memcpy(&sequence, block + 8, 8);
    const uint64_t odd = sequence | 1;
    std::memcpy(block + 8, &odd, 8);
    CHECK(!horus::read_camera_block(base, fields));
    std::memcpy(block + 8, &sequence, 8);

    // The tracker reports shared view only while the script ticks (age <= 2 presents).
    horus::CameraTracker tracker;
    CHECK(tracker.observe(1, nullptr).view_mode == horus::kViewModeDrone);
    CHECK(horus::read_camera_block(base, fields));
    CHECK(tracker.observe(2, &fields).view_mode == horus::kViewModeShared);
    CHECK(tracker.observe(3, &fields).view_mode == horus::kViewModeShared);
    CHECK(tracker.observe(4, &fields).view_mode == horus::kViewModeShared);
    const horus::CameraStamp stale = tracker.observe(5, &fields);  // paused: no tick for 3 presents
    CHECK(stale.view_mode == horus::kViewModeDrone);
    CHECK(stale.script_age == 3);
    horus::write_camera_block(base, 1, 9, 10, horus::kViewModeShared);
    CHECK(horus::read_camera_block(base, fields));
    CHECK(tracker.observe(6, &fields).view_mode == horus::kViewModeShared);
    horus::write_camera_block(base, 1, 11, 12, horus::kViewModeDrone);
    CHECK(horus::read_camera_block(base, fields));
    CHECK(tracker.observe(7, &fields).view_mode == horus::kViewModeDrone);
}

void test_shader_header(const char* hlsl_path) {
    std::ifstream in(hlsl_path, std::ios::binary);
    CHECK(static_cast<bool>(in));
    const std::string source((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    const bool current = fnv1a(source) == kCompositeHlslHash;
    if (!current) {
        std::printf("composite_shaders.hpp is not generated from %s; regenerate it (gta/capture/README.md)\n",
                    hlsl_path);
    }
    CHECK(current);
    CHECK(sizeof(kCompositeVs) > 32 && std::memcmp(kCompositeVs, "DXBC", 4) == 0);
    CHECK(sizeof(kCompositePs) > 32 && std::memcmp(kCompositePs, "DXBC", 4) == 0);
}

// ---------------------------------------------------------------------------------------------
// The compositor on a real D3D12 device

constexpr UINT kWidth = 64;
constexpr UINT kHeight = 32;

struct Gpu {
    ID3D12Device* device = nullptr;
    ID3D12CommandQueue* queue = nullptr;
    ID3D12CommandAllocator* allocator = nullptr;
    ID3D12GraphicsCommandList* list = nullptr;
    ID3D12Fence* fence = nullptr;
    HANDLE event = nullptr;
    uint64_t fence_value = 0;
    ID3D12DescriptorHeap* rtv_heap = nullptr;

    bool init() {
        if (FAILED(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) return false;
        D3D12_COMMAND_QUEUE_DESC queue_desc = {};
        queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        D3D12_DESCRIPTOR_HEAP_DESC heap_desc = {};
        heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
        heap_desc.NumDescriptors = 1;
        event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        return SUCCEEDED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue))) &&
               SUCCEEDED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))) &&
               SUCCEEDED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, nullptr,
                                                   IID_PPV_ARGS(&list))) &&
               SUCCEEDED(list->Close()) &&
               SUCCEEDED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence))) &&
               SUCCEEDED(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&rtv_heap))) && event;
    }

    bool finish() {
        queue->Signal(fence, ++fence_value);
        if (fence->GetCompletedValue() >= fence_value) return true;
        fence->SetEventOnCompletion(fence_value, event);
        return WaitForSingleObject(event, 10000) == WAIT_OBJECT_0;
    }

    void begin() {
        allocator->Reset();
        list->Reset(allocator, nullptr);
    }

    bool submit() {
        list->Close();
        ID3D12CommandList* lists[] = {list};
        queue->ExecuteCommandLists(1, lists);
        return finish();
    }
};

void STDMETHODCALLTYPE execute_on(ID3D12CommandQueue* queue, UINT count, ID3D12CommandList* const* lists) {
    queue->ExecuteCommandLists(count, lists);
}

D3D12_RESOURCE_BARRIER transition(ID3D12Resource* resource, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = resource;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = before;
    barrier.Transition.StateAfter = after;
    return barrier;
}

ID3D12Resource* create_back_buffer(Gpu& gpu, DXGI_FORMAT format) {
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = kWidth;
    desc.Height = kHeight;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.Format = format;
    desc.SampleDesc.Count = 1;
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    ID3D12Resource* texture = nullptr;
    gpu.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COMMON, nullptr,
                                        IID_PPV_ARGS(&texture));
    return texture;
}

// A frame as HorusGta leaves it: background, an object at x 20..29 y 10..19, the 8x8 marker.
struct Scene {
    float background[4];
    float object[4];
    const float* marker;
};

bool fill(Gpu& gpu, ID3D12Resource* target, DXGI_FORMAT view_format, const Scene& scene) {
    D3D12_RENDER_TARGET_VIEW_DESC rtv_desc = {};
    rtv_desc.Format = view_format;
    rtv_desc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv = gpu.rtv_heap->GetCPUDescriptorHandleForHeapStart();
    gpu.device->CreateRenderTargetView(target, &rtv_desc, rtv);
    gpu.begin();
    D3D12_RESOURCE_BARRIER barrier =
        transition(target, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_RENDER_TARGET);
    gpu.list->ResourceBarrier(1, &barrier);
    gpu.list->ClearRenderTargetView(rtv, scene.background, 0, nullptr);
    const D3D12_RECT object = {20, 10, 30, 20};
    gpu.list->ClearRenderTargetView(rtv, scene.object, 1, &object);
    const D3D12_RECT marker = {0, 0, 8, 8};
    gpu.list->ClearRenderTargetView(rtv, scene.marker, 1, &marker);
    barrier = transition(target, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COMMON);
    gpu.list->ResourceBarrier(1, &barrier);
    return gpu.submit();
}

// The texture's texels, rows packed.
std::vector<uint8_t> read_back(Gpu& gpu, ID3D12Resource* texture) {
    const D3D12_RESOURCE_DESC desc = texture->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
    UINT rows = 0;
    UINT64 row_bytes = 0;
    UINT64 total = 0;
    gpu.device->GetCopyableFootprints(&desc, 0, 1, 0, &footprint, &rows, &row_bytes, &total);
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC buffer_desc = {};
    buffer_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer_desc.Width = total;
    buffer_desc.Height = 1;
    buffer_desc.DepthOrArraySize = 1;
    buffer_desc.MipLevels = 1;
    buffer_desc.SampleDesc.Count = 1;
    buffer_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource* buffer = nullptr;
    std::vector<uint8_t> pixels;
    if (FAILED(gpu.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer_desc,
                                                   D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&buffer)))) {
        return pixels;
    }
    gpu.begin();
    D3D12_RESOURCE_BARRIER barrier =
        transition(texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
    gpu.list->ResourceBarrier(1, &barrier);
    D3D12_TEXTURE_COPY_LOCATION dst = {};
    dst.pResource = buffer;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint = footprint;
    D3D12_TEXTURE_COPY_LOCATION src = {};
    src.pResource = texture;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    gpu.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    barrier = transition(texture, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    gpu.list->ResourceBarrier(1, &barrier);
    void* mapped = nullptr;
    if (gpu.submit() && SUCCEEDED(buffer->Map(0, nullptr, &mapped))) {
        pixels.resize(static_cast<size_t>(row_bytes) * rows);
        for (UINT y = 0; y < rows; ++y) {
            std::memcpy(pixels.data() + y * row_bytes,
                        static_cast<const uint8_t*>(mapped) + footprint.Offset + y * footprint.Footprint.RowPitch,
                        static_cast<size_t>(row_bytes));
        }
        buffer->Unmap(0, nullptr);
    }
    buffer->Release();
    return pixels;
}

// Channel value whose stored code is `code`: 8-bit and 10-bit UNORM codes, half bit patterns.
float code_value(DXGI_FORMAT view_format, uint32_t code) {
    if (view_format == DXGI_FORMAT_R16G16B16A16_FLOAT) return half_to_float(static_cast<uint16_t>(code));
    return static_cast<float>(code) / (view_format == DXGI_FORMAT_R10G10B10A2_UNORM ? 1023.0f : 255.0f);
}

void test_compositor_format(Gpu& gpu, DXGI_FORMAT format, const char* name) {
    const DXGI_FORMAT view_format = composite_view_format(format);
    ID3D12Resource* target = create_back_buffer(gpu, format);
    CHECK(target != nullptr);
    if (!target) return;
    // Marker values just inside and just outside the drone rule, per format.
    uint32_t edge_drone = kDroneMax8;
    uint32_t edge_player = kDroneMax8 + 1;
    if (view_format == DXGI_FORMAT_R10G10B10A2_UNORM) {
        edge_drone = kDroneMax10;
        edge_player = kDroneMax10 + 1;
    } else if (view_format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
        edge_drone = 0x2C03;  // 0.062683
        edge_player = 0x2C04;  // 0.062744
    }
    const float black[4] = {0, 0, 0, 1};
    const float white[4] = {1, 1, 1, 1};
    const float near_drone[4] = {code_value(view_format, edge_drone), 0, code_value(view_format, edge_drone), 1};
    const float near_player[4] = {0, code_value(view_format, edge_player), 0, 1};
    struct Step {
        Scene scene;
        bool drone;     // the marker rule's verdict on this frame
        int shows;      // index of the step whose frame must be on screen afterwards
        bool restart;   // Compositor::start() before this step
    };
    const Step steps[] = {
        {{{0.8f, 0.1f, 0.1f, 1}, {0.1f, 0.1f, 0.9f, 1}, black}, true, 0, false},         // first: seeds saved
        {{{0.1f, 0.7f, 0.2f, 1}, {0.9f, 0.9f, 0.1f, 1}, white}, false, 1, false},        // player
        {{{0.2f, 0.2f, 0.8f, 1}, {0.5f, 0.3f, 0.1f, 1}, black}, true, 1, false},         // drone: hidden
        {{{0.6f, 0.6f, 0.6f, 1}, {0.2f, 0.4f, 0.6f, 1}, near_drone}, true, 1, false},    // edge drone value
        {{{0.3f, 0.5f, 0.7f, 1}, {0.7f, 0.5f, 0.3f, 1}, near_player}, false, 4, false},  // edge player value
        {{{0.9f, 0.4f, 0.0f, 1}, {0.0f, 0.4f, 0.9f, 1}, black}, true, 4, false},         // drone: hidden
        {{{0.4f, 0.0f, 0.4f, 1}, {0.0f, 0.9f, 0.0f, 1}, black}, true, 6, true},          // restart seeds saved
        {{{0.0f, 0.3f, 0.3f, 1}, {1.0f, 0.0f, 0.5f, 1}, white}, false, 7, false},        // player
    };
    Compositor compositor(&execute_on);
    std::vector<std::vector<uint8_t>> inputs;
    int wrong = 0;
    for (size_t i = 0; i < sizeof(steps) / sizeof(steps[0]); ++i) {
        if (!fill(gpu, target, view_format, steps[i].scene)) {
            std::printf("FAIL %s step %zu: GPU did not finish the fill\n", name, i);
            ++g_failures;
            break;
        }
        inputs.push_back(read_back(gpu, target));
        if (steps[i].restart) compositor.start();
        std::string message;
        const Compositor::Result result = compositor.composite(gpu.queue, target, message);
        if (i == 0) {
            if (message.find("compositor ready") != 0) std::printf("%s: %s\n", name, message.c_str());
            CHECK(message.find("compositor ready") == 0);
        } else {
            CHECK(message.empty());
        }
        CHECK(result == Compositor::Result::kRecorded);
        CHECK(gpu.finish());
        const std::vector<uint8_t> shown = read_back(gpu, target);
        const std::vector<uint8_t>& expected = inputs[static_cast<size_t>(steps[i].shows)];
        if (shown.empty() || shown != expected) {
            std::printf("FAIL %s step %zu: the screen does not show step %d's frame\n", name, i, steps[i].shows);
            ++wrong;
        }
        // The marker rule on the CPU agrees with what the GPU did.
        CHECK(frame_is_drone(view_format, inputs.back().data(), kWidth * texel_bytes(view_format), kWidth, kHeight) ==
              steps[i].drone);
    }
    check_all(name, wrong);

    // No free allocator: the GPU is held behind a fence, so after kFrames composites the next one is
    // skipped instead of waiting; released, the GPU finishes them all.
    ID3D12Fence* gate = nullptr;
    CHECK(SUCCEEDED(gpu.device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&gate))));
    if (gate) {
        gpu.queue->Wait(gate, 1);
        int recorded = 0;
        std::string message;
        for (int i = 0; i < Compositor::kFrames; ++i) {
            recorded += compositor.composite(gpu.queue, target, message) == Compositor::Result::kRecorded;
        }
        CHECK(recorded == Compositor::kFrames);
        CHECK(compositor.composite(gpu.queue, target, message) == Compositor::Result::kBusy);
        gate->Signal(1);
        CHECK(gpu.finish());
        CHECK(compositor.composite(gpu.queue, target, message) == Compositor::Result::kRecorded);
        CHECK(compositor.wait_idle());
        gate->Release();
    }
    target->Release();
}

void test_compositor() {
    Gpu gpu;
    if (!gpu.init()) {
        std::printf("SKIP compositor: no D3D12 device\n");
        return;
    }
    test_compositor_format(gpu, DXGI_FORMAT_B8G8R8A8_UNORM, "compositor B8G8R8A8_UNORM");
    test_compositor_format(gpu, DXGI_FORMAT_R8G8B8A8_UNORM, "compositor R8G8B8A8_UNORM");
    test_compositor_format(gpu, DXGI_FORMAT_R8G8B8A8_TYPELESS, "compositor R8G8B8A8_TYPELESS");
    test_compositor_format(gpu, DXGI_FORMAT_R10G10B10A2_UNORM, "compositor R10G10B10A2_UNORM");
    test_compositor_format(gpu, DXGI_FORMAT_R16G16B16A16_FLOAT, "compositor R16G16B16A16_FLOAT");

    // sRGB back buffers are not composited (an sRGB view would re-encode the copied values).
    ID3D12Resource* srgb = create_back_buffer(gpu, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB);
    if (srgb) {
        Compositor compositor(&execute_on);
        std::string message;
        CHECK(compositor.composite(gpu.queue, srgb, message) == Compositor::Result::kUnavailable);
        CHECK(message.find("compositor disabled: unsupported back buffer") == 0);
        message.clear();
        CHECK(compositor.composite(gpu.queue, srgb, message) == Compositor::Result::kUnavailable);
        CHECK(message.empty());  // reported once per back buffer change
        srgb->Release();
    }
    std::printf("compositor checked on a D3D12 device\n");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s composite.hlsl\n", argv[0]);
        return 2;
    }
    test_texel_8bit();
    test_texel_10bit();
    test_texel_half();
    test_frame_is_drone();
    test_publish_throttle();
    test_camera_block();
    test_shader_header(argv[1]);
    test_compositor();
    if (g_failures) {
        std::printf("%d of %d check(s) FAILED\n", g_failures, g_checks);
        return 1;
    }
    std::printf("all capture tests passed (%d checks)\n", g_checks);
    return 0;
}

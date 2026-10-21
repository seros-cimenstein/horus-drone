// horus_capture.asi: reads the drone camera straight out of GTA V Enhanced's D3D12 renderer.
//
// At every throttled IDXGISwapChain::Present the back buffer is copied on the game's own direct
// queue into a CPU-readable buffer (ring of 3, so the render thread never waits on the GPU). A
// worker thread converts finished copies to BGR8 and a TCP server hands the newest frame to one
// client per request (protocol: horus/perception/gta_stream.py).
//
// The presenting queue is captured when the game creates its swap chain; if this plugin loads too
// late for that, the last direct queue seen in ExecuteCommandLists on the same device is used.
//
// Every Present also reads HorusGta's camera block (gta/common/camera_block.hpp) so each frame
// says whether the drone camera was on screen and which pose packet it showed.

#define WIDL_EXPLICIT_AGGREGATE_RETURNS  // MSVC ABI for methods returning structs (GetDesc).
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <MinHook.h>

#include "camera_block.hpp"
#include "dxgi_probe.hpp"
#include "swapchain_queues.hpp"

#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

// ---------------------------------------------------------------------------------------------
// Config and logging (both live next to the .asi)

struct Config {
    std::string bind = "127.0.0.1";
    int port = 5610;
    double max_hz = 15.0;
};

Config g_config;
std::wstring g_module_dir;
std::mutex g_log_mutex;
FILE* g_log = nullptr;

void log(const char* fmt, ...) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    if (!g_log) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    std::fprintf(g_log, "%02u:%02u:%02u.%03u ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list args;
    va_start(args, fmt);
    std::vfprintf(g_log, fmt, args);
    va_end(args);
    std::fputc('\n', g_log);
    std::fflush(g_log);
}

void load_config() {
    const std::wstring ini = g_module_dir + L"\\horus_capture.ini";
    wchar_t buffer[128];
    char narrow[128];
    GetPrivateProfileStringW(L"capture", L"bind", L"127.0.0.1", buffer, 128, ini.c_str());
    WideCharToMultiByte(CP_UTF8, 0, buffer, -1, narrow, sizeof(narrow), nullptr, nullptr);
    g_config.bind = narrow;
    g_config.port = static_cast<int>(GetPrivateProfileIntW(L"capture", L"port", 5610, ini.c_str()));
    GetPrivateProfileStringW(L"capture", L"max_hz", L"15", buffer, 128, ini.c_str());
    const double hz = _wtof(buffer);
    g_config.max_hz = (std::isfinite(hz) && hz > 0.0) ? hz : 15.0;
}

uint64_t unix_time_ns() {
    FILETIME ft;
    GetSystemTimePreciseAsFileTime(&ft);
    const uint64_t ticks = (static_cast<uint64_t>(ft.dwHighDateTime) << 32) | ft.dwLowDateTime;
    return (ticks - 116444736000000000ULL) * 100ULL;
}

double seconds_now() {
    static const double frequency = [] {
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        return static_cast<double>(f.QuadPart);
    }();
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    return static_cast<double>(counter.QuadPart) / frequency;
}

// ---------------------------------------------------------------------------------------------
// Pixel conversion to tightly packed BGR8

bool supported_format(DXGI_FORMAT format) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
        case DXGI_FORMAT_R10G10B10A2_UNORM:
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
            return true;
        default:
            return false;
    }
}

float half_to_float(uint16_t h) {
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

// scRGB (linear, 1.0 = SDR white) half -> sRGB-encoded byte, for every possible half bit pattern.
const std::vector<uint8_t>& half_to_srgb8() {
    static const std::vector<uint8_t> table = [] {
        std::vector<uint8_t> t(65536);
        for (uint32_t i = 0; i < 65536; ++i) {
            float v = half_to_float(static_cast<uint16_t>(i));
            if (!(v > 0.0f)) v = 0.0f;  // negatives and NaN
            if (v > 1.0f) v = 1.0f;
            const float s = v <= 0.0031308f ? v * 12.92f : 1.055f * std::pow(v, 1.0f / 2.4f) - 0.055f;
            t[i] = static_cast<uint8_t>(std::lround(s * 255.0f));
        }
        return t;
    }();
    return table;
}

void convert_to_bgr(DXGI_FORMAT format, const uint8_t* src, uint32_t row_pitch, uint32_t width,
                    uint32_t height, uint8_t* dst) {
    for (uint32_t y = 0; y < height; ++y) {
        const uint8_t* s = src + static_cast<size_t>(y) * row_pitch;
        uint8_t* d = dst + static_cast<size_t>(y) * width * 3;
        switch (format) {
            case DXGI_FORMAT_R8G8B8A8_UNORM:
            case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
                for (uint32_t x = 0; x < width; ++x, s += 4, d += 3) {
                    d[0] = s[2];
                    d[1] = s[1];
                    d[2] = s[0];
                }
                break;
            case DXGI_FORMAT_B8G8R8A8_UNORM:
            case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
                for (uint32_t x = 0; x < width; ++x, s += 4, d += 3) {
                    d[0] = s[0];
                    d[1] = s[1];
                    d[2] = s[2];
                }
                break;
            case DXGI_FORMAT_R10G10B10A2_UNORM:
                for (uint32_t x = 0; x < width; ++x, s += 4, d += 3) {
                    uint32_t v;
                    std::memcpy(&v, s, 4);
                    d[2] = static_cast<uint8_t>(((v & 1023u) * 255u + 511u) / 1023u);
                    d[1] = static_cast<uint8_t>((((v >> 10) & 1023u) * 255u + 511u) / 1023u);
                    d[0] = static_cast<uint8_t>((((v >> 20) & 1023u) * 255u + 511u) / 1023u);
                }
                break;
            case DXGI_FORMAT_R16G16B16A16_FLOAT: {
                const std::vector<uint8_t>& table = half_to_srgb8();
                for (uint32_t x = 0; x < width; ++x, s += 8, d += 3) {
                    uint16_t rgb[3];
                    std::memcpy(rgb, s, 6);
                    d[0] = table[rgb[2]];
                    d[1] = table[rgb[1]];
                    d[2] = table[rgb[0]];
                }
                break;
            }
            default:
                return;
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Latest converted frame, shared by the readback worker and the TCP server

#pragma pack(push, 1)
struct FrameHeader {
    char magic[4] = {'H', 'G', 'T', 'A'};
    uint32_t header_bytes = sizeof(FrameHeader);
    uint64_t present_index = 0;
    uint64_t capture_unix_ns = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t pixel_format = 1;  // BGR8
    uint32_t payload_bytes = 0;
    // HorusGta's camera block at this frame's Present (horus::CameraStamp).
    uint32_t camera_active = 0;
    uint32_t script_age_presents = horus::kScriptAgeNever;
    uint64_t pose_seq = 0;
    uint64_t game_frame = 0;
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 64, "wire header is 64 bytes");

struct LatestFrame {
    std::mutex mutex;
    std::condition_variable updated;
    FrameHeader header;
    std::vector<uint8_t> pixels;
    uint64_t sequence = 0;
};
LatestFrame g_latest;
std::atomic<uint64_t> g_present_index{0};
std::atomic<bool> g_client_connected{false};  // no client: Present does no GPU copy at all

// Render thread only: HorusGta's camera block and what the current Present stamps.
volatile uint8_t* g_camera_block = nullptr;
horus::CameraTracker g_camera_tracker;
horus::CameraStamp g_camera_now;

// ---------------------------------------------------------------------------------------------
// GPU readback ring

using ExecuteCommandListsFn = void(STDMETHODCALLTYPE*)(ID3D12CommandQueue*, UINT, ID3D12CommandList* const*);
ExecuteCommandListsFn g_execute_command_lists = nullptr;  // original (unhooked) entry point

template <typename T>
void release(T*& object) {
    if (object) {
        object->Release();
        object = nullptr;
    }
}

class Readback {
public:
    // Detached: a joinable std::thread destroyed at process exit would call std::terminate.
    void start() { std::thread([this] { run(); }).detach(); }

    // Render thread, inside Present: queue a copy of the current back buffer.
    void capture(IDXGISwapChain3* swapchain, ID3D12CommandQueue* queue) {
        ID3D12Resource* back_buffer = nullptr;
        if (FAILED(swapchain->GetBuffer(swapchain->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&back_buffer)))) {
            return;
        }
        ID3D12Device* device = nullptr;
        if (FAILED(back_buffer->GetDevice(IID_PPV_ARGS(&device)))) {
            back_buffer->Release();
            return;
        }
        const D3D12_RESOURCE_DESC desc = back_buffer->GetDesc();
        if (!supported_format(desc.Format) || desc.SampleDesc.Count != 1) {
            if (!warned_format_) {
                log("unsupported back buffer: format %d, %u samples", static_cast<int>(desc.Format),
                    desc.SampleDesc.Count);
                warned_format_ = true;
            }
        } else if (ensure_resources(device, desc)) {
            submit(queue, back_buffer, desc);
        }
        device->Release();
        back_buffer->Release();
    }

    // Wait until no queued copy references a back buffer (before ResizeBuffers).
    void wait_idle() {
        std::unique_lock<std::mutex> lock(mutex_);
        idle_.wait(lock, [this] { return pending_.empty() && busy_count() == 0; });
    }

private:
    struct Slot {
        ID3D12CommandAllocator* allocator = nullptr;
        ID3D12GraphicsCommandList* list = nullptr;
        ID3D12Resource* buffer = nullptr;
        uint64_t fence_value = 0;
        bool busy = false;
        uint64_t present_index = 0;
        uint64_t capture_ns = 0;
        horus::CameraStamp camera;
    };

    int busy_count() const {
        int count = 0;
        for (const Slot& slot : slots_) count += slot.busy ? 1 : 0;
        return count;
    }

    bool ensure_resources(ID3D12Device* device, const D3D12_RESOURCE_DESC& desc) {
        if (device == device_ && desc.Width == width_ && desc.Height == height_ && desc.Format == format_) {
            return ready_;
        }
        wait_idle();
        destroy();
        ready_ = false;
        device_ = device;
        device_->AddRef();
        width_ = desc.Width;
        height_ = static_cast<UINT>(desc.Height);
        format_ = desc.Format;

        UINT rows = 0;
        UINT64 row_bytes = 0;
        UINT64 total = 0;
        device->GetCopyableFootprints(&desc, 0, 1, 0, &footprint_, &rows, &row_bytes, &total);

        D3D12_HEAP_PROPERTIES heap{};
        heap.Type = D3D12_HEAP_TYPE_READBACK;
        D3D12_RESOURCE_DESC buffer_desc{};
        buffer_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        buffer_desc.Width = total;
        buffer_desc.Height = 1;
        buffer_desc.DepthOrArraySize = 1;
        buffer_desc.MipLevels = 1;
        buffer_desc.SampleDesc.Count = 1;
        buffer_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence_)))) {
            log("CreateFence failed");
            return false;
        }
        for (Slot& slot : slots_) {
            if (FAILED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&slot.allocator))) ||
                FAILED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, slot.allocator, nullptr,
                                                 IID_PPV_ARGS(&slot.list))) ||
                FAILED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer_desc,
                                                       D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                                       IID_PPV_ARGS(&slot.buffer)))) {
                log("creating readback slot failed");
                return false;
            }
            slot.list->Close();
        }
        log("capturing %llux%u format %d at up to %.1f Hz", static_cast<unsigned long long>(width_), height_,
            static_cast<int>(format_), g_config.max_hz);
        ready_ = true;
        return true;
    }

    void destroy() {
        for (Slot& slot : slots_) {
            release(slot.list);
            release(slot.allocator);
            release(slot.buffer);
            slot = Slot{};
        }
        release(fence_);
        release(device_);
        fence_counter_ = 0;
    }

    void submit(ID3D12CommandQueue* queue, ID3D12Resource* back_buffer, const D3D12_RESOURCE_DESC&) {
        int index = -1;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            for (int i = 0; i < static_cast<int>(slots_.size()); ++i) {
                if (!slots_[i].busy) {
                    index = i;
                    break;
                }
            }
        }
        if (index < 0) return;  // GPU or converter behind: drop this frame
        Slot& slot = slots_[index];
        if (FAILED(slot.allocator->Reset()) || FAILED(slot.list->Reset(slot.allocator, nullptr))) return;

        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = back_buffer;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
        slot.list->ResourceBarrier(1, &barrier);

        D3D12_TEXTURE_COPY_LOCATION dst{};
        dst.pResource = slot.buffer;
        dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        dst.PlacedFootprint = footprint_;
        D3D12_TEXTURE_COPY_LOCATION src{};
        src.pResource = back_buffer;
        src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        src.SubresourceIndex = 0;
        slot.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);

        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        slot.list->ResourceBarrier(1, &barrier);
        if (FAILED(slot.list->Close())) return;

        ID3D12CommandList* lists[] = {slot.list};
        g_execute_command_lists(queue, 1, lists);
        const uint64_t value = ++fence_counter_;
        if (FAILED(queue->Signal(fence_, value))) return;

        std::lock_guard<std::mutex> lock(mutex_);
        slot.fence_value = value;
        slot.present_index = g_present_index.load(std::memory_order_relaxed);
        slot.capture_ns = unix_time_ns();
        slot.camera = g_camera_now;
        slot.busy = true;
        pending_.push_back(index);
        work_.notify_one();
    }

    // Worker thread: wait for each copy, convert it, publish it as the latest frame.
    void run() {
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        std::vector<uint8_t> pixels;
        for (;;) {
            int index;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                work_.wait(lock, [this] { return !pending_.empty(); });
                index = pending_.front();
            }
            Slot& slot = slots_[index];
            if (fence_->GetCompletedValue() < slot.fence_value) {
                fence_->SetEventOnCompletion(slot.fence_value, event);
                while (WaitForSingleObject(event, 1000) == WAIT_TIMEOUT &&
                       fence_->GetCompletedValue() < slot.fence_value) {
                }
            }
            const uint32_t width = static_cast<uint32_t>(width_);
            const uint32_t height = height_;
            pixels.resize(static_cast<size_t>(width) * height * 3);
            void* mapped = nullptr;
            D3D12_RANGE read_range{0, static_cast<SIZE_T>(footprint_.Footprint.RowPitch) * height};
            if (SUCCEEDED(slot.buffer->Map(0, &read_range, &mapped))) {
                convert_to_bgr(format_, static_cast<const uint8_t*>(mapped) + footprint_.Offset,
                               footprint_.Footprint.RowPitch, width, height, pixels.data());
                D3D12_RANGE written{0, 0};
                slot.buffer->Unmap(0, &written);
                std::lock_guard<std::mutex> lock(g_latest.mutex);
                g_latest.pixels.swap(pixels);
                g_latest.header.present_index = slot.present_index;
                g_latest.header.capture_unix_ns = slot.capture_ns;
                g_latest.header.width = width;
                g_latest.header.height = height;
                g_latest.header.payload_bytes = width * height * 3;
                g_latest.header.camera_active = slot.camera.active;
                g_latest.header.script_age_presents = slot.camera.script_age;
                g_latest.header.pose_seq = slot.camera.pose_seq;
                g_latest.header.game_frame = slot.camera.game_frame;
                ++g_latest.sequence;
                g_latest.updated.notify_all();
            }
            std::lock_guard<std::mutex> lock(mutex_);
            pending_.pop_front();
            slot.busy = false;
            idle_.notify_all();
        }
    }

    std::mutex mutex_;
    std::condition_variable work_;
    std::condition_variable idle_;
    std::deque<int> pending_;
    std::array<Slot, 3> slots_{};
    ID3D12Device* device_ = nullptr;
    ID3D12Fence* fence_ = nullptr;
    uint64_t fence_counter_ = 0;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint_{};
    UINT64 width_ = 0;
    UINT height_ = 0;
    DXGI_FORMAT format_ = DXGI_FORMAT_UNKNOWN;
    bool ready_ = false;
    bool warned_format_ = false;
};

Readback g_readback;

// ---------------------------------------------------------------------------------------------
// Which queue presents which swap chain

horus::SwapchainQueues g_queues;  // gta/common/swapchain_queues.hpp

void remember_swapchain_queue(IUnknown* swapchain, IUnknown* device_or_queue) {
    if (ID3D12CommandQueue* queue = g_queues.remember(swapchain, device_or_queue)) {
        log("swap chain %p presents on queue %p", static_cast<void*>(swapchain), static_cast<void*>(queue));
    }
}

ID3D12CommandQueue* queue_for(IDXGISwapChain3* swapchain, IUnknown* identity) {
    bool fallback = false;
    ID3D12CommandQueue* queue = g_queues.find(swapchain, identity, &fallback);
    static bool logged_fallback = false;
    if (queue && fallback && !logged_fallback) {
        log("swap chain created before the hooks; using direct queue %p from ExecuteCommandLists",
            static_cast<void*>(queue));
        logged_fallback = true;
    }
    return queue;
}

// ---------------------------------------------------------------------------------------------
// Hooks

using PresentFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT);
using Present1Fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain1*, UINT, UINT, const DXGI_PRESENT_PARAMETERS*);
using ResizeBuffersFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);
using ResizeBuffers1Fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain3*, UINT, UINT, UINT, DXGI_FORMAT, UINT,
                                                     const UINT*, IUnknown* const*);
using CreateSwapChainFn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory*, IUnknown*, DXGI_SWAP_CHAIN_DESC*,
                                                      IDXGISwapChain**);
using CreateSwapChainForHwndFn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory2*, IUnknown*, HWND,
                                                             const DXGI_SWAP_CHAIN_DESC1*,
                                                             const DXGI_SWAP_CHAIN_FULLSCREEN_DESC*, IDXGIOutput*,
                                                             IDXGISwapChain1**);

PresentFn g_present = nullptr;
Present1Fn g_present1 = nullptr;
ResizeBuffersFn g_resize_buffers = nullptr;
ResizeBuffers1Fn g_resize_buffers1 = nullptr;
CreateSwapChainFn g_create_swapchain = nullptr;
CreateSwapChainForHwndFn g_create_swapchain_for_hwnd = nullptr;

thread_local bool t_in_present = false;  // Present may forward to Present1 internally
double g_next_capture_at = 0.0;

void on_present(IDXGISwapChain* swapchain, UINT flags) {
    if (t_in_present || (flags & DXGI_PRESENT_TEST)) return;
    const uint64_t present_index = g_present_index.fetch_add(1, std::memory_order_relaxed) + 1;
    if (g_camera_block) {
        horus::CameraFields fields;
        const bool read = horus::read_camera_block(g_camera_block, fields);
        g_camera_now = g_camera_tracker.observe(present_index, read ? &fields : nullptr);
    }
    if (!g_client_connected.load(std::memory_order_relaxed)) return;
    const double now = seconds_now();
    if (now < g_next_capture_at) return;
    const double period = 1.0 / g_config.max_hz;
    g_next_capture_at = (now - g_next_capture_at < period) ? g_next_capture_at + period : now + period;

    IDXGISwapChain3* swapchain3 = nullptr;
    if (FAILED(swapchain->QueryInterface(IID_PPV_ARGS(&swapchain3)))) return;
    IUnknown* identity = nullptr;
    swapchain->QueryInterface(IID_PPV_ARGS(&identity));
    if (ID3D12CommandQueue* queue = queue_for(swapchain3, identity)) {
        g_readback.capture(swapchain3, queue);
    } else {
        static bool warned = false;
        if (!warned) {
            log("no D3D12 direct queue known for swap chain %p yet", static_cast<void*>(identity));
            warned = true;
        }
    }
    release(identity);
    swapchain3->Release();
}

HRESULT STDMETHODCALLTYPE hook_present(IDXGISwapChain* swapchain, UINT sync, UINT flags) {
    on_present(swapchain, flags);
    const bool outer = !t_in_present;
    t_in_present = true;
    const HRESULT hr = g_present(swapchain, sync, flags);
    if (outer) t_in_present = false;
    return hr;
}

HRESULT STDMETHODCALLTYPE hook_present1(IDXGISwapChain1* swapchain, UINT sync, UINT flags,
                                        const DXGI_PRESENT_PARAMETERS* params) {
    on_present(swapchain, flags);
    const bool outer = !t_in_present;
    t_in_present = true;
    const HRESULT hr = g_present1(swapchain, sync, flags, params);
    if (outer) t_in_present = false;
    return hr;
}

HRESULT STDMETHODCALLTYPE hook_resize_buffers(IDXGISwapChain* swapchain, UINT count, UINT width, UINT height,
                                              DXGI_FORMAT format, UINT flags) {
    g_readback.wait_idle();
    return g_resize_buffers(swapchain, count, width, height, format, flags);
}

HRESULT STDMETHODCALLTYPE hook_resize_buffers1(IDXGISwapChain3* swapchain, UINT count, UINT width, UINT height,
                                               DXGI_FORMAT format, UINT flags, const UINT* node_masks,
                                               IUnknown* const* queues) {
    g_readback.wait_idle();
    return g_resize_buffers1(swapchain, count, width, height, format, flags, node_masks, queues);
}

HRESULT STDMETHODCALLTYPE hook_create_swapchain(IDXGIFactory* factory, IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc,
                                                IDXGISwapChain** swapchain) {
    const HRESULT hr = g_create_swapchain(factory, device, desc, swapchain);
    if (SUCCEEDED(hr) && swapchain && *swapchain && device) {
        IUnknown* identity = nullptr;
        if (SUCCEEDED((*swapchain)->QueryInterface(IID_PPV_ARGS(&identity)))) {
            remember_swapchain_queue(identity, device);
            identity->Release();
        }
    }
    return hr;
}

HRESULT STDMETHODCALLTYPE hook_create_swapchain_for_hwnd(IDXGIFactory2* factory, IUnknown* device, HWND hwnd,
                                                         const DXGI_SWAP_CHAIN_DESC1* desc,
                                                         const DXGI_SWAP_CHAIN_FULLSCREEN_DESC* fullscreen,
                                                         IDXGIOutput* output, IDXGISwapChain1** swapchain) {
    const HRESULT hr = g_create_swapchain_for_hwnd(factory, device, hwnd, desc, fullscreen, output, swapchain);
    if (SUCCEEDED(hr) && swapchain && *swapchain && device) {
        IUnknown* identity = nullptr;
        if (SUCCEEDED((*swapchain)->QueryInterface(IID_PPV_ARGS(&identity)))) {
            remember_swapchain_queue(identity, device);
            identity->Release();
        }
    }
    return hr;
}

void STDMETHODCALLTYPE hook_execute_command_lists(ID3D12CommandQueue* queue, UINT count,
                                                  ID3D12CommandList* const* lists) {
    g_queues.note_execute(queue);
    g_execute_command_lists(queue, count, lists);
}

bool hook(void* target, void* detour, void** original, const char* name) {
    const MH_STATUS created = MH_CreateHook(target, detour, original);
    if (created != MH_OK) {
        log("hooking %s failed: %s", name, MH_StatusToString(created));
        return false;
    }
    return true;
}

// Find the runtime's DXGI/D3D12 method addresses and hook them.
bool install_hooks() {
    horus::DxgiVtables vt;
    if (const char* error = horus::probe_dxgi_vtables(vt, L"horus_capture_probe")) {
        log("%s failed", error);
        return false;
    }
    // Present and ExecuteCommandLists are required; the rest improve queue lookup and resize safety.
    const bool ok = hook(vt.present, reinterpret_cast<void*>(&hook_present), reinterpret_cast<void**>(&g_present),
                         "IDXGISwapChain::Present") &&
                    hook(vt.execute_command_lists, reinterpret_cast<void*>(&hook_execute_command_lists),
                         reinterpret_cast<void**>(&g_execute_command_lists), "ID3D12CommandQueue::ExecuteCommandLists");
    if (!ok) return false;
    hook(vt.present1, reinterpret_cast<void*>(&hook_present1), reinterpret_cast<void**>(&g_present1),
         "IDXGISwapChain1::Present1");
    hook(vt.resize_buffers, reinterpret_cast<void*>(&hook_resize_buffers), reinterpret_cast<void**>(&g_resize_buffers),
         "IDXGISwapChain::ResizeBuffers");
    hook(vt.resize_buffers1, reinterpret_cast<void*>(&hook_resize_buffers1),
         reinterpret_cast<void**>(&g_resize_buffers1), "IDXGISwapChain3::ResizeBuffers1");
    hook(vt.create_swapchain, reinterpret_cast<void*>(&hook_create_swapchain),
         reinterpret_cast<void**>(&g_create_swapchain), "IDXGIFactory::CreateSwapChain");
    hook(vt.create_swapchain_for_hwnd, reinterpret_cast<void*>(&hook_create_swapchain_for_hwnd),
         reinterpret_cast<void**>(&g_create_swapchain_for_hwnd), "IDXGIFactory2::CreateSwapChainForHwnd");
    const MH_STATUS enabled = MH_EnableHook(MH_ALL_HOOKS);
    if (enabled != MH_OK) {
        log("enabling hooks failed: %s", MH_StatusToString(enabled));
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------------------------
// TCP server: one client at a time; each request byte is answered with a frame newer than the last.

bool send_all(SOCKET socket, const char* data, size_t size) {
    while (size > 0) {
        const int chunk = static_cast<int>(size < (1u << 30) ? size : (1u << 30));
        const int sent = send(socket, data, chunk, 0);
        if (sent <= 0) return false;
        data += sent;
        size -= static_cast<size_t>(sent);
    }
    return true;
}

void serve_client(SOCKET client) {
    // Frames captured before this client connected (or for a previous client) are never sent.
    uint64_t last_sent;
    {
        std::lock_guard<std::mutex> lock(g_latest.mutex);
        last_sent = g_latest.sequence;
    }
    std::vector<uint8_t> pixels;
    FrameHeader header;
    for (;;) {
        char request = 0;
        if (recv(client, &request, 1, 0) != 1) return;
        {
            std::unique_lock<std::mutex> lock(g_latest.mutex);
            g_latest.updated.wait(lock, [&] { return g_latest.sequence > last_sent; });
            last_sent = g_latest.sequence;
            header = g_latest.header;
            pixels.assign(g_latest.pixels.begin(), g_latest.pixels.begin() + header.payload_bytes);
        }
        if (!send_all(client, reinterpret_cast<const char*>(&header), sizeof(header)) ||
            !send_all(client, reinterpret_cast<const char*>(pixels.data()), pixels.size())) {
            return;
        }
    }
}

void run_server() {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        log("WSAStartup failed");
        return;
    }
    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(g_config.port));
    if (listener == INVALID_SOCKET || inet_pton(AF_INET, g_config.bind.c_str(), &address.sin_addr) != 1) {
        log("invalid bind address %s", g_config.bind.c_str());
        return;
    }
    const BOOL reuse = TRUE;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&reuse), sizeof(reuse));
    if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 || listen(listener, 1) != 0) {
        log("cannot listen on %s:%d (WSA error %d)", g_config.bind.c_str(), g_config.port, WSAGetLastError());
        closesocket(listener);
        return;
    }
    log("serving frames on %s:%d", g_config.bind.c_str(), g_config.port);
    for (;;) {
        SOCKET client = accept(listener, nullptr, nullptr);
        if (client == INVALID_SOCKET) continue;
        const BOOL no_delay = TRUE;
        setsockopt(client, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&no_delay), sizeof(no_delay));
        log("client connected");
        g_client_connected.store(true, std::memory_order_relaxed);
        serve_client(client);
        g_client_connected.store(false, std::memory_order_relaxed);
        closesocket(client);
        log("client disconnected");
    }
}

// ---------------------------------------------------------------------------------------------

std::atomic<int> g_ready{0};  // 1 = hooks installed, -1 = failed

DWORD WINAPI initialise(LPVOID) {
    load_config();
    log("horus_capture starting (bind %s:%d, max %.1f Hz)", g_config.bind.c_str(), g_config.port,
        g_config.max_hz);
    // Before the hooks: on_present reads g_camera_block without synchronisation.
    g_camera_block = horus::open_camera_block();
    if (g_camera_block) {
        log("camera block Local\\HorusGtaCamera mapped");
    } else {
        log("camera block Local\\HorusGtaCamera not mapped (error %lu); every frame reports camera_active 0",
            GetLastError());
    }
    if (MH_Initialize() != MH_OK || !install_hooks()) {
        log("disabled: hooks not installed");
        g_ready.store(-1);
        return 0;
    }
    g_readback.start();
    std::thread(run_server).detach();
    log("hooks installed");
    g_ready.store(1);
    return 0;
}

}  // namespace

extern "C" __declspec(dllexport) int horus_capture_ready() { return g_ready.load(); }

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        wchar_t path[MAX_PATH];
        const DWORD length = GetModuleFileNameW(instance, path, MAX_PATH);
        g_module_dir.assign(path, length);
        g_module_dir.resize(g_module_dir.find_last_of(L"\\/") == std::wstring::npos
                                ? 0
                                : g_module_dir.find_last_of(L"\\/"));
        g_log = _wfopen((g_module_dir + L"\\horus_capture.log").c_str(), L"w");
        // D3D/DXGI must not be initialised under the loader lock.
        if (HANDLE thread = CreateThread(nullptr, 0, initialise, nullptr, 0, nullptr)) CloseHandle(thread);
    }
    return TRUE;
}

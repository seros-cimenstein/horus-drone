// Stand-in for GTA V when checking horus_capture.asi: a D3D12 window that clears its back buffer to
// a known colour palette (see check_stream.py) and presents, with the plugin loaded like an ASI.
//
//   horus_capture_selftest.exe <bgra8|rgba8|rgb10a2|rgba16f> <seconds> [--late-load] [--camera]
//
// --late-load creates the swap chain before loading the plugin, exercising its fallback that finds
// the presenting queue through ExecuteCommandLists instead of the CreateSwapChain hook.
//
// --camera also stands in for HorusGta's script: before each Present of an even palette colour it
// writes the camera block (active 1, pose_seq = game_frame = frame number); during odd colours it
// writes nothing, like a paused game whose block still says active 1 (check_stream.py --camera).

#define WIDL_EXPLICIT_AGGREGATE_RETURNS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>

#include "camera_block.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>

namespace {

constexpr UINT kWidth = 640;
constexpr UINT kHeight = 360;
constexpr UINT kBuffers = 2;
constexpr int kFramesPerColour = 10;
// sRGB bytes; must match PALETTE in check_stream.py.
constexpr unsigned char kPalette[][3] = {
    {230, 30, 20}, {20, 200, 40}, {40, 60, 220}, {240, 220, 30}, {200, 40, 210}, {30, 210, 220},
};

int fail(const char* what) {
    std::printf("SELFTEST FAILED: %s\n", what);
    std::fflush(stdout);
    return 1;
}

bool load_plugin() {
    wchar_t path[MAX_PATH];
    const DWORD length = GetModuleFileNameW(nullptr, path, MAX_PATH);
    wchar_t* slash = std::wcsrchr(path, L'\\');
    if (length == 0 || !slash) return false;
    std::wcscpy(slash + 1, L"horus_capture.asi");
    HMODULE plugin = LoadLibraryW(path);
    if (!plugin) return false;
    auto ready = reinterpret_cast<int (*)()>(reinterpret_cast<void*>(GetProcAddress(plugin, "horus_capture_ready")));
    if (!ready) return false;
    for (int i = 0; i < 600; ++i) {  // up to 30 s: the probe device is slow while the GPU is busy
        const int state = ready();
        if (state != 0) return state == 1;
        Sleep(50);
    }
    return false;
}

float srgb_to_linear(float s) { return s <= 0.04045f ? s / 12.92f : std::pow((s + 0.055f) / 1.055f, 2.4f); }

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        return fail("usage: horus_capture_selftest <bgra8|rgba8|rgb10a2|rgba16f> <seconds> [--late-load] [--camera]");
    }
    DXGI_FORMAT format;
    bool linear = false;
    if (!std::strcmp(argv[1], "bgra8")) {
        format = DXGI_FORMAT_B8G8R8A8_UNORM;
    } else if (!std::strcmp(argv[1], "rgba8")) {
        format = DXGI_FORMAT_R8G8B8A8_UNORM;
    } else if (!std::strcmp(argv[1], "rgb10a2")) {
        format = DXGI_FORMAT_R10G10B10A2_UNORM;
    } else if (!std::strcmp(argv[1], "rgba16f")) {
        format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        linear = true;
    } else {
        return fail("unknown format");
    }
    const double seconds = std::atof(argv[2]);
    bool late_load = false;
    bool camera = false;
    for (int i = 3; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--late-load")) {
            late_load = true;
        } else if (!std::strcmp(argv[i], "--camera")) {
            camera = true;
        } else {
            return fail("unknown option");
        }
    }
    volatile uint8_t* camera_block = camera ? horus::open_camera_block() : nullptr;
    if (camera && !camera_block) return fail("camera block not mapped");
    if (!late_load && !load_plugin()) return fail("plugin did not install its hooks");

    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.lpfnWndProc = DefWindowProcW;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpszClassName = L"horus_capture_selftest";
    RegisterClassExW(&window_class);
    RECT rect{0, 0, static_cast<LONG>(kWidth), static_cast<LONG>(kHeight)};
    AdjustWindowRect(&rect, WS_OVERLAPPEDWINDOW, FALSE);
    HWND window = CreateWindowExW(0, window_class.lpszClassName, L"horus_capture selftest", WS_OVERLAPPEDWINDOW,
                                  CW_USEDEFAULT, CW_USEDEFAULT, rect.right - rect.left, rect.bottom - rect.top,
                                  nullptr, nullptr, window_class.hInstance, nullptr);
    if (!window) return fail("CreateWindow");
    ShowWindow(window, SW_SHOW);

    IDXGIFactory2* factory = nullptr;
    ID3D12Device* device = nullptr;
    ID3D12CommandQueue* queue = nullptr;
    IDXGISwapChain1* swapchain1 = nullptr;
    IDXGISwapChain3* swapchain = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return fail("CreateDXGIFactory1");
    if (FAILED(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) return fail("D3D12CreateDevice");
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue)))) return fail("CreateCommandQueue");
    DXGI_SWAP_CHAIN_DESC1 desc{};
    desc.Width = kWidth;
    desc.Height = kHeight;
    desc.Format = format;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = kBuffers;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    if (FAILED(factory->CreateSwapChainForHwnd(queue, window, &desc, nullptr, nullptr, &swapchain1)))
        return fail("CreateSwapChainForHwnd");
    if (FAILED(swapchain1->QueryInterface(IID_PPV_ARGS(&swapchain)))) return fail("IDXGISwapChain3");

    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{};
    heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heap_desc.NumDescriptors = kBuffers;
    ID3D12DescriptorHeap* heap = nullptr;
    if (FAILED(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)))) return fail("CreateDescriptorHeap");
    const UINT rtv_size = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    ID3D12Resource* buffers[kBuffers] = {};
    for (UINT i = 0; i < kBuffers; ++i) {
        if (FAILED(swapchain->GetBuffer(i, IID_PPV_ARGS(&buffers[i])))) return fail("GetBuffer");
        D3D12_CPU_DESCRIPTOR_HANDLE handle = heap->GetCPUDescriptorHandleForHeapStart();
        handle.ptr += static_cast<SIZE_T>(i) * rtv_size;
        device->CreateRenderTargetView(buffers[i], nullptr, handle);
    }
    ID3D12CommandAllocator* allocator = nullptr;
    ID3D12GraphicsCommandList* list = nullptr;
    ID3D12Fence* fence = nullptr;
    if (FAILED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))) ||
        FAILED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, nullptr, IID_PPV_ARGS(&list))) ||
        FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence))))
        return fail("command objects");
    list->Close();
    HANDLE fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    UINT64 fence_value = 0;

    std::printf("SELFTEST RUNNING %s %ux%u%s%s\n", argv[1], kWidth, kHeight, late_load ? " late-load" : "",
                camera ? " camera" : "");
    std::fflush(stdout);

    const ULONGLONG end = GetTickCount64() + static_cast<ULONGLONG>(seconds * 1000.0);
    for (int frame = 0; GetTickCount64() < end; ++frame) {
        MSG message;
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if (late_load && frame == 30 && !load_plugin()) return fail("late-loaded plugin did not install its hooks");

        const int colour_index = (frame / kFramesPerColour) % static_cast<int>(sizeof(kPalette) / sizeof(kPalette[0]));
        const unsigned char* colour = kPalette[colour_index];
        if (camera_block && colour_index % 2 == 0) {
            horus::write_camera_block(camera_block, 1, static_cast<uint64_t>(frame), static_cast<uint64_t>(frame));
        }
        float clear[4] = {colour[0] / 255.0f, colour[1] / 255.0f, colour[2] / 255.0f, 1.0f};
        if (linear) {
            for (int c = 0; c < 3; ++c) clear[c] = srgb_to_linear(clear[c]);
        }
        const UINT index = swapchain->GetCurrentBackBufferIndex();
        D3D12_CPU_DESCRIPTOR_HANDLE rtv = heap->GetCPUDescriptorHandleForHeapStart();
        rtv.ptr += static_cast<SIZE_T>(index) * rtv_size;

        allocator->Reset();
        list->Reset(allocator, nullptr);
        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = buffers[index];
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        list->ResourceBarrier(1, &barrier);
        list->ClearRenderTargetView(rtv, clear, 0, nullptr);
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        list->ResourceBarrier(1, &barrier);
        list->Close();
        ID3D12CommandList* lists[] = {list};
        queue->ExecuteCommandLists(1, lists);
        if (FAILED(swapchain->Present(1, 0))) return fail("Present");
        queue->Signal(fence, ++fence_value);
        if (fence->GetCompletedValue() < fence_value) {
            fence->SetEventOnCompletion(fence_value, fence_event);
            WaitForSingleObject(fence_event, INFINITE);
        }
    }
    std::printf("SELFTEST DONE\n");
    std::fflush(stdout);
    // Exit without tearing down: the plugin's threads live for the process, as they do in GTA.
    ExitProcess(0);
}

// Shared by horus_capture.asi and the HORUS Script Hook: find the D3D12/DXGI runtime's method
// addresses by creating a throwaway device, queue and swap chain on a hidden window. Hooking these
// addresses catches the game's own objects, which share the runtime's vtables.
#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>

namespace horus {

struct DxgiVtables {
    void* present = nullptr;                    // IDXGISwapChain::Present
    void* resize_buffers = nullptr;             // IDXGISwapChain::ResizeBuffers
    void* present1 = nullptr;                   // IDXGISwapChain1::Present1
    void* resize_buffers1 = nullptr;            // IDXGISwapChain3::ResizeBuffers1
    void* create_swapchain = nullptr;           // IDXGIFactory::CreateSwapChain
    void* create_swapchain_for_hwnd = nullptr;  // IDXGIFactory2::CreateSwapChainForHwnd
    void* execute_command_lists = nullptr;      // ID3D12CommandQueue::ExecuteCommandLists
};

inline void* vtable_entry(void* object, int index) { return (*static_cast<void***>(object))[index]; }

// Returns nullptr on success, otherwise the step that failed. Must not run under the loader lock.
inline const char* probe_dxgi_vtables(DxgiVtables& out, const wchar_t* window_class_name) {
    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.lpfnWndProc = DefWindowProcW;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpszClassName = window_class_name;
    RegisterClassExW(&window_class);
    HWND window = CreateWindowExW(0, window_class_name, L"", WS_OVERLAPPEDWINDOW, 0, 0, 64, 64, nullptr, nullptr,
                                  window_class.hInstance, nullptr);

    IDXGIFactory2* factory = nullptr;
    ID3D12Device* device = nullptr;
    ID3D12CommandQueue* queue = nullptr;
    IDXGISwapChain1* swapchain = nullptr;
    const char* error = nullptr;
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    DXGI_SWAP_CHAIN_DESC1 desc{};
    desc.Width = 64;
    desc.Height = 64;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    if (!window) {
        error = "probe window creation";
    } else if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) {
        error = "CreateDXGIFactory1";
    } else if (FAILED(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) {
        error = "D3D12CreateDevice";
    } else if (FAILED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue)))) {
        error = "CreateCommandQueue";
    } else if (FAILED(factory->CreateSwapChainForHwnd(queue, window, &desc, nullptr, nullptr, &swapchain))) {
        error = "probe CreateSwapChainForHwnd";
    } else {
        out.present = vtable_entry(swapchain, 8);
        out.resize_buffers = vtable_entry(swapchain, 13);
        out.present1 = vtable_entry(swapchain, 22);
        out.resize_buffers1 = vtable_entry(swapchain, 39);
        out.create_swapchain = vtable_entry(factory, 10);
        out.create_swapchain_for_hwnd = vtable_entry(factory, 15);
        out.execute_command_lists = vtable_entry(queue, 10);
    }
    if (swapchain) swapchain->Release();
    if (queue) queue->Release();
    if (device) device->Release();
    if (factory) factory->Release();
    if (window) DestroyWindow(window);
    UnregisterClassW(window_class_name, window_class.hInstance);
    return error;
}

}  // namespace horus

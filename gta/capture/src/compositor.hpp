// horus_capture's shared-view compositor (gta/capture/README.md "Shared view"): keeps HorusGta's
// drone frames off the screen. Per Present, on the presenting queue and after the capture's
// readback copy:
//
//   back buffer -> cur (copy)
//   fullscreen triangle into the back buffer: saved.Load(p) if cur's marker texel says drone,
//     else cur.Load(p)                                          (composite.hlsl)
//   back buffer -> saved (copy)
//
// so a drone frame is replaced by the last frame shown and a player frame passes unchanged. The
// first composite after start() seeds `saved` from `cur`, so nothing undefined reaches the
// screen. The decision is made on the GPU because the CPU cannot know which camera a Presented
// frame used; D3D12 predicated copies would do it without a shader, but vkd3d-proton does not
// honour predication for copies.
//
// Shared with gta/capture/tests/capture_tests.cpp, which runs it on an off-screen target.
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <d3d12.h>

#include <cstdint>
#include <cstdio>
#include <string>

#include "composite_shaders.hpp"
#include "shared_view.hpp"

namespace horus::capture {

using ExecuteCommandListsFn = void(STDMETHODCALLTYPE*)(ID3D12CommandQueue*, UINT, ID3D12CommandList* const*);

// The typed view format the compositor uses for a back buffer of `format`, or DXGI_FORMAT_UNKNOWN if
// it does not composite that format. sRGB back buffers are left alone: an sRGB render target view
// would re-encode the raw values the shader copies (flip-model swap chains never have one anyway).
inline DXGI_FORMAT composite_view_format(DXGI_FORMAT format) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS:
        case DXGI_FORMAT_R8G8B8A8_UNORM:
            return DXGI_FORMAT_R8G8B8A8_UNORM;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
            return DXGI_FORMAT_B8G8R8A8_UNORM;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS:
        case DXGI_FORMAT_R10G10B10A2_UNORM:
            return DXGI_FORMAT_R10G10B10A2_UNORM;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS:
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
            return DXGI_FORMAT_R16G16B16A16_FLOAT;
        default:
            return DXGI_FORMAT_UNKNOWN;
    }
}

class Compositor {
public:
    static constexpr int kFrames = 8;  // command allocators in flight

    enum class Result {
        kRecorded,     // submitted on the queue
        kBusy,         // every allocator still in flight on the GPU: skipped, the frame shows as is
        kUnavailable,  // unsupported back buffer or set-up failed: skipped
    };

    // `execute` submits on a queue; the capture plugin passes the unhooked ExecuteCommandLists.
    explicit Compositor(ExecuteCommandListsFn execute) : execute_(execute) {}
    ~Compositor() { reset(); }
    Compositor(const Compositor&) = delete;
    Compositor& operator=(const Compositor&) = delete;

    // The next composite seeds `saved` from its own frame (shared view (re)started).
    void start() { seeded_ = false; }

    // Render thread, inside Present: `back_buffer` is in PRESENT (= COMMON) state before and after,
    // `queue` presents it. Never waits for the GPU except when the back buffer's size or format
    // changed. `message` gets a line worth logging (set-up result), else stays empty.
    Result composite(ID3D12CommandQueue* queue, ID3D12Resource* back_buffer, std::string& message) {
        const D3D12_RESOURCE_DESC desc = back_buffer->GetDesc();
        ID3D12Device* device = nullptr;
        if (FAILED(back_buffer->GetDevice(IID_PPV_ARGS(&device)))) return Result::kUnavailable;
        const bool same = device == device_ && desc.Width == width_ && desc.Height == height_ &&
                          desc.Format == format_;
        if (!same) {
            setup(device, desc, message);
            seeded_ = false;
        }
        device->Release();
        if (!ready_) return Result::kUnavailable;
        return record(queue, back_buffer);
    }

    // Waits (up to 5 s) until everything submitted has finished on the GPU.
    bool wait_idle() { return wait_for(fence_counter_); }

private:
    struct Frame {
        ID3D12CommandAllocator* allocator = nullptr;
        uint64_t fence_value = 0;
    };

    template <typename T>
    static void release(T*& object) {
        if (object) object->Release();
        object = nullptr;
    }

    static std::string hresult_text(const char* what, HRESULT hr) {
        char text[160];
        std::snprintf(text, sizeof(text), "%s failed (hr 0x%08lx)", what, static_cast<unsigned long>(hr));
        return text;
    }

    bool wait_for(uint64_t value) {
        if (!fence_ || fence_->GetCompletedValue() >= value) return true;
        if (FAILED(fence_->SetEventOnCompletion(value, event_))) return false;
        return WaitForSingleObject(event_, 5000) == WAIT_OBJECT_0 || fence_->GetCompletedValue() >= value;
    }

    void reset() {
        if (fence_ && !wait_idle()) return;  // leak rather than free objects the GPU may still use
        if (event_) CloseHandle(event_);
        event_ = nullptr;
        for (Frame& frame : frames_) release(frame.allocator), frame.fence_value = 0;
        release(list_);
        release(fence_);
        release(pipeline_);
        release(root_signature_);
        release(srv_heap_);
        release(rtv_heap_);
        release(cur_);
        release(saved_);
        release(device_);
        fence_counter_ = 0;
        next_frame_ = 0;
        ready_ = false;
    }

    // (Re)creates everything for this device and back buffer; on failure the compositor stays
    // unavailable until the back buffer changes again.
    void setup(ID3D12Device* device, const D3D12_RESOURCE_DESC& desc, std::string& message) {
        reset();
        device->AddRef();
        device_ = device;
        width_ = desc.Width;
        height_ = desc.Height;
        format_ = desc.Format;
        std::string error;
        if (build(desc, error)) {
            ready_ = true;
            char text[160];
            std::snprintf(text, sizeof(text), "compositor ready: %llux%u format %d",
                          static_cast<unsigned long long>(desc.Width), desc.Height, static_cast<int>(desc.Format));
            message = text;
        } else {
            message = "compositor disabled: " + error;
        }
    }

    bool build(const D3D12_RESOURCE_DESC& desc, std::string& error) {
        view_format_ = composite_view_format(desc.Format);
        if (view_format_ == DXGI_FORMAT_UNKNOWN || desc.SampleDesc.Count != 1 ||
            desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D) {
            char text[120];
            std::snprintf(text, sizeof(text), "unsupported back buffer (format %d, %u samples)",
                          static_cast<int>(desc.Format), desc.SampleDesc.Count);
            error = text;
            return false;
        }
        threshold_ = drone_marker_threshold(view_format_);

        D3D12_DESCRIPTOR_RANGE range = {};
        range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        range.NumDescriptors = 2;  // t0 cur, t1 saved
        D3D12_ROOT_PARAMETER params[2] = {};
        params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        params[0].Constants.Num32BitValues = 1;  // b0 threshold
        params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[1].DescriptorTable.NumDescriptorRanges = 1;
        params[1].DescriptorTable.pDescriptorRanges = &range;
        params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        D3D12_ROOT_SIGNATURE_DESC root = {};
        root.NumParameters = 2;
        root.pParameters = params;
        ID3DBlob* serialized = nullptr;
        ID3DBlob* messages = nullptr;
        HRESULT hr = D3D12SerializeRootSignature(&root, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, &messages);
        if (SUCCEEDED(hr)) {
            hr = device_->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                              IID_PPV_ARGS(&root_signature_));
        }
        release(serialized);
        release(messages);
        if (FAILED(hr)) {
            error = hresult_text("creating the root signature", hr);
            return false;
        }

        D3D12_GRAPHICS_PIPELINE_STATE_DESC pso = {};
        pso.pRootSignature = root_signature_;
        pso.VS = {kCompositeVs, sizeof(kCompositeVs)};
        pso.PS = {kCompositePs, sizeof(kCompositePs)};
        pso.BlendState.RenderTarget[0].LogicOp = D3D12_LOGIC_OP_NOOP;
        pso.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        pso.SampleMask = UINT_MAX;
        pso.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
        pso.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        pso.RasterizerState.DepthClipEnable = TRUE;
        pso.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        pso.NumRenderTargets = 1;
        pso.RTVFormats[0] = view_format_;
        pso.SampleDesc.Count = 1;
        hr = device_->CreateGraphicsPipelineState(&pso, IID_PPV_ARGS(&pipeline_));
        if (FAILED(hr)) {
            error = hresult_text("creating the pipeline", hr);
            return false;
        }

        // cur and saved: the back buffer's own format, so whole-resource copies are exact.
        D3D12_HEAP_PROPERTIES heap = {};
        heap.Type = D3D12_HEAP_TYPE_DEFAULT;
        D3D12_RESOURCE_DESC texture = {};
        texture.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        texture.Width = desc.Width;
        texture.Height = desc.Height;
        texture.DepthOrArraySize = 1;
        texture.MipLevels = 1;
        texture.Format = desc.Format;
        texture.SampleDesc.Count = 1;
        texture.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        hr = device_->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &texture,
                                              D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, nullptr, IID_PPV_ARGS(&cur_));
        if (SUCCEEDED(hr)) {
            hr = device_->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &texture,
                                                  D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, nullptr,
                                                  IID_PPV_ARGS(&saved_));
        }
        if (FAILED(hr)) {
            error = hresult_text("creating the cur/saved textures", hr);
            return false;
        }

        D3D12_DESCRIPTOR_HEAP_DESC srv_desc = {};
        srv_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        srv_desc.NumDescriptors = 2;
        srv_desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        D3D12_DESCRIPTOR_HEAP_DESC rtv_desc = {};
        rtv_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
        rtv_desc.NumDescriptors = 1;
        hr = device_->CreateDescriptorHeap(&srv_desc, IID_PPV_ARGS(&srv_heap_));
        if (SUCCEEDED(hr)) hr = device_->CreateDescriptorHeap(&rtv_desc, IID_PPV_ARGS(&rtv_heap_));
        for (Frame& frame : frames_) {
            if (SUCCEEDED(hr)) {
                hr = device_->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&frame.allocator));
            }
        }
        if (SUCCEEDED(hr)) {
            hr = device_->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, frames_[0].allocator, nullptr,
                                            IID_PPV_ARGS(&list_));
        }
        if (SUCCEEDED(hr)) hr = list_->Close();
        if (SUCCEEDED(hr)) hr = device_->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence_));
        if (SUCCEEDED(hr)) {
            event_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            if (!event_) hr = HRESULT_FROM_WIN32(GetLastError());
        }
        if (FAILED(hr)) {
            error = hresult_text("creating the heaps, command list or fence", hr);
            return false;
        }

        D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
        srv.Format = view_format_;
        srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Texture2D.MipLevels = 1;
        D3D12_CPU_DESCRIPTOR_HANDLE handle = srv_heap_->GetCPUDescriptorHandleForHeapStart();
        device_->CreateShaderResourceView(cur_, &srv, handle);
        handle.ptr += device_->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        device_->CreateShaderResourceView(saved_, &srv, handle);
        return true;
    }

    static D3D12_RESOURCE_BARRIER transition(ID3D12Resource* resource, D3D12_RESOURCE_STATES before,
                                             D3D12_RESOURCE_STATES after) {
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = resource;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = before;
        barrier.Transition.StateAfter = after;
        return barrier;
    }

    Result record(ID3D12CommandQueue* queue, ID3D12Resource* back_buffer) {
        Frame& frame = frames_[next_frame_];
        if (fence_->GetCompletedValue() < frame.fence_value) return Result::kBusy;
        if (FAILED(frame.allocator->Reset()) || FAILED(list_->Reset(frame.allocator, pipeline_))) {
            return Result::kUnavailable;
        }

        constexpr D3D12_RESOURCE_STATES kPresent = D3D12_RESOURCE_STATE_PRESENT;
        constexpr D3D12_RESOURCE_STATES kCopySource = D3D12_RESOURCE_STATE_COPY_SOURCE;
        constexpr D3D12_RESOURCE_STATES kCopyDest = D3D12_RESOURCE_STATE_COPY_DEST;
        constexpr D3D12_RESOURCE_STATES kShaderResource = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
        constexpr D3D12_RESOURCE_STATES kRenderTarget = D3D12_RESOURCE_STATE_RENDER_TARGET;
        const bool seed = !seeded_;

        // back buffer -> cur (and -> saved when seeding)
        D3D12_RESOURCE_BARRIER barriers[3] = {transition(back_buffer, kPresent, kCopySource),
                                              transition(cur_, kShaderResource, kCopyDest),
                                              transition(saved_, kShaderResource, kCopyDest)};
        list_->ResourceBarrier(seed ? 3 : 2, barriers);
        list_->CopyResource(cur_, back_buffer);
        if (seed) list_->CopyResource(saved_, back_buffer);
        barriers[0] = transition(back_buffer, kCopySource, kRenderTarget);
        barriers[1] = transition(cur_, kCopyDest, kShaderResource);
        barriers[2] = transition(saved_, kCopyDest, kShaderResource);
        list_->ResourceBarrier(seed ? 3 : 2, barriers);

        // back buffer = drone ? saved : cur
        D3D12_RENDER_TARGET_VIEW_DESC rtv_desc = {};
        rtv_desc.Format = view_format_;
        rtv_desc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
        const D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtv_heap_->GetCPUDescriptorHandleForHeapStart();
        device_->CreateRenderTargetView(back_buffer, &rtv_desc, rtv);
        const D3D12_VIEWPORT viewport = {0.0f, 0.0f, static_cast<float>(width_), static_cast<float>(height_),
                                         0.0f, 1.0f};
        const D3D12_RECT scissor = {0, 0, static_cast<LONG>(width_), static_cast<LONG>(height_)};
        list_->RSSetViewports(1, &viewport);
        list_->RSSetScissorRects(1, &scissor);
        list_->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
        list_->SetGraphicsRootSignature(root_signature_);
        ID3D12DescriptorHeap* heaps[] = {srv_heap_};
        list_->SetDescriptorHeaps(1, heaps);
        list_->SetGraphicsRoot32BitConstants(0, 1, &threshold_, 0);
        list_->SetGraphicsRootDescriptorTable(1, srv_heap_->GetGPUDescriptorHandleForHeapStart());
        list_->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        list_->DrawInstanced(3, 1, 0, 0);

        // back buffer -> saved, back buffer back to PRESENT
        barriers[0] = transition(back_buffer, kRenderTarget, kCopySource);
        barriers[1] = transition(saved_, kShaderResource, kCopyDest);
        list_->ResourceBarrier(2, barriers);
        list_->CopyResource(saved_, back_buffer);
        barriers[0] = transition(back_buffer, kCopySource, kPresent);
        barriers[1] = transition(saved_, kCopyDest, kShaderResource);
        list_->ResourceBarrier(2, barriers);
        if (FAILED(list_->Close())) return Result::kUnavailable;

        ID3D12CommandList* lists[] = {list_};
        execute_(queue, 1, lists);
        // Submitted: this allocator stays in flight until the fence reaches its value (never, should
        // the Signal fail; that keeps just this allocator busy).
        frame.fence_value = ++fence_counter_;
        next_frame_ = (next_frame_ + 1) % kFrames;
        seeded_ = true;
        return SUCCEEDED(queue->Signal(fence_, frame.fence_value)) ? Result::kRecorded : Result::kUnavailable;
    }

    ExecuteCommandListsFn execute_;
    ID3D12Device* device_ = nullptr;
    UINT64 width_ = 0;
    UINT height_ = 0;
    DXGI_FORMAT format_ = DXGI_FORMAT_UNKNOWN;
    DXGI_FORMAT view_format_ = DXGI_FORMAT_UNKNOWN;
    float threshold_ = 0.0f;
    bool ready_ = false;
    bool seeded_ = false;
    ID3D12RootSignature* root_signature_ = nullptr;
    ID3D12PipelineState* pipeline_ = nullptr;
    ID3D12DescriptorHeap* srv_heap_ = nullptr;
    ID3D12DescriptorHeap* rtv_heap_ = nullptr;
    ID3D12Resource* cur_ = nullptr;
    ID3D12Resource* saved_ = nullptr;
    Frame frames_[kFrames];
    int next_frame_ = 0;
    ID3D12GraphicsCommandList* list_ = nullptr;
    ID3D12Fence* fence_ = nullptr;
    HANDLE event_ = nullptr;
    uint64_t fence_counter_ = 0;
};

}  // namespace horus::capture

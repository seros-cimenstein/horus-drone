// Draws textured quads (drawTexture instances) into a render target on a given D3D12 queue: one
// root signature (12 root constants + one SRV + a linear clamp sampler), one pipeline per target
// format with straight-alpha blending, shaders compiled at run time with D3DCompile from
// d3dcompiler_47.dll. Shared by hook.cpp (into the swap chain's back buffer on the presenting queue)
// and hook/tests/texture_selftest.cpp (into an off-screen target).
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <d3d12.h>
#include <d3dcompiler.h>

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "texture.hpp"

namespace horus::hook {

struct QuadDraw {
    ID3D12Resource* texture;  // R8G8B8A8_UNORM, PIXEL_SHADER_RESOURCE state
    int srv_slot;             // from QuadRenderer::create_srv
    float corners[8];         // clip space, texture corners (0,0), (1,0), (0,1), (1,1)
    float color[4];           // multiplies the texel
};

// Render targets come and go in D3D12_RESOURCE_STATE_PRESENT (= COMMON), as a back buffer at
// Present; the same holds for an off-screen target created in COMMON state.
inline DXGI_FORMAT render_target_format(DXGI_FORMAT format) {
    switch (format) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS: return DXGI_FORMAT_R8G8B8A8_UNORM;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS: return DXGI_FORMAT_B8G8R8A8_UNORM;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS: return DXGI_FORMAT_R10G10B10A2_UNORM;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS: return DXGI_FORMAT_R16G16B16A16_FLOAT;
        default: return format;
    }
}

class QuadRenderer {
public:
    static constexpr int kSrvSlots = 1024;
    static constexpr int kFrames = 4;  // command allocators in flight

    ~QuadRenderer() { reset(); }

    ID3D12Device* device() const { return device_; }
    DXGI_FORMAT format() const { return format_; }

    bool init(ID3D12Device* device, DXGI_FORMAT format, std::string& error) {
        reset();
        device->AddRef();
        device_ = device;
        format_ = format;
        if (!build(error)) {
            reset();
            return false;
        }
        return true;
    }

    // A shader-visible SRV slot for `texture`, or -1 when all are taken.
    int create_srv(ID3D12Resource* texture) {
        int slot = -1;
        for (int i = 0; i < kSrvSlots && slot < 0; ++i) {
            if (!srv_used_[i]) slot = i;
        }
        if (slot < 0) return -1;
        srv_used_[slot] = true;
        D3D12_SHADER_RESOURCE_VIEW_DESC desc = {};
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        desc.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        desc.Texture2D.MipLevels = 1;
        D3D12_CPU_DESCRIPTOR_HANDLE handle = srv_heap_->GetCPUDescriptorHandleForHeapStart();
        handle.ptr += static_cast<SIZE_T>(slot) * srv_step_;
        device_->CreateShaderResourceView(texture, &desc, handle);
        return slot;
    }

    // Only after wait_idle(): the GPU may still read the slot's view.
    void free_srv(int slot) {
        if (slot >= 0 && slot < kSrvSlots) srv_used_[slot] = false;
    }

    // Records the quads into `target` (PRESENT/COMMON state before and after) and submits them on
    // `queue`, which must belong to this renderer's device. Ordering with other work on that queue
    // is the queue's submission order; a fence on it tracks completion of our part.
    bool draw(ID3D12CommandQueue* queue, ID3D12Resource* target, const std::vector<QuadDraw>& quads,
              std::string& error) {
        Frame& frame = frames_[next_frame_];
        next_frame_ = (next_frame_ + 1) % kFrames;
        if (!wait_for(frame.fence_value)) {
            error = "previous overlay frame still running after 5 s";
            return false;
        }
        const D3D12_RESOURCE_DESC desc = target->GetDesc();
        D3D12_RENDER_TARGET_VIEW_DESC rtv_desc = {};
        rtv_desc.Format = format_;
        rtv_desc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
        D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtv_heap_->GetCPUDescriptorHandleForHeapStart();
        device_->CreateRenderTargetView(target, &rtv_desc, rtv);

        HRESULT hr = frame.allocator->Reset();
        if (SUCCEEDED(hr)) hr = list_->Reset(frame.allocator, pipeline_);
        if (FAILED(hr)) {
            error = hresult_text("resetting the overlay command list", hr);
            return false;
        }
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = target;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        list_->ResourceBarrier(1, &barrier);
        D3D12_VIEWPORT viewport = {0.0f, 0.0f, static_cast<float>(desc.Width), static_cast<float>(desc.Height), 0.0f,
                                   1.0f};
        D3D12_RECT scissor = {0, 0, static_cast<LONG>(desc.Width), static_cast<LONG>(desc.Height)};
        list_->RSSetViewports(1, &viewport);
        list_->RSSetScissorRects(1, &scissor);
        list_->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
        list_->SetGraphicsRootSignature(root_signature_);
        ID3D12DescriptorHeap* heaps[] = {srv_heap_};
        list_->SetDescriptorHeaps(1, heaps);
        list_->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
        for (const QuadDraw& quad : quads) {
            float constants[12];
            std::memcpy(constants, quad.corners, sizeof(quad.corners));
            std::memcpy(constants + 8, quad.color, sizeof(quad.color));
            D3D12_GPU_DESCRIPTOR_HANDLE srv = srv_heap_->GetGPUDescriptorHandleForHeapStart();
            srv.ptr += static_cast<UINT64>(quad.srv_slot) * srv_step_;
            list_->SetGraphicsRoot32BitConstants(0, 12, constants, 0);
            list_->SetGraphicsRootDescriptorTable(1, srv);
            list_->DrawInstanced(4, 1, 0, 0);
        }
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list_->ResourceBarrier(1, &barrier);
        hr = list_->Close();
        if (FAILED(hr)) {
            error = hresult_text("closing the overlay command list", hr);
            return false;
        }
        ID3D12CommandList* lists[] = {list_};
        queue->ExecuteCommandLists(1, lists);
        frame.fence_value = ++fence_counter_;
        hr = queue->Signal(fence_, frame.fence_value);
        if (FAILED(hr)) {
            error = hresult_text("signalling the overlay fence", hr);
            return false;
        }
        return true;
    }

    // Waits (up to 5 s) until everything submitted has finished on the GPU.
    bool wait_idle() { return wait_for(fence_counter_); }

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
        release(device_);
        std::memset(srv_used_, 0, sizeof(srv_used_));
        fence_counter_ = 0;
        next_frame_ = 0;
    }

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

    bool wait_for(uint64_t value) {
        if (!fence_ || fence_->GetCompletedValue() >= value) return true;
        if (FAILED(fence_->SetEventOnCompletion(value, event_))) return false;
        return WaitForSingleObject(event_, 5000) == WAIT_OBJECT_0 || fence_->GetCompletedValue() >= value;
    }

    bool compile(const char* entry, const char* target, ID3DBlob** blob, std::string& error) {
        static const char kSource[] = R"(
cbuffer Quad : register(b0) { float4 corners01; float4 corners23; float4 color; };
Texture2D image : register(t0);
SamplerState linear_clamp : register(s0);
struct Vertex { float4 position : SV_Position; float2 uv : TEXCOORD0; };
Vertex vs_main(uint id : SV_VertexID) {
    Vertex v;
    float2 corner = id == 0 ? corners01.xy : id == 1 ? corners01.zw : id == 2 ? corners23.xy : corners23.zw;
    v.position = float4(corner, 0.0, 1.0);
    v.uv = float2(id & 1, id >> 1);
    return v;
}
float4 ps_main(Vertex v) : SV_Target { return image.Sample(linear_clamp, v.uv) * color; }
)";
        using D3DCompileFn = HRESULT(WINAPI*)(const void*, SIZE_T, const char*, const D3D_SHADER_MACRO*, ID3DInclude*,
                                              const char*, const char*, UINT, UINT, ID3DBlob**, ID3DBlob**);
        static D3DCompileFn compile_fn = [] {
            HMODULE module = LoadLibraryW(L"d3dcompiler_47.dll");
            return module ? reinterpret_cast<D3DCompileFn>(reinterpret_cast<void*>(GetProcAddress(module, "D3DCompile")))
                          : nullptr;
        }();
        if (!compile_fn) {
            error = "d3dcompiler_47.dll (D3DCompile) not available";
            return false;
        }
        ID3DBlob* messages = nullptr;
        const HRESULT hr = compile_fn(kSource, sizeof(kSource) - 1, "horus_quad", nullptr, nullptr, entry, target,
                                      D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, blob, &messages);
        if (FAILED(hr)) {
            error = hresult_text(entry, hr);
            if (messages) error += std::string(": ") + static_cast<const char*>(messages->GetBufferPointer());
        }
        release(messages);
        return SUCCEEDED(hr);
    }

    bool build(std::string& error) {
        D3D12_ROOT_PARAMETER params[2] = {};
        params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        params[0].Constants.Num32BitValues = 12;
        params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        D3D12_DESCRIPTOR_RANGE range = {};
        range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        range.NumDescriptors = 1;
        params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[1].DescriptorTable.NumDescriptorRanges = 1;
        params[1].DescriptorTable.pDescriptorRanges = &range;
        params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        D3D12_STATIC_SAMPLER_DESC sampler = {};
        sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
        sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
        sampler.MaxLOD = D3D12_FLOAT32_MAX;
        sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        D3D12_ROOT_SIGNATURE_DESC root = {};
        root.NumParameters = 2;
        root.pParameters = params;
        root.NumStaticSamplers = 1;
        root.pStaticSamplers = &sampler;
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
            error = hresult_text("creating the overlay root signature", hr);
            return false;
        }

        ID3DBlob* vs = nullptr;
        ID3DBlob* ps = nullptr;
        if (!compile("vs_main", "vs_5_0", &vs, error) || !compile("ps_main", "ps_5_0", &ps, error)) {
            release(vs);
            return false;
        }
        D3D12_GRAPHICS_PIPELINE_STATE_DESC pso = {};
        pso.pRootSignature = root_signature_;
        pso.VS = {vs->GetBufferPointer(), vs->GetBufferSize()};
        pso.PS = {ps->GetBufferPointer(), ps->GetBufferSize()};
        D3D12_RENDER_TARGET_BLEND_DESC& blend = pso.BlendState.RenderTarget[0];
        blend.BlendEnable = TRUE;
        blend.SrcBlend = D3D12_BLEND_SRC_ALPHA;
        blend.DestBlend = D3D12_BLEND_INV_SRC_ALPHA;
        blend.BlendOp = D3D12_BLEND_OP_ADD;
        blend.SrcBlendAlpha = D3D12_BLEND_ONE;
        blend.DestBlendAlpha = D3D12_BLEND_INV_SRC_ALPHA;
        blend.BlendOpAlpha = D3D12_BLEND_OP_ADD;
        blend.LogicOp = D3D12_LOGIC_OP_NOOP;
        blend.RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        pso.SampleMask = UINT_MAX;
        pso.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
        pso.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        pso.RasterizerState.DepthClipEnable = TRUE;
        pso.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        pso.NumRenderTargets = 1;
        pso.RTVFormats[0] = format_;
        pso.SampleDesc.Count = 1;
        hr = device_->CreateGraphicsPipelineState(&pso, IID_PPV_ARGS(&pipeline_));
        release(vs);
        release(ps);
        if (FAILED(hr)) {
            error = hresult_text("creating the overlay pipeline", hr);
            return false;
        }

        D3D12_DESCRIPTOR_HEAP_DESC srv_desc = {};
        srv_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        srv_desc.NumDescriptors = kSrvSlots;
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
            error = hresult_text("creating the overlay heaps, lists or fence", hr);
            return false;
        }
        srv_step_ = device_->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        return true;
    }

    ID3D12Device* device_ = nullptr;
    DXGI_FORMAT format_ = DXGI_FORMAT_UNKNOWN;
    ID3D12RootSignature* root_signature_ = nullptr;
    ID3D12PipelineState* pipeline_ = nullptr;
    ID3D12DescriptorHeap* srv_heap_ = nullptr;
    ID3D12DescriptorHeap* rtv_heap_ = nullptr;
    UINT srv_step_ = 0;
    bool srv_used_[kSrvSlots] = {};
    Frame frames_[kFrames];
    int next_frame_ = 0;
    ID3D12GraphicsCommandList* list_ = nullptr;
    ID3D12Fence* fence_ = nullptr;
    HANDLE event_ = nullptr;
    uint64_t fence_counter_ = 0;
};

}  // namespace horus::hook

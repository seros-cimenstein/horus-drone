// Shared by horus_capture.asi and the HORUS Script Hook: which D3D12 command queue presents which
// swap chain. Work that must be ordered with the game's frame (capture's back-buffer copy, the
// hook's drawTexture overlay) goes on that queue.
//
// A swap chain created after the hooks is recorded from IDXGIFactory::CreateSwapChain(ForHwnd)
// (whose "device" argument is the queue for D3D12). For one created earlier, the last direct queue
// passed to ExecuteCommandLists on the same device stands in.
#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>

#include <atomic>
#include <mutex>
#include <utility>
#include <vector>

namespace horus {

class SwapchainQueues {
public:
    // From the CreateSwapChain hooks: `swapchain` is the new swap chain's IUnknown identity. Returns
    // the queue now recorded for it (a reference this object holds), or nullptr if not D3D12.
    ID3D12CommandQueue* remember(IUnknown* swapchain, IUnknown* device_or_queue) {
        ID3D12CommandQueue* queue = nullptr;
        if (!swapchain || !device_or_queue || FAILED(device_or_queue->QueryInterface(IID_PPV_ARGS(&queue)))) {
            return nullptr;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto& entry : entries_) {
            if (entry.first == swapchain) {
                entry.second->Release();
                entry.second = queue;
                return queue;
            }
        }
        if (entries_.size() >= 8) {
            entries_.front().second->Release();
            entries_.erase(entries_.begin());
        }
        entries_.emplace_back(swapchain, queue);
        return queue;
    }

    // From the ExecuteCommandLists hook.
    void note_execute(ID3D12CommandQueue* queue) {
        if (queue != last_direct_.load(std::memory_order_relaxed) &&
            queue->GetDesc().Type == D3D12_COMMAND_LIST_TYPE_DIRECT) {
            last_direct_.store(queue, std::memory_order_release);
        }
    }

    // The queue presenting `swapchain` (`identity` is its IUnknown), or nullptr if none is known yet;
    // *fallback says whether it is the ExecuteCommandLists stand-in. Not AddRef'ed: valid while the
    // game keeps the queue, which outlives its swap chain.
    ID3D12CommandQueue* find(IDXGISwapChain3* swapchain, IUnknown* identity, bool* fallback) {
        *fallback = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            for (auto& entry : entries_) {
                if (entry.first == identity) return entry.second;
            }
        }
        ID3D12CommandQueue* queue = last_direct_.load(std::memory_order_acquire);
        if (!queue || !same_device(queue, swapchain)) return nullptr;
        *fallback = true;
        return queue;
    }

private:
    static bool same_device(ID3D12CommandQueue* queue, IDXGISwapChain3* swapchain) {
        ID3D12Device* queue_device = nullptr;
        ID3D12Device* swapchain_device = nullptr;
        ID3D12Resource* buffer = nullptr;
        bool same = false;
        if (SUCCEEDED(queue->GetDevice(IID_PPV_ARGS(&queue_device))) &&
            SUCCEEDED(swapchain->GetBuffer(0, IID_PPV_ARGS(&buffer))) &&
            SUCCEEDED(buffer->GetDevice(IID_PPV_ARGS(&swapchain_device)))) {
            same = queue_device == swapchain_device;
        }
        if (queue_device) queue_device->Release();
        if (swapchain_device) swapchain_device->Release();
        if (buffer) buffer->Release();
        return same;
    }

    std::mutex mutex_;
    std::vector<std::pair<IUnknown*, ID3D12CommandQueue*>> entries_;  // swap chain -> queue (owned ref)
    std::atomic<ID3D12CommandQueue*> last_direct_{nullptr};
};

}  // namespace horus

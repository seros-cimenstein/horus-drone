// Texture loading for the HORUS Script Hook's createTexture: decode an image file with WIC into
// RGBA8 and upload it to a D3D12 texture on a given device. Shared by hook.cpp (the game's device,
// at Present) and hook/tests/texture_selftest.cpp (its own device, with a read-back check).
#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <wincodec.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace horus::hook {

struct Image {
    uint32_t width = 0;
    uint32_t height = 0;
    std::vector<uint8_t> rgba;  // width * height * 4 bytes, rows top to bottom, straight alpha
};

inline std::string hresult_text(const char* what, HRESULT hr) {
    char text[96];
    std::snprintf(text, sizeof(text), "%s failed (0x%08lX)", what, static_cast<unsigned long>(hr));
    return text;
}

// Decodes the first frame of `path` (any format WIC reads: PNG, JPEG, BMP, ...) to RGBA8.
inline bool decode_image(const std::wstring& path, Image& out, std::string& error) {
    // The calling thread may already be in COM with another apartment model; that is fine for WIC.
    const HRESULT init = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    IWICImagingFactory* factory = nullptr;
    IWICBitmapDecoder* decoder = nullptr;
    IWICBitmapFrameDecode* frame = nullptr;
    IWICFormatConverter* converter = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory));
    if (FAILED(hr)) error = hresult_text("CoCreateInstance(WICImagingFactory)", hr);
    if (SUCCEEDED(hr)) {
        hr = factory->CreateDecoderFromFilename(path.c_str(), nullptr, GENERIC_READ, WICDecodeMetadataCacheOnDemand,
                                                &decoder);
        if (FAILED(hr)) error = hresult_text("opening or decoding the file", hr);
    }
    if (SUCCEEDED(hr)) {
        hr = decoder->GetFrame(0, &frame);
        if (FAILED(hr)) error = hresult_text("GetFrame(0)", hr);
    }
    if (SUCCEEDED(hr)) {
        hr = factory->CreateFormatConverter(&converter);
        if (SUCCEEDED(hr)) {
            hr = converter->Initialize(frame, GUID_WICPixelFormat32bppRGBA, WICBitmapDitherTypeNone, nullptr, 0.0,
                                       WICBitmapPaletteTypeCustom);
        }
        if (FAILED(hr)) error = hresult_text("converting to 32bpp RGBA", hr);
    }
    UINT width = 0, height = 0;
    if (SUCCEEDED(hr)) {
        hr = converter->GetSize(&width, &height);
        if (SUCCEEDED(hr) && (width == 0 || height == 0 || width > 16384 || height > 16384)) {
            hr = E_INVALIDARG;
            error = "image size " + std::to_string(width) + "x" + std::to_string(height) + " out of range";
        } else if (FAILED(hr)) {
            error = hresult_text("GetSize", hr);
        }
    }
    if (SUCCEEDED(hr)) {
        out.width = width;
        out.height = height;
        out.rgba.assign(static_cast<size_t>(width) * height * 4, 0);
        hr = converter->CopyPixels(nullptr, width * 4, static_cast<UINT>(out.rgba.size()), out.rgba.data());
        if (FAILED(hr)) error = hresult_text("CopyPixels", hr);
    }
    if (converter) converter->Release();
    if (frame) frame->Release();
    if (decoder) decoder->Release();
    if (factory) factory->Release();
    if (SUCCEEDED(init)) CoUninitialize();
    return SUCCEEDED(hr);
}

// Uploads images to DEFAULT-heap R8G8B8A8_UNORM textures on one device through its own direct
// queue, waiting for each copy; the textures end in PIXEL_SHADER_RESOURCE state.
class TextureUploader {
public:
    ~TextureUploader() { reset(); }

    ID3D12Device* device() const { return device_; }

    bool init(ID3D12Device* device, std::string& error) {
        reset();
        D3D12_COMMAND_QUEUE_DESC queue_desc = {};
        queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        HRESULT hr = device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue_));
        if (SUCCEEDED(hr)) hr = device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator_));
        if (SUCCEEDED(hr)) {
            hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator_, nullptr, IID_PPV_ARGS(&list_));
        }
        if (SUCCEEDED(hr)) hr = list_->Close();
        if (SUCCEEDED(hr)) hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence_));
        if (SUCCEEDED(hr)) {
            event_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            if (!event_) hr = HRESULT_FROM_WIN32(GetLastError());
        }
        if (FAILED(hr)) {
            error = hresult_text("creating the upload queue", hr);
            reset();
            return false;
        }
        device->AddRef();
        device_ = device;
        return true;
    }

    // A new texture holding `image`, or nullptr with `error` set.
    ID3D12Resource* upload(const Image& image, std::string& error) {
        if (!device_) {
            error = "no device";
            return nullptr;
        }
        D3D12_RESOURCE_DESC desc = {};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        desc.Width = image.width;
        desc.Height = image.height;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        D3D12_HEAP_PROPERTIES default_heap = {};
        default_heap.Type = D3D12_HEAP_TYPE_DEFAULT;
        ID3D12Resource* texture = nullptr;
        HRESULT hr = device_->CreateCommittedResource(&default_heap, D3D12_HEAP_FLAG_NONE, &desc,
                                                      D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&texture));
        if (FAILED(hr)) {
            error = hresult_text("CreateCommittedResource(texture)", hr);
            return nullptr;
        }

        D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
        UINT64 total = 0;
        device_->GetCopyableFootprints(&desc, 0, 1, 0, &footprint, nullptr, nullptr, &total);
        D3D12_HEAP_PROPERTIES upload_heap = {};
        upload_heap.Type = D3D12_HEAP_TYPE_UPLOAD;
        D3D12_RESOURCE_DESC buffer_desc = {};
        buffer_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        buffer_desc.Width = total;
        buffer_desc.Height = 1;
        buffer_desc.DepthOrArraySize = 1;
        buffer_desc.MipLevels = 1;
        buffer_desc.SampleDesc.Count = 1;
        buffer_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        ID3D12Resource* staging = nullptr;
        hr = device_->CreateCommittedResource(&upload_heap, D3D12_HEAP_FLAG_NONE, &buffer_desc,
                                              D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&staging));
        uint8_t* mapped = nullptr;
        if (SUCCEEDED(hr)) hr = staging->Map(0, nullptr, reinterpret_cast<void**>(&mapped));
        if (FAILED(hr)) {
            error = hresult_text("creating the upload buffer", hr);
            if (staging) staging->Release();
            texture->Release();
            return nullptr;
        }
        const size_t row = static_cast<size_t>(image.width) * 4;
        for (uint32_t y = 0; y < image.height; ++y) {
            std::memcpy(mapped + footprint.Offset + static_cast<size_t>(y) * footprint.Footprint.RowPitch,
                        image.rgba.data() + y * row, row);
        }
        staging->Unmap(0, nullptr);

        D3D12_TEXTURE_COPY_LOCATION dst = {};
        dst.pResource = texture;
        dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        dst.SubresourceIndex = 0;
        D3D12_TEXTURE_COPY_LOCATION src = {};
        src.pResource = staging;
        src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        src.PlacedFootprint = footprint;
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = texture;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
        hr = run([&](ID3D12GraphicsCommandList* list) {
            list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
            list->ResourceBarrier(1, &barrier);
        });
        staging->Release();
        if (FAILED(hr)) {
            error = hresult_text("copying to the texture", hr);
            texture->Release();
            return nullptr;
        }
        return texture;
    }

    // Records with `record`, runs on the queue and waits (up to 5 s); used by the self-test's
    // read-back too.
    template <typename Record>
    HRESULT run(Record record) {
        HRESULT hr = allocator_->Reset();
        if (SUCCEEDED(hr)) hr = list_->Reset(allocator_, nullptr);
        if (FAILED(hr)) return hr;
        record(list_);
        hr = list_->Close();
        return SUCCEEDED(hr) ? submit_and_wait() : hr;
    }

    void reset() {
        if (event_) CloseHandle(event_);
        event_ = nullptr;
        for (IUnknown** object : {reinterpret_cast<IUnknown**>(&fence_), reinterpret_cast<IUnknown**>(&list_),
                                  reinterpret_cast<IUnknown**>(&allocator_), reinterpret_cast<IUnknown**>(&queue_),
                                  reinterpret_cast<IUnknown**>(&device_)}) {
            if (*object) (*object)->Release();
            *object = nullptr;
        }
    }

private:
    HRESULT submit_and_wait() {
        ID3D12CommandList* lists[] = {list_};
        queue_->ExecuteCommandLists(1, lists);
        const uint64_t value = ++fence_value_;
        HRESULT hr = queue_->Signal(fence_, value);
        if (SUCCEEDED(hr) && fence_->GetCompletedValue() < value) {
            hr = fence_->SetEventOnCompletion(value, event_);
            if (SUCCEEDED(hr) && WaitForSingleObject(event_, 5000) != WAIT_OBJECT_0) hr = HRESULT_FROM_WIN32(ERROR_TIMEOUT);
        }
        return SUCCEEDED(hr) ? device_->GetDeviceRemovedReason() : hr;
    }

    ID3D12Device* device_ = nullptr;
    ID3D12CommandQueue* queue_ = nullptr;
    ID3D12CommandAllocator* allocator_ = nullptr;
    ID3D12GraphicsCommandList* list_ = nullptr;
    ID3D12Fence* fence_ = nullptr;
    HANDLE event_ = nullptr;
    uint64_t fence_value_ = 0;
};

}  // namespace horus::hook

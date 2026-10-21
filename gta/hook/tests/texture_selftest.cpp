// Self-test of createTexture/drawTexture's GPU code (hook/src/texture.hpp, texture_draw.hpp) on a
// real D3D12 device, without the game. Run under Proton on Koko (see gta/hook/README.md) or wine:
//   horus_texture_selftest.exe [work-dir [--log]]
// It writes PNGs with an independent encoder (test_png.hpp), decodes them with WIC and compares
// every byte, uploads them with TextureUploader, reads each texture back from the GPU and compares
// again. Then it draws a 2x2 texture with QuadRenderer into an off-screen target (placement, a
// quarter turn, alpha blending, draw order) and checks the pixels. With the D3D12 debug layer
// requested, it also counts error messages in the device's info queue when the runtime provides
// one. Prints PASS and exits 0, or each failure and exits 1; --log writes that report to
// work-dir\texture_selftest.log instead (`proton run` drops stdout).

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>

#include "hook_core.hpp"
#include "test_png.hpp"
#include "texture.hpp"
#include "texture_draw.hpp"

#include <cstdio>
#include <string>
#include <utility>
#include <vector>

namespace {

int g_failures = 0;

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);    \
            ++g_failures;                                                       \
        }                                                                       \
    } while (0)

using horus::hook::Image;

std::wstring widen(const std::string& text) { return std::wstring(text.begin(), text.end()); }

// GPU texture -> CPU bytes, rows packed; the texture is in `state` before and after.
bool read_back(horus::hook::TextureUploader& uploader, ID3D12Resource* texture, std::vector<uint8_t>& out,
               D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE) {
    ID3D12Device* device = uploader.device();
    const D3D12_RESOURCE_DESC desc = texture->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
    UINT64 total = 0;
    device->GetCopyableFootprints(&desc, 0, 1, 0, &footprint, nullptr, nullptr, &total);
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
    if (FAILED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer_desc, D3D12_RESOURCE_STATE_COPY_DEST,
                                               nullptr, IID_PPV_ARGS(&buffer)))) {
        return false;
    }
    const HRESULT hr = uploader.run([&](ID3D12GraphicsCommandList* list) {
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = texture;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = state;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
        list->ResourceBarrier(1, &barrier);
        D3D12_TEXTURE_COPY_LOCATION dst = {};
        dst.pResource = buffer;
        dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        dst.PlacedFootprint = footprint;
        D3D12_TEXTURE_COPY_LOCATION src = {};
        src.pResource = texture;
        src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list->ResourceBarrier(1, &barrier);
    });
    uint8_t* mapped = nullptr;
    const bool ok = SUCCEEDED(hr) && SUCCEEDED(buffer->Map(0, nullptr, reinterpret_cast<void**>(&mapped)));
    if (ok) {
        const size_t row = static_cast<size_t>(desc.Width) * 4;
        out.resize(row * desc.Height);
        for (UINT y = 0; y < desc.Height; ++y) {
            std::memcpy(out.data() + y * row, mapped + footprint.Offset + y * footprint.Footprint.RowPitch, row);
        }
        buffer->Unmap(0, nullptr);
    }
    buffer->Release();
    return ok;
}

// An off-screen 64x64 R8G8B8A8_UNORM render target in COMMON (= PRESENT) state, cleared to zero.
ID3D12Resource* make_target(horus::hook::TextureUploader& uploader) {
    ID3D12Device* device = uploader.device();
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = 64;
    desc.Height = 64;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    ID3D12Resource* target = nullptr;
    ID3D12DescriptorHeap* rtv_heap = nullptr;
    D3D12_DESCRIPTOR_HEAP_DESC heap_desc = {};
    heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heap_desc.NumDescriptors = 1;
    if (FAILED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COMMON, nullptr,
                                               IID_PPV_ARGS(&target))) ||
        FAILED(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&rtv_heap)))) {
        if (target) target->Release();
        return nullptr;
    }
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtv_heap->GetCPUDescriptorHandleForHeapStart();
    device->CreateRenderTargetView(target, nullptr, rtv);
    const HRESULT hr = uploader.run([&](ID3D12GraphicsCommandList* list) {
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = target;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        list->ResourceBarrier(1, &barrier);
        const float zero[4] = {};
        list->ClearRenderTargetView(rtv, zero, 0, nullptr);
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list->ResourceBarrier(1, &barrier);
    });
    rtv_heap->Release();
    if (FAILED(hr)) {
        target->Release();
        return nullptr;
    }
    return target;
}

// Draws `layouts` (in order) of `texture` with `alpha` into a fresh target and returns its pixels.
bool render(horus::hook::TextureUploader& uploader, horus::hook::QuadRenderer& renderer, ID3D12CommandQueue* queue,
            ID3D12Resource* texture, int srv, const std::vector<horus::hook::TextureLayout>& layouts, float alpha,
            std::vector<uint8_t>& pixels) {
    ID3D12Resource* target = make_target(uploader);
    if (!target) return false;
    std::vector<horus::hook::QuadDraw> quads;
    for (const horus::hook::TextureLayout& layout : layouts) {
        horus::hook::QuadDraw quad{texture, srv, {}, {1.0f, 1.0f, 1.0f, alpha}};
        horus::hook::texture_quad(layout, 64.0f, 64.0f, quad.corners);
        quads.push_back(quad);
    }
    std::string error;
    const bool drawn = renderer.draw(queue, target, quads, error) && renderer.wait_idle();
    if (!drawn) std::printf("draw: %s\n", error.c_str());
    const bool ok = drawn && read_back(uploader, target, pixels, D3D12_RESOURCE_STATE_COMMON);
    target->Release();
    return ok;
}

uint32_t pixel(const std::vector<uint8_t>& pixels, int x, int y) {
    const uint8_t* p = &pixels[(static_cast<size_t>(y) * 64 + x) * 4];
    return static_cast<uint32_t>(p[0]) << 24 | p[1] << 16 | p[2] << 8 | p[3];  // 0xRRGGBBAA
}

// drawTexture's renderer and layout on a real device: a 2x2 red/green/blue/white texture drawn at
// half the target's size in its centre (pixels 16..47), then turned, faded and drawn twice.
void test_draw(horus::hook::TextureUploader& uploader, ID3D12Device* device) {
    Image image;
    image.width = image.height = 2;
    image.rgba = {255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 255, 255};
    std::string error;
    ID3D12Resource* texture = uploader.upload(image, error);
    horus::hook::QuadRenderer renderer;
    ID3D12CommandQueue* queue = nullptr;
    D3D12_COMMAND_QUEUE_DESC queue_desc = {};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    const bool ready = texture && SUCCEEDED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue))) &&
                       renderer.init(device, DXGI_FORMAT_R8G8B8A8_UNORM, error);
    if (!ready) std::printf("draw setup: %s\n", error.c_str());
    CHECK(ready);
    if (ready) {
        const int srv = renderer.create_srv(texture);
        const horus::hook::TextureLayout centred = {0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.0f, 1.0f};
        horus::hook::TextureLayout turned = centred;
        turned.rotation = 0.25f;
        horus::hook::TextureLayout half_turn = centred;
        half_turn.rotation = 0.5f;
        std::vector<uint8_t> px;
        const uint32_t red = 0xFF0000FF, green = 0x00FF00FF, blue = 0x0000FFFF, white = 0xFFFFFFFF;

        CHECK(render(uploader, renderer, queue, texture, srv, {centred}, 1.0f, px));
        const bool placed = !px.empty() && pixel(px, 20, 20) == red && pixel(px, 43, 20) == green &&
                            pixel(px, 20, 43) == blue && pixel(px, 43, 43) == white && pixel(px, 8, 8) == 0 &&
                            pixel(px, 56, 56) == 0 && pixel(px, 15, 32) == 0 && pixel(px, 16, 32) != 0;
        CHECK(placed);
        std::printf("draw centred: %s\n", placed ? "corners and edges in place" : "WRONG");

        CHECK(render(uploader, renderer, queue, texture, srv, {turned}, 1.0f, px));
        const bool rotated = !px.empty() && pixel(px, 43, 20) == red && pixel(px, 43, 43) == green &&
                             pixel(px, 20, 20) == blue && pixel(px, 20, 43) == white;
        CHECK(rotated);
        std::printf("draw quarter turn clockwise: %s\n", rotated ? "red top right, blue top left" : "WRONG");

        CHECK(render(uploader, renderer, queue, texture, srv, {centred}, 0.5f, px));
        const uint32_t faded = px.empty() ? 0 : pixel(px, 43, 43);
        const bool blended =
            (faded >> 24) >= 127 && (faded >> 24) <= 128 && (faded & 0xFF) >= 127 && (faded & 0xFF) <= 128;
        CHECK(blended);
        std::printf("draw alpha 0.5 over black: 0x%08X\n", faded);

        CHECK(render(uploader, renderer, queue, texture, srv, {centred, half_turn}, 1.0f, px));
        const bool ordered = !px.empty() && pixel(px, 20, 20) == white && pixel(px, 43, 43) == red;
        CHECK(ordered);
        std::printf("draw order: %s\n", ordered ? "the second quad covers the first" : "WRONG");
    }
    renderer.reset();
    if (queue) queue->Release();
    if (texture) texture->Release();
}

}  // namespace

int main(int argc, char** argv) {
    std::string dir = argc > 1 ? argv[1] : ".";
    if (!dir.empty() && dir.back() != '\\' && dir.back() != '/') dir += '\\';
    if (argc > 2 && std::string(argv[2]) == "--log" &&
        !std::freopen((dir + "texture_selftest.log").c_str(), "w", stdout)) {
        return 2;
    }

    // Decode: sizes that exercise row pitch padding (37 px = 148 B rows, pitch 256) and 1x1.
    struct Case {
        uint32_t width, height;
    };
    const Case cases[] = {{37, 23}, {1, 1}, {256, 64}};
    std::vector<Image> images;
    for (const Case& c : cases) {
        const std::string path = dir + "texture_selftest_" + std::to_string(c.width) + "x" + std::to_string(c.height) + ".png";
        const std::vector<uint8_t> expected = horus::hook::test::pattern_rgba(c.width, c.height);
        CHECK(horus::hook::test::write_png(path, c.width, c.height, expected));
        Image image;
        std::string error;
        const bool decoded = horus::hook::decode_image(widen(path), image, error);
        if (!decoded) std::printf("decode %s: %s\n", path.c_str(), error.c_str());
        CHECK(decoded && image.width == c.width && image.height == c.height && image.rgba == expected);
        std::printf("decoded %s: %ux%u%s\n", path.c_str(), image.width, image.height,
                    decoded && image.rgba == expected ? ", pixels match" : "");
        images.push_back(std::move(image));
        DeleteFileA(path.c_str());
    }
    {
        Image image;
        std::string error;
        CHECK(!horus::hook::decode_image(widen(dir + "no_such_texture.png"), image, error) && !error.empty());
        std::printf("missing file: %s\n", error.c_str());
        const std::string text = dir + "texture_selftest_not_an_image.png";
        FILE* file = std::fopen(text.c_str(), "wb");
        if (file) {
            std::fputs("not an image\n", file);
            std::fclose(file);
        }
        CHECK(!horus::hook::decode_image(widen(text), image, error) && !error.empty());
        std::printf("not an image: %s\n", error.c_str());
        DeleteFileA(text.c_str());
    }

    // Upload and read back on a real device.
    ID3D12Debug* debug = nullptr;
    if (SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug)))) {
        debug->EnableDebugLayer();
        debug->Release();
        std::printf("D3D12 debug layer requested\n");
    }
    ID3D12Device* device = nullptr;
    HRESULT hr = D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device));
    CHECK(SUCCEEDED(hr));
    if (SUCCEEDED(hr)) {
        ID3D12InfoQueue* info = nullptr;
        device->QueryInterface(IID_PPV_ARGS(&info));
        horus::hook::TextureUploader uploader;
        std::string error;
        CHECK(uploader.init(device, error));
        for (const Image& image : images) {
            ID3D12Resource* texture = uploader.upload(image, error);
            if (!texture) std::printf("upload %ux%u: %s\n", image.width, image.height, error.c_str());
            CHECK(texture != nullptr);
            if (!texture) continue;
            std::vector<uint8_t> back;
            CHECK(read_back(uploader, texture, back));
            CHECK(back == image.rgba);
            std::printf("uploaded %ux%u, read back: %s\n", image.width, image.height,
                        back == image.rgba ? "pixels match" : "MISMATCH");
            texture->Release();
        }
        test_draw(uploader, device);
        CHECK(device->GetDeviceRemovedReason() == S_OK);
        if (info) {
            UINT64 errors = 0;
            for (UINT64 i = 0, n = info->GetNumStoredMessages(); i < n; ++i) {
                SIZE_T size = 0;
                info->GetMessage(i, nullptr, &size);
                std::vector<uint8_t> storage(size);
                auto* message = reinterpret_cast<D3D12_MESSAGE*>(storage.data());
                if (SUCCEEDED(info->GetMessage(i, message, &size)) &&
                    message->Severity <= D3D12_MESSAGE_SEVERITY_ERROR) {
                    std::printf("D3D12 error: %s\n", message->pDescription);
                    ++errors;
                }
            }
            std::printf("D3D12 info queue: %llu error message(s)\n", static_cast<unsigned long long>(errors));
            CHECK(errors == 0);
            info->Release();
        } else {
            std::printf("D3D12 info queue: not provided by this runtime\n");
        }
        uploader.reset();
        device->Release();
    }

    if (g_failures) {
        std::printf("%d check(s) FAILED\n", g_failures);
        return 1;
    }
    std::printf("PASS\n");
    return 0;
}

// horus_capture's shared-view compositor (compositor.hpp). One fullscreen triangle into the back
// buffer: when the frame in `cur` (a copy of the back buffer) carries HorusGta's drone marker, the
// pixel comes from `saved` (the last frame shown), otherwise from `cur`. The marker test reads
// texel (2, 2) like the capture worker (shared_view.hpp frame_is_drone); `threshold` is
// drone_marker_threshold() of the back buffer's format.
//
// Compiled into composite_shaders.hpp by tools/shader_gen.cpp (gta/capture/README.md "Shaders").

cbuffer Marker : register(b0) { float threshold; };
Texture2D<float4> cur : register(t0);
Texture2D<float4> saved : register(t1);

float4 vs_main(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);  // (0,0) (2,0) (0,2): covers the whole target
    return float4(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0, 0.0, 1.0);
}

float4 ps_main(float4 position : SV_Position) : SV_Target {
    int3 p = int3(int2(position.xy), 0);
    float3 marker = cur.Load(int3(2, 2, 0)).rgb;
    if (max(marker.r, max(marker.g, marker.b)) <= threshold) return saved.Load(p);
    return cur.Load(p);
}

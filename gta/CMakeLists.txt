cmake_minimum_required(VERSION 3.20)
project(horus_gta_native LANGUAGES C CXX RC)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
if(POLICY CMP0169)
  cmake_policy(SET CMP0169 OLD)  # FetchContent_Populate: MinHook sources only, not its CMake project
endif()
FetchContent_Declare(minhook
  GIT_REPOSITORY https://github.com/TsudaKageyu/minhook.git
  GIT_TAG c3fcafdc10146beb5919319d0683e44e3c30d537 # v1.3.4
)
FetchContent_GetProperties(minhook)
if(NOT minhook_POPULATED)
  FetchContent_Populate(minhook)
endif()
add_library(minhook STATIC
  ${minhook_SOURCE_DIR}/src/buffer.c
  ${minhook_SOURCE_DIR}/src/hook.c
  ${minhook_SOURCE_DIR}/src/trampoline.c
  ${minhook_SOURCE_DIR}/src/hde/hde64.c
)
target_include_directories(minhook PUBLIC ${minhook_SOURCE_DIR}/include)

set(static_runtime -static -static-libgcc -static-libstdc++)

# ASI loaders LoadLibrary any *.asi; it is an ordinary DLL with a different suffix.
add_library(horus_capture SHARED capture/src/capture.cpp)
set_target_properties(horus_capture PROPERTIES PREFIX "" SUFFIX ".asi")
target_compile_options(horus_capture PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_capture PRIVATE common)
target_link_libraries(horus_capture PRIVATE minhook d3d12 dxgi ws2_32 ${static_runtime})

add_executable(horus_capture_selftest capture/selftest/selftest.cpp)
target_compile_options(horus_capture_selftest PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_capture_selftest PRIVATE common)
target_link_libraries(horus_capture_selftest PRIVATE d3d12 dxgi ${static_runtime})

# HORUS Script Hook: our own implementation of the Script Hook V plugin API. Plugins link against
# "ScriptHookV.dll" by name, so the output keeps that name; exports.def gives the MSVC-mangled names.
add_library(horus_hook SHARED hook/src/hook.cpp hook/src/exports.def)
set_target_properties(horus_hook PROPERTIES PREFIX "" OUTPUT_NAME "ScriptHookV")
target_compile_options(horus_hook PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_hook PRIVATE common)
target_link_libraries(horus_hook PRIVATE minhook d3d12 dxgi windowscodecs ole32 uuid ${static_runtime})

# Unit tests of the hook's pure parts and of its export names (run under wine, see gta/hook/README.md).
add_executable(horus_hook_tests hook/tests/hook_tests.cpp hook/tests/hook_tests.rc)
target_compile_options(horus_hook_tests PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_hook_tests PRIVATE hook/src)
target_link_libraries(horus_hook_tests PRIVATE version ${static_runtime})  # version: reference parser in the tests
add_dependencies(horus_hook_tests horus_hook)

# createTexture's decode/upload path on a real D3D12 device, without the game (see gta/hook/README.md).
add_executable(horus_texture_selftest hook/tests/texture_selftest.cpp)
target_compile_options(horus_texture_selftest PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_texture_selftest PRIVATE hook/src)
target_link_libraries(horus_texture_selftest PRIVATE d3d12 windowscodecs ole32 uuid ${static_runtime})

# In-game API probe plugin (installed only by `gta_mods.sh install --api-probe`, see gta/hook/README.md).
add_library(horus_api_probe SHARED hook/tests/api_probe.cpp)
set_target_properties(horus_api_probe PROPERTIES PREFIX "" SUFFIX ".asi")
target_compile_options(horus_api_probe PRIVATE -O2 -Wall -Wextra)
target_include_directories(horus_api_probe PRIVATE hook/src)
target_link_libraries(horus_api_probe PRIVATE ${static_runtime})

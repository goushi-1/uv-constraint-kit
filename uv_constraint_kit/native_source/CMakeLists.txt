cmake_minimum_required(VERSION 3.20)

project(uvck_engine LANGUAGES CXX)

add_library(uvck_engine SHARED engine.cpp)
target_compile_features(uvck_engine PRIVATE cxx_std_17)
set_target_properties(uvck_engine PROPERTIES
    CXX_EXTENSIONS OFF
    MSVC_RUNTIME_LIBRARY MultiThreaded
    OUTPUT_NAME uvck_engine
    WINDOWS_EXPORT_ALL_SYMBOLS OFF
)

if(MSVC)
    target_compile_options(uvck_engine PRIVATE /O2 /fp:precise)
else()
    message(FATAL_ERROR "uvck_engine is configured for the MSVC x64 toolchain")
endif()

option(UVCK_BUILD_READER "Build optional read-only CPython stable-ABI adapter" OFF)
set(UVCK_PYTHON_INCLUDE "" CACHE PATH "Directory containing Python.h")
set(UVCK_PYTHON_LIBRARY "" CACHE FILEPATH "Stable ABI python3.lib")
if(UVCK_BUILD_READER)
    if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
        message(FATAL_ERROR "uvck_reader requires MSVC x64")
    endif()
    if(NOT EXISTS "${UVCK_PYTHON_INCLUDE}/Python.h" OR NOT EXISTS "${UVCK_PYTHON_LIBRARY}")
        message(FATAL_ERROR "Set UVCK_PYTHON_INCLUDE and the stable ABI UVCK_PYTHON_LIBRARY")
    endif()
    add_library(uvck_reader MODULE mesh_reader.cpp)
    target_include_directories(uvck_reader PRIVATE "${UVCK_PYTHON_INCLUDE}")
    target_link_libraries(uvck_reader PRIVATE "${UVCK_PYTHON_LIBRARY}")
    target_compile_features(uvck_reader PRIVATE cxx_std_17)
    target_compile_options(uvck_reader PRIVATE /O2 /fp:precise)
    set_target_properties(uvck_reader PROPERTIES PREFIX "" SUFFIX ".pyd"
        OUTPUT_NAME "_uvck_reader" MSVC_RUNTIME_LIBRARY MultiThreaded CXX_EXTENSIONS OFF)
endif()

option(UVCK_BUILD_GPU "Build the optional CUDA-driver GPU sidecar" OFF)
set(UVCK_CUDA_INCLUDE "" CACHE PATH "Directory containing cuda.h")
if(UVCK_BUILD_GPU)
    if(NOT MSVC)
        message(FATAL_ERROR "uvck_gpu requires the MSVC x64 toolchain")
    endif()
    if(NOT EXISTS "${UVCK_CUDA_INCLUDE}/cuda.h")
        message(FATAL_ERROR "Set UVCK_CUDA_INCLUDE to a CUDA runtime include directory")
    endif()
    add_library(uvck_gpu SHARED engine_gpu.cpp)
    target_compile_features(uvck_gpu PRIVATE cxx_std_17)
    target_include_directories(uvck_gpu PRIVATE "${UVCK_CUDA_INCLUDE}")
    target_compile_options(uvck_gpu PRIVATE /O2 /fp:precise)
    set_target_properties(uvck_gpu PROPERTIES
        CXX_EXTENSIONS OFF
        MSVC_RUNTIME_LIBRARY MultiThreaded
        OUTPUT_NAME uvck_gpu
        WINDOWS_EXPORT_ALL_SYMBOLS OFF
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/gpu"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/gpu"
    )
endif()

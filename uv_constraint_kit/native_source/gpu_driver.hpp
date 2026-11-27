// SPDX-License-Identifier: GPL-3.0-or-later
// Optional CUDA bridge. The separate CPU engine does not include this file.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <cuda.h>
#include <fstream>
#include <filesystem>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

static std::recursive_mutex gpu_mutex;
static int gpu_mode = 0;
static std::string gpu_error;
// sessions, energy calls, gradient pipelines, errors, HtoD bytes, DtoH bytes, initializations
static std::array<I,7> gpu_counts{};

static constexpr int SC_ENERGY = 0;
static constexpr int SC_INVALID = 1;
static constexpr int SC_DOT = 2;
static constexpr int SC_MAX_G = 3;
static constexpr int SC_SLOPE = 4;
static constexpr int SC_GAMMA = 5;
static constexpr int SC_SY = 6;
static constexpr int SC_YMY = 7;
static constexpr int SC_TMIN = 8;
static constexpr int SC_PEN_E = 9;
static constexpr int SC_BETA = 10;
static constexpr int SC_RESET_FLAG = 11;
static constexpr int SC_ALPHA_BASE = 16;
static constexpr int SC_RHO_BASE = 24;
static constexpr int SC_COUNT = 32;
static constexpr int GPU_HIST_LIMIT = 8;
static constexpr int GPU_MAX_REDUCE_BLOCKS = 256;

static std::uint64_t fnv64_bytes(const void* data, std::size_t nbytes) noexcept {
    const auto* p = static_cast<const std::uint8_t*>(data);
    std::uint64_t h = 14695981039346656037ULL;
    std::size_t i = 0;
    for(; i + 8 <= nbytes; i += 8) {
        std::uint64_t w = 0;
        std::memcpy(&w, p + i, 8);
        h ^= w;
        h *= 1099511628211ULL;
    }
    for(; i < nbytes; ++i) {
        h ^= static_cast<std::uint64_t>(p[i]);
        h *= 1099511628211ULL;
    }
    return h;
}

struct CachedGpuBuffers {
    I cap_n = 0, cap_nt = 0;
    I cap_sets = 0, cap_set_members = 0;
    I cap_ne = 0, cap_pv = 0, cap_pv_edges = 0;
    I cap_nbv = 0;
    I cached_n = 0, cached_nt = 0;
    std::uint64_t tri_hash = 0, inv_hash = 0, weights_hash = 0;
    bool tri_valid = false, inv_valid = false, weights_valid = false;

    // Legacy & FP64 geometry buffers
    CUdeviceptr uv = 0, triangles = 0, j = 0, inverse = 0, weights = 0, result = 0;
    CUdeviceptr adjacency_offsets = 0, adjacency_entries = 0, vertex_gradient = 0;

    // Resident FP32 + int32 constant buffers
    CUdeviceptr tri_i32 = 0, adj_off_i32 = 0, adj_ent_i32 = 0;
    CUdeviceptr inv_f = 0, weights_f = 0;

    // Resident FP64 solver vectors
    CUdeviceptr x_d = 0, cand_d = 0, g_d = 0, next_g_d = 0, dir_d = 0;
    CUdeviceptr inv_diag_d = 0, held_d = 0;
    CUdeviceptr s_hist_d[GPU_HIST_LIMIT]{}, y_hist_d[GPU_HIST_LIMIT]{};

    // Resident FP32 solver vectors
    CUdeviceptr j_f = 0, gu_f = 0;
    CUdeviceptr x_f = 0, cand_f = 0, g_f = 0, next_g_f = 0, dir_f = 0;
    CUdeviceptr inv_diag_f = 0, held_f = 0;
    CUdeviceptr s_hist_f[GPU_HIST_LIMIT]{}, y_hist_f[GPU_HIST_LIMIT]{};

    // Fixed vertices & reductions
    CUdeviceptr fixed_u8 = 0;
    CUdeviceptr block_buf0 = 0, block_buf1 = 0, block_buf2 = 0, block_inv = 0;
    CUdeviceptr scalars = 0;

    // Dynamic constraint / penalty / border buffers
    CUdeviceptr set_offsets = 0, set_members = 0, set_axis = 0, set_anchored = 0, set_anchor = 0;
    CUdeviceptr pen_edges = 0, pen_axis = 0, pen_w_d = 0, pen_w_f = 0;
    CUdeviceptr pv_ids = 0, pv_offsets = 0, pv_edges = 0;
    CUdeviceptr border_verts = 0, border_out = 0;

    void* staging_in = nullptr;
    void* staging_out = nullptr;
    double* staging_scalars = nullptr;
    double* staging_border = nullptr;
    CUstream stream = nullptr;
};

struct CudaRuntime {
    HMODULE dll = nullptr;
    CUdevice device = 0;
    CUcontext context = nullptr;
    CUmodule module = nullptr;
    bool poisoned = false;
    std::string path;
    char name[256]{};
    CachedGpuBuffers cache{};

    // Legacy kernels
    CUfunction energy_kernel = nullptr, gradient_kernel = nullptr, scatter_kernel = nullptr;
    // Conversion & reduction kernels
    CUfunction cast_d2f = nullptr, cast_f2d = nullptr, cast_i64_to_i32 = nullptr;
    CUfunction reduce_sum_to_slot = nullptr, reduce_energy_final = nullptr;
    CUfunction reduce_alpha = nullptr, reduce_beta = nullptr;
    CUfunction reduce_max_to_slot = nullptr, reduce_min_to_slot = nullptr;
    CUfunction reduce_sy_ymy_maxg = nullptr, add_pen_energy = nullptr;
    // FP32 resident kernels
    CUfunction energy_block_f32 = nullptr, grad_f32 = nullptr, scatter_f32 = nullptr;
    CUfunction penalty_energy_block_f32 = nullptr, penalty_grad_f32 = nullptr;
    CUfunction project_axes_f32 = nullptr, project_grad_axes_f32 = nullptr;
    CUfunction apply_fixed_f32 = nullptr, zero_fixed_f32 = nullptr, copy_f32 = nullptr;
    CUfunction dot_block_f32 = nullptr, axpy_slot_f32 = nullptr, scale_diag_slot_f32 = nullptr;
    CUfunction negate_dot_block_f32 = nullptr, cand_step_f32 = nullptr, cand_trial0_f32 = nullptr;
    CUfunction max_abs_block_f32 = nullptr, sy_ymy_block_f32 = nullptr;
    CUfunction max_flip_step_block_f32 = nullptr, gather_border_f32 = nullptr;
    // FP64 resident kernels
    CUfunction energy_block_d = nullptr, scatter_i32_d = nullptr;
    CUfunction penalty_energy_block_d = nullptr, penalty_grad_d = nullptr;
    CUfunction project_axes_d = nullptr, project_grad_axes_d = nullptr;
    CUfunction apply_fixed_d = nullptr, zero_fixed_d = nullptr, copy_d = nullptr;
    CUfunction dot_block_d = nullptr, axpy_slot_d = nullptr, scale_diag_slot_d = nullptr;
    CUfunction negate_dot_block_d = nullptr, cand_step_d = nullptr, cand_trial0_d = nullptr;
    CUfunction max_abs_block_d = nullptr, sy_ymy_block_d = nullptr;
    CUfunction max_flip_step_block_d = nullptr, gather_border_d = nullptr;

#define CUDA_FN(member, fn) decltype(&::fn) member = nullptr
    CUDA_FN(init, cuInit); CUDA_FN(device_get, cuDeviceGet);
    CUDA_FN(device_count, cuDeviceGetCount); CUDA_FN(device_attribute, cuDeviceGetAttribute);
    CUDA_FN(driver_version, cuDriverGetVersion); CUDA_FN(memory_info, cuMemGetInfo);
    CUDA_FN(device_name, cuDeviceGetName);
    CUDA_FN(retain, cuDevicePrimaryCtxRetain); CUDA_FN(release, cuDevicePrimaryCtxRelease);
    CUDA_FN(push, cuCtxPushCurrent); CUDA_FN(pop, cuCtxPopCurrent);
    CUDA_FN(load, cuModuleLoadData); CUDA_FN(unload, cuModuleUnload);
    CUDA_FN(function, cuModuleGetFunction);
    CUDA_FN(allocate, cuMemAlloc); CUDA_FN(free_memory, cuMemFree);
    CUDA_FN(upload, cuMemcpyHtoDAsync); CUDA_FN(download, cuMemcpyDtoHAsync);
    CUDA_FN(memcpy_dto_d, cuMemcpyDtoDAsync);
    CUDA_FN(host_allocate, cuMemHostAlloc); CUDA_FN(host_free, cuMemFreeHost);
    CUDA_FN(stream_create, cuStreamCreate); CUDA_FN(stream_destroy, cuStreamDestroy);
    CUDA_FN(launch, cuLaunchKernel); CUDA_FN(wait, cuStreamSynchronize);
    CUDA_FN(error_name, cuGetErrorName);
#undef CUDA_FN

    template<class T> void symbol(T& value, const char* sym) {
        value = reinterpret_cast<T>(GetProcAddress(dll, sym));
        if(!value) throw std::runtime_error(std::string("Missing CUDA driver symbol: ") + sym);
    }
    void check(CUresult result, const char* action) const {
        if(result == CUDA_SUCCESS) return;
        const char* reason = nullptr;
        if(error_name) error_name(result, &reason);
        throw std::runtime_error(std::string(action) + ": " + (reason ? reason : std::to_string(result)));
    }
    void free_ptr(CUdeviceptr& p) noexcept {
        if(p) { free_memory(p); p = 0; }
    }
    void release_cache_in_context() noexcept {
        if(cache.stream) {
            if(wait(cache.stream) != CUDA_SUCCESS) {
                poisoned = true;
                cache = {};
                return;
            }
            stream_destroy(cache.stream);
            cache.stream = nullptr;
        }
        CUdeviceptr* ptrs[] = {
            &cache.uv, &cache.triangles, &cache.j, &cache.inverse, &cache.weights, &cache.result,
            &cache.adjacency_offsets, &cache.adjacency_entries, &cache.vertex_gradient,
            &cache.tri_i32, &cache.adj_off_i32, &cache.adj_ent_i32, &cache.inv_f, &cache.weights_f,
            &cache.x_d, &cache.cand_d, &cache.g_d, &cache.next_g_d, &cache.dir_d,
            &cache.inv_diag_d, &cache.held_d,
            &cache.j_f, &cache.gu_f, &cache.x_f, &cache.cand_f, &cache.g_f, &cache.next_g_f,
            &cache.dir_f, &cache.inv_diag_f, &cache.held_f,
            &cache.fixed_u8, &cache.block_buf0, &cache.block_buf1, &cache.block_buf2,
            &cache.block_inv, &cache.scalars,
            &cache.set_offsets, &cache.set_members, &cache.set_axis, &cache.set_anchored, &cache.set_anchor,
            &cache.pen_edges, &cache.pen_axis, &cache.pen_w_d, &cache.pen_w_f,
            &cache.pv_ids, &cache.pv_offsets, &cache.pv_edges,
            &cache.border_verts, &cache.border_out
        };
        for(auto* p : ptrs) free_ptr(*p);
        for(int k = 0; k < GPU_HIST_LIMIT; ++k) {
            free_ptr(cache.s_hist_d[k]); free_ptr(cache.y_hist_d[k]);
            free_ptr(cache.s_hist_f[k]); free_ptr(cache.y_hist_f[k]);
        }
        if(cache.staging_in) { host_free(cache.staging_in); cache.staging_in = nullptr; }
        if(cache.staging_out) { host_free(cache.staging_out); cache.staging_out = nullptr; }
        if(cache.staging_scalars) { host_free(cache.staging_scalars); cache.staging_scalars = nullptr; }
        if(cache.staging_border) { host_free(cache.staging_border); cache.staging_border = nullptr; }
        cache = {};
    }
    explicit CudaRuntime(const std::string& ptx_path) : path(ptx_path) {
        dll = LoadLibraryExW(L"nvcuda.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        if(!dll) throw std::runtime_error("NVIDIA CUDA driver unavailable");
        bool pushed = false;
        try {
#define LOAD(member, sym) symbol(member, sym)
            LOAD(init, "cuInit"); LOAD(device_get, "cuDeviceGet"); LOAD(device_name, "cuDeviceGetName");
            LOAD(device_count, "cuDeviceGetCount"); LOAD(device_attribute, "cuDeviceGetAttribute");
            LOAD(driver_version, "cuDriverGetVersion"); LOAD(memory_info, "cuMemGetInfo_v2");
            LOAD(retain, "cuDevicePrimaryCtxRetain"); LOAD(release, "cuDevicePrimaryCtxRelease_v2");
            LOAD(push, "cuCtxPushCurrent_v2"); LOAD(pop, "cuCtxPopCurrent_v2");
            LOAD(load, "cuModuleLoadData"); LOAD(unload, "cuModuleUnload"); LOAD(function, "cuModuleGetFunction");
            LOAD(allocate, "cuMemAlloc_v2"); LOAD(free_memory, "cuMemFree_v2");
            LOAD(upload, "cuMemcpyHtoDAsync_v2"); LOAD(download, "cuMemcpyDtoHAsync_v2");
            LOAD(memcpy_dto_d, "cuMemcpyDtoDAsync_v2");
            LOAD(host_allocate, "cuMemHostAlloc"); LOAD(host_free, "cuMemFreeHost");
            LOAD(stream_create, "cuStreamCreate"); LOAD(stream_destroy, "cuStreamDestroy_v2");
            LOAD(launch, "cuLaunchKernel"); LOAD(wait, "cuStreamSynchronize"); LOAD(error_name, "cuGetErrorName");
#undef LOAD
            std::ifstream file(std::filesystem::u8path(ptx_path), std::ios::binary);
            if(!file) throw std::runtime_error("GPU PTX file unavailable");
            std::string ptx((std::istreambuf_iterator<char>(file)), {});
            if(ptx.empty() || ptx.size() > 10*1024*1024) throw std::runtime_error("Invalid GPU PTX size");
            check(init(0), "cuInit");
            int version = 0, count = 0;
            check(driver_version(&version), "cuDriverGetVersion");
            if(version < 12040) throw std::runtime_error("CUDA driver lacks the required PTX version");
            check(device_count(&count), "cuDeviceGetCount");
            bool compatible = false;
            for(int index = 0; index < count; ++index) {
                CUdevice candidate; int major = 0, minor = 0;
                check(device_get(&candidate, index), "cuDeviceGet");
                check(device_attribute(&major, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR, candidate), "compute major");
                check(device_attribute(&minor, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR, candidate), "compute minor");
                if(major*10 + minor >= 86) { device = candidate; compatible = true; break; }
            }
            if(!compatible) throw std::runtime_error("No CUDA device supports compute_86 PTX");
            check(device_name(name, sizeof(name), device), "cuDeviceGetName");
            check(retain(&context, device), "cuDevicePrimaryCtxRetain");
            check(push(context), "cuCtxPushCurrent"); pushed = true;
            check(load(&module, ptx.c_str()), "cuModuleLoadData");
#define KERN(member, sym) check(function(&member, module, sym), sym)
            KERN(energy_kernel, "uvck_energy");
            KERN(gradient_kernel, "uvck_gradient");
            KERN(scatter_kernel, "uvck_scatter");
            KERN(cast_d2f, "uvck_cast_d2f");
            KERN(cast_f2d, "uvck_cast_f2d");
            KERN(cast_i64_to_i32, "uvck_cast_i64_to_i32");
            KERN(reduce_sum_to_slot, "uvck_reduce_sum_to_slot");
            KERN(reduce_energy_final, "uvck_reduce_energy_final");
            KERN(reduce_alpha, "uvck_reduce_alpha");
            KERN(reduce_beta, "uvck_reduce_beta");
            KERN(reduce_max_to_slot, "uvck_reduce_max_to_slot");
            KERN(reduce_min_to_slot, "uvck_reduce_min_to_slot");
            KERN(reduce_sy_ymy_maxg, "uvck_reduce_sy_ymy_maxg");
            KERN(add_pen_energy, "uvck_add_pen_energy");
            KERN(energy_block_f32, "uvck_energy_block_f32");
            KERN(grad_f32, "uvck_grad_f32");
            KERN(scatter_f32, "uvck_scatter_f32");
            KERN(penalty_energy_block_f32, "uvck_penalty_energy_block_f32");
            KERN(penalty_grad_f32, "uvck_penalty_grad_f32");
            KERN(project_axes_f32, "uvck_project_axes_f32");
            KERN(project_grad_axes_f32, "uvck_project_grad_axes_f32");
            KERN(apply_fixed_f32, "uvck_apply_fixed_f32");
            KERN(zero_fixed_f32, "uvck_zero_fixed_f32");
            KERN(copy_f32, "uvck_copy_f32");
            KERN(dot_block_f32, "uvck_dot_block_f32");
            KERN(axpy_slot_f32, "uvck_axpy_slot_f32");
            KERN(scale_diag_slot_f32, "uvck_scale_diag_slot_f32");
            KERN(negate_dot_block_f32, "uvck_negate_dot_block_f32");
            KERN(cand_step_f32, "uvck_cand_step_f32");
            KERN(cand_trial0_f32, "uvck_cand_trial0_f32");
            KERN(max_abs_block_f32, "uvck_max_abs_block_f32");
            KERN(sy_ymy_block_f32, "uvck_sy_ymy_block_f32");
            KERN(max_flip_step_block_f32, "uvck_max_flip_step_block_f32");
            KERN(gather_border_f32, "uvck_gather_border_f32");
            KERN(energy_block_d, "uvck_energy_block_d");
            KERN(scatter_i32_d, "uvck_scatter_i32_d");
            KERN(penalty_energy_block_d, "uvck_penalty_energy_block_d");
            KERN(penalty_grad_d, "uvck_penalty_grad_d");
            KERN(project_axes_d, "uvck_project_axes_d");
            KERN(project_grad_axes_d, "uvck_project_grad_axes_d");
            KERN(apply_fixed_d, "uvck_apply_fixed_d");
            KERN(zero_fixed_d, "uvck_zero_fixed_d");
            KERN(copy_d, "uvck_copy_d");
            KERN(dot_block_d, "uvck_dot_block_d");
            KERN(axpy_slot_d, "uvck_axpy_slot_d");
            KERN(scale_diag_slot_d, "uvck_scale_diag_slot_d");
            KERN(negate_dot_block_d, "uvck_negate_dot_block_d");
            KERN(cand_step_d, "uvck_cand_step_d");
            KERN(cand_trial0_d, "uvck_cand_trial0_d");
            KERN(max_abs_block_d, "uvck_max_abs_block_d");
            KERN(sy_ymy_block_d, "uvck_sy_ymy_block_d");
            KERN(max_flip_step_block_d, "uvck_max_flip_step_block_d");
            KERN(gather_border_d, "uvck_gather_border_d");
#undef KERN
            CUcontext previous = nullptr; check(pop(&previous), "cuCtxPopCurrent"); pushed = false;
            ++gpu_counts[6];
        } catch(...) {
            if(pushed) { if(module && unload) unload(module); CUcontext previous = nullptr; if(pop) pop(&previous); }
            if(context && release) release(device);
            FreeLibrary(dll); dll = nullptr; throw;
        }
    }
    ~CudaRuntime() {
        if(context) {
            if(push(context) == CUDA_SUCCESS) {
                if(!poisoned) release_cache_in_context();
                if(module) unload(module);
                CUcontext previous = nullptr; pop(&previous);
            }
            release(device);
        }
        if(dll) FreeLibrary(dll);
    }
};
static CudaRuntime* gpu_runtime = nullptr;
static std::string gpu_ptx_path;

struct RecentTriangles {
    std::array<I,8> ids{{-1,-1,-1,-1,-1,-1,-1,-1}};
    void remember(I triangle) {
        auto at = std::find(ids.begin(), ids.end(), triangle);
        if(at == ids.end()) --at;
        std::move_backward(ids.begin(), at, at + 1);
        ids.front() = triangle;
    }
};

struct GpuSession {
    std::unique_lock<std::recursive_mutex> lock;
    CudaRuntime& driver;
    I n, nt;
    RecentTriangles invalid_triangles;
    CUdeviceptr uv = 0, triangles = 0, j = 0, inverse = 0, weights = 0, result = 0;
    CUdeviceptr adjacency_offsets = 0, adjacency_entries = 0, vertex_gradient = 0;
    void* staging_in = nullptr;
    void* staging_out = nullptr;
    CUstream stream = nullptr;
    bool pushed = false;

    static unsigned grid256(int count) noexcept {
        return count > 0 ? unsigned((count + 255) / 256) : 1u;
    }
    static unsigned reduce_grid(int count) noexcept {
        unsigned g = grid256(count);
        return g > unsigned(GPU_MAX_REDUCE_BLOCKS) ? unsigned(GPU_MAX_REDUCE_BLOCKS) : g;
    }

    GpuSession(const Input& input) : lock(gpu_mutex), driver(*gpu_runtime), n(input.n), nt(input.nt) {
        try {
            driver.check(driver.push(driver.context), "session context"); pushed = true;
            auto& c = driver.cache;
            const bool need_alloc = (!c.stream || c.cap_n < n || c.cap_nt < nt);
            if(need_alloc) {
                driver.release_cache_in_context();
                if(driver.poisoned) throw std::runtime_error("CUDA stream poisoned");
                std::size_t free_bytes = 0, total_bytes = 0;
                driver.check(driver.memory_info(&free_bytes, &total_bytes), "cuMemGetInfo");
                const std::size_t device_bytes =
                    std::size_t(nt) * (19*sizeof(double) + 16*sizeof(float) + 6*sizeof(int))
                    + std::size_t(n) * (50*sizeof(double) + 48*sizeof(float) + 16);
                if(device_bytes > free_bytes - free_bytes / 4)
                    throw std::runtime_error("Insufficient GPU memory headroom");
                MEMORYSTATUSEX host_memory{}; host_memory.dwLength = sizeof(host_memory);
                const std::size_t pinned_bytes = (11*nt + 2*n + 1 + std::max(4*nt, 2*n)) * sizeof(double) + 4096;
                if(GlobalMemoryStatusEx(&host_memory) && pinned_bytes > host_memory.ullAvailPhys / 4)
                    throw std::runtime_error("Insufficient host memory for GPU staging");
                driver.check(driver.stream_create(&c.stream, CU_STREAM_NON_BLOCKING), "cuStreamCreate");
                driver.check(driver.allocate(&c.uv, 2*n*sizeof(double)), "allocate UV");
                driver.check(driver.allocate(&c.triangles, 3*nt*sizeof(I)), "allocate triangles");
                driver.check(driver.allocate(&c.j, 4*nt*sizeof(double)), "allocate J");
                driver.check(driver.allocate(&c.inverse, 4*nt*sizeof(double)), "allocate inverse");
                driver.check(driver.allocate(&c.weights, nt*sizeof(double)), "allocate weights");
                driver.check(driver.allocate(&c.result, 4*nt*sizeof(double)), "allocate result");
                driver.check(driver.allocate(&c.adjacency_offsets, (n+1)*sizeof(I)), "allocate adj offsets");
                driver.check(driver.allocate(&c.adjacency_entries, 3*nt*sizeof(I)), "allocate adj entries");
                driver.check(driver.allocate(&c.vertex_gradient, 2*n*sizeof(double)), "allocate vertex grad");

                // Resident compact constants
                driver.check(driver.allocate(&c.tri_i32, 3*nt*sizeof(int)), "allocate tri_i32");
                driver.check(driver.allocate(&c.adj_off_i32, (n+1)*sizeof(int)), "allocate adj_off_i32");
                driver.check(driver.allocate(&c.adj_ent_i32, 3*nt*sizeof(int)), "allocate adj_ent_i32");
                driver.check(driver.allocate(&c.inv_f, 4*nt*sizeof(float)), "allocate inv_f");
                driver.check(driver.allocate(&c.weights_f, nt*sizeof(float)), "allocate weights_f");

                // Resident FP64 solver vectors
                driver.check(driver.allocate(&c.x_d, 2*n*sizeof(double)), "allocate x_d");
                driver.check(driver.allocate(&c.cand_d, 2*n*sizeof(double)), "allocate cand_d");
                driver.check(driver.allocate(&c.g_d, 2*n*sizeof(double)), "allocate g_d");
                driver.check(driver.allocate(&c.next_g_d, 2*n*sizeof(double)), "allocate next_g_d");
                driver.check(driver.allocate(&c.dir_d, 2*n*sizeof(double)), "allocate dir_d");
                driver.check(driver.allocate(&c.inv_diag_d, 2*n*sizeof(double)), "allocate inv_diag_d");
                driver.check(driver.allocate(&c.held_d, 2*n*sizeof(double)), "allocate held_d");
                for(int k = 0; k < GPU_HIST_LIMIT; ++k) {
                    driver.check(driver.allocate(&c.s_hist_d[k], 2*n*sizeof(double)), "allocate s_hist_d");
                    driver.check(driver.allocate(&c.y_hist_d[k], 2*n*sizeof(double)), "allocate y_hist_d");
                }

                // Resident FP32 solver vectors
                driver.check(driver.allocate(&c.j_f, 4*nt*sizeof(float)), "allocate j_f");
                driver.check(driver.allocate(&c.gu_f, 4*nt*sizeof(float)), "allocate gu_f");
                driver.check(driver.allocate(&c.x_f, 2*n*sizeof(float)), "allocate x_f");
                driver.check(driver.allocate(&c.cand_f, 2*n*sizeof(float)), "allocate cand_f");
                driver.check(driver.allocate(&c.g_f, 2*n*sizeof(float)), "allocate g_f");
                driver.check(driver.allocate(&c.next_g_f, 2*n*sizeof(float)), "allocate next_g_f");
                driver.check(driver.allocate(&c.dir_f, 2*n*sizeof(float)), "allocate dir_f");
                driver.check(driver.allocate(&c.inv_diag_f, 2*n*sizeof(float)), "allocate inv_diag_f");
                driver.check(driver.allocate(&c.held_f, 2*n*sizeof(float)), "allocate held_f");
                for(int k = 0; k < GPU_HIST_LIMIT; ++k) {
                    driver.check(driver.allocate(&c.s_hist_f[k], 2*n*sizeof(float)), "allocate s_hist_f");
                    driver.check(driver.allocate(&c.y_hist_f[k], 2*n*sizeof(float)), "allocate y_hist_f");
                }

                driver.check(driver.allocate(&c.fixed_u8, std::size_t(n)), "allocate fixed_u8");
                driver.check(driver.allocate(&c.block_buf0, GPU_MAX_REDUCE_BLOCKS*sizeof(double)), "allocate b0");
                driver.check(driver.allocate(&c.block_buf1, GPU_MAX_REDUCE_BLOCKS*sizeof(double)), "allocate b1");
                driver.check(driver.allocate(&c.block_buf2, GPU_MAX_REDUCE_BLOCKS*sizeof(double)), "allocate b2");
                driver.check(driver.allocate(&c.block_inv, GPU_MAX_REDUCE_BLOCKS*sizeof(int)), "allocate binv");
                driver.check(driver.allocate(&c.scalars, SC_COUNT*sizeof(double)), "allocate scalars");

                driver.check(driver.host_allocate(&c.staging_in, (11*nt+2*n+1)*sizeof(double), 0), "alloc staging_in");
                driver.check(driver.host_allocate(&c.staging_out, std::max(4*nt, 2*n)*sizeof(double), 0), "alloc staging_out");
                void* sc_host = nullptr;
                driver.check(driver.host_allocate(&sc_host, SC_COUNT*sizeof(double), 0), "alloc staging_scalars");
                c.staging_scalars = static_cast<double*>(sc_host);
                c.cap_n = n; c.cap_nt = nt;
                c.tri_valid = c.inv_valid = c.weights_valid = false;
            }
            uv = c.uv; triangles = c.triangles; j = c.j; inverse = c.inverse; weights = c.weights; result = c.result;
            adjacency_offsets = c.adjacency_offsets; adjacency_entries = c.adjacency_entries;
            vertex_gradient = c.vertex_gradient;
            staging_in = c.staging_in; staging_out = c.staging_out; stream = c.stream;

            const std::uint64_t h_tri = fnv64_bytes(input.tri, std::size_t(3*nt)*sizeof(I));
            const std::uint64_t h_inv = fnv64_bytes(input.inverse, std::size_t(4*nt)*sizeof(double));
            const std::uint64_t h_w = fnv64_bytes(input.weights, std::size_t(nt)*sizeof(double));
            const bool same_shape = (c.cached_n == n && c.cached_nt == nt);
            bool uploaded_any = false;
            if(!(same_shape && c.inv_valid && c.inv_hash == h_inv)) {
                std::memcpy(staging_in, input.inverse, 4*nt*sizeof(double));
                driver.check(driver.upload(inverse, staging_in, 4*nt*sizeof(double), stream), "upload inverse");
                int cnt = int(4*nt);
                void* a[] = {&cnt, &inverse, &c.inv_f};
                driver.check(driver.launch(driver.cast_d2f, grid256(cnt), 1, 1, 256, 1, 1, 0, stream, a, nullptr), "cast inv_f");
                gpu_counts[4] += 4*nt*sizeof(double);
                c.inv_hash = h_inv; c.inv_valid = true; uploaded_any = true;
            }
            if(!(same_shape && c.weights_valid && c.weights_hash == h_w)) {
                double* host_weights = static_cast<double*>(staging_in) + 4*nt;
                std::memcpy(host_weights, input.weights, nt*sizeof(double));
                driver.check(driver.upload(weights, host_weights, nt*sizeof(double), stream), "upload weights");
                int cnt = int(nt);
                void* a[] = {&cnt, &weights, &c.weights_f};
                driver.check(driver.launch(driver.cast_d2f, grid256(cnt), 1, 1, 256, 1, 1, 0, stream, a, nullptr), "cast weights_f");
                gpu_counts[4] += nt*sizeof(double);
                c.weights_hash = h_w; c.weights_valid = true; uploaded_any = true;
            }
            if(!(same_shape && c.tri_valid && c.tri_hash == h_tri)) {
                void* host_triangles = static_cast<double*>(staging_in) + 5*nt;
                std::memcpy(host_triangles, input.tri, 3*nt*sizeof(I));
                std::vector<I> offsets(n+1, 0), entries(3*nt);
                for(I t = 0; t < nt; ++t) for(I corner = 0; corner < 3; ++corner) ++offsets[input.tri[3*t+corner]+1];
                std::partial_sum(offsets.begin(), offsets.end(), offsets.begin());
                std::vector<I> cursor = offsets;
                for(I corner = 0; corner < 3; ++corner) for(I t = 0; t < nt; ++t)
                    entries[cursor[input.tri[3*t+corner]]++] = corner*nt + t;
                void* host_offsets = static_cast<double*>(staging_in) + 8*nt;
                void* host_entries = static_cast<double*>(staging_in) + 8*nt + n + 1;
                std::memcpy(host_offsets, offsets.data(), (n+1)*sizeof(I));
                std::memcpy(host_entries, entries.data(), 3*nt*sizeof(I));
                driver.check(driver.upload(triangles, host_triangles, 3*nt*sizeof(I), stream), "upload triangles");
                driver.check(driver.upload(adjacency_offsets, host_offsets, (n+1)*sizeof(I), stream), "upload adj offsets");
                driver.check(driver.upload(adjacency_entries, host_entries, 3*nt*sizeof(I), stream), "upload adj entries");
                int c_tri = int(3*nt), c_off = int(n+1);
                void* a1[] = {&c_tri, &triangles, &c.tri_i32};
                driver.check(driver.launch(driver.cast_i64_to_i32, grid256(c_tri), 1, 1, 256, 1, 1, 0, stream, a1, nullptr), "cast tri_i32");
                void* a2[] = {&c_off, &adjacency_offsets, &c.adj_off_i32};
                driver.check(driver.launch(driver.cast_i64_to_i32, grid256(c_off), 1, 1, 256, 1, 1, 0, stream, a2, nullptr), "cast off_i32");
                void* a3[] = {&c_tri, &adjacency_entries, &c.adj_ent_i32};
                driver.check(driver.launch(driver.cast_i64_to_i32, grid256(c_tri), 1, 1, 256, 1, 1, 0, stream, a3, nullptr), "cast ent_i32");
                gpu_counts[4] += (6*nt + n + 1)*sizeof(I);
                c.tri_hash = h_tri; c.tri_valid = true; uploaded_any = true;
            }
            c.cached_n = n; c.cached_nt = nt;
            if(uploaded_any) driver.check(driver.wait(stream), "constant upload wait");
            ++gpu_counts[0];
        } catch(...) { cleanup(true); throw; }
    }

    void ensure_set_buffers(I nsets, I nmembers) {
        auto& c = driver.cache;
        if(nsets > c.cap_sets) {
            driver.free_ptr(c.set_offsets); driver.free_ptr(c.set_axis);
            driver.free_ptr(c.set_anchored); driver.free_ptr(c.set_anchor);
            driver.check(driver.allocate(&c.set_offsets, (nsets+1)*sizeof(int)), "alloc set_offsets");
            driver.check(driver.allocate(&c.set_axis, nsets*sizeof(int)), "alloc set_axis");
            driver.check(driver.allocate(&c.set_anchored, std::size_t(nsets)), "alloc set_anchored");
            driver.check(driver.allocate(&c.set_anchor, nsets*sizeof(double)), "alloc set_anchor");
            c.cap_sets = nsets;
        }
        if(nmembers > c.cap_set_members) {
            driver.free_ptr(c.set_members);
            driver.check(driver.allocate(&c.set_members, nmembers*sizeof(int)), "alloc set_members");
            c.cap_set_members = nmembers;
        }
    }

    void ensure_penalty_buffers(I ne, I npv, I npv_edges) {
        auto& c = driver.cache;
        if(ne > c.cap_ne) {
            driver.free_ptr(c.pen_edges); driver.free_ptr(c.pen_axis);
            driver.free_ptr(c.pen_w_d); driver.free_ptr(c.pen_w_f);
            driver.check(driver.allocate(&c.pen_edges, 2*ne*sizeof(int)), "alloc pen_edges");
            driver.check(driver.allocate(&c.pen_axis, ne*sizeof(int)), "alloc pen_axis");
            driver.check(driver.allocate(&c.pen_w_d, ne*sizeof(double)), "alloc pen_w_d");
            driver.check(driver.allocate(&c.pen_w_f, ne*sizeof(float)), "alloc pen_w_f");
            c.cap_ne = ne;
        }
        if(npv > c.cap_pv) {
            driver.free_ptr(c.pv_ids); driver.free_ptr(c.pv_offsets);
            driver.check(driver.allocate(&c.pv_ids, npv*sizeof(int)), "alloc pv_ids");
            driver.check(driver.allocate(&c.pv_offsets, (npv+1)*sizeof(int)), "alloc pv_offsets");
            c.cap_pv = npv;
        }
        if(npv_edges > c.cap_pv_edges) {
            driver.free_ptr(c.pv_edges);
            driver.check(driver.allocate(&c.pv_edges, npv_edges*sizeof(int)), "alloc pv_edges");
            c.cap_pv_edges = npv_edges;
        }
    }

    void ensure_border_buffers(I nv_b) {
        auto& c = driver.cache;
        if(nv_b > c.cap_nbv) {
            driver.free_ptr(c.border_verts); driver.free_ptr(c.border_out);
            if(c.staging_border) { driver.host_free(c.staging_border); c.staging_border = nullptr; }
            driver.check(driver.allocate(&c.border_verts, nv_b*sizeof(int)), "alloc border_verts");
            driver.check(driver.allocate(&c.border_out, 2*nv_b*sizeof(double)), "alloc border_out");
            void* hb = nullptr;
            driver.check(driver.host_allocate(&hb, 2*nv_b*sizeof(double), 0), "alloc staging_border");
            c.staging_border = static_cast<double*>(hb);
            c.cap_nbv = nv_b;
        }
    }

    void cleanup(bool failed = false) noexcept {
        if(pushed) {
            if(stream) {
                if(driver.wait(stream) != CUDA_SUCCESS) {
                    driver.poisoned = true;
                    driver.cache = {};
                    CUcontext previous = nullptr; driver.pop(&previous); pushed = false; return;
                }
            }
            if(failed) driver.release_cache_in_context();
            CUcontext previous = nullptr; driver.pop(&previous); pushed = false;
        }
    }
    ~GpuSession() { cleanup(false); }

    double energy(const Vec& host_uv, bool reciprocal, bool prevent, double angle) {
        auto& c = driver.cache;
        std::memcpy(staging_in, host_uv.data(), 2*n*sizeof(double));
        driver.check(driver.upload(uv, staging_in, 2*n*sizeof(double), stream), "upload UV");
        int nti = int(nt), rec = reciprocal, flip = prevent;
        unsigned nb = reduce_grid(nti);
        void* args[] = {&nti, &uv, &c.tri_i32, &inverse, &j, &weights, &rec, &flip, &angle, &c.block_buf0, &c.block_inv};
        driver.check(driver.launch(driver.energy_block_d, nb, 1, 1, 256, 1, 1, 0, stream, args, nullptr), "energy_block_d");
        int nbi = int(nb);
        void* rargs[] = {&nbi, &c.block_buf0, &c.block_inv, &c.scalars};
        driver.check(driver.launch(driver.reduce_energy_final, 1, 1, 1, 256, 1, 1, 0, stream, rargs, nullptr), "reduce_energy");
        driver.check(driver.download(c.staging_scalars, c.scalars, 2*sizeof(double), stream), "download scalar");
        driver.check(driver.wait(stream), "energy wait");
        ++gpu_counts[1]; gpu_counts[4] += 2*n*sizeof(double); gpu_counts[5] += 2*sizeof(double);
        const double sum = c.staging_scalars[SC_ENERGY];
        if((reciprocal || prevent) && !std::isfinite(sum)) {
            const I inv_t = I(c.staging_scalars[SC_INVALID]);
            if(inv_t >= 0 && inv_t < nt) invalid_triangles.remember(inv_t);
        }
        return sum;
    }

    void gradient(bool reciprocal, bool prevent, double angle, Vec& grad) {
        auto& c = driver.cache;
        int rec = reciprocal, flip = prevent;
        void* args[] = {&nt, &j, &inverse, &weights, &rec, &flip, &angle, &result};
        driver.check(driver.launch(driver.gradient_kernel, unsigned((nt+127)/128), 1, 1, 128, 1, 1, 0, stream, args, nullptr), "grad launch");
        int ni = int(n), nti = int(nt);
        void* sargs[] = {&ni, &nti, &c.adj_off_i32, &c.adj_ent_i32, &result, &vertex_gradient};
        driver.check(driver.launch(driver.scatter_i32_d, grid256(ni), 1, 1, 256, 1, 1, 0, stream, sargs, nullptr), "scatter_d");
        driver.check(driver.download(staging_out, vertex_gradient, 2*n*sizeof(double), stream), "download grad");
        driver.check(driver.wait(stream), "gradient wait");
        std::memcpy(grad.data(), staging_out, 2*n*sizeof(double));
        ++gpu_counts[2]; gpu_counts[5] += 2*n*sizeof(double);
    }
};

static std::unique_ptr<GpuSession> make_gpu_session(const Input& input) {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!gpu_mode) return nullptr;
    try {
        if(!gpu_runtime || gpu_runtime->path != gpu_ptx_path) {
            auto next = std::make_unique<CudaRuntime>(gpu_ptx_path);
            delete gpu_runtime; gpu_runtime = next.release();
        }
        return std::make_unique<GpuSession>(input);
    } catch(const std::exception& e) { gpu_error = e.what(); ++gpu_counts[3]; if(gpu_mode == 1) throw; return nullptr; }
}
static void gpu_failure(const std::exception& error) {
    gpu_error = error.what(); ++gpu_counts[3];
    if(gpu_mode == 1) throw std::runtime_error(gpu_error);
}
API int uvck_gpu_configure(const char* path, I mode) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(mode < 0 || mode > 2) return -1;
    if(mode && gpu_runtime && gpu_runtime->poisoned) return -1;
    gpu_mode = int(mode); gpu_ptx_path = path ? path : ""; gpu_error.clear();
    return 0;
}
API int uvck_gpu_stats(I* output, I count, I reset) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!output || count != 7) return -1;
    std::copy(gpu_counts.begin(), gpu_counts.end(), output);
    if(reset) gpu_counts.fill(0);
    return 0;
}
API int uvck_gpu_error(char* output, I capacity) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!output || capacity < 1) return -1;
    auto n = std::min<std::size_t>(gpu_error.size(), std::size_t(capacity - 1));
    std::copy_n(gpu_error.data(), n, output); output[n] = 0; return 0;
}
API int uvck_gpu_shutdown() noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(gpu_runtime && !gpu_runtime->poisoned) { delete gpu_runtime; gpu_runtime = nullptr; }
    gpu_mode = 0; gpu_ptx_path.clear(); return 0;
}

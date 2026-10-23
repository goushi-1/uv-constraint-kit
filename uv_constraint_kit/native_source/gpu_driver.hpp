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

struct CudaRuntime {
    HMODULE dll = nullptr;
    CUdevice device = 0;
    CUcontext context = nullptr;
    CUmodule module = nullptr;
    CUfunction energy_kernel = nullptr, gradient_kernel = nullptr,scatter_kernel=nullptr;
    bool poisoned=false;
    std::string path;
    char name[256]{};
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
    CUDA_FN(host_allocate, cuMemHostAlloc); CUDA_FN(host_free, cuMemFreeHost);
    CUDA_FN(stream_create, cuStreamCreate); CUDA_FN(stream_destroy, cuStreamDestroy);
    CUDA_FN(launch, cuLaunchKernel); CUDA_FN(wait, cuStreamSynchronize);
    CUDA_FN(error_name, cuGetErrorName);
#undef CUDA_FN
    template<class T> void symbol(T& value, const char* name) {
        value = reinterpret_cast<T>(GetProcAddress(dll,name));
        if(!value) throw std::runtime_error(std::string("Missing CUDA driver symbol: ")+name);
    }
    void check(CUresult result, const char* action) const {
        if(result==CUDA_SUCCESS) return;
        const char* reason=nullptr;
        if(error_name) error_name(result,&reason);
        throw std::runtime_error(std::string(action)+": "+(reason?reason:std::to_string(result)));
    }
    explicit CudaRuntime(const std::string& ptx_path):path(ptx_path) {
        dll=LoadLibraryExW(L"nvcuda.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);
        if(!dll) throw std::runtime_error("NVIDIA CUDA driver unavailable");
        bool pushed=false;
        try {
#define LOAD(member,name) symbol(member,name)
            LOAD(init,"cuInit"); LOAD(device_get,"cuDeviceGet"); LOAD(device_name,"cuDeviceGetName");
            LOAD(device_count,"cuDeviceGetCount"); LOAD(device_attribute,"cuDeviceGetAttribute");
            LOAD(driver_version,"cuDriverGetVersion"); LOAD(memory_info,"cuMemGetInfo_v2");
            LOAD(retain,"cuDevicePrimaryCtxRetain"); LOAD(release,"cuDevicePrimaryCtxRelease_v2");
            LOAD(push,"cuCtxPushCurrent_v2"); LOAD(pop,"cuCtxPopCurrent_v2");
            LOAD(load,"cuModuleLoadData"); LOAD(unload,"cuModuleUnload"); LOAD(function,"cuModuleGetFunction");
            LOAD(allocate,"cuMemAlloc_v2"); LOAD(free_memory,"cuMemFree_v2");
            LOAD(upload,"cuMemcpyHtoDAsync_v2"); LOAD(download,"cuMemcpyDtoHAsync_v2");
            LOAD(host_allocate,"cuMemHostAlloc"); LOAD(host_free,"cuMemFreeHost");
            LOAD(stream_create,"cuStreamCreate"); LOAD(stream_destroy,"cuStreamDestroy_v2");
            LOAD(launch,"cuLaunchKernel"); LOAD(wait,"cuStreamSynchronize"); LOAD(error_name,"cuGetErrorName");
#undef LOAD
            std::ifstream file(std::filesystem::u8path(ptx_path),std::ios::binary);
            if(!file) throw std::runtime_error("GPU PTX file unavailable");
            std::string ptx((std::istreambuf_iterator<char>(file)),{});
            if(ptx.empty() || ptx.size()>10*1024*1024) throw std::runtime_error("Invalid GPU PTX size");
            check(init(0),"cuInit");
            int version=0,count=0;
            check(driver_version(&version),"cuDriverGetVersion");
            if(version<12040) throw std::runtime_error("CUDA driver lacks the required PTX version");
            check(device_count(&count),"cuDeviceGetCount");
            bool compatible=false;
            for(int index=0;index<count;++index) {
                CUdevice candidate;int major=0,minor=0;
                check(device_get(&candidate,index),"cuDeviceGet");
                check(device_attribute(&major,CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR,candidate),"compute major");
                check(device_attribute(&minor,CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR,candidate),"compute minor");
                if(major*10+minor>=89) {device=candidate;compatible=true;break;}
            }
            if(!compatible) throw std::runtime_error("No CUDA device supports compute_89 PTX");
            check(device_name(name,sizeof(name),device),"cuDeviceGetName");
            check(retain(&context,device),"cuDevicePrimaryCtxRetain");
            check(push(context),"cuCtxPushCurrent"); pushed=true;
            check(load(&module,ptx.c_str()),"cuModuleLoadData");
            check(function(&energy_kernel,module,"uvck_energy"),"energy kernel");
            check(function(&gradient_kernel,module,"uvck_gradient"),"gradient kernel");
            check(function(&scatter_kernel,module,"uvck_scatter"),"scatter kernel");
            CUcontext previous=nullptr;check(pop(&previous),"cuCtxPopCurrent");pushed=false;
            ++gpu_counts[6];
        } catch(...) {
            if(pushed) {if(module && unload) unload(module); CUcontext previous=nullptr;if(pop) pop(&previous);}
            if(context && release) release(device);
            FreeLibrary(dll);dll=nullptr;throw;
        }
    }
    ~CudaRuntime() {
        if(context) {
            if(push(context)==CUDA_SUCCESS) {
                if(module) unload(module);
                CUcontext previous=nullptr;pop(&previous);
            }
            release(device);
        }
        if(dll) FreeLibrary(dll);
    }
};
// No CUDA calls from a DLL static destructor/Windows loader lock. Normal
// unload uses uvck_gpu_shutdown; otherwise the OS reclaims this at exit.
static CudaRuntime* gpu_runtime=nullptr;
static std::string gpu_ptx_path;

struct GpuSession {
    std::unique_lock<std::recursive_mutex> lock;
    CudaRuntime& driver;
    I n,nt;
    CUdeviceptr uv=0,triangles=0,j=0,inverse=0,weights=0,result=0;
    CUdeviceptr adjacency_offsets=0,adjacency_entries=0,vertex_gradient=0;
    void* staging_in=nullptr;
    void* staging_out=nullptr;
    CUstream stream=nullptr;
    bool pushed=false;
    GpuSession(const Input& input):lock(gpu_mutex),driver(*gpu_runtime),n(input.n),nt(input.nt) {
        try {
            driver.check(driver.push(driver.context),"session context");pushed=true;
            std::size_t free_bytes=0,total_bytes=0;
            driver.check(driver.memory_info(&free_bytes,&total_bytes),"cuMemGetInfo");
            const std::size_t device_bytes=(19*nt+5*n+1)*sizeof(double);
            if(device_bytes>free_bytes-free_bytes/4) throw std::runtime_error("Insufficient GPU memory headroom");
            MEMORYSTATUSEX host_memory{};host_memory.dwLength=sizeof(host_memory);
            const std::size_t pinned_bytes=(11*nt+2*n+1+std::max(4*nt,2*n))*sizeof(double);
            if(GlobalMemoryStatusEx(&host_memory) && pinned_bytes>host_memory.ullAvailPhys/4)
                throw std::runtime_error("Insufficient host memory for GPU staging");
            driver.check(driver.stream_create(&stream,CU_STREAM_NON_BLOCKING),"cuStreamCreate");
            driver.check(driver.allocate(&uv,2*n*sizeof(double)),"allocate UV");
            driver.check(driver.allocate(&triangles,3*nt*sizeof(I)),"allocate triangles");
            driver.check(driver.allocate(&j,4*nt*sizeof(double)),"allocate J");
            driver.check(driver.allocate(&inverse,4*nt*sizeof(double)),"allocate inverse");
            driver.check(driver.allocate(&weights,nt*sizeof(double)),"allocate weights");
            driver.check(driver.allocate(&result,4*nt*sizeof(double)),"allocate result");
            driver.check(driver.allocate(&adjacency_offsets,(n+1)*sizeof(I)),"allocate adjacency offsets");
            driver.check(driver.allocate(&adjacency_entries,3*nt*sizeof(I)),"allocate adjacency entries");
            driver.check(driver.allocate(&vertex_gradient,2*n*sizeof(double)),"allocate vertex gradient");
            driver.check(driver.host_allocate(&staging_in,(11*nt+2*n+1)*sizeof(double),0),"allocate upload staging");
            driver.check(driver.host_allocate(&staging_out,std::max(4*nt,2*n)*sizeof(double),0),"allocate download staging");
            std::memcpy(staging_in,input.inverse,4*nt*sizeof(double));
            double* host_weights=static_cast<double*>(staging_in)+4*nt;
            std::memcpy(host_weights,input.weights,nt*sizeof(double));
            void* host_triangles=static_cast<double*>(staging_in)+5*nt;
            std::memcpy(host_triangles,input.tri,3*nt*sizeof(I));
            std::vector<I> offsets(n+1,0),entries(3*nt);
            for(I t=0;t<nt;++t) for(I corner=0;corner<3;++corner) ++offsets[input.tri[3*t+corner]+1];
            std::partial_sum(offsets.begin(),offsets.end(),offsets.begin());
            std::vector<I> cursor=offsets;
            for(I corner=0;corner<3;++corner) for(I t=0;t<nt;++t)
                entries[cursor[input.tri[3*t+corner]]++]=corner*nt+t;
            void* host_offsets=static_cast<double*>(staging_in)+8*nt;
            void* host_entries=static_cast<double*>(staging_in)+8*nt+n+1;
            std::memcpy(host_offsets,offsets.data(),(n+1)*sizeof(I));
            std::memcpy(host_entries,entries.data(),3*nt*sizeof(I));
            driver.check(driver.upload(inverse,staging_in,4*nt*sizeof(double),stream),"upload inverse");
            driver.check(driver.upload(weights,host_weights,nt*sizeof(double),stream),"upload weights");
            driver.check(driver.upload(triangles,host_triangles,3*nt*sizeof(I),stream),"upload triangles");
            driver.check(driver.upload(adjacency_offsets,host_offsets,(n+1)*sizeof(I),stream),"upload adjacency offsets");
            driver.check(driver.upload(adjacency_entries,host_entries,3*nt*sizeof(I),stream),"upload adjacency entries");
            driver.check(driver.wait(stream),"constant upload wait");
            gpu_counts[4]+=(11*nt+n+1)*sizeof(double);++gpu_counts[0];
        } catch(...) {cleanup();throw;}
    }
    void cleanup() noexcept {
        if(pushed) {
            // Error paths may still have work queued. Keep pinned staging
            // alive until our stream has been drained (best effort on errors).
            if(stream) {
                if(driver.wait(stream)!=CUDA_SUCCESS) {
                    // A fatal stream error cannot prove that DMA is finished.
                    // Quarantine this session's allocations until process exit
                    // instead of releasing host memory that may still be used.
                    driver.poisoned=true;
                    CUcontext previous=nullptr;driver.pop(&previous);pushed=false;return;
                }
                driver.stream_destroy(stream);
            }
            for(CUdeviceptr p:{uv,triangles,j,inverse,weights,result,adjacency_offsets,adjacency_entries,vertex_gradient})
                if(p) driver.free_memory(p);
            if(staging_in) driver.host_free(staging_in);
            if(staging_out) driver.host_free(staging_out);
            CUcontext previous=nullptr;driver.pop(&previous);pushed=false;
        }
    }
    ~GpuSession(){cleanup();}
    void energy(const Vec& host_uv,bool reciprocal,bool prevent,double angle,Vec& terms) {
        // Copies and kernels share one stream. A pageable synchronous copy on
        // the default stream need not finish DMA before a nonblocking kernel.
        std::memcpy(staging_in,host_uv.data(),2*n*sizeof(double));
        driver.check(driver.upload(uv,staging_in,2*n*sizeof(double),stream),"upload UV");
        int rec=reciprocal,flip=prevent;
        void* args[]={&nt,&uv,&triangles,&inverse,&j,&weights,&rec,&flip,&angle,&result};
        driver.check(driver.launch(driver.energy_kernel,unsigned((nt+127)/128),1,1,128,1,1,0,stream,args,nullptr),"energy launch");
        driver.check(driver.download(staging_out,result,nt*sizeof(double),stream),"download terms");
        driver.check(driver.wait(stream),"energy wait");
        std::memcpy(terms.data(),staging_out,nt*sizeof(double));
        ++gpu_counts[1];gpu_counts[4]+=2*n*sizeof(double);gpu_counts[5]+=nt*sizeof(double);
    }
    void gradient(bool reciprocal,bool prevent,double angle,Vec& gradient) {
        int rec=reciprocal,flip=prevent;
        void* args[]={&nt,&j,&inverse,&weights,&rec,&flip,&angle,&result};
        driver.check(driver.launch(driver.gradient_kernel,unsigned((nt+127)/128),1,1,128,1,1,0,stream,args,nullptr),"gradient launch");
        void* scatter_args[]={&n,&nt,&adjacency_offsets,&adjacency_entries,&result,&vertex_gradient};
        driver.check(driver.launch(driver.scatter_kernel,unsigned((n+127)/128),1,1,128,1,1,0,stream,scatter_args,nullptr),"scatter launch");
        driver.check(driver.download(staging_out,vertex_gradient,2*n*sizeof(double),stream),"download vertex gradient");
        driver.check(driver.wait(stream),"gradient wait");
        std::memcpy(gradient.data(),staging_out,2*n*sizeof(double));
        ++gpu_counts[2];gpu_counts[5]+=2*n*sizeof(double);
    }
};
static std::unique_ptr<GpuSession> make_gpu_session(const Input& input) {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!gpu_mode) return nullptr;
    try {
        if(!gpu_runtime || gpu_runtime->path!=gpu_ptx_path) {
            auto next=std::make_unique<CudaRuntime>(gpu_ptx_path);
            delete gpu_runtime;gpu_runtime=next.release();
        }
        return std::make_unique<GpuSession>(input);
    } catch(const std::exception& e) {gpu_error=e.what();++gpu_counts[3];if(gpu_mode==1) throw;return nullptr;}
}
static void gpu_failure(const std::exception& error) {
    gpu_error=error.what();++gpu_counts[3];
    if(gpu_mode==1) throw std::runtime_error(gpu_error);
}
API int uvck_gpu_configure(const char* path,I mode) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(mode<0 || mode>2) return -1;
    if(mode && gpu_runtime && gpu_runtime->poisoned) return -1;
    gpu_mode=int(mode);gpu_ptx_path=path?path:"";gpu_error.clear();
    return 0;
}
API int uvck_gpu_stats(I* output,I count,I reset) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!output || count!=7) return -1;
    std::copy(gpu_counts.begin(),gpu_counts.end(),output);
    if(reset) gpu_counts.fill(0);
    return 0;
}
API int uvck_gpu_error(char* output,I capacity) noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    if(!output || capacity<1) return -1;
    auto n=std::min<std::size_t>(gpu_error.size(),std::size_t(capacity-1));
    std::copy_n(gpu_error.data(),n,output);output[n]=0;return 0;
}
API int uvck_gpu_shutdown() noexcept {
    std::lock_guard<std::recursive_mutex> guard(gpu_mutex);
    // A fatal stream error leaves completion uncertain. Keep the CUDA module,
    // driver handle and primary-context reference resident with its quarantined
    // allocations; only the OS may reclaim them at process exit.
    if(gpu_runtime && !gpu_runtime->poisoned) {delete gpu_runtime;gpu_runtime=nullptr;}
    gpu_mode=0;gpu_ptx_path.clear();return 0;
}

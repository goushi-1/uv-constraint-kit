// SPDX-License-Identifier: GPL-3.0-or-later
// Standalone offline prototype, not linked to the released solve engine.
#include <algorithm>
#include <cfloat>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <vector>

#ifdef _WIN32
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C" __attribute__((visibility("default")))
#endif
using I=std::int64_t;

// An independent compensated accumulator; exact agreement is checked against
// this host's Python sum, rather than assumed from the accumulator's formula.
struct Sum {
    double hi=0.,lo=0.;
    void add(double x) {
        const double t=hi+x;
        lo += std::abs(hi)>=std::abs(x) ? (hi-t)+x : (x-t)+hi;
        hi=t;
    }
    double value() const { return hi+lo; }
};

API int uvck_numeric_stage_abi() { return 1; }

// 0: scaled rows; 1: original no-op; 2: defer to the untouched Python path.
// Output arguments are untouched on 2. No input memory is ever modified.
API int uvck_numeric_area_scale(I n,I nf,I nm,const double* xy,
    const I* offsets,const I* members,double target,double* output,double* stats) {
    try {
        if(n<0 || nf<0 || nm<0 || !xy || !offsets || !members || !output || !stats
            || std::fegetround()!=FE_TONEAREST || !std::isfinite(target)) return 2;
        if(offsets[0]!=0 || offsets[nf]!=nm) return 2;
        for(I f=0;f<nf;++f)
            if(offsets[f]<0 || offsets[f+1]<offsets[f] || offsets[f+1]>nm) return 2;
        std::vector<I> unique;
        unique.reserve(static_cast<std::size_t>(nm));
        for(I k=0;k<nm;++k) {
            const I p=members[k];
            if(p<0 || p>=n) return 2;
            unique.push_back(p);
            for(I axis=0;axis<2;++axis) {
                const double x=xy[2*p+axis];
                if(!std::isfinite(x) || std::abs(x)>FLT_MAX) return 2;
                const float rounded=static_cast<float>(x);
                if(static_cast<double>(rounded)!=x) return 2;
            }
        }
        std::sort(unique.begin(),unique.end());
        if(std::adjacent_find(unique.begin(),unique.end())!=unique.end()) return 2;

        double area=0.,factor=1.,cu=0.,cv=0.;
        bool scaled=false;
        if(target>0.) {
            // Finite float32 coordinates imply nonzero polygon areas are on
            // a binary lattice; this conservative target domain also keeps
            // division away from double overflow/underflow. Other inputs defer.
            if(target<0x1p-300 || target>0x1p400) return 2;
            for(I f=0;f<nf;++f) {
                const I start=offsets[f],end=offsets[f+1];
                double acc=0.;
                for(I k=start;k<end;++k) {
                    const I p=members[k],q=members[k+1==end ? start : k+1];
                    const double left=xy[2*p]*xy[2*q+1];
                    const double right=xy[2*q]*xy[2*p+1];
                    const double difference=left-right;
                    acc=acc+difference;
                }
                area=area+std::abs(acc)*0.5;
            }
            if(area>0.) {
                const double ratio=target/area;
                const double proposed=std::sqrt(ratio);
                if(proposed>1e-3 && proposed<1e3 && std::abs(proposed-1.)>=1e-6 && nm>0) {
                    factor=proposed;
                    Sum u,v;
                    for(I k=0;k<nm;++k) { u.add(xy[2*members[k]]);v.add(xy[2*members[k]+1]); }
                    cu=u.value()/static_cast<double>(nm);
                    cv=v.value()/static_cast<double>(nm);
                    scaled=true;
                }
            }
        }
        std::vector<double> result(static_cast<std::size_t>(nm)*2);
        for(I k=0;k<nm;++k) {
            for(I axis=0;axis<2;++axis) {
                const double x=xy[2*members[k]+axis];
                if(!scaled) { result[2*k+axis]=x;continue; }
                const double centre=axis==0 ? cu : cv;
                const double delta=x-centre;
                const double product=delta*factor;
                const double value=centre+product;
                if(!std::isfinite(value) || std::abs(value)>FLT_MAX
                    || (value!=0. && std::abs(value)<FLT_MIN)) return 2;
                result[2*k+axis]=static_cast<double>(static_cast<float>(value));
            }
        }
        std::copy(result.begin(),result.end(),output);
        stats[0]=area;stats[1]=factor;stats[2]=cu;stats[3]=cv;
        return scaled ? 0 : 1;
    } catch(...) { return 2; }
}

// Read-only offline area experiment. Caller supplies faces in sorted order and
// each face's loops in original order. Return 2 without touching out whenever
// the narrow input/rounding contract is not met. Repeated loop indices are
// deliberately allowed: the original polygon sum defines their behaviour.
//
// Finite binary32 numbers are integer multiples of 2^-149 and have magnitude
// below 2^128. Their binary64 products are exact multiples of 2^-298 and have
// magnitude below 2^256. With at most 2^31 terms, the serial area accumulator
// stays far below binary64 overflow, while a nonzero half-area stays at least
// 2^-299, far above binary64 subnormal range. This does not prove equivalence
// under FTZ/DAZ, exception traps or other floating environments; those require
// separate host checks and conservative dispatch.
API int uvck_numeric_area_read(I n,I nf,I nm,const double* xy,
                               const I* offsets,const I* members,double* out) {
    try {
        constexpr I limit=I(1)<<31;
        if(n<0 || nf<0 || nm<0 || n>limit || nf>limit || nm>limit ||
           !xy || !offsets || !members || !out || std::fegetround()!=FE_TONEAREST)
            return 2;
        // Reject a raw ABI caller's output alias, so even successful calls do
        // not overwrite input coordinates or metadata. Python uses fresh out.
        auto overlaps_out=[&](const void* ptr,std::uint64_t bytes) {
            const std::uintptr_t begin=reinterpret_cast<std::uintptr_t>(ptr);
            const std::uintptr_t dest=reinterpret_cast<std::uintptr_t>(out);
            if(bytes>UINTPTR_MAX-begin || sizeof(double)>UINTPTR_MAX-dest) return true;
            return begin<dest+sizeof(double) && dest<begin+bytes;
        };
        if(overlaps_out(xy,static_cast<std::uint64_t>(n)*2*sizeof(double)) ||
           overlaps_out(offsets,static_cast<std::uint64_t>(nf+1)*sizeof(I)) ||
           overlaps_out(members,static_cast<std::uint64_t>(nm)*sizeof(I))) return 2;
        if(offsets[0]!=0 || offsets[nf]!=nm) return 2;
        for(I f=0;f<nf;++f)
            if(offsets[f]<0 || offsets[f+1]<offsets[f] || offsets[f+1]>nm) return 2;
        for(I k=0;k<nm;++k) {
            const I p=members[k];
            if(p<0 || p>=n) return 2;
            for(int axis=0;axis<2;++axis) {
                const double x=xy[2*p+axis];
                if(!std::isfinite(x) || std::abs(x)>FLT_MAX) return 2;
                if(static_cast<double>(static_cast<float>(x))!=x) return 2;
            }
        }
        double total=0.;
        for(I f=0;f<nf;++f) {
            const I start=offsets[f],end=offsets[f+1];
            double acc=0.;
            for(I k=start;k<end;++k) {
                const I p=members[k],q=members[k+1==end ? start : k+1];
                const double left=xy[2*p]*xy[2*q+1];
                const double right=xy[2*q]*xy[2*p+1];
                const double difference=left-right;
                acc=acc+difference;
            }
            total=total+std::abs(acc)*0.5;
        }
        *out=total;
        return 0;
    } catch(...) { return 2; }
}

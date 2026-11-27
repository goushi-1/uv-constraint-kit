// SPDX-License-Identifier: GPL-3.0-or-later
// UV Constraint Kit CPU engine. No Blender/Python objects cross this C ABI.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <numeric>
#include <stdexcept>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#ifdef _M_X64
#include <xmmintrin.h>
#endif

#ifdef _WIN32
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C" __attribute__((visibility("default")))
#endif
using I = std::int64_t;
using Vec = std::vector<double>;
constexpr double INF = std::numeric_limits<double>::infinity();
struct Input {
    I n, nt, nr, nm, ne, nb;
    const double *uv, *inverse, *weights;
    const I *tri, *kinds, *offsets, *members;
    const std::uint8_t *fixed;
    const double *held;
    const I *penalty_axis, *penalty_edges;
    const double *penalty_weights;
    const I *border_edges;
    double density, angle_length, strength, penalty_scale, overlap_space;
    I iterations, automatic, prevent_flips, prevent_overlap;
};
struct Output { I accepted, evaluations, reciprocal, finite; double energy, gradient_max; };
#include "gpu_driver.hpp"
static double dot(const Vec& a, const Vec& b) {
    double r=0; for(std::size_t i=0;i<a.size();++i) r+=a[i]*b[i]; return r;
}

template<bool subtract>
static double update_and_dot(Vec& direction,const Vec& update,double scale,const Vec& weights) {
    double sum=0;
    for(std::size_t i=0;i<direction.size();++i) {
        const double change=scale*update[i];
        const double value=subtract ? direction[i]-change : direction[i]+change;
        direction[i]=value;
        sum+=weights[i]*value;
    }
    return sum;
}
static double max_abs(const Vec& a) {
    double r=0; for(double x:a) r=std::max(r,std::abs(x)); return r;
}
struct AxisSet { I axis; std::vector<I> members; bool anchored=false; double anchor=0; };
struct Problem {
    const Input& p;
    bool soft, axes_only=true, reciprocal=false;
    std::vector<AxisSet> sets;
    Vec jacobian;
    std::unique_ptr<GpuSession> gpu;
    I evaluations=0;
    mutable I witness_i=-1,witness_j=-1;
    mutable bool border_rejected=false,flip_rejected=false;
    const Vec* baseline_uv=nullptr;
    double root_penalty_scale=1.;
    struct BorderBound {double low,high,ylow,yhigh;I index;};
    mutable std::vector<BorderBound> border_bounds;
    explicit Problem(const Input& in):p(in),soft(in.strength>=0),jacobian(4*in.nt),
            root_penalty_scale(std::sqrt(std::max(in.penalty_scale,1e-20))) {
        for(I r=0;r<p.nr;++r) if(p.kinds[r]!=1 && p.kinds[r]!=2) axes_only=false;
        if(axes_only) for(I kind=1;kind<=2;++kind) {
            std::vector<I> parent(p.n,-1), order;
            auto root=[&](I i) { while(parent[i]!=i) {parent[i]=parent[parent[i]]; i=parent[i];} return i; };
            for(I r=0;r<p.nr;++r) if(p.kinds[r]==kind && p.offsets[r]<p.offsets[r+1]) {
                for(I k=p.offsets[r];k<p.offsets[r+1];++k) {
                    I i=p.members[k]; if(parent[i]<0) {parent[i]=i;order.push_back(i);}
                }
                I first=root(p.members[p.offsets[r]]);
                for(I k=p.offsets[r]+1;k<p.offsets[r+1];++k) parent[root(p.members[k])]=first;
            }
            std::map<I,std::size_t> locations;
            for(I i:order) {
                I key=root(i);
                if(!locations.count(key)) {locations[key]=sets.size(); sets.push_back({kind==1?1:0,{}});}
                sets[locations[key]].members.push_back(i);
            }
        }
        for(auto& set:sets) for(I i:set.members) if(p.fixed[i]) {
            set.anchored=true;set.anchor=p.held[2*i+set.axis];break;
        }
    }
    void init_gpu() {gpu=make_gpu_session(p);}
    void project_hard_axes(Vec& x) const {
        for(const auto& set:sets) {
            double mean=set.anchor;
            if(!set.anchored) {mean=0;for(I i:set.members) mean+=x[2*i+set.axis];mean/=set.members.size();}
            for(I i:set.members) x[2*i+set.axis]=mean;
        }
        for(I i=0;i<p.n;++i) if(p.fixed[i]) {x[2*i]=p.held[2*i];x[2*i+1]=p.held[2*i+1];}
    }
    bool hard_subspace_ready(const Vec& x) {
        Vec trial(x.size());
        for(std::size_t k=0;k<x.size();++k) trial[k]=static_cast<double>(static_cast<float>(x[k]));
        project_hard_axes(trial);
        for(I t=0;t<p.nt;++t) {
            I a=p.tri[3*t],b=p.tri[3*t+1],c=p.tri[3*t+2];const double* v=&p.inverse[4*t];
            double ux=trial[2*b]-trial[2*a],wx=trial[2*c]-trial[2*a];
            double uy=trial[2*b+1]-trial[2*a+1],wy=trial[2*c+1]-trial[2*a+1];
            double j0=ux*v[0]+wx*v[2],j1=ux*v[1]+wx*v[3];
            double j2=uy*v[0]+wy*v[2],j3=uy*v[1]+wy*v[3];
            double d=j0*j3-j1*j2;
            if(!std::isfinite(j0) || !std::isfinite(j1) || !std::isfinite(j2) || !std::isfinite(j3) ||
                    !std::isfinite(d) || d<=1e-6)
                return false;
        }
        I saved_wi=witness_i,saved_wj=witness_j;
        bool saved_br=border_rejected;
        const Vec* saved_base=baseline_uv;
        baseline_uv=nullptr;
        bool ok=borders_valid(trial);
        baseline_uv=saved_base;
        witness_i=saved_wi;witness_j=saved_wj;border_rejected=saved_br;
        return ok;
    }
    double max_flip_free_step(const Vec& x,const Vec& projected) const {
        double t_min=1.;
        for(I t=0;t<p.nt;++t) {
            I a=p.tri[3*t],b=p.tri[3*t+1],c=p.tri[3*t+2];
            double u0x=x[2*b]-x[2*a],u0y=x[2*b+1]-x[2*a+1];
            double v0x=x[2*c]-x[2*a],v0y=x[2*c+1]-x[2*a+1];
            double A0=u0x*v0y-u0y*v0x;
            if(!std::isfinite(A0) || A0==0.) continue;
            double dux=(projected[2*b]-x[2*b])-(projected[2*a]-x[2*a]);
            double duy=(projected[2*b+1]-x[2*b+1])-(projected[2*a+1]-x[2*a+1]);
            double dvx=(projected[2*c]-x[2*c])-(projected[2*a]-x[2*a]);
            double dvy=(projected[2*c+1]-x[2*c+1])-(projected[2*a+1]-x[2*a+1]);
            double s=(A0>=0.)?1.:-1.;
            double c0=s*A0;
            double c1=s*(dux*v0y-duy*v0x+u0x*dvy-u0y*dvx);
            double c2=s*(dux*dvy-duy*dvx);
            double t_star=INF;
            if(std::abs(c2)<=1e-20*(std::abs(c1)+c0)) {
                if(c1<0.) t_star=-c0/c1;
            } else if(c2<0.) {
                double disc=std::max(0.,c1*c1-4.*c2*c0);
                double q=std::sqrt(disc);
                t_star=(c1>=0.)?(c1+q)/(-2.*c2):(2.*c0)/(-c1+q);
            } else {
                double disc=c1*c1-4.*c2*c0;
                if(c1<0. && disc>=0.) t_star=(2.*c0)/(-c1+std::sqrt(disc));
            }
            if(t_star>0. && std::isfinite(t_star)) t_min=std::min(t_min,t_star);
        }
        return t_min;
    }
    void project(Vec& x) const {
        if(!soft) {
            if(axes_only) {
                project_hard_axes(x);
                return;
            }
            else {
                for(int pass=0;pass<32;++pass) {
                    Vec before=x;
                    for(I r=0;r<p.nr;++r) {
                        I a=p.offsets[r],b=p.offsets[r+1]; if(b-a<2) continue;
                        double cx=0,cy=0;for(I k=a;k<b;++k) {I i=p.members[k];cx+=x[2*i];cy+=x[2*i+1];}
                        cx/=(b-a);cy/=(b-a);
                        double xx=0,yy=0,xy=0;
                        for(I k=a;k<b;++k) {I i=p.members[k];double u=x[2*i]-cx,v=x[2*i+1]-cy;xx+=u*u;yy+=v*v;xy+=u*v;}
                        if(xx+yy<1e-20) continue;
                        double theta=.5*std::atan2(2*xy,xx-yy),dx=std::cos(theta),dy=std::sin(theta);
                        for(I k=a;k<b;++k) {
                            I i=p.members[k];
                            if(p.kinds[r]==1) x[2*i+1]=cy;
                            else if(p.kinds[r]==2) x[2*i]=cx;
                            else {double along=(x[2*i]-cx)*dx+(x[2*i+1]-cy)*dy;x[2*i]=cx+along*dx;x[2*i+1]=cy+along*dy;}
                        }
                    }
                    double change=0;for(std::size_t i=0;i<x.size();++i) change=std::max(change,std::abs(x[i]-before[i]));
                    if(change<=1e-13) break;
                }
                for(I r=0;r<p.nr;++r) if(p.kinds[r]==1 || p.kinds[r]==2) {
                    I axis=p.kinds[r]==1?1:0;
                    for(I k=p.offsets[r];k<p.offsets[r+1];++k) if(p.fixed[p.members[k]]) {
                        double anchor=p.held[2*p.members[k]+axis];
                        for(I j=p.offsets[r];j<p.offsets[r+1];++j) x[2*p.members[j]+axis]=anchor;
                        break;
                    }
                }
            }
        }
        for(I i=0;i<p.n;++i) if(p.fixed[i]) {x[2*i]=p.held[2*i];x[2*i+1]=p.held[2*i+1];}
    }
    static double cross(double ax,double ay,double bx,double by) {return ax*by-ay*bx;}
    static double distance(const double* q,const double* a,const double* b) {
        double vx=b[0]-a[0],vy=b[1]-a[1];
        double t=std::clamp(((q[0]-a[0])*vx+(q[1]-a[1])*vy)/std::max(vx*vx+vy*vy,1e-20),0.,1.);
        return std::hypot(q[0]-a[0]-t*vx,q[1]-a[1]-t*vy);
    }
    bool border_pair_valid(const Vec& x,I i,I j) const {
            I ia=p.border_edges[2*i],ib=p.border_edges[2*i+1],ic=p.border_edges[2*j],id=p.border_edges[2*j+1];
            if(ia==ic || ia==id || ib==ic || ib==id) return true;
            const double *a=&x[2*ia],*b=&x[2*ib],*c=&x[2*ic],*d=&x[2*id];
            const bool margin_guard=(p.prevent_overlap==1 && baseline_uv!=nullptr && p.overlap_space<=0.);
            double coord_scale=1.,local_margin=0.;
            if(margin_guard) {
                coord_scale=std::max({1.0,root_penalty_scale,
                                      std::abs(a[0]),std::abs(a[1]),std::abs(b[0]),std::abs(b[1]),
                                      std::abs(c[0]),std::abs(c[1]),std::abs(d[0]),std::abs(d[1])});
                local_margin=6e-7*coord_scale;
            }
            double margin=margin_guard?std::max({p.overlap_space,0.,local_margin}):std::max(p.overlap_space,0.);
            if(std::max(a[0],b[0])+margin<std::min(c[0],d[0]) || std::max(c[0],d[0])+margin<std::min(a[0],b[0]) ||
               std::max(a[1],b[1])+margin<std::min(c[1],d[1]) || std::max(c[1],d[1])+margin<std::min(a[1],b[1])) return true;
            double abx=b[0]-a[0],aby=b[1]-a[1],cdx=d[0]-c[0],cdy=d[1]-c[1];
            if(cross(abx,aby,c[0]-a[0],c[1]-a[1])*cross(abx,aby,d[0]-a[0],d[1]-a[1])<0 &&
               cross(cdx,cdy,a[0]-c[0],a[1]-c[1])*cross(cdx,cdy,b[0]-c[0],b[1]-c[1])<0) return false;
            if(p.prevent_overlap==1) {
                const double af[2]={static_cast<double>(static_cast<float>(a[0])),static_cast<double>(static_cast<float>(a[1]))};
                const double bf[2]={static_cast<double>(static_cast<float>(b[0])),static_cast<double>(static_cast<float>(b[1]))};
                const double cf[2]={static_cast<double>(static_cast<float>(c[0])),static_cast<double>(static_cast<float>(c[1]))};
                const double df[2]={static_cast<double>(static_cast<float>(d[0])),static_cast<double>(static_cast<float>(d[1]))};
                const double abfx=bf[0]-af[0],abfy=bf[1]-af[1],cdfx=df[0]-cf[0],cdfy=df[1]-cf[1];
                if(cross(abfx,abfy,cf[0]-af[0],cf[1]-af[1])*cross(abfx,abfy,df[0]-af[0],df[1]-af[1])<0 &&
                   cross(cdfx,cdfy,af[0]-cf[0],af[1]-cf[1])*cross(cdfx,cdfy,bf[0]-cf[0],bf[1]-cf[1])<0) return false;
            }
            if(margin_guard) {
                const double d_cand=std::min({distance(a,c,d),distance(b,c,d),distance(c,a,b),distance(d,a,b)});
                if(d_cand<local_margin) {
                    const Vec& base=*baseline_uv;
                    const double *ba=&base[2*ia],*bb=&base[2*ib],*bc=&base[2*ic],*bd=&base[2*id];
                    const double d_curr=std::min({distance(ba,bc,bd),distance(bb,bc,bd),distance(bc,ba,bb),distance(bd,ba,bb)});
                    if(d_cand<std::min(local_margin,0.5*d_curr)) return false;
                }
            }
            if(p.overlap_space>0 && std::min({distance(a,c,d),distance(b,c,d),distance(c,a,b),distance(d,a,b)})<p.overlap_space) return false;
            return true;
    }
    double max_border_pair_step(const Vec& x0,const Vec& x1,I i,I j) const {
        if(i<0 || j<0 || i>=p.nb || j>=p.nb) return 1.;
        const I ia=p.border_edges[2*i],ib=p.border_edges[2*i+1],ic=p.border_edges[2*j],id=p.border_edges[2*j+1];
        if(ia==ic || ia==id || ib==ic || ib==id) return 1.;
        double best=1.;
        auto check_pt_seg=[&](I ip,I iq,I ir) {
            const double p0x=x0[2*ip],p0y=x0[2*ip+1],dpx=x1[2*ip]-p0x,dpy=x1[2*ip+1]-p0y;
            const double q0x=x0[2*iq],q0y=x0[2*iq+1],dqx=x1[2*iq]-q0x,dqy=x1[2*iq+1]-q0y;
            const double r0x=x0[2*ir],r0y=x0[2*ir+1],drx=x1[2*ir]-r0x,dry=x1[2*ir+1]-r0y;
            const double ux=r0x-q0x,uy=r0y-q0y,dux=drx-dqx,duy=dry-dqy;
            const double vx=p0x-q0x,vy=p0y-q0y,dvx=dpx-dqx,dvy=dpy-dqy;
            const double a=dux*dvy-duy*dvx;
            const double b=(ux*dvy-uy*dvx)+(dux*vy-duy*vx);
            const double c=ux*vy-uy*vx;
            auto test_root=[&](double t) {
                if(!(t>1e-12 && t<best)) return;
                const double ex=ux+t*dux,ey=uy+t*duy;
                const double px=vx+t*dvx,py=vy+t*dvy;
                const double len2=ex*ex+ey*ey;
                if(len2<=1e-24) {best=t;return;}
                const double s=(px*ex+py*ey)/len2;
                if(s>=-0.05 && s<=1.05) best=t;
            };
            if(std::abs(a)<=1e-18) {
                if(std::abs(b)>1e-18) test_root(-c/b);
            } else {
                const double disc=b*b-4.*a*c;
                if(disc>=0.) {
                    const double sd=std::sqrt(disc);
                    test_root((-b-sd)/(2.*a));
                    test_root((-b+sd)/(2.*a));
                }
            }
        };
        check_pt_seg(ia,ic,id);check_pt_seg(ib,ic,id);
        check_pt_seg(ic,ia,ib);check_pt_seg(id,ia,ib);
        return best;
    }
    bool borders_valid(const Vec& x) const {
        if(!p.prevent_overlap) return true;
        auto pair_valid=[&](I i,I j) {
            if(border_pair_valid(x,i,j)) return true;
            if(p.prevent_overlap==1) {witness_i=i;witness_j=j;border_rejected=true;}
            return false;
        };
        // A broad phase only: retain the same exact pair predicate, including
        // shared vertices, touching edges and the requested spacing margin.
        // Tiny boundaries avoid sorting; nonfinite trials keep the old path.
        border_bounds.clear();
        bool sweep=p.nb>=64;
        const bool margin_guard=(p.prevent_overlap==1 && baseline_uv!=nullptr && p.overlap_space<=0.);
        double max_abs_coord=0.;
        if(sweep) {
            border_bounds.reserve(p.nb);
            for(I i=0;i<p.nb;++i) {
                I a=p.border_edges[2*i],b=p.border_edges[2*i+1];
                if(!std::isfinite(x[2*a]) || !std::isfinite(x[2*a+1]) ||
                   !std::isfinite(x[2*b]) || !std::isfinite(x[2*b+1])) {sweep=false;break;}
                if(margin_guard) {
                    max_abs_coord=std::max({max_abs_coord,std::abs(x[2*a]),std::abs(x[2*a+1]),
                                            std::abs(x[2*b]),std::abs(x[2*b+1])});
                }
                border_bounds.push_back({std::min(x[2*a],x[2*b]),std::max(x[2*a],x[2*b]),
                                         std::min(x[2*a+1],x[2*b+1]),std::max(x[2*a+1],x[2*b+1]),i});
            }
        }
        if(!sweep) {
            for(I i=0;i<p.nb;++i) for(I j=i+1;j<p.nb;++j) if(!pair_valid(i,j)) return false;
            return true;
        }
        std::sort(border_bounds.begin(),border_bounds.end(),[](const BorderBound& a,const BorderBound& b) {
            return a.low<b.low || (a.low==b.low && a.index<b.index);
        });
        const double margin=margin_guard
            ?std::max(p.overlap_space,6e-7*std::max({1.0,root_penalty_scale,max_abs_coord}))
            :std::max(p.overlap_space,0.);
        for(I i=0;i<p.nb;++i) {
            const double hi=border_bounds[i].high+margin;
            const double ylo=border_bounds[i].ylow,yhi=border_bounds[i].yhigh;
            const I idx_i=border_bounds[i].index;
            for(I j=i+1;j<p.nb;++j) {
                if(hi<border_bounds[j].low) break;
                if(yhi+margin<border_bounds[j].ylow || border_bounds[j].yhigh+margin<ylo) continue;
                I a=idx_i,b=border_bounds[j].index;
                if(!pair_valid(std::min(a,b),std::max(a,b))) return false;
            }
        }
        return true;
    }
    void build_j(const Vec& x) {
        for(I t=0;t<p.nt;++t) {
            I a=p.tri[3*t],b=p.tri[3*t+1],c=p.tri[3*t+2];const double* v=&p.inverse[4*t];double* j=&jacobian[4*t];
            for(I axis=0;axis<2;++axis) {
                double u=x[2*b+axis]-x[2*a+axis],w=x[2*c+axis]-x[2*a+axis];
                j[2*axis]=u*v[0]+w*v[2];j[2*axis+1]=u*v[1]+w*v[3];
            }
        }
    }
    void penalty(const Vec& x,double* energy,Vec* gradient) const {
        if(!soft) return;
        for(I r=0;r<p.nr;++r) if(p.kinds[r]!=1 && p.kinds[r]!=2) {
            I a=p.offsets[r],b=p.offsets[r+1];if(b-a<2) continue;
            double cx=0,cy=0;
            for(I k=a;k<b;++k) {I i=p.members[k];cx+=x[2*i];cy+=x[2*i+1];}cx/=(b-a);cy/=(b-a);
            double xx=0,xy=0,yy=0;
            for(I k=a;k<b;++k) {I i=p.members[k];double u=x[2*i]-cx,v=x[2*i+1]-cy;xx+=u*u;xy+=u*v;yy+=v*v;}
            double theta=.5*std::atan2(2*xy,xx-yy),dx=-std::sin(theta),dy=std::cos(theta);
            if(xy==0 && xx==yy) {dx=1;dy=0;}
            double weight=p.strength/((b-a)*p.penalty_scale),e=0;
            for(I k=a;k<b;++k) {I i=p.members[k];double v=(x[2*i]-cx)*dx+(x[2*i+1]-cy)*dy;e+=v*v;
                if(gradient) {(*gradient)[2*i]+=2*weight*v*dx;(*gradient)[2*i+1]+=2*weight*v*dy;}}
            if(energy) *energy+=weight*e;
        }
        if(energy) for(I e=0;e<p.ne;++e) {
            I a=p.penalty_edges[2*e],b=p.penalty_edges[2*e+1],axis=p.penalty_axis[e];
            double d=x[2*b+axis]-x[2*a+axis];*energy+=p.penalty_weights[e]*d*d;
        }
        // Same order as concatenate(-contribution, contribution) + bincount.
        if(gradient) for(int end=0;end<2;++end) for(I e=0;e<p.ne;++e) {
            I a=p.penalty_edges[2*e],b=p.penalty_edges[2*e+1],axis=p.penalty_axis[e];
            double d=x[2*b+axis]-x[2*a+axis];
            (*gradient)[2*(end?b:a)+axis]+=(end?2:-2)*p.penalty_weights[e]*d;
        }
    }
    bool rejects_at_previous_triangle(const Vec& x) const {
#ifdef _M_X64
        if(!gpu || (!reciprocal && !p.prevent_flips) || gpu->invalid_triangles.ids.front()<0) return false;
        // Bundled PTX uses separate round-to-nearest double operations
        // without flush-to-zero. Use the shortcut only in that FP mode.
        // Restore the Blender thread's full control and exception flags
        // before returning to any of the original solver arithmetic.
        struct RestoreMxcsr {
            unsigned original = _mm_getcsr();
            ~RestoreMxcsr() {_mm_setcsr(original);}
        } fp;
        constexpr unsigned ieee_nearest = _MM_MASK_MASK;
        _mm_setcsr(ieee_nearest);
        auto fails_determinant=[&](I t) {
            const I a=p.tri[3*t],b=p.tri[3*t+1],c=p.tri[3*t+2];
            const double* inv=&p.inverse[4*t];
            double jacobian[4];
            for(int axis=0;axis<2;++axis) {
                const __m128d base=_mm_set_sd(x[2*a+axis]);
                const __m128d u=_mm_sub_sd(_mm_set_sd(x[2*b+axis]),base);
                const __m128d w=_mm_sub_sd(_mm_set_sd(x[2*c+axis]),base);
                for(int column=0;column<2;++column) {
                    const volatile double u_term=_mm_cvtsd_f64(_mm_mul_sd(u,_mm_set_sd(inv[column])));
                    const volatile double w_term=_mm_cvtsd_f64(_mm_mul_sd(w,_mm_set_sd(inv[column+2])));
                    jacobian[2*axis+column]=_mm_cvtsd_f64(_mm_add_sd(_mm_set_sd(u_term),_mm_set_sd(w_term)));
                }
            }
            for(double component:jacobian) if(!std::isfinite(component)) return false;
            const volatile double left=jacobian[0]*jacobian[3],right=jacobian[1]*jacobian[2];
            const double determinant=left-right;
            return std::isfinite(determinant) && determinant<=1e-6;
        };
        for(const I triangle:gpu->invalid_triangles.ids) {
            if(triangle<0) break;
            if(fails_determinant(triangle)) {flip_rejected=true;return true;}
        }
        return false;
#else
        return false;
#endif
    }
    double energy(const Vec& x) {
        flip_rejected=false;++evaluations;
        if(p.prevent_overlap==1 && !borders_valid(x)) return INF;
        double e=0;
        if(gpu) {
            try {e=gpu->energy(x,reciprocal,p.prevent_flips!=0,p.angle_length);}
            catch(const std::exception& error) {gpu_failure(error);gpu.reset();}
            if(gpu && !std::isfinite(e)) {flip_rejected=true;return INF;}
        }
        if(!gpu) build_j(x);
        if(!gpu) for(I t=0;t<p.nt;++t) {
            const double* j=&jacobian[4*t];
            for(int k=0;k<4;++k) if(!std::isfinite(j[k])) return INF;
            double det=j[0]*j[3]-j[1]*j[2];
            if((p.prevent_flips || reciprocal) && det<=1e-6) {flip_rejected=true;return INF;}
            double norm=j[0]*j[0]+j[1]*j[1]+j[2]*j[2]+j[3]*j[3],squared=det*det+1e-16;
            double denominator=(p.prevent_flips || reciprocal)?det:std::sqrt(squared);
            double root=std::sqrt(std::max(norm+2*det,1e-20));
            double length=reciprocal?.5*(norm+norm/squared-4):norm-2*root+2;
            e+=((1-p.angle_length)*norm/denominator+p.angle_length*length)*p.weights[t];
        }
        if(p.prevent_overlap!=1 && !borders_valid(x)) return INF;
        penalty(x,&e,nullptr);return std::isfinite(e)?e:INF;
    }
    void gradient(const Vec& x,Vec& g) {
        Vec gu;
        if(gpu) {
            g.resize(2*p.n);
            try {gpu->gradient(reciprocal,p.prevent_flips!=0,p.angle_length,g);}
            catch(const std::exception& error) {gpu_failure(error);gpu.reset();g.assign(2*p.n,0);build_j(x);}
        } else {
            g.assign(2*p.n,0);
        }
        if(!gpu) gu.resize(4*p.nt);
        if(!gpu) for(I t=0;t<p.nt;++t) {
            const double* j=&jacobian[4*t];const double* inv=&p.inverse[4*t];
            double det=j[0]*j[3]-j[1]*j[2],norm=j[0]*j[0]+j[1]*j[1]+j[2]*j[2]+j[3]*j[3];
            double squared=det*det+1e-16,denominator=(p.prevent_flips || reciprocal)?det:std::sqrt(squared);
            double derivative=(p.prevent_flips || reciprocal)?1:det/denominator;
            double root=std::sqrt(std::max(norm+2*det,1e-20)),a,b;
            if(reciprocal) {a=(1-p.angle_length)*2/denominator+p.angle_length*(1+1/squared);
                b=-(1-p.angle_length)*norm*derivative/(denominator*denominator)-p.angle_length*norm*det/(squared*squared);}
            else {double rotation=(norm+2*det>1e-20)?2*p.angle_length/root:0;
                a=(1-p.angle_length)*2/denominator+2*p.angle_length-rotation;
                b=-(1-p.angle_length)*norm*derivative/(denominator*denominator)-rotation;}
            double gj[]={a*j[0]+b*j[3],a*j[1]-b*j[2],a*j[2]-b*j[1],a*j[3]+b*j[0]};
            for(int axis=0;axis<2;++axis) {
                gu[4*t+2*axis]=(gj[2*axis]*inv[0]+gj[2*axis+1]*inv[1])*p.weights[t];
                gu[4*t+2*axis+1]=(gj[2*axis]*inv[2]+gj[2*axis+1]*inv[3])*p.weights[t];
            }
        }
        if(!gpu) for(int corner=0;corner<3;++corner) for(I t=0;t<p.nt;++t) for(int axis=0;axis<2;++axis) {
            double a=gu[4*t+2*axis],b=gu[4*t+2*axis+1];
            g[2*p.tri[3*t+corner]+axis]+=(corner==0?-a-b:corner==1?a:b);
        }
        if(!soft) {
            if(axes_only) for(const auto& set:sets) {
                double mean=0;if(!set.anchored) {for(I i:set.members) mean+=g[2*i+set.axis];mean/=set.members.size();}
                for(I i:set.members) g[2*i+set.axis]=mean;
            }
            else for(I r=0;r<p.nr;++r) if(p.kinds[r]==1 || p.kinds[r]==2) {
                I a=p.offsets[r],b=p.offsets[r+1],axis=p.kinds[r]==1?1:0; if(a==b) continue;
                double mean=0;bool anchored=false;
                for(I k=a;k<b;++k) {mean+=g[2*p.members[k]+axis];anchored|=p.fixed[p.members[k]]!=0;}
                mean=anchored?0:mean/(b-a);for(I k=a;k<b;++k) g[2*p.members[k]+axis]=mean;
            }
        } else penalty(x,nullptr,&g);
        for(I i=0;i<p.n;++i) if(p.fixed[i]) {g[2*i]=0;g[2*i+1]=0;}
    }
};
static bool valid_input(const Input& p) {
    if(p.n<=0 || p.nt<=0 || p.nr<0 || p.nm<0 || p.ne<0 || p.nb<0 || p.n>10000000 || p.nt>20000000) return false;
    if(!p.uv || !p.inverse || !p.weights || !p.tri || !p.fixed || !p.held || !p.offsets) return false;
    if((p.nr && !p.kinds) || (p.nm && !p.members) || (p.ne && (!p.penalty_axis || !p.penalty_edges || !p.penalty_weights)) || (p.nb && !p.border_edges)) return false;
    if(!std::isfinite(p.density) || !std::isfinite(p.angle_length) || !std::isfinite(p.strength) || !std::isfinite(p.penalty_scale) || p.penalty_scale<=0 || !std::isfinite(p.overlap_space)) return false;
    for(I i=0;i<3*p.nt;++i) if(p.tri[i]<0 || p.tri[i]>=p.n) return false;
    for(I i=0;i<2*p.n;++i) if(!std::isfinite(p.uv[i]) || !std::isfinite(p.held[i])) return false;
    for(I i=0;i<4*p.nt;++i) if(!std::isfinite(p.inverse[i])) return false;
    for(I i=0;i<p.nt;++i) if(!std::isfinite(p.weights[i]) || p.weights[i]<0) return false;
    if(p.offsets[0]!=0 || p.offsets[p.nr]!=p.nm) return false;
    for(I r=0;r<p.nr;++r) if(p.offsets[r]<0 || p.offsets[r]>p.offsets[r+1] || p.offsets[r+1]>p.nm || p.kinds[r]<1 || p.kinds[r]>3) return false;
    for(I i=0;i<p.nm;++i) if(p.members[i]<0 || p.members[i]>=p.n) return false;
    for(I i=0;i<2*p.ne;++i) if(p.penalty_edges[i]<0 || p.penalty_edges[i]>=p.n) return false;
    for(I i=0;i<p.ne;++i) if(p.penalty_axis[i]<0 || p.penalty_axis[i]>1 || !std::isfinite(p.penalty_weights[i]) || p.penalty_weights[i]<0) return false;
    for(I i=0;i<2*p.nb;++i) if(p.border_edges[i]<0 || p.border_edges[i]>=p.n) return false;
    return true;
}
API I uvck_abi(I input_size,I output_size) {return input_size==sizeof(Input) && output_size==sizeof(Output)?1:0;}
static int uvck_solve_impl(const Input* input,double* output,Output* stats) noexcept {
    try {
        if(!input || !output || !stats || !valid_input(*input)) return -1;
        const Input& p=*input;Problem problem(p);Vec x(p.uv,p.uv+2*p.n);problem.project(x);problem.build_j(x);
        problem.reciprocal=true;
        bool finite_j=true;
        for(I t=0;t<p.nt;++t) {
            const double* j=&problem.jacobian[4*t];
            for(int k=0;k<4;++k) if(!std::isfinite(j[k])) finite_j=false;
            double d=j[0]*j[3]-j[1]*j[2];
            if(!std::isfinite(d) || d<=1e-6) problem.reciprocal=false;
        }
        if(!finite_j || (p.prevent_flips && !problem.reciprocal) || !problem.borders_valid(x)) {
            *stats={0,1,problem.reciprocal,0,INF,INF};
            return 1; // Do not initialize GPU or publish an invalid initial projection.
        }
        problem.init_gpu();
        if(problem.gpu && problem.axes_only && p.n <= 200000000 && p.nt <= 200000000) {
            try {
                auto& gs = *problem.gpu;
                auto& drv = gs.driver;
                auto& c = drv.cache;
                CUstream st = gs.stream;
                const int ni = int(p.n), nti = int(p.nt), n2 = int(2*p.n);
                const unsigned g_n = GpuSession::grid256(ni);
                const unsigned g_2n = GpuSession::grid256(n2);
                const unsigned rb_nt = GpuSession::reduce_grid(nti);
                const unsigned rb_2n = GpuSession::reduce_grid(n2);
                const int rb_nt_i = int(rb_nt), rb_2n_i = int(rb_2n);
                const int rec_i = problem.reciprocal ? 1 : 0;
                const int flip_i = p.prevent_flips ? 1 : 0;
                const double angle_d = p.angle_length;
                const float angle_f = static_cast<float>(p.angle_length);

                // 1. Diagonal scaling (1.0 on free DOFs, 0.0 on fixed DOFs; keeps exact L-BFGS conditioning)
                Vec inv_diag_h(2*p.n, 0.0);
                for(I v = 0; v < p.n; ++v) if(!p.fixed[v]) {
                    inv_diag_h[2*v] = 1.0;
                    inv_diag_h[2*v+1] = 1.0;
                }
                if(!problem.soft) {
                    for(const auto& set : problem.sets) if(set.anchored) {
                        for(I i : set.members) inv_diag_h[2*i+set.axis] = 0.0;
                    }
                }

                // 2. Upload fixed, held, inv_diag, initial x_d
                std::vector<std::uint8_t> fixed_h(p.fixed, p.fixed + p.n);
                Vec held_h(p.held, p.held + 2*p.n);
                bool has_fixed = false;
                for(I v = 0; v < p.n; ++v) if(fixed_h[v]) { has_fixed = true; break; }
                drv.check(drv.upload(c.fixed_u8, fixed_h.data(), std::size_t(p.n), st), "up fixed");
                drv.check(drv.upload(c.held_d, held_h.data(), 2*p.n*sizeof(double), st), "up held");
                drv.check(drv.upload(c.inv_diag_d, inv_diag_h.data(), 2*p.n*sizeof(double), st), "up inv_diag");
                drv.check(drv.upload(c.x_d, x.data(), 2*p.n*sizeof(double), st), "up x_d");
                {
                    int cnt2n = n2;
                    void* a1[] = {&cnt2n, &c.held_d, &c.held_f};
                    drv.check(drv.launch(drv.cast_d2f, g_2n, 1, 1, 256, 1, 1, 0, st, a1, nullptr), "cast held_f");
                    void* a2[] = {&cnt2n, &c.inv_diag_d, &c.inv_diag_f};
                    drv.check(drv.launch(drv.cast_d2f, g_2n, 1, 1, 256, 1, 1, 0, st, a2, nullptr), "cast inv_diag_f");
                }
                gpu_counts[4] += std::size_t(p.n) + 6*p.n*sizeof(double);

                // 3. Upload AxisSet CSR (!soft)
                const int nsets = (!problem.soft) ? int(problem.sets.size()) : 0;
                if(nsets > 0) {
                    std::vector<int> s_off(nsets + 1, 0), s_mem, s_ax(nsets);
                    std::vector<std::uint8_t> s_anc(nsets);
                    std::vector<double> s_val(nsets);
                    for(int s = 0; s < nsets; ++s) {
                        s_ax[s] = int(problem.sets[s].axis);
                        s_anc[s] = problem.sets[s].anchored ? 1 : 0;
                        s_val[s] = problem.sets[s].anchor;
                        for(I m : problem.sets[s].members) s_mem.push_back(int(m));
                        s_off[s + 1] = int(s_mem.size());
                    }
                    gs.ensure_set_buffers(nsets, std::max<I>(1, I(s_mem.size())));
                    drv.check(drv.upload(c.set_offsets, s_off.data(), (nsets+1)*sizeof(int), st), "up set_off");
                    if(!s_mem.empty())
                        drv.check(drv.upload(c.set_members, s_mem.data(), s_mem.size()*sizeof(int), st), "up set_mem");
                    drv.check(drv.upload(c.set_axis, s_ax.data(), nsets*sizeof(int), st), "up set_ax");
                    drv.check(drv.upload(c.set_anchored, s_anc.data(), std::size_t(nsets), st), "up set_anc");
                    drv.check(drv.upload(c.set_anchor, s_val.data(), nsets*sizeof(double), st), "up set_val");
                }

                // 4. Upload soft penalty CSR (soft && ne > 0)
                const int nei = (problem.soft && p.ne > 0) ? int(p.ne) : 0;
                int npv = 0;
                if(nei > 0) {
                    std::vector<int> pe(2*nei), pax(nei);
                    std::vector<double> pwd(nei);
                    std::vector<float> pwf(nei);
                    std::map<int, std::vector<int>> v2e;
                    for(int e_idx = 0; e_idx < nei; ++e_idx) {
                        int a = int(p.penalty_edges[2*e_idx]), b = int(p.penalty_edges[2*e_idx+1]);
                        pe[2*e_idx] = a; pe[2*e_idx+1] = b;
                        pax[e_idx] = int(p.penalty_axis[e_idx]);
                        pwd[e_idx] = p.penalty_weights[e_idx];
                        pwf[e_idx] = static_cast<float>(p.penalty_weights[e_idx]);
                        v2e[a].push_back((e_idx << 1) | 0);
                        v2e[b].push_back((e_idx << 1) | 1);
                    }
                    npv = int(v2e.size());
                    std::vector<int> pv_ids, pv_off(npv + 1, 0), pv_edges;
                    pv_ids.reserve(npv);
                    pv_edges.reserve(2*nei);
                    int idx = 0;
                    for(const auto& kv : v2e) {
                        pv_ids.push_back(kv.first);
                        for(int code : kv.second) pv_edges.push_back(code);
                        pv_off[++idx] = int(pv_edges.size());
                    }
                    gs.ensure_penalty_buffers(nei, npv, std::max<I>(1, I(pv_edges.size())));
                    drv.check(drv.upload(c.pen_edges, pe.data(), 2*nei*sizeof(int), st), "up pen_e");
                    drv.check(drv.upload(c.pen_axis, pax.data(), nei*sizeof(int), st), "up pen_ax");
                    drv.check(drv.upload(c.pen_w_d, pwd.data(), nei*sizeof(double), st), "up pen_wd");
                    drv.check(drv.upload(c.pen_w_f, pwf.data(), nei*sizeof(float), st), "up pen_wf");
                    drv.check(drv.upload(c.pv_ids, pv_ids.data(), npv*sizeof(int), st), "up pv_ids");
                    drv.check(drv.upload(c.pv_offsets, pv_off.data(), (npv+1)*sizeof(int), st), "up pv_off");
                    drv.check(drv.upload(c.pv_edges, pv_edges.data(), pv_edges.size()*sizeof(int), st), "up pv_edges");
                }

                // 5. Compact border table for prevent_overlap == 1
                const bool check_border = (p.prevent_overlap != 0 && p.nb > 0);
                std::vector<int> b_verts;
                std::vector<I> local_be;
                std::unordered_map<I, I> g2local_b;
                Vec init_border_uv, curr_border_uv;
                int nv_b = 0;
                if(check_border) {
                    b_verts.reserve(std::size_t(p.nb * 2));
                    for(I k = 0; k < 2*p.nb; ++k) b_verts.push_back(int(p.border_edges[k]));
                    std::sort(b_verts.begin(), b_verts.end());
                    b_verts.erase(std::unique(b_verts.begin(), b_verts.end()), b_verts.end());
                    nv_b = int(b_verts.size());
                    g2local_b.reserve(std::size_t(nv_b) * 2);
                    init_border_uv.resize(2*nv_b);
                    for(int i = 0; i < nv_b; ++i) {
                        g2local_b[b_verts[i]] = i;
                        init_border_uv[2*i] = x[2*b_verts[i]];
                        init_border_uv[2*i+1] = x[2*b_verts[i]+1];
                    }
                    curr_border_uv = init_border_uv;
                    local_be.resize(2*p.nb);
                    for(I k = 0; k < 2*p.nb; ++k) local_be[k] = g2local_b[p.border_edges[k]];
                    gs.ensure_border_buffers(nv_b);
                    drv.check(drv.upload(c.border_verts, b_verts.data(), nv_b*sizeof(int), st), "up b_verts");
                }
                drv.check(drv.wait(st), "setup wait");

                const bool keep_initial_border = (p.prevent_overlap == 1 && p.overlap_space <= 0.0);
                auto compact_pair_valid = [&](const double* bx, const double* base_bx, I i, I j) -> bool {
                    I ia = local_be[2*i], ib = local_be[2*i+1], ic = local_be[2*j], id = local_be[2*j+1];
                    if(ia == ic || ia == id || ib == ic || ib == id) return true;
                    const double *a = &bx[2*ia], *b = &bx[2*ib], *cc = &bx[2*ic], *dd = &bx[2*id];
                    const bool margin_guard = (p.prevent_overlap == 1 && base_bx != nullptr && p.overlap_space <= 0.0);
                    double coord_scale = 1.0, local_margin = 0.0;
                    if(margin_guard) {
                        coord_scale = std::max({1.0, problem.root_penalty_scale,
                            std::abs(a[0]), std::abs(a[1]), std::abs(b[0]), std::abs(b[1]),
                            std::abs(cc[0]), std::abs(cc[1]), std::abs(dd[0]), std::abs(dd[1])});
                        local_margin = 6e-7 * coord_scale;
                    }
                    double margin = margin_guard ? std::max({p.overlap_space, 0.0, local_margin}) : std::max(p.overlap_space, 0.0);
                    if(std::max(a[0],b[0])+margin < std::min(cc[0],dd[0]) || std::max(cc[0],dd[0])+margin < std::min(a[0],b[0]) ||
                       std::max(a[1],b[1])+margin < std::min(cc[1],dd[1]) || std::max(cc[1],dd[1])+margin < std::min(a[1],b[1])) return true;
                    double abx = b[0]-a[0], aby = b[1]-a[1], cdx = dd[0]-cc[0], cdy = dd[1]-cc[1];
                    if(Problem::cross(abx,aby,cc[0]-a[0],cc[1]-a[1])*Problem::cross(abx,aby,dd[0]-a[0],dd[1]-a[1]) < 0 &&
                       Problem::cross(cdx,cdy,a[0]-cc[0],a[1]-cc[1])*Problem::cross(cdx,cdy,b[0]-cc[0],b[1]-cc[1]) < 0) return false;
                    if(p.prevent_overlap == 1) {
                        const double af[2] = {double(float(a[0])), double(float(a[1]))};
                        const double bf[2] = {double(float(b[0])), double(float(b[1]))};
                        const double cf[2] = {double(float(cc[0])), double(float(cc[1]))};
                        const double df[2] = {double(float(dd[0])), double(float(dd[1]))};
                        const double abfx = bf[0]-af[0], abfy = bf[1]-af[1], cdfx = df[0]-cf[0], cdfy = df[1]-cf[1];
                        if(Problem::cross(abfx,abfy,cf[0]-af[0],cf[1]-af[1])*Problem::cross(abfx,abfy,df[0]-af[0],df[1]-af[1]) < 0 &&
                           Problem::cross(cdfx,cdfy,af[0]-cf[0],af[1]-cf[1])*Problem::cross(cdfx,cdfy,bf[0]-cf[0],bf[1]-cf[1]) < 0) return false;
                    }
                    if(margin_guard) {
                        const double d_cand = std::min({Problem::distance(a,cc,dd), Problem::distance(b,cc,dd),
                                                        Problem::distance(cc,a,b), Problem::distance(dd,a,b)});
                        if(d_cand < local_margin) {
                            const double *ba = &base_bx[2*ia], *bb = &base_bx[2*ib], *bc = &base_bx[2*ic], *bd = &base_bx[2*id];
                            const double d_curr = std::min({Problem::distance(ba,bc,bd), Problem::distance(bb,bc,bd),
                                                            Problem::distance(bc,ba,bb), Problem::distance(bd,ba,bb)});
                            if(d_cand < std::min(local_margin, 0.5*d_curr)) return false;
                        }
                    }
                    if(p.overlap_space > 0 && std::min({Problem::distance(a,cc,dd), Problem::distance(b,cc,dd),
                                                        Problem::distance(cc,a,b), Problem::distance(dd,a,b)}) < p.overlap_space) return false;
                    return true;
                };
                auto check_compact_borders = [&](const double* bx, const double* base_bx) -> bool {
                    if(!check_border) return true;
                    auto pair_ok = [&](I i, I j) {
                        if(compact_pair_valid(bx, base_bx, i, j)) return true;
                        if(p.prevent_overlap == 1) { problem.witness_i = i; problem.witness_j = j; problem.border_rejected = true; }
                        return false;
                    };
                    problem.border_bounds.clear();
                    bool sweep = p.nb >= 64;
                    const bool margin_guard = (p.prevent_overlap == 1 && base_bx != nullptr && p.overlap_space <= 0.0);
                    double max_abs_c = 0.0;
                    if(sweep) {
                        problem.border_bounds.reserve(p.nb);
                        for(I i = 0; i < p.nb; ++i) {
                            I a = local_be[2*i], b = local_be[2*i+1];
                            if(!std::isfinite(bx[2*a]) || !std::isfinite(bx[2*a+1]) ||
                               !std::isfinite(bx[2*b]) || !std::isfinite(bx[2*b+1])) { sweep = false; break; }
                            if(margin_guard) {
                                max_abs_c = std::max({max_abs_c, std::abs(bx[2*a]), std::abs(bx[2*a+1]),
                                                      std::abs(bx[2*b]), std::abs(bx[2*b+1])});
                            }
                            problem.border_bounds.push_back({std::min(bx[2*a],bx[2*b]), std::max(bx[2*a],bx[2*b]),
                                                             std::min(bx[2*a+1],bx[2*b+1]), std::max(bx[2*a+1],bx[2*b+1]), i});
                        }
                    }
                    if(!sweep) {
                        for(I i = 0; i < p.nb; ++i) for(I j = i+1; j < p.nb; ++j) if(!pair_ok(i, j)) return false;
                        return true;
                    }
                    std::sort(problem.border_bounds.begin(), problem.border_bounds.end(),
                        [](const Problem::BorderBound& a, const Problem::BorderBound& b) {
                            return a.low < b.low || (a.low == b.low && a.index < b.index);
                        });
                    const double margin = margin_guard
                        ? std::max(p.overlap_space, 6e-7*std::max({1.0, problem.root_penalty_scale, max_abs_c}))
                        : std::max(p.overlap_space, 0.0);
                    for(I i = 0; i < p.nb; ++i) {
                        const double hi = problem.border_bounds[i].high + margin;
                        const double ylo = problem.border_bounds[i].ylow, yhi = problem.border_bounds[i].yhigh;
                        const I idx_i = problem.border_bounds[i].index;
                        for(I j = i+1; j < p.nb; ++j) {
                            if(hi < problem.border_bounds[j].low) break;
                            if(yhi + margin < problem.border_bounds[j].ylow || problem.border_bounds[j].yhigh + margin < ylo) continue;
                            I a = idx_i, b = problem.border_bounds[j].index;
                            if(!pair_ok(std::min(a,b), std::max(a,b))) return false;
                        }
                    }
                    return true;
                };
                auto compact_border_ccd = [&](const double* x0, const double* x1, I i, I j) -> double {
                    if(i < 0 || j < 0 || i >= p.nb || j >= p.nb) return 1.0;
                    const I ia = local_be[2*i], ib = local_be[2*i+1], ic = local_be[2*j], id = local_be[2*j+1];
                    if(ia == ic || ia == id || ib == ic || ib == id) return 1.0;
                    double best = 1.0;
                    auto check_pt = [&](I ip, I iq, I ir) {
                        const double p0x = x0[2*ip], p0y = x0[2*ip+1], dpx = x1[2*ip]-p0x, dpy = x1[2*ip+1]-p0y;
                        const double q0x = x0[2*iq], q0y = x0[2*iq+1], dqx = x1[2*iq]-q0x, dqy = x1[2*iq+1]-q0y;
                        const double r0x = x0[2*ir], r0y = x0[2*ir+1], drx = x1[2*ir]-r0x, dry = x1[2*ir+1]-r0y;
                        const double ux = r0x-q0x, uy = r0y-q0y, dux = drx-dqx, duy = dry-dqy;
                        const double vx = p0x-q0x, vy = p0y-q0y, dvx = dpx-dqx, dvy = dpy-dqy;
                        const double a = dux*dvy - duy*dvx;
                        const double b = (ux*dvy - uy*dvx) + (dux*vy - duy*vx);
                        const double cc = ux*vy - uy*vx;
                        auto test_r = [&](double t) {
                            if(!(t > 1e-12 && t < best)) return;
                            const double ex = ux+t*dux, ey = uy+t*duy;
                            const double px = vx+t*dvx, py = vy+t*dvy;
                            const double len2 = ex*ex + ey*ey;
                            if(len2 <= 1e-24) { best = t; return; }
                            const double s = (px*ex + py*ey) / len2;
                            if(s >= -0.05 && s <= 1.05) best = t;
                        };
                        if(std::abs(a) <= 1e-18) {
                            if(std::abs(b) > 1e-18) test_r(-cc/b);
                        } else {
                            const double disc = b*b - 4.0*a*cc;
                            if(disc >= 0.0) {
                                const double sd = std::sqrt(disc);
                                test_r((-b-sd)/(2.0*a)); test_r((-b+sd)/(2.0*a));
                            }
                        }
                    };
                    check_pt(ia,ic,id); check_pt(ib,ic,id); check_pt(ic,ia,ib); check_pt(id,ia,ib);
                    return best;
                };

                const bool overlap_progress = problem.soft && p.prevent_overlap == 1;
                const bool allow_contact_pin = (p.prevent_overlap == 1 && !problem.soft);
                std::vector<std::uint8_t> contact_pinned(allow_contact_pin ? p.n : 0, 0);
                std::vector<std::uint8_t> constrained_vert(allow_contact_pin ? p.n : 0, 0);
                if(allow_contact_pin) {
                    for(I k = 0; k < p.nm; ++k) constrained_vert[p.members[k]] = 1;
                }
                I pinned_border_count = 0;

                const bool early_subspace = (p.automatic && problem.soft && problem.axes_only
                    && p.nr > 0 && p.ne > 0 && p.prevent_overlap == 1 && p.strength < 1e6);
                auto max_pen_tan = [&](const Vec& v) -> double {
                    double worst = 0.0;
                    for(I e_idx = 0; e_idx < p.ne; ++e_idx) {
                        I a = p.penalty_edges[2*e_idx], b = p.penalty_edges[2*e_idx+1], ax = p.penalty_axis[e_idx];
                        double along = std::abs(v[2*b+1-ax] - v[2*a+1-ax]);
                        double across = std::abs(v[2*b+ax] - v[2*a+ax]);
                        if(along <= 1e-12) return INF;
                        worst = std::max(worst, across / along);
                    }
                    return worst;
                };
                const double initial_max_tan = early_subspace ? max_pen_tan(x) : 0.0;
                const double init_scale = std::max(p.density*p.density, 1e-6) * 0.05;
                const double max_disp = 0.05 * std::sqrt(std::max(p.penalty_scale, 1e-20));
                const I limit = p.automatic ? 1000 : std::max<I>(1, p.iterations);

                // Initial FP64 energy check on GPU
                {
                    int nti_v = nti, rec_v = rec_i, fl_v = flip_i; double ang_v = angle_d;
                    void* ea[] = {&nti_v, &c.x_d, &c.tri_i32, &c.inverse, &c.j, &c.weights, &rec_v, &fl_v, &ang_v, &c.block_buf0, &c.block_inv};
                    drv.check(drv.launch(drv.energy_block_d, rb_nt, 1, 1, 256, 1, 1, 0, st, ea, nullptr), "e0_d");
                    int nb_v = rb_nt_i;
                    void* ra[] = {&nb_v, &c.block_buf0, &c.block_inv, &c.scalars};
                    drv.check(drv.launch(drv.reduce_energy_final, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "r0_d");
                    if(nei > 0) {
                        int ne_v = nei; unsigned rbn = GpuSession::reduce_grid(nei); int rbn_i = int(rbn);
                        void* pa[] = {&ne_v, &c.x_d, &c.pen_edges, &c.pen_axis, &c.pen_w_d, &c.block_buf0};
                        drv.check(drv.launch(drv.penalty_energy_block_d, rbn, 1, 1, 256, 1, 1, 0, st, pa, nullptr), "p0_d");
                        void* pra[] = {&rbn_i, &c.block_buf0, &c.scalars};
                        drv.check(drv.launch(drv.add_pen_energy, 1, 1, 1, 256, 1, 1, 0, st, pra, nullptr), "pr0_d");
                    }
                    drv.check(drv.download(c.staging_scalars, c.scalars, 2*sizeof(double), st), "dl e0");
                    drv.check(drv.wait(st), "w e0");
                    ++gpu_counts[1]; ++problem.evaluations; gpu_counts[5] += 2*sizeof(double);
                }
                const double e_d0 = c.staging_scalars[SC_ENERGY];
                *stats = {0, problem.evaluations, problem.reciprocal, 0, e_d0, INF};
                if(!std::isfinite(e_d0)) return 1;

                // Helper to run either FP32 Sprint (is_f32=true) or FP64 Polish (is_f32=false)
                I total_accepted = 0;
                bool hit_early_subspace = false;
                auto run_phase = [&](bool is_f32, CUdeviceptr& x_ptr, CUdeviceptr& cand_ptr,
                                     CUdeviceptr& g_ptr, CUdeviceptr& next_g_ptr, CUdeviceptr dir_ptr,
                                     CUdeviceptr inv_diag_ptr, CUdeviceptr held_ptr,
                                     CUdeviceptr* s_hist, CUdeviceptr* y_hist,
                                     I phase_limit, double& out_e, double& out_ginf) {
                    const CUfunction k_eb = is_f32 ? drv.energy_block_f32 : drv.energy_block_d;
                    const CUfunction k_pe = is_f32 ? drv.penalty_energy_block_f32 : drv.penalty_energy_block_d;
                    const CUfunction k_pg = is_f32 ? drv.penalty_grad_f32 : drv.penalty_grad_d;
                    const CUfunction k_pa = is_f32 ? drv.project_axes_f32 : drv.project_axes_d;
                    const CUfunction k_pga = is_f32 ? drv.project_grad_axes_f32 : drv.project_grad_axes_d;
                    const CUfunction k_af = is_f32 ? drv.apply_fixed_f32 : drv.apply_fixed_d;
                    const CUfunction k_zf = is_f32 ? drv.zero_fixed_f32 : drv.zero_fixed_d;
                    const CUfunction k_cp = is_f32 ? drv.copy_f32 : drv.copy_d;
                    const CUfunction k_dot = is_f32 ? drv.dot_block_f32 : drv.dot_block_d;
                    const CUfunction k_axpy = is_f32 ? drv.axpy_slot_f32 : drv.axpy_slot_d;
                    const CUfunction k_sdiag = is_f32 ? drv.scale_diag_slot_f32 : drv.scale_diag_slot_d;
                    const CUfunction k_ndot = is_f32 ? drv.negate_dot_block_f32 : drv.negate_dot_block_d;
                    const CUfunction k_cstep = is_f32 ? drv.cand_step_f32 : drv.cand_step_d;
                    const CUfunction k_ct0 = is_f32 ? drv.cand_trial0_f32 : drv.cand_trial0_d;
                    const CUfunction k_mabs = is_f32 ? drv.max_abs_block_f32 : drv.max_abs_block_d;
                    const CUfunction k_sy = is_f32 ? drv.sy_ymy_block_f32 : drv.sy_ymy_block_d;
                    const CUfunction k_mfs = is_f32 ? drv.max_flip_step_block_f32 : drv.max_flip_step_block_d;
                    const CUfunction k_gb = is_f32 ? drv.gather_border_f32 : drv.gather_border_d;
                    CUdeviceptr inv_p = is_f32 ? c.inv_f : c.inverse;
                    CUdeviceptr w_p = is_f32 ? c.weights_f : c.weights;
                    CUdeviceptr j_p = is_f32 ? c.j_f : c.j;
                    CUdeviceptr gu_p = is_f32 ? c.gu_f : c.result;
                    CUdeviceptr penw_p = is_f32 ? c.pen_w_f : c.pen_w_d;

                    auto l_project = [&](CUdeviceptr vptr) {
                        if(nsets > 0) {
                            int ns_v = nsets;
                            void* a[] = {&ns_v, &c.set_offsets, &c.set_members, &c.set_axis, &c.set_anchored, &c.set_anchor, &vptr};
                            drv.check(drv.launch(k_pa, GpuSession::grid256(nsets), 1, 1, 256, 1, 1, 0, st, a, nullptr), "proj_ax");
                        }
                        if(has_fixed) {
                            int n_v = ni;
                            void* a[] = {&n_v, &c.fixed_u8, &held_ptr, &vptr};
                            drv.check(drv.launch(k_af, g_n, 1, 1, 256, 1, 1, 0, st, a, nullptr), "proj_fix");
                        }
                    };
                    auto l_energy = [&](CUdeviceptr vptr, bool dl_border) {
                        int nti_v = nti, rec_v = rec_i, fl_v = flip_i;
                        if(is_f32) {
                            float ang_v = angle_f;
                            void* ea[] = {&nti_v, &vptr, &c.tri_i32, &inv_p, &j_p, &w_p, &rec_v, &fl_v, &ang_v, &c.block_buf0, &c.block_inv};
                            drv.check(drv.launch(k_eb, rb_nt, 1, 1, 256, 1, 1, 0, st, ea, nullptr), "eb_f32");
                        } else {
                            double ang_v = angle_d;
                            void* ea[] = {&nti_v, &vptr, &c.tri_i32, &inv_p, &j_p, &w_p, &rec_v, &fl_v, &ang_v, &c.block_buf0, &c.block_inv};
                            drv.check(drv.launch(k_eb, rb_nt, 1, 1, 256, 1, 1, 0, st, ea, nullptr), "eb_d");
                        }
                        int nb_v = rb_nt_i;
                        void* ra[] = {&nb_v, &c.block_buf0, &c.block_inv, &c.scalars};
                        drv.check(drv.launch(drv.reduce_energy_final, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "re_fin");
                        if(nei > 0) {
                            int ne_v = nei; unsigned rbn = GpuSession::reduce_grid(nei); int rbn_i = int(rbn);
                            void* pa[] = {&ne_v, &vptr, &c.pen_edges, &c.pen_axis, &penw_p, &c.block_buf0};
                            drv.check(drv.launch(k_pe, rbn, 1, 1, 256, 1, 1, 0, st, pa, nullptr), "pen_e");
                            void* pra[] = {&rbn_i, &c.block_buf0, &c.scalars};
                            drv.check(drv.launch(drv.add_pen_energy, 1, 1, 1, 256, 1, 1, 0, st, pra, nullptr), "add_pe");
                        }
                        if(dl_border && nv_b > 0) {
                            int nvb_v = nv_b;
                            void* ga[] = {&nvb_v, &c.border_verts, &vptr, &c.border_out};
                            drv.check(drv.launch(k_gb, GpuSession::grid256(nv_b), 1, 1, 256, 1, 1, 0, st, ga, nullptr), "g_bord");
                            drv.check(drv.download(c.staging_border, c.border_out, 2*nv_b*sizeof(double), st), "dl_bord");
                            gpu_counts[5] += 2*nv_b*sizeof(double);
                        }
                        ++gpu_counts[1]; ++problem.evaluations;
                    };
                    auto l_grad = [&](CUdeviceptr vptr, CUdeviceptr gptr) {
                        int nti_v = nti, ni_v = ni, rec_v = rec_i, fl_v = flip_i;
                        if(is_f32) {
                            float ang_v = angle_f;
                            void* ga[] = {&nti_v, &j_p, &inv_p, &w_p, &rec_v, &fl_v, &ang_v, &gu_p};
                            drv.check(drv.launch(drv.grad_f32, GpuSession::grid256(nti), 1, 1, 256, 1, 1, 0, st, ga, nullptr), "grad_f32");
                            void* sa[] = {&ni_v, &nti_v, &c.adj_off_i32, &c.adj_ent_i32, &gu_p, &gptr};
                            drv.check(drv.launch(drv.scatter_f32, g_n, 1, 1, 256, 1, 1, 0, st, sa, nullptr), "scat_f32");
                        } else {
                            I nt_ll = p.nt; double ang_v = angle_d;
                            void* ga[] = {&nt_ll, &j_p, &inv_p, &w_p, &rec_v, &fl_v, &ang_v, &gu_p};
                            drv.check(drv.launch(drv.gradient_kernel, unsigned((p.nt+127)/128), 1, 1, 128, 1, 1, 0, st, ga, nullptr), "grad_d");
                            void* sa[] = {&ni_v, &nti_v, &c.adj_off_i32, &c.adj_ent_i32, &gu_p, &gptr};
                            drv.check(drv.launch(drv.scatter_i32_d, g_n, 1, 1, 256, 1, 1, 0, st, sa, nullptr), "scat_d");
                        }
                        ++gpu_counts[2];
                        if(nei > 0) {
                            int npv_v = npv;
                            void* pa[] = {&npv_v, &c.pv_ids, &c.pv_offsets, &c.pv_edges, &c.pen_edges, &c.pen_axis, &penw_p, &vptr, &gptr};
                            drv.check(drv.launch(k_pg, GpuSession::grid256(npv), 1, 1, 256, 1, 1, 0, st, pa, nullptr), "pen_g");
                        }
                        if(nsets > 0) {
                            int ns_v = nsets;
                            void* a[] = {&ns_v, &c.set_offsets, &c.set_members, &c.set_axis, &c.set_anchored, &gptr};
                            drv.check(drv.launch(k_pga, GpuSession::grid256(nsets), 1, 1, 256, 1, 1, 0, st, a, nullptr), "proj_ga");
                        }
                        if(has_fixed) {
                            int n_v = ni;
                            void* a[] = {&n_v, &c.fixed_u8, &gptr};
                            drv.check(drv.launch(k_zf, g_n, 1, 1, 256, 1, 1, 0, st, a, nullptr), "zero_fix");
                        }
                    };
                    auto sync_sc = [&](int count = 12) {
                        drv.check(drv.download(c.staging_scalars, c.scalars, count*sizeof(double), st), "dl_sc");
                        drv.check(drv.wait(st), "wait_sc");
                        gpu_counts[5] += count*sizeof(double);
                    };

                    l_energy(x_ptr, check_border);
                    sync_sc(2);
                    double e = c.staging_scalars[SC_ENERGY];
                    if(!std::isfinite(e)) { out_e = INF; out_ginf = INF; return; }
                    if(check_border) std::memcpy(curr_border_uv.data(), c.staging_border, 2*nv_b*sizeof(double));
                    l_grad(x_ptr, g_ptr);
                    {
                        int n2_v = n2, sl = SC_MAX_G, nb_v = rb_2n_i;
                        void* ma[] = {&n2_v, &g_ptr, &c.block_buf0};
                        drv.check(drv.launch(k_mabs, rb_2n, 1, 1, 256, 1, 1, 0, st, ma, nullptr), "mabs0");
                        void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &sl};
                        drv.check(drv.launch(drv.reduce_max_to_slot, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "rmabs0");
                    }
                    sync_sc(4);
                    double g_inf = c.staging_scalars[SC_MAX_G];
                    out_e = e; out_ginf = g_inf;
                    if(phase_limit <= 0) return;

                    int ring_slots[GPU_HIST_LIMIT]{};
                    int hist_len = 0;
                    double checkpoint_energy = e;
                    I phase_accepted = 0;
                    const double grad_tol = is_f32 ? 1e-4 : 1e-6;
                    const double imp_tol = is_f32 ? 1e-7 : 1e-10;
                    const double* base_b = keep_initial_border ? init_border_uv.data() : nullptr;

                    auto try_pin_gpu = [&]() -> bool {
                        if(!allow_contact_pin || !problem.border_rejected || problem.witness_i < 0 || pinned_border_count >= 16)
                            return false;
                        const I verts[4] = {
                            p.border_edges[2*problem.witness_i], p.border_edges[2*problem.witness_i+1],
                            p.border_edges[2*problem.witness_j], p.border_edges[2*problem.witness_j+1]
                        };
                        bool added = false;
                        for(I v : verts) if(!contact_pinned[v] && !constrained_vert[v] && !fixed_h[v]) {
                            contact_pinned[v] = 1;
                            fixed_h[v] = 1;
                            has_fixed = true;
                            I lb = g2local_b[v];
                            held_h[2*v] = curr_border_uv[2*lb];
                            held_h[2*v+1] = curr_border_uv[2*lb+1];
                            inv_diag_h[2*v] = 0.0;
                            inv_diag_h[2*v+1] = 0.0;
                            ++pinned_border_count;
                            added = true;
                        }
                        if(added) {
                            problem.witness_i = -1; problem.witness_j = -1;
                            hist_len = 0;
                            drv.check(drv.upload(c.fixed_u8, fixed_h.data(), std::size_t(p.n), st), "pin_fix");
                            drv.check(drv.upload(c.held_d, held_h.data(), 2*p.n*sizeof(double), st), "pin_held");
                            drv.check(drv.upload(c.inv_diag_d, inv_diag_h.data(), 2*p.n*sizeof(double), st), "pin_id");
                            if(is_f32) {
                                int cnt2n = n2;
                                void* a1[] = {&cnt2n, &c.held_d, &c.held_f};
                                drv.check(drv.launch(drv.cast_d2f, g_2n, 1, 1, 256, 1, 1, 0, st, a1, nullptr), "pin_hf");
                                void* a2[] = {&cnt2n, &c.inv_diag_d, &c.inv_diag_f};
                                drv.check(drv.launch(drv.cast_d2f, g_2n, 1, 1, 256, 1, 1, 0, st, a2, nullptr), "pin_idf");
                            }
                            int n_v = ni;
                            void* za[] = {&n_v, &c.fixed_u8, &g_ptr};
                            drv.check(drv.launch(k_zf, g_n, 1, 1, 256, 1, 1, 0, st, za, nullptr), "pin_zg");
                            drv.check(drv.wait(st), "pin_w");
                        }
                        return added;
                    };

                    for(I iter = 0; iter < phase_limit; ++iter) {
                        if(p.automatic && g_inf < grad_tol) break;
                        // Copy g -> dir
                        {
                            int n2_v = n2;
                            void* ca[] = {&n2_v, &g_ptr, &dir_ptr};
                            drv.check(drv.launch(k_cp, g_2n, 1, 1, 256, 1, 1, 0, st, ca, nullptr), "cp_dir");
                        }
                        // Zero-sync L-BFGS two-loop
                        for(int idx = hist_len - 1; idx >= 0; --idx) {
                            int slot = ring_slots[idx], n2_v = n2, nb_v = rb_2n_i;
                            CUdeviceptr sp = s_hist[slot], yp = y_hist[slot];
                            void* da[] = {&n2_v, &sp, &dir_ptr, &c.block_buf0};
                            drv.check(drv.launch(k_dot, rb_2n, 1, 1, 256, 1, 1, 0, st, da, nullptr), "dot_b");
                            void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &slot};
                            drv.check(drv.launch(drv.reduce_alpha, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "red_a");
                            int aslot = SC_ALPHA_BASE + slot; double sgn = -1.0;
                            void* aa[] = {&n2_v, &dir_ptr, &yp, &c.scalars, &aslot, &sgn};
                            drv.check(drv.launch(k_axpy, g_2n, 1, 1, 256, 1, 1, 0, st, aa, nullptr), "axpy_b");
                        }
                        {
                            int n2_v = n2, use_g = hist_len > 0 ? 1 : 0;
                            double isc = init_scale, mdisp = max_disp;
                            void* sa[] = {&n2_v, &dir_ptr, &inv_diag_ptr, &c.scalars, &use_g, &isc, &mdisp};
                            drv.check(drv.launch(k_sdiag, g_2n, 1, 1, 256, 1, 1, 0, st, sa, nullptr), "sdiag");
                        }
                        for(int idx = 0; idx < hist_len; ++idx) {
                            int slot = ring_slots[idx], n2_v = n2, nb_v = rb_2n_i;
                            CUdeviceptr sp = s_hist[slot], yp = y_hist[slot];
                            void* da[] = {&n2_v, &yp, &dir_ptr, &c.block_buf0};
                            drv.check(drv.launch(k_dot, rb_2n, 1, 1, 256, 1, 1, 0, st, da, nullptr), "dot_f");
                            void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &slot};
                            drv.check(drv.launch(drv.reduce_beta, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "red_b");
                            int bslot = SC_BETA; double sgn = 1.0;
                            void* aa[] = {&n2_v, &dir_ptr, &sp, &c.scalars, &bslot, &sgn};
                            drv.check(drv.launch(k_axpy, g_2n, 1, 1, 256, 1, 1, 0, st, aa, nullptr), "axpy_f");
                        }
                        {
                            int n2_v = n2, nb_v = rb_2n_i, sl = SC_SLOPE;
                            void* na[] = {&n2_v, &dir_ptr, &g_ptr, &c.block_buf0};
                            drv.check(drv.launch(k_ndot, rb_2n, 1, 1, 256, 1, 1, 0, st, na, nullptr), "ndot");
                            void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &sl};
                            drv.check(drv.launch(drv.reduce_sum_to_slot, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "rslope");
                        }
                        // Speculative trial 0 fused into same stream submission
                        {
                            int n2_v = n2; double mdisp = max_disp;
                            void* ta[] = {&n2_v, &x_ptr, &dir_ptr, &g_ptr, &inv_diag_ptr, &c.scalars, &mdisp, &cand_ptr};
                            drv.check(drv.launch(k_ct0, g_2n, 1, 1, 256, 1, 1, 0, st, ta, nullptr), "ct0");
                        }
                        l_project(cand_ptr);
                        l_energy(cand_ptr, check_border);
                        sync_sc(12);

                        double slope = c.staging_scalars[SC_SLOPE];
                        if(c.staging_scalars[SC_RESET_FLAG] > 0.5) {
                            hist_len = 0;
                            int n2_v = n2, nb_v = rb_2n_i, sl = SC_SLOPE;
                            void* da[] = {&n2_v, &g_ptr, &dir_ptr, &c.block_buf0};
                            drv.check(drv.launch(k_dot, rb_2n, 1, 1, 256, 1, 1, 0, st, da, nullptr), "dot_rst");
                            void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &sl};
                            drv.check(drv.launch(drv.reduce_sum_to_slot, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "rrst");
                            sync_sc(12);
                            slope = c.staging_scalars[SC_SLOPE];
                        }

                        problem.border_rejected = false;
                        problem.flip_rejected = false;
                        bool border_ok = !check_border || check_compact_borders(c.staging_border, base_b);
                        double next_e = border_ok ? c.staging_scalars[SC_ENERGY] : INF;
                        if(border_ok && !std::isfinite(next_e)) problem.flip_rejected = true;
                        double step = 1.0;
                        bool accepted = (border_ok && std::isfinite(next_e) && next_e <= e + 1e-4*step*slope);
                        bool used_analytic = false, used_border_ccd = false;
                        const bool analytic_bound = (p.prevent_flips || problem.reciprocal) && (problem.soft || problem.axes_only);

                        const int max_search = is_f32 ? 8 : 32;
                        for(int search = 1; !accepted && search < max_search; ++search) {
                            if(search == 1 && !border_ok && !used_border_ccd && problem.witness_i >= 0) {
                                used_border_ccd = true;
                                double t_ccd = compact_border_ccd(curr_border_uv.data(), c.staging_border, problem.witness_i, problem.witness_j);
                                step = (t_ccd < 1.0) ? std::min(0.5, std::max(0.75*t_ccd, 1e-12)) : 0.5;
                            } else if(search == 1 && border_ok && problem.flip_rejected && !used_analytic && analytic_bound) {
                                used_analytic = true;
                                int nti_v = nti, nb_v = rb_nt_i, sl = SC_TMIN;
                                void* fa[] = {&nti_v, &c.tri_i32, &x_ptr, &cand_ptr, &c.block_buf0};
                                drv.check(drv.launch(k_mfs, rb_nt, 1, 1, 256, 1, 1, 0, st, fa, nullptr), "mfs");
                                void* ra[] = {&nb_v, &c.block_buf0, &c.scalars, &sl};
                                drv.check(drv.launch(drv.reduce_min_to_slot, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "rmfs");
                                sync_sc(12);
                                double t_min = c.staging_scalars[SC_TMIN];
                                step = (t_min < 1.0) ? std::min(0.5, std::max(0.8*t_min, 1e-12)) : 0.5;
                            } else {
                                step *= 0.5;
                            }
                            if(is_f32 && step < 1e-4) break;
                            int n2_v = n2;
                            if(is_f32) {
                                float sf = static_cast<float>(step);
                                void* ca[] = {&n2_v, &x_ptr, &dir_ptr, &sf, &cand_ptr};
                                drv.check(drv.launch(k_cstep, g_2n, 1, 1, 256, 1, 1, 0, st, ca, nullptr), "cs_f");
                            } else {
                                double sd = step;
                                void* ca[] = {&n2_v, &x_ptr, &dir_ptr, &sd, &cand_ptr};
                                drv.check(drv.launch(k_cstep, g_2n, 1, 1, 256, 1, 1, 0, st, ca, nullptr), "cs_d");
                            }
                            l_project(cand_ptr);
                            l_energy(cand_ptr, check_border);
                            sync_sc(12);
                            problem.border_rejected = false;
                            problem.flip_rejected = false;
                            border_ok = !check_border || check_compact_borders(c.staging_border, base_b);
                            next_e = border_ok ? c.staging_scalars[SC_ENERGY] : INF;
                            if(border_ok && !std::isfinite(next_e)) problem.flip_rejected = true;
                            if(border_ok && std::isfinite(next_e) && next_e <= e + 1e-4*step*slope) {
                                accepted = true;
                                break;
                            }
                        }

                        if(!accepted) {
                            if(is_f32) break; // Hand off immediately to FP64 polish on FP32 step limit
                            if(try_pin_gpu()) continue;
                            if(hist_len == 0) break;
                            hist_len = 0;
                            continue;
                        }

                        int next_slot = (hist_len < GPU_HIST_LIMIT) ? hist_len : ring_slots[0];
                        l_grad(cand_ptr, next_g_ptr);
                        {
                            int n2_v = n2, nb_v = rb_2n_i;
                            CUdeviceptr sp = s_hist[next_slot], yp = y_hist[next_slot];
                            void* sa[] = {&n2_v, &cand_ptr, &x_ptr, &next_g_ptr, &g_ptr, &inv_diag_ptr, &sp, &yp, &c.block_buf0, &c.block_buf1, &c.block_buf2};
                            drv.check(drv.launch(k_sy, rb_2n, 1, 1, 256, 1, 1, 0, st, sa, nullptr), "sy_blk");
                            void* ra[] = {&nb_v, &c.block_buf0, &c.block_buf1, &c.block_buf2, &c.scalars, &next_slot};
                            drv.check(drv.launch(drv.reduce_sy_ymy_maxg, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "rsy");
                        }
                        sync_sc(12);
                        const double sy = c.staging_scalars[SC_SY], ymy = c.staging_scalars[SC_YMY];
                        if(sy > 1e-14 && std::isfinite(sy) && std::isfinite(ymy)) {
                            if(hist_len < GPU_HIST_LIMIT) {
                                ring_slots[hist_len++] = next_slot;
                            } else {
                                for(int k = 0; k < GPU_HIST_LIMIT - 1; ++k) ring_slots[k] = ring_slots[k+1];
                                ring_slots[GPU_HIST_LIMIT - 1] = next_slot;
                            }
                        }
                        std::swap(x_ptr, cand_ptr);
                        std::swap(g_ptr, next_g_ptr);
                        if(check_border) std::memcpy(curr_border_uv.data(), c.staging_border, 2*nv_b*sizeof(double));
                        double improvement = e - next_e;
                        e = next_e;
                        g_inf = c.staging_scalars[SC_MAX_G];
                        ++phase_accepted;
                        ++total_accepted;
                        out_e = e; out_ginf = g_inf;

                        if(p.automatic && improvement <= imp_tol * std::max(1.0, std::abs(e))) {
                            if(try_pin_gpu()) continue;
                            break;
                        }
                        if(p.automatic && (!problem.soft || overlap_progress) && phase_accepted % 25 == 0) {
                            double reduction = checkpoint_energy - e;
                            checkpoint_energy = e;
                            double base_tol = (p.nt >= 32768) ? 0.015 : 0.01;
                            double tolerance = overlap_progress ? 0.0001 : base_tol;
                            if(is_f32) tolerance = std::max(tolerance, 0.005);
                            bool direction_ready = true;
                            Vec host_x_chk;
                            auto ensure_host_x = [&]() {
                                if(!host_x_chk.empty()) return;
                                host_x_chk.resize(2*p.n);
                                if(is_f32) {
                                    int cnt2n = n2;
                                    void* ca[] = {&cnt2n, &x_ptr, &c.uv};
                                    drv.check(drv.launch(drv.cast_f2d, g_2n, 1, 1, 256, 1, 1, 0, st, ca, nullptr), "chk_f2d");
                                    drv.check(drv.download(c.staging_out, c.uv, 2*p.n*sizeof(double), st), "dl_chk");
                                } else {
                                    drv.check(drv.download(c.staging_out, x_ptr, 2*p.n*sizeof(double), st), "dl_chk");
                                }
                                drv.check(drv.wait(st), "w_chk");
                                gpu_counts[5] += 2*p.n*sizeof(double);
                                std::memcpy(host_x_chk.data(), c.staging_out, 2*p.n*sizeof(double));
                            };
                            if(overlap_progress && p.strength >= 1e6) {
                                ensure_host_x();
                                const double tangent = std::tan(0.045*3.14159265358979323846/180.0);
                                for(I e_idx = 0; e_idx < p.ne; ++e_idx) {
                                    I a = p.penalty_edges[2*e_idx], b = p.penalty_edges[2*e_idx+1], ax = p.penalty_axis[e_idx];
                                    double along = std::abs(host_x_chk[2*b+1-ax] - host_x_chk[2*a+1-ax]);
                                    double across = std::abs(host_x_chk[2*b+ax] - host_x_chk[2*a+ax]);
                                    if(along <= 1e-12 || across > tangent*along) { direction_ready = false; break; }
                                }
                            }
                            if(early_subspace && total_accepted >= 25) {
                                ensure_host_x();
                                if(max_pen_tan(host_x_chk) < initial_max_tan - 1e-5 && problem.hard_subspace_ready(host_x_chk)) {
                                    hit_early_subspace = true;
                                    break;
                                }
                            }
                            if(direction_ready && (!overlap_progress || total_accepted >= 100) && reduction >= 0.0 &&
                                    reduction <= tolerance * std::max(std::abs(e), 1e-12)) {
                                if(g_inf > 1e-3 && try_pin_gpu()) continue;
                                break;
                            }
                        }
                    }
                };

                // Phase 1: FP32 GPU-Resident L-BFGS Sprint (skip on stiff penalty >= 1e4 where FP32 hits 7-digit wall)
                const bool use_f32_sprint = !(problem.soft && p.strength >= 1e4);
                const I f64_reserve = p.automatic ? 250 : std::min<I>(12, std::max<I>(4, limit / 4));
                const I f32_limit = use_f32_sprint ? (p.automatic ? std::min<I>(limit, 75) : std::max<I>(0, limit - f64_reserve)) : 0;
                CUdeviceptr x_d_cur = c.x_d, cand_d_cur = c.cand_d, g_d_cur = c.g_d, ng_d_cur = c.next_g_d;
                double final_e = e_d0, final_ginf = INF;

                if(f32_limit > 0) {
                    int cnt2n = n2;
                    void* ca[] = {&cnt2n, &c.x_d, &c.x_f};
                    drv.check(drv.launch(drv.cast_d2f, g_2n, 1, 1, 256, 1, 1, 0, st, ca, nullptr), "init_d2f");
                    CUdeviceptr x_f_cur = c.x_f, cand_f_cur = c.cand_f, g_f_cur = c.g_f, ng_f_cur = c.next_g_f;
                    double e_f = INF, g_f = INF;
                    run_phase(true, x_f_cur, cand_f_cur, g_f_cur, ng_f_cur, c.dir_f, c.inv_diag_f, c.held_f,
                              c.s_hist_f, c.y_hist_f, f32_limit, e_f, g_f);
                    // Verified FP32 -> FP64 Handoff
                    void* ca2[] = {&cnt2n, &x_f_cur, &cand_d_cur};
                    drv.check(drv.launch(drv.cast_f2d, g_2n, 1, 1, 256, 1, 1, 0, st, ca2, nullptr), "ho_f2d");
                    if(nsets > 0) {
                        int ns_v = nsets;
                        void* pa[] = {&ns_v, &c.set_offsets, &c.set_members, &c.set_axis, &c.set_anchored, &c.set_anchor, &cand_d_cur};
                        drv.check(drv.launch(drv.project_axes_d, GpuSession::grid256(nsets), 1, 1, 256, 1, 1, 0, st, pa, nullptr), "ho_pa");
                    }
                    if(has_fixed) {
                        int n_v = ni;
                        void* fa[] = {&n_v, &c.fixed_u8, &c.held_d, &cand_d_cur};
                        drv.check(drv.launch(drv.apply_fixed_d, g_n, 1, 1, 256, 1, 1, 0, st, fa, nullptr), "ho_af");
                    }
                    int nti_v = nti, rec_v = rec_i, fl_v = flip_i; double ang_v = angle_d;
                    void* ea[] = {&nti_v, &cand_d_cur, &c.tri_i32, &c.inverse, &c.j, &c.weights, &rec_v, &fl_v, &ang_v, &c.block_buf0, &c.block_inv};
                    drv.check(drv.launch(drv.energy_block_d, rb_nt, 1, 1, 256, 1, 1, 0, st, ea, nullptr), "ho_ed");
                    int nb_v = rb_nt_i;
                    void* ra[] = {&nb_v, &c.block_buf0, &c.block_inv, &c.scalars};
                    drv.check(drv.launch(drv.reduce_energy_final, 1, 1, 1, 256, 1, 1, 0, st, ra, nullptr), "ho_rd");
                    if(nei > 0) {
                        int ne_v = nei; unsigned rbn = GpuSession::reduce_grid(nei); int rbn_i = int(rbn);
                        void* pa[] = {&ne_v, &cand_d_cur, &c.pen_edges, &c.pen_axis, &c.pen_w_d, &c.block_buf0};
                        drv.check(drv.launch(drv.penalty_energy_block_d, rbn, 1, 1, 256, 1, 1, 0, st, pa, nullptr), "ho_pe");
                        void* pra[] = {&rbn_i, &c.block_buf0, &c.scalars};
                        drv.check(drv.launch(drv.add_pen_energy, 1, 1, 1, 256, 1, 1, 0, st, pra, nullptr), "ho_ape");
                    }
                    if(check_border && nv_b > 0) {
                        int nvb_v = nv_b;
                        void* ga[] = {&nvb_v, &c.border_verts, &cand_d_cur, &c.border_out};
                        drv.check(drv.launch(drv.gather_border_d, GpuSession::grid256(nv_b), 1, 1, 256, 1, 1, 0, st, ga, nullptr), "ho_gb");
                        drv.check(drv.download(c.staging_border, c.border_out, 2*nv_b*sizeof(double), st), "ho_dlb");
                    }
                    drv.check(drv.download(c.staging_scalars, c.scalars, 2*sizeof(double), st), "ho_dl");
                    drv.check(drv.wait(st), "ho_w");
                    ++gpu_counts[1]; ++problem.evaluations;
                    const double* base_b = keep_initial_border ? init_border_uv.data() : nullptr;
                    const bool ho_border_ok = !check_border || check_compact_borders(c.staging_border, base_b);
                    const double e_ho = c.staging_scalars[SC_ENERGY];
                    if(ho_border_ok && std::isfinite(e_ho) && e_ho <= e_d0) {
                        std::swap(x_d_cur, cand_d_cur);
                        final_e = e_ho;
                    } else {
                        hit_early_subspace = false;
                        total_accepted = 0;
                        // Roll back any contact pins from rejected FP32 sprint
                        if(pinned_border_count > 0) {
                            fixed_h.assign(p.fixed, p.fixed + p.n);
                            held_h.assign(p.held, p.held + 2*p.n);
                            contact_pinned.assign(contact_pinned.size(), 0);
                            pinned_border_count = 0;
                            has_fixed = std::any_of(fixed_h.begin(), fixed_h.end(), [](std::uint8_t v){ return v != 0; });
                            drv.check(drv.upload(c.fixed_u8, fixed_h.data(), std::size_t(p.n), st), "rb_fix");
                            drv.check(drv.upload(c.held_d, held_h.data(), 2*p.n*sizeof(double), st), "rb_held");
                            drv.check(drv.wait(st), "rb_w");
                        }
                    }
                }

                // Phase 2: FP64 GPU-Resident Exact Polish
                const I f64_limit = hit_early_subspace ? 0 : (p.automatic ? std::max<I>(250, limit - total_accepted) : f64_reserve);
                run_phase(false, x_d_cur, cand_d_cur, g_d_cur, ng_d_cur, c.dir_d, c.inv_diag_d, c.held_d,
                          c.s_hist_d, c.y_hist_d, f64_limit, final_e, final_ginf);

                // Download final FP64 UVs once
                drv.check(drv.download(c.staging_out, x_d_cur, 2*p.n*sizeof(double), st), "dl_final");
                drv.check(drv.wait(st), "w_final");
                gpu_counts[5] += 2*p.n*sizeof(double);
                const auto* out_src = static_cast<const double*>(c.staging_out);
                for(I k = 0; k < 2*p.n; ++k) if(!std::isfinite(out_src[k])) return -2;
                std::memcpy(output, out_src, 2*p.n*sizeof(double));
                stats->accepted = total_accepted;
                stats->evaluations = problem.evaluations;
                stats->reciprocal = problem.reciprocal;
                stats->finite = 1;
                stats->energy = final_e;
                stats->gradient_max = final_ginf;
                return 0;
            } catch(const std::exception& error) {
                gpu_failure(error);
                problem.gpu.reset();
            }
        }
        double e=problem.energy(x);*stats={0,problem.evaluations,problem.reciprocal,0,e,INF};
        if(!std::isfinite(e)) return 1; // Do not publish even the initial projection.
        Vec g,next_g;
        problem.gradient(x,g);
        Vec direction(x.size()),candidate(x.size()),change(x.size()),y(x.size());
        constexpr std::size_t history_limit=8;
        struct History {Vec s,y;double rho,sy,yy;};
        std::vector<History> history;
        history.reserve(history_limit);
        std::array<double,history_limit> coefficients{};
        const bool overlap_progress=problem.soft && p.prevent_overlap==1;
        double checkpoint_energy=e;
        const bool keep_initial_border=(p.prevent_overlap==1 && p.overlap_space<=0.);
        const Vec initial_uv=keep_initial_border?x:Vec{};
        const bool allow_contact_pin=(p.prevent_overlap==1 && !problem.soft);
        std::vector<std::uint8_t> contact_pinned(allow_contact_pin?p.n:0,0);
        std::vector<std::uint8_t> constrained_vert(allow_contact_pin?p.n:0,0);
        if(allow_contact_pin) {
            for(I k=0;k<p.nm;++k) constrained_vert[p.members[k]]=1;
        }
        I pinned_border_count=0;
        auto try_pin_witness=[&]() -> bool {
            if(!allow_contact_pin || !problem.border_rejected || problem.witness_i<0 || pinned_border_count>=16)
                return false;
            const I verts[4]={
                p.border_edges[2*problem.witness_i],p.border_edges[2*problem.witness_i+1],
                p.border_edges[2*problem.witness_j],p.border_edges[2*problem.witness_j+1]
            };
            bool added=false;
            for(I v:verts) if(!contact_pinned[v] && !constrained_vert[v] && !p.fixed[v]) {
                contact_pinned[v]=1;
                g[2*v]=0.;g[2*v+1]=0.;
                ++pinned_border_count;
                added=true;
            }
            if(added) {problem.witness_i=-1;problem.witness_j=-1;history.clear();}
            return added;
        };
        const bool early_subspace = (p.automatic && problem.soft && problem.axes_only
            && p.nr>0 && p.ne>0 && p.prevent_overlap==1 && p.strength<1e6);
        auto max_penalty_angle_tan = [&](const Vec& v) -> double {
            double worst=0.;
            for(I edge=0;edge<p.ne;++edge) {
                I a=p.penalty_edges[2*edge],b=p.penalty_edges[2*edge+1];
                I axis=p.penalty_axis[edge];
                double along=std::abs(v[2*b+1-axis]-v[2*a+1-axis]);
                double across=std::abs(v[2*b+axis]-v[2*a+axis]);
                if(along<=1e-12) return INF;
                worst=std::max(worst,across/along);
            }
            return worst;
        };
        const double initial_max_tan = early_subspace ? max_penalty_angle_tan(x) : 0.;
        I limit=p.automatic?1000:std::max<I>(1,p.iterations);
        for(I iteration=0;iteration<limit;++iteration) {
            if(p.automatic && max_abs(g)<1e-6) break;
            direction=g;
            if(!history.empty()) {
                std::size_t i=history.size()-1;
                coefficients[i]=history[i].rho*dot(history[i].s,direction);
                while(i>0) {
                    const auto& current=history[i];
                    const auto& next=history[i-1];
                    // Apply this pair while reading the vector for the next dot,
                    // keeping the original element order of its scalar sum.
                    coefficients[i-1]=next.rho*update_and_dot<true>(
                        direction,current.y,coefficients[i],next.s);
                    --i;
                }
                for(std::size_t k=0;k<direction.size();++k)
                    direction[k]-=coefficients[0]*history.front().y[k];
            }
            double scale=history.empty()?std::max(p.density*p.density,1e-6)*.05
                :history.back().sy/std::max(history.back().yy,1e-20);
            if(history.empty()) {
                const double g_inf=max_abs(g);
                const double max_disp=.05*std::sqrt(std::max(p.penalty_scale,1e-20));
                if(g_inf>0. && max_disp>0. && scale*g_inf>max_disp) scale=max_disp/g_inf;
            }
            for(double& v:direction) v*=scale;
            if(!history.empty()) {
                double v=coefficients[0]-history.front().rho*dot(history.front().y,direction);
                for(std::size_t i=1;i<history.size();++i) {
                    const double next_dot=update_and_dot<false>(direction,history[i-1].s,v,history[i].y);
                    v=coefficients[i]-history[i].rho*next_dot;
                }
                for(std::size_t k=0;k<direction.size();++k)
                    direction[k]+=history.back().s[k]*v;
            }
            for(double& v:direction) v=-v;
            double slope=dot(g,direction);
            if(slope>=0) {
                const double g_inf=max_abs(g);
                const double max_disp=.05*std::sqrt(std::max(p.penalty_scale,1e-20));
                const double reset_scale=(g_inf>max_disp && max_disp>0.)?(max_disp/g_inf):1.;
                for(std::size_t k=0;k<g.size();++k) direction[k]=-reset_scale*g[k];
                slope=dot(g,direction);
                history.clear();
            }
            double step=1,next_e=INF;
            bool accepted=false,used_analytic=false,used_border_ccd=false;
            const bool analytic_bound=(p.nt>=32768) && (p.prevent_flips || problem.reciprocal) && (problem.soft || problem.axes_only);
            problem.baseline_uv=keep_initial_border?&initial_uv:nullptr;
            problem.border_rejected=false;
            problem.flip_rejected=false;
            for(int search=0;search<32;++search) {
                for(std::size_t k=0;k<x.size();++k) candidate[k]=x[k]+step*direction[k];
                problem.project(candidate);
                const double threshold=e+1e-4*step*slope;
                if(std::isfinite(threshold) && problem.rejects_at_previous_triangle(candidate)) {
                    // The full kernel would return invalid energy. Count the
                    // trial and follow the same rejection/halving policy.
                    ++problem.evaluations;
                    if(search==0 && !used_analytic && problem.flip_rejected && !problem.border_rejected && analytic_bound) {
                        used_analytic=true;
                        const double t_min=problem.max_flip_free_step(x,candidate);
                        step=(t_min<1.)?std::min(0.5,std::max(0.8*t_min,1e-12)):0.5;
                    } else {
                        step*=.5;
                    }
                    continue;
                }
                if(p.prevent_overlap==1 && problem.witness_i>=0 && std::isfinite(threshold) &&
                        !problem.border_pair_valid(candidate,problem.witness_i,problem.witness_j)) {
                    problem.border_rejected=true;
                    ++problem.evaluations;
                    if(search==0 && !used_border_ccd) {
                        used_border_ccd=true;
                        const double t_ccd=problem.max_border_pair_step(x,candidate,problem.witness_i,problem.witness_j);
                        step=(t_ccd<1.)?std::min(0.5,std::max(0.75*t_ccd,1e-12)):0.5;
                    } else {
                        step*=.5;
                    }
                    continue;
                }
                next_e=problem.energy(candidate);
                if(next_e<=threshold) {accepted=true;break;}
                if(search==0 && !used_border_ccd && problem.border_rejected && problem.witness_i>=0) {
                    used_border_ccd=true;
                    const double t_ccd=problem.max_border_pair_step(x,candidate,problem.witness_i,problem.witness_j);
                    step=(t_ccd<1.)?std::min(0.5,std::max(0.75*t_ccd,1e-12)):0.5;
                } else if(search==0 && !used_analytic && problem.flip_rejected && !problem.border_rejected && analytic_bound) {
                    used_analytic=true;
                    const double t_min=problem.max_flip_free_step(x,candidate);
                    step=(t_min<1.)?std::min(0.5,std::max(0.8*t_min,1e-12)):0.5;
                } else {
                    step*=.5;
                }
            }
            problem.baseline_uv=nullptr;
            if(!accepted) {
                if(try_pin_witness()) continue;
                if(history.empty()) break;
                history.clear();
                continue; // retry from x with the capped steepest-descent step
            }
            problem.gradient(candidate,next_g);
            if(pinned_border_count>0) {
                for(I v=0;v<p.n;++v) if(contact_pinned[v]) {next_g[2*v]=0.;next_g[2*v+1]=0.;}
            }
            double sy=0,yy=0;
            for(std::size_t k=0;k<x.size();++k) {
                change[k]=candidate[k]-x[k];
                y[k]=next_g[k]-g[k];
                sy+=change[k]*y[k];
                yy+=y[k]*y[k];
            }
            if(sy>1e-14) {
                History entry{std::move(change),std::move(y),1/sy,sy,yy};
                if(history.size()==history_limit) {
                    // Reuse the buffers of the history pair that is leaving the window.
                    change=std::move(history.front().s);
                    y=std::move(history.front().y);
                    history.erase(history.begin());
                } else {
                    change.resize(x.size());
                    y.resize(x.size());
                }
                history.push_back(std::move(entry));
            }
            double improvement=e-next_e;
            x.swap(candidate);
            e=next_e;
            g.swap(next_g);
            ++stats->accepted;
            if(p.automatic && improvement<=1e-10*std::max(1.,std::abs(e))) {
                if(try_pin_witness()) continue;
                break;
            }
            if(p.automatic && (!problem.soft || overlap_progress) && stats->accepted%25==0) {
                double reduction=checkpoint_energy-e;
                checkpoint_energy=e;
                double base_tol=(p.nt>=32768)?0.015:0.01;
                double tolerance=overlap_progress?0.0001:base_tol;
                bool direction_ready=true;
                if(overlap_progress && p.strength>=1e6) {
                    const double tangent=std::tan(0.045*3.14159265358979323846/180.);
                    for(I edge=0;edge<p.ne;++edge) {
                        I a=p.penalty_edges[2*edge],b=p.penalty_edges[2*edge+1];
                        I axis=p.penalty_axis[edge];
                        double along=std::abs(x[2*b+1-axis]-x[2*a+1-axis]);
                        double across=std::abs(x[2*b+axis]-x[2*a+axis]);
                        if(along<=1e-12 || across>tangent*along) {direction_ready=false;break;}
                    }
                }
                if(early_subspace && stats->accepted>=25 &&
                        max_penalty_angle_tan(x)<initial_max_tan-1e-5 &&
                        problem.hard_subspace_ready(x)) {
                    break;
                }
                if(direction_ready && (!overlap_progress || stats->accepted>=100) && reduction>=0 &&
                        reduction<=tolerance*std::max(std::abs(e),1e-12)) {
                    if(max_abs(g)>1e-3 && try_pin_witness()) continue;
                    break;
                }
            }
        }
        for(double v:x) if(!std::isfinite(v)) return -2;
        std::copy(x.begin(),x.end(),output);stats->evaluations=problem.evaluations;stats->finite=1;stats->energy=e;stats->gradient_max=max_abs(g);return 0;
    } catch(...) {return -3;}
}
API int uvck_solve(const Input* input,double* output,Output* stats) noexcept {
    if(!stats) return -1;
    Output temporary{};
    int status=uvck_solve_impl(input,output,&temporary);
    if(status==0 || status==1 || status==-2) *stats=temporary;
    return status;
}
// Test entry point: evaluate the same objective at a supplied point, without projection.
API int uvck_evaluate(const Input* input,I reciprocal,double* energy,double* gradient) noexcept {
    try {if(!input || !energy || !gradient || !valid_input(*input)) return -1;
        Problem p(*input);p.init_gpu();p.reciprocal=reciprocal!=0;Vec x(input->uv,input->uv+2*input->n);
        double e=p.energy(x);if(!std::isfinite(e)) {*energy=e;return 1;}
        Vec g;p.gradient(x,g);*energy=e;std::copy(g.begin(),g.end(),gradient);return 0;
    }catch(...){return -3;}
}
API int uvck_deform(I n,I ne,const I* edges,const double* weights,const double* degree,const std::uint8_t* fixed,double* displacement) noexcept {
    try {if(n<=0 || ne<0 || !edges || !weights || !degree || !fixed || !displacement) return -1;
        for(I e=0;e<2*ne;++e) if(edges[e]<0 || edges[e]>=n) return -1;
        Vec current(displacement,displacement+2*n),next(2*n),summed(2*n);
        for(int iteration=0;iteration<400;++iteration) {
            std::fill(summed.begin(),summed.end(),0.);
            for(int end=0;end<2;++end) for(I e=0;e<ne;++e) {I a=edges[2*e+end],b=edges[2*e+1-end];for(int axis=0;axis<2;++axis) summed[2*a+axis]+=current[2*b+axis]*weights[e];}
            double change=0;
            for(I i=0;i<n;++i) for(int axis=0;axis<2;++axis) {I k=2*i+axis;next[k]=fixed[i]?current[k]:summed[k]/std::max(degree[i],1e-12);change=std::max(change,std::abs(next[k]-current[k]));}
            current.swap(next);if(change<1e-9) break;
        }
        for(double v:current) if(!std::isfinite(v)) return -2;
        std::copy(current.begin(),current.end(),displacement);return 0;
    }catch(...){return -3;}
}
// Per-corner UV unions preserve the Python ordered union's representative IDs.
API int uvck_uv_groups(I n,const I* vertices,const double* uv,double epsilon,I* output) noexcept {
    try {if(n<0 || !vertices || !uv || !output || epsilon<0 || !std::isfinite(epsilon)) return -1;
        std::vector<I> parent(n);std::iota(parent.begin(),parent.end(),0);std::map<I,std::vector<I>> groups;
        for(I i=0;i<n;++i) groups[vertices[i]].push_back(i);
        auto root=[&](I i) {while(parent[i]!=i) {parent[i]=parent[parent[i]];i=parent[i];}return i;};
        for(const auto& item:groups) {const auto& members=item.second;
            for(std::size_t a=0;a<members.size();++a) for(std::size_t b=a+1;b<members.size();++b) {
                I i=members[a],j=members[b];if(std::abs(uv[2*i]-uv[2*j])<=epsilon && std::abs(uv[2*i+1]-uv[2*j+1])<=epsilon) parent[root(i)]=root(j);
            }
        }
        for(I i=0;i<n;++i) output[i]=root(i);return 0;
    }catch(...){return -3;}
}

// Face connectivity uses radial-loop order, retaining Python's exact root labels.
API int uvck_islands(I nf,I nl,I ne,const I* offsets,const I* members,
                     const I* faces,const I* following,const double* uv,double eps,I* output) noexcept {
    try {
        if(nf<0 || nl<0 || ne<0 || !offsets || !members || !faces || !following || !uv || !output || !std::isfinite(eps) || eps<0) return -1;
        if(offsets[0]!=0 || offsets[ne]!=nl) return -1;
        for(I i=0;i<nl;++i) if(faces[i]<0 || faces[i]>=nf || following[i]<0 || following[i]>=nl || members[i]<0 || members[i]>=nl) return -1;
        for(I e=0;e<ne;++e) if(offsets[e]<0 || offsets[e]>offsets[e+1] || offsets[e+1]>nl) return -1;
        std::vector<I> parent(nf);std::iota(parent.begin(),parent.end(),0);
        auto root=[&](I i) {while(parent[i]!=i) {parent[i]=parent[parent[i]];i=parent[i];}return i;};
        auto close=[&](I a,I b) {return std::abs(uv[2*a]-uv[2*b])<=eps && std::abs(uv[2*a+1]-uv[2*b+1])<=eps;};
        for(I e=0;e<ne;++e) for(I i=offsets[e];i<offsets[e+1];++i) for(I j=i+1;j<offsets[e+1];++j) {
            I a=members[i],b=members[j],an=following[a],bn=following[b];
            if((close(a,b) && close(an,bn)) || (close(a,bn) && close(an,b))) {
                I ra=root(faces[a]),rb=root(faces[b]);if(ra!=rb) parent[ra]=rb;
            }
        }
        for(I i=0;i<nf;++i) output[i]=root(i);return 0;
    }catch(...){return -3;}
}

struct Point {double x,y;};
using Triangle=std::array<Point,3>;
static bool equal_point(const Point& a,const Point& b) {return a.x==b.x && a.y==b.y;}
static double polygon_area(const std::vector<Point>& points) {
    double area=0;
    for(std::size_t i=0;i<points.size();++i) {const auto& a=points[i];const auto& b=points[(i+1)%points.size()];area+=a.x*b.y-b.x*a.y;}
    return area*.5;
}
static double side(const Point& a,const Point& b,const Point& p) {return (b.x-a.x)*(p.y-a.y)-(b.y-a.y)*(p.x-a.x);}
static double triangle_intersection(const Triangle& a,const Triangle& b) {
    std::vector<Point> shared;
    for(const auto& p:a) {
        bool in_b=false,seen=false;
        for(const auto& q:b) in_b|=equal_point(p,q);
        for(const auto& q:shared) seen|=equal_point(p,q);
        if(in_b && !seen) shared.push_back(p);
    }
    if(shared.size()==2) {
        const Point *u=nullptr,*v=nullptr;
        for(const auto& p:a) if(!equal_point(p,shared[0]) && !equal_point(p,shared[1])) {u=&p;break;}
        for(const auto& p:b) if(!equal_point(p,shared[0]) && !equal_point(p,shared[1])) {v=&p;break;}
        if(!u || !v || side(shared[0],shared[1],*u)*side(shared[0],shared[1],*v)<=0) return 0;
    }
    std::vector<Point> poly(a.begin(),a.end()),clip(b.begin(),b.end()),next;
    if(polygon_area(poly)<0) std::reverse(poly.begin(),poly.end());
    if(polygon_area(clip)<0) std::reverse(clip.begin(),clip.end());
    next.reserve(12);
    for(std::size_t edge=0;edge<3;++edge) {
        const auto& v=clip[edge];const auto& w=clip[(edge+1)%3];next.clear();
        for(std::size_t i=0;i<poly.size();++i) {
            const auto& p=poly[i];const auto& q=poly[(i+1)%poly.size()];
            double sp=side(v,w,p),sq=side(v,w,q);
            if(sp>=-1e-12) next.push_back(p);
            if((sp>1e-12 && sq< -1e-12) || (sp< -1e-12 && sq>1e-12)) {
                double t=sp/(sp-sq);next.push_back({p.x+t*(q.x-p.x),p.y+t*(q.y-p.y)});
            }
        }
        poly.swap(next);if(poly.empty()) return 0;
    }
    return std::abs(polygon_area(poly));
}
static Triangle read_triangle(const double* value) {return {{{value[0],value[1]},{value[2],value[3]},{value[4],value[5]}}};}
API int uvck_intersections(I n,const double* a,const double* b,double* output) noexcept {
    try {
        if(n<0 || !a || !b || !output) return -1;
        for(I i=0;i<n;++i) output[i]=triangle_intersection(read_triangle(a+6*i),read_triangle(b+6*i));
        return 0;
    }catch(...){return -3;}
}
struct CellKey {
    I bucket,x,y;
    bool operator==(const CellKey& b) const {return bucket==b.bucket && x==b.x && y==b.y;}
};
struct CellHash {
    std::size_t operator()(const CellKey& a) const {
        auto h=std::hash<I>{}(a.bucket);
        h^=std::hash<I>{}(a.x)+0x9e3779b9+(h<<6)+(h>>2);
        return h^(std::hash<I>{}(a.y)+0x9e3779b9+(h<<6)+(h>>2));
    }
};
struct Bounds {
    double x0,y0,x1,y1;
    I ix0=0,iy0=0,ix1=0,iy1=0,bucket=0;
    bool valid=false,large=false;
};
// First violating pair only, like AcceptanceProbe. Existing overlap is a
// tolerated relationship, not an area budget; static/static pairs are ignored.
API int uvck_check_overlaps(I n,const I* faces,const I* roots,const std::uint8_t* changed,
                            const double* current,const double* reference,I other,I existing,I* output) noexcept {
    try {
        if(n<0 || !faces || !roots || !changed || !current || !reference || !output) return -1;
        std::vector<Bounds> boxes(n);Vec sizes;sizes.reserve(n);
        bool unchanged=true;
        for(I i=0;i<n;++i) if(changed[i]) for(int k=0;k<6;++k) unchanged&=current[6*i+k]==reference[6*i+k];
        if(!existing && unchanged) {output[0]=output[1]=0;return 0;}
        for(I i=0;i<n;++i) {
            const double* p=current+6*i;bool finite=true;for(int k=0;k<6;++k) finite&=std::isfinite(p[k]);if(!finite) continue;
            auto& box=boxes[i];box.x0=std::min({p[0],p[2],p[4]});box.x1=std::max({p[0],p[2],p[4]});
            box.y0=std::min({p[1],p[3],p[5]});box.y1=std::max({p[1],p[3],p[5]});box.valid=true;
            double size=std::max(box.x1-box.x0,box.y1-box.y0);if(size>0) sizes.push_back(size);
        }
        double cell=1e-12;
        if(!sizes.empty()) {auto middle=sizes.begin()+sizes.size()/2;std::nth_element(sizes.begin(),middle,sizes.end());cell=std::max(*middle*2,1e-12);}
        if(!std::isfinite(cell)) return -2; // Python can handle unbounded integer cells.
        std::unordered_map<CellKey,std::vector<I>,CellHash> grid;
        std::unordered_map<I,std::vector<I>> large,buckets;
        auto coordinate=[&](double value,I& out) {
            double v=std::floor(value/cell);
            if(!std::isfinite(v) || v< -9e18 || v>9e18) return false;
            out=static_cast<I>(v);return true;
        };
        for(I i=0;i<n;++i) {
            auto& b=boxes[i];if(!b.valid) continue;b.bucket=other?0:roots[i];
            if(!coordinate(b.x0,b.ix0) || !coordinate(b.x1,b.ix1) || !coordinate(b.y0,b.iy0) || !coordinate(b.y1,b.iy1)) return -2;
            // A cell count above 64 takes the existing large-triangle route.
            long double width=static_cast<long double>(b.ix1)-b.ix0+1,height=static_cast<long double>(b.iy1)-b.iy0+1;
            b.large=width*height>64;buckets[b.bucket].push_back(i);
            if(b.large) large[b.bucket].push_back(i);
            else for(I x=b.ix0;x<=b.ix1;++x) for(I y=b.iy0;y<=b.iy1;++y) grid[{b.bucket,x,y}].push_back(i);
        }
        I tested=0,kind=0;
        for(I i=0;i<n;++i) {
            const auto& a=boxes[i];if(!changed[i] || !a.valid) continue;
            std::unordered_set<I> candidates;
            auto add=[&](const std::vector<I>& rows) {candidates.insert(rows.begin(),rows.end());};
            auto big=large.find(a.bucket);if(big!=large.end()) add(big->second);
            if(a.large) add(buckets[a.bucket]);
            else for(I x=a.ix0;x<=a.ix1;++x) for(I y=a.iy0;y<=a.iy1;++y) {auto row=grid.find({a.bucket,x,y});if(row!=grid.end()) add(row->second);}
            for(I j:candidates) {
                const auto& b=boxes[j];
                if(j==i || !b.valid || (changed[j] && j<i) || faces[i]==faces[j] || (!other && roots[i]!=roots[j])) continue;
                if(a.x0>=b.x1 || b.x0>=a.x1 || a.y0>=b.y1 || b.y0>=a.y1) continue;
                ++tested;
                if(triangle_intersection(read_triangle(current+6*i),read_triangle(current+6*j))<=1e-10) continue;
                if(!existing && !(triangle_intersection(read_triangle(reference+6*i),read_triangle(reference+6*j))<=1e-10)) continue;
                const I found=roots[i]==roots[j]?1:2;
                if(!other) {output[0]=found;output[1]=tested;return 0;}
                // Python's set order can choose a different class when both
                // kinds fail. Ask the existing Python path to resolve that
                // rare ambiguity instead of changing the acceptance result.
                if(kind && kind!=found) {output[0]=3;output[1]=tested;return 0;}
                kind=found;
            }
        }
        output[0]=kind;output[1]=tested;return 0;
    }catch(...){return -3;}
}

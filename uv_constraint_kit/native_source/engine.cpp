// SPDX-License-Identifier: GPL-3.0-or-later
// UV Constraint Kit CPU engine. No Blender/Python objects cross this C ABI.
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cfenv>
#include <cstring>
#if defined(_M_X64) || defined(__SSE__)
#include <xmmintrin.h>
#endif
#include <cfloat>
#include <set>
#include <cstdint>
#include <limits>
#include <map>
#include <numeric>
#include <stdexcept>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
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
static double dot(const Vec& a, const Vec& b) {
    double r=0; for(std::size_t i=0;i<a.size();++i) r+=a[i]*b[i]; return r;
}
static double max_abs(const Vec& a) {
    double r=0; for(double x:a) r=std::max(r,std::abs(x)); return r;
}
struct AxisSet { I axis; std::vector<I> members; bool anchored=false; double anchor=0; };
struct Problem {
    const Input& p;

    bool contact_valid=true;
    double contact_h=0,contact_sigma=0,contact_k=0;
    std::vector<std::pair<I,I>> contact_edges;
    std::vector<I> contact_vertices;
    bool soft, axes_only=true, reciprocal=false;
    std::vector<AxisSet> sets;
    Vec jacobian, gradient_terms;
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
        if(p.prevent_overlap==2) init_contact();
    }
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
            bool proper=(cross(abx,aby,c[0]-a[0],c[1]-a[1])*cross(abx,aby,d[0]-a[0],d[1]-a[1])<0 &&
                         cross(cdx,cdy,a[0]-c[0],a[1]-c[1])*cross(cdx,cdy,b[0]-c[0],b[1]-c[1])<0);
            if(proper) {
                if(p.prevent_overlap!=2 ||
                   std::min({contact_segment_distance(a,c,d),contact_segment_distance(b,c,d),
                             contact_segment_distance(c,a,b),contact_segment_distance(d,a,b)})>contact_sigma)
                    return false;
            }
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

    static double contact_segment_distance(const double* q,const double* a,const double* b) {
        double vx=b[0]-a[0],vy=b[1]-a[1];
        double t=std::clamp(((q[0]-a[0])*vx+(q[1]-a[1])*vy)/
                            std::max(vx*vx+vy*vy,1e-30),0.,1.);
        return std::hypot(q[0]-a[0]-t*vx,q[1]-a[1]-t*vy);
    }
    void init_contact() {
        std::set<std::pair<I,I>> unique;
        for(I e=0;e<p.nb;++e) {
            I a=p.border_edges[2*e],b=p.border_edges[2*e+1];
            if(a>b) std::swap(a,b);
            unique.emplace(a,b);
        }
        contact_edges.assign(unique.begin(),unique.end());
        std::set<I> vertices;
        std::vector<double> lengths;
        lengths.reserve(contact_edges.size());
        for(const auto& edge:contact_edges) {
            vertices.insert(edge.first);vertices.insert(edge.second);
            double dx=p.uv[2*edge.second]-p.uv[2*edge.first];
            double dy=p.uv[2*edge.second+1]-p.uv[2*edge.first+1];
            double length=std::hypot(dx,dy);
            if(std::isfinite(length) && length>0) lengths.push_back(length);
        }
        contact_vertices.assign(vertices.begin(),vertices.end());
        std::vector<double> positive_weights;
        positive_weights.reserve(p.nt);
        for(I t=0;t<p.nt;++t) if(std::isfinite(p.weights[t])) positive_weights.push_back(p.weights[t]);
        auto median=[](std::vector<double>& values)->double {
            if(values.empty()) return 0;
            std::sort(values.begin(),values.end());
            std::size_t middle=values.size()/2;
            return values.size()%2?values[middle]:(values[middle-1]+values[middle])*.5;
        };
        double span=1;
        for(I i=0;i<p.n;++i) {
            span=std::max(span,std::abs(p.uv[2*i]));
            span=std::max(span,std::abs(p.uv[2*i+1]));
        }
        contact_h=median(lengths)*.1;
        contact_sigma=span*static_cast<double>(FLT_EPSILON)*.25;
        contact_k=median(positive_weights)*.1;
        contact_valid=!contact_edges.empty() && !contact_vertices.empty() &&
            std::isfinite(contact_h) && contact_h>0 &&
            std::isfinite(contact_sigma) && contact_sigma>0 &&
            std::isfinite(contact_k) && contact_k>0;
    }
    double contact(const Vec& x,Vec* gradient) const {
        if(p.prevent_overlap!=2) return 0;
        if(!contact_valid) return INF;
        double min0=INF,max0=-INF,min1=INF,max1=-INF;
        for(I v:contact_vertices) {
            min0=std::min(min0,x[2*v]);max0=std::max(max0,x[2*v]);
            min1=std::min(min1,x[2*v+1]);max1=std::max(max1,x[2*v+1]);
        }
        int axis=(max0-min0)>=(max1-min1)?0:1,other=1-axis;
        std::vector<std::pair<double,I>> ordered;
        ordered.reserve(contact_vertices.size());
        for(I v:contact_vertices) ordered.emplace_back(x[2*v+axis],v);
        std::sort(ordered.begin(),ordered.end(),[](const auto& a,const auto& b) {
            return a.first<b.first || (a.first==b.first && a.second<b.second);
        });
        struct Pair {I vertex,edge;};
        std::vector<Pair> pairs;
        const double h2=contact_h*contact_h,sigma2=contact_sigma*contact_sigma;
        for(I ei=0;ei<static_cast<I>(contact_edges.size());++ei) {
            I a=contact_edges[ei].first,b=contact_edges[ei].second;
            double low=std::min(x[2*a+axis],x[2*b+axis])-contact_h;
            double high=std::max(x[2*a+axis],x[2*b+axis])+contact_h;
            auto first=std::lower_bound(ordered.begin(),ordered.end(),low,
                [](const auto& value,double key){return value.first<key;});
            auto last=std::upper_bound(first,ordered.end(),high,
                [](double key,const auto& value){return key<value.first;});
            double lo=std::min(x[2*a+other],x[2*b+other]);
            double hi=std::max(x[2*a+other],x[2*b+other]);
            for(auto it=first;it!=last;++it) {
                I v=it->second;if(v==a || v==b) continue;
                double q=x[2*v+other];
                double gap=std::max(std::max(lo-q,q-hi),0.);
                // The sweep axis interval is already edge-expanded. Test the
                // orthogonal AABB gap to reproduce the 2D squared-gap gate.
                double along_gap=std::max(std::max(std::min(x[2*a+axis],x[2*b+axis])-it->first,
                                                    it->first-std::max(x[2*a+axis],x[2*b+axis])),0.);
                if(along_gap*along_gap+gap*gap>h2) continue;
                pairs.push_back({v,ei});
            }
        }
        std::sort(pairs.begin(),pairs.end(),[](const Pair& a,const Pair& b) {
            return a.vertex<b.vertex || (a.vertex==b.vertex && a.edge<b.edge);
        });
        double total=0;
        const double denominator=h2+sigma2;
        for(const Pair& pair:pairs) {
            I v=pair.vertex,a=contact_edges[pair.edge].first,b=contact_edges[pair.edge].second;
            const double *q=&x[2*v],*pa=&x[2*a],*pb=&x[2*b];
            double abx=pb[0]-pa[0],aby=pb[1]-pa[1];
            double ab2=abx*abx+aby*aby;
            double t=std::clamp(((q[0]-pa[0])*abx+(q[1]-pa[1])*aby)/std::max(ab2,sigma2),0.,1.);
            double dx=q[0]-pa[0]-t*abx,dy=q[1]-pa[1]-t*aby;
            double d2=dx*dx+dy*dy;
            double s=(d2+sigma2)/denominator;
            if(!(s<1.)) continue;
            double one=1.-s;
            total+=-contact_k*one*one*std::log(s);
            if(gradient) {
                double db_dd2=contact_k*(2.*one*std::log(s)-one*one/s)/denominator;
                double gx=2.*db_dd2*dx,gy=2.*db_dd2*dy;
                (*gradient)[2*v]+=gx;(*gradient)[2*v+1]+=gy;
                (*gradient)[2*a]-=(1.-t)*gx;(*gradient)[2*a+1]-=(1.-t)*gy;
                (*gradient)[2*b]-=t*gx;(*gradient)[2*b+1]-=t*gy;
            }
        }
        return total;
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
    double energy(const Vec& x) {
        flip_rejected=false;++evaluations;
        if(p.prevent_overlap==1 && !borders_valid(x)) return INF;
        build_j(x);double e=0;
        for(I t=0;t<p.nt;++t) {
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
        if(p.prevent_overlap==2) {double contact_energy=contact(x,nullptr);if(!std::isfinite(contact_energy)) return INF;e+=contact_energy;}
        penalty(x,&e,nullptr);return std::isfinite(e)?e:INF;
    }
    void gradient(const Vec& x, Vec& g) {
        // Every triangle term is overwritten; only the vertex accumulation
        // needs resetting. Scratch storage belongs to this solve alone.
        Vec& gu=gradient_terms;gu.resize(4*p.nt);g.assign(2*p.n,0);
        for(I t=0;t<p.nt;++t) {
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
        for(int corner=0;corner<3;++corner) for(I t=0;t<p.nt;++t) for(int axis=0;axis<2;++axis) {
            double a=gu[4*t+2*axis],b=gu[4*t+2*axis+1];
            g[2*p.tri[3*t+corner]+axis]+=(corner==0?-a-b:corner==1?a:b);
        }
        if(p.prevent_overlap==2) contact(x,&g);
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
// Geometric Gram plus exact H/V quadratic penalties, not the full Hessian.
// Fixed variables are eliminated before factorization; no H/V averaging.
struct SoftMetric {
    std::vector<I> offsets,columns,lower_ends;
    Vec values,diagonal;
    bool enabled() const {return !diagonal.empty();}
    static void fixed_zero(const Problem& problem,Vec& v) {
        for(I i=0;i<problem.p.n;++i) if(problem.p.fixed[i]) {v[2*i]=0.;v[2*i+1]=0.;}
    }
    bool apply(const Problem& problem,const Vec& rhs,Vec& result) const {
        if(!enabled()) return false;
        const I n=2*problem.p.n;
        Vec input=rhs,forward(n);result.assign(n,0.);fixed_zero(problem,input);
        for(I i=0;i<n;++i) {
            double v=input[i];
            for(I k=offsets[i];k<lower_ends[i];++k) v-=values[k]*forward[columns[k]];
            forward[i]=v/diagonal[i];if(!std::isfinite(forward[i])) return false;
        }
        for(I i=n;i-->0;) {
            double v=forward[i];
            for(I k=lower_ends[i];k<offsets[i+1];++k) v-=values[k]*result[columns[k]];
            result[i]=v/diagonal[i];if(!std::isfinite(result[i])) return false;
        }
        fixed_zero(problem,result);
        for(double v:result) if(!std::isfinite(v)) return false;
        return true;
    }
    bool apply_pair(const Problem& problem,const Vec& rhs_y,const Vec& rhs_q,Vec& mapped_y,Vec& mapped_q) const {
        if(!enabled()) return false;
        const I n=2*problem.p.n;
        Vec input_y=rhs_y,input_q=rhs_q,forward_y(n),forward_q(n);
        mapped_y.assign(n,0.);mapped_q.assign(n,0.);
        fixed_zero(problem,input_y);fixed_zero(problem,input_q);
        for(I i=0;i<n;++i) {
            double y=input_y[i],q=input_q[i];
            for(I k=offsets[i];k<lower_ends[i];++k) {y-=values[k]*forward_y[columns[k]];q-=values[k]*forward_q[columns[k]];}
            forward_y[i]=y/diagonal[i];forward_q[i]=q/diagonal[i];
            if(!std::isfinite(forward_y[i]) || !std::isfinite(forward_q[i])) return false;
        }
        for(I i=n;i-->0;) {
            double y=forward_y[i],q=forward_q[i];
            for(I k=lower_ends[i];k<offsets[i+1];++k) {y-=values[k]*mapped_y[columns[k]];q-=values[k]*mapped_q[columns[k]];}
            mapped_y[i]=y/diagonal[i];mapped_q[i]=q/diagonal[i];
            if(!std::isfinite(mapped_y[i]) || !std::isfinite(mapped_q[i])) return false;
        }
        fixed_zero(problem,mapped_y);fixed_zero(problem,mapped_q);
        for(double v:mapped_y) if(!std::isfinite(v)) return false;
        for(double v:mapped_q) if(!std::isfinite(v)) return false;
        return true;
    }
};
static SoftMetric make_soft_metric(const Problem& problem) noexcept {
    try {
        const Input& p=problem.p;
        if(!p.automatic || !problem.soft || !problem.axes_only || !problem.reciprocal
                || p.angle_length!=1. || p.ne<=0 || p.strength<0.) return {};
        const bool orig_contact=(p.prevent_overlap==2 && p.strength<=100.);
        const bool fast_small=(p.prevent_overlap>=1 && p.n<=8192 && p.strength<=1e6);
        if(!orig_contact && !fast_small) return {};
        const I n=2*p.n;
        std::vector<std::map<I,double>> rows(n);
        for(I t=0;t<p.nt;++t) {
            const double* v=&p.inverse[4*t];
            double cx[3]={-v[0]-v[2],v[0],v[2]},cy[3]={-v[1]-v[3],v[1],v[3]};
            for(I i=0;i<3;++i) for(I j=i;j<3;++j) {
                double add=2*p.weights[t]*(cx[i]*cx[j]+cy[i]*cy[j]);if(!std::isfinite(add)) return {};
                for(I axis=0;axis<2;++axis) {
                    I a=2*p.tri[3*t+i]+axis,b=2*p.tri[3*t+j]+axis;
                    rows[a][b]+=add;if(i!=j) rows[b][a]+=add;
                    if(!std::isfinite(rows[a][b]) || !std::isfinite(rows[b][a])) return {};
                }
            }
        }
        double sum=0.;I count=0;
        for(I i=0;i<n;++i) if(!p.fixed[i/2]) {
            double d=rows[i][i];if(d>0.) {sum+=d;++count;if(!std::isfinite(sum)) return {};}
        }
        if(!count) return {};
        const double damping=.01*(sum/count);
        if(!(damping>0.) || !std::isfinite(damping)) return {};
        bool positive=false;
        for(I e=0;e<p.ne;++e) {
            I a=2*p.penalty_edges[2*e]+p.penalty_axis[e],b=2*p.penalty_edges[2*e+1]+p.penalty_axis[e];
            if(a==b || p.penalty_weights[e]==0.) continue;
            double add=2*p.penalty_weights[e];if(!(add>0.) || !std::isfinite(add)) return {};
            positive=true;rows[a][a]+=add;rows[b][b]+=add;rows[a][b]-=add;rows[b][a]-=add;
            if(!std::isfinite(rows[a][a]) || !std::isfinite(rows[b][b]) || !std::isfinite(rows[a][b]) || !std::isfinite(rows[b][a])) return {};
        }
        if(!positive) return {};
        for(I i=0;i<n;++i) {
            if(p.fixed[i/2]) {rows[i].clear();rows[i][i]=1.;}
            else {
                rows[i][i]+=damping;if(!std::isfinite(rows[i][i])) return {};
                for(auto it=rows[i].begin();it!=rows[i].end();) {
                    if(it->first!=i && p.fixed[it->first/2]) it=rows[i].erase(it);else ++it;
                }
            }
        }
        std::vector<std::map<I,double>> lower(n);
        SoftMetric result;result.diagonal.resize(n);
        for(I i=0;i<n;++i) {
            for(const auto& item:rows[i]) {
                I j=item.first;if(j>=i) break;if(item.second==0.) continue;
                double value=item.second;auto a=lower[i].begin(),b=lower[j].begin();
                while(a!=lower[i].end() && b!=lower[j].end()) {
                    if(a->first>=j || b->first>=j) break;
                    if(a->first<b->first) ++a;else if(b->first<a->first) ++b;
                    else {value-=a->second*b->second;++a;++b;}
                    if(!std::isfinite(value)) return {};
                }
                value/=result.diagonal[j];if(!std::isfinite(value)) return {};
                lower[i].emplace(j,value);
            }
            double pivot=rows[i][i];if(!std::isfinite(pivot)) return {};
            for(const auto& item:lower[i]) {pivot-=item.second*item.second;if(!std::isfinite(pivot)) return {};}
            if(!(pivot>0.)) return {};
            result.diagonal[i]=std::sqrt(pivot);
            if(!(result.diagonal[i]>0.) || !std::isfinite(result.diagonal[i])) return {};
        }
        std::vector<std::map<I,double>> factor(n);
        for(I i=0;i<n;++i) for(const auto& item:lower[i]) {factor[i].emplace(item.first,item.second);factor[item.first].emplace(i,item.second);}
        result.offsets.push_back(0);
        for(I i=0;i<n;++i) {
            const std::size_t begin=static_cast<std::size_t>(result.offsets.back());
            const std::size_t maximum=static_cast<std::size_t>(std::numeric_limits<I>::max());
            if(lower[i].size()>maximum-begin) return {};
            const std::size_t split=begin+lower[i].size();
            for(const auto& item:factor[i]) {result.columns.push_back(item.first);result.values.push_back(item.second);}
            if(result.columns.size()>maximum || split>result.columns.size()) return {};
            if(split>begin && result.columns[split-1]>=i) return {};
            if(split<result.columns.size() && result.columns[split]<=i) return {};
            result.lower_ends.push_back(static_cast<I>(split));result.offsets.push_back(static_cast<I>(result.columns.size()));
        }
        return result;
    }catch(...) {return {};}
}

API I uvck_contact_capability() {return 1;}
API I uvck_abi(I input_size,I output_size) {return input_size==sizeof(Input) && output_size==sizeof(Output)?1:0;}
API int uvck_solve(const Input* input,double* output,Output* stats) noexcept {
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
            return 1; // Do not publish even the initial projection.
        }
        double e=problem.energy(x);*stats={0,problem.evaluations,problem.reciprocal,0,e,INF};
        if(!std::isfinite(e)) return 1; // Do not publish even the initial projection.
        Vec g;problem.gradient(x,g);
        struct History {Vec s,y;double rho;};std::vector<History> history;
        history.reserve(8);
        Vec q,direction,candidate,next_g,change(x.size()),y(x.size());std::vector<double> coefficients;
        const SoftMetric soft_metric=make_soft_metric(problem);
        const bool contact_progress=(p.prevent_overlap==2 && soft_metric.enabled());
        // Soft stages with the explicit overlap guard can stall against a
        // boundary while line search still accepts vanishing improvements.
        // Hand control back to the existing stronger-penalty continuation.
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
            q=g;coefficients.clear();
            for(auto i=history.rbegin();i!=history.rend();++i) {double alpha=i->rho*dot(i->s,q);coefficients.push_back(alpha);for(std::size_t k=0;k<q.size();++k) q[k]-=alpha*i->y[k];}
            bool preconditioned=false;
            if(soft_metric.enabled()) {
                Vec mapped;double gamma=1.;bool finite=true;
                if(!history.empty()) {
                    Vec mapped_y;
                    finite=soft_metric.apply_pair(problem,history.back().y,q,mapped_y,mapped);
                    double denominator=finite?dot(history.back().y,mapped_y):0.;
                    finite=finite && denominator>0. && std::isfinite(denominator);
                    if(finite) gamma=dot(history.back().s,history.back().y)/std::max(denominator,1e-20);
                }
                finite=finite && gamma>0. && std::isfinite(gamma);
                if(finite && history.empty()) finite=soft_metric.apply(problem,q,mapped);
                if(finite) for(double& v:mapped) {v*=gamma;if(!std::isfinite(v)) {finite=false;break;}}
                if(finite) {q.swap(mapped);preconditioned=true;}
            }
            if(!preconditioned) {
                double scale=history.empty()?std::max(p.density*p.density,1e-6)*.05:dot(history.back().s,history.back().y)/std::max(dot(history.back().y,history.back().y),1e-20);
                if(history.empty()) {
                    const double g_inf=max_abs(g);
                    const double max_disp=.05*std::sqrt(std::max(p.penalty_scale,1e-20));
                    if(g_inf>0. && max_disp>0. && scale*g_inf>max_disp) scale=max_disp/g_inf;
                }
                for(double& v:q) v*=scale;
            }
            for(std::size_t i=0;i<history.size();++i) {double v=coefficients[history.size()-i-1]-history[i].rho*dot(history[i].y,q);for(std::size_t k=0;k<q.size();++k) q[k]+=history[i].s[k]*v;}
            direction=q;for(double& v:direction) v=-v;double slope=dot(g,direction);
            if(slope>=0) {
                const double g_inf=max_abs(g);
                const double max_disp=.05*std::sqrt(std::max(p.penalty_scale,1e-20));
                const double reset_scale=(g_inf>max_disp && max_disp>0.)?(max_disp/g_inf):1.;
                for(std::size_t k=0;k<g.size();++k) direction[k]=-reset_scale*g[k];
                slope=dot(g,direction);
                history.clear();
            }
            double step=1,next_e=INF;candidate.resize(x.size());bool accepted=false,used_analytic=false,used_border_ccd=false;
            const bool analytic_bound=(p.nt>=32768) && (p.prevent_flips || problem.reciprocal) && (problem.soft || problem.axes_only);
            problem.baseline_uv=keep_initial_border?&initial_uv:nullptr;
            problem.border_rejected=false;
            problem.flip_rejected=false;
            for(int search=0;search<32;++search) {
                for(std::size_t k=0;k<x.size();++k) candidate[k]=x[k]+step*direction[k];
                problem.project(candidate);
                const double threshold=e+1e-4*step*slope;
                // A pair remembered by the original scan must fail again
                // at the current projected UVs before it can reject a trial.
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
            for(std::size_t k=0;k<x.size();++k) {change[k]=candidate[k]-x[k];y[k]=next_g[k]-g[k];}
            double sy=dot(change,y);
            if(sy>1e-14) {
                History entry{std::move(change),std::move(y),1/sy};
                if(history.size()==8) {
                    change=std::move(history.front().s);
                    y=std::move(history.front().y);
                    history.erase(history.begin());
                } else {
                    change.resize(x.size());
                    y.resize(x.size());
                }
                history.push_back(std::move(entry));
            }
            double improvement=e-next_e;x.swap(candidate);e=next_e;g.swap(next_g);++stats->accepted;
            if(p.automatic && improvement<=1e-10*std::max(1.,std::abs(e))) {
                if(try_pin_witness()) continue;
                break;
            }
            // Global objective progress, not a fixed short solve or mesh-specific rule.
            // Keep old early stops and the1000 ceiling; manual solves are unchanged.
            if(p.automatic && (!problem.soft || contact_progress || overlap_progress) && stats->accepted%25==0) {
                double reduction=checkpoint_energy-e;
                checkpoint_energy=e;
                double base_tol=(p.nt>=32768)?0.015:0.01;
                double tolerance=overlap_progress?0.0001:(contact_progress?0.001:base_tol);
                bool direction_ready=true;
                // The terminal penalty of the Python ramp must already meet
                // direction tolerance before this additional stop can fire.
                if((contact_progress && p.strength>1.) || (overlap_progress && p.strength>=1e6)) {
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
                if(direction_ready && (!(contact_progress || overlap_progress) || stats->accepted>=100) && reduction>=0 &&
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
// Test entry point: evaluate the same objective at a supplied point, without projection.
API int uvck_evaluate(const Input* input,I reciprocal,double* energy,double* gradient) noexcept {
    try {if(!input || !energy || !gradient || !valid_input(*input)) return -1;
        Problem p(*input);p.reciprocal=reciprocal!=0;Vec x(input->uv,input->uv+2*input->n);
        *energy=p.energy(x);if(!std::isfinite(*energy)) return 1;Vec g;p.gradient(x,g);std::copy(g.begin(),g.end(),gradient);return 0;
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
static double polygon_area(const Point* points,std::size_t count) {
    double area=0;
    for(std::size_t i=0;i<count;++i) {
        const auto& a=points[i];const auto& b=points[i+1==count?0:i+1];
        area+=a.x*b.y-b.x*a.y;
    }
    return area*.5;
}
static double side(const Point& a,const Point& b,const Point& p) {return (b.x-a.x)*(p.y-a.y)-(b.y-a.y)*(p.x-a.x);}
// A filtered sign with an exact integer fallback for finite binary64 points.
// Products have exponent >= -2148; six terms need <4200 bits on this grid.
using OrientationWords=std::array<std::uint32_t,136>;
struct OrientationDyadic {std::uint64_t mantissa;int exponent;bool negative;};
static OrientationDyadic orientation_dyadic(double value) {
    std::uint64_t bits;std::memcpy(&bits,&value,sizeof(bits));
    const unsigned field=static_cast<unsigned>((bits>>52)&2047);
    return {(bits&0xfffffffffffffULL)|(field?1ULL<<52:0),
        field?static_cast<int>(field)-1075:-1074,(bits>>63)!=0};
}
static void orientation_add_word(OrientationWords& sum,std::size_t index,std::uint32_t value) {
    std::uint64_t carry=value;
    while(carry) {
        if(index>=sum.size()) throw std::overflow_error("orientation integer bound");
        carry+=sum[index];sum[index]=static_cast<std::uint32_t>(carry);
        carry>>=32;++index;
    }
}
static void orientation_add_partial(OrientationWords& sum,std::uint64_t value,int shift) {
    const std::size_t word=static_cast<std::size_t>(shift/32);
    const unsigned offset=static_cast<unsigned>(shift%32);
    for(unsigned part=0;part<2;++part) {
        const std::uint64_t chunk=static_cast<std::uint32_t>(value>>(32*part));
        const std::uint64_t shifted=chunk<<offset;
        orientation_add_word(sum,word+part,static_cast<std::uint32_t>(shifted));
        orientation_add_word(sum,word+part+1,static_cast<std::uint32_t>(shifted>>32));
    }
}
static int exact_orientation(const Point& a,const Point& b,const Point& c) {
    const OrientationDyadic values[6]={orientation_dyadic(a.x),orientation_dyadic(a.y),
        orientation_dyadic(b.x),orientation_dyadic(b.y),orientation_dyadic(c.x),orientation_dyadic(c.y)};
    OrientationWords positive{},negative{};
    const int terms[6][3]={{0,3,0},{0,5,1},{2,5,0},{2,1,1},{4,1,0},{4,3,1}};
    for(const auto& term:terms) {
        const auto& u=values[term[0]];const auto& v=values[term[1]];
        if(!u.mantissa || !v.mantissa) continue;
        auto& sum=(u.negative^v.negative^(term[2]!=0))?negative:positive;
        const int shift=u.exponent+v.exponent+2148;
        for(unsigned i=0;i<2;++i) for(unsigned j=0;j<2;++j) {
            const std::uint64_t first=static_cast<std::uint32_t>(u.mantissa>>(32*i));
            const std::uint64_t second=static_cast<std::uint32_t>(v.mantissa>>(32*j));
            orientation_add_partial(sum,first*second,shift+32*static_cast<int>(i+j));
        }
    }
    for(std::size_t i=positive.size();i-->0;) {
        if(positive[i]>negative[i]) return 1;
        if(positive[i]<negative[i]) return -1;
    }
    return 0;
}
enum class OrientationFilter { exact, normal_range, unrestricted };
static OrientationFilter orientation_filter_mode() {
    if(std::fegetround()!=FE_TONEAREST) return OrientationFilter::exact;
#if defined(_M_X64) || defined(__SSE__)
    const unsigned control=_mm_getcsr();
    if(control&0x6000U) return OrientationFilter::exact;
    if(control&0x8040U) {
        // The new arithmetic must not introduce an unmasked FP trap.
        if((control&0x1f80U)!=0x1f80U) return OrientationFilter::exact;
        return OrientationFilter::normal_range;
    }
#endif
    return OrientationFilter::unrestricted;
}
static int orientation_sign(const Point& a,const Point& b,const Point& c,bool filtered) {
    if(!filtered) return exact_orientation(a,b,c);
    const double values[6]={a.x,a.y,b.x,b.y,c.x,c.y};
    for(double value:values) {
        std::uint64_t bits;std::memcpy(&bits,&value,sizeof(bits));
        if(!(bits&0x7ff0000000000000ULL) && (bits&0xfffffffffffffULL))
            return exact_orientation(a,b,c);
    }
    if(equal_point(a,b) || equal_point(a,c) || equal_point(b,c)
            || (a.x==b.x && a.x==c.x) || (a.y==b.y && a.y==c.y)) return 0;
    const double ax=a.x-c.x,ay=a.y-c.y,bx=b.x-c.x,by=b.y-c.y;
    const auto safe=[](double v) {return std::isfinite(v) && (v==0. || std::abs(v)>=DBL_MIN);};
    if(!safe(ax) || !safe(ay) || !safe(bx) || !safe(by)
            || (ax==0. && a.x!=c.x) || (ay==0. && a.y!=c.y)
            || (bx==0. && b.x!=c.x) || (by==0. && b.y!=c.y))
        return exact_orientation(a,b,c);
    const double left=ax*by,right=ay*bx,determinant=left-right;
    const double bound=8.*DBL_EPSILON*(std::abs(left)+std::abs(right));
    if(safe(left) && safe(right) && std::isfinite(determinant) && std::isfinite(bound) && bound>=DBL_MIN
            && !(left==0. && ax!=0. && by!=0.) && !(right==0. && ay!=0. && bx!=0.)) {
        if(determinant>bound) return 1;
        if(determinant< -bound) return -1;
    }
    return exact_orientation(a,b,c);
}
static bool proper_triangle_crossing(const Triangle& a,const Triangle& b,bool filtered) {
    for(const auto& p:a) if(!std::isfinite(p.x) || !std::isfinite(p.y)) return false;
    for(const auto& p:b) if(!std::isfinite(p.x) || !std::isfinite(p.y)) return false;
    for(int i=0;i<3;++i) for(int j=0;j<3;++j) {
        const Point& p=a[i];const Point& q=a[(i+1)%3];
        const Point& r=b[j];const Point& s=b[(j+1)%3];
        if(std::max(p.x,q.x)<std::min(r.x,s.x) || std::max(r.x,s.x)<std::min(p.x,q.x)
                || std::max(p.y,q.y)<std::min(r.y,s.y) || std::max(r.y,s.y)<std::min(p.y,q.y)) continue;
        if(filtered && (equal_point(p,r) || equal_point(p,s) || equal_point(q,r) || equal_point(q,s))) continue;
        const int first=orientation_sign(p,q,r,filtered),second=orientation_sign(p,q,s,filtered);
        if(!first || !second || first==second) continue;
        const int third=orientation_sign(r,s,p,filtered),fourth=orientation_sign(r,s,q,filtered);
        if(third && fourth && third!=fourth)
            return orientation_sign(a[0],a[1],a[2],filtered)!=0 && orientation_sign(b[0],b[1],b[2],filtered)!=0;
    }
    return false;
}

static double triangle_intersection(const Triangle& a,const Triangle& b) {
    Triangle shared;
    std::size_t shared_count=0;
    for(const auto& p:a) {
        bool in_b=false,seen=false;
        for(const auto& q:b) in_b|=equal_point(p,q);
        for(std::size_t i=0;i<shared_count;++i) seen|=equal_point(p,shared[i]);
        if(in_b && !seen) shared[shared_count++]=p;
    }
    if(shared_count==2) {
        const Point *u=nullptr,*v=nullptr;
        for(const auto& p:a) if(!equal_point(p,shared[0]) && !equal_point(p,shared[1])) {u=&p;break;}
        for(const auto& p:b) if(!equal_point(p,shared[0]) && !equal_point(p,shared[1])) {v=&p;break;}
        if(!u || !v || side(shared[0],shared[1],*u)*side(shared[0],shared[1],*v)<=0) return 0;
    }
    // Each clipping pass emits at most two vertices per input edge.
    // Three passes from a triangle therefore fit even with duplicate points.
    std::array<Point,3*2*2*2> scratch_a,scratch_b;
    Point* poly=scratch_a.data();
    Point* next=scratch_b.data();
    std::copy(a.begin(),a.end(),poly);
    Triangle clip=b;
    std::size_t count=3;
    if(polygon_area(poly,count)<0) std::reverse(poly,poly+count);
    if(polygon_area(clip.data(),clip.size())<0) std::reverse(clip.begin(),clip.end());
    for(std::size_t edge=0;edge<clip.size();++edge) {
        const auto& v=clip[edge];const auto& w=clip[(edge+1)%clip.size()];
        std::size_t next_count=0;
        for(std::size_t i=0;i<count;++i) {
            const auto& p=poly[i];const auto& q=poly[i+1==count?0:i+1];
            double sp=side(v,w,p),sq=side(v,w,q);
            if(sp>=-1e-12) next[next_count++]=p;
            if((sp>1e-12 && sq< -1e-12) || (sp< -1e-12 && sq>1e-12)) {
                double t=sp/(sp-sq);next[next_count++]={p.x+t*(q.x-p.x),p.y+t*(q.y-p.y)};
            }
        }
        std::swap(poly,next);count=next_count;
        if(!count) return 0;
    }
    return std::abs(polygon_area(poly,count));
}
static bool orientation_normal_range(const Triangle& triangle) {
    // With FTZ/DAZ, keep differences, products, determinants and filter
    // bounds normal or zero by limiting nonzero coordinates to this range.
    constexpr std::uint64_t low=std::uint64_t{1023-200}<<52;
    constexpr std::uint64_t high=std::uint64_t{1023+200}<<52;
    for(const auto& p:triangle) for(double coordinate:{p.x,p.y}) {
        std::uint64_t bits;std::memcpy(&bits,&coordinate,sizeof(bits));
        bits&=0x7fffffffffffffffULL;
        if(bits!=0 && (bits<low || bits>high)) return false;
    }
    return true;
}
static bool triangles_overlap(const Triangle& a,const Triangle& b,OrientationFilter mode) {
    if(triangle_intersection(a,b)>1e-10) return true;
    const bool filtered=mode==OrientationFilter::unrestricted
        || (mode==OrientationFilter::normal_range && orientation_normal_range(a) && orientation_normal_range(b));
    return proper_triangle_crossing(a,b,filtered);
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
template<bool OrderedPair=false>
static int check_overlaps_impl(I n,const I* faces,const I* roots,const std::uint8_t* changed,
                            const double* current,const double* reference,I other,I existing,I* output,I* witness) noexcept {
    try {
        if(n<0 || !faces || !current || !reference || !output || (!OrderedPair && (!roots || !changed))) return -1;
        std::vector<Bounds> boxes(n);Vec sizes;sizes.reserve(n);
        if constexpr (!OrderedPair) {
            if(!existing) {
                bool unchanged=true;
                for(I i=0;i<n;++i) if(changed[i]) for(int k=0;k<6;++k) unchanged&=current[6*i+k]==reference[6*i+k];
                if(unchanged) {output[0]=output[1]=0;return 0;}
            }
        }
        for(I i=0;i<n;++i) {
            const double* p=current+6*i;bool finite=true;for(int k=0;k<6;++k) finite&=std::isfinite(p[k]);if(!finite) continue;
            auto& box=boxes[i];box.x0=std::min({p[0],p[2],p[4]});box.x1=std::max({p[0],p[2],p[4]});
            box.y0=std::min({p[1],p[3],p[5]});box.y1=std::max({p[1],p[3],p[5]});box.valid=true;
            double size=std::max(box.x1-box.x0,box.y1-box.y0);if(size>0) sizes.push_back(size);
        }
        double cell=1e-12;
        if(!sizes.empty()) {auto middle=sizes.begin()+sizes.size()/2;std::nth_element(sizes.begin(),middle,sizes.end());cell=std::max(*middle*2,1e-12);}
        if(!std::isfinite(cell)) return -2; // Python can handle unbounded integer cells.
        struct BoxLite { double x0,y0,x1,y1; I idx; };
        std::unordered_map<I,std::vector<BoxLite>> large_boxes,bucket_boxes;
        auto coordinate=[&](double value,I& out) {
            double v=std::floor(value/cell);
            if(!std::isfinite(v) || v< -9e18 || v>9e18) return false;
            out=static_cast<I>(v);return true;
        };
        std::size_t total_entries=0;
        for(I i=0;i<n;++i) {
            auto& b=boxes[i];if(!b.valid) continue;b.bucket=(OrderedPair || other)?0:roots[i];
            if(!coordinate(b.x0,b.ix0) || !coordinate(b.x1,b.ix1) || !coordinate(b.y0,b.iy0) || !coordinate(b.y1,b.iy1)) return -2;
            // A cell count above 64 takes the existing large-triangle route.
            long double width=static_cast<long double>(b.ix1)-b.ix0+1,height=static_cast<long double>(b.iy1)-b.iy0+1;
            b.large=width*height>64;
            const BoxLite lite{b.x0,b.y0,b.x1,b.y1,i};
            bucket_boxes[b.bucket].push_back(lite);
            if(b.large) large_boxes[b.bucket].push_back(lite);
            else total_entries+=static_cast<std::size_t>(b.ix1-b.ix0+1)*static_cast<std::size_t>(b.iy1-b.iy0+1);
        }
        std::size_t ht_size=256;
        while(ht_size<total_entries*2+16) ht_size<<=1;
        const std::size_t ht_mask=ht_size-1;
        std::vector<I> ht_slot(ht_size,-1),cell_counts,entry_cid,entry_tri;
        std::vector<CellKey> cell_keys;
        entry_cid.reserve(total_entries);
        entry_tri.reserve(total_entries);
        cell_keys.reserve(std::min<std::size_t>(total_entries,ht_size/2));
        cell_counts.reserve(std::min<std::size_t>(total_entries,ht_size/2));
        const CellHash hasher{};
        auto find_or_insert=[&](const CellKey& key) -> I {
            std::size_t idx=hasher(key)&ht_mask;
            while(ht_slot[idx]>=0) {
                const I cid=ht_slot[idx];
                if(cell_keys[cid]==key) return cid;
                idx=(idx+1)&ht_mask;
            }
            const I cid=static_cast<I>(cell_keys.size());
            cell_keys.push_back(key);
            cell_counts.push_back(0);
            ht_slot[idx]=cid;
            return cid;
        };
        auto find_cell=[&](const CellKey& key) -> I {
            std::size_t idx=hasher(key)&ht_mask;
            while(ht_slot[idx]>=0) {
                const I cid=ht_slot[idx];
                if(cell_keys[cid]==key) return cid;
                idx=(idx+1)&ht_mask;
            }
            return -1;
        };
        std::vector<std::size_t> tri_off(n+1,0);
        for(I i=0;i<n;++i) {
            tri_off[i]=entry_cid.size();
            const auto& b=boxes[i];
            if(!b.valid || b.large) continue;
            for(I x=b.ix0;x<=b.ix1;++x) for(I y=b.iy0;y<=b.iy1;++y) {
                const I cid=find_or_insert({b.bucket,x,y});
                ++cell_counts[cid];
                entry_cid.push_back(cid);
                entry_tri.push_back(i);
            }
        }
        tri_off[n]=entry_cid.size();
        std::vector<std::size_t> cell_off(cell_counts.size()+1,0);
        for(std::size_t c=0;c<cell_counts.size();++c)
            cell_off[c+1]=cell_off[c]+static_cast<std::size_t>(cell_counts[c]);
        std::vector<std::size_t> cursor=cell_off;
        std::vector<I> cell_tris(total_entries);
        for(std::size_t k=0;k<entry_tri.size();++k)
            cell_tris[cursor[entry_cid[k]]++]=entry_tri[k];
        const auto filtered_orientation=orientation_filter_mode();
        I tested=0,kind=0;
        auto gather_into=[&](I i,std::vector<std::int32_t>& seen_buf,std::int32_t& visit_buf,std::vector<I>& cand_buf) {
            const auto& a=boxes[i];
            const I fa=faces[i];
            const std::int32_t epoch=visit_buf++;
            if(epoch==std::numeric_limits<std::int32_t>::max()-1) {
                std::fill(seen_buf.begin(),seen_buf.end(),-1);
                visit_buf=1;
            }
            cand_buf.clear();
            auto add_boxes=[&](const std::vector<BoxLite>& rows) {
                for(const auto& lb:rows) {
                    if(lb.idx!=i && faces[lb.idx]!=fa &&
                       a.x0<lb.x1 && lb.x0<a.x1 && a.y0<lb.y1 && lb.y0<a.y1 && seen_buf[lb.idx]!=epoch) {
                        seen_buf[lb.idx]=epoch;
                        cand_buf.push_back(lb.idx);
                    }
                }
            };
            if(a.large) {
                auto all_b=bucket_boxes.find(a.bucket);
                if(all_b!=bucket_boxes.end()) add_boxes(all_b->second);
            } else {
                auto big=large_boxes.find(a.bucket);
                if(big!=large_boxes.end()) add_boxes(big->second);
                for(std::size_t e=tri_off[i];e<tri_off[i+1];++e) {
                    const I cid=entry_cid[e];
                    const std::size_t s0=cell_off[cid],s1=cell_off[cid+1];
                    for(std::size_t k=s0;k<s1;++k) {
                        const I j=cell_tris[k];
                        if(j==i || faces[j]==fa) continue;
                        const auto& b=boxes[j];
                        if(a.x0<b.x1 && b.x0<a.x1 && a.y0<b.y1 && b.y0<a.y1 && seen_buf[j]!=epoch) {
                            seen_buf[j]=epoch;
                            cand_buf.push_back(j);
                        }
                    }
                }
            }
        };
        auto test_pair=[&](I i,I j,I& local_tested) {
            ++local_tested;
            return triangles_overlap(read_triangle(current+6*i),read_triangle(current+6*j),filtered_orientation);
        };
        auto worker_count=[](I span) noexcept -> unsigned {
            if(span<8192) return 1u;
            unsigned hc=std::thread::hardware_concurrency();
            if(hc<=1u) return 1u;
#ifdef _WIN32
            DWORD_PTR proc_mask=0,sys_mask=0;
            if(GetProcessAffinityMask(GetCurrentProcess(),&proc_mask,&sys_mask) && proc_mask!=0) {
                unsigned allowed=0u;
                for(DWORD_PTR m=proc_mask;m;m&=(m-1)) ++allowed;
                if(allowed<hc) hc=allowed;
            }
#endif
            return std::min<unsigned>(8u,std::max<unsigned>(1u,hc));
        };
        struct ThreadJoiner {
            std::vector<std::thread>& threads;
            ~ThreadJoiner() { for(auto& w:threads) if(w.joinable()) w.join(); }
        };
        std::vector<std::int32_t> seen(n,-1);
        std::vector<I> candidates;
        std::int32_t visit=0;
        if constexpr (OrderedPair) {
            auto has_pair=[&](I start,I end) -> I {
                const unsigned nthreads=worker_count(end-start);
                if(nthreads<=1u) {
                    for(I i=start;i<end;++i) {
                        if(!boxes[i].valid) continue;
                        gather_into(i,seen,visit,candidates);
                        // An earlier partner is skipped only when both rows are
                        // active. Preserve clipping argument order on every probe.
                        for(I j:candidates)
                            if((j>=i || j<start) && test_pair(i,j,tested)) return i;
                    }
                    return -1;
                }
                std::atomic<I> best_hit{end};
                std::vector<I> local_tested(nthreads,0);
                std::vector<std::thread> workers;
                workers.reserve(nthreads);
                const I span=end-start;
                {
                    ThreadJoiner guard{workers};
                    for(unsigned t=0;t<nthreads;++t) {
                        const I c0=start+(span*static_cast<I>(t))/static_cast<I>(nthreads);
                        const I c1=start+(span*static_cast<I>(t+1))/static_cast<I>(nthreads);
                        workers.emplace_back([&,t,c0,c1]() {
                            std::vector<std::int32_t> t_seen(n,-1);
                            std::vector<I> t_cand;
                            std::int32_t t_visit=0;
                            I t_tested=0;
                            for(I i=c0;i<c1;++i) {
                                if((i&63)==0 && i>=best_hit.load(std::memory_order_relaxed)) break;
                                if(!boxes[i].valid) continue;
                                gather_into(i,t_seen,t_visit,t_cand);
                                for(I j:t_cand) {
                                    if((j>=i || j<start) && test_pair(i,j,t_tested)) {
                                        I prev=best_hit.load(std::memory_order_relaxed);
                                        while(i<prev && !best_hit.compare_exchange_weak(prev,i,std::memory_order_relaxed)) {}
                                        local_tested[t]=t_tested;
                                        return;
                                    }
                                }
                            }
                            local_tested[t]=t_tested;
                        });
                    }
                }
                for(I c:local_tested) tested+=c;
                const I hit=best_hit.load();
                return hit<end ? hit : -1;
            };
            const I first_hit=has_pair(0,n);
            if(first_hit<0) {output[0]=0;output[1]=tested;return 0;}
            I start=0,end=first_hit+1;
            while(end-start>1) {
                const I middle=start+(end-start)/2;
                const I h=has_pair(start,middle);
                if(h>=0) end=h+1;
                else start=middle;
            }
            gather_into(start,seen,visit,candidates);
            I partner=n;
            // For the single active row the legacy partner probes all have the
            // same argument order. Choose the smallest passing original index.
            for(I j:candidates) if(j<partner && test_pair(start,j,tested)) partner=j;
            if(partner==n) return -4;
            witness[0]=start;witness[1]=partner;
            output[0]=1;output[1]=tested;
            return 0;
        }
        const unsigned nthreads=worker_count(n);
        if(nthreads<=1u) {
            for(I i=0;i<n;++i) {
                if(!changed[i] || !boxes[i].valid) continue;
                gather_into(i,seen,visit,candidates);
                for(I j:candidates) {
                    if((changed[j] && j<i) || (!other && roots[i]!=roots[j]) || !test_pair(i,j,tested)) continue;
                    if(!existing && triangles_overlap(read_triangle(reference+6*i),read_triangle(reference+6*j),filtered_orientation)) continue;
                    const I found=roots[i]==roots[j]?1:2;
                    if(!other) {if(witness) {witness[0]=i;witness[1]=j;} output[0]=found;output[1]=tested;return 0;}
                    // Python's set order can choose a different class when both
                    // kinds fail. Ask the existing Python path to resolve that
                    // rare ambiguity instead of changing the acceptance result.
                    if(kind && kind!=found) {output[0]=3;output[1]=tested;return 0;}
                    kind=found;
                }
            }
            output[0]=kind;output[1]=tested;return 0;
        }
        struct ChunkResult { I kind=0, wi=-1, wj=-1, tested=0; };
        std::vector<ChunkResult> results(nthreads);
        std::atomic<I> best_i{n};
        std::atomic<bool> found_both{false};
        std::vector<std::thread> workers;
        workers.reserve(nthreads);
        {
            ThreadJoiner guard{workers};
            for(unsigned t=0;t<nthreads;++t) {
                const I c0=(n*static_cast<I>(t))/static_cast<I>(nthreads);
                const I c1=(n*static_cast<I>(t+1))/static_cast<I>(nthreads);
                workers.emplace_back([&,t,c0,c1]() {
                    std::vector<std::int32_t> t_seen(n,-1);
                    std::vector<I> t_cand;
                    std::int32_t t_visit=0;
                    ChunkResult r{};
                    for(I i=c0;i<c1;++i) {
                        if((i&63)==0) {
                            if(!other && i>=best_i.load(std::memory_order_relaxed)) break;
                            if(other && found_both.load(std::memory_order_relaxed)) break;
                        }
                        if(!changed[i] || !boxes[i].valid) continue;
                        gather_into(i,t_seen,t_visit,t_cand);
                        for(I j:t_cand) {
                            if((changed[j] && j<i) || (!other && roots[i]!=roots[j]) || !test_pair(i,j,r.tested)) continue;
                            if(!existing && triangles_overlap(read_triangle(reference+6*i),read_triangle(reference+6*j),filtered_orientation)) continue;
                            const I found=roots[i]==roots[j]?1:2;
                            if(!other) {
                                r.kind=found;r.wi=i;r.wj=j;
                                I prev=best_i.load(std::memory_order_relaxed);
                                while(i<prev && !best_i.compare_exchange_weak(prev,i,std::memory_order_relaxed)) {}
                                results[t]=r;
                                return;
                            }
                            if(r.kind && r.kind!=found) {
                                r.kind=3;
                                found_both.store(true,std::memory_order_relaxed);
                                results[t]=r;
                                return;
                            }
                            r.kind=found;
                        }
                    }
                    results[t]=r;
                });
            }
        }
        for(const auto& r:results) tested+=r.tested;
        if(!other) {
            for(const auto& r:results) {
                if(r.kind) {
                    if(witness) {witness[0]=r.wi;witness[1]=r.wj;}
                    output[0]=r.kind;output[1]=tested;return 0;
                }
            }
            output[0]=0;output[1]=tested;return 0;
        }
        for(const auto& r:results) {
            if(!r.kind) continue;
            if(r.kind==3 || (kind && kind!=r.kind)) {output[0]=3;output[1]=tested;return 0;}
            kind=r.kind;
        }
        output[0]=kind;output[1]=tested;return 0;
    }catch(...){return -3;}
}

// The legacy ABI never takes the witness shortcut.
API int uvck_check_overlaps(I n,const I* faces,const I* roots,const std::uint8_t* changed,
                            const double* current,const double* reference,I other,I existing,I* output) noexcept {
    return check_overlaps_impl(n,faces,roots,changed,current,reference,other,existing,output,nullptr);
}

// Contact recovery keeps the legacy ordered witness search on one spatial grid.
API int uvck_find_self_overlap_pair(I n,const I* faces,const double* current,I* output,I* pair) noexcept {
    if(n<0 || n>10000000 || !faces || !current || !output || !pair) return -1;
    pair[0]=pair[1]=-1;
    // The legacy partner probes build subset grids. This range ensures every
    // possible subset grid (cell >= 1e-12) can use native integer cells.
    for(I k=0;k<6*n;++k) {
        if(!std::isfinite(current[k]) || std::abs(current[k])>1e6) return 2;
    }
    return check_overlaps_impl<true>(n,faces,nullptr,nullptr,current,current,0,1,output,pair);
}

// Optional private ABI. witness[0:2] is a numeric row hint, witness[2] reports
// a freshly confirmed early rejection. Buffers must be valid and disjoint
// except that current/reference may alias. No clean result skips the full scan.
API int uvck_check_overlaps_witness(I n,const I* faces,const I* roots,const std::uint8_t* changed,
                            const double* current,const double* reference,I other,I existing,I* output,I* witness) noexcept {
    auto clear=[&]() {if(witness) {witness[0]=witness[1]=-1;witness[2]=0;}};
    try {
        if(n<0 || !faces || !roots || !changed || !current || !reference || !output) {clear();return -1;}
        I i=witness?witness[0]:-1,j=witness?witness[1]:-1;
        clear();
        // Bound the optional scan and its index products. Other sizes retain
        // the legacy implementation, including its allocation/error policy.
        // Blender can flush denormals. Match the full check's predicate mode
        // without changing the caller's floating-point environment.
        const auto filtered=orientation_filter_mode();
        bool nearest=std::fegetround()==FE_TONEAREST;
#if defined(_M_X64) || defined(__SSE__)
        nearest=nearest && !(_mm_getcsr()&0x6000U);
#endif
        bool hint=witness && other==0 && existing==1 && n>0 && n<=10000000
            && i>=0 && j>=0 && i<n && j<n && i!=j && nearest;
        // The full grid has cell>=1e-12. This conservative finite range keeps
        // every floor(coord/cell) safely inside its +/-9e18 integer guard.
        if(hint) for(I k=0;k<6*n;++k) {
            if(!std::isfinite(current[k]) || std::abs(current[k])>1e6) {hint=false;break;}
        }
        if(hint) {
            // Match the argument order of the original changed-row scan;
            // floating clipping is not assumed to be symmetric under swap.
            if((!changed[i] && changed[j]) || (changed[i] && changed[j] && j<i)) std::swap(i,j);
            hint=changed[i] && faces[i]!=faces[j] && roots[i]==roots[j];
        }
        if(hint) {
            const auto a=read_triangle(current+6*i),b=read_triangle(current+6*j);
            auto bounds=[](const Triangle& t) {
                return std::array<double,4>{std::min({t[0].x,t[1].x,t[2].x}),std::min({t[0].y,t[1].y,t[2].y}),
                    std::max({t[0].x,t[1].x,t[2].x}),std::max({t[0].y,t[1].y,t[2].y})};
            };
            const auto x=bounds(a),y=bounds(b);
            if(x[0]<y[2] && y[0]<x[2] && x[1]<y[3] && y[1]<x[3] && triangles_overlap(a,b,filtered)) {
                output[0]=1;output[1]=1;witness[0]=i;witness[1]=j;witness[2]=1;return 0;
            }
        }
        const int status=check_overlaps_impl(n,faces,roots,changed,current,reference,other,existing,output,witness);
        if(status || other!=0 || existing!=1 || output[0]!=1) clear();
        return status;
    }catch(...) {clear();return -3;}
}

// Read-only reference frames. Match mathutils float component subtraction,
// float products and reverse double accumulation; solve arithmetic is untouched.
// Precision verified against Blender v5.2.3 mathutils_Vector.cc/math_vector.cc.
API int uvck_triangle_frames(I n,I nt,const float* xyz,const I* triangles,
                             double* frames,std::uint8_t* eligible) noexcept {
    try {
        if(n<0 || nt<0 || n>10000000 || nt>20000000 || !xyz || !triangles || !frames || !eligible) return -1;
        for(I k=0;k<3*nt;++k) if(triangles[k]<0 || triangles[k]>=n) return -1;
        for(I t=0;t<nt;++t) {
            double* frame=frames+6*t;
            std::fill_n(frame,6,0.);
            eligible[t]=0;
            const float* origin=xyz+3*triangles[3*t];
            const float* first=xyz+3*triangles[3*t+1];
            const float* second=xyz+3*triangles[3*t+2];
            float edge[3],other[3];
            for(int k=0;k<3;++k) {edge[k]=first[k]-origin[k];other[k]=second[k]-origin[k];}
            double first_squared=0.,second_squared=0.,product=0.;
            for(int k=2;k>=0;--k) {
                const float a=edge[k]*edge[k],b=other[k]*other[k],c=other[k]*edge[k];
                first_squared+=static_cast<double>(a);
                second_squared+=static_cast<double>(b);
                product+=static_cast<double>(c);
            }
            const double length=std::sqrt(first_squared);
            if(length<1e-12) continue;
            const double x=product/length;
            const double height_squared=second_squared-x*x;
            if(height_squared<=1e-24) continue;
            frame[2]=length;frame[4]=x;frame[5]=std::sqrt(height_squared);
            eligible[t]=1;
        }
        return 0;
    }catch(...){return -3;}
}

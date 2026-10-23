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
    explicit Problem(const Input& in):p(in),soft(in.strength>=0),jacobian(4*in.nt),gpu(make_gpu_session(in)) {
        for(I r=0;r<p.nr;++r) if(p.kinds[r]!=1 && p.kinds[r]!=2) axes_only=false;
        if(!soft && axes_only) for(I kind=1;kind<=2;++kind) {
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
    void project(Vec& x) const {
        if(!soft) {
            if(axes_only) for(const auto& set:sets) {
                double mean=set.anchor;
                if(!set.anchored) {mean=0;for(I i:set.members) mean+=x[2*i+set.axis];mean/=set.members.size();}
                for(I i:set.members) x[2*i+set.axis]=mean;
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
    bool borders_valid(const Vec& x) const {
        if(!p.prevent_overlap) return true;
        auto pair_valid=[&](I i,I j) {
            I ia=p.border_edges[2*i],ib=p.border_edges[2*i+1],ic=p.border_edges[2*j],id=p.border_edges[2*j+1];
            if(ia==ic || ia==id || ib==ic || ib==id) return true;
            const double *a=&x[2*ia],*b=&x[2*ib],*c=&x[2*ic],*d=&x[2*id];
            double margin=std::max(p.overlap_space,0.);
            if(std::max(a[0],b[0])+margin<std::min(c[0],d[0]) || std::max(c[0],d[0])+margin<std::min(a[0],b[0]) ||
               std::max(a[1],b[1])+margin<std::min(c[1],d[1]) || std::max(c[1],d[1])+margin<std::min(a[1],b[1])) return true;
            double abx=b[0]-a[0],aby=b[1]-a[1],cdx=d[0]-c[0],cdy=d[1]-c[1];
            if(cross(abx,aby,c[0]-a[0],c[1]-a[1])*cross(abx,aby,d[0]-a[0],d[1]-a[1])<0 &&
               cross(cdx,cdy,a[0]-c[0],a[1]-c[1])*cross(cdx,cdy,b[0]-c[0],b[1]-c[1])<0) return false;
            if(p.overlap_space>0 && std::min({distance(a,c,d),distance(b,c,d),distance(c,a,b),distance(d,a,b)})<p.overlap_space) return false;
            return true;
        };
        // A broad phase only: retain the same exact pair predicate, including
        // shared vertices, touching edges and the requested spacing margin.
        // Tiny boundaries avoid sorting; nonfinite trials keep the old path.
        struct Bound {double low,high;I index;};
        std::vector<Bound> bounds;
        bool sweep=p.nb>=64;
        if(sweep) {
            bounds.reserve(p.nb);
            for(I i=0;i<p.nb;++i) {
                I a=p.border_edges[2*i],b=p.border_edges[2*i+1];
                if(!std::isfinite(x[2*a]) || !std::isfinite(x[2*a+1]) ||
                   !std::isfinite(x[2*b]) || !std::isfinite(x[2*b+1])) {sweep=false;break;}
                bounds.push_back({std::min(x[2*a],x[2*b]),std::max(x[2*a],x[2*b]),i});
            }
        }
        if(!sweep) {
            for(I i=0;i<p.nb;++i) for(I j=i+1;j<p.nb;++j) if(!pair_valid(i,j)) return false;
            return true;
        }
        std::sort(bounds.begin(),bounds.end(),[](const Bound& a,const Bound& b) {
            return a.low<b.low || (a.low==b.low && a.index<b.index);
        });
        const double margin=std::max(p.overlap_space,0.);
        for(I i=0;i<p.nb;++i) for(I j=i+1;j<p.nb;++j) {
            if(bounds[i].high+margin<bounds[j].low) break;
            I a=bounds[i].index,b=bounds[j].index;
            if(!pair_valid(std::min(a,b),std::max(a,b))) return false;
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
    double energy(const Vec& x) {
        ++evaluations;double e=0;
        Vec gpu_terms;
        if(gpu) {
            gpu_terms.resize(p.nt);
            try {gpu->energy(x,reciprocal,p.prevent_flips!=0,p.angle_length,gpu_terms);}
            catch(const std::exception& error) {gpu_failure(error);gpu.reset();}
        }
        if(!gpu) build_j(x);
        for(I t=0;t<p.nt;++t) {
            if(gpu) {e+=gpu_terms[t];continue;}
            const double* j=&jacobian[4*t];
            for(int k=0;k<4;++k) if(!std::isfinite(j[k])) return INF;
            double det=j[0]*j[3]-j[1]*j[2];
            if((p.prevent_flips || reciprocal) && det<=1e-6) return INF;
            double norm=j[0]*j[0]+j[1]*j[1]+j[2]*j[2]+j[3]*j[3],squared=det*det+1e-16;
            double denominator=(p.prevent_flips || reciprocal)?det:std::sqrt(squared);
            double root=std::sqrt(std::max(norm+2*det,1e-20));
            double length=reciprocal?.5*(norm+norm/squared-4):norm-2*root+2;
            e+=((1-p.angle_length)*norm/denominator+p.angle_length*length)*p.weights[t];
        }
        if(!borders_valid(x)) return INF;
        penalty(x,&e,nullptr);return std::isfinite(e)?e:INF;
    }
    Vec gradient(const Vec& x) {
        Vec gu,g(2*p.n,0);
        if(gpu) {
            try {gpu->gradient(reciprocal,p.prevent_flips!=0,p.angle_length,g);}
            catch(const std::exception& error) {gpu_failure(error);gpu.reset();build_j(x);}
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
        return g;
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
        for(I t=0;t<p.nt;++t) {const double* j=&problem.jacobian[4*t];double d=j[0]*j[3]-j[1]*j[2];if(!std::isfinite(d) || d<=1e-6) problem.reciprocal=false;}
        double e=problem.energy(x);*stats={0,problem.evaluations,problem.reciprocal,0,e,INF};
        if(!std::isfinite(e)) return 1; // Do not publish even the initial projection.
        Vec g=problem.gradient(x);
        struct History {Vec s,y;double rho;};std::vector<History> history;
        I limit=p.automatic?1000:std::max<I>(1,p.iterations);
        for(I iteration=0;iteration<limit;++iteration) {
            if(p.automatic && max_abs(g)<1e-6) break;
            Vec q=g;std::vector<double> coefficients;
            for(auto i=history.rbegin();i!=history.rend();++i) {double alpha=i->rho*dot(i->s,q);coefficients.push_back(alpha);for(std::size_t k=0;k<q.size();++k) q[k]-=alpha*i->y[k];}
            double scale=history.empty()?std::max(p.density*p.density,1e-6)*.05:dot(history.back().s,history.back().y)/std::max(dot(history.back().y,history.back().y),1e-20);
            for(double& v:q) v*=scale;
            for(std::size_t i=0;i<history.size();++i) {double v=coefficients[history.size()-i-1]-history[i].rho*dot(history[i].y,q);for(std::size_t k=0;k<q.size();++k) q[k]+=history[i].s[k]*v;}
            Vec direction=q;for(double& v:direction) v=-v;double slope=dot(g,direction);
            if(slope>=0) {for(std::size_t k=0;k<g.size();++k) direction[k]=-g[k];slope=-dot(g,g);history.clear();}
            double step=1,next_e=INF;Vec candidate(x.size());bool accepted=false;
            for(int search=0;search<32;++search) {
                for(std::size_t k=0;k<x.size();++k) candidate[k]=x[k]+step*direction[k];
                problem.project(candidate);next_e=problem.energy(candidate);
                if(next_e<=e+1e-4*step*slope) {accepted=true;break;}step*=.5;
            }
            if(!accepted) break;
            Vec next_g=problem.gradient(candidate),change(x.size()),y(x.size());
            for(std::size_t k=0;k<x.size();++k) {change[k]=candidate[k]-x[k];y[k]=next_g[k]-g[k];}
            double sy=dot(change,y);if(sy>1e-14) {history.push_back({std::move(change),std::move(y),1/sy});if(history.size()>8) history.erase(history.begin());}
            double improvement=e-next_e;x=std::move(candidate);e=next_e;g=std::move(next_g);++stats->accepted;
            if(p.automatic && improvement<=1e-10*std::max(1.,std::abs(e))) break;
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
        Problem p(*input);p.reciprocal=reciprocal!=0;Vec x(input->uv,input->uv+2*input->n);
        double e=p.energy(x);if(!std::isfinite(e)) {*energy=e;return 1;}Vec g=p.gradient(x);*energy=e;std::copy(g.begin(),g.end(),gradient);return 0;
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

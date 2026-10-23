// SPDX-License-Identifier: GPL-3.0-or-later
// NVRTC supplies CUDA math builtins without host C runtime headers.

extern "C" __global__ void uvck_energy(
    long long nt, const double* uv, const long long* triangles,
    const double* inverse, double* j, const double* weights,
    int reciprocal, int prevent_flips, double angle, double* terms) {
    const long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= nt) return;
    const long long a=triangles[3*t], b=triangles[3*t+1], c=triangles[3*t+2];
    const double* v=inverse+4*t;
    for(int axis=0;axis<2;++axis) {
        const double u=uv[2*b+axis]-uv[2*a+axis], w=uv[2*c+axis]-uv[2*a+axis];
        j[4*t+2*axis]=u*v[0]+w*v[2];
        j[4*t+2*axis+1]=u*v[1]+w*v[3];
    }
    const double j0 = j[4*t], j1 = j[4*t+1], j2 = j[4*t+2], j3 = j[4*t+3];
    if (!isfinite(j0) || !isfinite(j1) || !isfinite(j2) || !isfinite(j3)) {
        terms[t] = __longlong_as_double(0x7ff0000000000000LL);
        return;
    }
    const double det = j0*j3-j1*j2;
    if ((prevent_flips || reciprocal) && det <= 1e-6) {
        terms[t] = __longlong_as_double(0x7ff0000000000000LL);
        return;
    }
    const double norm = j0*j0+j1*j1+j2*j2+j3*j3;
    const double squared = det*det+1e-16;
    const double denominator = (prevent_flips || reciprocal) ? det : sqrt(squared);
    const double root = sqrt((norm+2*det < 1e-20) ? 1e-20 : norm+2*det);
    const double length = reciprocal ? .5*(norm+norm/squared-4) : norm-2*root+2;
    terms[t] = ((1-angle)*norm/denominator+angle*length)*weights[t];
}

extern "C" __global__ void uvck_gradient(
    long long nt, const double* j, const double* inverse, const double* weights,
    int reciprocal, int prevent_flips, double angle, double* gu) {
    const long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= nt) return;
    const double j0 = j[4*t], j1 = j[4*t+1], j2 = j[4*t+2], j3 = j[4*t+3];
    const double det = j0*j3-j1*j2;
    const double norm = j0*j0+j1*j1+j2*j2+j3*j3;
    const double squared = det*det+1e-16;
    const double denominator = (prevent_flips || reciprocal) ? det : sqrt(squared);
    const double derivative = (prevent_flips || reciprocal) ? 1. : det/denominator;
    const double root = sqrt((norm+2*det < 1e-20) ? 1e-20 : norm+2*det);
    double a, b;
    if (reciprocal) {
        a = (1-angle)*2/denominator+angle*(1+1/squared);
        b = -(1-angle)*norm*derivative/(denominator*denominator)
            -angle*norm*det/(squared*squared);
    } else {
        const double rotation = (norm+2*det>1e-20) ? 2*angle/root : 0.;
        a = (1-angle)*2/denominator+2*angle-rotation;
        b = -(1-angle)*norm*derivative/(denominator*denominator)-rotation;
    }
    const double g0 = a*j0+b*j3, g1 = a*j1-b*j2;
    const double g2 = a*j2-b*j1, g3 = a*j3+b*j0;
    const double* inv = inverse+4*t;
    for (int axis=0; axis<2; ++axis) {
        const double x = axis ? g2 : g0, y = axis ? g3 : g1;
        gu[4*t+2*axis] = (x*inv[0]+y*inv[1])*weights[t];
        gu[4*t+2*axis+1] = (x*inv[2]+y*inv[3])*weights[t];
    }
}

// Per-vertex adjacency is built in the original CPU scatter order: corner,
// then ascending triangle index. Each thread owns its two sums; no atomics.
extern "C" __global__ void uvck_scatter(
    long long n, long long nt, const long long* offsets,
    const long long* entries, const double* gu, double* gradient) {
    const long long vertex=(long long)blockIdx.x*blockDim.x+threadIdx.x;
    if(vertex>=n) return;
    for(int axis=0;axis<2;++axis) {
        double sum=0;
        for(long long i=offsets[vertex];i<offsets[vertex+1];++i) {
            const long long code=entries[i],corner=code/nt,t=code%nt;
            const double a=gu[4*t+2*axis],b=gu[4*t+2*axis+1];
            sum+=(corner==0?-a-b:corner==1?a:b);
        }
        gradient[2*vertex+axis]=sum;
    }
}

// SPDX-License-Identifier: GPL-3.0-or-later
// NVRTC supplies CUDA math builtins without host C runtime headers.
// All reductions use fixed block-stride + shared-memory tree order (zero atomics)
// so same-device execution is 100% bitwise deterministic.

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
static constexpr int SC_ALPHA_BASE = 16; // 16..23
static constexpr int SC_RHO_BASE = 24;   // 24..31

static constexpr int INVALID_NONE = 2147483647;

// ---------------------------------------------------------------------------
// Legacy FP64 kernels (kept for uvck_evaluate and non-axis-only fallback)
// ---------------------------------------------------------------------------

extern "C" __global__ void uvck_energy(
    long long nt, const double* uv, const long long* triangles,
    const double* inverse, double* j, const double* weights,
    int reciprocal, int prevent_flips, double angle, double* terms) {
    const long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= nt) return;
    const long long a = triangles[3*t], b = triangles[3*t+1], c = triangles[3*t+2];
    const double* v = inverse + 4*t;
    for (int axis = 0; axis < 2; ++axis) {
        const double u = uv[2*b+axis] - uv[2*a+axis], w = uv[2*c+axis] - uv[2*a+axis];
        j[4*t+2*axis] = u*v[0] + w*v[2];
        j[4*t+2*axis+1] = u*v[1] + w*v[3];
    }
    const double j0 = j[4*t], j1 = j[4*t+1], j2 = j[4*t+2], j3 = j[4*t+3];
    if (!isfinite(j0) || !isfinite(j1) || !isfinite(j2) || !isfinite(j3)) {
        terms[t] = __longlong_as_double(0x7ff0000000000000LL);
        return;
    }
    const double det = j0*j3 - j1*j2;
    if ((prevent_flips || reciprocal) && det <= 1e-6) {
        terms[t] = __longlong_as_double(0x7ff0000000000000LL);
        return;
    }
    const double norm = j0*j0 + j1*j1 + j2*j2 + j3*j3;
    const double squared = det*det + 1e-16;
    const double denominator = (prevent_flips || reciprocal) ? det : sqrt(squared);
    double length;
    if (reciprocal) {
        length = .5*(norm + norm/squared - 4);
    } else {
        const double root = sqrt((norm + 2*det < 1e-20) ? 1e-20 : norm + 2*det);
        length = norm - 2*root + 2;
    }
    terms[t] = ((1 - angle)*norm/denominator + angle*length)*weights[t];
}

extern "C" __global__ void uvck_gradient(
    long long nt, const double* j, const double* inverse, const double* weights,
    int reciprocal, int prevent_flips, double angle, double* gu) {
    const long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= nt) return;
    const double j0 = j[4*t], j1 = j[4*t+1], j2 = j[4*t+2], j3 = j[4*t+3];
    const double det = j0*j3 - j1*j2;
    const double norm = j0*j0 + j1*j1 + j2*j2 + j3*j3;
    const double squared = det*det + 1e-16;
    const double denominator = (prevent_flips || reciprocal) ? det : sqrt(squared);
    const double derivative = (prevent_flips || reciprocal) ? 1. : det/denominator;
    double a, b;
    if (reciprocal) {
        a = (1 - angle)*2/denominator + angle*(1 + 1/squared);
        b = -(1 - angle)*norm*derivative/(denominator*denominator)
            - angle*norm*det/(squared*squared);
    } else {
        const double n2d = norm + 2*det;
        const double rotation = (n2d > 1e-20) ? 2*angle/sqrt(n2d) : 0.;
        a = (1 - angle)*2/denominator + 2*angle - rotation;
        b = -(1 - angle)*norm*derivative/(denominator*denominator) - rotation;
    }
    const double g0 = a*j0 + b*j3, g1 = a*j1 - b*j2;
    const double g2 = a*j2 - b*j1, g3 = a*j3 + b*j0;
    const double* inv = inverse + 4*t;
    for (int axis = 0; axis < 2; ++axis) {
        const double x = axis ? g2 : g0, y = axis ? g3 : g1;
        gu[4*t+2*axis] = (x*inv[0] + y*inv[1])*weights[t];
        gu[4*t+2*axis+1] = (x*inv[2] + y*inv[3])*weights[t];
    }
}

extern "C" __global__ void uvck_scatter(
    long long n, long long nt, const long long* offsets,
    const long long* entries, const double* gu, double* gradient) {
    const long long vertex = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (vertex >= n) return;
    for (int axis = 0; axis < 2; ++axis) {
        double sum = 0;
        for (long long i = offsets[vertex]; i < offsets[vertex+1]; ++i) {
            const long long code = entries[i], corner = code/nt, t = code%nt;
            const double a = gu[4*t+2*axis], b = gu[4*t+2*axis+1];
            sum += (corner == 0 ? -a - b : corner == 1 ? a : b);
        }
        gradient[2*vertex+axis] = sum;
    }
}

// ---------------------------------------------------------------------------
// Conversion helpers (on-device FP64 <-> FP32, int64 -> int32)
// ---------------------------------------------------------------------------

extern "C" __global__ void uvck_cast_d2f(int count, const double* src, float* dst) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dst[i] = (float)src[i];
}

extern "C" __global__ void uvck_cast_f2d(int count, const float* src, double* dst) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dst[i] = (double)src[i];
}

extern "C" __global__ void uvck_cast_i64_to_i32(int count, const long long* src, int* dst) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dst[i] = (int)src[i];
}

// ---------------------------------------------------------------------------
// Deterministic block reductions (always 256 threads per block, zero atomics)
// ---------------------------------------------------------------------------

extern "C" __global__ void uvck_reduce_sum_to_slot(
    int nblocks, const double* block_sums, double* scalars, int slot) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double acc = 0.0;
    for (int i = tid; i < nblocks; i += 256) acc += block_sums[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) scalars[slot] = sdata[0];
}

extern "C" __global__ void uvck_reduce_energy_final(
    int nblocks, const double* block_sums, const int* block_inv, double* scalars) {
    __shared__ double ssum[256];
    __shared__ int sinv[256];
    const int tid = threadIdx.x;
    double acc = 0.0;
    int inv = INVALID_NONE;
    for (int i = tid; i < nblocks; i += 256) {
        acc += block_sums[i];
        int bi = block_inv[i];
        if (bi < inv) inv = bi;
    }
    ssum[tid] = acc;
    sinv[tid] = inv;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            ssum[tid] += ssum[tid + s];
            if (sinv[tid + s] < sinv[tid]) sinv[tid] = sinv[tid + s];
        }
        __syncthreads();
    }
    if (tid == 0) {
        if (sinv[0] < INVALID_NONE || !isfinite(ssum[0])) {
            scalars[SC_ENERGY] = __longlong_as_double(0x7ff0000000000000LL);
            scalars[SC_INVALID] = (double)(sinv[0] < INVALID_NONE ? sinv[0] : -1);
        } else {
            scalars[SC_ENERGY] = ssum[0];
            scalars[SC_INVALID] = -1.0;
        }
    }
}

extern "C" __global__ void uvck_reduce_alpha(
    int nblocks, const double* block_sums, double* scalars, int hist_idx) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double acc = 0.0;
    for (int i = tid; i < nblocks; i += 256) acc += block_sums[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) {
        scalars[SC_ALPHA_BASE + hist_idx] = scalars[SC_RHO_BASE + hist_idx] * sdata[0];
    }
}

extern "C" __global__ void uvck_reduce_beta(
    int nblocks, const double* block_sums, double* scalars, int hist_idx) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double acc = 0.0;
    for (int i = tid; i < nblocks; i += 256) acc += block_sums[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) {
        scalars[SC_BETA] = scalars[SC_ALPHA_BASE + hist_idx] - scalars[SC_RHO_BASE + hist_idx] * sdata[0];
    }
}

extern "C" __global__ void uvck_reduce_max_to_slot(
    int nblocks, const double* block_max, double* scalars, int slot) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double m = 0.0;
    for (int i = tid; i < nblocks; i += 256) m = fmax(m, block_max[i]);
    sdata[tid] = m;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmax(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) scalars[slot] = sdata[0];
}

extern "C" __global__ void uvck_reduce_min_to_slot(
    int nblocks, const double* block_min, double* scalars, int slot) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double m = 1.0;
    for (int i = tid; i < nblocks; i += 256) m = fmin(m, block_min[i]);
    sdata[tid] = m;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmin(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) scalars[slot] = sdata[0];
}

extern "C" __global__ void uvck_reduce_sy_ymy_maxg(
    int nblocks, const double* b_sy, const double* b_ymy, const double* b_maxg,
    double* scalars, int hist_slot) {
    __shared__ double s_sy[256];
    __shared__ double s_ymy[256];
    __shared__ double s_mg[256];
    const int tid = threadIdx.x;
    double sy = 0.0, ymy = 0.0, mg = 0.0;
    for (int i = tid; i < nblocks; i += 256) {
        sy += b_sy[i];
        ymy += b_ymy[i];
        mg = fmax(mg, b_maxg[i]);
    }
    s_sy[tid] = sy; s_ymy[tid] = ymy; s_mg[tid] = mg;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            s_sy[tid] += s_sy[tid + s];
            s_ymy[tid] += s_ymy[tid + s];
            s_mg[tid] = fmax(s_mg[tid], s_mg[tid + s]);
        }
        __syncthreads();
    }
    if (tid == 0) {
        const double final_sy = s_sy[0];
        const double final_ymy = s_ymy[0];
        scalars[SC_SY] = final_sy;
        scalars[SC_YMY] = final_ymy;
        scalars[SC_MAX_G] = s_mg[0];
        if (final_sy > 1e-14 && isfinite(final_sy) && isfinite(final_ymy)) {
            scalars[SC_RHO_BASE + hist_slot] = 1.0 / final_sy;
            scalars[SC_GAMMA] = final_sy / fmax(final_ymy, 1e-20);
        }
    }
}

extern "C" __global__ void uvck_add_pen_energy(int nblocks, const double* block_sums, double* scalars) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    double acc = 0.0;
    for (int i = tid; i < nblocks; i += 256) acc += block_sums[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) {
        scalars[SC_PEN_E] = sdata[0];
        if (isfinite(scalars[SC_ENERGY])) {
            scalars[SC_ENERGY] += sdata[0];
        }
    }
}

// ---------------------------------------------------------------------------
// FP32 Resident Solver Kernels (64x throughput on Ada / Ampere)
// ---------------------------------------------------------------------------

extern "C" __global__ void uvck_energy_block_f32(
    int nt, const float* uv, const int* tri, const float* inv,
    float* j_out, const float* weights, int reciprocal, int prevent_flips,
    float angle, double* block_sums, int* block_inv) {
    __shared__ double ssum[256];
    __shared__ int sinv[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double local_sum = 0.0;
    int local_inv = INVALID_NONE;
    for (int t = blockIdx.x * 256 + tid; t < nt; t += stride) {
        const int a = tri[3*t], b = tri[3*t+1], c = tri[3*t+2];
        const float* v = inv + 4*t;
        const float u0 = uv[2*b] - uv[2*a], w0 = uv[2*c] - uv[2*a];
        const float u1 = uv[2*b+1] - uv[2*a+1], w1 = uv[2*c+1] - uv[2*a+1];
        const float j0 = u0*v[0] + w0*v[2], j1 = u0*v[1] + w0*v[3];
        const float j2 = u1*v[0] + w1*v[2], j3 = u1*v[1] + w1*v[3];
        j_out[4*t] = j0; j_out[4*t+1] = j1; j_out[4*t+2] = j2; j_out[4*t+3] = j3;
        const float det = j0*j3 - j1*j2;
        if (!isfinite(j0) || !isfinite(j1) || !isfinite(j2) || !isfinite(j3) || !isfinite(det) ||
            ((prevent_flips || reciprocal) && det <= 1.2e-6f)) {
            if (t < local_inv) local_inv = t;
            continue;
        }
        const float norm = j0*j0 + j1*j1 + j2*j2 + j3*j3;
        const float squared = det*det + 1e-16f;
        const float denom = (prevent_flips || reciprocal) ? det : sqrtf(squared);
        float length;
        if (reciprocal) {
            length = 0.5f * (norm + norm/squared - 4.0f);
        } else {
            const float n2d = norm + 2.0f * det;
            const float root = sqrtf(n2d > 1e-20f ? n2d : 1e-20f);
            length = norm - 2.0f*root + 2.0f;
        }
        const float term = ((1.0f - angle)*norm/denom + angle*length) * weights[t];
        if (!isfinite(term)) {
            if (t < local_inv) local_inv = t;
            continue;
        }
        local_sum += (double)term;
    }
    ssum[tid] = local_sum;
    sinv[tid] = local_inv;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            ssum[tid] += ssum[tid + s];
            if (sinv[tid + s] < sinv[tid]) sinv[tid] = sinv[tid + s];
        }
        __syncthreads();
    }
    if (tid == 0) {
        block_sums[blockIdx.x] = ssum[0];
        block_inv[blockIdx.x] = sinv[0];
    }
}

extern "C" __global__ void uvck_grad_f32(
    int nt, const float* j, const float* inv, const float* weights,
    int reciprocal, int prevent_flips, float angle, float* gu) {
    const int t = blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= nt) return;
    const float j0 = j[4*t], j1 = j[4*t+1], j2 = j[4*t+2], j3 = j[4*t+3];
    const float det = j0*j3 - j1*j2;
    const float norm = j0*j0 + j1*j1 + j2*j2 + j3*j3;
    const float squared = det*det + 1e-16f;
    const float denom = (prevent_flips || reciprocal) ? det : sqrtf(squared);
    const float deriv = (prevent_flips || reciprocal) ? 1.0f : det/denom;
    float a, b;
    if (reciprocal) {
        a = (1.0f - angle)*2.0f/denom + angle*(1.0f + 1.0f/squared);
        b = -(1.0f - angle)*norm*deriv/(denom*denom) - angle*norm*det/(squared*squared);
    } else {
        const float n2d = norm + 2.0f * det;
        const float rot = (n2d > 1e-20f) ? 2.0f*angle/sqrtf(n2d) : 0.0f;
        a = (1.0f - angle)*2.0f/denom + 2.0f*angle - rot;
        b = -(1.0f - angle)*norm*deriv/(denom*denom) - rot;
    }
    const float g0 = a*j0 + b*j3, g1 = a*j1 - b*j2;
    const float g2 = a*j2 - b*j1, g3 = a*j3 + b*j0;
    const float* iv = inv + 4*t;
    const float w = weights[t];
    gu[4*t]   = (g0*iv[0] + g1*iv[1]) * w;
    gu[4*t+1] = (g0*iv[2] + g1*iv[3]) * w;
    gu[4*t+2] = (g2*iv[0] + g3*iv[1]) * w;
    gu[4*t+3] = (g2*iv[2] + g3*iv[3]) * w;
}

extern "C" __global__ void uvck_scatter_f32(
    int n, int nt, const int* offsets, const int* entries,
    const float* gu, float* grad) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v >= n) return;
    double s0 = 0.0, s1 = 0.0;
    const int lo = offsets[v], hi = offsets[v+1];
    for (int i = lo; i < hi; ++i) {
        const int code = entries[i];
        const int corner = code / nt;
        const int t = code - corner * nt;
        const float a0 = gu[4*t],   b0 = gu[4*t+1];
        const float a1 = gu[4*t+2], b1 = gu[4*t+3];
        s0 += (double)(corner == 0 ? (-a0 - b0) : (corner == 1 ? a0 : b0));
        s1 += (double)(corner == 0 ? (-a1 - b1) : (corner == 1 ? a1 : b1));
    }
    grad[2*v]   = (float)s0;
    grad[2*v+1] = (float)s1;
}

extern "C" __global__ void uvck_penalty_energy_block_f32(
    int ne, const float* uv, const int* pen_edges, const int* pen_axis,
    const float* pen_w, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int e = blockIdx.x * 256 + tid; e < ne; e += stride) {
        const int a = pen_edges[2*e], b = pen_edges[2*e+1], ax = pen_axis[e];
        const double d = (double)uv[2*b+ax] - (double)uv[2*a+ax];
        acc += (double)pen_w[e] * d * d;
    }
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_penalty_grad_f32(
    int n_pen_verts, const int* pv_ids, const int* pv_offsets,
    const int* pv_edges, const int* pen_edges, const int* pen_axis,
    const float* pen_w, const float* uv, float* grad) {
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= n_pen_verts) return;
    const int v = pv_ids[idx];
    double g0 = (double)grad[2*v], g1 = (double)grad[2*v+1];
    const int lo = pv_offsets[idx], hi = pv_offsets[idx+1];
    for (int k = lo; k < hi; ++k) {
        const int code = pv_edges[k];
        const int end = code & 1;
        const int e = code >> 1;
        const int a = pen_edges[2*e], b = pen_edges[2*e+1], ax = pen_axis[e];
        const double d = (double)uv[2*b+ax] - (double)uv[2*a+ax];
        const double contrib = (end ? 2.0 : -2.0) * (double)pen_w[e] * d;
        if (ax == 0) g0 += contrib; else g1 += contrib;
    }
    grad[2*v] = (float)g0;
    grad[2*v+1] = (float)g1;
}

extern "C" __global__ void uvck_project_axes_f32(
    int nsets, const int* set_offsets, const int* set_members,
    const int* set_axis, const unsigned char* set_anchored,
    const double* set_anchor, float* x) {
    const int s = blockIdx.x * blockDim.x + threadIdx.x;
    if (s >= nsets) return;
    const int lo = set_offsets[s], hi = set_offsets[s+1];
    if (hi <= lo) return;
    const int ax = set_axis[s];
    double mean = set_anchor[s];
    if (!set_anchored[s]) {
        double sum = 0.0;
        for (int k = lo; k < hi; ++k) sum += (double)x[2*set_members[k] + ax];
        mean = sum / (double)(hi - lo);
    }
    const float mf = (float)mean;
    for (int k = lo; k < hi; ++k) x[2*set_members[k] + ax] = mf;
}

extern "C" __global__ void uvck_project_grad_axes_f32(
    int nsets, const int* set_offsets, const int* set_members,
    const int* set_axis, const unsigned char* set_anchored, float* g) {
    const int s = blockIdx.x * blockDim.x + threadIdx.x;
    if (s >= nsets) return;
    const int lo = set_offsets[s], hi = set_offsets[s+1];
    if (hi <= lo) return;
    const int ax = set_axis[s];
    double mean = 0.0;
    if (!set_anchored[s]) {
        double sum = 0.0;
        for (int k = lo; k < hi; ++k) sum += (double)g[2*set_members[k] + ax];
        mean = sum / (double)(hi - lo);
    }
    const float mf = (float)mean;
    for (int k = lo; k < hi; ++k) g[2*set_members[k] + ax] = mf;
}

extern "C" __global__ void uvck_apply_fixed_f32(
    int n, const unsigned char* fixed, const float* held, float* x) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v < n && fixed[v]) { x[2*v] = held[2*v]; x[2*v+1] = held[2*v+1]; }
}

extern "C" __global__ void uvck_zero_fixed_f32(
    int n, const unsigned char* fixed, float* g) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v < n && fixed[v]) { g[2*v] = 0.0f; g[2*v+1] = 0.0f; }
}

extern "C" __global__ void uvck_copy_f32(int count, const float* src, float* dst) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dst[i] = src[i];
}

extern "C" __global__ void uvck_dot_block_f32(
    int count, const float* a, const float* b, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride)
        acc += (double)a[i] * (double)b[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_axpy_slot_f32(
    int count, float* y, const float* x, const double* scalars, int slot, double sign) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        const float alpha = (float)(sign * scalars[slot]);
        y[i] += alpha * x[i];
    }
}

extern "C" __global__ void uvck_scale_diag_slot_f32(
    int count, float* dir, const float* inv_diag, const double* scalars,
    int use_gamma_slot, double init_scale, double max_disp) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        double sc = use_gamma_slot ? scalars[SC_GAMMA] : init_scale;
        if (!use_gamma_slot) {
            const double mg = scalars[SC_MAX_G];
            if (mg > 0.0 && max_disp > 0.0 && sc * mg > max_disp) sc = max_disp / mg;
        }
        dir[i] = (float)(sc * (double)inv_diag[i] * (double)dir[i]);
    }
}

extern "C" __global__ void uvck_negate_dot_block_f32(
    int count, float* dir, const float* g, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride) {
        const float d = -dir[i];
        dir[i] = d;
        acc += (double)g[i] * (double)d;
    }
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_cand_step_f32(
    int count, const float* x, const float* dir, float step, float* cand) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) cand[i] = x[i] + step * dir[i];
}

extern "C" __global__ void uvck_cand_trial0_f32(
    int count, const float* x, float* dir, const float* g, const float* inv_diag,
    double* scalars, double max_disp, float* cand) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        if (scalars[SC_SLOPE] >= 0.0 || !isfinite(scalars[SC_SLOPE])) {
            const double mg = scalars[SC_MAX_G];
            const double rs = (mg > max_disp && max_disp > 0.0) ? (max_disp / mg) : 1.0;
            const float d = (float)(-rs * (double)inv_diag[i] * (double)g[i]);
            dir[i] = d;
            cand[i] = x[i] + d;
        } else {
            cand[i] = x[i] + dir[i];
        }
    }
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        scalars[SC_RESET_FLAG] = (scalars[SC_SLOPE] >= 0.0 || !isfinite(scalars[SC_SLOPE])) ? 1.0 : 0.0;
    }
}

extern "C" __global__ void uvck_max_abs_block_f32(
    int count, const float* a, double* block_max) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double m = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride)
        m = fmax(m, (double)fabsf(a[i]));
    sdata[tid] = m;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmax(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) block_max[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_sy_ymy_block_f32(
    int count, const float* cand, const float* x, const float* next_g, const float* g,
    const float* inv_diag, float* s_out, float* y_out,
    double* b_sy, double* b_ymy, double* b_maxg) {
    __shared__ double s_sy[256];
    __shared__ double s_ymy[256];
    __shared__ double s_mg[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double sy = 0.0, ymy = 0.0, mg = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride) {
        const float sv = cand[i] - x[i];
        const float ng = next_g[i];
        const float yv = ng - g[i];
        s_out[i] = sv;
        y_out[i] = yv;
        sy += (double)sv * (double)yv;
        ymy += (double)yv * (double)inv_diag[i] * (double)yv;
        mg = fmax(mg, (double)fabsf(ng));
    }
    s_sy[tid] = sy; s_ymy[tid] = ymy; s_mg[tid] = mg;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            s_sy[tid] += s_sy[tid + s];
            s_ymy[tid] += s_ymy[tid + s];
            s_mg[tid] = fmax(s_mg[tid], s_mg[tid + s]);
        }
        __syncthreads();
    }
    if (tid == 0) {
        b_sy[blockIdx.x] = s_sy[0];
        b_ymy[blockIdx.x] = s_ymy[0];
        b_maxg[blockIdx.x] = s_mg[0];
    }
}

extern "C" __global__ void uvck_max_flip_step_block_f32(
    int nt, const int* tri, const float* x, const float* cand, double* block_min) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double t_min = 1.0;
    for (int t = blockIdx.x * 256 + tid; t < nt; t += stride) {
        const int a = tri[3*t], b = tri[3*t+1], c = tri[3*t+2];
        const double u0x = (double)x[2*b] - (double)x[2*a], u0y = (double)x[2*b+1] - (double)x[2*a+1];
        const double v0x = (double)x[2*c] - (double)x[2*a], v0y = (double)x[2*c+1] - (double)x[2*a+1];
        const double A0 = u0x*v0y - u0y*v0x;
        if (!isfinite(A0) || A0 == 0.0) continue;
        const double dux = ((double)cand[2*b] - (double)x[2*b]) - ((double)cand[2*a] - (double)x[2*a]);
        const double duy = ((double)cand[2*b+1] - (double)x[2*b+1]) - ((double)cand[2*a+1] - (double)x[2*a+1]);
        const double dvx = ((double)cand[2*c] - (double)x[2*c]) - ((double)cand[2*a] - (double)x[2*a]);
        const double dvy = ((double)cand[2*c+1] - (double)x[2*c+1]) - ((double)cand[2*a+1] - (double)x[2*a+1]);
        const double s = (A0 >= 0.0) ? 1.0 : -1.0;
        const double c0 = s * A0;
        const double c1 = s * (dux*v0y - duy*v0x + u0x*dvy - u0y*dvx);
        const double c2 = s * (dux*dvy - duy*dvx);
        double t_star = 2.0;
        if (fabs(c2) <= 1e-20 * (fabs(c1) + c0)) {
            if (c1 < 0.0) t_star = -c0 / c1;
        } else if (c2 < 0.0) {
            const double disc = fmax(0.0, c1*c1 - 4.0*c2*c0);
            const double q = sqrt(disc);
            t_star = (c1 >= 0.0) ? (c1 + q) / (-2.0*c2) : (2.0*c0) / (-c1 + q);
        } else {
            const double disc = c1*c1 - 4.0*c2*c0;
            if (c1 < 0.0 && disc >= 0.0) t_star = (2.0*c0) / (-c1 + sqrt(disc));
        }
        if (t_star > 0.0 && isfinite(t_star)) t_min = fmin(t_min, t_star);
    }
    sdata[tid] = t_min;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmin(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) block_min[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_gather_border_f32(
    int nv_b, const int* border_verts, const float* x, double* out) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < nv_b) {
        const int v = border_verts[i];
        out[2*i] = (double)x[2*v];
        out[2*i+1] = (double)x[2*v+1];
    }
}

// ---------------------------------------------------------------------------
// FP64 Resident Polish Kernels (exact double precision on GPU, zero PCIe syncs)
// ---------------------------------------------------------------------------

extern "C" __global__ void uvck_energy_block_d(
    int nt, const double* uv, const int* tri, const double* inv,
    double* j_out, const double* weights, int reciprocal, int prevent_flips,
    double angle, double* block_sums, int* block_inv) {
    __shared__ double ssum[256];
    __shared__ int sinv[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double local_sum = 0.0;
    int local_inv = INVALID_NONE;
    for (int t = blockIdx.x * 256 + tid; t < nt; t += stride) {
        const int a = tri[3*t], b = tri[3*t+1], c = tri[3*t+2];
        const double* v = inv + 4*t;
        const double u0 = uv[2*b] - uv[2*a], w0 = uv[2*c] - uv[2*a];
        const double u1 = uv[2*b+1] - uv[2*a+1], w1 = uv[2*c+1] - uv[2*a+1];
        const double j0 = u0*v[0] + w0*v[2], j1 = u0*v[1] + w0*v[3];
        const double j2 = u1*v[0] + w1*v[2], j3 = u1*v[1] + w1*v[3];
        j_out[4*t] = j0; j_out[4*t+1] = j1; j_out[4*t+2] = j2; j_out[4*t+3] = j3;
        const double det = j0*j3 - j1*j2;
        if (!isfinite(j0) || !isfinite(j1) || !isfinite(j2) || !isfinite(j3) || !isfinite(det) ||
            ((prevent_flips || reciprocal) && det <= 1e-6)) {
            if (t < local_inv) local_inv = t;
            continue;
        }
        const double norm = j0*j0 + j1*j1 + j2*j2 + j3*j3;
        const double squared = det*det + 1e-16;
        const double denom = (prevent_flips || reciprocal) ? det : sqrt(squared);
        double length;
        if (reciprocal) {
            length = 0.5 * (norm + norm/squared - 4.0);
        } else {
            const double n2d = norm + 2.0 * det;
            const double root = sqrt(n2d > 1e-20 ? n2d : 1e-20);
            length = norm - 2.0*root + 2.0;
        }
        const double term = ((1.0 - angle)*norm/denom + angle*length) * weights[t];
        if (!isfinite(term)) {
            if (t < local_inv) local_inv = t;
            continue;
        }
        local_sum += term;
    }
    ssum[tid] = local_sum;
    sinv[tid] = local_inv;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            ssum[tid] += ssum[tid + s];
            if (sinv[tid + s] < sinv[tid]) sinv[tid] = sinv[tid + s];
        }
        __syncthreads();
    }
    if (tid == 0) {
        block_sums[blockIdx.x] = ssum[0];
        block_inv[blockIdx.x] = sinv[0];
    }
}

extern "C" __global__ void uvck_scatter_i32_d(
    int n, int nt, const int* offsets, const int* entries,
    const double* gu, double* grad) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v >= n) return;
    double s0 = 0.0, s1 = 0.0;
    const int lo = offsets[v], hi = offsets[v+1];
    for (int i = lo; i < hi; ++i) {
        const int code = entries[i];
        const int corner = code / nt;
        const int t = code - corner * nt;
        const double a0 = gu[4*t],   b0 = gu[4*t+1];
        const double a1 = gu[4*t+2], b1 = gu[4*t+3];
        s0 += (corner == 0 ? (-a0 - b0) : (corner == 1 ? a0 : b0));
        s1 += (corner == 0 ? (-a1 - b1) : (corner == 1 ? a1 : b1));
    }
    grad[2*v] = s0;
    grad[2*v+1] = s1;
}

extern "C" __global__ void uvck_penalty_energy_block_d(
    int ne, const double* uv, const int* pen_edges, const int* pen_axis,
    const double* pen_w, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int e = blockIdx.x * 256 + tid; e < ne; e += stride) {
        const int a = pen_edges[2*e], b = pen_edges[2*e+1], ax = pen_axis[e];
        const double d = uv[2*b+ax] - uv[2*a+ax];
        acc += pen_w[e] * d * d;
    }
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_penalty_grad_d(
    int n_pen_verts, const int* pv_ids, const int* pv_offsets,
    const int* pv_edges, const int* pen_edges, const int* pen_axis,
    const double* pen_w, const double* uv, double* grad) {
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= n_pen_verts) return;
    const int v = pv_ids[idx];
    double g0 = grad[2*v], g1 = grad[2*v+1];
    const int lo = pv_offsets[idx], hi = pv_offsets[idx+1];
    for (int k = lo; k < hi; ++k) {
        const int code = pv_edges[k];
        const int end = code & 1;
        const int e = code >> 1;
        const int a = pen_edges[2*e], b = pen_edges[2*e+1], ax = pen_axis[e];
        const double d = uv[2*b+ax] - uv[2*a+ax];
        const double contrib = (end ? 2.0 : -2.0) * pen_w[e] * d;
        if (ax == 0) g0 += contrib; else g1 += contrib;
    }
    grad[2*v] = g0;
    grad[2*v+1] = g1;
}

extern "C" __global__ void uvck_project_axes_d(
    int nsets, const int* set_offsets, const int* set_members,
    const int* set_axis, const unsigned char* set_anchored,
    const double* set_anchor, double* x) {
    const int s = blockIdx.x * blockDim.x + threadIdx.x;
    if (s >= nsets) return;
    const int lo = set_offsets[s], hi = set_offsets[s+1];
    if (hi <= lo) return;
    const int ax = set_axis[s];
    double mean = set_anchor[s];
    if (!set_anchored[s]) {
        double sum = 0.0;
        for (int k = lo; k < hi; ++k) sum += x[2*set_members[k] + ax];
        mean = sum / (double)(hi - lo);
    }
    for (int k = lo; k < hi; ++k) x[2*set_members[k] + ax] = mean;
}

extern "C" __global__ void uvck_project_grad_axes_d(
    int nsets, const int* set_offsets, const int* set_members,
    const int* set_axis, const unsigned char* set_anchored, double* g) {
    const int s = blockIdx.x * blockDim.x + threadIdx.x;
    if (s >= nsets) return;
    const int lo = set_offsets[s], hi = set_offsets[s+1];
    if (hi <= lo) return;
    const int ax = set_axis[s];
    double mean = 0.0;
    if (!set_anchored[s]) {
        double sum = 0.0;
        for (int k = lo; k < hi; ++k) sum += g[2*set_members[k] + ax];
        mean = sum / (double)(hi - lo);
    }
    for (int k = lo; k < hi; ++k) g[2*set_members[k] + ax] = mean;
}

extern "C" __global__ void uvck_apply_fixed_d(
    int n, const unsigned char* fixed, const double* held, double* x) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v < n && fixed[v]) { x[2*v] = held[2*v]; x[2*v+1] = held[2*v+1]; }
}

extern "C" __global__ void uvck_zero_fixed_d(
    int n, const unsigned char* fixed, double* g) {
    const int v = blockIdx.x * blockDim.x + threadIdx.x;
    if (v < n && fixed[v]) { g[2*v] = 0.0; g[2*v+1] = 0.0; }
}

extern "C" __global__ void uvck_copy_d(int count, const double* src, double* dst) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dst[i] = src[i];
}

extern "C" __global__ void uvck_dot_block_d(
    int count, const double* a, const double* b, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride)
        acc += a[i] * b[i];
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_axpy_slot_d(
    int count, double* y, const double* x, const double* scalars, int slot, double sign) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) y[i] += (sign * scalars[slot]) * x[i];
}

extern "C" __global__ void uvck_scale_diag_slot_d(
    int count, double* dir, const double* inv_diag, const double* scalars,
    int use_gamma_slot, double init_scale, double max_disp) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        double sc = use_gamma_slot ? scalars[SC_GAMMA] : init_scale;
        if (!use_gamma_slot) {
            const double mg = scalars[SC_MAX_G];
            if (mg > 0.0 && max_disp > 0.0 && sc * mg > max_disp) sc = max_disp / mg;
        }
        dir[i] = sc * inv_diag[i] * dir[i];
    }
}

extern "C" __global__ void uvck_negate_dot_block_d(
    int count, double* dir, const double* g, double* block_sums) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double acc = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride) {
        const double d = -dir[i];
        dir[i] = d;
        acc += g[i] * d;
    }
    sdata[tid] = acc;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }
    if (tid == 0) block_sums[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_cand_step_d(
    int count, const double* x, const double* dir, double step, double* cand) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) cand[i] = x[i] + step * dir[i];
}

extern "C" __global__ void uvck_cand_trial0_d(
    int count, const double* x, double* dir, const double* g, const double* inv_diag,
    double* scalars, double max_disp, double* cand) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        if (scalars[SC_SLOPE] >= 0.0 || !isfinite(scalars[SC_SLOPE])) {
            const double mg = scalars[SC_MAX_G];
            const double rs = (mg > max_disp && max_disp > 0.0) ? (max_disp / mg) : 1.0;
            const double d = -rs * inv_diag[i] * g[i];
            dir[i] = d;
            cand[i] = x[i] + d;
        } else {
            cand[i] = x[i] + dir[i];
        }
    }
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        scalars[SC_RESET_FLAG] = (scalars[SC_SLOPE] >= 0.0 || !isfinite(scalars[SC_SLOPE])) ? 1.0 : 0.0;
    }
}

extern "C" __global__ void uvck_max_abs_block_d(
    int count, const double* a, double* block_max) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double m = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride)
        m = fmax(m, fabs(a[i]));
    sdata[tid] = m;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmax(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) block_max[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_sy_ymy_block_d(
    int count, const double* cand, const double* x, const double* next_g, const double* g,
    const double* inv_diag, double* s_out, double* y_out,
    double* b_sy, double* b_ymy, double* b_maxg) {
    __shared__ double s_sy[256];
    __shared__ double s_ymy[256];
    __shared__ double s_mg[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double sy = 0.0, ymy = 0.0, mg = 0.0;
    for (int i = blockIdx.x * 256 + tid; i < count; i += stride) {
        const double sv = cand[i] - x[i];
        const double ng = next_g[i];
        const double yv = ng - g[i];
        s_out[i] = sv;
        y_out[i] = yv;
        sy += sv * yv;
        ymy += yv * inv_diag[i] * yv;
        mg = fmax(mg, fabs(ng));
    }
    s_sy[tid] = sy; s_ymy[tid] = ymy; s_mg[tid] = mg;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) {
            s_sy[tid] += s_sy[tid + s];
            s_ymy[tid] += s_ymy[tid + s];
            s_mg[tid] = fmax(s_mg[tid], s_mg[tid + s]);
        }
        __syncthreads();
    }
    if (tid == 0) {
        b_sy[blockIdx.x] = s_sy[0];
        b_ymy[blockIdx.x] = s_ymy[0];
        b_maxg[blockIdx.x] = s_mg[0];
    }
}

extern "C" __global__ void uvck_max_flip_step_block_d(
    int nt, const int* tri, const double* x, const double* cand, double* block_min) {
    __shared__ double sdata[256];
    const int tid = threadIdx.x;
    const int stride = gridDim.x * 256;
    double t_min = 1.0;
    for (int t = blockIdx.x * 256 + tid; t < nt; t += stride) {
        const int a = tri[3*t], b = tri[3*t+1], c = tri[3*t+2];
        const double u0x = x[2*b] - x[2*a], u0y = x[2*b+1] - x[2*a+1];
        const double v0x = x[2*c] - x[2*a], v0y = x[2*c+1] - x[2*a+1];
        const double A0 = u0x*v0y - u0y*v0x;
        if (!isfinite(A0) || A0 == 0.0) continue;
        const double dux = (cand[2*b] - x[2*b]) - (cand[2*a] - x[2*a]);
        const double duy = (cand[2*b+1] - x[2*b+1]) - (cand[2*a+1] - x[2*a+1]);
        const double dvx = (cand[2*c] - x[2*c]) - (cand[2*a] - x[2*a]);
        const double dvy = (cand[2*c+1] - x[2*c+1]) - (cand[2*a+1] - x[2*a+1]);
        const double s = (A0 >= 0.0) ? 1.0 : -1.0;
        const double c0 = s * A0;
        const double c1 = s * (dux*v0y - duy*v0x + u0x*dvy - u0y*dvx);
        const double c2 = s * (dux*dvy - duy*dvx);
        double t_star = 2.0;
        if (fabs(c2) <= 1e-20 * (fabs(c1) + c0)) {
            if (c1 < 0.0) t_star = -c0 / c1;
        } else if (c2 < 0.0) {
            const double disc = fmax(0.0, c1*c1 - 4.0*c2*c0);
            const double q = sqrt(disc);
            t_star = (c1 >= 0.0) ? (c1 + q) / (-2.0*c2) : (2.0*c0) / (-c1 + q);
        } else {
            const double disc = c1*c1 - 4.0*c2*c0;
            if (c1 < 0.0 && disc >= 0.0) t_star = (2.0*c0) / (-c1 + sqrt(disc));
        }
        if (t_star > 0.0 && isfinite(t_star)) t_min = fmin(t_min, t_star);
    }
    sdata[tid] = t_min;
    __syncthreads();
    for (int s = 128; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] = fmin(sdata[tid], sdata[tid + s]);
        __syncthreads();
    }
    if (tid == 0) block_min[blockIdx.x] = sdata[0];
}

extern "C" __global__ void uvck_gather_border_d(
    int nv_b, const int* border_verts, const double* x, double* out) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < nv_b) {
        const int v = border_verts[i];
        out[2*i] = x[2*v];
        out[2*i+1] = x[2*v+1];
    }
}

"""布をつまんで動かすための幾何計算(bpy 非依存。M7)。

ビューポートでは、マウスの下の面を拾い、その面で一番近い頂点をつまむ。
動かすときは、つまんだ頂点を通って**カメラに正対する平面**の上でマウスを
追う。奥行きが変わらないので、画面上で動かした分だけ布が動く
(先行事例 Taremin Cloth と同じやり方)。
"""

import math


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def ray_plane(origin, direction, plane_point, plane_normal):
    """レイと平面の交点。平行、または交点がレイの後ろにあれば None。"""
    denom = _dot(direction, plane_normal)
    if abs(denom) < 1e-12:
        return None
    t = _dot(_sub(plane_point, origin), plane_normal) / denom
    if t < 0.0:
        return None
    return (origin[0] + direction[0] * t,
            origin[1] + direction[1] * t,
            origin[2] + direction[2] * t)


def ray_triangles(origin, direction, positions, triangles):
    """レイが最初に当たる三角形 (番号, 当たった点)。当たらなければ None。

    positions は (N, 3)、triangles は (M, 3) の頂点番号。Möller–Trumbore 法を
    全三角形にまとめて掛ける。Blender の `Object.ray_cast` は、布の形を
    メッシュに書いた直後だと古い形に当たることがあった(ヘッドレスで再現)ので、
    シミュレーションの座標に直接当てる。
    """
    import numpy as np
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    t = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if len(t) == 0:
        return None
    o = np.asarray(origin, dtype=np.float64)
    d = np.asarray(direction, dtype=np.float64)
    v0, v1, v2 = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    e1, e2 = v1 - v0, v2 - v0
    h = np.cross(d, e2)
    a = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(a) > 1e-12
    f = np.zeros_like(a)
    f[ok] = 1.0 / a[ok]
    s = o - v0
    u = f * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, e1)
    v = f * (q @ d)
    dist = f * np.einsum("ij,ij->i", e2, q)
    hit = ok & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0) & (dist > 1e-9)
    if not hit.any():
        return None
    candidates = np.where(hit)[0]
    best = candidates[np.argmin(dist[candidates])]
    point = o + d * dist[best]
    return int(best), tuple(float(c) for c in point)


def nearest_vertex(point, candidates, positions):
    """candidates(頂点番号)のうち point に一番近いもの。positions は平坦な配列。"""
    best, best_d = None, math.inf
    for i in candidates:
        p = positions[i * 3:i * 3 + 3]
        d = math.dist(point, p)
        if d < best_d:
            best, best_d = i, d
    return best


def drag_target(start, anchor_hit, now_hit):
    """つまんだ頂点の目標位置。つまんだ瞬間のマウスとのずれを保って動かす。

    頂点そのものではなく面の上の点をつまむので、差分で動かさないと
    つまんだ瞬間に頂点がマウスの位置まで跳ぶ。
    """
    return (start[0] + now_hit[0] - anchor_hit[0],
            start[1] + now_hit[1] - anchor_hit[1],
            start[2] + now_hit[2] - anchor_hit[2])

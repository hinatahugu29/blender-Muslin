"""2D の Delaunay 三角形分割(bpy 非依存、numpy のみ)。

Blender に scipy は無いので Bowyer-Watson 法で自前に持つ。点の挿入順は入力の順で、
乱数を使わないので結果は決定的。外周の辺(制約辺)は扱わない。呼び出し側が
三角形の重心の内外判定で不要な三角形を落とし、外周の辺が残っているか確かめる。
"""

import numpy as np


def _circumcircles(p, tris):
    """三角形ごとの外接円の (中心, 半径²)。面積が 0 の三角形は中心が nan(どの点も内側に入れない)。"""
    a, b, c = p[tris[:, 0]], p[tris[:, 1]], p[tris[:, 2]]
    d = 2.0 * (a[:, 0] * (b[:, 1] - c[:, 1]) + b[:, 0] * (c[:, 1] - a[:, 1])
               + c[:, 0] * (a[:, 1] - b[:, 1]))
    a2 = (a ** 2).sum(axis=1)
    b2 = (b ** 2).sum(axis=1)
    c2 = (c ** 2).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ux = (a2 * (b[:, 1] - c[:, 1]) + b2 * (c[:, 1] - a[:, 1]) + c2 * (a[:, 1] - b[:, 1])) / d
        uy = (a2 * (c[:, 0] - b[:, 0]) + b2 * (a[:, 0] - c[:, 0]) + c2 * (b[:, 0] - a[:, 0])) / d
    centre = np.stack([ux, uy], axis=1)
    centre[np.abs(d) < 1e-300] = np.nan
    r2 = ((a - centre) ** 2).sum(axis=1)
    return centre, r2


def triangulate(points):
    """点(n, 2)を三角形分割し、反時計回りの三角形 (m, 3) を返す。

    点が 3 つ未満、または全部が一直線なら空を返す。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = len(pts)
    if n < 3:
        return np.zeros((0, 3), dtype=np.int64)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = float(max(hi[0] - lo[0], hi[1] - lo[1]))
    if span <= 0.0:
        return np.zeros((0, 3), dtype=np.int64)
    mid = (lo + hi) / 2.0
    big = span * 50.0
    # 全体を囲む大きな三角形(反時計回り)
    super_pts = np.array([[mid[0] - big, mid[1] - big],
                          [mid[0] + big, mid[1] - big],
                          [mid[0], mid[1] + big]])
    p = np.vstack([pts, super_pts])
    tris = np.array([[n, n + 1, n + 2]], dtype=np.int64)
    centre, r2 = _circumcircles(p, tris)

    for i in range(n):
        d2 = ((centre - p[i]) ** 2).sum(axis=1)
        bad = d2 < r2 * (1.0 - 1e-12)
        if not bad.any():
            continue
        bt = tris[bad]
        edges = np.concatenate([bt[:, [0, 1]], bt[:, [1, 2]], bt[:, [2, 0]]])
        key = np.sort(edges, axis=1)
        _u, inverse, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
        rim = edges[counts[inverse.ravel()] == 1]
        new = np.column_stack([rim[:, 0], rim[:, 1], np.full(len(rim), i, dtype=np.int64)])
        keep = ~bad
        nc, nr = _circumcircles(p, new)
        tris = np.vstack([tris[keep], new])
        centre = np.vstack([centre[keep], nc])
        r2 = np.concatenate([r2[keep], nr])

    tris = tris[(tris < n).all(axis=1)]
    # 向きをそろえ、面積 0 の三角形を落とす
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    flip = area2 < 0
    tris[flip] = tris[flip][:, [0, 2, 1]]
    return tris[np.abs(area2) > 1e-14 * span * span]


def signed_areas(points, tris):
    """三角形ごとの符号付き面積(反時計回りが正)。"""
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    return 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                  - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))


def point_in_rings(points, rings):
    """点が、輪郭の集まりの内側にあるか(偶奇則。穴は内側の輪郭として渡す)。

    points: (m, 2) / rings: [(k, 2), ...] 閉じた折れ線(始点を終点に繰り返さない)。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    inside = np.zeros(len(pts), dtype=bool)
    for ring in rings:
        r = np.asarray(ring, dtype=np.float64)
        x1, y1 = r[:, 0], r[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        for k in range(len(r)):
            if y1[k] == y2[k]:
                continue
            crosses = (pts[:, 1] > min(y1[k], y2[k])) & (pts[:, 1] <= max(y1[k], y2[k]))
            xi = x1[k] + (pts[:, 1] - y1[k]) * (x2[k] - x1[k]) / (y2[k] - y1[k])
            inside ^= crosses & (pts[:, 0] < xi)
    return inside


def distance_to_rings(points, rings):
    """点から輪郭の折れ線までの最短距離(m,)。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    best = np.full(len(pts), np.inf)
    for ring in rings:
        r = np.asarray(ring, dtype=np.float64)
        a = r
        b = np.roll(r, -1, axis=0)
        ab = b - a
        denom = (ab ** 2).sum(axis=1)
        denom[denom == 0] = 1.0
        for k in range(len(r)):
            t = np.clip(((pts - a[k]) @ ab[k]) / denom[k], 0.0, 1.0)
            proj = a[k] + t[:, None] * ab[k]
            best = np.minimum(best, np.linalg.norm(pts - proj, axis=1))
    return best

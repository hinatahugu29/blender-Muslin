"""Rebuild のときに、旧メッシュの姿勢(着せた形)を新しい頂点へ引き継ぐ(bpy 非依存)。

頂点数や接続が変わる Rebuild では、旧頂点と新頂点に 1 対 1 の対応がない。3D 空間の
最近傍で写すと意味のない対応になる(服の別の場所に引き寄せられる)ので、**型紙空間(2D)**を
介す。旧メッシュの三角形の中での重心座標で、旧頂点の現在の 3D 位置を補間する。

旧メッシュの型紙空間の座標は「今の Curve の形」でなければならない(Shape Update で
形が変わっていれば、生成時の座標ではずれる)。そこで旧メッシュの境界頂点を、今の Curve の
上に(区間 uid と弧長比で)置き直し、内部は緩和で決める(`old_pattern_now`)。Curve に点が
足された区間は、足された点をまたいだ弧長比で対応させる。

旧メッシュの外側(Curve が広がった分)の新頂点は、最も近い三角形の縁へ寄せて補間する。
その数は呼び出し側に返す(多ければ姿勢が歪むので警告する材料になる)。
"""

import numpy as np

try:                                    # アドオンとして読み込まれたとき
    from . import curve_eval
    from . import curve_update
except ImportError:                     # scripts/verify_core.py が単体で読み込むとき
    import curve_eval
    import curve_update

OUTSIDE_TOLERANCE = 1e-6
_CHUNK = 64


def _walk(current_uids, ia, ib, step):
    """点 ia から step 方向に、既知の点にぶつかるまで進む。ib にぶつかれば通った区間の index を返す。"""
    n = len(current_uids)
    segs = []
    i = ia
    for _ in range(n):
        nxt = (i + step) % n
        segs.append(i if step > 0 else nxt)
        i = nxt
        if i == ib:
            return segs
        if current_uids[i] is not None:
            return None          # 別の既知の点にぶつかった(a と b の間にあった点が消えた)
    return None


def _path_points(segs, step, fractions):
    """区間の並び(index)を連結した道を、弧長比 fractions の位置で評価する。"""
    lengths = np.array([s.length for s in segs])
    total = float(lengths.sum())
    cum = np.concatenate([[0.0], np.cumsum(lengths)])
    target = np.asarray(fractions, dtype=np.float64) * total
    out = np.empty((len(target), 2))
    for k, t in enumerate(target):
        j = int(np.clip(np.searchsorted(cum, t, side="right") - 1, 0, len(segs) - 1))
        local = (t - cum[j]) / lengths[j] if lengths[j] > 1e-15 else 0.0
        if step < 0:
            local = 1.0 - local
        out[k] = segs[j].point_at([local])[0]
    return out


def old_pattern_now(mesh, outlines):
    """旧メッシュの各頂点が、今の Curve の形のどこにあるか(型紙空間の座標)。

    mesh: `curve_pattern.reconstruct` が返す旧メッシュの離散化の結果
    outlines: {輪郭の uid: {"uids"(未知の点は None), "co", "hl", "hr"}}(今の Curve)
    戻り値: (N, 2) の座標、または None(旧メッシュの区間が今の Curve に見つからない)。
    None のときは、呼び出し側が生成時の座標で代用する(形が変わっていれば近似になる)。
    """
    indices, xy = [], []
    for ring in mesh["rings"]:
        current = outlines.get(ring["outline_uid"])
        if current is None:
            return None
        uids = list(current["uids"])
        segs = curve_eval.segments(current["co"], current["hl"], current["hr"], True)
        known = {u: i for i, u in enumerate(uids) if u is not None}
        v = ring["vertices"]
        a = mesh["seg_start"][v]
        b = mesh["seg_end"][v]
        u = mesh["u"][v]
        for key in sorted({(int(x), int(y)) for x, y in zip(a, b)}):
            if key[0] not in known or key[1] not in known:
                return None
            ia, ib = known[key[0]], known[key[1]]
            path = _walk(uids, ia, ib, +1)
            step = +1
            if path is None:
                path = _walk(uids, ia, ib, -1)
                step = -1
            if path is None:
                return None
            sel = np.nonzero((a == key[0]) & (b == key[1]))[0]
            indices.append(v[sel])
            xy.append(_path_points([segs[k] for k in path], step, u[sel]))
    if not indices:
        return None
    idx = np.concatenate(indices)
    bxy = np.vstack(xy)
    return curve_update.relax_interior(mesh, idx, bxy)


def locate(points, old_xy, old_tris, point_piece=None, old_piece=None):
    """新しい点が、旧メッシュのどの三角形のどこ(重心座標)にあるか。

    戻り値: (三角形の index (n,), 重心座標 (n, 3), 外側だった点の数)。
    旧メッシュの外側の点は、最も近い三角形へ重心座標を [0, 1] に丸めて寄せる。
    piece を渡せば、同じピースの三角形だけを探す。
    """
    pts = np.asarray(points, dtype=np.float64)
    old_xy = np.asarray(old_xy, dtype=np.float64)
    tris = np.asarray(old_tris, dtype=np.int64)
    a, b, c = old_xy[tris[:, 0]], old_xy[tris[:, 1]], old_xy[tris[:, 2]]
    v0, v1 = b - a, c - a
    d00 = (v0 * v0).sum(axis=1)
    d01 = (v0 * v1).sum(axis=1)
    d11 = (v1 * v1).sum(axis=1)
    denom = d00 * d11 - d01 * d01
    ok = np.abs(denom) > 1e-30
    denom = np.where(ok, denom, 1.0)
    tri_piece = None
    if point_piece is not None and old_piece is not None:
        tri_piece = np.asarray(old_piece)[tris[:, 0]]
        point_piece = np.asarray(point_piece)

    tri_index = np.empty(len(pts), dtype=np.int64)
    bary = np.empty((len(pts), 3))
    outside = 0
    for s in range(0, len(pts), _CHUNK):
        p = pts[s:s + _CHUNK]
        w = p[:, None, :] - a[None, :, :]
        d20 = (w * v0[None]).sum(axis=2)
        d21 = (w * v1[None]).sum(axis=2)
        v = (d11 * d20 - d01 * d21) / denom
        u = (d00 * d21 - d01 * d20) / denom
        lam = np.stack([1.0 - v - u, v, u], axis=2)           # (chunk, M, 3)
        worst = lam.min(axis=2)                                # 負が大きいほど外側
        worst = np.where(ok[None, :], worst, -np.inf)
        if tri_piece is not None:
            same = tri_piece[None, :] == point_piece[s:s + _CHUNK, None]
            worst = np.where(same, worst, -np.inf)
        best = worst.argmax(axis=1)
        rows = np.arange(len(p))
        chosen = lam[rows, best]
        outside += int((worst[rows, best] < -OUTSIDE_TOLERANCE).sum())
        chosen = np.clip(chosen, 0.0, None)
        chosen /= np.maximum(chosen.sum(axis=1, keepdims=True), 1e-30)
        tri_index[s:s + _CHUNK] = best
        bary[s:s + _CHUNK] = chosen
    return tri_index, bary, outside


def transfer(old_xy, old_tris, old_pose, new_xy, old_piece=None, new_piece=None):
    """旧メッシュの姿勢(3D の頂点位置)を、新しい頂点へ写す。

    old_xy / new_xy: 型紙空間の座標 / old_pose: 旧頂点の 3D 位置 (N, 3)
    戻り値: (新頂点の 3D 位置 (M, 3), 旧メッシュの外側だった新頂点の数)
    """
    tris = np.asarray(old_tris, dtype=np.int64)
    pose = np.asarray(old_pose, dtype=np.float64)
    index, bary, outside = locate(new_xy, old_xy, tris, new_piece, old_piece)
    corners = pose[tris[index]]                     # (M, 3, 3)
    return (bary[:, :, None] * corners).sum(axis=1), outside

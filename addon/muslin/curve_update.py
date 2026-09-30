"""Shape Update: 離散化済みのメッシュを、topology を保ったまま新しい Curve に追従させる。

bpy 非依存。頂点数と接続は生成時のまま(`curve_discretize.discretize` の結果)で、
座標だけを更新する。

- 境界の頂点: 生成時に記録した (区間, 弧長比 u) から、今の Curve を評価し直す。閉形式で決定的
- 内部の頂点: 境界を固定し、生成時の辺長の逆数を重みにしたバネ緩和(重み付きラプラシアンの
  ディリクレ問題)を共役勾配法で解く。反復は上限と許容値を固定してあり、結果は決定的

更新後の形の品質(反転した三角形・辺の伸縮・最小角)が許容を外れたら、その形は採用せず
`Rebuild Required` として理由を返す。呼び出し側は最後に成功した形を保持する。
"""

import numpy as np

try:                                    # アドオンとして読み込まれたとき
    from . import curve_discretize
    from . import curve_eval
    from . import delaunay2d
except ImportError:                     # scripts/verify_core.py が単体で読み込むとき
    import curve_discretize
    import curve_eval
    import delaunay2d

# 更新後の辺長 / 生成時の辺長 がこの範囲を外れたら Rebuild Required
EDGE_RATIO_RANGE = (0.6, 1.6)
# 最小角(度)。これを下回る三角形ができたら Rebuild Required
MIN_ANGLE_DEGREES = 8.0

CG_MAX_ITERATIONS = 400
CG_TOLERANCE = 1e-12

SHAPE_UPDATE = "shape_update"
REBUILD_REQUIRED = "rebuild_required"


def boundary_positions(mesh, outlines):
    """境界頂点の新しい座標。

    outlines: {輪郭の uid: {"uids", "co", "hl", "hr"}}(今の Curve)
    戻り値: {頂点番号の配列, 座標 (k, 2)} を (indices, xy) で。
    今の輪郭に生成時の区間が無ければ ValueError(点の構成が変わっている)。
    """
    indices, xy = [], []
    for ring in mesh["rings"]:
        current = outlines.get(ring["outline_uid"])
        if current is None:
            raise ValueError(f"輪郭 {ring['outline_uid']} が今の Curve にありません")
        uids = list(current["uids"])
        n = len(uids)
        segs = curve_eval.segments(current["co"], current["hl"], current["hr"], True)
        lookup = {}
        for k in range(n):
            lookup[(uids[k], uids[(k + 1) % n])] = (k, False)
            lookup[(uids[(k + 1) % n], uids[k])] = (k, True)

        v = ring["vertices"]
        a = mesh["seg_start"][v]
        b = mesh["seg_end"][v]
        u = mesh["u"][v]
        for key in sorted({(int(x), int(y)) for x, y in zip(a, b)}):
            if key not in lookup:
                raise ValueError(f"区間 {key[0]}→{key[1]} が今の Curve にありません")
            k, reversed_ = lookup[key]
            sel = np.nonzero((a == key[0]) & (b == key[1]))[0]
            uu = 1.0 - u[sel] if reversed_ else u[sel]
            indices.append(v[sel])
            xy.append(segs[k].point_at(uu))
    return np.concatenate(indices), np.vstack(xy)


def relax_interior(mesh, boundary_indices, boundary_xy, initial=None):
    """境界を固定して内部の頂点を決める(重み 1/L₀ のバネ緩和)。

    座標そのものではなく、生成時の座標からの**変位**に対して境界値問題を解く。
    こうすると Curve が変わっていないとき(変位 0)は内部も生成時のまま動かない
    (生成時の内部の配置は格子点で、バネの釣り合いの位置とは限らないので)。
    initial(省略時は生成時の座標)は共役勾配法の出発点。
    戻り値: 全頂点の座標 (N, 2)
    """
    gen = mesh["positions"]
    n = len(gen)
    is_boundary = np.zeros(n, dtype=bool)
    is_boundary[boundary_indices] = True
    interior = np.nonzero(~is_boundary)[0]

    disp = np.zeros((n, 2))
    disp[boundary_indices] = boundary_xy - gen[boundary_indices]
    if len(interior) == 0:
        return gen + disp
    if initial is not None:
        start = np.asarray(initial, dtype=np.float64) - gen
        disp[interior] = start[interior]

    edges = curve_discretize.mesh_edges(mesh["triangles"])
    i, j = edges[:, 0], edges[:, 1]
    w = 1.0 / np.maximum(np.linalg.norm(gen[i] - gen[j], axis=1), 1e-12)
    degree = np.bincount(i, weights=w, minlength=n) + np.bincount(j, weights=w, minlength=n)

    def laplacian(v):
        out = degree * v
        out -= np.bincount(i, weights=w * v[j], minlength=n)
        out -= np.bincount(j, weights=w * v[i], minlength=n)
        return out

    def apply(vec):
        return np.where(is_boundary, 0.0, laplacian(np.where(is_boundary, 0.0, vec)))

    for axis in range(2):
        d = disp[:, axis].copy()
        rhs = -laplacian(np.where(is_boundary, d, 0.0))
        v = np.where(is_boundary, 0.0, d)
        r = np.where(is_boundary, 0.0, rhs - apply(v))
        p = r.copy()
        rs = float(r @ r)
        scale = max(float(rhs @ rhs), 1e-30)
        for _ in range(CG_MAX_ITERATIONS):
            if rs <= CG_TOLERANCE * scale:
                break
            ap = apply(p)
            alpha = rs / float(p @ ap)
            v = v + alpha * p
            r = r - alpha * ap
            rs_new = float(r @ r)
            p = r + (rs_new / rs) * p
            rs = rs_new
        d[interior] = v[interior]
        disp[:, axis] = d
    return gen + disp


def quality(mesh, positions):
    """形の品質。戻り値: dict(inverted, edge_ratio_min, edge_ratio_max, min_angle)"""
    tris = mesh["triangles"]
    areas = delaunay2d.signed_areas(positions, tris)
    edges = curve_discretize.mesh_edges(tris)
    gen = mesh["positions"]
    l0 = np.linalg.norm(gen[edges[:, 0]] - gen[edges[:, 1]], axis=1)
    l1 = np.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)
    ratio = l1 / np.maximum(l0, 1e-12)

    p = positions[tris]
    angles = []
    for k in range(3):
        a = p[:, (k + 1) % 3] - p[:, k]
        b = p[:, (k + 2) % 3] - p[:, k]
        cos = (a * b).sum(axis=1) / np.maximum(
            np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-30)
        angles.append(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))
    return {
        "inverted": int((areas <= 0).sum()),
        "edge_ratio_min": float(ratio.min()),
        "edge_ratio_max": float(ratio.max()),
        "min_angle": float(np.min(angles)),
    }


def problems(q):
    """品質が許容を外れていれば、理由の一覧を返す。"""
    out = []
    if q["inverted"]:
        out.append(f"反転した三角形が {q['inverted']} 個あります")
    lo, hi = EDGE_RATIO_RANGE
    if q["edge_ratio_min"] < lo or q["edge_ratio_max"] > hi:
        out.append(f"辺の長さが生成時の {q['edge_ratio_min']:.2f}〜{q['edge_ratio_max']:.2f} 倍に"
                   f"なります(許容 {lo}〜{hi})")
    if q["min_angle"] < MIN_ANGLE_DEGREES:
        out.append(f"最小角が {q['min_angle']:.1f}°になります(許容 {MIN_ANGLE_DEGREES}° 以上)")
    return out


def update(mesh, outlines, target_length, structure_problems=(), initial=None):
    """新しい Curve に追従した座標を作り、採用できるかを判定する。

    structure_problems: `curve_ids` の構造比較が返した理由(空なら構造は同じ)。
      あれば座標は作らず Rebuild Required にする(点の構成が変わっていて評価できない)
    戻り値: dict(status, positions or None, reasons, quality or None)
    """
    reasons = list(structure_problems)
    if abs(float(target_length) - mesh["target_length"]) > 1e-12:
        reasons.append(f"目標辺長が変わりました({mesh['target_length']:g} → {float(target_length):g})")
    if mesh["algorithm_version"] != curve_discretize.ALGORITHM_VERSION:
        reasons.append("離散化のアルゴリズムが更新されています")
    if reasons:
        return {"status": REBUILD_REQUIRED, "positions": None, "reasons": reasons, "quality": None}

    try:
        indices, xy = boundary_positions(mesh, outlines)
    except ValueError as exc:
        return {"status": REBUILD_REQUIRED, "positions": None, "reasons": [str(exc)], "quality": None}
    positions = relax_interior(mesh, indices, xy, initial=initial)
    q = quality(mesh, positions)
    bad = problems(q)
    if bad:
        return {"status": REBUILD_REQUIRED, "positions": None, "reasons": bad, "quality": q}
    return {"status": SHAPE_UPDATE, "positions": positions, "reasons": [], "quality": q}

"""Curve の輪郭を、シミュレーションできる三角形メッシュへ離散化する(bpy 非依存)。

入力は輪郭(閉じた Bezier)の一覧、出力は「派生データ」の辞書。派生データは
いつでも Curve から作り直せる。ユーザーが直接編集するものではない。

約束(Shape Update が成り立つ理由):
- 制御点は必ず頂点にする。区間ごとに `n = round(長さ / 目標辺長)` を決め、区間内は
  弧長で等分する。だから区間の弧長が変わっても頂点数は変わらず、境界頂点の位置は
  (区間, 弧長比 u)から Curve を評価し直すだけで決まる
- 内部の頂点は正六角格子を輪郭の内側に置き、境界とあわせて Delaunay で三角形にする。
  輪郭に近すぎる格子点は落とす
- 輪郭の辺が三角形の辺として残らなければ(細い凹みなど)失敗として理由を返す。
  黙って壊れた三角形分割を作らない
"""

import numpy as np

try:                                    # アドオンとして読み込まれたとき
    from . import curve_eval
    from . import delaunay2d
except ImportError:                     # scripts/verify_core.py が単体で読み込むとき
    import curve_eval
    import delaunay2d

ALGORITHM_VERSION = 1

# 輪郭からこの距離(目標辺長に対する割合)より近い格子点は置かない
INTERIOR_MARGIN = 0.6


class DiscretizeError(Exception):
    """輪郭から三角形メッシュを作れなかった。"""


def _ring_points(outline, h):
    """1 本の輪郭の境界頂点。

    戻り値: (座標, 区間の始点 uid, 区間の終点 uid, 弧長比, 区間ごとの分割数, 区間ごとの長さ)
    """
    uids = outline["uids"]
    n = len(uids)
    if n < 3:
        raise DiscretizeError(f"輪郭 {outline['piece_uid']} の点が 3 つ未満です")
    segs = curve_eval.segments(outline["co"], outline["hl"], outline["hr"], True)
    xy, seg_start, seg_end, us, counts, lengths = [], [], [], [], [], []
    for k, seg in enumerate(segs):
        if seg.length <= 1e-12:
            raise DiscretizeError(f"輪郭 {outline['piece_uid']} に長さ 0 の区間があります")
        m = max(1, int(round(seg.length / h)))
        frac = np.arange(m, dtype=np.float64) / m
        xy.append(seg.point_at(frac))
        seg_start += [uids[k]] * m
        seg_end += [uids[(k + 1) % n]] * m
        us.append(frac)
        counts.append(m)
        lengths.append(seg.length)
    return (np.vstack(xy), np.array(seg_start), np.array(seg_end),
            np.concatenate(us), counts, lengths)


def _hex_lattice(lo, hi, h):
    dy = h * np.sqrt(3.0) / 2.0
    rows = int(np.ceil((hi[1] - lo[1]) / dy)) + 1
    cols = int(np.ceil((hi[0] - lo[0]) / h)) + 2
    pts = []
    for r in range(rows):
        x0 = lo[0] + (h / 2.0 if r % 2 else 0.0)
        pts.append(np.column_stack([x0 + h * np.arange(cols), np.full(cols, lo[1] + r * dy)]))
    return np.vstack(pts)


def _grid(boundary_xy):
    """境界の頂点が軸にそろった長方形の格子の外枠になっていれば、内側の格子点と三角形を返す。

    マチ(帯)のような長方形のピースは、六角格子を斜めに横切って折れるとしわが寄る
    (角で 90° 折れる帯の、角のまわりの折れ角の和が 600° 前後。縫い目で折れる角は 11°)。
    格子を縦横にそろえれば、境界の頂点の列(角の位置を含む)に縦の辺が通り、そこで素直に折れる。
    外枠になっていなければ None(呼び出し側が六角格子に戻す)。
    戻り値: (内側の点 (k, 2), 三角形 (m, 3)。番号は境界の頂点のあとに内側の点を続けたもの)
    """
    xy = np.asarray(boundary_xy, dtype=np.float64)
    span = float(np.ptp(xy, axis=0).max())
    if span <= 0.0:
        return None
    tol = span * 1e-6

    def levels(values):
        order = np.sort(values)
        keep = [order[0]]
        for v in order[1:]:
            if v - keep[-1] > tol:
                keep.append(v)
        return np.array(keep)

    xs, ys = levels(xy[:, 0]), levels(xy[:, 1])
    nx, ny = len(xs), len(ys)
    if nx < 2 or ny < 2 or len(xy) != 2 * nx + 2 * ny - 4:
        return None
    cell = {}
    for k, (x, y) in enumerate(xy):
        j = int(np.argmin(np.abs(xs - x)))
        i = int(np.argmin(np.abs(ys - y)))
        if abs(xs[j] - x) > tol or abs(ys[i] - y) > tol:
            return None
        if not (j in (0, nx - 1) or i in (0, ny - 1)) or (i, j) in cell:
            return None
        cell[(i, j)] = k
    inner = []
    for i in range(1, ny - 1):
        for j in range(1, nx - 1):
            cell[(i, j)] = len(xy) + len(inner)
            inner.append((xs[j], ys[i]))
    tris = []
    for i in range(ny - 1):
        for j in range(nx - 1):
            a, b, c, d = cell[(i, j)], cell[(i, j + 1)], cell[(i + 1, j + 1)], cell[(i + 1, j)]
            tris += [(a, b, c), (a, c, d)]          # 反時計回り(x が右、y が上)
    inner = np.array(inner, dtype=np.float64).reshape(-1, 2)
    return inner, np.array(tris, dtype=np.int64)


def discretize(outlines, target_length):
    """輪郭の一覧から派生データを作る。

    outlines: [{"piece_uid", "uids", "co", "hl", "hr", "hole_of", "grid"(任意)}, ...]
      hole_of が None なら外周、ピースの piece_uid を入れればその穴。
      grid が真の外周(マチ)は、長方形なら縦横の格子で分ける(`_grid`)
    戻り値: dict(positions, triangles, boundary, seg_start, seg_end, u, piece, rings,
                 segment_counts, segment_lengths, target_length, algorithm_version)
    """
    h = float(target_length)
    if h <= 0:
        raise DiscretizeError("目標辺長は正の値にしてください")

    outers = [o for o in outlines if o.get("hole_of") is None]
    if not outers:
        raise DiscretizeError("外周の輪郭がありません")

    positions, triangles = [], []
    seg_start, seg_end, us, piece_of, boundary = [], [], [], [], []
    rings = []
    segment_counts, segment_lengths = {}, {}
    offset = 0

    for outer in outers:
        pid = outer["piece_uid"]
        holes = [o for o in outlines if o.get("hole_of") == pid]
        ring_defs = [outer] + holes
        piece_xy, ring_slices = [], []
        start = 0
        for o in ring_defs:
            xy, ss, se, uu, counts, lengths = _ring_points(o, h)
            piece_xy.append(xy)
            ring_slices.append((o, start, len(xy), ss, se, uu, counts, lengths))
            start += len(xy)
        boundary_xy = np.vstack(piece_xy)

        grid = _grid(boundary_xy) if outer.get("grid") and not holes else None
        if grid is not None:
            lattice, tris = grid
            pts = np.vstack([boundary_xy, lattice]) if len(lattice) else boundary_xy
        else:
            lo, hi = piece_xy[0].min(axis=0), piece_xy[0].max(axis=0)
            lattice = _hex_lattice(lo, hi, h)
            lattice = lattice[delaunay2d.point_in_rings(lattice, piece_xy)]
            if len(lattice):
                far = delaunay2d.distance_to_rings(lattice, piece_xy) >= INTERIOR_MARGIN * h
                lattice = lattice[far]

            pts = np.vstack([boundary_xy, lattice]) if len(lattice) else boundary_xy
            tris = delaunay2d.triangulate(pts)
            if len(tris):
                centroid = pts[tris].mean(axis=1)
                tris = tris[delaunay2d.point_in_rings(centroid, piece_xy)]
        if len(tris) == 0:
            raise DiscretizeError(f"ピース {pid} を三角形にできませんでした")

        # 輪郭の辺がすべて三角形の辺として残っているか
        edge_set = set()
        for a, b, c in tris:
            for e in ((a, b), (b, c), (c, a)):
                edge_set.add((min(e), max(e)))
        for (_o, s0, cnt, *_rest) in ring_slices:
            for k in range(cnt):
                a, b = s0 + k, s0 + (k + 1) % cnt
                if (min(a, b), max(a, b)) not in edge_set:
                    raise DiscretizeError(
                        f"ピース {pid} の輪郭の辺が三角形分割に残りませんでした"
                        "(輪郭が細すぎるか、自己交差しています)")

        n_b = len(boundary_xy)
        n_i = len(pts) - n_b
        positions.append(pts)
        triangles.append(tris + offset)
        flag = np.zeros(len(pts), dtype=bool)
        flag[:n_b] = True
        boundary.append(flag)
        piece_of.append(np.full(len(pts), pid))
        seg_start.append(np.concatenate([np.concatenate([r[3] for r in ring_slices]),
                                         np.full(n_i, -1)]))
        seg_end.append(np.concatenate([np.concatenate([r[4] for r in ring_slices]),
                                       np.full(n_i, -1)]))
        us.append(np.concatenate([np.concatenate([r[5] for r in ring_slices]),
                                  np.zeros(n_i)]))

        for (o, s0, cnt, _ss, _se, uu, counts, lengths) in ring_slices:
            uids = o["uids"]
            seg_index = np.repeat(np.arange(len(counts)), counts)
            rings.append({
                "piece_uid": pid,
                "outline_uid": o["piece_uid"],
                "hole": o.get("hole_of") is not None,
                "uids": list(uids),
                "vertices": np.arange(s0, s0 + cnt) + offset,
                "s": seg_index + uu,
                "segment_total": len(counts),
            })
            for k, uid in enumerate(uids):
                segment_counts[int(uid)] = counts[k]
                segment_lengths[int(uid)] = lengths[k]
        offset += len(pts)

    return {
        "positions": np.vstack(positions),
        "triangles": np.vstack(triangles),
        "boundary": np.concatenate(boundary),
        "seg_start": np.concatenate(seg_start).astype(np.int64),
        "seg_end": np.concatenate(seg_end).astype(np.int64),
        "u": np.concatenate(us),
        "piece": np.concatenate(piece_of).astype(np.int64),
        "rings": rings,
        "segment_counts": segment_counts,
        "segment_lengths": segment_lengths,
        "target_length": h,
        "algorithm_version": ALGORITHM_VERSION,
    }


def mesh_edges(triangles):
    """三角形から重複のない辺 (k, 2) を返す(各辺は小さい番号が先)。"""
    t = np.asarray(triangles, dtype=np.int64)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e.sort(axis=1)
    return np.unique(e, axis=0)


def boundary_edges(triangles):
    """外周の辺(ちょうど 1 つの三角形にだけ使われる辺)。"""
    t = np.asarray(triangles, dtype=np.int64)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e.sort(axis=1)
    u, counts = np.unique(e, axis=0, return_counts=True)
    return u[counts == 1]


# ---------------------------------------------------------------------------
# Curve の選択 → 境界の辺・頂点(選択の連動)
# ---------------------------------------------------------------------------

def boundary_selection(data, segments, points):
    """Curve で選んだ区間と点に対応する、布の境界の辺と頂点を返す。

    segments: 選んだ区間の集合 {(始点 uid, 終点 uid)}(輪郭の向きに隣り合う 2 点が両方選ばれたもの)
    points: 選んだ点の uid の集合
    data: `discretize`(または布から組み直したもの)。境界の頂点は輪郭に沿って並び、
          頂点 k から k+1 への辺は、頂点 k の区間 (seg_start, seg_end) に属する。
          制御点の頂点は、その点から始まる区間の弧長比 0 にある。
    戻り値: (辺 [(頂点 a, 頂点 b), ...], 頂点 [index, ...])。記録に無い uid は単に何も返さない
    """
    seg_start, seg_end, u = data["seg_start"], data["seg_end"], data["u"]
    edges = []
    if segments:
        for ring in data["rings"]:
            v = ring["vertices"]
            n = len(v)
            for k in range(n):
                a = int(v[k])
                if (int(seg_start[a]), int(seg_end[a])) in segments:
                    edges.append((a, int(v[(k + 1) % n])))
    vertices = []
    if points:
        for ring in data["rings"]:
            for a in ring["vertices"]:
                a = int(a)
                if u[a] == 0.0 and int(seg_start[a]) in points:
                    vertices.append(a)
    return edges, vertices


# ---------------------------------------------------------------------------
# seam の区間 → 境界の辺
# ---------------------------------------------------------------------------

def expand_range(ring, start_uid, t0, end_uid, t1):
    """CurveRange (start_uid, t0, end_uid, t1) を、境界の辺(頂点番号の組)に展開する。

    輪郭の向き(生成時の並び)に沿って、区間 start_uid の弧長比 t0 から、区間 end_uid の
    弧長比 t1 まで。閉じた輪郭なので、終点が始点より手前なら一周して回り込む。
    始点と終点が同じ位置なら一周とみなす。ring は `discretize` の "rings" の 1 要素。
    戻り値: [(頂点 a, 頂点 b), ...] を輪郭の並び順で。
    """
    uids = ring["uids"]
    if start_uid not in uids or end_uid not in uids:
        return []
    total = ring["segment_total"]
    s0 = uids.index(start_uid) + t0
    s1 = uids.index(end_uid) + t1
    span = (s1 - s0) % total
    if span < 1e-9:
        span = float(total)
    s = ring["s"]
    v = ring["vertices"]
    n = len(v)
    eps = 1e-9
    out = []
    for k in range(n):
        rel = (s[k] - s0) % total
        if rel > span - eps and span < total - eps:
            continue            # 範囲の外(終点の頂点は次の辺の始点にはならない)
        nxt = (k + 1) % n
        step = (s[nxt] - s[k]) % total
        if step < eps:
            step = float(total)
        if rel + step <= span + eps:
            out.append((int(v[k]), int(v[nxt])))
    return out

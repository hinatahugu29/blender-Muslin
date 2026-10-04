"""縫い目をまたぐ曲げ制約の頂点の組み立て(bpy 非依存)。

縫い目は距離の制約だけだと蝶番になる(硬い生地を高品質で解くと、縫い目の位置だけ折れ曲がる。
ROADMAP M10)。コアの `set_seam_bending` に、縫い目の隣り合う頂点対ごとの折り目を渡す。

縫い目の A 側の隣り合う頂点 (a0, a1) を共有辺とし、その両側の三角形の対角頂点を折り目の
2 枚の羽根にする。A 側は辺の三角形の 3 番目の頂点、B 側は対応する辺 (b0, b1) の三角形の
3 番目の頂点。静止形状は、B 側の羽根を共有辺の向こうへ展開した仮想の位置で決める
(平らな 1 枚の布なら、そこにあるはずの位置)。型紙(reference)の上で作る。

B 側の頂点が A 側と 1 対 1 で並ばない部分(頂点数の違う辺どうしを縫う場合)は飛ばす。
そこは距離の制約だけが残る。
"""

import numpy as np


def boundary_wings(triangles):
    """外周の辺(ちょうど 1 つの三角形にだけ使われる辺)ごとの、対角頂点。{(小, 大): 頂点}"""
    count = {}
    opposite = {}
    for a, b, c in triangles:
        for u, v, w in ((a, b, c), (b, c, a), (c, a, b)):
            key = (u, v) if u < v else (v, u)
            count[key] = count.get(key, 0) + 1
            opposite[key] = w
    return {k: opposite[k] for k, n in count.items() if n == 1}


def rungs(pairs, chain_a, chain_b, wings, reference):
    """縫い目 1 本ぶんの曲げ制約。

    pairs: 縫い合わせる頂点対 [(a, b), ...](a は chain_a 側、b は chain_b 側)
    chain_a / chain_b: 縫い目の両側の頂点列(辺をたどる順)
    wings: `boundary_wings` の結果(頂点番号は pairs と同じ番号づけ)
    reference: 寸法の基準(型紙)の座標の平坦な配列(同じ番号づけ)
    戻り値: (items, rest)
      items: [(p1, p2, p3, p4, a0, b0, a1, b1), ...]
      rest: 1 件あたり 12 個の数(p1〜p4 の仮想の静止位置)を並べた平坦なリスト
    """
    ref = np.asarray(reference, dtype=np.float64).reshape(-1, 3)
    partners = {}
    for a, b in pairs:
        partners.setdefault(a, []).append(b)
    index_b = {v: i for i, v in enumerate(chain_b)}

    items, rest = [], []
    for i in range(len(chain_a) - 1):
        a0, a1 = chain_a[i], chain_a[i + 1]
        pa, pb = partners.get(a0, []), partners.get(a1, [])
        if len(pa) != 1 or len(pb) != 1:
            continue
        b0, b1 = pa[0], pb[0]
        if b0 == b1 or abs(index_b.get(b0, -10) - index_b.get(b1, -20)) != 1:
            continue
        wing_a = wings.get((a0, a1) if a0 < a1 else (a1, a0))
        wing_b = wings.get((b0, b1) if b0 < b1 else (b1, b0))
        if wing_a is None or wing_b is None:
            continue

        # 仮想の静止形状: B 側の羽根を、共有辺 (a0, a1) の向こう側へ展開する
        p0, p1 = ref[a0], ref[a1]
        e = p1 - p0
        length = float(np.linalg.norm(e))
        if length < 1e-12:
            continue
        eh = e / length
        w_a = ref[wing_a] - p0
        perp_a = w_a - float(w_a @ eh) * eh
        norm_a = float(np.linalg.norm(perp_a))
        if norm_a < 1e-12:
            continue
        u_a = perp_a / norm_a
        eb = ref[b1] - ref[b0]
        len_b = float(np.linalg.norm(eb))
        if len_b < 1e-12:
            continue
        ebh = eb / len_b
        w_b = ref[wing_b] - ref[b0]
        t_b = float(w_b @ ebh)
        h_b = float(np.linalg.norm(w_b - t_b * ebh))
        virtual = p0 + t_b * eh - h_b * u_a

        items.append((a0, a1, wing_a, wing_b, a0, b0, a1, b1))
        rest.extend(p0.tolist() + p1.tolist() + ref[wing_a].tolist() + virtual.tolist())
    return items, rest

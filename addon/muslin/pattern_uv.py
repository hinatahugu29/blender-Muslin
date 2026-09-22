"""型紙から UV を作る(bpy 非依存。M7)。

型紙(`muslin_pattern`、平らな形)をピースごとにその平面へ投影して並べる。
全ピースで縮尺をそろえるので、テクスチャの柄の大きさが型紙の実寸に比例する
(Marvelous Designer の UV と同じ考え方)。着せた後の形ではなく型紙から
作るので、布が伸びたり曲がったりしても柄はゆがまない。
"""

import numpy as np

# ピースどうしの間隔(並べた全体の幅に対する割合ではなく、メートル)。
# テクスチャのにじみがとなりのピースに入らない程度
MARGIN = 0.02


def islands(count, edges):
    """辺でつながった頂点のまとまり(ピース)を返す。頂点番号のリストのリスト。"""
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _plane_axes(points):
    """ピースの平面の (u 軸, v 軸)。v がなるべくワールドの上(+Z)を向くようにする。

    立てて作った型紙なら柄が上下正しく乗る。寝かせて作った型紙(平面が
    ほぼ水平)は +Y を上とみなす。
    """
    centered = points - points.mean(axis=0)
    # 一番薄い方向 = 平面の法線
    normal = np.linalg.svd(centered, full_matrices=False)[2][-1]
    up = np.array([0.0, 0.0, 1.0])
    if abs(normal @ up) > 0.9:
        up = np.array([0.0, 1.0, 0.0])
    v = up - normal * (up @ normal)
    v /= np.linalg.norm(v)
    u = np.cross(v, normal)
    # 法線の向き(表裏)で u が左右反転しないよう、ワールドの +X / +Y 寄りにそろえる
    if u @ np.array([1.0, 1.0, 0.0]) < 0.0:
        u = -u
    return u, v


def layout(positions, edges, margin=MARGIN):
    """頂点ごとの UV と、UV の 1 が何メートルかを返す。

    戻り値: (uvs: (N, 2) ndarray, meters_per_uv: float)
    ピースは平面へ投影したあと、高さの順に棚へ並べ、全体を 0〜1 に収める。
    縮尺は全ピース共通。
    """
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = len(p)
    uvs = np.zeros((n, 2))
    if n == 0:
        return uvs, 1.0

    pieces = []
    for members in islands(n, edges):
        pts = p[members]
        if len(members) >= 3:
            u, v = _plane_axes(pts)
            flat = np.stack([pts @ u, pts @ v], axis=1)
        else:
            flat = pts[:, :2].copy()
        flat -= flat.min(axis=0)
        size = flat.max(axis=0)
        pieces.append((members, flat, size))

    # 棚詰め: 高い順に、目安の幅を超えたら次の段へ
    pieces.sort(key=lambda piece: -piece[2][1])
    total_area = sum((s[0] + margin) * (s[1] + margin) for _, _, s in pieces)
    widest = max(s[0] for _, _, s in pieces)
    shelf_width = max(np.sqrt(total_area) * 1.2, widest + margin)

    x = y = row_height = 0.0
    placed = []
    for members, flat, size in pieces:
        if x > 0.0 and x + size[0] > shelf_width:
            x = 0.0
            y += row_height + margin
            row_height = 0.0
        placed.append((members, flat + np.array([x, y])))
        x += size[0] + margin
        row_height = max(row_height, size[1])

    extent = max(max(f[:, 0].max(), f[:, 1].max()) for _, f in placed)
    extent = max(extent, 1e-9)
    for members, flat in placed:
        uvs[members] = flat / extent
    return uvs, float(extent)

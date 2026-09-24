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


def _plane_axes(points, outward=None):
    """ピースの平面の (u 軸, v 軸)。v がなるべくワールドの上(+Z)を向くようにする。

    立てて作った型紙なら柄が上下正しく乗る。寝かせて作った型紙(平面が
    ほぼ水平)は +Y を上とみなす。

    outward はピースの外側(服の外から見る側)の向き。渡されたら、その側から
    見て柄が左右反転しないように u を決める。渡されなければ、u をワールドの
    +X / +Y 寄りにそろえる(前身頃は正しく読めるが、後ろ身頃は外から見ると
    鏡像になる)。
    """
    centered = points - points.mean(axis=0)
    # 一番薄い方向 = 平面の法線
    normal = np.linalg.svd(centered, full_matrices=False)[2][-1]
    up = np.array([0.0, 0.0, 1.0])
    if abs(normal @ up) > 0.9:
        up = np.array([0.0, 1.0, 0.0])
    v = up - normal * (up @ normal)
    v /= np.linalg.norm(v)
    if outward is not None:
        # 外側から見る人にとっての右 = 上 × 手前向き
        facing = normal if normal @ outward >= 0.0 else -normal
        return np.cross(v, facing), v
    u = np.cross(v, normal)
    if u @ np.array([1.0, 1.0, 0.0]) < 0.0:
        u = -u
    return u, v


# 外側の判定に使う指標(下の _outward の説明を参照)がこれより小さければ、
# 外側を決められないとみなす。平らな単独のピースは 0 になる
OUTWARD_MIN = 0.05


def _outward(pattern, current, triangles, center):
    """ピースの外側を、型紙の座標での向きとして返す。決められなければ None。

    面の巻き順は型紙でも着せた形でも同じなので、「着せた形で面の法線が
    服の中心から離れる向きか」を見れば、型紙の面の法線のどちら側が外かが
    分かる。後ろ身頃のように法線が内側を向いて作られたピースも正しく扱える。

    指標は Σ(面積ベクトル・中心からの向き) を Σ(面積 × 中心からの距離) で
    割ったもの(-1〜1)。中心と同じ平面にある単独のピースでは 0 になる。
    """
    if len(triangles) == 0:
        return None
    t = np.asarray(triangles, dtype=np.int64)
    c0, c1, c2 = current[t[:, 0]], current[t[:, 1]], current[t[:, 2]]
    area = np.cross(c1 - c0, c2 - c0)
    away = (c0 + c1 + c2) / 3.0 - center
    total = np.linalg.norm(area, axis=1) @ np.linalg.norm(away, axis=1)
    if total <= 1e-18:
        return None
    score = float(np.einsum("ij,ij->", area, away)) / total
    if abs(score) < OUTWARD_MIN:
        return None
    p0, p1, p2 = pattern[t[:, 0]], pattern[t[:, 1]], pattern[t[:, 2]]
    winding = np.cross(p1 - p0, p2 - p0).sum(axis=0)
    length = np.linalg.norm(winding)
    if length <= 1e-18:
        return None
    winding /= length
    return winding if score > 0.0 else -winding


def layout(positions, edges, current=None, triangles=None, margin=MARGIN):
    """頂点ごとの UV と、UV の 1 が何メートルかを返す。

    戻り値: (uvs: (N, 2) ndarray, meters_per_uv: float)
    ピースは平面へ投影したあと、高さの順に棚へ並べ、全体を 0〜1 に収める。
    縮尺は全ピース共通。

    current(今の形。着せた形)と triangles を渡すと、ピースごとに外側を
    判定し、服の外から見て柄が左右反転しない向きにする。
    """
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = len(p)
    uvs = np.zeros((n, 2))
    if n == 0:
        return uvs, 1.0

    cur = None
    tri_of = {}
    if current is not None and triangles is not None:
        cur = np.asarray(current, dtype=np.float64).reshape(-1, 3)
        if cur.shape != p.shape:
            cur = None
    if cur is not None:
        center = cur.mean(axis=0)
        owner = np.empty(n, dtype=np.int64)
        groups = islands(n, edges)
        for k, members in enumerate(groups):
            owner[members] = k
        for tri in triangles:
            tri_of.setdefault(int(owner[tri[0]]), []).append(tri)
    else:
        groups = islands(n, edges)

    pieces = []
    for k, members in enumerate(groups):
        pts = p[members]
        if len(members) >= 3:
            outward = None
            if cur is not None:
                outward = _outward(p, cur, tri_of.get(k, []), center)
            u, v = _plane_axes(pts, outward)
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

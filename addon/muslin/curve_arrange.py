"""型紙のピースを人体の周りへ置く(bpy 非依存の幾何)。

Marvelous Designer の「アレンジメントポイント」に当たる操作の土台。平らな型紙のピースを、
縦軸(Z)のまわりの円柱に巻き付ける。ピースの中心を指定した向き(0° = 正面 = -Y、
90° = +X 側、180° = 背面)に置き、幅を円周に沿った弧長として使う。高さ(型紙の y)は
そのまま Z になる。

円柱の半径は、コライダー(体)の頂点のうち、ピースの高さの範囲にあるものの軸からの
最大距離 + 余裕。体が円柱でなくても、その高さの最も張り出した所の外側に置ける。
"""

import numpy as np


class ArrangeError(Exception):
    """ピースを置けなかった。"""


def collider_radius(points, axis_xy, z_min, z_max):
    """コライダーの頂点(ワールド座標 (n, 3))のうち、高さが z_min〜z_max のものの、軸からの最大距離。

    その高さに頂点が無ければ(体が短いなど)、高さが最も近い頂点(2cm 以内の層)で測る。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) == 0:
        raise ArrangeError("コライダーに頂点がありません")
    band = pts[(pts[:, 2] >= z_min) & (pts[:, 2] <= z_max)]
    if len(band) == 0:
        d = np.abs(pts[:, 2] - (z_min + z_max) / 2.0)
        band = pts[d <= d.min() + 0.02]
    return float(np.hypot(band[:, 0] - axis_xy[0], band[:, 1] - axis_xy[1]).max())


def wrap_span(width, radius):
    """幅(弧長)を円周に沿って巻いたときの角度(ラジアン)。"""
    return width / radius


def wrap_cylinder(xy, center_x, radius, angle, axis_xy, z):
    """型紙の座標 xy (n, 2)を、軸 axis_xy のまわりの半径 radius の円柱に巻き付けたワールド座標 (n, 3)。

    center_x: ピースの中心の型紙 x(ここが angle の向きに来る) / angle: ラジアン(0 = -Y 向き)
    z: 各頂点のワールド Z (n,)
    """
    theta = angle + (np.asarray(xy)[:, 0] - center_x) / radius
    out = np.empty((len(xy), 3))
    out[:, 0] = axis_xy[0] + radius * np.sin(theta)
    out[:, 1] = axis_xy[1] - radius * np.cos(theta)
    out[:, 2] = z
    return out


def auto_angles(count):
    """ピース数から、周りに等間隔に置く向き(度)。1 枚は正面、2 枚は正面と背面。"""
    if count <= 0:
        return []
    return [360.0 * i / count for i in range(count)]


def arrange_pieces(pieces, collider_points, axis_xy, margin, angles=None, max_wrap=1.9 * np.pi):
    """複数のピースをまとめて円柱に巻き付ける。

    pieces: [{"xy": (n, 2) 型紙座標, "z": (n,) ワールド Z(平らなときの高さ)}]
    angles: ピースごとの向き(度)。省略すると周りに等間隔
    margin: コライダーの外側に空ける距離
    戻り値: ([(n, 3) ワールド座標, ...], 警告の一覧)
    巻き付く角度が max_wrap を超える(幅が円周に近い)ピースは ArrangeError。
    """
    if angles is None:
        angles = auto_angles(len(pieces))
    if len(angles) != len(pieces):
        raise ArrangeError(f"向きの数({len(angles)})がピース数({len(pieces)})と合いません")
    out, warnings = [], []
    for k, (piece, angle) in enumerate(zip(pieces, angles)):
        xy = np.asarray(piece["xy"], dtype=np.float64)
        z = np.asarray(piece["z"], dtype=np.float64)
        radius = collider_radius(collider_points, axis_xy, float(z.min()), float(z.max())) + margin
        width = float(np.ptp(xy[:, 0]))
        span = wrap_span(width, radius)
        if span > max_wrap:
            raise ArrangeError(
                f"ピース {k + 1} の幅 {width:.2f}m は、半径 {radius:.2f}m の周り(円周 "
                f"{2 * np.pi * radius:.2f}m)にほぼ一周するため巻けません")
        if span > np.pi * 1.2:
            warnings.append(f"ピース {k + 1} は半周以上({np.degrees(span):.0f}°)巻き付きます")
        center_x = float((xy[:, 0].min() + xy[:, 0].max()) / 2.0)
        out.append(wrap_cylinder(xy, center_x, radius, np.radians(angle), axis_xy, z))
    return out, warnings

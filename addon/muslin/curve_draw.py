"""描いた型紙の線の後処理(bpy 非依存)。左右対称の焼き込み。

パターン描画モード(`pattern_draw.py`)で、左右対称を選んで半分だけ描いた線を、
確定のときに閉じた 1 本の輪郭にする。Blender の Curve には Mirror モディファイアを
適用できない(「constructive modifiers」はメッシュにしか適用できない)ので、点を直接書き足す。

対称の軸は Curve のローカル座標の x = 0(縦の線)。
"""

import numpy as np


def _mirror(points):
    p = np.array(points, dtype=np.float64).reshape(-1, 2)
    p[:, 0] = -p[:, 0]
    return p


def mirror_half(co, hl, hr):
    """半分の線(始点と終点が軸の近く)から、左右対称の閉じた輪郭を作る。

    co / hl / hr: 点・左の取っ手・右の取っ手 (n, 2)。線は軸の片側に描いてある前提で、
    始点と終点は軸の上へ寄せる(x = 0)。反対側は逆順に並べてつなぐので、輪郭は 1 周の
    閉じた線になる。軸の上の 2 点は両側で共有し、取っ手は軸をまたいで鏡写しにそろえる
    (軸のところで折れずになめらかにつながる)。
    戻り値: (co, hl, hr) (2n - 2, 2)
    """
    co = np.array(co, dtype=np.float64).reshape(-1, 2)
    hl = np.array(hl, dtype=np.float64).reshape(-1, 2)
    hr = np.array(hr, dtype=np.float64).reshape(-1, 2)
    n = len(co)
    if n < 2:
        raise ValueError("左右対称にするには、点が 2 つ以上要ります")
    # 始点と終点を軸の上へ(取っ手も同じだけずらす)
    for k in (0, n - 1):
        shift = -co[k, 0]
        co[k, 0] = 0.0
        hl[k, 0] += shift
        hr[k, 0] += shift
    # 軸の上の点の取っ手は、反対側の取っ手を鏡写しにしたものとそろえる
    hl[0] = _mirror(hr[0:1])[0]
    hr[n - 1] = _mirror(hl[n - 1:n])[0]
    # 反対側: 内側の点(端点を除く)を逆順に、取っ手は左右を入れ替えて鏡写し
    inner = list(range(n - 2, 0, -1))
    m_co = _mirror(co[inner])
    m_hl = _mirror(hr[inner])
    m_hr = _mirror(hl[inner])
    return (np.vstack([co, m_co]), np.vstack([hl, m_hl]), np.vstack([hr, m_hr]))


def snap_closed(co, hl, hr, tolerance):
    """描いた線の始点と終点が近ければ、終点を落として閉じた輪郭にする(取っ手は保つ)。"""
    co = np.array(co, dtype=np.float64).reshape(-1, 2)
    if len(co) >= 4 and np.linalg.norm(co[0] - co[-1]) <= tolerance:
        hl = np.array(hl, dtype=np.float64).reshape(-1, 2)
        hr = np.array(hr, dtype=np.float64).reshape(-1, 2)
        hl = hl.copy()
        hl[0] = hl[-1] - co[-1] + co[0]
        return co[:-1], hl[:-1], np.array(hr, dtype=np.float64).reshape(-1, 2)[:-1]
    return co, np.array(hl, dtype=np.float64).reshape(-1, 2), np.array(hr, dtype=np.float64).reshape(-1, 2)

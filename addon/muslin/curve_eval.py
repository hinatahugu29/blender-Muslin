"""Bezier の輪郭を弧長で評価する(bpy 非依存。Curve Pattern)。

輪郭は点ごとの (座標, 左ハンドル, 右ハンドル) の並び。点 i から次の点 j への区間は
三次ベジェ (co[i], hr[i], hl[j], co[j])。ハンドルが座標と同じなら直線になる。

離散化と Shape Update はどちらも「区間の弧長比 u(0〜1)から座標を出す」ことだけを使う。
ベジェの媒介変数ではなく弧長比にするのは、頂点を区間内で等間隔に置くため。
弧長は折れ線で近似する(区間あたり SAMPLES 分割)。決定的で、区間が変わっても同じ
やり方で測るので、更新の前後で頂点の位置が連続する。
"""

import numpy as np

SAMPLES = 96


def _bezier(p0, p1, p2, p3, t):
    t = np.asarray(t, dtype=np.float64)[:, None]
    mt = 1.0 - t
    return (mt ** 3) * p0 + 3.0 * (mt ** 2) * t * p1 + 3.0 * mt * (t ** 2) * p2 + (t ** 3) * p3


class Segment:
    """1 区間。弧長の表を持つ。"""

    def __init__(self, p0, p1, p2, p3):
        self.ctrl = tuple(np.asarray(p, dtype=np.float64) for p in (p0, p1, p2, p3))
        t = np.linspace(0.0, 1.0, SAMPLES + 1)
        pts = _bezier(*self.ctrl, t)
        step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        cumulative = np.concatenate([[0.0], np.cumsum(step)])
        self.length = float(cumulative[-1])
        # 弧長比 → 媒介変数(折れ線上で逆引き)
        if self.length > 1e-15:
            self._fraction = cumulative / self.length
        else:
            self._fraction = t.copy()
        self._t = t

    def point_at(self, u):
        """弧長比 u(配列可)の座標。u は 0〜1 に丸める。"""
        u = np.clip(np.atleast_1d(np.asarray(u, dtype=np.float64)), 0.0, 1.0)
        t = np.interp(u, self._fraction, self._t)
        return _bezier(*self.ctrl, t)


def segments(co, hl, hr, cyclic):
    """輪郭の区間の一覧。開いていれば n-1 本、閉じていれば n 本。

    区間 k は点 k から点 k+1(閉じていれば最後は点 0)まで。
    """
    co = np.asarray(co, dtype=np.float64)
    hl = np.asarray(hl, dtype=np.float64)
    hr = np.asarray(hr, dtype=np.float64)
    n = len(co)
    count = n if cyclic else n - 1
    return [Segment(co[k], hr[k], hl[(k + 1) % n], co[(k + 1) % n]) for k in range(count)]


def outline_length(segs):
    return float(sum(s.length for s in segs))

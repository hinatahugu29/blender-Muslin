"""マチ(帯状のピース)の計画と配置(bpy 非依存。M11)。

厚みのあるクッション(座布団・箱形)は、表と裏のあいだに帯を一周縫い付けて側面を作る。
帯は Curve に長方形のピースとして足し(長さ = 表の外周、幅 = 厚み)、縫い目は次の 5 本:

- 表の外周 ↔ 帯の上の縁(前半・後半の 2 本)
- 裏の外周 ↔ 帯の下の縁(前半・後半の 2 本)
- 帯の両端どうし(輪にする)

外周を一周する縫い目を 1 本にしないのは、閉じた頂点の列はどこから始まるかが決まらず、
開いた帯の縁と弧長で対応付けると始点がずれるため。外周を制御点で 2 つに分け、帯の縁にも
同じ位置に点を置けば、どの縫い目も両端の決まった開いた列になる(分け目が辺の途中に来ると、
その辺がどちらの縫い目にも入らず穴が開くので、分け目は必ず制御点にする)。
"""

import numpy as np

try:                                    # アドオンとして読み込まれたとき
    from . import curve_eval
except ImportError:                     # scripts/verify_core.py が単体で読み込むとき
    import curve_eval


class GussetError(Exception):
    pass


# 帯の点の並び(帯の輪郭。反時計回り)。下の縁(裏に縫う。y = 0)に x = 0 から外周の長さまで、
# 上の縁(表に縫う。y = 高さ)に x = 外周の長さから 0 まで。どちらの縁にも、表の輪郭の制御点
# (角)と同じ位置に点を置く。角に必ず頂点が来るので、帯が角で折れ目をジグザグにせず、
# 縫い目も角どうしで合う
#   2m+1 -- ... -- m+1      y = 高さ
#    |               |
#    0 ---- ... ---- m      y = 0      (m = 表の制御点の数)


def outline_arcs(co, hl, hr):
    """閉じた輪郭の区間と、制御点ごとの累積の弧長 (n + 1)。"""
    segs = curve_eval.segments(co, hl, hr, True)
    cum = np.concatenate([[0.0], np.cumsum([s.length for s in segs])])
    return segs, cum


def point_at_arc(segs, cum, s):
    """輪郭の始点からの弧長 s(配列可。周の長さで回り込む)の座標 (n, 2)。"""
    total = cum[-1]
    s = np.mod(np.atleast_1d(np.asarray(s, dtype=np.float64)), total)
    k = np.clip(np.searchsorted(cum, s, side='right') - 1, 0, len(segs) - 1)
    out = np.empty((len(s), 2))
    for i in range(len(segs)):
        mask = k == i
        if mask.any():
            u = (s[mask] - cum[i]) / segs[i].length
            out[mask] = segs[i].point_at(u)
    return out


def _samples(segs, per_segment=16):
    u = np.arange(per_segment) / per_segment
    return np.vstack([seg.point_at(u) for seg in segs])


def signed_area(segs):
    """輪郭の符号付き面積(反時計回りなら正)。"""
    p = _samples(segs)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


# 輪郭の始点でこれより大きく向きが変われば「角」とみなす(度)
CORNER_ANGLE = 30.0


def turn_at_start(segs):
    """輪郭の始点(制御点 0)での向きの変わり方(度)。滑らかなら 0、正方形の角なら 90。"""
    eps = 1e-3
    a = segs[-1].point_at([1.0 - eps, 1.0])
    b = segs[0].point_at([0.0, eps])
    d0, d1 = a[1] - a[0], b[1] - b[0]
    n0, n1 = np.linalg.norm(d0), np.linalg.norm(d1)
    if n0 <= 1e-15 or n1 <= 1e-15:
        return 0.0
    return float(np.degrees(np.arccos(np.clip(np.dot(d0, d1) / (n0 * n1), -1.0, 1.0))))


def centroid(segs):
    return _samples(segs).mean(axis=0)


def plan(top, bottom, height):
    """表と裏の輪郭から、帯の形と縫い目の割り当てを決める。

    top / bottom: {"uids", "co", "hl", "hr"}(型紙の座標)。height: 帯の幅(= クッションの厚み)
    戻り値: dict(
      length: 帯の長さ(= 表の外周), split_x: 帯の縁の分け目の x, height,
      points: 帯の点の座標(2 × (表の制御点の数 + 1) 点。原点 (0, 0) 基準。置き場所は呼び出し側が足す),
      seams: [(名前, 側 a の区間, 側 b の区間, 折り返し)](表・裏との縫い目は角になる縁なので折り返し。
             帯の両端どうしは、表の始点が角のときだけ折り返し)
             区間は (始点, t0, 終点, t1)。始点・終点は表と裏では点の uid、帯では ("g", 点の番号)
    )
    """
    if height <= 0.0:
        raise GussetError("マチの幅(厚み)は正の値にしてください")
    t_uids, b_uids = list(top["uids"]), list(bottom["uids"])
    if len(t_uids) < 2 or len(b_uids) < 2:
        raise GussetError("表と裏の輪郭には点が 2 つ以上必要です")
    t_segs, t_cum = outline_arcs(top["co"], top["hl"], top["hr"])
    b_segs, b_cum = outline_arcs(bottom["co"], bottom["hl"], bottom["hr"])
    length = float(t_cum[-1])
    if length <= 1e-9:
        raise GussetError("表の輪郭の長さが 0 です")

    # 表の分け目: 外周のちょうど半分に最も近い制御点(始点を除く)
    halves = np.abs(t_cum[1:-1] - length / 2.0)
    split = 1 + int(np.argmin(halves))
    s_split = float(t_cum[split])

    # 裏で対応する点: 中心をそろえて重ねたときに、表の始点・分け目に最も近い制御点
    shift = centroid(t_segs) - centroid(b_segs)
    b_co = np.asarray(bottom["co"], dtype=np.float64) + shift
    t_co = np.asarray(top["co"], dtype=np.float64)
    b_start = int(np.argmin(np.linalg.norm(b_co - t_co[0], axis=1)))
    b_split = int(np.argmin(np.linalg.norm(b_co - t_co[split], axis=1)))
    if b_start == b_split:
        raise GussetError("裏の輪郭で、表の始点と分け目に当たる点が同じになりました"
                          "(表と裏は同じくらいの形・点の数にしてください)")

    ccw = signed_area(t_segs) >= 0.0
    same_direction = (signed_area(b_segs) >= 0.0) == ccw
    # 帯の x は表の外周を反時計回りにたどる(帯の面が外を向くように)。時計回りに描いた表なら逆からたどる
    split_x = s_split if ccw else length - s_split
    m = len(t_uids)
    xs = t_cum[:m] if ccw else (length - t_cum[:m])[::-1] % length
    xs = np.append(np.sort(xs), length)                 # 0, 角..., 外周の長さ(m + 1 点)
    points = [(float(x), 0.0) for x in xs] + [(float(x), float(height)) for x in xs[::-1]]
    k_split = int(np.argmin(np.abs(xs - split_x)))      # 下の縁での分け目の番号
    top_end = 2 * m + 1                                 # 上の縁の x = 0
    k_split_top = m + 1 + (m - k_split)                 # 上の縁での分け目の番号

    u0, us = t_uids[0], t_uids[split]
    v0, vs = b_uids[b_start], b_uids[b_split]
    # 帯の前半(x = 0〜分け目)に当たる表の区間。輪郭の向き(弧長の増える向き)で書く
    top_first = (u0, us) if ccw else (us, u0)
    top_second = (us, u0) if ccw else (u0, us)
    to_bottom = {u0: v0, us: vs}

    def bottom_range(pair):
        a, b = to_bottom[pair[0]], to_bottom[pair[1]]
        return (a, 0.0, b, 0.0) if same_direction else (b, 0.0, a, 0.0)

    def g(i, j):
        return (("g", i), 0.0, ("g", j), 0.0)

    seams = [
        ("Gusset Top 1", (top_first[0], 0.0, top_first[1], 0.0), g(k_split_top, top_end), True),
        ("Gusset Top 2", (top_second[0], 0.0, top_second[1], 0.0), g(m + 1, k_split_top), True),
        ("Gusset Bottom 1", bottom_range(top_first), g(0, k_split), True),
        ("Gusset Bottom 2", bottom_range(top_second), g(k_split, m), True),
        # 帯の両端は表の始点で出会う。始点が角なら、そこで帯が折れるので折り返しにする
        # (縫い目をまたぐ曲げ制約は平らにつなげようとして、角を潰す)。滑らかなら平らにつなぐ
        ("Gusset End", g(top_end, 0), g(m, m + 1), turn_at_start(t_segs) > CORNER_ANGLE),
    ]
    return {"length": length, "split_x": split_x, "height": float(height),
            "points": points, "seams": seams, "ccw": ccw}


def wall_positions(gusset_xy, top, offset=0.0):
    """帯の頂点(型紙の座標)を、表の外周に沿って立てた側面の座標 (n, 3) にする。

    帯の x を表の外周の弧長に、y を面に垂直な方向に写す。帯の x の幅は表の今の外周に
    合わせて伸び縮みさせる(Curve を編集して長さが変わっても置ける)。
    第 3 成分は、帯の下の縁(y = 最小)が offset - 高さ/2、上の縁が offset + 高さ/2。
    戻り値: (座標 (n, 3), 高さ)
    """
    xy = np.asarray(gusset_xy, dtype=np.float64)
    t_segs, t_cum = outline_arcs(top["co"], top["hl"], top["hr"])
    length = float(t_cum[-1])
    x0, y0 = xy.min(axis=0)
    x1, y1 = xy.max(axis=0)
    width = max(x1 - x0, 1e-12)
    height = float(y1 - y0)
    s = (xy[:, 0] - x0) / width * length
    if signed_area(t_segs) < 0.0:
        s = length - s
    out = np.empty((len(xy), 3))
    out[:, :2] = point_at_arc(t_segs, t_cum, s)
    out[:, 2] = offset - height / 2.0 + (xy[:, 1] - y0)
    return out, height

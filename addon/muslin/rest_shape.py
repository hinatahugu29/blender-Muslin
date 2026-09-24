"""シミュレーション前のメッシュ形状(元の形)をファイルに残す。

Muslin は Blender 標準のクロスと違ってモディファイアではなく、計算結果を
`obj.data.vertices` に直接書く。つまり**元の形は上書きされて消える**。

以前は元の形を実行中のメモリ(state["rest_positions"])にしか持っていな
かったため、

- 停止してからフレーム 1 に戻しても、変形したままだった
- 停止して開始し直すと、**変形後の形が新しい「元の形」として取り込まれ**、
  元の形が永久に失われた
- .blend を保存して開き直しても戻せなかった

メッシュの属性として持たせれば .blend に保存され、どの順で操作しても
戻せる。頂点座標はローカル座標で持つ(オブジェクトを動かしても、元の形は
メッシュの形そのものなので変わらない)。
"""

import numpy as np

ATTRIBUTE = "muslin_rest"

# 「今のメッシュは Muslin が書いたもので、人が作った形ではない」という印。
# これが無いと、人がメッシュを編集してから開始したときに、その編集を
# 古い元の形で上書きしてしまう。
DEFORMED_FLAG = "muslin_deformed"


def mark_deformed(obj):
    """シミュレーション結果を書き込んだことを記録する。"""
    obj[DEFORMED_FLAG] = True


def is_deformed(obj):
    """今のメッシュが Muslin の出力か(人が作った形ではないか)。"""
    return bool(obj.get(DEFORMED_FLAG, False))


def _clear_deformed(obj):
    if DEFORMED_FLAG in obj:
        del obj[DEFORMED_FLAG]


def _attribute(mesh):
    attr = mesh.attributes.get(ATTRIBUTE)
    if attr is None:
        return None
    # 種類が違うものが同名で作られていたら、こちらのものではない
    if attr.domain != 'POINT' or attr.data_type != 'FLOAT_VECTOR':
        return None
    return attr


def has_rest(obj):
    return obj.type == 'MESH' and _attribute(obj.data) is not None


def store(obj, positions=None):
    """今のメッシュ形状(または渡された座標)を元の形として保存する。

    positions はローカル座標の平坦な配列。省略すると今の頂点をそのまま使う。
    """
    mesh = obj.data
    count = len(mesh.vertices)
    if count == 0:
        return False

    if positions is None:
        positions = np.empty(count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", positions)
    else:
        positions = np.asarray(positions, dtype=np.float32)
        if positions.size != count * 3:
            return False

    attr = _attribute(mesh)
    if attr is None:
        attr = mesh.attributes.new(ATTRIBUTE, 'FLOAT_VECTOR', 'POINT')
    attr.data.foreach_set("vector", positions)
    mesh.update()
    # 今の形を元の形として採ったので、変形済みの印は外す
    _clear_deformed(obj)
    return True


def load(obj):
    """保存した元の形をローカル座標の配列で返す。無ければ None。

    頂点数が合わない場合も None を返す。メッシュを編集して頂点数が
    変わった後に古い形を書き戻すと、まったく別の形になってしまう。
    """
    if obj.type != 'MESH':
        return None
    mesh = obj.data
    attr = _attribute(mesh)
    if attr is None:
        return None
    count = len(mesh.vertices)
    if len(attr.data) != count or count == 0:
        return None

    out = np.empty(count * 3, dtype=np.float32)
    attr.data.foreach_get("vector", out)
    return out


def restore(obj):
    """保存した元の形をメッシュに書き戻す。戻せたら True。"""
    positions = load(obj)
    if positions is None:
        return False
    obj.data.vertices.foreach_set("co", positions)
    obj.data.update()
    _clear_deformed(obj)
    return True


def sync_before_start(obj):
    """開始前に、元の形をどう扱うか決める。

    - Muslin が書いた形のまま(変形済みの印がある) → 元の形に戻して始める。
      止めて開始し直したときに、変形後の形が新しい元の形として取り込まれて
      しまうのを防ぐ。
    - 人が作った形(印が無い) → 今の形を元の形として保存し直す。
      メッシュを編集したあとに、その編集を古い形で上書きしないため。
    """
    if is_deformed(obj) and restore(obj):
        return "restored"
    store(obj)
    return "stored"


# ---------------------------------------------------------------------------
# 型紙(寸法の基準)
#
# 「元の形」は2つの役目を兼ねていた。フレーム 1 の姿勢と、布の寸法
# (伸びの基準の辺長・質量を出す面積・曲げの基準)である。着せた形を元の形に
# し直すと、収束しきらない伸び(0.3% ほど)がそのまま寸法に焼き込まれ、
# し直すたびに服が大きくなっていった(6回で 1.8%。ROADMAP M7)。
#
# そこで寸法の基準を「型紙」として別の属性に持つ。
#
# - 型紙を作っている段階(着せた印が無い): 人の編集は型紙の変更。
#   開始のたびに今の形を型紙として取り直す(以前と同じ振る舞い)
# - 着せた段階(`Set Current as Rest` で姿勢を保存した後): 型紙は固定。
#   人の編集は姿勢の微調整として扱い、寸法は変えない
# ---------------------------------------------------------------------------

PATTERN_ATTRIBUTE = "muslin_pattern"

# 「着せた姿勢を元の形として保存した」という印。これがある間は型紙を取り直さない。
DRESSED_FLAG = "muslin_dressed"


def is_dressed(obj):
    return bool(obj.get(DRESSED_FLAG, False))


def mark_dressed(obj):
    obj[DRESSED_FLAG] = True


def clear_dressed(obj):
    if DRESSED_FLAG in obj:
        del obj[DRESSED_FLAG]


def _pattern_attribute(mesh):
    attr = mesh.attributes.get(PATTERN_ATTRIBUTE)
    if attr is None or attr.domain != 'POINT' or attr.data_type != 'FLOAT_VECTOR':
        return None
    return attr


def has_pattern(obj):
    return obj.type == 'MESH' and _pattern_attribute(obj.data) is not None


def store_pattern(obj, positions=None):
    """今のメッシュ形状(または渡されたローカル座標)を型紙として保存する。"""
    mesh = obj.data
    count = len(mesh.vertices)
    if count == 0:
        return False
    if positions is None:
        positions = np.empty(count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", positions)
    else:
        positions = np.asarray(positions, dtype=np.float32)
        if positions.size != count * 3:
            return False
    attr = _pattern_attribute(mesh)
    if attr is None:
        attr = mesh.attributes.new(PATTERN_ATTRIBUTE, 'FLOAT_VECTOR', 'POINT')
    attr.data.foreach_set("vector", positions)
    mesh.update()
    return True


def load_pattern(obj):
    """型紙をローカル座標の平坦な配列で返す。無ければ None。"""
    if obj.type != 'MESH':
        return None
    mesh = obj.data
    attr = _pattern_attribute(mesh)
    count = len(mesh.vertices)
    if attr is None or count == 0 or len(attr.data) != count:
        return None
    out = np.empty(count * 3, dtype=np.float32)
    attr.data.foreach_get("vector", out)
    return out


def clear_pattern(obj):
    if obj.type != 'MESH':
        return False
    attr = _pattern_attribute(obj.data)
    if attr is None:
        return False
    obj.data.attributes.remove(attr)
    return True


# 着せた姿勢と型紙の辺長の比がこの範囲を外れる辺があれば、型紙は今の
# メッシュに対応していないとみなす。布は数 % しか伸びないので、半分や
# 倍になっている辺は「頂点を足した(属性が 0 で埋まった)」などで壊れている。
PATTERN_RATIO_RANGE = (0.5, 2.0)


def pattern_mismatch(pattern, current, edges):
    """型紙が今のメッシュに対応しているかを調べる。bpy を使わない。

    pattern / current は平坦な座標配列、edges は (i, j) の列。
    対応していなければ理由の文字列、していれば None を返す。
    """
    p = np.asarray(pattern, dtype=np.float64).reshape(-1, 3)
    c = np.asarray(current, dtype=np.float64).reshape(-1, 3)
    if p.shape != c.shape:
        return f"頂点数が違います(型紙 {len(p)} / 今 {len(c)})"
    if len(edges) == 0:
        return None
    e = np.asarray(edges, dtype=np.int64)
    lp = np.linalg.norm(p[e[:, 0]] - p[e[:, 1]], axis=1)
    lc = np.linalg.norm(c[e[:, 0]] - c[e[:, 1]], axis=1)
    lo, hi = PATTERN_RATIO_RANGE
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = lc / lp
    bad = ~np.isfinite(ratio) | (ratio < lo) | (ratio > hi)
    if bad.any():
        return f"{int(bad.sum())} 本の辺で長さが型紙と大きく食い違っています"
    return None


# 「型紙を確定した」という印(Lock Pattern)。着せる前、ピースを胴のまわりに
# 曲げて配置するときに要る。これが無いと、曲げてから初めて開始したときに
# 曲げた形が型紙として取り込まれる。着せた印(DRESSED_FLAG)も型紙の固定を
# 意味するが、あちらは「縫い目が閉じている」ことも表すので別に持つ。
LOCKED_FLAG = "muslin_pattern_locked"


def is_pattern_locked(obj):
    """型紙が固定されているか(確定したか、着せたか)。"""
    return bool(obj.get(LOCKED_FLAG, False)) or is_dressed(obj)


def lock_pattern(obj):
    """今の形を型紙として記録し、以後の開始で取り直さないようにする。"""
    if not store_pattern(obj):
        return False
    store(obj)
    obj[LOCKED_FLAG] = True
    return True


def unlock_pattern(obj):
    """型紙の固定を外し、型紙を作る段階に戻す(形はそのまま)。

    次に開始したときに今の形が型紙になる。着せた印も外す。
    """
    if LOCKED_FLAG in obj:
        del obj[LOCKED_FLAG]
    clear_dressed(obj)


# 平らとみなす厚み。型紙のピースの頂点が、最も薄い方向にこれ以上広がって
# いたら「曲がっている」と判断する。1mm 未満の揺らぎは平面の型紙の誤差。
FLAT_TOLERANCE = 0.001


def bent_islands(positions, edges):
    """平らでない(曲げて置かれた)ピースの数を返す。bpy を使わない。

    辺でつながった頂点のまとまりをピースとみなし、主成分分析で一番薄い
    方向の広がりを見る。筒状の型紙のようにわざと平らでないものもあるので、
    呼び出し側は止めずに警告にとどめる。
    """
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = len(p)
    parent = list(range(n))

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
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    bent = 0
    for members in groups.values():
        if len(members) < 4:
            continue
        pts = p[members]
        centered = pts - pts.mean(axis=0)
        thinnest = np.linalg.svd(centered, compute_uv=False)[-1] / np.sqrt(len(members))
        if thinnest > FLAT_TOLERANCE:
            bent += 1
    return bent


def outline_edges(edge_count, loop_edges):
    """型紙の外周(面を1つしか持たない辺)の辺番号を返す。bpy を使わない。

    loop_edges は面の角ごとの辺番号(`mesh.loops` の `edge_index`)。
    辺が何個の面に使われているかを数え、1つだけのものが外周。
    面に使われていない辺(ぶら下がった辺)は外周に含めない。
    """
    counts = np.bincount(np.asarray(loop_edges, dtype=np.int64), minlength=edge_count)
    return np.nonzero(counts[:edge_count] == 1)[0]


def sync_pattern_before_start(obj):
    """開始前に型紙をどう扱うか決める(`sync_before_start` の後に呼ぶ)。

    型紙が固定されていなければ、今の形(= 開始時の形)を型紙として取り直す。
    固定されていれば(Lock Pattern した、または着せた)取り直さない。
    """
    if not is_pattern_locked(obj) or not has_pattern(obj):
        store_pattern(obj)
        return "stored"
    return "kept"


def restore_pattern(obj):
    """型紙の形をメッシュに書き戻し、型紙を作る段階に戻す。戻せたら True。"""
    positions = load_pattern(obj)
    if positions is None:
        return False
    obj.data.vertices.foreach_set("co", positions)
    obj.data.update()
    store(obj)          # 開始時の姿勢も型紙の形にする
    unlock_pattern(obj) # 型紙を作る段階に戻る
    return True


def clear(obj):
    """保存した元の形を捨てる。"""
    if obj.type != 'MESH':
        return False
    attr = _attribute(obj.data)
    if attr is None:
        return False
    obj.data.attributes.remove(attr)
    return True

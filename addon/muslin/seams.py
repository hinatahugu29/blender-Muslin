"""縫い目(シーム)のデータ処理。

このモジュールの中核関数は **bpy に依存しない**純粋な関数として書いてある。
Blender を起動せずに `scripts/verify_core.py` から検証できるようにするため。

用語:
- チェーン: 一続きに繋がった頂点列(パターンピースの縫い代となるエッジ列)
- ペアリング: 2本のチェーンを弧長(arc length)で正規化して対応付けること。
  頂点数が違うチェーン同士でも縫い合わせられる。
"""


def split_edges_into_chains(edges):
    """エッジ集合を連結成分ごとに分け、それぞれ頂点列として順序付けて返す。

    - 端点(次数1)がある場合はそこから辿る。
    - 閉ループの場合は任意の頂点から辿り、始点に戻ったところで終わる。
    - 分岐(次数3以上)がある場合はその成分を諦めて None を含めず返す。

    戻り値: 頂点インデックスのリストのリスト(長さ2以上のもののみ)
    """
    adjacency = {}
    for a, b in edges:
        if a == b:
            continue
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    if not adjacency:
        return []

    visited = set()
    chains = []

    for start_vertex in sorted(adjacency):
        if start_vertex in visited:
            continue

        # この連結成分の頂点を集める
        component = set()
        stack = [start_vertex]
        while stack:
            v = stack.pop()
            if v in component:
                continue
            component.add(v)
            stack.extend(adjacency[v] - component)
        visited |= component

        # 分岐があるチェーンは扱えない(縫い代は一本道であるべき)
        if any(len(adjacency[v]) > 2 for v in component):
            continue

        endpoints = sorted(v for v in component if len(adjacency[v]) == 1)
        start = endpoints[0] if endpoints else min(component)

        chain = [start]
        previous = None
        current = start
        while True:
            candidates = [v for v in adjacency[current] if v != previous]
            if not candidates:
                break
            nxt = min(candidates)
            if nxt == start:  # 閉ループを一周した
                break
            chain.append(nxt)
            previous, current = current, nxt
            if len(chain) > len(component):  # 保険(通常起こらない)
                break

        if len(chain) >= 2:
            chains.append(chain)

    return chains


def _point(positions, index):
    base = index * 3
    return positions[base], positions[base + 1], positions[base + 2]


def _distance(positions, i, j):
    ax, ay, az = _point(positions, i)
    bx, by, bz = _point(positions, j)
    return ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5


def chain_parameters(chain, positions):
    """チェーン上の各頂点を弧長で 0.0〜1.0 に正規化したパラメータ列を返す。"""
    if len(chain) < 2:
        return [0.0] * len(chain)

    cumulative = [0.0]
    total = 0.0
    for k in range(1, len(chain)):
        total += _distance(positions, chain[k - 1], chain[k])
        cumulative.append(total)

    if total <= 1e-12:
        # 全頂点が同一位置(退化)。等間隔とみなす
        n = len(chain) - 1
        return [k / n for k in range(len(chain))]

    return [c / total for c in cumulative]


def _nearest_by_parameter(target, params):
    """params(昇順)の中で target に最も近い要素のインデックスを返す。"""
    best_index = 0
    best_diff = abs(params[0] - target)
    for i in range(1, len(params)):
        diff = abs(params[i] - target)
        if diff < best_diff:
            best_diff = diff
            best_index = i
    return best_index


def pair_chains(chain_a, chain_b, positions, flipped=False):
    """2本のチェーンを弧長で対応付け、縫い合わせる頂点ペアを返す。

    頂点数が異なっていてもよい。密な側の全頂点が必ずどこかに縫い付けられるよう、
    両方向から対応付けて重複を除く。
    """
    if len(chain_a) < 2 or len(chain_b) < 2:
        return []

    b_ordered = list(reversed(chain_b)) if flipped else list(chain_b)

    params_a = chain_parameters(chain_a, positions)
    params_b = chain_parameters(b_ordered, positions)

    pairs = []
    seen = set()

    def add(ia, ib):
        if ia == ib:
            return
        key = (ia, ib) if ia < ib else (ib, ia)
        if key in seen:
            return
        seen.add(key)
        pairs.append((ia, ib))

    for i, t in enumerate(params_a):
        add(chain_a[i], b_ordered[_nearest_by_parameter(t, params_b)])
    for j, t in enumerate(params_b):
        add(chain_a[_nearest_by_parameter(t, params_a)], b_ordered[j])

    return pairs


def should_flip(chain_a, chain_b, positions):
    """2本のチェーンの向きが逆かどうかを、両向きの総距離を比べて判定する。"""
    forward = pair_chains(chain_a, chain_b, positions, flipped=False)
    backward = pair_chains(chain_a, chain_b, positions, flipped=True)

    def total(pairs):
        return sum(_distance(positions, a, b) for a, b in pairs)

    if not forward:
        return False
    return total(backward) < total(forward)


def seam_length(chain, positions):
    """チェーンの弧長(縫い目の長さ)。"""
    return sum(
        _distance(positions, chain[k - 1], chain[k]) for k in range(1, len(chain))
    )


# ---------------------------------------------------------------------------
# 辺の属性による縫い目の保存(M7)
#
# 縫い目を頂点番号で持つと、ピースの統合・細分化・頂点の削除で番号が
# ずれて壊れる。とくに `Join Pattern Pieces` は既存の縫い目を消すしかなく、
# 「統合してから縫い目を定義する」順番が強制されていた。
#
# 辺の整数属性 `muslin_seam` に「どの縫い目の、どちら側か」を書いておけば、
# Blender が統合・細分化・削除に合わせて辺と一緒に運ぶ。頂点の列は使う
# たびに属性から作り直す。値は `uid * 2 + side + 1`(0 は縫い目なし)。
# ---------------------------------------------------------------------------

SEAM_ATTRIBUTE = "muslin_seam"


def seam_code(uid, side):
    """縫い目 uid の side(0 = A, 1 = B)を表す属性値。"""
    return uid * 2 + side + 1


def chains_from_codes(codes, edges, uid, side):
    """属性値の列から、縫い目 uid の side にあたる頂点の列を作る。

    codes は辺ごとの属性値、edges は辺ごとの (頂点, 頂点)。
    縫い目の片側はちょうど1本の頂点列のはずなので、呼び出し側は
    戻り値が1本でなければ壊れているとみなす。
    """
    code = seam_code(uid, side)
    selected = [tuple(e) for c, e in zip(codes, edges) if c == code]
    return split_edges_into_chains(selected)

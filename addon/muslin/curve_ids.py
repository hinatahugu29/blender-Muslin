"""Curve の点の同一性(bpy 非依存。Curve Pattern の一次データの土台)。

Blender の Curve には点の恒久 ID が無い(index は削除・分割・cyclic の辺の削除で動く)。
点ごとの float は編集操作を通しても点に付いて回り、新しい点は複製か補間として生まれる。
そこで `radius` に uid、`weight_softbody` に uid の検算値を書き、読み取り時に

- 検算が合う  → 既知の点(uid を復元できる)
- 検算が合わない → 新しい点(補間か手描き)

と見分ける。検算値は uid の非線形ハッシュにする。線形だと補間しても一致してしまう
(補間で生まれた点の約 25% が正規の点に見えた。docs/design/curve_spike_results.md)。

このモジュールは値の読み書きの規則と、記録した構造と今の構造の比較だけを持つ。
Blender の Curve から値を出し入れする側(bpy を使う層)は別に書く。
"""

import numpy as np

# radius は float32 の整数を 2^24 まで正確に持てる
MAX_UID = 1 << 24

# weight_softbody は 0.01〜100 に丸められる
CHECK_MIN = 0.01
CHECK_SPAN = 99.98

# 検算値の許容誤差。float32 の精度(100 付近で 8e-6)より少し大きい
CHECK_TOLERANCE = 1e-4
UID_TOLERANCE = 1e-4


def _mix(uid):
    """整数 → 0..1 の擬似乱数(非線形)。"""
    x = (int(uid) * 0x9E3779B1) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & 0xFFFFFFFF
    x ^= x >> 16
    return x / 2 ** 32


def check_value(uid):
    """uid の検算値(`weight_softbody` に書く値)。"""
    return CHECK_MIN + CHECK_SPAN * _mix(uid)


def encode(uid):
    """uid → (radius, weight_softbody)。"""
    if not 1 <= int(uid) < MAX_UID:
        raise ValueError(f"uid は 1〜{MAX_UID - 1} の整数です: {uid}")
    return float(uid), float(np.float32(check_value(uid)))


def decode(radius, weight):
    """(radius, weight_softbody) → uid。既知の点でなければ None。"""
    uid = round(radius)
    if uid < 1 or uid >= MAX_UID or abs(radius - uid) > UID_TOLERANCE:
        return None
    if abs(weight - float(np.float32(check_value(uid)))) > CHECK_TOLERANCE:
        return None
    return int(uid)


def decode_all(radii, weights):
    """点の並びの (radius, weight) → uid の並び(既知でない点は None)。"""
    return [decode(r, w) for r, w in zip(radii, weights)]


# ---------------------------------------------------------------------------
# 記録した構造と、今の構造の比較
# ---------------------------------------------------------------------------

SAME = "same"
NEW_POINTS = "new_points"          # 検算が合わない点がある(細分化・手描き)
DUPLICATED = "duplicated"          # 同じ uid が 2 点以上(extrude・split・duplicate)
REMOVED = "removed"                # 記録にあった点が無い
REORDERED = "reordered"            # 点は同じだが並びが入れ替わっている
CYCLIC_CHANGED = "cyclic_changed"  # 開閉が変わった


def diff_outline(recorded, current, recorded_cyclic, current_cyclic):
    """1 本の輪郭(spline)について、記録した uid の並びと今の並びを比べる。

    recorded: 記録した uid の並び / current: 今の点の uid の並び(未知の点は None)
    戻り値: dict
      status     上の定数のどれか(いくつも当てはまるときは深刻な順に 1 つ)
      reasons    人が読める理由の一覧(SAME なら空)
      inserted   [(今の index, 手前の既知 uid, 後ろの既知 uid)] 新しい点の位置。
                 前後が既知なら「その区間が分割された」と分かる
      removed    無くなった uid
      duplicated 重複している uid
      reversed   向きが逆になっているか(SAME のときだけ意味を持つ)
      shift      cyclic の始点が回った量(SAME のときだけ意味を持つ)
    """
    out = {"status": SAME, "reasons": [], "inserted": [], "removed": [],
           "duplicated": [], "reversed": False, "shift": 0}
    reasons = out["reasons"]

    if recorded_cyclic != current_cyclic:
        out["status"] = CYCLIC_CHANGED
        reasons.append("輪郭の開閉が変わりました")
        return out

    known = [u for u in current if u is not None]
    counts = {}
    for u in known:
        counts[u] = counts.get(u, 0) + 1
    duplicated = sorted(u for u, c in counts.items() if c > 1)
    removed = sorted(set(recorded) - set(known))
    unknown = [i for i, u in enumerate(current) if u is None]

    if unknown:
        n = len(current)
        for i in unknown:
            before = _neighbour(current, i, -1, current_cyclic)
            after = _neighbour(current, i, +1, current_cyclic)
            out["inserted"].append((i, before, after))
        reasons.append(f"新しい点が {len(unknown)} 個あります(細分化・手描き)")
        out["status"] = NEW_POINTS
    if duplicated:
        out["duplicated"] = duplicated
        reasons.append(f"同じ点が複製されています(uid {', '.join(map(str, duplicated[:3]))})")
        out["status"] = DUPLICATED
    if removed:
        out["removed"] = removed
        reasons.append(f"点が {len(removed)} 個無くなりました")
        out["status"] = REMOVED
    if out["status"] != SAME:
        return out

    # ここまで来れば、記録した点と今の点は同じ集合。並びだけを見る
    n = len(recorded)
    if list(current) == list(recorded):
        return out
    if current_cyclic:
        for shift in range(n):
            rotated = [recorded[(i + shift) % n] for i in range(n)]
            if rotated == list(current):
                out["shift"] = shift
                return out
            if rotated[::-1] == list(current):
                out["shift"] = shift
                out["reversed"] = True
                return out
    else:
        if recorded[::-1] == list(current):
            out["reversed"] = True
            return out
    out["status"] = REORDERED
    reasons.append("点の並びが入れ替わっています")
    return out


def _neighbour(seq, i, step, cyclic):
    """i から step 方向にある最初の既知の点の uid。無ければ None。"""
    n = len(seq)
    j = i
    for _ in range(n):
        j += step
        if cyclic:
            j %= n
        elif not 0 <= j < n:
            return None
        if seq[j] is not None:
            return seq[j]
    return None


def match_pieces(recorded, current):
    """記録したピースと、今の輪郭(spline)を uid の重なりで対応付ける。

    spline には ID を持たせられないので、ピースの同一性は所属する点の uid から導く。

    recorded: {piece_uid: [uid, ...]} / current: [[uid or None, ...], ...](spline ごと)
    戻り値: (対応 {今の spline index: piece_uid}, 新しい spline の index の一覧,
             無くなった piece_uid の一覧)
    重なりの最も大きいピースを選ぶ。同点なら piece_uid の小さいほう。
    """
    taken = {}
    new_splines = []
    for si, uids in enumerate(current):
        known = {u for u in uids if u is not None}
        best, best_overlap = None, 0
        for piece_uid in sorted(recorded):
            if piece_uid in taken.values():
                continue
            overlap = len(known & set(recorded[piece_uid]))
            if overlap > best_overlap:
                best, best_overlap = piece_uid, overlap
        if best is None:
            new_splines.append(si)
        else:
            taken[si] = best
    gone = sorted(set(recorded) - set(taken.values()))
    return taken, new_splines, gone


def selection_runs(selected, cyclic):
    """選ばれた点の連なり(点の index の並びのリスト)を、輪郭に沿った順で返す。

    cyclic なら末尾と先頭をつなぐ(始点をまたぐ連なりは 1 本になる)。全部が選ばれていれば
    一周分の 1 本。縫い目の片側を「選んだ連なり」から作るのに使う(bpy 非依存)。
    """
    n = len(selected)
    if n == 0:
        return []
    if all(selected):
        return [list(range(n))]
    runs = []
    for i in range(n):
        if not selected[i]:
            continue
        prev = (i - 1) % n if cyclic else i - 1
        if (cyclic or i > 0) and selected[prev]:
            continue
        run = [i]
        j = i
        while True:
            j = (j + 1) % n if cyclic else j + 1
            if j >= n or not selected[j] or j == i:
                break
            run.append(j)
        runs.append(run)
    return runs

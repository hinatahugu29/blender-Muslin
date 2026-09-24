"""型紙オブジェクト: 型紙を別のオブジェクトとして持ち、編集を走っている布へ流す(M8)。

Marvelous Designer の「2D で型紙を直すと 3D の服が追従する」を、トポロジが
変わらない範囲で行う。布(シミュレーションするメッシュ)とは別に、頂点が
1対1で対応する平らなメッシュを脇に置き、それを型紙として扱う。

- 型紙オブジェクトのメッシュのローカル座標が、布のローカル座標での型紙になる。
  オブジェクトの位置と回転は置き場所にすぎない(寸法は剛体の動きで変わらない)。
  **スケールだけは型紙に効く**(オブジェクトモードで S を押して拡大すれば服も大きくなる)
- 編集モード中は編集用の BMesh から読むので、頂点を動かしている最中から反映される
- 反映はポーリング: 再生のフレームごと、Dress / Adjust のタイマーごとに
  指紋を比べ、変わっていればコアの `set_reference` を呼ぶ

頂点数が布と違えば(型紙オブジェクトで頂点を足したなど)反映せず、理由を返す。
"""

import bmesh
import bpy
import numpy as np

from . import rest_shape

# 型紙オブジェクトに付ける印(どの布の型紙か。表示用)
PATTERN_OF = "muslin_pattern_of"


def linked_object(obj):
    """布 obj に結び付いた型紙オブジェクト。無ければ None。"""
    props = getattr(obj, "muslin", None)
    target = getattr(props, "pattern_object", None) if props is not None else None
    if target is None or target.type != 'MESH' or target is obj:
        return None
    return target


def _local_coords(pattern_obj):
    """型紙オブジェクトのローカル座標(平坦な float64)。編集モード中は BMesh から読む。"""
    if pattern_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(pattern_obj.data)
        return np.array([c for v in bm.verts for c in v.co], dtype=np.float64)
    mesh = pattern_obj.data
    out = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", out)
    return out.astype(np.float64)


def read(obj):
    """布 obj の型紙を、布のローカル座標の平坦な配列で返す。

    戻り値: (座標 or None, 理由 or None)。型紙オブジェクトが無ければ (None, None)。
    """
    pattern_obj = linked_object(obj)
    if pattern_obj is None:
        return None, None
    local = _local_coords(pattern_obj)
    count = len(obj.data.vertices)
    if local.size != count * 3:
        return None, (f"型紙オブジェクト '{pattern_obj.name}' の頂点数 "
                      f"({local.size // 3}) が布 ({count}) と違うため反映していません")
    scale = np.array(pattern_obj.matrix_world.to_scale(), dtype=np.float64)
    return (local.reshape(-1, 3) * scale).ravel(), None


def signature(obj):
    """型紙の指紋。変わったかどうかを安く見るため。"""
    local, reason = read(obj)
    if local is None:
        return reason
    return hash(np.round(local, 7).tobytes())


def sync_before_start(obj):
    """開始前に、型紙オブジェクトがあればそれを布の型紙として記録する。

    記録したら True。無い(または頂点数が違う)なら False を返し、呼び出し側は
    従来の `rest_shape.sync_pattern_before_start` に任せる。
    """
    local, _reason = read(obj)
    if local is None:
        return False
    rest_shape.store_pattern(obj, local.astype(np.float32))
    return True


def create(obj, context):
    """布 obj の型紙オブジェクトを作って結び付け、そのオブジェクトを返す。

    型紙がまだ無ければ今の形を型紙にする。作った後は型紙を確定扱いにする
    (以後は型紙オブジェクトが寸法を決めるので、布の形から取り直さない)。
    """
    pattern = rest_shape.load_pattern(obj)
    if pattern is None:
        if not rest_shape.store_pattern(obj):
            return None
        pattern = rest_shape.load_pattern(obj)

    mesh = obj.data.copy()
    mesh.name = f"{obj.data.name}_Pattern"
    # 布の側の記録(元の形・型紙)は型紙オブジェクトには要らない
    for name in (rest_shape.ATTRIBUTE, rest_shape.PATTERN_ATTRIBUTE):
        attr = mesh.attributes.get(name)
        if attr is not None:
            mesh.attributes.remove(attr)
    mesh.vertices.foreach_set("co", pattern)
    mesh.update()

    pattern_obj = bpy.data.objects.new(f"{obj.name}_Pattern", mesh)
    for collection in obj.users_collection:
        collection.objects.link(pattern_obj)
        break
    else:
        context.collection.objects.link(pattern_obj)

    # 布の横、布の幅の 1.5 倍だけ離して置く(重ならないように)
    pts = pattern.reshape(-1, 3)
    width = float(np.ptp(pts[:, 0])) if len(pts) else 1.0
    pattern_obj.location = obj.matrix_world.translation.copy()
    pattern_obj.location.x += max(width, 0.1) * 1.5
    pattern_obj.rotation_euler = obj.matrix_world.to_euler()
    pattern_obj[PATTERN_OF] = obj.name
    pattern_obj.display_type = 'WIRE'
    pattern_obj.show_in_front = True

    obj.muslin.pattern_object = pattern_obj
    # 型紙の確定と同じ扱いにする。ただし Lock Pattern と違って今の形を
    # 型紙に取り直さない(曲げて置いた後に作っても型紙は平らなまま)
    if not rest_shape.has_rest(obj):
        rest_shape.store(obj)
    obj[rest_shape.LOCKED_FLAG] = True
    return pattern_obj


def references(state, members):
    """状態の全メンバーの型紙(ワールド座標の平坦な配列)と、変わったかどうか。

    型紙オブジェクトの無い布は、組み立て時の型紙のまま。
    戻り値: (reference, changed, reasons)
    """
    offsets = state["members"]
    reference = state.get("reference")
    if reference is None:
        return None, False, []
    reference = np.asarray(reference, dtype=np.float64).copy()
    signatures = state.setdefault("pattern_signatures", {})
    changed = False
    reasons = []
    for m, (key, start, count) in zip(members, offsets):
        sig = signature(m)
        if sig is None:
            continue
        if isinstance(sig, str):
            reasons.append(sig)
            continue
        if signatures.get(key) == sig:
            continue
        local, _ = read(m)
        world = _to_world(local, m.matrix_world)
        reference[start * 3:(start + count) * 3] = world
        signatures[key] = sig
        changed = True
    return reference, changed, reasons


def _to_world(local, matrix):
    from .transform import transform
    return transform(local, matrix).ravel()

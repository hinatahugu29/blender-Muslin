"""着せたときの体(コライダー)のポーズを覚えておき、再生を始めるときに比べる(M7)。

着せた形は、着せたときの体のポーズに合っている。アニメーションの先頭で体が別のポーズに
なっていると、最初のフレームで布が体に食い込む(あるいは浮く)。黙って始めると原因が
分からないので、ずれていれば開始時に警告する。

体の頂点を間引いてワールド座標で布のカスタムプロパティに持つ(数百点。.blend に残る)。
頂点数が変わったコライダー(モディファイアの変更など)は比べようがないので黙って飛ばす。
"""

import json

import numpy as np

from . import mesh_io

POSE_KEY = "muslin_dress_pose"
# 1 体あたり記録する頂点の数の上限
SAMPLES = 256
# これより大きくずれていれば警告する(衝突の厚みの 2 倍が小さすぎるときの下限)
MIN_WARN_DISTANCE = 0.01


def _samples(collider):
    positions, _tris = mesh_io.build_collider_mesh(collider, positions_only=True)
    pts = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    step = max(1, len(pts) // SAMPLES)
    return len(pts), pts[::step][:SAMPLES]


def record(obj, frame):
    """着せた今の体のポーズを布 obj に記録する(衝突が無効なら消す)。"""
    props = obj.muslin
    colliders = mesh_io.collect_collider_objects(props) if props.collision_enabled else []
    if not colliders:
        obj.pop(POSE_KEY, None)
        return
    data = {"frame": int(frame), "colliders": {}}
    for c in colliders:
        count, pts = _samples(c)
        data["colliders"][c.name] = {"count": count, "co": np.round(pts, 6).ravel().tolist()}
    obj[POSE_KEY] = json.dumps(data, separators=(",", ":"))


def mismatch(obj):
    """着せたときと今の体のずれ。戻り値: (最大のずれ m, 体の名前, 着せたフレーム) または None。"""
    raw = obj.get(POSE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    worst = None
    for c in mesh_io.collect_collider_objects(obj.muslin):
        rec = data.get("colliders", {}).get(c.name)
        if rec is None:
            continue
        count, pts = _samples(c)
        then = np.asarray(rec["co"], dtype=np.float64).reshape(-1, 3)
        if count != rec["count"] or len(pts) != len(then):
            continue
        d = float(np.linalg.norm(pts - then, axis=1).max())
        if worst is None or d > worst[0]:
            worst = (d, c.name, data.get("frame"))
    return worst


def warnings(obj):
    """開始時の警告(着せた布で、体のポーズが着せたときから大きくずれているとき)。"""
    from . import rest_shape
    if not rest_shape.is_dressed(obj) or not obj.muslin.collision_enabled:
        return []
    found = mismatch(obj)
    if found is None:
        return []
    distance, name, frame = found
    if distance <= max(2.0 * obj.muslin.collision_thickness, MIN_WARN_DISTANCE):
        return []
    return [f"体 '{name}' のポーズが、'{obj.name}' を着せたとき(フレーム {frame})と違います"
            f"(最大 {distance * 100:.1f}cm)。最初のフレームで布が体に食い込みます。"
            "着せたときのフレームから再生するか、今のポーズで着せ直してください(Dress)"]

"""Blenderメッシュ <-> cloth_core.ClothSim の橋渡し。

パフォーマンス方針: 頂点座標の読み書きは `foreach_get` / `foreach_set` で
一括転送し、行列変換は [transform](transform.py) の numpy 実装に任せる。

`co` は Blender 内部で単精度なので、foreach_get/set には float32 配列を渡す
(dtype が合っていないと高速パスに乗らない)。
"""

import bmesh
import bpy
import numpy as np

from . import cloth_core
from . import seams
from .transform import transform as _transform


# ---------------------------------------------------------------- トポロジ抽出

def _collect_topology(mesh):
    """メッシュから (edges, bending_quads, triangles) を取り出す。

    bending_quads は曲げ制約の4頂点で、`(エッジの端点2つ, 両側の対角頂点2つ)`。
    2つの面に共有されるエッジだけが対象(境界のエッジは曲げようがない)。
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    edges = [(e.verts[0].index, e.verts[1].index) for e in bm.edges]

    bending_quads = []
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        a, b = edge.verts[0].index, edge.verts[1].index
        edge_verts = {a, b}
        opposite = []
        for face in edge.link_faces:
            for v in face.verts:
                if v.index not in edge_verts:
                    opposite.append(v.index)
                    break
        if len(opposite) == 2 and opposite[0] != opposite[1]:
            bending_quads.append((a, b, opposite[0], opposite[1]))

    triangles = []
    for face in bm.faces:
        idx = [v.index for v in face.verts]
        # 扇状分割。凸でない n-gon でも質量算出用途では十分な近似
        for i in range(1, len(idx) - 1):
            triangles.append((idx[0], idx[i], idx[i + 1]))

    bm.free()
    return edges, bending_quads, triangles


def get_world_positions(obj):
    """ワールド座標のフラット配列 [x0,y0,z0, ...] を返す。"""
    mesh = obj.data
    count = len(mesh.vertices)
    local = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", local)

    return _transform(local, obj.matrix_world).ravel().tolist()


def write_positions_to_mesh(obj, flat_world_positions):
    """ワールド座標のフラット配列をメッシュのローカル座標に書き戻す。"""
    mesh = obj.data
    count = len(mesh.vertices)
    if count * 3 != len(flat_world_positions):
        return False

    local = _transform(flat_world_positions, obj.matrix_world.inverted())
    mesh.vertices.foreach_set("co", local.ravel().astype(np.float32))
    mesh.update()
    return True


# ------------------------------------------------------------ 頂点グループ関連

def find_vertex_group_indices(obj, group_name):
    """指定した頂点グループに weight > 0 で属する頂点インデックス一覧。"""
    if not group_name or group_name not in obj.vertex_groups:
        return []
    group_index = obj.vertex_groups[group_name].index
    result = []
    for v in obj.data.vertices:
        for g in v.groups:
            if g.group == group_index and g.weight > 0.0:
                result.append(v.index)
                break
    return result


# ------------------------------------------------------------------ シーム

def seam_chains(seam):
    """シームの2本のチェーンを頂点インデックスのリストで返す。"""
    return (
        [v.index for v in seam.chain_a],
        [v.index for v in seam.chain_b],
    )


def build_seam_pairs(obj, positions=None):
    """オブジェクトに登録された全シームから、縫い合わせる頂点ペアを作る。

    各シームは2本の頂点チェーンを弧長で対応付ける(頂点数が違ってもよい)。
    """
    if not getattr(obj, "cloth_md_seams", None):
        return []

    if positions is None:
        positions = get_world_positions(obj)

    vertex_count = len(obj.data.vertices)
    pairs = []
    seen = set()

    for seam in obj.cloth_md_seams:
        if not seam.enabled:
            continue
        chain_a, chain_b = seam_chains(seam)
        # メッシュ編集で頂点が減っている可能性があるので範囲を確認する
        if any(i >= vertex_count for i in chain_a + chain_b):
            continue
        for ia, ib in seams.pair_chains(chain_a, chain_b, positions, seam.flipped):
            key = (ia, ib) if ia < ib else (ib, ia)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((ia, ib))

    return pairs


# -------------------------------------------------------------- コリジョン

def collect_collider_objects(props):
    """props の設定からコリジョン対象のメッシュオブジェクト一覧を返す(重複除去)。"""
    objects = []
    if props.collider_object is not None and props.collider_object.type == 'MESH':
        objects.append(props.collider_object)
    if props.collider_collection is not None:
        for obj in props.collider_collection.all_objects:
            if obj.type == 'MESH' and obj not in objects:
                objects.append(obj)
    return objects


def build_collider_mesh(obj, depsgraph=None, positions_only=False):
    """コライダーの三角形メッシュを (world_positions, triangles) で返す。

    モディファイア・シェイプキー・アーマチュア変形を反映させるため、
    評価済みオブジェクト(depsgraph)から取り出す。

    `positions_only` を立てると三角形の抽出を省く。動くコライダーの追従は
    座標だけ更新すれば足り、これは毎フレーム走る経路なので効いてくる。
    """
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()

    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        count = len(mesh.vertices)
        local = np.empty(count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", local)
        positions = _transform(local, eval_obj.matrix_world).ravel().tolist()

        triangles = []
        if not positions_only:
            mesh.calc_loop_triangles()
            tri_count = len(mesh.loop_triangles)
            flat = np.empty(tri_count * 3, dtype=np.int32)
            mesh.loop_triangles.foreach_get("vertices", flat)
            # pyo3 側は Vec<(u32, u32, u32)> を期待するのでタプルに揃える
            triangles = [tuple(t) for t in flat.reshape(-1, 3).tolist()]
    finally:
        eval_obj.to_mesh_clear()

    return positions, triangles


def median_edge_length(mesh):
    """エッジ長の中央値。厚みの妥当な上限を決めるのに使う。

    平均ではなく中央値を使うのは、細分化の不均一なメッシュで極端に長い/短い
    エッジに引きずられないようにするため。エッジが無ければ None。
    """
    count = len(mesh.edges)
    if count == 0:
        return None

    flat = np.empty(count * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", flat)
    coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    coords = coords.reshape(-1, 3).astype(np.float64)

    pairs = flat.reshape(-1, 2)
    lengths = np.linalg.norm(coords[pairs[:, 0]] - coords[pairs[:, 1]], axis=1)
    return float(np.median(lengths))


def suggest_thickness(obj):
    """このメッシュに合う (collision_thickness, self_collision_thickness) を返す。

    自己衝突は、制約で結ばれていない最も近い頂点対(四角形グリッドなら斜め方向、
    エッジ長の約1.41倍)より小さくないと、平らな状態で頂点対が押し合って座屈する。
    エッジ長の 0.4 倍を目安にすると、三角形メッシュでも余裕がある。

    エッジが無い場合は None を返す。
    """
    edge = median_edge_length(obj.data)
    if edge is None or edge <= 0.0:
        return None

    # オブジェクトのスケールはワールド長に効くので掛けておく
    scale = obj.matrix_world.to_scale()
    edge *= (abs(scale.x) + abs(scale.y) + abs(scale.z)) / 3.0

    return edge * 0.25, edge * 0.4


# ---------------------------------------------------------------- 検証

class MeshValidationError(Exception):
    """シミュレーションできないメッシュを渡されたときに投げる。"""


def validate_mesh(obj):
    """シミュレーション前にメッシュを検査する。

    致命的な問題は例外、続行可能な問題は警告文字列のリストとして返す。
    """
    mesh = obj.data

    if len(mesh.vertices) == 0:
        raise MeshValidationError("メッシュに頂点がありません")
    if len(mesh.edges) == 0:
        raise MeshValidationError(
            "エッジがありません。布として振る舞うには面またはエッジが必要です"
        )

    warnings = []

    if len(mesh.polygons) == 0:
        warnings.append(
            "面がありません。質量が面積から計算できないため一様質量になります"
        )

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)

        loose_verts = sum(1 for v in bm.verts if not v.link_edges)
        if loose_verts:
            warnings.append(f"孤立した頂点が {loose_verts} 個あります(落下し続けます)")

        non_manifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        if non_manifold:
            warnings.append(
                f"非多様体エッジが {non_manifold} 本あります(曲げ制約が正しく張れません)"
            )

        # 極端に短いエッジは制約解決を不安定にする
        tiny = sum(1 for e in bm.edges if e.calc_length() < 1e-5)
        if tiny:
            warnings.append(
                f"極端に短いエッジが {tiny} 本あります(不安定になる場合は Merge by Distance を検討)"
            )
    finally:
        bm.free()

    scale = obj.matrix_world.to_scale()
    if max(abs(scale.x), abs(scale.y), abs(scale.z)) > 1e-6:
        ratio = max(abs(scale.x), abs(scale.y), abs(scale.z)) / max(
            min(abs(scale.x), abs(scale.y), abs(scale.z)), 1e-9
        )
        if ratio > 1.01:
            warnings.append(
                "オブジェクトのスケールが非一様です。Apply Scale を推奨します"
            )

    return warnings


# ------------------------------------------------------------------ 構築

def check_thickness(obj, props):
    """厚みの設定がメッシュの細かさに対して妥当かを調べ、警告文を返す。"""
    suggestion = suggest_thickness(obj)
    if suggestion is None:
        return []

    collision_hint, self_hint = suggestion
    warnings = []

    if props.self_collision_enabled and props.self_collision_thickness > self_hint * 1.5:
        warnings.append(
            f"Self Thickness ({props.self_collision_thickness:.4f}) が"
            f"メッシュの細かさに対して大きすぎます。"
            f"{self_hint:.4f} 以下を推奨(Fit Thickness to Mesh で設定できます)"
        )

    if props.collision_enabled and props.collision_thickness > collision_hint * 2.0:
        warnings.append(
            f"Thickness ({props.collision_thickness:.4f}) が大きすぎて"
            f"布がコライダーから浮きます。{collision_hint:.4f} 前後を推奨"
        )

    return warnings


def build_cloth_sim(obj, props):
    """obj(メッシュオブジェクト)から ClothSim を構築する。座標はワールド座標系。

    戻り値: (sim, info) info には頂点数・制約数などの診断情報が入る。
    """
    mesh = obj.data
    warnings = validate_mesh(obj) + check_thickness(obj, props)
    positions = get_world_positions(obj)
    edges, bending_quads, triangles = _collect_topology(mesh)
    pinned = find_vertex_group_indices(obj, props.pin_vertex_group)

    sim = cloth_core.ClothSim(
        positions,
        edges,
        bending_quads,
        triangles,
        pinned,
        props.density,
        props.stretch_compliance,
        props.bending_compliance,
    )

    seam_pairs = build_seam_pairs(obj, positions)
    if seam_pairs:
        sim.set_seams(seam_pairs, props.seam_compliance)
        sim.set_seam_closure(0.0)

    collider_objects = []
    if props.collision_enabled:
        for collider in collect_collider_objects(props):
            if collider == obj:
                continue  # 自分自身はコライダーにしない(それは自己衝突の役目)
            c_positions, c_triangles = build_collider_mesh(collider)
            if not c_triangles:
                continue
            sim.add_collider(c_positions, c_triangles)
            collider_objects.append(collider.name)

    info = {
        "vertices": len(mesh.vertices),
        "edges": len(edges),
        "bending": len(bending_quads),
        "triangles": len(triangles),
        "pinned": len(pinned),
        "seams": len(seam_pairs),
        "colliders": collider_objects,
        "collider_triangles": sim.collider_triangle_count,
        "warnings": warnings,
    }
    return sim, info

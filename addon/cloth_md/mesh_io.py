"""Blenderメッシュ <-> cloth_core.ClothSim の橋渡し。

パフォーマンス方針: 頂点座標の読み書きは Python ループではなく
`foreach_get` / `foreach_set` を使う(数万頂点で桁違いに速い)。
"""

import bmesh
import bpy

from . import cloth_core
from . import seams


# ---------------------------------------------------------------- トポロジ抽出

def _collect_topology(mesh):
    """メッシュから (edges, bending_pairs, triangles) を取り出す。

    bending_pairs は Provot 方式: 各エッジに隣接する2面の対角頂点同士を
    距離制約で結ぶことで曲げ剛性を表現する。
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    edges = [(e.verts[0].index, e.verts[1].index) for e in bm.edges]

    bending_pairs = []
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        edge_verts = {v.index for v in edge.verts}
        opposite = []
        for face in edge.link_faces:
            for v in face.verts:
                if v.index not in edge_verts:
                    opposite.append(v.index)
                    break
        if len(opposite) == 2 and opposite[0] != opposite[1]:
            bending_pairs.append((opposite[0], opposite[1]))

    triangles = []
    for face in bm.faces:
        idx = [v.index for v in face.verts]
        # 扇状分割。凸でない n-gon でも質量算出用途では十分な近似
        for i in range(1, len(idx) - 1):
            triangles.append((idx[0], idx[i], idx[i + 1]))

    bm.free()
    return edges, bending_pairs, triangles


def get_world_positions(obj):
    """ワールド座標のフラット配列 [x0,y0,z0, ...] を返す。"""
    mesh = obj.data
    count = len(mesh.vertices)
    local = [0.0] * (count * 3)
    mesh.vertices.foreach_get("co", local)

    world = obj.matrix_world
    # 4x4 行列を展開して Python 側で一括変換(Vector 生成のオーバーヘッドを避ける)
    m = world
    r0, r1, r2 = m[0], m[1], m[2]
    out = [0.0] * (count * 3)
    for i in range(count):
        x = local[i * 3]
        y = local[i * 3 + 1]
        z = local[i * 3 + 2]
        out[i * 3] = r0[0] * x + r0[1] * y + r0[2] * z + r0[3]
        out[i * 3 + 1] = r1[0] * x + r1[1] * y + r1[2] * z + r1[3]
        out[i * 3 + 2] = r2[0] * x + r2[1] * y + r2[2] * z + r2[3]
    return out


def write_positions_to_mesh(obj, flat_world_positions):
    """ワールド座標のフラット配列をメッシュのローカル座標に書き戻す。"""
    mesh = obj.data
    count = len(mesh.vertices)
    if count * 3 != len(flat_world_positions):
        return False

    inv = obj.matrix_world.inverted()
    r0, r1, r2 = inv[0], inv[1], inv[2]
    local = [0.0] * (count * 3)
    for i in range(count):
        x = flat_world_positions[i * 3]
        y = flat_world_positions[i * 3 + 1]
        z = flat_world_positions[i * 3 + 2]
        local[i * 3] = r0[0] * x + r0[1] * y + r0[2] * z + r0[3]
        local[i * 3 + 1] = r1[0] * x + r1[1] * y + r1[2] * z + r1[3]
        local[i * 3 + 2] = r2[0] * x + r2[1] * y + r2[2] * z + r2[3]

    mesh.vertices.foreach_set("co", local)
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


def build_collider_mesh(obj, depsgraph=None):
    """コライダーの三角形メッシュを (world_positions, triangles) で返す。

    モディファイア・シェイプキー・アーマチュア変形を反映させるため、
    評価済みオブジェクト(depsgraph)から取り出す。
    """
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()

    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        mesh.calc_loop_triangles()

        count = len(mesh.vertices)
        local = [0.0] * (count * 3)
        mesh.vertices.foreach_get("co", local)

        m = eval_obj.matrix_world
        r0, r1, r2 = m[0], m[1], m[2]
        positions = [0.0] * (count * 3)
        for i in range(count):
            x, y, z = local[i * 3], local[i * 3 + 1], local[i * 3 + 2]
            positions[i * 3] = r0[0] * x + r0[1] * y + r0[2] * z + r0[3]
            positions[i * 3 + 1] = r1[0] * x + r1[1] * y + r1[2] * z + r1[3]
            positions[i * 3 + 2] = r2[0] * x + r2[1] * y + r2[2] * z + r2[3]

        tri_count = len(mesh.loop_triangles)
        flat = [0] * (tri_count * 3)
        mesh.loop_triangles.foreach_get("vertices", flat)
        triangles = [
            (flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2]) for i in range(tri_count)
        ]
    finally:
        eval_obj.to_mesh_clear()

    return positions, triangles


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

def build_cloth_sim(obj, props):
    """obj(メッシュオブジェクト)から ClothSim を構築する。座標はワールド座標系。

    戻り値: (sim, info) info には頂点数・制約数などの診断情報が入る。
    """
    mesh = obj.data
    warnings = validate_mesh(obj)
    positions = get_world_positions(obj)
    edges, bending_pairs, triangles = _collect_topology(mesh)
    pinned = find_vertex_group_indices(obj, props.pin_vertex_group)

    sim = cloth_core.ClothSim(
        positions,
        edges,
        bending_pairs,
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
        "bending": len(bending_pairs),
        "triangles": len(triangles),
        "pinned": len(pinned),
        "seams": len(seam_pairs),
        "colliders": collider_objects,
        "collider_triangles": sim.collider_triangle_count,
        "warnings": warnings,
    }
    return sim, info

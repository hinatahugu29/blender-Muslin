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
from . import rest_shape
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


def pattern_world_positions(obj, positions, edges, warnings=None):
    """型紙(寸法の基準)をワールド座標のフラット配列で返す。

    型紙が無い、または今のメッシュに対応していなければ `positions` を
    そのまま返す(呼び出し側は `is` で見分けられる)。対応していない場合は
    `warnings` に理由を足す。黙って今の形を基準にすると、着せた段階で
    寸法が変わったことに気づけない。
    """
    local = rest_shape.load_pattern(obj)
    if local is None:
        return positions
    reference = _transform(local, obj.matrix_world).ravel().tolist()
    reason = rest_shape.pattern_mismatch(reference, positions, edges)
    if reason is not None:
        if warnings is not None:
            warnings.append(
                f"型紙が今のメッシュと合わないため、今の形を寸法の基準にしました({reason})。"
                "着せた後に頂点を足したり消したりすると起きます。"
                "Restore Pattern で型紙を作る段階に戻せます"
            )
        return positions
    return reference


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
    """旧形式(頂点番号で持つ)シームの2本のチェーンを返す。"""
    return (
        [v.index for v in seam.chain_a],
        [v.index for v in seam.chain_b],
    )


def read_seam_codes(mesh):
    """辺ごとの縫い目の属性値と、辺の頂点対を返す。属性が無ければ (None, None)。

    オブジェクトモードのメッシュから読む。編集モード中は編集用の BMesh が
    正しいので `bmesh_seam_codes` を使う。
    """
    return read_edge_ints(mesh, seams.SEAM_ATTRIBUTE)


def read_edge_ints(mesh, name):
    """辺の整数属性 name の値と、辺の頂点対を返す(縫い目・ゴム紐で共通)。"""
    attr = mesh.attributes.get(name)
    if attr is None or attr.domain != 'EDGE' or attr.data_type != 'INT':
        return None, None
    count = len(mesh.edges)
    # 編集モード中は属性のデータが空になる(中身は編集用の BMesh にある)。
    # 長さを確かめずに読むと foreach_get が例外を投げる
    if len(attr.data) != count:
        return None, None
    codes = np.zeros(count, dtype=np.int32)
    attr.data.foreach_get("value", codes)
    edges = np.empty(count * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edges)
    return codes.tolist(), edges.reshape(-1, 2).tolist()


def object_seam_codes(obj):
    """オブジェクトの今の縫い目の属性値と辺。編集モード中は BMesh から読む。

    パネルや縫い線の描画は編集モード中にも走るので、こちらを使う。
    """
    if obj.mode == 'EDIT':
        _, codes, edges = bmesh_seam_codes(bmesh.from_edit_mesh(obj.data))
        return codes, edges
    return read_seam_codes(obj.data)


def bmesh_seam_codes(bm, create=False):
    """編集モード用。BMesh の縫い目の層と (属性値, 辺) を返す。"""
    layer = bm.edges.layers.int.get(seams.SEAM_ATTRIBUTE)
    if layer is None:
        if not create:
            return None, None, None
        layer = bm.edges.layers.int.new(seams.SEAM_ATTRIBUTE)
    codes = [e[layer] for e in bm.edges]
    edges = [(e.verts[0].index, e.verts[1].index) for e in bm.edges]
    return layer, codes, edges


def resolve_seam(obj, seam, positions=None, codes=None, edges=None):
    """シームの (chain_a, chain_b, flipped) を返す。壊れていれば None。

    - 新形式(uid あり): 辺の属性から頂点の列を作り直す。向きは今の形で
      自動判定し、`invert` が立っていれば反転する
    - 旧形式(uid 0): 保存した頂点番号をそのまま使う(以前と同じ)

    positions は向きの自動判定に使う今の形(ワールド座標)。
    codes / edges を渡すとそれを使う(何本も解くときに読み直さないため)。
    """
    vertex_count = len(obj.data.vertices)
    if seam.uid == 0:
        chain_a, chain_b = seam_chains(seam)
        if not chain_a or not chain_b or any(i >= vertex_count for i in chain_a + chain_b):
            return None
        return chain_a, chain_b, seam.flipped

    if codes is None:
        codes, edges = read_seam_codes(obj.data)
    if codes is None:
        return None
    side_a = seams.chains_from_codes(codes, edges, seam.uid, 0)
    side_b = seams.chains_from_codes(codes, edges, seam.uid, 1)
    if len(side_a) != 1 or len(side_b) != 1:
        return None
    chain_a, chain_b = side_a[0], side_b[0]
    if positions is None:
        positions = get_world_positions(obj)
    auto = seams.should_flip(chain_a, chain_b, positions)
    return chain_a, chain_b, auto != seam.invert


def next_seam_uid():
    """全オブジェクトで重ならない縫い目の uid。統合しても衝突しないように。"""
    used = 0
    for obj in bpy.data.objects:
        for seam in getattr(obj, "muslin_seams", []):
            used = max(used, seam.uid)
    return used + 1


def build_seam_pairs(obj, positions=None, report=None, pose=None):
    """オブジェクトに登録された全シームから、縫い合わせる頂点ペアを作る。

    各シームは2本の頂点チェーンを弧長で対応付ける(頂点数が違ってもよい)。
    positions は弧長を測る形(寸法の基準 = 型紙)、pose は向きの自動判定に
    使う今の形。pose を省略すると positions を使う。

    `report` にリストを渡すと、壊れて使えなかった縫い目の名前が入る。
    黙って飛ばすと「縫ったはずなのに縫われない」が原因不明のまま起きる。
    """
    if not getattr(obj, "muslin_seams", None):
        return []

    if positions is None:
        positions = get_world_positions(obj)
    if pose is None:
        pose = positions

    codes, edges = read_seam_codes(obj.data)
    pairs = []
    seen = set()

    for seam in obj.muslin_seams:
        if not seam.enabled:
            continue
        resolved = resolve_seam(obj, seam, pose, codes, edges)
        if resolved is None:
            if report is not None:
                report.append(seam.name)
            continue
        chain_a, chain_b, flipped = resolved
        for ia, ib in seams.pair_chains(chain_a, chain_b, positions, flipped):
            key = (ia, ib) if ia < ib else (ib, ia)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((ia, ib))

    return pairs


# ------------------------------------------------------------------ ゴム紐

# 辺の整数属性。値はゴム紐の uid(0 はどれにも属さない)。縫い目と同じく
# Blender が統合・細分化・削除に合わせて辺と一緒に運ぶ。
ELASTIC_ATTRIBUTE = "muslin_elastic"


def elastic_signature(obj):
    """ゴム紐の設定の指紋。走らせたまま倍率を変えたことを見つけるのに使う。"""
    return tuple((e.uid, round(e.scale, 6), e.enabled)
                 for e in getattr(obj, "muslin_elastics", []))


def elastic_edges(obj):
    """ゴム紐として縮める (辺, 倍率) の列。オブジェクトモードのメッシュから読む。"""
    wanted = {e.uid: e.scale for e in getattr(obj, "muslin_elastics", [])
              if e.enabled and e.uid}
    if not wanted:
        return [], []
    codes, edges = read_edge_ints(obj.data, ELASTIC_ATTRIBUTE)
    if codes is None:
        return [], []
    pairs, scales = [], []
    for c, e in zip(codes, edges):
        if c in wanted:
            pairs.append((int(e[0]), int(e[1])))
            scales.append(float(wanted[c]))
    return pairs, scales


def apply_elastics(obj, sim):
    """ゴム紐の倍率をコアに渡す。当てはまった辺の数を返す。"""
    pairs, scales = elastic_edges(obj)
    return sim.set_rest_scales(pairs, scales)


def next_elastic_uid():
    used = 0
    for obj in bpy.data.objects:
        for e in getattr(obj, "muslin_elastics", []):
            used = max(used, e.uid)
    return used + 1


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

    **この 0.4 は実測で裏を取ってある。** 0.30m 角・辺長 0.01m の布を床に
    堆積させ、厚みを変えて最長の辺と広がりを測った:

        厚み/辺長 0.2〜0.7 -> 最長辺 1.000倍  広がり 0.300m(変化なし)
        厚み/辺長 0.8      -> 最長辺 1.000倍  広がり 0.341m
        厚み/辺長 1.0      -> 最長辺 1.053倍  広がり 0.370m
        厚み/辺長 2.0      -> 最長辺 2.677倍  広がり 0.418m
        厚み/辺長 4.0      -> 最長辺 8.107倍  広がり 0.333m

    崩れ始めるのは 0.8倍。薦める 0.4倍、警告する 0.6倍(`check_thickness`)の
    どちらにも余裕がある。Rust 側の
    `self_collision_does_not_inflate_cloth_at_recommended_thickness` が
    0.4倍で膨らまないことを固定している。

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


# 硬い生地が硬く見えるのに要る Substeps。下回ると Bending Compliance を
# どう設定しても差が出ない(実測は check_bending_stiffness の説明を参照)。
SUBSTEPS_FOR_STIFF_FABRIC = 16

# 開始時点の食い込みを解消する反復回数。押し離した先でまた別の対が近づくので
# 数回まわす。深く絡んだ状態は数回では解けないが、そこは警告で知らせる。
UNTANGLE_ITERATIONS = 8

# これ**未満**の Bending Compliance を「硬さが売りの生地」とみなす。
# 常に出る警告は読まれなくなるので、Wool / Denim / Leather のように硬さ
# そのものが特徴の生地だけに絞る。
#
# 値は Cotton (0.02) と Wool (0.005) の間に置く。既定値そのものを境界に
# すると、FloatProperty が単精度なせいで 0.02 が 0.0199999995 として
# 読めてしまい、既定のまま警告が出る。
STIFF_BENDING_COMPLIANCE = 0.01


def needs_more_substeps_for_bending(props):
    """硬い生地を選んでいるのに Substeps が足りていないか。

    表示の仕方が場所ごとに違う(パネルは狭いので短く、オペレータは詳しく)
    ので、条件だけをここに置いて文面は呼び出し側に任せる。
    """
    return (
        props.bending_compliance < STIFF_BENDING_COMPLIANCE
        and props.substeps < SUBSTEPS_FOR_STIFF_FABRIC
    )


def check_bending_stiffness(props):
    """曲げの設定が Substeps に対して意味を持つかを調べ、警告文を返す。

    曲げ剛性は solver の反復数に依存する(PBD 系に共通の性質)。既定の
    Substeps 4 では**曲げ制約が収束しないため**、Bending Compliance を 0
    (完全剛体)にしても布は垂れきる。片持ちの短冊で測ると:

        substeps  4 -> compliance 0〜0.3 のどれでも 垂れ比 1.02〜1.04
        substeps 16 -> 硬い側が 0.89 まで下がる
        substeps 32 -> 硬い側が 0.70 まで下がる

    つまり**硬さの上限を決めているのは生地の設定ではなく solver** で、
    Substeps を上げる以外に硬くする手段が無い。黙って柔らかい布が出ると
    「プリセットが効いていない」と見えるので、選んだ時点で知らせる。
    """
    if not needs_more_substeps_for_bending(props):
        return []

    hint = (
        f"Bending Compliance ({props.bending_compliance:.4g}) は硬い生地の設定ですが、"
        f"Substeps が {props.substeps} では曲げ剛性がほとんど出ません"
        f"(この設定ではどの生地もほぼ同じように垂れます)。"
        f"硬さを出すには Quality を High 以上にするか、"
        f"Substeps を {SUBSTEPS_FOR_STIFF_FABRIC} 以上にしてください"
    )
    if props.chebyshev_radius <= 0.0:
        hint += "。あわせて Convergence Boost を 0.98 にすると効果が大きくなります"
    return [hint]


def build_cloth_sim(obj, props):
    """obj(メッシュオブジェクト)から ClothSim を構築する。座標はワールド座標系。

    戻り値: (sim, info) info には頂点数・制約数などの診断情報が入る。
    """
    mesh = obj.data
    warnings = (
        validate_mesh(obj)
        + check_thickness(obj, props)
        + check_bending_stiffness(props)
    )
    positions = get_world_positions(obj)
    edges, bending_quads, triangles = _collect_topology(mesh)
    pinned = find_vertex_group_indices(obj, props.pin_vertex_group)

    # 寸法の基準(伸びの辺長・質量の面積・曲げ)は型紙から、位置は今の形から取る。
    # 型紙が今のメッシュに対応していなければ、今の形を基準にする(以前と同じ)。
    reference = pattern_world_positions(obj, positions, edges, warnings)

    # 型紙を確定しないまま曲げて置いたピースは、曲げた形が型紙になる。
    # わざと平らでない型紙もあるので止めはしない
    if not rest_shape.is_pattern_locked(obj):
        bent = rest_shape.bent_islands(positions, edges)
        if bent:
            warnings.append(
                f"平らでないピースが {bent} 枚あり、その形のまま型紙(寸法の基準)に"
                "なりました。曲げて配置したのなら、Restore Pattern か編集で平らに戻し、"
                "Lock Pattern で型紙を確定してから曲げてください"
                "(わざと平らでない型紙なら、この警告は気にしなくて構いません)"
            )

    sim = cloth_core.ClothSim(
        reference,
        edges,
        bending_quads,
        triangles,
        pinned,
        props.density,
        props.stretch_compliance,
        props.bending_compliance,
    )
    if reference is not positions:
        sim.set_positions(positions)

    broken_seams = []
    # 縫い目の弧長による対応付けは寸法の問題なので、型紙の上で測る
    seam_pairs = build_seam_pairs(obj, reference, report=broken_seams, pose=positions)
    if broken_seams:
        warnings.append(
            f"使えない縫い目が {len(broken_seams)} 本あります"
            f"({', '.join(broken_seams[:3])}"
            f"{' ほか' if len(broken_seams) > 3 else ''})。"
            "縫い目の辺を消したか、片側が途切れて2本以上に分かれています。縫い直してください"
        )
    if seam_pairs:
        sim.set_seams(seam_pairs, props.seam_compliance)
        sim.set_seam_closure(0.0)

    elastic_count = apply_elastics(obj, sim)

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

    # 開始時点で食い込んでいる分を、速度を発生させずに片付ける。
    # そのまま走らせると押し出した距離がそのまま速度になり、布が吹き飛ぶ
    # (分離速度 = 食い込みの深さ ÷ サブステップの時間)。パターンを重ねて
    # 置いてから縫う使い方で実際に起きるので、開始前に必ず通す。
    remaining = sim.untangle(
        UNTANGLE_ITERATIONS,
        props.self_collision_enabled,
        props.self_collision_thickness,
        props.collision_enabled,
        props.collision_thickness,
        props.floor_enabled,
        props.floor_z,
    )
    if remaining:
        warnings.append(
            f"開始時点の食い込みを解消しきれませんでした(残り {remaining} 箇所)。"
            "布やコライダーが深く交差しています。配置を見直すか、"
            "Thickness を小さくしてください"
        )

    info = {
        "vertices": len(mesh.vertices),
        "edges": len(edges),
        "bending": len(bending_quads),
        "triangles": len(triangles),
        "pinned": len(pinned),
        "seams": len(seam_pairs),
        "elastic_edges": elastic_count,
        "colliders": collider_objects,
        "collider_triangles": sim.collider_triangle_count,
        "untangle_remaining": remaining,
        "warnings": warnings,
    }
    return sim, info

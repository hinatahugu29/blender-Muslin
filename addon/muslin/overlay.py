"""ビューポートへの縫い目と型紙の表示。

縫い合わせる頂点ペアを線で描く。Marvelous Designer で縫い線が見えるのと同じ役割で、
「どこがどこに縫われるのか」「向きが反転していないか」を目視確認できる。

型紙オブジェクト(M8)には、外周の輪郭を太い線で、縫い目を服と同じ色で描く。
どちらも奥行きに関係なく手前に描くので、背景やテーマの色に左右されずに見える
(ワイヤーフレームの線はテーマの色で、既定の黒い背景では見えなかった)。
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import mesh_io
from . import rest_shape
from . import seams
from . import sim_state

_draw_handle = None

# オブジェクトキー -> (シグネチャ, ペアのリスト)。毎フレームのペア再計算を避けるキャッシュ
_pair_cache = {}

SEAM_COLOR = (1.0, 0.35, 0.1, 0.9)
ACTIVE_SEAM_COLOR = (1.0, 0.9, 0.2, 1.0)

# 型紙の外周。黒い背景でも明るい背景でも目立つ、彩度の高い水色
PATTERN_OUTLINE_COLOR = (0.25, 0.85, 1.0, 1.0)
PATTERN_OUTLINE_WIDTH = 3.0
PATTERN_SEAM_WIDTH = 4.0

# 型紙オブジェクトのキー -> (指紋, 外周の頂点対, 縫い目ごとの頂点対)
_pattern_cache = {}


def _seam_signature(obj):
    """シームの構成が変わったことを検出するための指紋。

    チェーンの頂点インデックスそのものと総頂点数を含める。
    長さだけを見ていると、メッシュ編集で頂点番号がずれても指紋が変わらず、
    古いインデックスで描画して IndexError を起こす。
    """
    # 新形式の縫い目は辺の属性にあるので、その中身も指紋に入れる
    codes, _ = mesh_io.read_seam_codes(obj.data)
    return (
        len(obj.data.vertices),
        len(obj.data.edges),
        hash(tuple(codes)) if codes is not None else None,
        tuple(
            (
                s.uid,
                s.invert,
                tuple(v.index for v in s.chain_a),
                tuple(v.index for v in s.chain_b),
                s.flipped,
                s.enabled,
            )
            for s in obj.muslin_seams
        ),
    )


def _get_pairs(obj):
    """シームごとのペア一覧を返す(キャッシュ付き)。

    ペアの計算には座標が要るが、シーム構成が変わらない限り結果は変わらないので
    キャッシュする。再描画のたびに全頂点を走査すると重すぎるため。
    """
    key = sim_state.obj_key(obj)
    signature = _seam_signature(obj)
    cached = _pair_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    positions = mesh_io.get_world_positions(obj)
    codes, edges = mesh_io.read_seam_codes(obj.data)
    per_seam = []
    for seam in obj.muslin_seams:
        resolved = mesh_io.resolve_seam(obj, seam, positions, codes, edges) if seam.enabled else None
        if resolved is None:
            per_seam.append([])
            continue
        chain_a, chain_b, flipped = resolved
        per_seam.append(seams.pair_chains(chain_a, chain_b, positions, flipped))

    _pair_cache[key] = (signature, per_seam)
    return per_seam


def invalidate_cache(obj=None):
    """ペアキャッシュを捨てる。obj を省略すると全件。

    通常は `_seam_signature` が変化を検出するので不要だが、
    ファイル読み込み時のように状態ごと捨てたい場面で使う。
    """
    if obj is None:
        _pair_cache.clear()
        _pattern_cache.clear()
    else:
        _pair_cache.pop(sim_state.obj_key(obj), None)


def pattern_lines(pattern_obj, cloth):
    """型紙オブジェクトに描く線を (外周の頂点対, 縫い目ごとの頂点対) で返す。

    頂点番号は型紙オブジェクトと布で共通(1対1で対応している)。縫い目は
    布の側の登録と辺の属性から取る(型紙オブジェクトの属性は作ったときの
    写しなので、後から縫い直すと古くなる)。トポロジと縫い目が変わらない
    限り結果は変わらないのでキャッシュする。
    """
    import numpy as np
    mesh = pattern_obj.data
    if pattern_obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(mesh)
        size = (len(bm.verts), len(bm.edges))
    else:
        size = (len(mesh.vertices), len(mesh.edges))
    key = sim_state.obj_key(pattern_obj)
    signature = (size, _seam_signature(cloth))
    cached = _pattern_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]

    # 外周はオブジェクトモードのメッシュで数える(編集中に頂点を動かしても
    # トポロジは変わらない。頂点数が変わったら指紋が変わって作り直す)
    edge_count = len(mesh.edges)
    flat = np.empty(edge_count * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", flat)
    pairs = flat.reshape(-1, 2)
    loop_edges = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("edge_index", loop_edges)
    outline = [tuple(pairs[i]) for i in rest_shape.outline_edges(edge_count, loop_edges)]

    codes, cloth_edges = mesh_io.read_seam_codes(cloth.data)
    per_seam = []
    for seam in cloth.muslin_seams:
        if not seam.enabled:
            per_seam.append([])
        elif seam.uid:
            mine = {seams.seam_code(seam.uid, 0), seams.seam_code(seam.uid, 1)}
            per_seam.append([tuple(e) for c, e in zip(codes or [], cloth_edges or [])
                             if c in mine])
        else:
            chain_a, chain_b = mesh_io.seam_chains(seam)
            per_seam.append([p for chain in (chain_a, chain_b)
                             for p in zip(chain, chain[1:])])

    _pattern_cache[key] = (signature, outline, per_seam)
    return outline, per_seam


def _pattern_coords(pattern_obj, pairs):
    """頂点対の両端のワールド座標。編集中は編集用の BMesh から読む(動かしている最中も追従する)。"""
    matrix = pattern_obj.matrix_world
    if pattern_obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(pattern_obj.data)
        bm.verts.ensure_lookup_table()
        verts = bm.verts
    else:
        verts = pattern_obj.data.vertices
    count = len(verts)
    coords = []
    for a, b in pairs:
        if a >= count or b >= count:
            continue
        coords.append(matrix @ verts[a].co)
        coords.append(matrix @ verts[b].co)
    return coords


def _draw_patterns(context, tools):
    """型紙オブジェクトの外周と縫い目を描く。"""
    links = []
    for cloth in context.view_layer.objects:
        props = getattr(cloth, "muslin", None)
        target = getattr(props, "pattern_object", None) if props is not None else None
        if (target is not None and target is not cloth and target.type == 'MESH'
                and target.visible_get()):
            links.append((target, cloth))
    if not links:
        return

    region = context.region
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (region.width, region.height))
    gpu.state.blend_set('ALPHA')
    try:
        for pattern_obj, cloth in links:
            try:
                outline, per_seam = pattern_lines(pattern_obj, cloth)
            except (AttributeError, ReferenceError):
                continue

            coords = _pattern_coords(pattern_obj, outline)
            if coords:
                shader.uniform_float("lineWidth", PATTERN_OUTLINE_WIDTH)
                shader.uniform_float("color", PATTERN_OUTLINE_COLOR)
                batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)

            if not tools.show_seams:
                continue
            # 縫い目は布の側と同じ色(選んでいる縫い目は強調)。どの辺と
            # どの辺が縫われるかを、型紙の上で追えるようにする
            active_index = cloth.muslin_seam_active
            highlight_cloth = context.active_object in (cloth, pattern_obj)
            shader.uniform_float("lineWidth", PATTERN_SEAM_WIDTH)
            for seam_index, pairs in enumerate(per_seam):
                coords = _pattern_coords(pattern_obj, pairs)
                if not coords:
                    continue
                highlight = highlight_cloth and seam_index == active_index
                shader.uniform_float("color", ACTIVE_SEAM_COLOR if highlight else SEAM_COLOR)
                batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')


def _draw():
    context = bpy.context
    scene = context.scene
    tools = getattr(scene, "muslin_tools", None)
    if tools is None:
        return
    _draw_patterns(context, tools)
    if not tools.show_seams:
        return

    objects = [
        obj for obj in context.view_layer.objects
        if obj.type == 'MESH' and obj.visible_get() and len(getattr(obj, "muslin_seams", [])) > 0
    ]
    if not objects:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.line_width_set(2.0)
    gpu.state.blend_set('ALPHA')

    try:
        for obj in objects:
            try:
                per_seam = _get_pairs(obj)
            except (AttributeError, ReferenceError):
                continue

            # 縫い目に関わる頂点だけをローカル座標で読み、ワールドへ変換する。
            # 全頂点を走査すると密なメッシュで再描画が重くなる。
            matrix = obj.matrix_world
            vertices = obj.data.vertices
            active_index = obj.muslin_seam_active
            is_active_object = obj == context.active_object

            for seam_index, pairs in enumerate(per_seam):
                if not pairs:
                    continue

                vertex_count = len(vertices)
                coords = []
                for ia, ib in pairs:
                    # キャッシュが古い場合に備えた保険(描画中の例外は毎再描画で出続ける)
                    if ia >= vertex_count or ib >= vertex_count:
                        continue
                    coords.append(matrix @ vertices[ia].co)
                    coords.append(matrix @ vertices[ib].co)
                if not coords:
                    continue

                highlight = is_active_object and seam_index == active_index
                shader.uniform_float(
                    "color", ACTIVE_SEAM_COLOR if highlight else SEAM_COLOR
                )
                batch = batch_for_shader(shader, 'LINES', {"pos": coords})
                batch.draw(shader)
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)


def register():
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_VIEW'
        )


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _pair_cache.clear()
    _pattern_cache.clear()

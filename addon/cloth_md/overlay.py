"""ビューポートへの縫い目表示。

縫い合わせる頂点ペアを線で描く。MD で縫い線が見えるのと同じ役割で、
「どこがどこに縫われるのか」「向きが反転していないか」を目視確認できる。
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import mesh_io
from . import seams

_draw_handle = None

# オブジェクト名 -> (シグネチャ, ペアのリスト)。毎フレームのペア再計算を避けるキャッシュ
_pair_cache = {}

SEAM_COLOR = (1.0, 0.35, 0.1, 0.9)
ACTIVE_SEAM_COLOR = (1.0, 0.9, 0.2, 1.0)


def _seam_signature(obj):
    """シームの構成が変わったことを検出するための軽量な指紋。"""
    return tuple(
        (len(s.chain_a), len(s.chain_b), s.flipped, s.enabled)
        for s in obj.cloth_md_seams
    )


def _get_pairs(obj):
    """シームごとのペア一覧を返す(キャッシュ付き)。

    ペアの計算には座標が要るが、シーム構成が変わらない限り結果は変わらないので
    キャッシュする。再描画のたびに全頂点を走査すると重すぎるため。
    """
    signature = _seam_signature(obj)
    cached = _pair_cache.get(obj.name)
    if cached is not None and cached[0] == signature:
        return cached[1]

    positions = mesh_io.get_world_positions(obj)
    vertex_count = len(obj.data.vertices)
    per_seam = []
    for seam in obj.cloth_md_seams:
        chain_a, chain_b = mesh_io.seam_chains(seam)
        if not seam.enabled or any(i >= vertex_count for i in chain_a + chain_b):
            per_seam.append([])
            continue
        per_seam.append(seams.pair_chains(chain_a, chain_b, positions, seam.flipped))

    _pair_cache[obj.name] = (signature, per_seam)
    return per_seam


def invalidate_cache(obj_name=None):
    if obj_name is None:
        _pair_cache.clear()
    else:
        _pair_cache.pop(obj_name, None)


def _draw():
    context = bpy.context
    scene = context.scene
    props = getattr(scene, "cloth_md_props", None)
    if props is None or not props.show_seams:
        return

    objects = [
        obj for obj in context.view_layer.objects
        if obj.type == 'MESH' and obj.visible_get() and len(getattr(obj, "cloth_md_seams", [])) > 0
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
            active_index = obj.cloth_md_seam_active
            is_active_object = obj == context.active_object

            for seam_index, pairs in enumerate(per_seam):
                if not pairs:
                    continue

                coords = []
                for ia, ib in pairs:
                    coords.append(matrix @ vertices[ia].co)
                    coords.append(matrix @ vertices[ib].co)

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

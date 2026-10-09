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

from . import curve_discretize
from . import curve_eval
from . import curve_pattern
from . import mesh_io
from . import rest_shape
from . import seams
from . import sim_state

_draw_handle = None
_label_handle = None

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
        _curve_cloth_cache.clear()
        _cloth_data_cache.clear()
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


# ---------------------------------------------------------------- Curve Pattern

# 縫い目ごとの色。Curve と布の両方に同じ色で描き、どの辺とどの辺が対応するかを色で追えるようにする
SEAM_PALETTE = (
    (1.0, 0.35, 0.1, 1.0), (0.2, 0.9, 0.3, 1.0), (0.3, 0.55, 1.0, 1.0), (1.0, 0.85, 0.1, 1.0),
    (0.9, 0.3, 0.9, 1.0), (0.1, 0.9, 0.9, 1.0), (1.0, 0.55, 0.65, 1.0), (0.7, 0.55, 0.3, 1.0),
)
CURVE_SEAM_WIDTH = 5.0
LABEL_COLOR = (1.0, 1.0, 1.0, 1.0)

# 布のキー -> (指紋, {縫い目 uid: [(頂点 a, 頂点 b), ...]}, {ピース uid: 頂点の index 配列})
_curve_cloth_cache = {}


def seam_color(index):
    return SEAM_PALETTE[index % len(SEAM_PALETTE)]


def _cloth_seam_edges(cloth):
    """布の縫い目 uid ごとの辺(頂点対)と、ピースごとの頂点の index(キャッシュ付き)。"""
    import numpy as np
    key = sim_state.obj_key(cloth)
    signature = (_seam_signature(cloth), cloth.data.get(curve_pattern.GEN_KEY, ""))
    cached = _curve_cloth_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    by_uid = {}
    codes, edges = mesh_io.read_seam_codes(cloth.data)
    for code, e in zip(codes or [], edges or []):
        if code:
            by_uid.setdefault((code - 1) // 2, []).append(tuple(e))
    pieces = {}
    attr = cloth.data.attributes.get(curve_pattern.ATTR_PIECE)
    if attr is not None and len(attr.data) == len(cloth.data.vertices):
        values = np.empty(len(attr.data), dtype=np.int32)
        attr.data.foreach_get("value", values)
        for pid in sorted(set(int(v) for v in values)):
            pieces[pid] = np.nonzero(values == pid)[0]
    _curve_cloth_cache[key] = (signature, by_uid, pieces)
    return by_uid, pieces


def curve_overlay_geometry(curve, cloth):
    """Curve と布に描く、対応を示す線と文字(描画から切り離してあるので、ヘッドレスで検査できる)。

    戻り値: {"lines": [(色, [ワールド座標, ...])],   # 2 点ずつが 1 本の線分
             "labels": [(文字, ワールド座標, 色)]}
    - 縫い目ごとの色で、Curve の該当区間と、布の該当する辺を太く描く(同じ色)
    - ピースの番号を、Curve と布のそれぞれの中心に出す
    - 縫い目の名前を、Curve と布のそれぞれの区間の中ほどに出す
    """
    import numpy as np
    from mathutils import Vector

    record = curve_pattern.load_record(curve)
    if record is None:
        return {"lines": [], "labels": []}
    splines = curve_pattern.read_splines(curve)
    recorded = curve_pattern._recorded_shapes(record)
    from . import curve_ids
    taken, _new, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    ring_of = {}                         # 点 uid → (その輪郭・内部線の区間、uid の並び)
    for si, pid in taken.items():
        s = splines[si]
        segs = curve_eval.segments(s["co"], s["hl"], s["hr"], s["cyclic"])
        for u in s["uids"]:
            if u is not None:
                ring_of[u] = (segs, list(s["uids"]), si)

    rot = curve.matrix_world.to_quaternion().to_matrix()
    origin = curve.matrix_world.translation

    def to_world(xy):
        return origin + rot @ Vector((float(xy[0]), float(xy[1]), 0.0))

    lines, labels = [], []
    # ピースの番号(穴は数えない)
    numbering = [p["piece_uid"] for p in record["pieces"] if p["hole_of"] is None]
    for number, pid in enumerate(numbering, start=1):
        si = next((k for k, v in taken.items() if v == pid), None)
        if si is not None and len(splines[si]["co"]):
            labels.append((str(number), to_world(splines[si]["co"].mean(axis=0)), LABEL_COLOR))

    cloth_edges, cloth_pieces = _cloth_seam_edges(cloth) if cloth is not None else ({}, {})
    vertices = cloth.data.vertices if cloth is not None else None
    if cloth is not None:
        mw = cloth.matrix_world
        coords = np.empty(len(vertices) * 3, dtype=np.float32)
        vertices.foreach_get("co", coords)
        coords = coords.reshape(-1, 3)
        for number, pid in enumerate(numbering, start=1):
            idx = cloth_pieces.get(pid)
            if idx is not None and len(idx):
                labels.append((str(number), mw @ Vector(coords[idx].mean(axis=0).tolist()), LABEL_COLOR))

    for index, seam in enumerate(record["seams"]):
        if not seam.get("enabled", True):
            continue
        color = seam_color(index)
        # Curve 側: 両側の区間を折れ線で
        for side in ("a", "b"):
            for start, t0, end, t1 in seam[side]:
                if start not in ring_of:
                    continue
                segs, uids, _si = ring_of[start]
                pts = curve_eval.range_points(segs, uids, start, t0, end, t1)
                if pts is None or len(pts) < 2:
                    continue
                world = [to_world(p) for p in pts]
                pairs = []
                for a, b in zip(world, world[1:]):
                    pairs += [a, b]
                lines.append((color, pairs))
                labels.append((seam["name"], world[len(world) // 2], color))
        # 布側: 縫い目の辺(両側とも同じ色)
        if cloth is not None and seam["uid"] in cloth_edges:
            pairs, centre = [], []
            for a, b in cloth_edges[seam["uid"]]:
                pairs += [mw @ Vector(coords[a].tolist()), mw @ Vector(coords[b].tolist())]
                centre.append(coords[a])
            if pairs:
                lines.append((color, pairs))
                labels.append((seam["name"], mw @ Vector(np.mean(centre, axis=0).tolist()), color))
    return {"lines": lines, "labels": labels}


# 選択の連動の色と太さ。縫い目の色(パレット)と被らない白で、縫い目の線より上に太く描く
SELECTION_COLOR = (1.0, 1.0, 1.0, 1.0)
SELECTION_WIDTH = 7.0
SELECTION_POINT_SIZE = 10.0

# Curve を選んでいる間に布へ常に描く、ピースの外周。選択の白(7px)より細く薄くして、
# 選んだ部分の強調と見分けられるようにする
OUTLINE_COLOR = (1.0, 1.0, 1.0, 0.35)
OUTLINE_WIDTH = 2.5

# 布のキー -> (生成の記録, 布から組み直した離散化の結果)。生成が変わるまで使い回す
_cloth_data_cache = {}


def _cloth_data(cloth):
    key = sim_state.obj_key(cloth)
    gen = cloth.data.get(curve_pattern.GEN_KEY, "")
    cached = _cloth_data_cache.get(key)
    if cached is None or cached[0] != gen:
        cached = (gen, curve_pattern.reconstruct(cloth))
        _cloth_data_cache[key] = cached
    return cached[1]


def curve_selection(curve):
    """編集中の Curve で選ばれている区間 {(始点 uid, 終点 uid)} と点 {uid}。

    編集モードの Curve は、spline を読めば編集中の内容(選択も位置も)がそのまま見える
    (描画のたびに update_from_editmode を呼ばなくてよい)。目印(uid)の読めない点は数えない。
    """
    from . import curve_ids
    segments, points = set(), set()
    for sp in curve.data.splines:
        if sp.type != 'BEZIER':
            continue
        pts = sp.bezier_points
        n = len(pts)
        if n == 0:
            continue
        flags = [bool(p.select_control_point) for p in pts]
        if not any(flags):
            continue
        uids = curve_ids.decode_all([p.radius for p in pts], [p.weight_softbody for p in pts])
        for i in range(n):
            if not flags[i] or uids[i] is None:
                continue
            points.add(uids[i])
            j = i + 1
            if j >= n:
                if not sp.use_cyclic_u:
                    continue
                j = 0
            if flags[j] and uids[j] is not None:
                segments.add((uids[i], uids[j]))
    return segments, points


def curve_selection_geometry(curve, cloth):
    """Curve の選択に対応する、布の辺と頂点のワールド座標(ヘッドレスで検査できるよう描画から分けてある)。

    Curve を編集モードにしているときだけ返す。戻り値: {"edges": [座標, ...](2 点ずつが 1 本),
    "points": [座標, ...]}。対応が取れないとき(布が無い、生成から構造が変わった等)は空。
    """
    from mathutils import Vector
    empty = {"edges": [], "points": []}
    if cloth is None or curve.mode != 'EDIT':
        return empty
    segments, points = curve_selection(curve)
    if not segments and not points:
        return empty
    try:
        data = _cloth_data(cloth)
    except curve_pattern.CurvePatternError:
        return empty
    edges, verts = curve_discretize.boundary_selection(data, segments, points)
    vertices = cloth.data.vertices
    count = len(vertices)
    mw = cloth.matrix_world
    out_edges = []
    for a, b in edges:
        if a < count and b < count:
            out_edges += [mw @ vertices[a].co, mw @ vertices[b].co]
    out_points = [mw @ vertices[a].co for a in verts if a < count]
    return {"edges": out_edges, "points": out_points}


def outline_target(active):
    """外周を常に描く (Curve, 布) の組。Curve Pattern の Curve がアクティブなときだけ(モードは問わない)。"""
    if active is None or active.type != 'CURVE' or curve_pattern.load_record(active) is None:
        return None
    cloth = curve_pattern.cloth_of(active)
    if cloth is None or not cloth.visible_get():
        return None
    return active, cloth


def curve_outline_geometry(cloth):
    """布の各ピースの外周(輪郭と穴)の辺のワールド座標。2 点ずつが 1 本。

    外周は Curve の輪郭に沿って並ぶ境界の頂点(離散化の rings)なので、Curve の線と 1 対 1 に対応する。
    対応が取れないとき(生成から構造が変わった等)は空。
    """
    import numpy as np
    from mathutils import Matrix
    try:
        data = _cloth_data(cloth)
    except curve_pattern.CurvePatternError:
        return []
    vertices = cloth.data.vertices
    count = len(vertices)
    pairs = []
    for ring in data["rings"]:
        v = np.asarray(ring["vertices"], dtype=np.int64)
        if len(v) >= 2:
            ends = np.roll(v, -1)
            pair = np.stack([v, ends], axis=1)
            pairs.append(pair[:-1] if ring.get("open") else pair)    # 内部線は閉じない
    if not pairs:
        return []
    pairs = np.concatenate(pairs).ravel()
    if pairs.max() >= count:
        return []
    coords = np.empty(count * 3, dtype=np.float32)
    vertices.foreach_get("co", coords)
    local = coords.reshape(-1, 3)[pairs]
    m = np.array(Matrix(cloth.matrix_world), dtype=np.float64)
    world = local @ m[:3, :3].T + m[:3, 3]
    return [tuple(p) for p in world.tolist()]


def _draw_curve_outline(context, tools):
    """Curve を選んでいる間、布の各ピースの外周を薄く描く(選択の連動の下地)。"""
    if not getattr(tools, "show_curve_outline", True):
        return
    target = outline_target(context.active_object)
    if target is None:
        return
    try:
        coords = curve_outline_geometry(target[1])
    except (AttributeError, ReferenceError, IndexError):
        return
    if not coords:
        return
    region = context.region
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", OUTLINE_WIDTH)
    shader.uniform_float("color", OUTLINE_COLOR)
    gpu.state.blend_set('ALPHA')
    try:
        batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')


def _curve_pairs(context):
    """表示する (Curve, 布) の組。"""
    out = []
    for obj in context.view_layer.objects:
        if obj.type != 'CURVE' or not obj.visible_get():
            continue
        if curve_pattern.load_record(obj) is None:
            continue
        cloth = curve_pattern.cloth_of(obj)
        out.append((obj, cloth if cloth is not None and cloth.visible_get() else None))
    return out


def _draw_curve_patterns(context, tools):
    """Curve Pattern の縫い目を、縫い目ごとの色で Curve と布に描く。"""
    pairs = _curve_pairs(context)
    if not pairs:
        return
    region = context.region
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (region.width, region.height))
    gpu.state.blend_set('ALPHA')
    try:
        for curve, cloth in pairs:
            try:
                geometry = curve_overlay_geometry(curve, cloth)
            except (AttributeError, ReferenceError, IndexError):
                continue
            shader.uniform_float("lineWidth", CURVE_SEAM_WIDTH)
            for color, coords in geometry["lines"]:
                shader.uniform_float("color", color)
                batch_for_shader(shader, 'LINES', {"pos": coords}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')


def _draw_curve_selection(context):
    """Curve を編集しているとき、選んだ区間・点に対応する布の辺・頂点を白で重ねて描く(選択の連動)。"""
    curve = context.active_object
    if curve is None or curve.type != 'CURVE' or curve.mode != 'EDIT':
        return
    if curve_pattern.load_record(curve) is None:
        return
    cloth = curve_pattern.cloth_of(curve)
    if cloth is None or not cloth.visible_get():
        return
    try:
        geometry = curve_selection_geometry(curve, cloth)
    except (AttributeError, ReferenceError, IndexError):
        return
    if not geometry["edges"] and not geometry["points"]:
        return
    gpu.state.blend_set('ALPHA')
    try:
        if geometry["edges"]:
            region = context.region
            shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            shader.uniform_float("viewportSize", (region.width, region.height))
            shader.uniform_float("lineWidth", SELECTION_WIDTH)
            shader.uniform_float("color", SELECTION_COLOR)
            batch_for_shader(shader, 'LINES', {"pos": geometry["edges"]}).draw(shader)
        if geometry["points"]:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.point_size_set(SELECTION_POINT_SIZE)
            shader.uniform_float("color", SELECTION_COLOR)
            batch_for_shader(shader, 'POINTS', {"pos": geometry["points"]}).draw(shader)
    finally:
        gpu.state.point_size_set(1.0)
        gpu.state.blend_set('NONE')


def _draw_labels():
    """ピースの番号と縫い目の名前(画面に重ねる文字)。"""
    import blf
    from bpy_extras import view3d_utils
    context = bpy.context
    tools = getattr(context.scene, "muslin_tools", None)
    region, rv3d = context.region, context.region_data
    if tools is None or not tools.show_seams or region is None or rv3d is None:
        return
    blf.size(0, 15.0)
    for curve, cloth in _curve_pairs(context):
        try:
            geometry = curve_overlay_geometry(curve, cloth)
        except (AttributeError, ReferenceError, IndexError):
            continue
        for text, location, color in geometry["labels"]:
            point = view3d_utils.location_3d_to_region_2d(region, rv3d, location)
            if point is None:
                continue
            blf.color(0, *color)
            blf.position(0, point.x + 4.0, point.y + 4.0, 0.0)
            blf.draw(0, text)


def _draw():
    context = bpy.context
    scene = context.scene
    tools = getattr(scene, "muslin_tools", None)
    if tools is None:
        return
    _draw_patterns(context, tools)
    _draw_curve_outline(context, tools)      # 縫い目の色と選択の白は、この上に重ねる
    if tools.show_seams:
        _draw_curve_patterns(context, tools)
    _draw_curve_selection(context)
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
    global _draw_handle, _label_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_VIEW'
        )
    if _label_handle is None:
        _label_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_labels, (), 'WINDOW', 'POST_PIXEL'
        )


def unregister():
    global _draw_handle, _label_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if _label_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_label_handle, 'WINDOW')
        _label_handle = None
    _pair_cache.clear()
    _pattern_cache.clear()
    _curve_cloth_cache.clear()
    _cloth_data_cache.clear()

"""Blender をヘッドレスで起動し、アドオン層(bpy 依存部分)を検証する。

使い方:
    blender --background --factory-startup --python scripts/blender_selftest.py

`scripts/verify_core.py` は物理コアと bpy 非依存モジュールを検証するが、
アドオン層(operators / sim_state / mesh_io / bake_ops / panels)は
構文チェックしかできない。ここはその穴を埋めるためのもの。

CHECKPOINTS.md の CP-B のうち、GUI 操作を伴わない項目を自動化している。
ビューポート描画(overlay)だけはヘッドレスで確認できないため対象外。
"""

import functools
import inspect
import math
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
import numpy as np

# Windows のコンソールは既定が cp932 / cp1252 なので、日本語を print した
# 時点で UnicodeEncodeError で落ちる。呼び出し側の PYTHONIOENCODING に
# 依存しないよう、ここで UTF-8 に寄せる。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_PARENT = REPO_ROOT / "addon"
sys.path.insert(0, str(ADDON_PARENT))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  --  {detail}" if detail else ""), flush=True)


def section(title):
    print(f"\n---- {title} ----", flush=True)


# ------------------------------------------------------------------ 補助

def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_grid(name, side=9, size=1.0, z=0.0):
    """side x side の四角形グリッドを作ってシーンに置く。"""
    mesh = bpy.data.meshes.new(name)
    step = size / (side - 1)
    verts = [(x * step, y * step, z) for y in range(side) for x in range(side)]
    faces = []
    for y in range(side - 1):
        for x in range(side - 1):
            i = y * side + x
            faces.append((i, i + 1, i + side + 1, i + side))
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def positions_of(obj):
    return [tuple(v.co) for v in obj.data.vertices]


def advance(frames, start=1):
    scene = bpy.context.scene
    for f in range(start, start + frames):
        scene.frame_set(f)


# ================================================================= 本体

def main():
    import muslin
    from muslin import bake_ops, cache_io, mesh_io, rest_shape, sim_state

    section("登録と解除")
    muslin.register()
    check("register() が通る", True, f"Blender {bpy.app.version_string}")

    handler_count = len(bpy.app.handlers.frame_change_post)
    muslin.unregister()
    muslin.register()
    check(
        "再登録してもハンドラが二重にならない",
        len(bpy.app.handlers.frame_change_post) == handler_count,
        f"{handler_count} 個",
    )

    # @persistent は属性を生やすだけで、値は None。hasattr で見る
    check(
        "frame_change ハンドラが persistent である",
        hasattr(sim_state._frame_change_handler, "_bpy_persistent"),
        "ファイル読み込みで消えない",
    )
    check(
        "load_post ハンドラが登録されている",
        sim_state._load_post_handler in bpy.app.handlers.load_post,
    )

    # ----------------------------------------------------------------
    section("mesh_io の座標往復 (numpy float32 の経路)")
    clear_scene()
    obj = make_grid("Cloth", side=9)
    obj.location = (1.0, 2.0, 3.0)
    obj.rotation_euler = (0.3, 0.2, 0.1)
    obj.scale = (1.5, 1.5, 1.5)
    bpy.context.view_layer.update()

    world = mesh_io.get_world_positions(obj)
    check(
        "foreach_get に numpy float32 を渡せる",
        len(world) == len(obj.data.vertices) * 3,
        f"{len(world)} 要素",
    )

    expected = [obj.matrix_world @ v.co for v in obj.data.vertices]
    max_diff = max(
        max(abs(world[i * 3 + k] - e[k]) for k in range(3))
        for i, e in enumerate(expected)
    )
    check(
        "ワールド変換が matrix_world と一致する",
        max_diff < 1e-5,
        f"最大差 {max_diff:.2e} m",
    )

    before = positions_of(obj)
    ok = mesh_io.write_positions_to_mesh(obj, world)
    after = positions_of(obj)
    round_trip = max(
        max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(before, after)
    )
    check("foreach_set に numpy を渡せる", ok)
    check(
        "ローカル→ワールド→ローカルで元に戻る",
        round_trip < 1e-5,
        f"最大差 {round_trip:.2e} m",
    )

    # ----------------------------------------------------------------
    section("シミュレーションの実行")
    clear_scene()
    obj = make_grid("Cloth", side=11, z=1.0)
    props = obj.muslin      # 設定は布ごとに持つ
    props.collision_enabled = False

    rest = positions_of(obj)
    res = bpy.ops.muslin.start_sim()
    check("Start Simulation が通る", res == {'FINISHED'}, str(res))
    check("状態が登録される", sim_state.is_running(obj))

    advance(12, start=1)
    moved = positions_of(obj)
    drop = rest[0][2] - moved[0][2]
    check("フレームを進めるとメッシュが落下する", drop > 0.01, f"{drop:.4f} m 下降")

    state = sim_state.get_state(obj)
    check("発散していない", state["sim"].is_finite())
    check(
        "伸び誤差が妥当",
        state["last_error"] < 0.05,
        f"{state['last_error']:.5f}",
    )

    section("リネーム追従 (session_uid でキーしている)")
    obj.name = "Renamed_Cloth"
    advance(6, start=13)
    check("リネーム後も状態が残っている", sim_state.is_running(obj))
    check(
        "リネーム後も計算が進む",
        sim_state.get_state(obj)["current_frame"] == 18,
        f"frame {sim_state.get_state(obj)['current_frame']}",
    )
    bpy.ops.muslin.stop_sim()
    check("Stop Simulation が通る", not sim_state.is_running(obj))

    # ----------------------------------------------------------------
    section("ピン留め")
    clear_scene()
    obj = make_grid("Pinned", side=11, z=1.0)
    group = obj.vertex_groups.new(name="Pin")
    top = [i for i, v in enumerate(obj.data.vertices) if v.co.y > 0.99]
    group.add(top, 1.0, 'REPLACE')
    props = obj.muslin
    props.pin_vertex_group = "Pin"
    props.collision_enabled = False

    rest = positions_of(obj)
    bpy.ops.muslin.start_sim()
    check(
        "ピン留めした頂点が検出される",
        sim_state.get_state(obj)["info"]["pinned"] == len(top),
        f"{len(top)} 頂点",
    )
    advance(20, start=1)
    now = positions_of(obj)
    pin_move = max(
        max(abs(now[i][k] - rest[i][k]) for k in range(3)) for i in top
    )
    free = [i for i in range(len(rest)) if i not in top]
    free_move = max(abs(now[i][2] - rest[i][2]) for i in free)
    check("ピン留め頂点が動かない", pin_move < 1e-6, f"最大 {pin_move:.2e} m")
    check("自由な頂点は落ちる", free_move > 0.01, f"{free_move:.4f} m")
    bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("コリジョン (モディファイア評価済みメッシュ)")
    clear_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.4, location=(0.5, 0.5, 0.0))
    sphere = bpy.context.active_object
    sphere.name = "Collider"
    sphere.modifiers.new(name="Subsurf", type='SUBSURF')

    obj = make_grid("Drape", side=15, z=0.8)
    props = obj.muslin
    props.pin_vertex_group = ""
    props.collision_enabled = True
    props.collider_object = sphere
    props.collision_thickness = 0.02

    bpy.ops.muslin.start_sim()
    info = sim_state.get_state(obj)["info"]
    raw_tris = len(sphere.data.polygons) * 2
    check(
        "コライダーが登録される",
        info["collider_triangles"] > 0,
        f"{info['collider_triangles']} 三角形",
    )
    check(
        "モディファイア適用後の形状を使っている",
        info["collider_triangles"] > raw_tris,
        f"素の {raw_tris} に対し {info['collider_triangles']}",
    )

    advance(40, start=1)
    state = sim_state.get_state(obj)
    check("接触が検出される", state["last_contacts"] > 0, f"{state['last_contacts']} 点")

    center = sphere.matrix_world.translation
    deepest = min((obj.matrix_world @ v.co - center).length for v in obj.data.vertices)
    check("球を貫通していない", deepest > 0.4 - 1e-2, f"最小距離 {deepest:.4f} (半径 0.4)")
    bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("入力バリデーション")
    clear_scene()
    empty = bpy.data.objects.new("Empty", bpy.data.meshes.new("EmptyMesh"))
    bpy.context.collection.objects.link(empty)
    bpy.context.view_layer.objects.active = empty
    try:
        res = bpy.ops.muslin.start_sim()
        check("頂点0のメッシュで中止される", res == {'CANCELLED'}, str(res))
    except RuntimeError as exc:
        check("頂点0のメッシュで中止される", True, f"例外で拒否: {type(exc).__name__}")

    obj = make_grid("Scaled", side=9)
    obj.scale = (2.0, 1.0, 1.0)
    bpy.context.view_layer.update()
    warnings = mesh_io.validate_mesh(obj)
    check(
        "非一様スケールを警告する",
        any("スケール" in w for w in warnings),
        f"{len(warnings)} 件の警告",
    )

    # ----------------------------------------------------------------
    section("布ごとに別の設定を持てる")
    clear_scene()
    silk = make_grid("Silk", side=11, z=1.0)
    silk.location = (0.0, 0.0, 0.0)
    denim = make_grid("Denim", side=11, z=1.0)
    denim.location = (2.0, 0.0, 0.0)

    silk.muslin.fabric_preset = 'CHIFFON'
    denim.muslin.fabric_preset = 'LEATHER'
    check(
        "同じシーンで別々の生地を設定できる",
        silk.muslin.density != denim.muslin.density,
        f"{silk.muslin.density:.2f} kg/m2 vs {denim.muslin.density:.2f} kg/m2",
    )

    for obj in (silk, denim):
        obj.muslin.collision_enabled = False
        bpy.context.view_layer.objects.active = obj
        bpy.ops.muslin.start_sim()
    check("2枚とも同時に走らせられる",
          sim_state.is_running(silk) and sim_state.is_running(denim))

    advance(20, start=1)
    infos = (sim_state.get_state(silk)["info"], sim_state.get_state(denim)["info"])
    check("それぞれの状態が独立している", infos[0] is not infos[1])
    check(
        "設定を変えても互いに影響しない",
        silk.muslin.density != denim.muslin.density,
        "開始後も別々のまま",
    )
    for obj in (silk, denim):
        bpy.context.view_layer.objects.active = obj
        bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("厚みの自動設定")
    clear_scene()
    obj = make_grid("Fine", side=21, size=1.0)  # エッジ長 0.05
    edge = mesh_io.median_edge_length(obj.data)
    check("エッジ長の中央値を取れる", abs(edge - 0.05) < 1e-4, f"{edge:.5f} m")

    obj.scale = (2.0, 2.0, 2.0)
    bpy.context.view_layer.update()
    scaled = mesh_io.median_edge_length(obj.data)
    hint = mesh_io.suggest_thickness(obj)
    check(
        "スケールを掛けた実寸で厚みを提案する",
        abs(hint[1] - scaled * 2.0 * 0.4) < 1e-4,
        f"自己衝突 {hint[1]:.5f} (ワールドのエッジ長 {scaled * 2.0:.5f})",
    )

    obj.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()
    props = obj.muslin
    props.self_collision_enabled = True
    props.self_collision_thickness = 0.5   # エッジ長 0.05 に対して明らかに過大
    props.collision_enabled = False
    warns = mesh_io.check_thickness(obj, props)
    check(
        "過大な Self Thickness を警告する",
        any("Self Thickness" in w for w in warns),
        f"{len(warns)} 件",
    )

    res = bpy.ops.muslin.fit_thickness()
    check("Fit Thickness to Mesh が通る", res == {'FINISHED'}, str(res))
    check(
        "提案値がエッジ長より小さい",
        props.self_collision_thickness < edge,
        f"{props.self_collision_thickness:.5f} < エッジ長 {edge:.5f}",
    )
    check(
        "設定し直すと警告が消える",
        not mesh_io.check_thickness(obj, props),
    )
    props.self_collision_enabled = False

    # ----------------------------------------------------------------
    section("硬い生地と Substeps の不整合を知らせる")
    # 曲げ剛性は solver の反復数に依存するので、Substeps が低いと
    # Bending Compliance を 0 にしても硬くならない。黙って柔らかい布が
    # 出ると「プリセットが効いていない」と見えるため警告する。
    props.substeps = 4
    props.bending_compliance = 0.0          # 完全剛体 = 最も硬い設定
    warns = mesh_io.check_bending_stiffness(props)
    check(
        "硬い生地 + 低 Substeps を警告する",
        any("Substeps" in w for w in warns),
        f"{len(warns)} 件",
    )

    props.substeps = mesh_io.SUBSTEPS_FOR_STIFF_FABRIC
    check(
        "Substeps を上げると警告が消える",
        not mesh_io.check_bending_stiffness(props),
        f"substeps={props.substeps}",
    )

    props.substeps = 4
    props.bending_compliance = 0.3          # 柔らかい生地なら低 Substeps でよい
    check(
        "柔らかい生地では警告しない",
        not mesh_io.check_bending_stiffness(props),
    )

    # 常に出る警告は読まれなくなるので、既定値では黙っていること
    defaults = obj.muslin.bl_rna.properties
    props.bending_compliance = defaults["bending_compliance"].default
    props.substeps = defaults["substeps"].default
    check(
        "既定値では警告しない",
        not mesh_io.check_bending_stiffness(props),
        f"compliance={props.bending_compliance:.4g} / substeps={props.substeps}",
    )

    # ----------------------------------------------------------------
    section("開始時点で食い込んだ布が吹き飛ばない")
    # 速度は位置差から作るので、食い込んだまま走らせると押し出した距離が
    # そのまま速度になる。Start Simulation が untangle を通していれば防げる。
    clear_scene()
    side, spacing, gap = 5, 0.05, 0.004
    thickness = 0.02
    verts, faces = [], []
    for layer in range(2):
        base = layer * side * side
        for y in range(side):
            for x in range(side):
                verts.append((x * spacing, y * spacing, layer * gap))
        for y in range(side - 1):
            for x in range(side - 1):
                i = base + y * side + x
                faces.append((i, i + 1, i + side + 1, i + side))
    mesh = bpy.data.meshes.new("Stacked")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    stacked = bpy.data.objects.new("Stacked", mesh)
    bpy.context.collection.objects.link(stacked)
    bpy.context.view_layer.objects.active = stacked

    sprops = stacked.muslin
    sprops.self_collision_enabled = True
    sprops.self_collision_thickness = thickness
    sprops.collision_enabled = False
    sprops.gravity = 0.0
    sprops.damping = 0.0

    res = bpy.ops.muslin.start_sim()
    check("食い込んだ布でも開始できる", res == {'FINISHED'}, str(res))
    info = sim_state.get_state(stacked)["info"]
    check("開始時に食い込みを解消する", info["untangle_remaining"] == 0,
          f"残り {info['untangle_remaining']} 箇所")

    advance(30)
    coords = positions_of(stacked)
    half = side * side
    spread = max(
        abs(coords[i][2] - coords[i + half][2]) for i in range(half)
    )
    check("開始後に吹き飛ばない", spread < thickness * 2.0,
          f"層間 {spread:.5f} m (厚み {thickness})")
    bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("ベイクと .blend をまたいだ再生 (CP-B14)")
    clear_scene()
    blend_dir = tempfile.mkdtemp(prefix="muslin_selftest_")
    blend_path = os.path.join(blend_dir, "baked.blend")

    obj = make_grid("Baked", side=9, z=1.0)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 15
    props = obj.muslin
    props.collision_enabled = False
    props.pin_vertex_group = ""

    before_bake = positions_of(obj)
    res = bpy.ops.muslin.bake()
    check("Bake to Disk が通る", res == {'FINISHED'}, str(res))
    check("ベイク済みの印がつく", bake_ops.is_baked(obj))

    cache_dir = obj.get("muslin_cache_dir", "")
    check("キャッシュが書き出されている", cache_io.cache_size_bytes(cache_dir) > 0,
          f"{cache_io.cache_size_bytes(cache_dir)} bytes")

    scene.frame_set(15)
    at_15 = positions_of(obj)
    scene.frame_set(1)
    at_1 = positions_of(obj)
    scrub = max(abs(a[2] - b[2]) for a, b in zip(at_1, at_15))
    check("スクラブで形状が変わる", scrub > 0.01, f"フレーム1と15で {scrub:.4f} m 差")

    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    check(
        "ファイル読み込み後もハンドラが生きている",
        sim_state._frame_change_handler in bpy.app.handlers.frame_change_post,
        "@persistent が効いている",
    )

    reopened = bpy.data.objects.get("Baked")
    check("ベイク情報が .blend に保存されている", bake_ops.is_baked(reopened))

    bpy.context.scene.frame_set(15)
    re_15 = positions_of(reopened)
    bpy.context.scene.frame_set(1)
    re_1 = positions_of(reopened)
    diff_15 = max(abs(a[2] - b[2]) for a, b in zip(at_15, re_15))
    diff_1 = max(abs(a[2] - b[2]) for a, b in zip(at_1, re_1))
    check(
        "再起動相当の後もベイク再生が効く",
        diff_15 < 1e-5 and diff_1 < 1e-5,
        f"差 {max(diff_15, diff_1):.2e} m",
    )

    # 変換はベイク再生で最後に書いたフレームの形のまま呼ばれる。Basis は開始姿勢にする
    bpy.context.scene.frame_set(15)
    res = bpy.ops.muslin.bake_to_shape_keys()
    check("Convert to Shape Keys が通る", res == {'FINISHED'}, str(res))
    keys = reopened.data.shape_keys.key_blocks
    basis = [tuple(k.co) for k in keys["Basis"].data]
    drift = max(math.dist(a, b) for a, b in zip(before_bake, basis))
    check("Basis はベイク前の形", drift < 1e-5, f"差 {drift:.2e} m")
    last = [tuple(k.co) for k in keys["muslin_0015"].data]
    drift = max(math.dist(a, b) for a, b in zip(at_15, last))
    check("フレームのシェイプキーはそのフレームの形", drift < 1e-5, f"差 {drift:.2e} m")
    curves = bake_ops._action_fcurves(reopened.data.shape_keys.animation_data.action)
    modes = {k.interpolation for c in curves for k in c.keyframe_points}
    check("キーの補間は LINEAR", len(curves) == 15 and modes == {'LINEAR'},
          f"{len(curves)} 本 / {modes}")
    reopened.shape_key_clear()

    bpy.ops.muslin.free_bake()
    check("Free Bake でキャッシュが消える",
          cache_io.cache_size_bytes(cache_dir) == 0)
    # ベイクした形のまま残すと、次の開始でそれが開始姿勢(と型紙)として取り込まれる
    after_free = positions_of(reopened)
    drift = max(math.dist(a, b) for a, b in zip(before_bake, after_free))
    check("Free Bake でベイク前の形に戻る", drift < 1e-5, f"差 {drift:.2e} m")
    bpy.ops.muslin.start_sim()
    rest = rest_shape.load(reopened).reshape(-1, 3)
    drift = max(math.dist(a, b) for a, b in zip(before_bake, rest))
    check("Free Bake の後に開始しても開始姿勢はベイク前のまま", drift < 1e-5,
          f"差 {drift:.2e} m")
    bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("パターンと縫製")
    clear_scene()
    res = bpy.ops.muslin.add_pattern_piece()
    check("Add Pattern Piece が通る", res == {'FINISHED'}, str(res))
    piece = bpy.context.active_object
    check("パターンに面がある", len(piece.data.polygons) > 0,
          f"{len(piece.data.vertices)} 頂点")

    # 左右の端のエッジ列を2本選び、シームとして登録する
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    xs = [v.co.x for v in piece.data.vertices]
    left, right = min(xs), max(xs)
    for e in piece.data.edges:
        a = piece.data.vertices[e.vertices[0]].co
        b = piece.data.vertices[e.vertices[1]].co
        if abs(a.x - left) < 1e-5 and abs(b.x - left) < 1e-5:
            e.select = True
        elif abs(a.x - right) < 1e-5 and abs(b.x - right) < 1e-5:
            e.select = True
    bpy.ops.object.mode_set(mode='EDIT')
    res = bpy.ops.muslin.add_seam()
    bpy.ops.object.mode_set(mode='OBJECT')
    check("Add Seam From Selection が通る", res == {'FINISHED'}, str(res))
    check("シームが登録される", len(piece.muslin_seams) == 1,
          f"{len(piece.muslin_seams)} 本")

    pairs = mesh_io.build_seam_pairs(piece)
    check("縫い合わせるペアが作られる", len(pairs) > 0, f"{len(pairs)} 組")

    res = bpy.ops.muslin.validate_seams()
    check("Validate Seams が通る", res == {'FINISHED'}, str(res))

    # メッシュを編集して頂点番号がずれたら、黙って無視せず警告すること
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.delete(type='VERT')
    bpy.ops.object.mode_set(mode='OBJECT')
    broken = []
    mesh_io.build_seam_pairs(piece, report=broken)
    check("壊れた縫い目を報告する", len(broken) == 1, f"{broken}")

    # ----------------------------------------------------------------
    section("縫い目を辺の属性で持つ(M7)")
    from muslin import seams as seams_mod

    def select_side_edges(o):
        """左右の端のエッジ列を選んだ状態で編集モードに入る。"""
        bpy.ops.object.mode_set(mode='OBJECT')
        for e in o.data.edges:
            e.select = False
        for v in o.data.vertices:
            v.select = False
        xs_ = [v.co.x for v in o.data.vertices]
        lo, hi = min(xs_), max(xs_)
        for e in o.data.edges:
            a = o.data.vertices[e.vertices[0]].co
            b = o.data.vertices[e.vertices[1]].co
            if (abs(a.x - lo) < 1e-5 and abs(b.x - lo) < 1e-5) or \
               (abs(a.x - hi) < 1e-5 and abs(b.x - hi) < 1e-5):
                e.select = True
        bpy.ops.object.mode_set(mode='EDIT')

    def pair_set(o):
        return {tuple(sorted(p)) for p in mesh_io.build_seam_pairs(o)}

    def make_tube_piece(name, x):
        bpy.ops.object.select_all(action='DESELECT') if bpy.context.object else None
        bpy.context.scene.cursor.location = (x, 0.0, 1.0)
        bpy.ops.muslin.add_pattern_piece()
        o = bpy.context.active_object
        o.name = name
        select_side_edges(o)
        bpy.ops.muslin.add_seam()
        bpy.ops.object.mode_set(mode='OBJECT')
        return o

    clear_scene()
    piece_a = make_tube_piece("TubeA", 0.0)
    seam = piece_a.muslin_seams[0]
    check("新しい縫い目は uid を持つ", seam.uid > 0, str(seam.uid))
    check("頂点番号は持たない", len(seam.chain_a) == 0 and len(seam.chain_b) == 0)
    before = pair_set(piece_a)
    check("属性から縫い合わせるペアが作られる", len(before) > 0, f"{len(before)} 組")

    # 別のピースを足して統合しても縫い目が残る(以前は全部消えていた)
    piece_b = make_tube_piece("TubeB", 2.0)
    piece_c_obj = None
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.scene.objects:
        o.select_set(o in (piece_a, piece_b))
    bpy.context.view_layer.objects.active = piece_a
    res = bpy.ops.muslin.join_pieces()
    check("Join Pattern Pieces が通る", res == {'FINISHED'}, str(res))
    check("統合しても両方の縫い目が残る", len(piece_a.muslin_seams) == 2,
          f"{len(piece_a.muslin_seams)} 本")
    broken = []
    after = pair_set(piece_a)
    mesh_io.build_seam_pairs(piece_a, report=broken)
    check("統合後も壊れていない", broken == [], str(broken))
    check("アクティブ側のペアは統合前と同じ", before <= after,
          f"統合前 {len(before)} / 統合後 {len(after)}")
    check("統合したピースのペアも作られる", len(after) == 2 * len(before),
          f"{len(after)} 組")

    # 細分化しても辺と一緒に属性が運ばれる
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.subdivide()
    bpy.ops.object.mode_set(mode='OBJECT')
    broken = []
    subdivided = mesh_io.build_seam_pairs(piece_a, report=broken)
    check("細分化しても縫い目が壊れない", broken == [], str(broken))
    check("細分化で縫い合わせる頂点が増える", len(subdivided) > len(after),
          f"{len(after)} → {len(subdivided)} 組")

    # 縫い目と関係ない頂点を消しても残る(頂点番号が詰まっても平気)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for v in piece_a.data.vertices:
        v.select = v.co.x > 1.0          # 統合した2つめのピースだけ
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='VERT')
    bpy.ops.object.mode_set(mode='OBJECT')
    broken = []
    remaining = mesh_io.build_seam_pairs(piece_a, report=broken)
    check("前のピースの縫い目は残る", len(remaining) > 0 and
          broken == [piece_a.muslin_seams[1].name], f"{len(remaining)} 組 / 壊れた {broken}")

    # 縫い目を消すと属性からも消える
    piece_a.muslin_seam_active = 0
    uid0 = piece_a.muslin_seams[0].uid
    bpy.ops.muslin.remove_seam()
    codes, _ = mesh_io.read_seam_codes(piece_a.data)
    check("Remove Seam で属性からも消える",
          codes is not None and seams_mod.seam_code(uid0, 0) not in codes
          and seams_mod.seam_code(uid0, 1) not in codes)

    # 複製したピース(uid が重なる)を統合しても、両方の縫い目が使える
    clear_scene()
    original = make_tube_piece("Orig", 0.0)
    copy = original.copy()
    copy.data = original.data.copy()
    copy.location.x += 2.0
    bpy.context.collection.objects.link(copy)
    check("複製は uid が重なる", copy.muslin_seams[0].uid == original.muslin_seams[0].uid)
    for o in bpy.context.scene.objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = original
    bpy.ops.muslin.join_pieces()
    uids = [s.uid for s in original.muslin_seams]
    broken = []
    pairs = mesh_io.build_seam_pairs(original, report=broken)
    check("複製を統合すると uid が振り直される", len(set(uids)) == 2, str(uids))
    check("振り直した縫い目も両方使える", broken == [] and len(pairs) > 0,
          f"{len(pairs)} 組 / 壊れた {broken}")

    # 旧形式(頂点番号)の縫い目は、統合のときに属性へ移して引き継ぐ
    clear_scene()
    bpy.ops.muslin.add_pattern_piece()
    legacy = bpy.context.active_object
    xs = [v.co.x for v in legacy.data.vertices]
    left_ids = sorted((v.index for v in legacy.data.vertices if abs(v.co.x - min(xs)) < 1e-5),
                      key=lambda i: legacy.data.vertices[i].co.z)
    right_ids = sorted((v.index for v in legacy.data.vertices if abs(v.co.x - max(xs)) < 1e-5),
                       key=lambda i: -legacy.data.vertices[i].co.z)   # わざと逆向き
    old = legacy.muslin_seams.add()
    old.name = "Legacy"
    for i in left_ids:
        old.chain_a.add().index = i
    for i in right_ids:
        old.chain_b.add().index = i
    old.flipped = True                    # 逆向きに持っているので反転して縫う
    legacy_pairs = pair_set(legacy)
    check("旧形式の縫い目もそのまま使える", len(legacy_pairs) == len(left_ids),
          f"{len(legacy_pairs)} 組")
    bpy.context.scene.cursor.location = (3.0, 0.0, 0.0)
    bpy.ops.muslin.add_pattern_piece()
    other = bpy.context.active_object
    for o in bpy.context.scene.objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = legacy
    bpy.ops.muslin.join_pieces()
    check("旧形式は統合で属性へ移る", legacy.muslin_seams[0].uid > 0)
    check("移しても同じ頂点どうしが縫われる", pair_set(legacy) == legacy_pairs,
          f"{len(pair_set(legacy))} 組")

    # 縫い目リストの1行を描けること(新旧どちらの形式でも)
    from muslin import panels as panels_mod

    class _RowLog:
        def __init__(self):
            self.props = []

        def row(self, **_kw):
            return self

        def prop(self, _data, name, **_kw):
            self.props.append(name)

        def label(self, **_kw):
            pass

    ui_list = panels_mod.MUSLIN_UL_seams
    row = _RowLog()
    ui_list.draw_item(None, bpy.context, row, legacy, legacy.muslin_seams[0], 0, legacy,
                      "muslin_seam_active", 0)
    check("新形式の行は Flip に invert を出す", "invert" in row.props, str(row.props))
    old2 = legacy.muslin_seams.add()
    row = _RowLog()
    ui_list.draw_item(None, bpy.context, row, legacy, old2, 0, legacy, "muslin_seam_active", 1)
    check("旧形式の行は flipped を出す", "flipped" in row.props, str(row.props))

    # 編集モード中も描けること。メッシュの属性データが空になるので、
    # そのまま読むと foreach_get が例外を投げていた(実機で報告された)
    legacy.muslin_seams.remove(1)
    bpy.context.view_layer.objects.active = legacy
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        row = _RowLog()
        ui_list.draw_item(None, bpy.context, row, legacy, legacy.muslin_seams[0], 0, legacy,
                          "muslin_seam_active", 0)
        check("編集モードでも縫い目の行を描ける", "invert" in row.props, str(row.props))
    except Exception as exc:
        check("編集モードでも縫い目の行を描ける", False, repr(exc))
    try:
        mesh_io.read_seam_codes(legacy.data)
        overlay_ok = True
    except Exception as exc:
        overlay_ok = repr(exc)
    check("編集モードで属性を読んでも落ちない", overlay_ok is True, str(overlay_ok))
    bpy.ops.object.mode_set(mode='OBJECT')

    # ----------------------------------------------------------------
    section("着せ付け(M7)")
    from muslin import dress as dress_mod
    from muslin import rest_shape

    def make_dress_scene():
        """1枚を円柱(胴)のまわりに 3/4 周だけ曲げて置き、両脇を縫って着せる場面。

        平らなまま置くと、縫い目が閉じるときに布が平面の中で二つ折りになり、
        円柱に一度も触れなかった(最初の版の場面がそうだった)。型紙は平らな
        形で確定(Lock Pattern)してから曲げる。
        """
        clear_scene()
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_set(1)
        bpy.ops.mesh.primitive_cylinder_add(radius=0.06, depth=1.2, location=(0.0, 0.0, 0.0))
        body = bpy.context.active_object
        body.name = "Body"
        piece = make_tube_piece("Shirt", 0.0)
        bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
        piece.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()     # matrix_world を今の位置にそろえる
        top = max(v.co.z for v in piece.data.vertices)
        group = piece.vertex_groups.new(name="Pin")
        # 前の上端の中央だけを留める。端まで留めると縫い目の一番上が閉じようがない
        group.add([v.index for v in piece.data.vertices
                   if v.co.z > top - 1e-5 and abs(v.co.x) < 0.1], 1.0, "REPLACE")

        # 平らなうちに型紙を確定してから、円柱のまわりに曲げて置く(実際の手順どおり)
        bpy.context.view_layer.objects.active = piece
        assert bpy.ops.muslin.lock_pattern() == {'FINISHED'}
        width = max(v.co.x for v in piece.data.vertices) - min(v.co.x for v in piece.data.vertices)
        radius = width / (1.5 * math.pi)        # 3/4 周で一周する半径
        for v in piece.data.vertices:
            theta = v.co.x / radius
            v.co.x, v.co.y = radius * math.sin(theta), -radius * math.cos(theta)
        piece.data.update()
        props = piece.muslin
        props.pin_vertex_group = "Pin"
        props.collider_object = body
        props.collision_enabled = True
        props.self_collision_enabled = False
        props.seam_close_frames = 20
        for o in bpy.context.scene.objects:
            o.select_set(o is piece)
        bpy.context.view_layer.objects.active = piece
        return piece

    def seam_gap(o):
        pos = mesh_io.get_world_positions(o)
        gaps = [math.dist(pos[a * 3:a * 3 + 3], pos[b * 3:b * 3 + 3])
                for a, b in mesh_io.build_seam_pairs(o)]
        return max(gaps) if gaps else float("inf")

    piece = make_dress_scene()
    open_gap = seam_gap(piece)
    res = bpy.ops.muslin.dress()
    check("Dress が通る", res == {'FINISHED'}, str(res))
    check("タイムラインは進まない", bpy.context.scene.frame_current == 1,
          str(bpy.context.scene.frame_current))
    check("着せた段階になる", rest_shape.is_dressed(piece))
    closed_gap = seam_gap(piece)
    # 布が本当に円柱に触れていること(最初の版の場面は平面で二つ折りになり触れていなかった)
    radial = min(math.hypot(x, y) for x, y, _z in positions_of(piece))
    check("布が円柱(半径 6cm)に触れている", radial < 0.06 + piece.muslin.collision_thickness + 0.003,
          f"軸からの最短距離 {radial * 100:.2f}cm")
    check("縫い目が閉じている", closed_gap < 0.002,
          f"{open_gap * 100:.1f}cm → {closed_gap * 1000:.2f}mm")
    check("型紙は変わらない",
          abs(float(np.ptp(rest_shape.load_pattern(piece)[1::3]))) < 1e-6)
    check("シミュレーションを走らせたままにしない", not sim_state.is_running(piece))

    # 以後のアニメーションは着せた姿勢から始まる(縫い目の閉じる数十フレームが入らない)
    dressed_pos = positions_of(piece)
    bpy.ops.muslin.start_sim()
    started = positions_of(piece)
    check("開始時の形が着せた姿勢", max(math.dist(a, b) for a, b in zip(dressed_pos, started)) < 1e-5)
    advance(3, start=2)
    check("最初のフレームから縫い目は閉じたまま", seam_gap(piece) < 0.002,
          f"{seam_gap(piece) * 1000:.2f}mm")
    bpy.ops.muslin.stop_sim()
    bpy.context.scene.frame_set(1)
    check("フレーム1で着せた姿勢に戻る",
          max(math.dist(a, b) for a, b in zip(dressed_pos, positions_of(piece))) < 1e-5)

    # 落ち着いたことを判定して止まる(上限まで回らない)
    piece = make_dress_scene()
    d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene))
    while not d.step():
        pass
    check("落ち着いたと判定して止まる", d.settled and d.steps < dress_mod.DEFAULT_MAX_STEPS,
          d.summary())
    check("縫い目が閉じる前には止まらない", d.steps > piece.muslin.seam_close_frames,
          f"{d.steps} ステップ")

    # 中止すると元の形に戻る
    piece = make_dress_scene()
    before = positions_of(piece)
    d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene))
    for _ in range(10):
        d.step()
    d.show()
    moved = max(math.dist(a, b) for a, b in zip(before, positions_of(piece)))
    d.cancel()
    back = max(math.dist(a, b) for a, b in zip(before, positions_of(piece)))
    check("中止すると着せ付け前の形に戻る", moved > 0.01 and back < 1e-6,
          f"途中 {moved:.3f}m / 戻した後 {back:.2e}m")
    check("中止しても開始姿勢は変わらない",
          np.allclose(rest_shape.load(piece), np.asarray(before, dtype=np.float32).ravel(), atol=1e-6))

    # 着せ付けの最中に布をつまんで動かす(モーダルの左ドラッグと同じ呼び方)
    piece = make_dress_scene()
    d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene))
    for _ in range(40):
        d.step()
    picked = d.pick((0.0, -2.0, 0.0), (0.0, 1.0, 0.0))      # 正面から布の中央へ
    check("レイで布の頂点を拾える", picked is not None, str(picked))
    missed = d.pick((5.0, -2.0, 0.0), (0.0, 1.0, 0.0))
    check("布の外なら拾わない", missed is None, str(missed))
    if picked is not None:
        index, hit = picked
        d.begin_grab(index, hit)
        pulled = (hit[0], hit[1] - 0.1, hit[2])            # 10cm 手前へ引く
        d.move_grab(pulled)
        for _ in range(30):
            check_done = d.step()
        pos = d.state["sim"].get_positions()
        start = d.grabbed["start"]
        target = grab_target = (start[0] + pulled[0] - hit[0],
                                start[1] + pulled[1] - hit[1],
                                start[2] + pulled[2] - hit[2])
        off = math.dist(pos[index * 3:index * 3 + 3], grab_target)
        check("つまんだ頂点が目標へ行く", off < 0.005, f"{off * 1000:.1f}mm")
        check("つまんでいる間は終わらない", check_done is False and d.calm == 0)
        d.end_grab()
        check("離すとつまんでいない", d.grabbed is None and d.state["sim"].grabbed_vertex is None)
        while not d.step():
            pass
        check("離した後にあらためて落ち着く", d.settled, d.summary())
    d.cancel()

    # ---- 型紙から UV を作る: 着せた後でも型紙の実寸どおり ----
    piece = make_dress_scene()
    bpy.ops.muslin.dress()
    res = bpy.ops.muslin.pattern_to_uv()
    check("Pattern to UV が通る", res == {'FINISHED'}, str(res))
    layer = piece.data.uv_layers.get("MuslinPattern")
    check("UV マップ MuslinPattern ができる", layer is not None)
    if layer is not None:
        me = piece.data
        uv = np.empty(len(me.loops) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        lv = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", lv)
        per_vertex = np.zeros((len(me.vertices), 2))
        per_vertex[lv] = uv
        ev = np.empty(len(me.edges) * 2, dtype=np.int32)
        me.edges.foreach_get("vertices", ev)
        ev = ev.reshape(-1, 2)
        mpu = piece["muslin_meters_per_uv"]
        pat = rest_shape.load_pattern(piece).reshape(-1, 3)
        real = np.linalg.norm(pat[ev[:, 0]] - pat[ev[:, 1]], axis=1)
        on_uv = np.linalg.norm(per_vertex[ev[:, 0]] - per_vertex[ev[:, 1]], axis=1) * mpu
        check("着せた後でも UV は型紙の実寸どおり(ゆがまない)",
              np.allclose(real, on_uv, atol=1e-5), f"最大差 {np.abs(real - on_uv).max() * 1000:.3f}mm")
        now = np.asarray(positions_of(piece))
        dressed_len = np.linalg.norm(now[ev[:, 0]] - now[ev[:, 1]], axis=1)
        check("(前提)着せた形は型紙と違う", np.abs(dressed_len - real).max() > 1e-4)
        check("UV の 1 が何 m かを残す", mpu > 0.1, f"{mpu:.3f} m")
        # 胴の正面(-Y 側)にある頂点で、外(正面)から見て右 = +X に u が増える
        front = [i for i, (_x, y, _z) in enumerate(now) if y < -0.02]
        xs_front = np.array([now[i][0] for i in front])
        us_front = per_vertex[front, 0]
        slope = np.polyfit(xs_front, us_front, 1)[0] if len(front) > 2 else 0.0
        check("巻いた布を外から見て柄が左右反転しない", slope > 0.0, f"傾き {slope:.2f}")

    # ---- 型紙オブジェクト(M8): 型紙を直すと着ている布が追従する ----
    from muslin import pattern_link
    piece = make_dress_scene()
    bpy.ops.muslin.dress()
    pattern_before = rest_shape.load_pattern(piece).copy()
    res = bpy.ops.muslin.create_pattern_object()
    check("Create Pattern Object が通る", res == {'FINISHED'}, str(res))
    pat_obj = pattern_link.linked_object(piece)
    check("型紙オブジェクトが結び付く", pat_obj is not None
          and len(pat_obj.data.vertices) == len(piece.data.vertices))
    if pat_obj is not None:
        co = np.empty(len(pat_obj.data.vertices) * 3, dtype=np.float32)
        pat_obj.data.vertices.foreach_get("co", co)
        check("型紙オブジェクトの形は型紙(着せた形ではない)",
              np.allclose(co, pattern_before, atol=1e-6))
        check("型紙オブジェクトは布の頂点属性を持ち越さない",
              pat_obj.data.attributes.get(rest_shape.ATTRIBUTE) is None)
        check("作ると型紙は確定扱い", rest_shape.is_pattern_locked(piece))
        check("2つ目は作れない", not bpy.ops.muslin.create_pattern_object.poll())

        # 型紙の縦の辺(型紙で z だけが違う辺)の、着ている布での平均の長さ
        pts = pattern_before.reshape(-1, 3)
        ev = np.empty(len(piece.data.edges) * 2, dtype=np.int32)
        piece.data.edges.foreach_get("vertices", ev)
        ev = ev.reshape(-1, 2)
        vertical = ev[np.abs(pts[ev[:, 0], 0] - pts[ev[:, 1], 0]) < 1e-6]
        rest_len = float(np.mean(np.abs(pts[vertical[:, 0], 2] - pts[vertical[:, 1], 2])))

        def vertical_ratio(d):
            now = np.asarray(d.state["sim"].get_positions()).reshape(-1, 3)
            lens = np.linalg.norm(now[vertical[:, 0]] - now[vertical[:, 1]], axis=1)
            return float(np.mean(lens)) / rest_len

        # Adjust と同じ Dresser を回しながら型紙を縦に 1.2 倍にする(丈を伸ばす)
        d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene),
                              auto_finish=False)
        for _ in range(20):
            d.step()
        check("型紙が変わっていなければ何もしない", d.poll_pattern() is False)
        before_ratio = vertical_ratio(d)
        pat_obj.scale.z = 1.2
        bpy.context.view_layer.update()
        check("型紙のスケールを変えると流れる", d.poll_pattern() is True)
        check("流した直後はもう一度流さない", d.poll_pattern() is False)
        for _ in range(150):
            d.step()
        after_ratio = vertical_ratio(d)
        check("丈を 1.2 倍にすると着ている布の縦の辺が伸びる",
              abs(after_ratio / before_ratio - 1.2) < 0.04 and d.state["sim"].is_finite(),
              f"{before_ratio:.3f} → {after_ratio:.3f} ({after_ratio / before_ratio:.3f} 倍)")

        # 編集モード中の編集も流れる(編集用の BMesh から読む)
        pat_obj.scale.z = 1.0
        bpy.context.view_layer.update()
        d.poll_pattern()
        for o in bpy.context.scene.objects:
            o.select_set(o is pat_obj)
        bpy.context.view_layer.objects.active = pat_obj
        bpy.ops.object.mode_set(mode='EDIT')
        import bmesh as _bmesh
        bm = _bmesh.from_edit_mesh(pat_obj.data)
        for v in bm.verts:
            v.co.x *= 1.1
        _bmesh.update_edit_mesh(pat_obj.data)
        check("編集モード中の編集も流れる", d.poll_pattern() is True)

        # 頂点を足した型紙は反映しない(対応が取れない)
        bm.verts.new((0.0, 0.0, 5.0))
        _bmesh.update_edit_mesh(pat_obj.data)
        check("頂点数の違う型紙は流さない", d.poll_pattern() is False)
        check("流さない理由を出す", len(d.state["pattern_warnings"]) == 1,
              str(d.state["pattern_warnings"]))
        bpy.ops.mesh.select_all(action='DESELECT')
        bm = _bmesh.from_edit_mesh(pat_obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts.remove(bm.verts[len(bm.verts) - 1])
        _bmesh.update_edit_mesh(pat_obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')
        d.cancel()

        # 再生(タイムライン)でも流れる。開始時は型紙オブジェクトが型紙になる
        for o in bpy.context.scene.objects:
            o.select_set(o is piece)
        bpy.context.view_layer.objects.active = piece
        sim_state.start_simulation(piece, piece.muslin)
        stored = rest_shape.load_pattern(piece)
        co = np.empty(len(pat_obj.data.vertices) * 3, dtype=np.float32)
        pat_obj.data.vertices.foreach_get("co", co)
        check("開始時は型紙オブジェクトの形が型紙になる", np.allclose(stored, co, atol=1e-6))
        advance(3, start=2)
        state = sim_state.get_state(piece)
        pat_obj.scale.x = 1.1
        bpy.context.view_layer.update()
        advance(1, start=5)
        check("再生中の型紙の変更はフレームごとに流れる",
              sim_state.poll_pattern(state) is False and len(state["cache"]) <= 2,
              f"キャッシュ {len(state['cache'])} フレーム")
        sim_state.stop_simulation(piece)

    # ---- 型紙の確定(Lock Pattern): 曲げて配置する前に押す ----
    def bend_around(o):
        xs_ = [v.co.x for v in o.data.vertices]
        r = (max(xs_) - min(xs_)) / (1.5 * math.pi)
        for v in o.data.vertices:
            t = v.co.x / r
            v.co.x, v.co.y = r * math.sin(t), -r * math.cos(t)
        o.data.update()

    def pattern_depth(o):
        pat = rest_shape.load_pattern(o).reshape(-1, 3)
        return float(np.ptp(pat[:, 1]))

    # 確定しないまま曲げて開始すると、曲げた形が型紙になる(これが穴だった)
    clear_scene()
    bpy.ops.muslin.add_pattern_piece()
    loose = bpy.context.active_object
    loose.muslin.collision_enabled = False
    check("最初は型紙が確定していない", not rest_shape.is_pattern_locked(loose))
    bend_around(loose)
    info = sim_state.start_simulation(loose, loose.muslin)
    sim_state.stop_simulation(loose)
    check("確定せずに曲げると警告が出る",
          any("平らでないピース" in w for w in info["warnings"]), " / ".join(info["warnings"])[:80])
    check("確定せずに曲げると曲げた形が型紙になる", pattern_depth(loose) > 0.05,
          f"型紙の奥行き {pattern_depth(loose) * 100:.1f}cm")

    # 確定してから曲げれば、型紙は平らなまま
    clear_scene()
    bpy.ops.muslin.add_pattern_piece()
    locked = bpy.context.active_object
    locked.muslin.collision_enabled = False
    res = bpy.ops.muslin.lock_pattern()
    check("Lock Pattern が通る", res == {'FINISHED'} and rest_shape.is_pattern_locked(locked),
          str(res))
    check("確定済みならもう押せない", not bpy.ops.muslin.lock_pattern.poll())
    check("確定しても着せた段階にはならない(Adjust は押せない)",
          not rest_shape.is_dressed(locked) and not bpy.ops.muslin.adjust.poll())
    bend_around(locked)
    info = sim_state.start_simulation(locked, locked.muslin)
    check("確定してから曲げると警告は出ない",
          not any("平らでないピース" in w for w in info["warnings"]), " / ".join(info["warnings"])[:80])
    check("確定してから曲げても型紙は平らなまま", pattern_depth(locked) < 1e-6,
          f"{pattern_depth(locked):.2e}")
    advance(3, start=2)
    sim_state.stop_simulation(locked)

    # シミュレーションの結果が出ているときは確定できない(計算結果を型紙にしない)
    rest_shape.unlock_pattern(locked)
    check("結果が出ているときは確定できない",
          rest_shape.is_deformed(locked) and not bpy.ops.muslin.lock_pattern.poll())
    bpy.context.scene.frame_set(1)       # 元の形(曲げた形)に戻る

    # Unlock すると作る段階に戻り、次の開始で今の形が型紙になる
    bpy.ops.muslin.lock_pattern()
    res = bpy.ops.muslin.unlock_pattern()
    check("Unlock Pattern で作る段階に戻る",
          res == {'FINISHED'} and not rest_shape.is_pattern_locked(locked), str(res))

    # Restore Pattern は確定も外して、平らな型紙の形に戻す
    flat = make_grid("FlatPiece", side=5)
    flat.muslin.collision_enabled = False
    bpy.ops.muslin.lock_pattern()
    for v in flat.data.vertices:
        v.co.z += 0.1 * v.co.x
    flat.data.update()
    res = bpy.ops.muslin.restore_pattern()
    check("Restore Pattern で平らに戻り、確定も外れる",
          res == {'FINISHED'} and not rest_shape.is_pattern_locked(flat)
          and max(abs(v.co.z) for v in flat.data.vertices) < 1e-6, str(res))

    # ---- 整える(Adjust): つまむためのモード。人が確定するまで終わらない ----
    # 型紙を作る段階(縫い目が開いたまま)では押せない
    clear_scene()
    raw = make_grid("RawGrid", side=5)
    raw.muslin.collision_enabled = False
    check("着せる前は Adjust を押せない", not bpy.ops.muslin.adjust.poll())

    piece = make_dress_scene()
    bpy.ops.muslin.dress()
    check("着せた後は Adjust を押せる", bpy.ops.muslin.adjust.poll())
    dressed = positions_of(piece)

    d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene),
                          max_steps=50, auto_finish=False)
    ended = any(d.step() for _ in range(300))
    check("整えるモードは落ち着いても上限を過ぎても終わらない", not ended and d.steps == 300,
          f"{d.steps} ステップ / 落ち着き {d.settled}")
    check("落ち着いたことは分かる(ヘッダに出す)", d.settled, d.summary())

    picked = d.pick((0.0, -2.0, 0.0), (0.0, 1.0, 0.0))
    check("着せた布を拾える", picked is not None, str(picked))
    if picked is not None:
        index, hit = picked
        d.begin_grab(index, hit)
        d.move_grab((hit[0], hit[1], hit[2] + 0.08))        # 8cm 持ち上げる
        for _ in range(30):
            d.step()
        d.end_grab()
        for _ in range(60):
            d.step()
        check("確定すると保存する", d.finish() and rest_shape.is_dressed(piece))
        moved = max(math.dist(a, b) for a, b in zip(dressed, positions_of(piece)))
        check("つまんで整えた形が開始姿勢になる",
              moved > 0.01 and np.allclose(rest_shape.load(piece),
                                            np.asarray(positions_of(piece), np.float32).ravel(),
                                            atol=1e-6),
              f"着せた形から最大 {moved * 100:.1f}cm")
        check("整えても縫い目は閉じたまま", seam_gap(piece) < 0.002,
              f"{seam_gap(piece) * 1000:.2f}mm")

    # 中止すると始める前の形(着せた姿勢)に戻る
    kept = positions_of(piece)
    d = dress_mod.Dresser(piece, piece.muslin, sim_state.effective_dt(bpy.context.scene),
                          auto_finish=False)
    picked = d.pick((0.0, -2.0, 0.0), (0.0, 1.0, 0.0))
    if picked is not None:
        d.begin_grab(*picked)
        d.move_grab((picked[1][0], picked[1][1] - 0.1, picked[1][2]))
    for _ in range(20):
        d.step()
    d.show()
    d.cancel()
    check("整えるのを中止すると始める前の形に戻る",
          max(math.dist(a, b) for a, b in zip(kept, positions_of(piece))) < 1e-6)

    # スクリプトから呼んだときは決まった数だけ回して保存する
    res = bpy.ops.muslin.adjust(steps=10)
    check("スクリプトからの Adjust は保存して終わる",
          res == {'FINISHED'} and rest_shape.is_dressed(piece), str(res))

    # 上限で打ち切っても保存はする(途中でも着せた形が要ることがある)
    piece = make_dress_scene()
    res = bpy.ops.muslin.dress(max_steps=5)
    check("上限で打ち切っても保存する", res == {'FINISHED'} and rest_shape.is_dressed(piece),
          str(res))

    # ----------------------------------------------------------------
    section("ゴム紐(M7)")

    def hem_grid(name, x=0.0):
        """上端を留めた布。裾(一番下の行)の辺を選んだ状態で返す。"""
        o = make_grid(name, side=11, size=0.5, z=1.0)
        o.location.x = x
        bpy.context.view_layer.update()
        o.muslin.collision_enabled = False
        g = o.vertex_groups.new(name="Pin")
        g.add([v.index for v in o.data.vertices if v.co.y > 0.49], 1.0, 'REPLACE')
        o.muslin.pin_vertex_group = "Pin"
        # 頂点と面の選択も外す。辺だけ選び直しても、編集モードに入ると
        # 頂点の選択(作った直後は全選択)から辺の選択が作り直される
        for v in o.data.vertices:
            v.select = v.co.y < 1e-6
        for p in o.data.polygons:
            p.select = False
        for e in o.data.edges:
            a, b = (o.data.vertices[i].co for i in e.vertices)
            e.select = a.y < 1e-6 and b.y < 1e-6
        return o

    def hem_width(o, frames=40):
        bpy.context.scene.frame_set(1)
        bpy.context.view_layer.objects.active = o
        bpy.ops.muslin.start_sim()
        advance(frames, start=2)
        bottom = [v.co.x for v in o.data.vertices
                  if rest_shape.load(o)[v.index * 3 + 1] < 1e-6]
        bpy.ops.muslin.stop_sim()
        return max(bottom) - min(bottom)

    clear_scene()
    plain = hem_grid("Plain")
    plain_width = hem_width(plain)

    clear_scene()
    band = hem_grid("Band")
    bpy.ops.object.mode_set(mode='EDIT')
    res = bpy.ops.muslin.add_elastic()
    bpy.ops.object.mode_set(mode='OBJECT')
    check("Add Elastic From Selection が通る", res == {'FINISHED'}, str(res))
    check("ゴム紐が登録される", len(band.muslin_elastics) == 1 and band.muslin_elastics[0].uid > 0)
    band.muslin_elastics[0].scale = 0.6
    pairs, scales = mesh_io.elastic_edges(band)
    check("裾の辺がゴム紐になる", len(pairs) == 10 and all(abs(s - 0.6) < 1e-6 for s in scales),
          f"{len(pairs)} 本")
    band_width = hem_width(band)
    check("ゴム紐を入れた裾は狭まる", band_width < plain_width * 0.8,
          f"ゴムなし {plain_width * 100:.1f}cm / ゴム 0.6 {band_width * 100:.1f}cm")

    # 走らせたまま倍率を変えると効く(組み立て直さない)
    bpy.context.scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    advance(20, start=2)
    band.muslin_elastics[0].scale = 1.0
    advance(40, start=22)
    state = sim_state.get_state(band)
    relaxed = [v.co.x for v in band.data.vertices if rest_shape.load(band)[v.index * 3 + 1] < 1e-6]
    check("走らせたまま倍率を 1 に戻すと裾が広がる",
          max(relaxed) - min(relaxed) > band_width * 1.1 and state["info"]["elastic_edges"] == 10,
          f"{(max(relaxed) - min(relaxed)) * 100:.1f}cm")
    check("走らせたまま変えても組み立て直しを求めない",
          sim_state.restart_reasons(band, band.muslin) == [])
    bpy.ops.muslin.stop_sim()

    # 統合で引き継ぐ(複製した布の uid は振り直す)
    bpy.context.scene.frame_set(1)
    twin = band.copy()
    twin.data = band.data.copy()
    twin.location.x += 1.0
    bpy.context.collection.objects.link(twin)
    for o in bpy.context.scene.objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = band
    bpy.ops.muslin.join_pieces()
    uids = [e.uid for e in band.muslin_elastics]
    pairs, _ = mesh_io.elastic_edges(band)
    check("統合してもゴム紐が引き継がれる", len(uids) == 2 and len(set(uids)) == 2 and len(pairs) == 20,
          f"uid {uids} / {len(pairs)} 本")

    # 削除すると属性からも消える
    band.muslin_elastic_active = 0
    removed = band.muslin_elastics[0].uid
    bpy.ops.muslin.remove_elastic()
    codes, _ = mesh_io.read_edge_ints(band.data, mesh_io.ELASTIC_ATTRIBUTE)
    check("Remove Elastic で属性からも消える", removed not in codes and len(mesh_io.elastic_edges(band)[0]) == 10)

    from muslin import panels as panels_mod
    class _Row:
        def __init__(self):
            self.props = []
        def row(self, **_kw):
            return self
        def prop(self, _d, name, **_kw):
            self.props.append(name)
    r = _Row()
    panels_mod.MUSLIN_UL_elastics.draw_item(None, bpy.context, r, band, band.muslin_elastics[0],
                                            0, band, "muslin_elastic_active", 0)
    check("ゴム紐の行を描ける", r.props == ["enabled", "name", "scale"], str(r.props))

    # ----------------------------------------------------------------
    section("重ね着(M7)")

    def layered_scene(grouped=True):
        """台の上に内側・外側の2枚を重ねて置く。grouped=False は以前の一方向の作り。"""
        clear_scene()
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_set(1)
        bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0.25, 0.25, 0.3))
        post = bpy.context.active_object
        post.name = "Post"
        inner = make_grid("Inner", side=11, size=0.5, z=0.5)
        outer = make_grid("Outer", side=11, size=0.5, z=0.53)
        for o in (inner, outer):
            p = o.muslin
            p.collision_enabled = True
            p.self_collision_enabled = True
            p.self_collision_thickness = 0.01
        inner.muslin.collider_object = post
        if grouped:
            inner.muslin.sim_group = outer.muslin.sim_group = "Outfit"
            inner.muslin.layer, outer.muslin.layer = 0, 1
        else:
            # 以前の作り: 外側だけが内側をコライダーとして見る(内側は外側を知らない)
            coll = bpy.data.collections.new("OuterColliders")
            scene.collection.children.link(coll)
            coll.objects.link(post)
            coll.objects.link(inner)
            outer.muslin.collider_collection = coll
        return inner, outer

    def center_z(o):
        c = min(o.data.vertices, key=lambda v: (v.co.x - 0.25) ** 2 + (v.co.y - 0.25) ** 2)
        return (o.matrix_world @ c.co).z

    def run_frames(objs, frames=40):
        bpy.context.scene.frame_set(1)
        for o in objs:
            bpy.context.view_layer.objects.active = o
            bpy.ops.muslin.start_sim()
        advance(frames, start=2)

    def below_count(inner, outer):
        """外側の頂点のうち、内側の布の裏側へ突き抜けているものの数。

        外側の各頂点について内側の面との最近点を求め、その面の法線(格子の面は
        上向きに作ってあり、落ちても裏返らない)で表か裏かを見る。同じ番号の
        頂点どうしで高さを比べるだけだと、垂れた側面や横にずれた所で
        突き抜けていないのに数えてしまう(最初の版がそうだった)。
        """
        ip = np.array([tuple(inner.matrix_world @ v.co) for v in inner.data.vertices])
        op = np.array([tuple(outer.matrix_world @ v.co) for v in outer.data.vertices])
        inner.data.calc_loop_triangles()
        tris = np.array([tuple(t.vertices) for t in inner.data.loop_triangles])
        a, b, c = ip[tris[:, 0]], ip[tris[:, 1]], ip[tris[:, 2]]
        normals = np.cross(b - a, c - a)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        count = 0
        for p in op:
            # 近くの面だけを見る(面の重心で近い順に)
            centers = (a + b + c) / 3
            k = int(np.argmin(np.linalg.norm(centers - p, axis=1)))
            if np.linalg.norm(centers[k] - p) > 0.05:
                continue            # 内側の布から離れている
            if (p - a[k]) @ normals[k] < 0.0:
                count += 1
        return count

    # 内側だけを落としたときの形(外側に押されなければこれと同じになるはず)
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0.25, 0.25, 0.3))
    post_only = bpy.context.active_object
    alone = make_grid("Alone", side=11, size=0.5, z=0.5)
    alone.muslin.collider_object = post_only
    alone.muslin.self_collision_enabled = True
    alone.muslin.self_collision_thickness = 0.01
    run_frames([alone])
    alone_z = center_z(alone)
    bpy.ops.muslin.stop_sim()

    inner, outer = layered_scene(grouped=True)
    members = mesh_io.group_members(outer)
    check("グループは内側から並び、先頭が設定を使う布", [m.name for m in members] == ["Inner", "Outer"],
          str([m.name for m in members]))
    bpy.context.view_layer.objects.active = outer
    info = sim_state.start_simulation(outer, outer.muslin)
    check("まとめて1つで組み立てる", info["vertices"] == 242 and len(info["members"]) == 2,
          f"{info['vertices']} 頂点 / {info['members']}")
    check("2着が同じ状態を指す", sim_state.get_state(inner) is sim_state.get_state(outer))
    advance(40, start=2)
    grouped_gap = center_z(outer) - center_z(inner)
    grouped_below = below_count(inner, outer)
    check("外側は内側を突き抜けない", grouped_gap > 0.005 and grouped_below == 0,
          f"中央の隙間 {grouped_gap * 1000:.1f}mm / 下にある頂点 {grouped_below}")
    # 層の目的は「外側が内側を体(ここでは台)の中へ押し込まない」こと。内側は
    # 上に乗った外側に膨らみを押さえられるので単独より低くなるが(物理的にも
    # 自然)、台の上面より下へは行かない
    post_top = 0.4 + inner.muslin.collision_thickness
    grouped_inner = center_z(inner)
    check("内側は台(体)の中へ押し込まれない", grouped_inner > post_top - 0.001,
          f"内側の中央 {grouped_inner:.4f} / 台の上面+厚み {post_top:.4f}(単独 {alone_z:.4f})")

    # 走らせたまま片方の生地を変えても動く(布ごとの生地をまとめて渡す)
    outer.muslin.density = 0.6
    advance(3, start=42)
    check("走らせたまま外側の生地を変えられる", sim_state.is_running(outer)
          and sim_state.get_state(outer)["sim"].is_finite())

    sim_state.stop_simulation(inner)
    check("1着を止めるとグループ全体が止まる",
          not sim_state.is_running(inner) and not sim_state.is_running(outer))

    # 比較: 同じグループでも層を付けない(両方 0)と、外側が内側を押し込む
    inner0, outer0 = layered_scene(grouped=True)
    outer0.muslin.layer = 0
    bpy.context.view_layer.objects.active = outer0
    sim_state.start_simulation(outer0, outer0.muslin)
    advance(40, start=2)
    flat_inner = center_z(inner0)
    flat_gap = center_z(outer0) - center_z(inner0)
    flat_below = below_count(inner0, outer0)
    sim_state.stop_simulation(outer0)
    print(f"[muslin] 重ね着の比較: 層なし 内側 {flat_inner:.4f} / 隙間 {flat_gap * 1000:.1f}mm / "
          f"突き抜け {flat_below} 頂点、層あり 内側 {grouped_inner:.4f} / "
          f"隙間 {grouped_gap * 1000:.1f}mm / 突き抜け {grouped_below} 頂点")

    # 比較: 以前の一方向の作り(グループにしない)
    inner1, outer1 = layered_scene(grouped=False)
    run_frames([inner1, outer1])
    oneway_gap = center_z(outer1) - center_z(inner1)
    oneway_below = below_count(inner1, outer1)
    print(f"[muslin] 重ね着の比較: 一方向 隙間 {oneway_gap * 1000:.1f}mm / 突き抜け {oneway_below} 頂点、"
          f"グループ 隙間 {grouped_gap * 1000:.1f}mm / 突き抜け {grouped_below} 頂点")
    for o in (inner1, outer1):
        sim_state.stop_simulation(o)

    # Dress はグループ全体を着せる
    inner, outer = layered_scene(grouped=True)
    bpy.context.view_layer.objects.active = outer
    res = bpy.ops.muslin.dress(max_steps=60)
    check("Dress がグループ全体を着せる", res == {'FINISHED'}
          and rest_shape.is_dressed(inner) and rest_shape.is_dressed(outer))

    # ベイクもグループ全体をまとめて計算し、布ごとにキャッシュを書く
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 6
    bpy.context.view_layer.objects.active = inner
    res = bpy.ops.muslin.bake()
    check("ベイクがグループ全体を書く", res == {'FINISHED'}
          and inner.get("muslin_baked") and outer.get("muslin_baked"), str(res))
    # 1着で Free Bake を押すと、一緒にベイクしたグループ全体が片付く
    bpy.context.view_layer.objects.active = inner
    bpy.ops.muslin.free_bake()
    check("Free Bake がグループ全体を片付ける",
          not inner.get("muslin_cache_dir") and not outer.get("muslin_cache_dir")
          and not bake_ops.is_baked(outer))

    # ----------------------------------------------------------------
    section("計測機構")
    clear_scene()
    obj = make_grid("Timed", side=11, z=1.0)
    obj.muslin.collision_enabled = False
    bpy.ops.muslin.start_sim()
    advance(5, start=1)
    res = bpy.ops.muslin.print_timings()
    check("Print Timings が通る", res == {'FINISHED'}, str(res))
    t = sim_state.get_state(obj)["sim"].timings()
    check("内訳の合計が正", t["total"] > 0.0, f"{t['total']:.3f} ms")
    bpy.ops.muslin.stop_sim()

    # ----------------------------------------------------------------
    section("サンプルシーン")
    samples = sorted((REPO_ROOT / "samples").glob("*.blend"))
    if not samples:
        check("サンプルがある", False, "samples/ が空。make_samples.py を実行してください")
    for path in samples:
        bpy.ops.wm.open_mainfile(filepath=str(path))
        scene = bpy.context.scene

        # サンプル側で布に印をつけてある。頂点数で選ぶと球を拾ってしまう
        cloth = next(
            (o for o in scene.objects if o.get("muslin_sample", False)), None
        )
        if cloth is None:
            check(f"{path.name}: 布がある", False)
            continue

        bpy.context.view_layer.objects.active = cloth
        rest = positions_of(cloth)
        res = bpy.ops.muslin.start_sim()
        if res != {'FINISHED'}:
            check(f"{path.name}: 開始できる", False, str(res))
            continue

        info = sim_state.get_state(cloth)["info"]
        advance(24, start=scene.frame_start)
        now = positions_of(cloth)
        moved = max(
            max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(rest, now)
        )
        state = sim_state.get_state(cloth)
        check(
            f"{path.name}: 開いて再生すると動く",
            moved > 0.005 and state["sim"].is_finite(),
            f"{cloth.name} {info['vertices']}頂点 / {moved:.4f} m 移動"
            f" / 縫い目 {info['seams']}",
        )
        check(
            f"{path.name}: 厚みの設定が妥当",
            not mesh_io.check_thickness(cloth, cloth.muslin),
        )
        bpy.ops.muslin.stop_sim()

    # ------------------------------------------------------------------
    section("フレームと形の同期")

    # Blender では「フレーム 1 に戻す = 元のメッシュの形に戻る」。
    # Muslin はモディファイアではなくメッシュに直接書くので、明示的に
    # 戻さないと変形したまま残る。操作の順番によらずそうなることを見る。
    from muslin import rest_shape

    def drape_scenario(steps):
        """grid を作り、steps を実行し、(元の最低 z, 今の最低 z) を返す。"""
        clear_scene()
        scene = bpy.context.scene
        scene.frame_start = 1
        o = make_grid("SyncGrid", side=9, z=1.0)
        o.muslin.collision_enabled = False
        rest_low = min(v.co.z for v in o.data.vertices)
        steps(o, scene)
        return rest_low, min(v.co.z for v in o.data.vertices), o

    def run_and_back(o, scene, stop=False, restart=False, use_rewind=False,
                     start_at=1):
        scene.frame_set(start_at)
        bpy.ops.muslin.start_sim()
        for f in range(start_at + 1, start_at + 15):
            scene.frame_set(f)
        if stop:
            bpy.ops.muslin.stop_sim()
        if restart:
            bpy.ops.muslin.start_sim()
        if use_rewind:
            bpy.ops.muslin.rewind()
        else:
            scene.frame_set(1)

    cases = [
        ("1で開始して1へ戻す", dict()),
        ("停止してから1へ戻す", dict(stop=True)),
        ("途中フレームで開始して1へ戻す", dict(start_at=30)),
        ("停止して開始し直してから1へ", dict(stop=True, restart=True)),
        ("Rewind で戻す", dict(use_rewind=True)),
        ("停止してから Rewind", dict(stop=True, use_rewind=True)),
    ]
    for title, kwargs in cases:
        rest_low, now_low, _ = drape_scenario(
            lambda o, scene, k=kwargs: run_and_back(o, scene, **k)
        )
        check(f"{title} → 元の形に戻る", abs(now_low - rest_low) < 1e-5,
              f"元 {rest_low:.3f} / 今 {now_low:.3f}")

    # 元の形はメッシュの属性として .blend に残る
    rest_low, _, obj = drape_scenario(
        lambda o, scene: run_and_back(o, scene, stop=True)
    )
    check("元の形が属性として残る", rest_shape.has_rest(obj))

    # **人が編集した形を、古い元の形で上書きしないこと**
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    obj = make_grid("EditedGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False
    scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    advance(10, start=2)
    bpy.ops.muslin.stop_sim()
    scene.frame_set(1)          # ここで元の形に戻る

    # 人がメッシュを持ち上げる(編集したことにする)
    for v in obj.data.vertices:
        v.co.z += 2.0
    obj.data.update()
    lifted = min(v.co.z for v in obj.data.vertices)

    bpy.ops.muslin.start_sim()
    started = min(v.co.z for v in obj.data.vertices)
    check(
        "人が編集した形は開始時に上書きされない",
        abs(started - lifted) < 1e-5,
        f"編集後 {lifted:.3f} / 開始時 {started:.3f}",
    )
    advance(5, start=2)
    bpy.ops.muslin.stop_sim()
    scene.frame_set(1)
    back = min(v.co.z for v in obj.data.vertices)
    check(
        "編集した形が新しい元の形になる",
        abs(back - lifted) < 1e-5,
        f"編集後 {lifted:.3f} / 戻り先 {back:.3f}",
    )

    # 明示的に戻す/記録し直すオペレータ
    clear_scene()
    obj = make_grid("ResetGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False
    rest_low = min(v.co.z for v in obj.data.vertices)
    check("記録前は Reset を押せない", not bpy.ops.muslin.reset_shape.poll())

    bpy.ops.muslin.start_sim()
    advance(10, start=2)
    deformed = min(v.co.z for v in obj.data.vertices)
    check("走らせると変形している", deformed < rest_low - 0.1, f"{deformed:.3f}")

    res = bpy.ops.muslin.reset_shape()
    check("Reset to Rest Shape が通る", res == {'FINISHED'}, str(res))
    check("Reset で元の形に戻る",
          abs(min(v.co.z for v in obj.data.vertices) - rest_low) < 1e-5)
    check("Reset でシミュレーションも止まる", not sim_state.is_running(obj))

    # 今の形を元の形にし直す
    bpy.ops.muslin.start_sim()
    advance(10, start=2)
    now = min(v.co.z for v in obj.data.vertices)
    bpy.ops.muslin.set_rest_shape()
    check("Set Current as Rest で止まる", not sim_state.is_running(obj))
    bpy.context.scene.frame_set(1)
    check(
        "以後は今の形が元の形になる",
        abs(min(v.co.z for v in obj.data.vertices) - now) < 1e-5,
        f"設定時 {now:.3f} / 戻り先 {min(v.co.z for v in obj.data.vertices):.3f}",
    )

    # メッシュを細分化しても元の形は生き残る。
    #
    # 当初は「頂点数が変われば戻せないはず」と考えていたが、元の形は
    # メッシュの属性なので **Blender が細分化に合わせて補間してくれる**。
    # 頂点数も自動で揃う。
    clear_scene()
    obj = make_grid("SubdivGrid", side=5, z=1.0)
    for v in obj.data.vertices:          # 平らだと補間の確認にならない
        v.co.z += 0.3 * v.co.x
    obj.data.update()
    rest_shape.store(obj)
    before = [v.co.copy() for v in obj.data.vertices]
    before_span = max(v.z for v in before) - min(v.z for v in before)

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.subdivide()
    bpy.ops.object.mode_set(mode='OBJECT')

    loaded = rest_shape.load(obj)
    check("細分化しても元の形が残る", loaded is not None,
          f"{len(obj.data.vertices)} 頂点")
    check("頂点数も追随する",
          loaded is not None and len(loaded) == len(obj.data.vertices) * 3,
          f"{0 if loaded is None else len(loaded) // 3} / {len(obj.data.vertices)}")
    if loaded is not None:
        zs = loaded[2::3]
        span = float(zs.max() - zs.min())
        check("補間された形が元の起伏を保つ", abs(span - before_span) < 1e-4,
              f"元 {before_span:.4f} / 細分化後 {span:.4f}")

    # 一方、属性を消したうえで頂点数が変わった場合は当然戻せない
    rest_shape.clear(obj)
    check("消したら戻せない", rest_shape.load(obj) is None)

    # .blend に保存され、開き直しても戻せること。
    # 元の形をメモリではなくメッシュの属性にした理由がここなので、
    # 往復を実際に通す。
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    obj = make_grid("SavedGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False
    rest_low = min(v.co.z for v in obj.data.vertices)

    scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    advance(10, start=2)
    bpy.ops.muslin.stop_sim()          # 変形したまま保存する
    saved_low = min(v.co.z for v in obj.data.vertices)
    check("保存時点では変形している", saved_low < rest_low - 0.1, f"{saved_low:.3f}")

    blend_path = os.path.join(tempfile.gettempdir(), "muslin_rest_roundtrip.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    bpy.ops.wm.open_mainfile(filepath=blend_path)

    reopened = bpy.data.objects.get("SavedGrid")
    check("開き直せた", reopened is not None)
    if reopened is not None:
        check("元の形が .blend に残っている", rest_shape.has_rest(reopened))
        bpy.context.scene.frame_set(1)
        back = min(v.co.z for v in reopened.data.vertices)
        check(
            "開き直してフレーム1で元の形に戻る",
            abs(back - rest_low) < 1e-5,
            f"元 {rest_low:.3f} / 戻り先 {back:.3f}",
        )
    try:
        os.remove(blend_path)
    except OSError:
        pass

    # ------------------------------------------------------------------
    section("型紙と着せた姿勢(M7)")

    # 着せた形から始め直しても、布の寸法は型紙のまま変わらないこと。
    # 以前は「今の形を元の形に」するたびに伸びが寸法に焼き込まれ、
    # 服が少しずつ大きくなっていった(ROADMAP M7 の実測)。

    def edge_total(o):
        me = o.data
        co = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        ev = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", ev)
        ev = ev.reshape(-1, 2)
        return float(np.linalg.norm(co[ev[:, 0]] - co[ev[:, 1]], axis=1).sum())

    clear_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    obj = make_grid("DressGrid", side=13, size=0.6, z=1.0)
    props = obj.muslin
    props.collision_enabled = False
    group = obj.vertex_groups.new(name="Pin")
    group.add([v.index for v in obj.data.vertices if v.co.y > 0.59], 1.0, 'REPLACE')
    props.pin_vertex_group = "Pin"
    pattern_total = edge_total(obj)
    check("最初は型紙を作る段階", not rest_shape.is_dressed(obj))

    scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    check("開始すると型紙が記録される", rest_shape.has_pattern(obj))
    advance(24, start=2)
    res = bpy.ops.muslin.set_rest_shape()
    check("Save Dressed Pose が通る", res == {'FINISHED'}, str(res))
    check("着せた段階になる", rest_shape.is_dressed(obj))
    first = edge_total(obj) / pattern_total
    check("着せた姿勢は伸びている(計測の前提)", first > 1.0001, f"{first:.5f}")

    ratios = [first]
    for _ in range(3):
        scene.frame_set(1)
        bpy.ops.muslin.start_sim()
        advance(24, start=2)
        bpy.ops.muslin.set_rest_shape()
        ratios.append(edge_total(obj) / pattern_total)
    check(
        "着せ直しても寸法が積み重ならない",
        abs(ratios[-1] - ratios[0]) < 0.0005,
        " / ".join(f"{r:.5f}" for r in ratios),
    )
    check("型紙は元の平面のまま",
          abs(float(np.ptp(rest_shape.load_pattern(obj)[2::3]))) < 1e-6)

    # 着せた段階での人の編集は姿勢の微調整。型紙は変えない
    for v in obj.data.vertices:
        v.co.z += 0.5
    obj.data.update()
    before = rest_shape.load_pattern(obj).copy()
    scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    bpy.ops.muslin.stop_sim()
    check("着せた段階の編集では型紙を取り直さない",
          np.allclose(rest_shape.load_pattern(obj), before))

    # 型紙が今のメッシュと食い違っていたら、警告して今の形を基準にする
    attr = obj.data.attributes[rest_shape.PATTERN_ATTRIBUTE]
    attr.data[0].vector = (0.0, 0.0, 0.0)
    attr.data[1].vector = (0.0, 0.0, 0.0)
    info = sim_state.start_simulation(obj, props)
    sim_state.stop_simulation(obj)
    check("食い違う型紙は警告される",
          any("型紙が今のメッシュと合わない" in w for w in info["warnings"]),
          " / ".join(info["warnings"])[:120])
    attr.data.foreach_set("vector", before)

    # 型紙に戻すと、型紙を作る段階に戻る
    res = bpy.ops.muslin.restore_pattern()
    check("Restore Pattern が通る", res == {'FINISHED'}, str(res))
    check("型紙を作る段階に戻る", not rest_shape.is_dressed(obj))
    check("メッシュが型紙の形になる",
          abs(edge_total(obj) - pattern_total) < 1e-5,
          f"{edge_total(obj):.6f} / {pattern_total:.6f}")

    # 型紙を作る段階の編集は、以前どおり型紙の変更になる
    for v in obj.data.vertices:
        v.co.x *= 1.5
    obj.data.update()
    scene.frame_set(5)
    scene.frame_set(1)
    xs_now = [v.co.x for v in obj.data.vertices]
    check("人の編集は先頭フレームに戻っても消えない",
          abs(max(xs_now) - min(xs_now) - 0.9) < 1e-5, f"{max(xs_now) - min(xs_now):.4f}")
    bpy.ops.muslin.start_sim()
    bpy.ops.muslin.stop_sim()
    xs = rest_shape.load_pattern(obj)[0::3]
    check("型紙を作る段階の編集は型紙に入る",
          abs(float(xs.max() - xs.min()) - 0.9) < 1e-5, f"{float(xs.max() - xs.min()):.4f}")

    # ------------------------------------------------------------------
    section("選択からピン留めを作る")

    from muslin import pin_ops

    clear_scene()
    obj = make_grid("PinOpsGrid", side=9, z=1.0)
    props = obj.muslin
    props.collision_enabled = False
    check("最初はグループが無い", props.pin_vertex_group == "",
          repr(props.pin_vertex_group))

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')

    # 何も選んでいなければ断る。
    # report({'ERROR'}) は Python から呼ぶと例外になるので、そちらも受ける。
    try:
        res = bpy.ops.muslin.pin_selected()
        check("未選択では中止される", res == {'CANCELLED'}, str(res))
    except RuntimeError as exc:
        check("未選択では中止される", "選択されていません" in str(exc), str(exc))

    # 上端の一列を選んでピン留め
    bpy.ops.object.mode_set(mode='OBJECT')
    top = [i for i, v in enumerate(obj.data.vertices) if v.co.y > 0.99]
    for i in top:
        obj.data.vertices[i].select = True
    bpy.ops.object.mode_set(mode='EDIT')

    res = bpy.ops.muslin.pin_selected()
    check("Pin Selected が通る", res == {'FINISHED'}, str(res))
    check("グループが作られる", props.pin_vertex_group in obj.vertex_groups,
          repr(props.pin_vertex_group))
    check("選んだ頂点が入る", pin_ops.pinned_count(obj) == len(top),
          f"{pin_ops.pinned_count(obj)} / {len(top)} 頂点")

    # 選択を変えて足すと、既存のグループに追加される(作り直さない)
    name = props.pin_vertex_group
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    bottom = [i for i, v in enumerate(obj.data.vertices) if v.co.y < 0.01]
    for i in bottom:
        obj.data.vertices[i].select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.muslin.pin_selected()
    check("同じグループに足される", props.pin_vertex_group == name, props.pin_vertex_group)
    check("グループが増えない", len(obj.vertex_groups) == 1, f"{len(obj.vertex_groups)} 個")
    check("合計が両方ぶんになる",
          pin_ops.pinned_count(obj) == len(top) + len(bottom),
          f"{pin_ops.pinned_count(obj)} / {len(top) + len(bottom)} 頂点")

    # 外す
    res = bpy.ops.muslin.unpin_selected()
    check("Unpin Selected が通る", res == {'FINISHED'}, str(res))
    check("外したぶんだけ減る", pin_ops.pinned_count(obj) == len(top),
          f"{pin_ops.pinned_count(obj)} / {len(top)} 頂点")

    # ピン留めされている頂点を選び直せる
    bpy.ops.mesh.select_all(action='DESELECT')
    res = bpy.ops.muslin.select_pinned()
    check("Select Pinned が通る", res == {'FINISHED'}, str(res))
    bpy.ops.object.mode_set(mode='OBJECT')
    selected = [i for i, v in enumerate(obj.data.vertices) if v.select]
    check("ピン留めした頂点が選択される", sorted(selected) == sorted(top),
          f"{len(selected)} / {len(top)} 頂点")

    # 作ったグループが実際にシミュレーションで効く
    bpy.ops.muslin.start_sim()
    check("開始時にピンとして認識される",
          sim_state.get_state(obj)["info"]["pinned"] == len(top),
          f"{sim_state.get_state(obj)['info']['pinned']} / {len(top)} 頂点")
    rest = positions_of(obj)
    advance(15, start=1)
    now = positions_of(obj)
    pin_move = max(
        max(abs(now[i][k] - rest[i][k]) for k in range(3)) for i in top
    )
    check("ピン留めした頂点が動かない", pin_move < 1e-6, f"最大 {pin_move:.2e} m")
    bpy.ops.muslin.stop_sim()

    # 走らせたまま編集モードで足した分も、抜けたあと効く
    bpy.ops.muslin.start_sim()
    advance(5, start=1)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.muslin.pin_selected()
    bpy.ops.object.mode_set(mode='OBJECT')
    advance(1, start=7)
    check("編集中に足したピンが抜けたあと効く",
          sim_state.get_state(obj)["info"]["pinned"] == len(obj.data.vertices),
          f"{sim_state.get_state(obj)['info']['pinned']} / {len(obj.data.vertices)} 頂点")
    bpy.ops.muslin.stop_sim()

    # オブジェクトモードでは押せない(編集モードの選択を使うため)
    for idname in ("pin_selected", "unpin_selected", "select_pinned"):
        op = getattr(bpy.ops.muslin, idname)
        check(f"muslin.{idname} はオブジェクトモードで押せない", not op.poll())

    # 編集モード以外の経路(ウェイトペイント、オブジェクトデータのパネル)で
    # グループの中身を変えた場合は、名前が同じままなので自動では気付けない。
    # invalidate_pinning() を呼べば読み直す、というところまでを確かめる。
    clear_scene()
    obj = make_grid("WeightGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False
    group = obj.vertex_groups.new(name="Pin")
    corner = [0]
    group.add(corner, 1.0, 'REPLACE')
    obj.muslin.pin_vertex_group = "Pin"

    bpy.ops.muslin.start_sim()
    advance(3, start=1)
    check("開始時のピンは1頂点",
          sim_state.get_state(obj)["info"]["pinned"] == 1,
          str(sim_state.get_state(obj)["info"]["pinned"]))

    # オブジェクトモードのまま中身だけ増やす
    more = [i for i, v in enumerate(obj.data.vertices) if v.co.y > 0.99]
    group.add(more, 1.0, 'REPLACE')
    advance(1, start=5)
    check(
        "名前が同じだけでは気付けない(既知の限界)",
        sim_state.get_state(obj)["info"]["pinned"] == 1,
        f"{sim_state.get_state(obj)['info']['pinned']} 頂点のまま",
    )

    sim_state.invalidate_pinning(obj)
    advance(1, start=6)
    # 最初に入れた1頂点は残るので、合計は和集合になる
    expected = len(set(more) | set(corner))
    check(
        "invalidate_pinning を呼べば読み直す",
        sim_state.get_state(obj)["info"]["pinned"] == expected,
        f"{sim_state.get_state(obj)['info']['pinned']} / {expected} 頂点",
    )
    bpy.ops.muslin.stop_sim()

    # ------------------------------------------------------------------
    section("生地のサムネイル")

    from muslin import previews as muslin_previews
    from muslin.properties import FABRIC_ITEMS, FABRIC_PRESETS

    check(
        "サムネイルが読み込まれている",
        muslin_previews.loaded_count() == len(FABRIC_PRESETS),
        f"{muslin_previews.loaded_count()} / {len(FABRIC_PRESETS)} 枚",
    )
    # icon_id はヘッドレスでは常に 0 になる(アイコンの割り当てに UI が要る)。
    # 画像そのものは読めているので、そちらで確かめる。実際に絵が出るかは
    # GUI でしか見られない。
    sizes = {n: muslin_previews.image_size(n) for n in FABRIC_PRESETS}
    check(
        "全プリセットの画像が読めている",
        all(sz == (128, 128) for sz in sizes.values()),
        ", ".join(f"{n}:{sz}" for n, sz in sizes.items() if sz != (128, 128)) or "全て 128x128",
    )
    check(
        "Custom には絵が無い(手動設定なので)",
        muslin_previews.image_size('CUSTOM') is None,
    )

    # items の並びと保存される番号が、プリセット表と食い違っていないこと
    keys = [item[0] for item in FABRIC_ITEMS]
    check(
        "items がプリセット表を網羅している",
        set(keys) == set(FABRIC_PRESETS) | {'CUSTOM'},
        str(set(keys) ^ (set(FABRIC_PRESETS) | {'CUSTOM'})),
    )
    numbers = [item[3] for item in FABRIC_ITEMS]
    check("保存される番号が重複していない", len(set(numbers)) == len(numbers),
          str(numbers))
    check("Custom が先頭(動的 enum の既定になる)", keys[0] == 'CUSTOM', keys[0])

    # 実際に enum として全項目を選べること。
    # 動的 items の enum は bl_rna.enum_items が空になるので、そちらは見ない。
    clear_scene()
    obj = make_grid("IconGrid")
    unselectable = []
    for key, _label, _desc, _number in FABRIC_ITEMS:
        try:
            obj.muslin.fabric_preset = key
        except TypeError:
            unselectable.append(key)
            continue
        if obj.muslin.fabric_preset != key:
            unselectable.append(key)
    check("全項目を選べる", not unselectable, f"選べない: {unselectable}")

    # 描き直しても壊れない(items コールバックが毎回新しい文字列を返すため、
    # 参照を持ち損ねると表示が壊れるという落とし穴がある)
    for _ in range(3):
        obj.muslin.fabric_preset = 'DENIM'
        obj.muslin.fabric_preset = 'SILK'
    check("何度引き直しても値が保たれる", obj.muslin.fabric_preset == 'SILK',
          obj.muslin.fabric_preset)

    # ------------------------------------------------------------------
    section("組み立て直しが要る変更")

    # コライダーと縫い目は組み立て時に登録されるので、走らせたままでは
    # 差し替えられない。黙って効かないのではなく、パネルで知らせる。
    clear_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=(0.5, 0.5, 0.0))
    sphere = bpy.context.active_object
    sphere.name = "Ball"
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0.5, 0.5, -0.6))
    cube = bpy.context.active_object
    cube.name = "Box"

    obj = make_grid("RestartGrid", side=11, z=0.8)
    bpy.context.view_layer.objects.active = obj
    props = obj.muslin
    props.collision_enabled = True
    props.collider_object = sphere

    bpy.ops.muslin.start_sim()
    check("開始直後は組み立て直し不要", sim_state.restart_reasons(obj, props) == [],
          str(sim_state.restart_reasons(obj, props)))

    props.collider_object = cube
    reasons = sim_state.restart_reasons(obj, props)
    check("コライダーを変えると知らせる", len(reasons) == 1, str(reasons))

    res = bpy.ops.muslin.restart_sim()
    check("Restart が通る", res == {'FINISHED'}, str(res))
    check("Restart で警告が消える", sim_state.restart_reasons(obj, props) == [])
    check(
        "Restart で新しいコライダーが登録される",
        sim_state.get_state(obj)["info"]["colliders"] == ["Box"],
        str(sim_state.get_state(obj)["info"]["colliders"]),
    )

    # 縫い目の本数が変わった場合も知らせる
    obj.muslin_seams.add()
    reasons = sim_state.restart_reasons(obj, props)
    check("縫い目を足すと知らせる",
          any("縫い目" in r for r in reasons), str(reasons))
    # 無効な縫い目は数に入れない
    obj.muslin_seams[0].enabled = False
    check("無効な縫い目は数えない", sim_state.restart_reasons(obj, props) == [],
          str(sim_state.restart_reasons(obj, props)))

    # 生地の変更は組み立て直し不要(走らせたまま効くので)
    props.fabric_preset = 'DENIM'
    check("生地の変更では知らせない", sim_state.restart_reasons(obj, props) == [],
          str(sim_state.restart_reasons(obj, props)))
    bpy.ops.muslin.stop_sim()
    check("停止中は何も挙げない", sim_state.restart_reasons(obj, props) == [])

    # ----------------------------------------------------------------
    section("編集モードでの出し分け")

    # 編集中は計算を止める。
    #
    # 最初は「編集モードでは obj.data.vertices に書き戻らないから安全」と
    # 考えていたが、実際は**書き込めてしまう**ことがこのテストで分かった。
    # 表示されているのは編集用の BMesh の方なので、抜けるときにそちらが
    # 書き戻され、計算した結果は捨てられて布が飛ぶ。
    clear_scene()
    obj = make_grid("EditGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False
    bpy.ops.muslin.start_sim()
    advance(5, start=1)
    state = sim_state.get_state(obj)

    bpy.ops.object.mode_set(mode='EDIT')
    before = positions_of(obj)
    frozen_frame = state["current_frame"]
    advance(10, start=6)
    after = positions_of(obj)
    moved = max(
        max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(before, after)
    )
    check("編集中はメッシュに書き込まない", moved < 1e-9, f"最大差 {moved:.2e} m")
    check("編集中はフレームも進めない",
          state["current_frame"] == frozen_frame,
          f"{frozen_frame} -> {state['current_frame']}")
    check("編集中も走行状態は保たれる", sim_state.is_running(obj))

    # --- ここまで編集モード。パネルの出し分けもこの状態で見る ---
    settings = ("MUSLIN_PT_solver", "MUSLIN_PT_material",
                "MUSLIN_PT_forces", "MUSLIN_PT_collision")
    for name in settings:
        cls = getattr(bpy.types, name)
        check(f"{name} は編集モードで隠れる", not cls.poll(bpy.context))
    # ピン留めだけは頂点グループを作る場所なので出したまま
    check("MUSLIN_PT_pinning は編集モードでも出る",
          bpy.types.MUSLIN_PT_pinning.poll(bpy.context))
    # 縫製とパターンは編集モードでこそ使う
    for name in ("MUSLIN_PT_sewing", "MUSLIN_PT_pattern"):
        cls = getattr(bpy.types, name)
        check(f"{name} は編集モードでも出る",
              not hasattr(cls, "poll") or cls.poll(bpy.context))

    # 抜けたら追いつく
    bpy.ops.object.mode_set(mode='OBJECT')
    for name in settings:
        cls = getattr(bpy.types, name)
        check(f"{name} はオブジェクトモードで戻る", cls.poll(bpy.context))

    advance(1, start=16)
    check("編集を抜けると追いつく", state["current_frame"] == 16,
          f"frame {state['current_frame']}")
    resumed = positions_of(obj)
    caught_up = max(
        max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(before, resumed)
    )
    check("抜けたあと布が動き出す", caught_up > 1e-4, f"{caught_up:.4f} m")
    bpy.ops.muslin.stop_sim()

    # ------------------------------------------------------------------
    section("走らせたまま生地を変える")

    # density / compliance / ピン留めは組み立て時に焼き込まれるので、
    # 以前は走行中に生地を選び直しても何も起きなかった。
    clear_scene()
    obj = make_grid("LiveGrid", side=11, z=1.0)
    props = obj.muslin
    props.collision_enabled = False
    props.fabric_preset = 'CHIFFON'

    bpy.ops.muslin.start_sim()
    advance(10, start=1)
    state = sim_state.get_state(obj)
    soft = positions_of(obj)

    # 柔らかい生地で計算したフレームが、この時点でキャッシュに載っている
    stale = set(state["cache"]) - {state["start_frame"]}
    check("計算したフレームがキャッシュに載る", len(stale) > 0, f"{len(stale)} フレーム")

    props.fabric_preset = 'LEATHER'
    # 反映は次のフレーム更新のとき。1フレームだけ進めて確かめる。
    advance(1, start=11)
    check(
        "生地を変えると古いキャッシュが捨てられる",
        not (stale & set(state["cache"])),
        f"残ってしまった: {sorted(stale & set(state['cache']))}",
    )

    advance(9, start=12)
    stiff = positions_of(obj)
    changed = max(
        max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(soft, stiff)
    )
    check("走行中に生地を変えると形が変わる", changed > 1e-4, f"最大差 {changed:.4f} m")
    check("生地を変えても発散しない", state["sim"].is_finite())

    # 同じ生地のままなら、キャッシュは捨てられない
    advance(5, start=21)
    before_cache = set(state["cache"])
    advance(1, start=26)
    check(
        "生地が同じならキャッシュは残る",
        before_cache <= set(state["cache"]),
        f"{len(before_cache)} -> {len(state['cache'])}",
    )

    # ピン留めも走らせたまま効く
    group = obj.vertex_groups.new(name="Pin")
    top = [i for i, v in enumerate(obj.data.vertices) if v.co.y > 0.99]
    group.add(top, 1.0, 'REPLACE')
    props.pin_vertex_group = "Pin"
    advance(2, start=27)
    check(
        "走行中に付けたピンが効く",
        sim_state.get_state(obj)["info"]["pinned"] == len(top),
        f"{sim_state.get_state(obj)['info']['pinned']} / {len(top)} 頂点",
    )
    held = positions_of(obj)
    advance(10, start=29)
    now = positions_of(obj)
    pin_move = max(
        max(abs(now[i][k] - held[i][k]) for k in range(3)) for i in top
    )
    check("ピンを付けた頂点が止まる", pin_move < 1e-6, f"最大 {pin_move:.2e} m")
    bpy.ops.muslin.stop_sim()

    # ------------------------------------------------------------------
    section("Fabric プリセット")

    from muslin.properties import FABRIC_PRESETS

    clear_scene()
    obj = make_grid("FabricGrid")
    props = obj.muslin

    for name, want in FABRIC_PRESETS.items():
        props.fabric_preset = name
        got = (props.density, props.stretch_compliance,
               props.bending_compliance, props.damping)
        expect = (want["density"], want["stretch"], want["bending"], want["damping"])
        close = all(abs(a - b) < 1e-6 for a, b in zip(got, expect))
        check(f"{name} が物性値に反映される", close,
              " ".join(f"{v:.4g}" for v in got))
        check(f"{name} を選んでも Custom に落ちない",
              props.fabric_preset == name, props.fabric_preset)

    # 手で値を変えたら Custom に落ちる(Quality と同じ作法)
    props.fabric_preset = 'DENIM'
    props.density = FABRIC_PRESETS['DENIM']["density"] + 0.1
    check("手で変えると Custom に落ちる",
          props.fabric_preset == 'CUSTOM', props.fabric_preset)
    props.fabric_preset = 'DENIM'
    check("選び直すと戻る",
          props.fabric_preset == 'DENIM'
          and abs(props.density - FABRIC_PRESETS['DENIM']["density"]) < 1e-6,
          f"{props.fabric_preset} / density={props.density:.4g}")

    # 生地と品質は互いに干渉しない(同じ _applying_preset を使っているため)
    props.quality = 'HIGH'
    props.fabric_preset = 'SILK'
    check("生地を変えても Quality は保たれる",
          props.quality == 'HIGH', props.quality)
    props.quality = 'DRAFT'
    check("品質を変えても生地は保たれる",
          props.fabric_preset == 'SILK', props.fabric_preset)

    # プリセットどうしが重複していないこと
    sigs = {n: tuple(v[k] for k in ("density", "stretch", "bending", "damping"))
            for n, v in FABRIC_PRESETS.items()}
    check("生地どうしの中身が重複していない", len(set(sigs.values())) == len(sigs))
    # 織物は重いほど曲がりにくい、という並びになっていること。
    # ニットは編み地なので、コットンより重いのに柔らかい。目付と硬さが
    # 一致しない実在の例外なので、この並びからは外す。
    woven = ['CHIFFON', 'SILK', 'COTTON', 'WOOL', 'DENIM', 'LEATHER']
    by_density = sorted(woven, key=lambda n: FABRIC_PRESETS[n]["density"])
    bendings = [FABRIC_PRESETS[n]["bending"] for n in by_density]
    check("織物は重いほど bending が小さい",
          bendings == sorted(bendings, reverse=True),
          " > ".join(f"{n}:{b:.3g}" for n, b in zip(by_density, bendings)))
    check(
        "ニットはコットンより重いのに柔らかい",
        FABRIC_PRESETS['KNIT']["density"] > FABRIC_PRESETS['COTTON']["density"]
        and FABRIC_PRESETS['KNIT']["bending"] > FABRIC_PRESETS['COTTON']["bending"],
        "編み地なので目付と硬さが一致しない",
    )

    # ------------------------------------------------------------------
    section("再生の操作")

    # Play はパネルから離れずに回せるようにするためのもの。
    # 走っていなければ開始も兼ね、起点を先頭に揃える。
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 40
    obj = make_grid("PlayGrid", side=9, z=1.0)
    obj.muslin.collision_enabled = False

    scene.frame_set(17)             # わざと途中のフレームに居る状態から
    res = bpy.ops.muslin.play()
    check("Play で開始できる", res == {'FINISHED'}, str(res))
    check("Play がシミュレーションを開始する", sim_state.is_running(obj))
    state = sim_state.get_state(obj)
    check(
        "起点がシーンの先頭に揃う",
        state["start_frame"] == scene.frame_start,
        f"start_frame={state['start_frame']} / frame_start={scene.frame_start}",
    )

    # 走らせてから巻き戻すと、布が元の形に戻る
    rest = positions_of(obj)
    advance(15, start=scene.frame_start + 1)
    moved = positions_of(obj)
    drop = max(abs(r[2] - m[2]) for r, m in zip(rest, moved))
    check("再生すると布が動く", drop > 0.01, f"{drop:.4f} m")

    res = bpy.ops.muslin.rewind()
    check("Rewind が通る", res == {'FINISHED'}, str(res))
    check(
        "Rewind で開始フレームに戻る",
        scene.frame_current == state["start_frame"],
        f"frame {scene.frame_current}",
    )
    back = positions_of(obj)
    diff = max(
        max(abs(a[k] - b[k]) for k in range(3)) for a, b in zip(rest, back)
    )
    check("Rewind で布が元の形に戻る", diff < 1e-5, f"最大差 {diff:.2e} m")

    # 既に走っているときは、起点を勝手に動かさない
    scene.frame_set(9)
    bpy.ops.muslin.play()
    check(
        "走行中の Play は起点を動かさない",
        sim_state.get_state(obj)["start_frame"] == state["start_frame"],
        f"start_frame={sim_state.get_state(obj)['start_frame']}",
    )

    # 走っていない状態の Rewind はシーンの先頭へ
    bpy.ops.muslin.stop_sim()
    scene.frame_set(23)
    bpy.ops.muslin.rewind()
    check(
        "停止中の Rewind はシーン先頭へ",
        scene.frame_current == scene.frame_start,
        f"frame {scene.frame_current}",
    )

    # メッシュが無ければ押せない
    bpy.context.view_layer.objects.active = None
    check("非メッシュでは Play が押せない", not bpy.ops.muslin.play.poll())
    check("非メッシュでは Rewind が押せない", not bpy.ops.muslin.rewind.poll())

    # ------------------------------------------------------------------
    section("Quality の段")

    from muslin.properties import QUALITY_PRESETS

    clear_scene()
    obj = make_grid("QualityGrid")
    props = obj.muslin

    check("既定は Normal", props.quality == 'NORMAL', props.quality)
    normal = QUALITY_PRESETS['NORMAL']
    check(
        "既定値が Normal の中身と一致する",
        (props.iterations, props.substeps, props.cache_broadphase)
        == (normal["iterations"], normal["substeps"], normal["cache"])
        and abs(props.chebyshev_radius - normal["chebyshev"]) < 1e-6,
        f"it={props.iterations} sub={props.substeps} "
        f"cheb={props.chebyshev_radius:.2f} cache={props.cache_broadphase}",
    )

    for name, want in QUALITY_PRESETS.items():
        props.quality = name
        got = (props.iterations, props.substeps, props.post_collision_iterations,
               props.cache_broadphase)
        expect = (want["iterations"], want["substeps"], want["post"], want["cache"])
        check(
            f"{name} が各値に反映される",
            got == expect and abs(props.chebyshev_radius - want["chebyshev"]) < 1e-6,
            f"{got} / cheb={props.chebyshev_radius:.2f}",
        )
        # 段を適用しただけで Custom に落ちてはいけない
        check(f"{name} を選んでも Custom に落ちない", props.quality == name,
              props.quality)

    # 段の間に意味のある差があること(同じ設定が 2 段あったら畳むべき)
    signatures = {
        name: (v["iterations"], v["substeps"], v["chebyshev"], v["post"])
        for name, v in QUALITY_PRESETS.items()
    }
    check("段どうしの中身が重複していない",
          len(set(signatures.values())) == len(signatures))
    # Substeps は段が上がるほど増える
    order = ['DRAFT', 'NORMAL', 'HIGH', 'FINAL']
    subs = [QUALITY_PRESETS[n]["substeps"] for n in order]
    check("段が上がるほど Substeps が増える", subs == sorted(subs), str(subs))
    # High 以上なら硬い生地の硬さが出る
    check(
        f"High 以上は Substeps {mesh_io.SUBSTEPS_FOR_STIFF_FABRIC} 以上",
        all(QUALITY_PRESETS[n]["substeps"] >= mesh_io.SUBSTEPS_FOR_STIFF_FABRIC
            for n in ('HIGH', 'FINAL')),
    )

    # 手で値を変えたら Custom に落ちる(段の表示と中身が食い違わないこと)
    props.quality = 'HIGH'
    props.substeps = QUALITY_PRESETS['HIGH']["substeps"] + 1
    check("手で変えると Custom に落ちる", props.quality == 'CUSTOM', props.quality)
    props.quality = 'NORMAL'
    check("段を選び直すと戻る", props.quality == 'NORMAL'
          and props.substeps == normal["substeps"],
          f"{props.quality} / sub={props.substeps}")

    # 硬い生地の警告が Quality と噛み合っているか
    props.bending_compliance = 0.001       # 硬い生地
    props.quality = 'NORMAL'
    check("Normal + 硬い生地では警告が出る",
          mesh_io.needs_more_substeps_for_bending(props))
    props.quality = 'HIGH'
    check("High にすると警告が消える",
          not mesh_io.needs_more_substeps_for_bending(props))

    # ------------------------------------------------------------------
    section("UI の作法")

    # 押せないボタンは、なぜ押せないのかをツールチップに出す(Blender 3.0+)。
    # poll が False を返しただけでは灰色になるだけで理由が出ない。
    clear_scene()
    ops_with_reason = [
        ("muslin.bake_to_shape_keys", "ベイク前の Convert to Shape Keys"),
        ("muslin.join_pieces", "選択が足りない Join Pattern Pieces"),
        ("muslin.add_seam", "オブジェクトモードでの Add Seam"),
        ("muslin.print_timings", "停止中の Print Timings"),
    ]
    for idname, label in ops_with_reason:
        group, name = idname.split(".")
        op = getattr(getattr(bpy.ops, group), name)
        # 設定された理由は Python 側から読めないので、ここでは
        # 「押せないこと」だけを見る。文言は下の ui_poll 直接呼び出しで確かめる。
        check(f"{label} は押せない", not op.poll())

    from muslin import ui_poll

    class _FakeOp:
        message = None

        @classmethod
        def poll_message_set(cls, text):
            cls.message = text

    class _Ctx:
        active_object = None

    _FakeOp.message = None
    ui_poll.mesh_selected(_FakeOp, _Ctx)
    check(
        "理由が日本語で設定される",
        _FakeOp.message == "オブジェクトが選択されていません",
        repr(_FakeOp.message),
    )

    obj = make_grid("UIGrid")
    _Ctx.active_object = obj
    _FakeOp.message = None
    ui_poll.in_edit_mode(_FakeOp, _Ctx)
    check(
        "編集モードでない理由が出る",
        _FakeOp.message is not None and "編集モード" in _FakeOp.message,
        repr(_FakeOp.message),
    )
    _FakeOp.message = None
    ui_poll.has_seams(_FakeOp, _Ctx)
    check(
        "縫い目が無い理由が出る",
        _FakeOp.message is not None and "縫い目" in _FakeOp.message,
        repr(_FakeOp.message),
    )
    # poll_message_set を持たない古い Blender でも落ちないこと
    class _Old:
        pass
    check("poll_message_set が無くても落ちない", ui_poll.mesh_selected(_Old, _Ctx) is True)

    # 物理量には単位を付ける。付いていれば Blender が "9.81 m/s2" と表示し、
    # "9.81" のような裸の数字にならない。
    rna = obj.muslin.bl_rna.properties
    tools_rna = bpy.types.Scene.bl_rna.properties["muslin_tools"].fixed_type.properties
    units = [
        (rna["gravity"], "ACCELERATION", "Gravity"),
        (rna["floor_z"], "LENGTH", "Floor Z"),
        (rna["collision_thickness"], "LENGTH", "Thickness"),
        (rna["self_collision_thickness"], "LENGTH", "Self Thickness"),
        (tools_rna["dt"], "TIME_ABSOLUTE", "Time Step"),
    ]
    for prop, want, label in units:
        check(f"{label} に単位 {want} が付いている", prop.unit == want, prop.unit)

    # ------------------------------------------------------------------
    section("パネルの描画")

    # パネルの draw() はヘッドレスでは呼ばれないので、プロパティ名を打ち
    # 間違えても GUI を開くまで気づけない。layout と同じ形をした偽物を
    # 渡して draw() を実際に走らせ、存在しない名前を使っていたら落とす。
    class _FakeLayout:
        """bpy の UILayout のうち、このアドオンが使う分だけを真似る。"""

        def __init__(self, log):
            self.log = log
            self.use_property_split = False
            self.use_property_decorate = True
            self.alert = False
            self.enabled = True

        def _child(self, *_args, **_kw):
            child = _FakeLayout(self.log)
            child.use_property_split = self.use_property_split
            child.use_property_decorate = self.use_property_decorate
            return child

        row = column = box = split = column_flow = grid_flow = _child

        def prop(self, data, name, **_kw):
            if name not in data.bl_rna.properties:
                raise AttributeError(
                    f"{data.bl_rna.identifier} に '{name}' というプロパティは無い"
                )
            self.log.append(("prop", data.bl_rna.identifier, name))

        def prop_search(self, data, name, search_data, search_prop, **_kw):
            self.prop(data, name)
            if search_prop not in search_data.bl_rna.properties:
                raise AttributeError(f"検索先に '{search_prop}' が無い")

        def operator(self, idname, **_kw):
            group, _, name = idname.partition(".")
            if not hasattr(getattr(bpy.ops, group, None), name):
                raise AttributeError(f"{idname} というオペレータは無い")
            self.log.append(("operator", idname))
            return _FakeLayout(self.log)

        def template_list(self, listtype, _id, data, propname, active_data,
                          active_propname, **_kw):
            if not hasattr(bpy.types, listtype):
                raise AttributeError(f"{listtype} という UIList は無い")
            self.prop(data, propname)
            self.prop(active_data, active_propname)

        def template_icon_view(self, data, propname, **_kw):
            self.prop(data, propname)

        def label(self, **_kw):
            pass

        def separator(self, **_kw):
            pass

    class _PanelShim:
        """パネルの draw() を bpy 抜きで呼ぶための代理。

        bpy.types.Panel は Python から直接インスタンス化できないので、
        メソッドだけクラスから借りて self の代わりを務める。
        """

        def __init__(self, cls, layout):
            object.__setattr__(self, "_cls", cls)
            object.__setattr__(self, "layout", layout)

        def __getattr__(self, name):
            cls = object.__getattribute__(self, "_cls")
            attr = getattr(cls, name)
            # staticmethod / classmethod は既に束ねられているので触らない。
            # MRO をたどって、定義そのものがどちらでもない場合だけ self を渡す。
            raw = next(
                (c.__dict__[name] for c in cls.__mro__ if name in c.__dict__), None
            )
            if inspect.isfunction(attr) and not isinstance(
                raw, (staticmethod, classmethod)
            ):
                return functools.partial(attr, self)
            return attr

    panel_classes = [
        getattr(bpy.types, n) for n in dir(bpy.types)
        if n.startswith("MUSLIN_PT_")
    ]
    check("パネルが登録されている", len(panel_classes) >= 9, f"{len(panel_classes)} 枚")

    clear_scene()
    obj = make_grid("PanelGrid")
    bpy.context.view_layer.objects.active = obj
    ctx = bpy.context

    # 縫い目とベイク済みキャッシュが無い分岐・ある分岐の両方を通す
    for label, prepare in (("素の状態", lambda: None),
                           ("縫い目あり", lambda: obj.muslin_seams.add())):
        prepare()
        for cls in panel_classes:
            log = []
            layout = _FakeLayout(log)
            try:
                _PanelShim(cls, layout).draw(ctx)
            except Exception as exc:
                check(f"{cls.__name__} を描ける({label})", False, f"{type(exc).__name__}: {exc}")
            else:
                check(f"{cls.__name__} を描ける({label})", True, f"{len(log)} 項目")

    # 設定パネルは純正と同じ 2 カラム配置にしてある
    settings = [c for c in panel_classes
                if c.__name__ in ("MUSLIN_PT_solver", "MUSLIN_PT_material",
                                  "MUSLIN_PT_forces", "MUSLIN_PT_collision",
                                  "MUSLIN_PT_pinning")]
    for cls in settings:
        layout = _FakeLayout([])
        _PanelShim(cls, layout).draw(ctx)
        check(f"{cls.__name__} が use_property_split を使う", layout.use_property_split)

    # メッシュが選ばれていないときは、プロパティを出さずに案内だけ出す
    bpy.context.view_layer.objects.active = None
    for cls in settings:
        log = []
        _PanelShim(cls, _FakeLayout(log)).draw(ctx)
        check(f"{cls.__name__} は非メッシュで何も出さない", log == [], str(log))

    section("片付け")
    muslin.unregister()
    check(
        "unregister でハンドラが外れる",
        sim_state._frame_change_handler not in bpy.app.handlers.frame_change_post
        and sim_state._load_post_handler not in bpy.app.handlers.load_post,
    )


if __name__ == "__main__":
    code = 0
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("\n!! 例外で中断しました", flush=True)
        code = 1

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n==== {len(results) - len(failed)}/{len(results)} passed ====", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), flush=True)
        code = 1
    sys.exit(code)

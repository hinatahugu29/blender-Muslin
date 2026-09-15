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
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

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
    from muslin import bake_ops, cache_io, mesh_io, sim_state

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

    bpy.ops.muslin.free_bake()
    check("Free Bake でキャッシュが消える",
          cache_io.cache_size_bytes(cache_dir) == 0)

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

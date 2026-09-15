"""Blender をヘッドレスで起動し、アドオン層(bpy 依存部分)を検証する。

使い方:
    blender --background --factory-startup --python scripts/blender_selftest.py

`scripts/verify_core.py` は物理コアと bpy 非依存モジュールを検証するが、
アドオン層(operators / sim_state / mesh_io / bake_ops / panels)は
構文チェックしかできない。ここはその穴を埋めるためのもの。

CHECKPOINTS.md の CP-B のうち、GUI 操作を伴わない項目を自動化している。
ビューポート描画(overlay)だけはヘッドレスで確認できないため対象外。
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

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

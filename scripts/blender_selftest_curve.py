"""Curve Pattern(addon/muslin/curve_pattern.py)の Blender 側の検証。

使い方:
    blender --background --factory-startup --python scripts/blender_selftest_curve.py

bpy 非依存の層(目印・離散化・Shape Update)は scripts/verify_core.py が見る。
ここは Curve から布のメッシュを作る層と、Blender 標準の編集操作(細分化・削除・反転など)を
通したときの構造の検出を、実際の Blender で確かめる。
"""

import sys
import traceback
from pathlib import Path

import bpy
import numpy as np

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "addon"))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""), flush=True)


def section(title):
    print(f"\n---- {title} ----", flush=True)


def make_curve(name="Pattern", w=1.0, h=0.7):
    """閉じた長方形の Bezier(直線。点は左下・右下・右上・左上の順)。"""
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '2D'
    sp = cu.splines.new('BEZIER')
    sp.bezier_points.add(3)
    for p, (x, y) in zip(sp.bezier_points, [(0, 0), (w, 0), (w, h), (0, h)]):
        p.co = (x, y, 0)
        p.handle_left_type = p.handle_right_type = 'VECTOR'
    sp.use_cyclic_u = True
    obj = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    for o in bpy.context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    return obj


def select_points(obj, indices):
    for sp in obj.data.splines:
        for i, p in enumerate(sp.bezier_points):
            v = i in indices
            p.select_control_point = p.select_left_handle = p.select_right_handle = v


def edit_op(obj, indices, fn):
    select_points(obj, indices)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        fn()
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')


def mesh_area(mesh):
    return sum(p.area for p in mesh.polygons)


def main():
    import muslin
    from muslin import curve_ids, curve_pattern as cp, curve_discretize, curve_update, mesh_io, sim_state

    muslin.register()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.muslin_tools.pattern_resolution = 0.02

    section("初期化")
    curve = make_curve()
    record = cp.initialize(curve, 0.02)
    pts = curve.data.splines[0].bezier_points
    uids = curve_ids.decode_all([p.radius for p in pts], [p.weight_softbody for p in pts])
    check("全ての点に目印が振られる", uids == [1, 2, 3, 4], str(uids))
    check("ピースが 1 つ記録される", len(record["pieces"]) == 1 and record["pieces"][0]["uids"] == [1, 2, 3, 4])
    check("記録は Curve データに JSON で残る", cp.load_record(curve) == record)
    try:
        cp.initialize(curve, 0.02)
        again = False
    except cp.CurvePatternError:
        again = True
    check("初期化済みの Curve は再初期化できない", again)
    check("構造は変わっていない", cp.structure_problems(curve) == [])

    section("Rebuild(布のメッシュの生成)")
    cloth, warnings = cp.rebuild(bpy.context, curve)
    mesh = cloth.data
    expected = curve_discretize.discretize(cp._outlines_for_generation(curve, cp.load_record(curve)), 0.02)
    check("布のメッシュができる", cloth.type == 'MESH' and len(mesh.vertices) == len(expected["positions"]),
          f"{len(mesh.vertices)} 頂点 / {len(mesh.polygons)} 面")
    check("面はすべて三角形", all(len(p.vertices) == 3 for p in mesh.polygons))
    check("面積の総和 = 型紙の面積", abs(mesh_area(mesh) - 0.7) < 1e-4, f"{mesh_area(mesh):.5f}")
    check("Curve と布が ID ポインタで結ばれる", cp.cloth_of(curve) == cloth and cp.curve_of(cloth) == curve)
    check("警告は無い", warnings == [], str(warnings))
    for name in (cp.ATTR_SEG, cp.ATTR_SEG_END, cp.ATTR_U, cp.ATTR_PIECE, cp.ATTR_PATTERN):
        check(f"頂点属性 {name} がある", mesh.attributes.get(name) is not None)
    from muslin import rest_shape
    check("型紙(muslin_pattern)と元の形が平らな型紙で入り、固定されている",
          rest_shape.has_pattern(cloth) and rest_shape.has_rest(cloth) and rest_shape.is_pattern_locked(cloth))
    ys = np.array([v.co.y for v in mesh.vertices])
    check("XZ 平面(Y = 0)に立っている", np.abs(ys).max() < 1e-6)
    check("面は手前(-Y)を向く", all(p.normal.y < -0.99 for p in mesh.polygons))

    section("派生データの復元(Shape Update の入力)")
    data = cp.reconstruct(cloth)
    same = all(np.allclose(data[k], expected[k], atol=1e-6) for k in ("positions", "u"))
    same = same and all(np.array_equal(data[k], expected[k]) for k in ("triangles", "boundary", "seg_start", "seg_end", "piece"))
    check("メッシュから離散化の結果を組み直せる", same)
    ring_ok = all(np.array_equal(a["vertices"], b["vertices"]) and np.allclose(a["s"], b["s"], atol=1e-6)
                  for a, b in zip(data["rings"], expected["rings"]))
    check("リング(境界頂点の並びと弧長座標)も一致する", ring_ok)

    section("縫い目の展開")
    # 右辺(点 2 → 3)と左辺(点 4 → 1)を縫う
    uid = cp.add_seam(curve, [(2, 0.0, 3, 0.0)], [(4, 0.0, 1, 0.0)], name="Side")
    cloth, warnings = cp.rebuild(bpy.context, curve)
    check("縫い目が布に登録される", len(cloth.muslin_seams) == 1 and cloth.muslin_seams[0].uid == uid)
    codes, edges = mesh_io.read_seam_codes(cloth.data)
    codes = np.asarray(codes)
    n_a = int((codes == curve_seam_code(uid, 0)).sum())
    n_b = int((codes == curve_seam_code(uid, 1)).sum())
    check("辺の属性に両側が書かれる(右辺 35 本・左辺 35 本)", (n_a, n_b) == (35, 35), f"{n_a}/{n_b}")
    broken = []
    pairs = mesh_io.build_seam_pairs(cloth, report=broken)
    check("既存の縫い目処理でペアにできる(壊れていない)", pairs and not broken, f"{len(pairs)} ペア")
    xs = np.array([v.co.x for v in cloth.data.vertices])
    zs = np.array([v.co.z for v in cloth.data.vertices])
    on_sides = {v for a, b in pairs for v in (a, b)}
    check("縫い合わせる頂点は左右の辺の上にある",
          all(abs(xs[v]) < 1e-6 or abs(xs[v] - 1.0) < 1e-6 for v in on_sides))
    try:
        cp.add_seam(curve, [(99, 0.0, 3, 0.0)], [(4, 0.0, 1, 0.0)])
        rejected = False
    except cp.CurvePatternError:
        rejected = True
    check("存在しない点を指す縫い目は登録できない", rejected)

    section("Shape Update(点を動かす)")
    pts = curve.data.splines[0].bezier_points
    pts[1].co.x = 1.05
    pts[2].co.x = 1.05
    for p in (pts[1], pts[2]):
        p.handle_left = p.co
        p.handle_right = p.co
    check("点を動かしても構造は同じ", cp.structure_problems(curve) == [])
    data = cp.reconstruct(cloth)
    result = curve_update.update(data, cp.current_outlines(curve), 0.02)
    check("幅 1.00 → 1.05m は Shape Update", result["status"] == curve_update.SHAPE_UPDATE, str(result["reasons"]))
    check("頂点数は変わらず、新しい幅に追従する",
          len(result["positions"]) == len(cloth.data.vertices) and abs(result["positions"][:, 0].max() - 1.05) < 1e-6)

    section("Blender 標準の編集操作を通した構造の検出")
    def fresh_pattern():
        c = make_curve("P")
        cp.initialize(c, 0.02)
        return c

    c = fresh_pattern()
    edit_op(c, {0, 1, 2, 3}, lambda: bpy.ops.curve.subdivide(number_cuts=1))
    reasons = cp.structure_problems(c)
    check("細分化: 新しい点として検出される", any("新しい点" in r for r in reasons), str(reasons))
    check("細分化: 元の点は目印が残る(uid 1〜4 が残っている)",
          {1, 2, 3, 4} <= {u for u in cp.read_splines(c)[0]["uids"] if u is not None})

    c = fresh_pattern()
    edit_op(c, {1}, lambda: bpy.ops.curve.delete(type='VERT'))
    reasons = cp.structure_problems(c)
    check("点の削除: 無くなった点として検出される", any("無くなりました" in r for r in reasons), str(reasons))

    c = fresh_pattern()
    edit_op(c, {2}, lambda: bpy.ops.curve.extrude(mode='INIT'))
    reasons = cp.structure_problems(c)
    check("押し出し: 複製として検出される", any("複製" in r for r in reasons), str(reasons))

    c = fresh_pattern()
    edit_op(c, {0, 1, 2, 3}, lambda: bpy.ops.curve.switch_direction())
    check("向きの反転は構造の変化にならない", cp.structure_problems(c) == [])
    check("反転しても uid の並びは逆順で残る", cp.read_splines(c)[0]["uids"] == [4, 3, 2, 1])
    m = cp.reconstruct(cp.rebuild(bpy.context, c)[0])
    edit_op(c, {0, 1, 2, 3}, lambda: bpy.ops.curve.switch_direction())
    r = curve_update.update(m, cp.current_outlines(c), 0.02)
    check("再反転しても同じ形に追従する(uid で対応するので)",
          r["status"] == curve_update.SHAPE_UPDATE and np.abs(r["positions"] - m["positions"]).max() < 1e-6)

    c = fresh_pattern()
    edit_op(c, {0, 1, 2, 3}, lambda: bpy.ops.curve.cyclic_toggle())
    check("閉じた輪郭を開くと構造の変化", any("開閉" in r for r in cp.structure_problems(c)))

    section("Rebuild で縫い目の意味が保たれる")
    c = make_curve("Q")
    cp.initialize(c, 0.02)
    uid = cp.add_seam(c, [(2, 0.0, 3, 0.0)], [(4, 0.0, 1, 0.0)], name="Side")
    cloth_q, _ = cp.rebuild(bpy.context, c)
    before = len(cloth_q.data.vertices)
    # 右辺の区間(点 2 → 3)を細分化して点を足す
    edit_op(c, {1, 2}, lambda: bpy.ops.curve.subdivide(number_cuts=1))
    check("区間の途中に点を足すと Rebuild Required", cp.structure_problems(c) != [])
    cloth_q2, warnings = cp.rebuild(bpy.context, c)
    check("Rebuild は同じ布のオブジェクトを使い回す", cloth_q2 == cloth_q)
    check("Rebuild 後は構造の変化が無くなる", cp.structure_problems(c) == [], str(cp.structure_problems(c)))
    codes, edges = mesh_io.read_seam_codes(cloth_q2.data)
    codes = np.asarray(codes)
    n_a = int((codes == curve_seam_code(uid, 0)).sum())
    n_b = int((codes == curve_seam_code(uid, 1)).sum())
    # 右辺は 2 区間に分かれ、それぞれの分割数の丸めで本数が 1 本前後変わる。範囲(右辺の全体)は同じ
    edge_vs = [tuple(e.vertices) for e in cloth_q2.data.edges]
    side_a = sorted({v for c, e in zip(codes, edge_vs) if c == curve_seam_code(uid, 0) for v in e})
    co = cloth_q2.data.vertices
    zs_a = [co[v].co.z for v in side_a]
    check("縫い目 A は右辺の全体(x = 1、z が 0〜0.7)のまま",
          all(abs(co[v].co.x - 1.0) < 1e-6 for v in side_a)
          and abs(min(zs_a)) < 1e-6 and abs(max(zs_a) - 0.7) < 1e-6 and 34 <= n_a <= 36, f"{n_a} 本")
    check("縫い目 B(左辺)は変わらない", n_b == 35, f"{n_b} 本")
    broken = []
    check("縫い目は壊れていない", mesh_io.build_seam_pairs(cloth_q2, report=broken) and not broken)
    check("世代番号が進む", cp.load_record(c)["gen_id"] == 2)

    section("走行中は Rebuild しない")
    c = make_curve("R")
    cp.initialize(c, 0.05)
    cloth_r, _ = cp.rebuild(bpy.context, c)
    sim_state.start_simulation(cloth_r, cloth_r.muslin)
    try:
        cp.rebuild(bpy.context, c)
        refused = False
    except cp.CurvePatternError as exc:
        refused = "シミュレーション中" in str(exc)
    sim_state.stop_simulation(cloth_r)
    check("シミュレーション中の Rebuild は拒否される", refused)
    cloth_r2, _ = cp.rebuild(bpy.context, c)
    check("停止すれば Rebuild できる", cloth_r2 == cloth_r)

    section("保存して開き直す")
    import tempfile
    path = str(Path(tempfile.mkdtemp(prefix="muslin_curve_")) / "curve.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    q = bpy.data.objects["Q"]
    check("記録と ID ポインタが残る", cp.load_record(q) is not None and cp.cloth_of(q) is not None
          and cp.curve_of(cp.cloth_of(q)) == q)
    check("開き直しても構造は同じ", cp.structure_problems(q) == [])
    check("開き直しても派生データを組み直せる", len(cp.reconstruct(cp.cloth_of(q))["positions"]) > 0)

    muslin.unregister()


def curve_seam_code(uid, side):
    from muslin import seams
    return seams.seam_code(uid, side)


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

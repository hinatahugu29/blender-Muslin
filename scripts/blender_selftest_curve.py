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


class _FakeLayout:
    """bpy の UILayout のうち、Curve Pattern のパネルが使う分だけを真似る(存在しない名前で落とす)。"""

    def __init__(self, log):
        self.log = log

    def _child(self, *_a, **_k):
        return _FakeLayout(self.log)

    row = column = box = split = _child

    def prop(self, data, name, **_k):
        if name not in data.bl_rna.properties:
            raise AttributeError(f"{data.bl_rna.identifier} に '{name}' が無い")
        self.log.append(("prop", name))

    def operator(self, idname, **_k):
        group, _, name = idname.partition(".")
        if not hasattr(getattr(bpy.ops, group, None), name):
            raise AttributeError(f"{idname} というオペレータは無い")
        self.log.append(("operator", idname))
        return _FakeLayout(self.log)

    def label(self, **k):
        self.log.append(("label", k.get("text", "")))

    def separator(self, **_k):
        pass


class _PanelShim:
    """パネルの draw() を bpy 抜きで呼ぶための代理。"""

    def __init__(self, cls, layout):
        self._cls = cls
        self.layout = layout

    def draw(self, context):
        return self._cls.draw(self, context)


def draw_panel(context):
    log = []
    _PanelShim(bpy.types.MUSLIN_PT_curve_pattern, _FakeLayout(log)).draw(context)
    return log


def evaluated_stats(obj):
    """モディファイア適用後のメッシュの (頂点数, 外周の辺の数)。"""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    mesh = ev.to_mesh()
    try:
        loops = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("edge_index", loops)
        from muslin import rest_shape as _rs
        return len(mesh.vertices), len(_rs.outline_edges(len(mesh.edges), loops))
    finally:
        ev.to_mesh_clear()


def main():
    import muslin
    from muslin import rest_shape
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
    zs = np.array([v.co.z for v in mesh.vertices])
    check("既定は描いた平面のまま(Z = 0)", np.abs(zs).max() < 1e-6 and cp.orientation_of(cp.load_record(curve)) == 'FLAT')
    check("面は上(+Z)を向く", all(p.normal.z > 0.99 for p in mesh.polygons))

    def world_box(o, xyz):
        bpy.context.view_layer.update()      # location を変えた直後は matrix_world が古い
        mw = np.array(o.matrix_world, dtype=np.float64)
        w = np.asarray(xyz, dtype=np.float64) @ mw[:3, :3].T + mw[:3, 3]
        return w.min(axis=0), w.max(axis=0)

    curve_pts = np.array([tuple(pt.co) for sp in curve.data.splines for pt in sp.bezier_points])
    c_lo, c_hi = world_box(curve, curve_pts)
    m_lo, m_hi = world_box(cloth, [tuple(v.co) for v in mesh.vertices])
    check("布は Curve の輪郭の +X 側に、既定の隙間(0.1m)を空けて置かれる",
          abs((m_lo[0] - c_hi[0]) - cp.DEFAULT_GAP) < 1e-4 and abs(m_lo[1] - c_lo[1]) < 1e-4,
          f"隙間 {m_lo[0] - c_hi[0]:.4f}m")

    section("向きと置き場所を選んで作る")
    for side, axis, sign in (('-Y', 1, -1.0), ('+X', 0, 1.0)):
        cs = make_curve(f"Place{side}", 1.0, 0.7)
        cs.location = (3.0, 2.0, 0.5)
        bpy.context.view_layer.update()
        cp.initialize(cs, 0.05)
        orient = 'STANDING' if side == '+X' else 'FLAT'
        cloth_s, _ = cp.rebuild(bpy.context, cs, orientation=orient, side=side, gap=0.2)
        pts_s = np.array([tuple(pt.co) for sp in cs.data.splines for pt in sp.bezier_points])
        c_lo, c_hi = world_box(cs, pts_s)
        m_lo, m_hi = world_box(cloth_s, [tuple(v.co) for v in cloth_s.data.vertices])
        gap = (c_lo[axis] - m_hi[axis]) if sign < 0 else (m_lo[axis] - c_hi[axis])
        check(f"{side} 側に 0.2m の隙間で置かれる({orient})", abs(gap - 0.2) < 1e-4, f"{gap:.4f}m")
        check(f"向き {orient} が記録に残る", cp.orientation_of(cp.load_record(cs)) == orient)
    # オペレータから(ダイアログ・F9 と同じ経路)
    co = make_curve("PlaceOp", 0.5, 0.5)
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = co
    cp.initialize(co, 0.05)
    res = bpy.ops.muslin.curve_pattern_rebuild(orientation='STANDING', side='-X', gap=0.3)
    cloth_o = cp.cloth_of(co)
    pts_o = np.array([tuple(pt.co) for sp in co.data.splines for pt in sp.bezier_points])
    c_lo, c_hi = world_box(co, pts_o)
    m_lo, m_hi = world_box(cloth_o, [tuple(v.co) for v in cloth_o.data.vertices])
    check("Rebuild の操作で向き・方向・隙間を選べる",
          res == {'FINISHED'} and cp.orientation_of(cp.load_record(co)) == 'STANDING'
          and abs((c_lo[0] - m_hi[0]) - 0.3) < 1e-4, f"隙間 {c_lo[0] - m_hi[0]:.4f}m")

    ys = np.array([v.co.y for v in cloth_s.data.vertices])
    check("STANDING は XZ 平面(Y = 0)に立ち、面は手前(-Y)を向く",
          np.abs(ys).max() < 1e-6 and all(p.normal.y < -0.99 for p in cloth_s.data.polygons))
    before = cloth_s.matrix_world.copy()
    cloth_s.location.x += 1.0
    bpy.context.view_layer.update()
    moved = cloth_s.matrix_world.copy()
    cloth_s2, _ = cp.rebuild(bpy.context, cs, orientation='FLAT', side='-X', gap=1.0)
    check("すでに布があれば、Rebuild しても位置と向きは変わらない",
          cloth_s2 == cloth_s and cloth_s.matrix_world == moved and moved != before
          and cp.orientation_of(cp.load_record(cs)) == 'STANDING'
          and np.abs([v.co.y for v in cloth_s.data.vertices]).max() < 1e-6)

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
    ys_a = [co[v].co.y for v in side_a]
    check("縫い目 A は右辺の全体(x = 1、y が 0〜0.7)のまま",
          all(abs(co[v].co.x - 1.0) < 1e-6 for v in side_a)
          and abs(min(ys_a)) < 1e-6 and abs(max(ys_a) - 0.7) < 1e-6 and 34 <= n_a <= 36, f"{n_a} 本")
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

    section("走っている布へ Shape Update を流す(pattern_link)")
    from muslin import pattern_link
    c = make_curve("S")
    cp.initialize(c, 0.05)
    cloth_s, _ = cp.rebuild(bpy.context, c)
    check("布は Curve に結び付いている", pattern_link.linked_curve(cloth_s) == c)
    sim_state.start_simulation(cloth_s, cloth_s.muslin)
    state = sim_state.get_state(cloth_s)
    n_vertices = len(cloth_s.data.vertices)

    def span(state):
        ref = np.array(state["reference"]).reshape(-1, 3)
        return float(np.ptp(ref[:, 0]))

    check("開始時の寸法の基準は Curve のとおり(幅 1.00m)", abs(span(state) - 1.0) < 1e-6, f"{span(state):.4f}")
    check("変わっていなければ反映しない", sim_state.poll_pattern(state) is False)
    pts = c.data.splines[0].bezier_points
    for i in (1, 2):
        pts[i].co.x = 1.05
        pts[i].handle_left = pts[i].handle_right = pts[i].co
    check("Curve を動かすと走っている布へ流れる", sim_state.poll_pattern(state) is True)
    check("寸法の基準が新しい幅(1.05m)になる", abs(span(state) - 1.05) < 1e-6, f"{span(state):.4f}")
    check("頂点数は変わらない", len(state["reference"]) == n_vertices * 3)
    check("警告は出ない", state["pattern_warnings"] == [])
    check("同じ状態を続けて呼んでも再反映しない", sim_state.poll_pattern(state) is False)

    edit_op(c, {0, 1, 2, 3}, lambda: bpy.ops.curve.subdivide(number_cuts=1))
    check("点を足すと反映せず Rebuild Required を返す", sim_state.poll_pattern(state) is False)
    check("理由が保持される(パネルに出る)",
          any("Rebuild Required" in w for w in state["pattern_warnings"]), str(state["pattern_warnings"]))
    check("寸法の基準は最後に成功した値のまま", abs(span(state) - 1.05) < 1e-6)
    check("その間の指紋は理由の文字列", isinstance(pattern_link.signature(cloth_s), str))
    try:
        cp.rebuild(bpy.context, c)
        refused = False
    except cp.CurvePatternError:
        refused = True
    check("走行中は Rebuild Required でも Rebuild しない", refused)
    sim_state.stop_simulation(cloth_s)
    cloth_s2, _ = cp.rebuild(bpy.context, c)
    check("停止して Rebuild すると Rebuild Required が消える", pattern_link.read(cloth_s2)[1] is None
          and cp.structure_problems(c) == [])
    sim_state.start_simulation(cloth_s2, cloth_s2.muslin)
    state2 = sim_state.get_state(cloth_s2)
    check("Rebuild 後の布は新しい寸法(幅 1.05m)で始まる", abs(span(state2) - 1.05) < 1e-6, f"{span(state2):.4f}")
    sim_state.stop_simulation(cloth_s2)

    section("縫い目を選択から作る")
    c = make_curve("U")
    cp.initialize(c, 0.02)
    cloth_u, _ = cp.rebuild(bpy.context, c)
    # 右辺(点 2・3)と左辺(点 4・1)を選ぶ。点の index は 1・2 と 3・0
    select_points(c, {1, 2, 3, 0})
    check("全部が隣り合っていれば連なりは 1 本", len(cp.selection_runs_of(c)) == 1)
    select_points(c, {1, 2})
    check("選んだのが 1 か所だけなら縫い目を作れない", _raises(lambda: cp.seam_from_selection(c)))
    # 点が 4 つの正方形では、離れた 2 辺は「対辺の 2 点ずつ」= (1,2) と (3,0)。隣り合うので 1 本になる。
    # 離れた連なりを作るために、点を足した輪郭で試す
    c2 = make_curve("V", 1.0, 1.0)
    edit_op(c2, {0, 1, 2, 3}, lambda: bpy.ops.curve.subdivide(number_cuts=1))
    cp.initialize(c2, 0.05)
    # 細分化後は 8 点(0,1(中),2,3(中),4,5(中),6,7(中))。1 辺ずつ(0,1,2)と(4,5,6)を選ぶ
    for sp in c2.data.splines:
        for i, p in enumerate(sp.bezier_points):
            p.select_control_point = i in (0, 1, 2, 4, 5, 6)
    cloth_v, _ = cp.rebuild(bpy.context, c2)
    uid = cp.seam_from_selection(c2, "Pair")
    check("離れた 2 か所を選べば縫い目ができる", uid > 0 and len(cp.load_record(c2)["seams"]) == 1)
    check("布に縫い目が反映される(Rebuild なしで)",
          len(cloth_v.muslin_seams) == 1 and cloth_v.muslin_seams[0].uid == uid)
    broken = []
    pairs = mesh_io.build_seam_pairs(cloth_v, report=broken)
    check("既存の縫い目処理でペアにできる", bool(pairs) and not broken, f"{len(pairs)} ペア")
    cp.remove_seam(c2, uid)
    check("縫い目を削除すると布からも消える", len(cloth_v.muslin_seams) == 0
          and not any(mesh_io.read_seam_codes(cloth_v.data)[0]))

    section("パネルの描画(Curve Pattern)")
    bpy.context.view_layer.objects.active = c2
    log = draw_panel(bpy.context)
    check("Curve を選んだときのパネルを描ける", any(x[0] == "operator" for x in log), f"{len(log)} 項目")
    check("初期化済みなら Rebuild ボタンが出る", ("operator", "muslin.curve_pattern_rebuild") in log)
    check("Curve では縫い目の追加ボタンが出る", ("operator", "muslin.curve_seam_add") in log)
    fresh = make_curve("W")
    bpy.context.view_layer.objects.active = fresh
    log = draw_panel(bpy.context)
    check("未初期化なら Initialize ボタンが出る", ("operator", "muslin.curve_pattern_init") in log)
    bpy.context.view_layer.objects.active = cloth_v
    log = draw_panel(bpy.context)
    check("布を選んだときは元の Curve を示し、縫い目の追加は出さない",
          any(x[0] == "label" and "型紙" in x[1] for x in log) and ("operator", "muslin.curve_seam_add") not in log)
    edit_op(c2, {0}, lambda: bpy.ops.curve.delete(type='VERT'))
    bpy.context.view_layer.objects.active = c2
    log = draw_panel(bpy.context)
    check("構造が変わると Rebuild Required を表示する", ("label", "Rebuild Required") in log)
    from muslin import pattern_link
    check("布から元の Curve に辿れる", pattern_link.linked_curve(cloth_v) == c2)

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

    section("Curve Pattern の布を着せる(Dress)")
    import math
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_set(1)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.06, depth=1.2, location=(0.0, 0.0, 0.0))
    body = bpy.context.active_object
    body.name = "Body"
    dc = make_curve("Shirt", 0.5, 0.6)
    cp.initialize(dc, 0.03)
    # この節は着せ付けを見るので、体の前に立てた向きで作る(円柱に巻くのが z を高さとして扱う)
    dc_cloth, _ = cp.rebuild(bpy.context, dc, orientation='STANDING')
    cp.add_seam(dc, [(2, 0.0, 3, 0.0)], [(4, 0.0, 1, 0.0)], name="Side")
    cp.apply_seams(dc)
    dc_cloth.location = (0.0, 0.0, 0.0)
    dc_cloth.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    top = max(v.co.z for v in dc_cloth.data.vertices)
    group = dc_cloth.vertex_groups.new(name="Pin")
    group.add([v.index for v in dc_cloth.data.vertices
               if v.co.z > top - 1e-5 and abs(v.co.x - 0.25) < 0.1], 1.0, "REPLACE")
    width = 0.5
    radius = width / (1.5 * math.pi)
    for v in dc_cloth.data.vertices:
        theta = (v.co.x - 0.25) / radius
        v.co.x, v.co.y = radius * math.sin(theta), -radius * math.cos(theta)
    dc_cloth.data.update()
    props = dc_cloth.muslin
    props.pin_vertex_group = "Pin"
    props.collider_object = body
    props.collision_enabled = True
    props.self_collision_enabled = False
    props.seam_close_frames = 20
    for o in bpy.context.scene.objects:
        o.select_set(o is dc_cloth)
    bpy.context.view_layer.objects.active = dc_cloth

    def seam_gap(o):
        pos = mesh_io.get_world_positions(o)
        gaps = [math.dist(pos[a * 3:a * 3 + 3], pos[b * 3:b * 3 + 3])
                for a, b in mesh_io.build_seam_pairs(o)]
        return max(gaps) if gaps else float("inf")

    open_gap = seam_gap(dc_cloth)
    check("曲げて置いた布は縫い目が開いている", open_gap > 0.05, f"{open_gap * 100:.1f}cm")
    check("曲げても型紙は平らなまま(Curve から作った型紙が固定されている)",
          rest_shape.is_pattern_locked(dc_cloth) and
          np.ptp(rest_shape.load_pattern(dc_cloth).reshape(-1, 3)[:, 1]) < 1e-6)
    res = bpy.ops.muslin.dress()
    check("Dress が通る", res == {'FINISHED'}, str(res))
    check("着せた段階になる", rest_shape.is_dressed(dc_cloth))
    closed = seam_gap(dc_cloth)
    check("縫い目が閉じる(Curve の縫い目)", closed < 0.002, f"{closed * 1000:.2f}mm")
    verts = np.array([tuple(v.co) for v in dc_cloth.data.vertices])
    check("計算結果は有限", np.isfinite(verts).all())

    # 着せた後でも、型紙(Curve)を変えれば走っている布の寸法が変わる
    sim_state.start_simulation(dc_cloth, dc_cloth.muslin)
    st = sim_state.get_state(dc_cloth)
    ref = np.array(st["reference"]).reshape(-1, 3)
    w0 = float(np.ptp(ref[:, 0]))
    pose_before = list(st["sim"].get_positions())
    pts = dc.data.splines[0].bezier_points
    for i in (1, 2):
        pts[i].co.x = 0.55
        pts[i].handle_left = pts[i].handle_right = pts[i].co
    check("着せた後に Curve を広げると寸法の基準が変わる", sim_state.poll_pattern(st) is True)
    ref = np.array(st["reference"]).reshape(-1, 3)
    check("型紙の幅が 0.50 → 0.55m", abs(float(np.ptp(ref[:, 0])) - 0.55) < 1e-6 and abs(w0 - 0.5) < 1e-6,
          f"{w0:.3f} → {float(np.ptp(ref[:, 0])):.3f}")
    check("着せた姿勢(頂点)は保たれる(Shape Update は現在姿勢を維持する)",
          _maxdiff(st["sim"].get_positions(), pose_before) == 0.0)
    sim_state.stop_simulation(dc_cloth)


    section("Rebuild で着せた姿勢を引き継ぐ")
    bpy.context.view_layer.objects.active = dc_cloth
    old_count = len(dc_cloth.data.vertices)
    old_xyz = np.array([tuple(v.co) for v in dc_cloth.data.vertices])
    old_radial = np.hypot(old_xyz[:, 0], old_xyz[:, 1])
    check("Rebuild 前は着せた姿勢(曲がっている)", np.ptp(old_xyz[:, 1]) > 0.05 and rest_shape.is_dressed(dc_cloth))
    # 目標の辺を細かくして頂点数を変える。点も足す(区間の途中)
    edit_op(dc, {1, 2}, lambda: bpy.ops.curve.subdivide(number_cuts=1))
    check("Rebuild Required の状態(点が足された)", cp.structure_problems(dc) != [])
    cloth_k, warns = cp.rebuild(bpy.context, dc, target_length=0.02)
    check("同じ布のオブジェクトを使い回す", cloth_k == dc_cloth)
    new_xyz = np.array([tuple(v.co) for v in cloth_k.data.vertices])
    check("頂点数が変わる(0.03 → 0.02)", len(new_xyz) > old_count, f"{old_count} → {len(new_xyz)}")
    check("姿勢は引き継がれる(曲がったまま)", np.ptp(new_xyz[:, 1]) > 0.05 and np.isfinite(new_xyz).all())
    new_radial = np.hypot(new_xyz[:, 0], new_xyz[:, 1])
    check("円柱からの距離の範囲が保たれる(旧 ± 3mm)",
          new_radial.min() > old_radial.min() - 0.003 and new_radial.max() < old_radial.max() + 0.003,
          f"{old_radial.min():.3f}〜{old_radial.max():.3f} → {new_radial.min():.3f}〜{new_radial.max():.3f}")
    check("着せた段階のまま", rest_shape.is_dressed(cloth_k))
    check("元の形(rest)も引き継いだ姿勢",
          _maxdiff(rest_shape.load(cloth_k), new_xyz.ravel()) < 1e-5 and rest_shape.has_pattern(cloth_k))
    check("型紙は平らな新しい型紙", np.ptp(rest_shape.load_pattern(cloth_k).reshape(-1, 3)[:, 1]) < 1e-6)
    check("縫い目は(閉じたまま)壊れていない", seam_gap(cloth_k) < 0.005, f"{seam_gap(cloth_k) * 1000:.2f}mm")
    check("近似の警告は出ない", not any("近似" in w for w in warns), str(warns))
    check("ピン留め(頂点グループ)も引き継がれ、失われた警告は出ない",
          "Pin" in cloth_k.vertex_groups and not any("ピン留め" in w for w in warns), str(warns))
    pinned = [v.index for v in cloth_k.data.vertices if any(g.weight >= 0.5 for g in v.groups)]
    top_z = max(v.co.z for v in cloth_k.data.vertices)
    check("ピンは上端に残る(新しい頂点のうち、旧と同じ場所の頂点)",
          len(pinned) > 0 and all(cloth_k.data.vertices[i].co.z > top_z - 0.02 for i in pinned),
          f"{len(pinned)} 頂点")
    sim_state.start_simulation(cloth_k, cloth_k.muslin)
    st_k = sim_state.get_state(cloth_k)
    start_pos = np.array(st_k["sim"].get_positions()).reshape(-1, 3)
    for f in range(2, 8):
        scene.frame_set(f)
    end_pos = np.array(sim_state.get_state(cloth_k)["sim"].get_positions()).reshape(-1, 3)
    # 型紙は 0.50 → 0.55m に広げてあるので、着せた姿勢より布に余りがあって少し落ち着き直す
    check("引き継いだ姿勢から再生しても崩れない(6 フレームで最大移動 10cm 未満・有限)",
          np.isfinite(end_pos).all() and np.abs(end_pos - start_pos).max() < 0.1,
          f"{np.abs(end_pos - start_pos).max() * 1000:.2f}mm")
    sim_state.stop_simulation(cloth_k)
    scene.frame_set(1)

    flat_cloth, _ = cp.rebuild(bpy.context, dc, keep_pose=False)
    flat_xyz = np.array([tuple(v.co) for v in flat_cloth.data.vertices])
    check("Keep Pose を切ると平らな型紙から作り直す", np.ptp(flat_xyz[:, 1]) < 1e-6
          and not rest_shape.is_dressed(flat_cloth))

    section("Curve Pattern の布に UV を作る(Pattern to UV)")
    bpy.context.view_layer.objects.active = dc_cloth
    for o in bpy.context.scene.objects:
        o.select_set(o is dc_cloth)
    res = bpy.ops.muslin.pattern_to_uv()
    check("Pattern to UV が通る", res == {'FINISHED'}, str(res))
    uv = dc_cloth.data.uv_layers.active
    uvs = np.array([tuple(d.uv) for d in uv.data]) if uv is not None else np.zeros((0, 2))
    check("UV は 0〜1 に収まり、面ごとに揃っている",
          len(uvs) == len(dc_cloth.data.loops) and uvs.min() >= -1e-6 and uvs.max() <= 1 + 1e-6)

    section("ピースを体の周りに置く(Arrange Around Collider)")
    import math
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_set(1)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.1, depth=1.2, location=(0.0, 0.0, 0.0))
    body2 = bpy.context.active_object
    body2.name = "Body2"

    # 前身頃と後身頃(XY 平面に並べた 2 枚。幅 0.3m・高さ 0.5m)
    cu2 = bpy.data.curves.new("Pair", 'CURVE')
    cu2.dimensions = '2D'
    for x0 in (0.0, 1.0):
        sp = cu2.splines.new('BEZIER')
        sp.bezier_points.add(3)
        for pt, (x, y) in zip(sp.bezier_points, [(x0, 0.0), (x0 + 0.3, 0.0), (x0 + 0.3, 0.5), (x0, 0.5)]):
            pt.co = (x, y, 0.0)
            pt.handle_left_type = pt.handle_right_type = 'VECTOR'
        sp.use_cyclic_u = True
    pair = bpy.data.objects.new("Pair", cu2)
    scene.collection.objects.link(pair)
    bpy.context.view_layer.objects.active = pair
    cp.initialize(pair, 0.03)
    pair_cloth, _ = cp.rebuild(bpy.context, pair)
    check("2 枚のピースができる", len(cp.load_record(pair)["pieces"]) == 2)
    # 点: 前身頃 1〜4(左下・右下・右上・左上)、後身頃 5〜8。+X 側で前の右辺と後ろの左辺、-X 側で前の左辺と後ろの右辺を縫う
    cp.add_seam(pair, [(2, 0.0, 3, 0.0)], [(5 + 3, 0.0, 5, 0.0)], name="Right")
    cp.add_seam(pair, [(4, 0.0, 1, 0.0)], [(6, 0.0, 7, 0.0)], name="Left")
    cp.apply_seams(pair)
    pair_cloth.location = (0.0, 0.0, 0.0)     # 型紙の高さ 0〜0.5m が、体(高さ -0.6〜0.6m)の中に収まる
    pair_cloth.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    props2 = pair_cloth.muslin
    props2.collider_object = body2
    props2.collision_enabled = True
    props2.self_collision_enabled = False
    props2.seam_close_frames = 20

    check("コライダーが無ければ配置の操作は使えない(poll)",
          (setattr(props2, "collider_object", None), bpy.ops.muslin.curve_arrange.poll())[1] is False)
    props2.collider_object = body2
    for o in scene.objects:
        o.select_set(o is pair_cloth)
    bpy.context.view_layer.objects.active = pair_cloth
    check("コライダーを指定すれば使える", bpy.ops.muslin.curve_arrange.poll())
    res = bpy.ops.muslin.curve_arrange()
    check("Arrange が通る", res == {'FINISHED'}, str(res))
    xyz = np.array([tuple(v.co) for v in pair_cloth.data.vertices])
    piece_ids = np.array(list(pair_cloth.data.attributes[cp.ATTR_PIECE].data[i].value
                              for i in range(len(xyz))))
    radial = np.hypot(xyz[:, 0], xyz[:, 1])
    check("すべての頂点が体の外側にある(半径 0.1m より外)", radial.min() > 0.1 + 1e-6 and np.isfinite(xyz).all(),
          f"最小 {radial.min():.4f}m")
    first, second = sorted(set(piece_ids))
    check("1 枚目は正面(-Y)、2 枚目は背面(+Y)",
          xyz[piece_ids == first, 1].mean() < -0.05 and xyz[piece_ids == second, 1].mean() > 0.05)
    check("高さは型紙のとおり(0〜0.5m)",
          abs(xyz[:, 2].min() - 0.0) < 1e-4 and abs(xyz[:, 2].max() - 0.5) < 1e-4)
    check("置いた形が元の形(rest)になり、着せた段階は解除される",
          _maxdiff(rest_shape.load(pair_cloth), xyz.ravel()) < 1e-5 and not rest_shape.is_dressed(pair_cloth))
    check("型紙(寸法の基準)は平らなまま(描いた平面 = Z が一定)",
          np.ptp(rest_shape.load_pattern(pair_cloth).reshape(-1, 3)[:, 2]) < 1e-6)
    check("平らに出した布でも、立てて体の周りへ置ける(高さに幅がある)", np.ptp(xyz[:, 2]) > 0.49)

    # 高さを指定すると、型紙の下端がその高さに来る(F9 で変える Height)
    res = bpy.ops.muslin.curve_arrange(height=0.1)
    xyz_h = np.array([tuple(v.co) for v in pair_cloth.data.vertices])
    check("Height を指定すると下端がその高さ(0.1〜0.6m)",
          res == {'FINISHED'} and abs(xyz_h[:, 2].min() - 0.1) < 1e-4 and abs(xyz_h[:, 2].max() - 0.6) < 1e-4,
          f"{xyz_h[:, 2].min():.4f}〜{xyz_h[:, 2].max():.4f}")
    bpy.ops.muslin.curve_arrange()            # 既定(布の原点の高さ)に戻して先へ進む

    # 上端の中央を留めて着せる。2 本の縫い目が閉じる
    top2 = xyz[:, 2].max()
    grp = pair_cloth.vertex_groups.new(name="Pin")
    pins = [i for i in range(len(xyz)) if xyz[i, 2] > top2 - 1e-5 and abs(xyz[i, 0]) < 0.05]
    grp.add(pins, 1.0, "REPLACE")
    props2.pin_vertex_group = "Pin"
    open_gap2 = seam_gap(pair_cloth)
    check("置いただけでは縫い目は開いている", open_gap2 > 0.03, f"{open_gap2 * 100:.1f}cm")
    # 縫い目の溶接(Weld Seams): 縫い目が開いている間は何も溶接されない
    from muslin import weld
    base_count = len(pair_cloth.data.vertices)
    before_v, before_b = evaluated_stats(pair_cloth)
    props2.weld_seams = True
    group = pair_cloth.vertex_groups.get(weld.GROUP_NAME)
    seam_v = weld.seam_vertices(pair_cloth)
    check("Weld Seams を入れると頂点グループと Weld モディファイアができる",
          group is not None and weld._our_modifier(pair_cloth) is not None and len(seam_v) > 0, f"{len(seam_v)} 頂点")
    check("モディファイアは先頭で、頂点グループに限って溶接する",
          list(pair_cloth.modifiers)[0].name == weld.MODIFIER_NAME
          and pair_cloth.modifiers[weld.MODIFIER_NAME].vertex_group == weld.GROUP_NAME
          and pair_cloth.modifiers[weld.MODIFIER_NAME].mode == 'ALL')
    members = sorted(v.index for v in pair_cloth.data.vertices if any(g.group == group.index for g in v.groups))
    check("頂点グループは縫い目の頂点と一致する", members == seam_v)
    check("距離は辺の長さの 2%", abs(pair_cloth.modifiers[weld.MODIFIER_NAME].merge_threshold
                                   - max(weld.MIN_DISTANCE, 0.02 * mesh_io.median_edge_length(pair_cloth.data))) < 1e-9)
    open_v, open_b = evaluated_stats(pair_cloth)
    check("縫い目が開いている間は溶接されない(頂点数・外周が変わらない)",
          open_v == before_v == base_count and open_b == before_b, f"{open_v} 頂点 / 外周 {open_b}")

    res = bpy.ops.muslin.dress()
    check("Dress が通る(2 ピース・2 本の縫い目)", res == {'FINISHED'}, str(res))
    check("縫い目が閉じる", seam_gap(pair_cloth) < 0.003, f"{seam_gap(pair_cloth) * 1000:.2f}mm")
    closed_v, closed_b = evaluated_stats(pair_cloth)
    check("縫い目が閉じると、その頂点が溶接されて減る", closed_v < base_count, f"{base_count} → {closed_v}")
    check("溶接で縫い目が外周でなくなる(外周の辺が減る)", closed_b < open_b, f"{open_b} → {closed_b}")
    check("溶接で減った頂点数は縫い目の頂点のおよそ半分(2 枚の頂点が対になる)",
          0.35 * len(seam_v) < base_count - closed_v < 0.65 * len(seam_v), f"{base_count - closed_v} / {len(seam_v)}")
    check("ベースメッシュ(シミュレーションの頂点)は溶接されない", len(pair_cloth.data.vertices) == base_count)
    props2.weld_distance = 0.0001
    check("距離を設定すればモディファイアに反映される",
          abs(pair_cloth.modifiers[weld.MODIFIER_NAME].merge_threshold - 0.0001) < 1e-9)
    props2.weld_distance = 0.0
    cp.remove_seam(pair, cp.load_record(pair)["seams"][1]["uid"])      # Left を消す(後で足し直す)
    check("縫い目を消すと頂点グループも合わせて更新される",
          len(weld.seam_vertices(pair_cloth)) < len(seam_v)
          and sorted(v.index for v in pair_cloth.data.vertices
                     if any(g.group == pair_cloth.vertex_groups[weld.GROUP_NAME].index for g in v.groups))
          == weld.seam_vertices(pair_cloth))
    props2.weld_seams = False
    check("Weld Seams を切ると、モディファイアと頂点グループが片付く",
          weld._our_modifier(pair_cloth) is None and weld.GROUP_NAME not in pair_cloth.vertex_groups
          and "Pin" in pair_cloth.vertex_groups)
    # 後の検査のために、消した縫い目を足し直す(順番は Right、Left のまま)
    cp.add_seam(pair, [(4, 0.0, 1, 0.0)], [(6, 0.0, 7, 0.0)], name="Left")
    cp.apply_seams(pair)
    # 縫い目をまたぐ曲げ制約(蝶番にならないように)。縫い目 2 本 × 頂点 17 本ぶんの隣り合う対
    sim_state.start_simulation(pair_cloth, pair_cloth.muslin)
    st_p = sim_state.get_state(pair_cloth)
    bend_n = st_p["sim"].seam_bending_count
    check("縫い目をまたぐ曲げ制約がコアに渡される", bend_n > 10 and st_p["info"]["seam_bending"] == bend_n, f"{bend_n} 組")
    start_p = np.array(st_p["sim"].get_positions()).reshape(-1, 3)
    for f in range(2, 8):
        scene.frame_set(f)
    end_p = np.array(sim_state.get_state(pair_cloth)["sim"].get_positions()).reshape(-1, 3)
    check("曲げ制約があっても着せた姿勢から崩れない(6 フレームで最大移動 3cm 未満)",
          np.isfinite(end_p).all() and np.abs(end_p - start_p).max() < 0.03, f"{np.abs(end_p - start_p).max() * 1000:.2f}mm")
    # 型紙(Curve)を変えると、静止形状が型紙で決まるので作り直される
    pts_p = pair.data.splines[0].bezier_points
    for i in (1, 2):
        pts_p[i].co.x = 0.32
        pts_p[i].handle_left = pts_p[i].handle_right = pts_p[i].co
    check("型紙を変えても縫い目の曲げ制約は残る", sim_state.poll_pattern(st_p) is True
          and st_p["sim"].seam_bending_count == bend_n)
    sim_state.stop_simulation(pair_cloth)
    scene.frame_set(1)
    for i in (1, 2):
        pts_p[i].co.x = 0.3
        pts_p[i].handle_left = pts_p[i].handle_right = pts_p[i].co

    dressed = np.array([tuple(v.co) for v in pair_cloth.data.vertices])
    rad = np.hypot(dressed[:, 0], dressed[:, 1])
    check("着せた後も体の外側にある", rad.min() > 0.1 - 0.004, f"最小 {rad.min():.4f}m")

    section("対応の表示(Curve と布の縫い目の色・ピースの番号)")
    from muslin import overlay
    geo = overlay.curve_overlay_geometry(pair, pair_cloth)
    colors = [c for c, _ in geo["lines"]]
    check("縫い目 2 本 × (Curve 側 2 辺 + 布側 1 本) = 6 本の線", len(geo["lines"]) == 6, str(len(geo["lines"])))
    check("縫い目ごとに別の色(2 色)で、各色が Curve 2 本 + 布 1 本",
          len(set(colors)) == 2 and all(colors.count(c) == 3 for c in set(colors)))
    check("Curve と布の同じ縫い目は同じ色", colors[:3] == [colors[0]] * 3 and colors[3:] == [colors[3]] * 3)
    right_front = geo["lines"][0][1]
    xs = [v.x for v in right_front]
    ys = [v.y for v in right_front]
    check("Curve 側の線は縫い目の区間(前身頃の右辺 x = 0.3、y = 0〜0.5)をなぞる",
          all(abs(x - 0.3) < 1e-3 for x in xs) and abs(min(ys)) < 1e-3 and abs(max(ys) - 0.5) < 1e-3)
    codes_, edges_ = mesh_io.read_seam_codes(pair_cloth.data)
    seam_uids = [s_["uid"] for s_ in cp.load_record(pair)["seams"]]
    n_edges = sum(1 for c_ in codes_ if c_ and (c_ - 1) // 2 == seam_uids[0])
    check("布側の線は縫い目の辺(両側)と同じ数", len(geo["lines"][2][1]) == 2 * n_edges, f"{n_edges} 辺")
    texts = sorted(t for t, _, _ in geo["labels"])
    check("ピースの番号は Curve と布に 1 つずつ(1 と 2 が 2 回ずつ)", texts.count("1") == 2 and texts.count("2") == 2)
    check("縫い目の名前も出る(Right・Left が Curve の 2 辺と布の 1 か所)",
          texts.count("Right") == 3 and texts.count("Left") == 3, str(texts))
    piece_labels = [(t, v) for t, v, _ in geo["labels"] if t == "1"]
    check("番号 1 の位置は Curve の前身頃の中心と、布の前身頃の中心(正面 = -Y 側)",
          abs(piece_labels[0][1].x - 0.15) < 1e-3 and piece_labels[1][1].y < -0.05, str(piece_labels))
    pair.location = (5.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    moved = overlay.curve_overlay_geometry(pair, pair_cloth)
    check("Curve を動かせば、描く位置も動く(ワールド座標)", abs(min(v.x for v in moved["lines"][0][1]) - 5.3) < 1e-3)
    pair.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    only_curve = overlay.curve_overlay_geometry(pair, None)
    check("布が無ければ Curve 側だけを出す", len(only_curve["lines"]) == 4)
    check("Curve Pattern でない Curve は何も出さない",
          overlay.curve_overlay_geometry(make_curve("Plain"), None) == {"lines": [], "labels": []})
    overlay.invalidate_cache()
    check("描画のハンドラが登録されている(線と文字)", overlay._draw_handle is not None and overlay._label_handle is not None)

    section("選択の連動(Curve で選んだ区間に対応する布の辺)")
    from muslin import curve_ids
    check("オブジェクトモードでは何も出さない",
          overlay.curve_selection_geometry(pair, pair_cloth) == {"edges": [], "points": []})
    for o in scene.objects:
        o.select_set(o is pair)
    bpy.context.view_layer.objects.active = pair
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.curve.select_all(action='DESELECT')
    sp0 = pair.data.splines[0]
    pts0 = sp0.bezier_points
    uids0 = curve_ids.decode_all([p.radius for p in pts0], [p.weight_softbody for p in pts0])
    pts0[1].select_control_point = True       # 前身頃の右下
    pts0[2].select_control_point = True       # 前身頃の右上
    check("選んだ区間と点を読める(編集中の Curve から直接)",
          overlay.curve_selection(pair) == ({(uids0[1], uids0[2])}, {uids0[1], uids0[2]}))
    sel = overlay.curve_selection_geometry(pair, pair_cloth)
    pdata = cp.reconstruct(pair_cloth)
    expected_n = sum(1 for ring in pdata["rings"] for a in ring["vertices"]
                     if (int(pdata["seg_start"][a]), int(pdata["seg_end"][a])) == (uids0[1], uids0[2]))
    check("選んだ区間(前身頃の右辺)に対応する布の辺を返す",
          len(sel["edges"]) == 2 * expected_n and expected_n > 0, f"{expected_n} 辺")
    mwc = pair_cloth.matrix_world
    corner = [v.index for v in pair_cloth.data.vertices
              if pdata["u"][v.index] == 0.0 and int(pdata["seg_start"][v.index]) == uids0[1]]
    check("選んだ点に対応する布の頂点(制御点の頂点)を、布の今の位置で返す",
          len(sel["points"]) == 2 and len(corner) == 1
          and any((p - mwc @ pair_cloth.data.vertices[corner[0]].co).length < 1e-6 for p in sel["points"]))
    pts0[2].select_control_point = False
    single = overlay.curve_selection_geometry(pair, pair_cloth)
    check("点を 1 つだけ選ぶと、辺は出ず頂点 1 つ", single["edges"] == [] and len(single["points"]) == 1)
    bpy.ops.curve.select_all(action='SELECT')
    whole = overlay.curve_selection_geometry(pair, pair_cloth)
    n_boundary = sum(len(ring["vertices"]) for ring in pdata["rings"])
    check("全部選ぶと、布の外周すべて(2 枚分)", len(whole["edges"]) == 2 * n_boundary, f"{n_boundary} 辺")
    bpy.ops.object.mode_set(mode='OBJECT')

    section("布を消してから Rebuild し直す")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    c = make_curve("D")
    cp.initialize(c, 0.05)
    cloth_d, _ = cp.rebuild(bpy.context, c)
    first_name = cloth_d.name
    for o in bpy.context.selected_objects:
        o.select_set(False)
    cloth_d.select_set(True)
    bpy.context.view_layer.objects.active = cloth_d
    bpy.ops.object.delete()
    bpy.context.view_layer.objects.active = c
    check("ビューポートで消した布は、無いものとして扱われる", cp.cloth_of(c) is None)
    check("パネルは布が無い状態になる", cp.status(c)["cloth"] is None)
    cloth_d2, warns = cp.rebuild(bpy.context, c)
    check("消した後に Rebuild すると、布がシーンに現れる", cloth_d2.name in bpy.context.scene.objects
          and len(cloth_d2.users_collection) == 1 and len(cloth_d2.data.vertices) > 0)
    check("名前は同じ(.001 が付かない)", cloth_d2.name == first_name, cloth_d2.name)
    check("古い布はデータからも片付けられる",
          [o.name for o in bpy.data.objects if o.type == 'MESH'] == [first_name])
    check("警告は出ない", warns == [], str(warns))
    check("Curve と布は結ばれ直している", cp.cloth_of(c) == cloth_d2 and cp.curve_of(cloth_d2) == c)
    # 外した(unlink だけ)場合と、データごと消した場合も同じ
    for c_ in list(cloth_d2.users_collection):
        c_.objects.unlink(cloth_d2)
    cloth_d3, _ = cp.rebuild(bpy.context, c)
    check("コレクションから外しただけでも、Rebuild で現れる", cloth_d3.name in bpy.context.scene.objects)
    bpy.data.objects.remove(cloth_d3)
    cloth_d4, _ = cp.rebuild(bpy.context, c)
    check("データごと消した後も Rebuild で現れる", cloth_d4.name in bpy.context.scene.objects)

    # ------------------------------------------------------------------
    section("ピースを向かい合わせに重ねる(Stack Pieces for Bag / M11)")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_set(1)

    # 同じ大きさの 2 枚(0.3m 角)を XY 平面に並べる。クッションの表と裏
    cu3 = bpy.data.curves.new("Bag", 'CURVE')
    cu3.dimensions = '2D'
    for x0 in (0.0, 0.5):
        sp = cu3.splines.new('BEZIER')
        sp.bezier_points.add(3)
        for pt, (x, y) in zip(sp.bezier_points,
                              [(x0, 0.0), (x0 + 0.3, 0.0), (x0 + 0.3, 0.3), (x0, 0.3)]):
            pt.co = (x, y, 0.0)
            pt.handle_left_type = pt.handle_right_type = 'VECTOR'
        sp.use_cyclic_u = True
    bag = bpy.data.objects.new("Bag", cu3)
    scene.collection.objects.link(bag)
    bpy.context.view_layer.objects.active = bag
    cp.initialize(bag, 0.02)
    check("袋にできる枚数が数えられる", cp.status(bag)["panels"] == 2,
          str(cp.status(bag).get("panels")))

    bag_cloth, warns = cp.stack_pieces(bpy.context, bag)
    check("Stack が通る", bag_cloth is not None)
    check("重ねるだけでは警告は出ない", warns == [], str(warns))

    xyz = np.array([tuple(v.co) for v in bag_cloth.data.vertices])
    piece_ids = np.array([bag_cloth.data.attributes[cp.ATTR_PIECE].data[i].value
                          for i in range(len(bag_cloth.data.vertices))])
    ids = sorted(set(int(i) for i in piece_ids))
    top = xyz[piece_ids == ids[0]]
    bottom = xyz[piece_ids == ids[1]]
    check("2 枚が面に垂直な方向(Z)で離れている",
          abs(top[:, 2].mean() - bottom[:, 2].mean() - cp.DEFAULT_BAG_GAP) < 1e-6,
          f"隙間 {top[:, 2].mean() - bottom[:, 2].mean():.4f} m")
    check("2 枚が XY で重なる(中心がそろう)",
          abs(top[:, 0].mean() - bottom[:, 0].mean()) < 1e-6
          and abs(top[:, 1].mean() - bottom[:, 1].mean()) < 1e-6)
    check("重ねても型紙の大きさは変わらない",
          abs(np.ptp(top[:, 0]) - 0.3) < 1e-6 and abs(np.ptp(bottom[:, 0]) - 0.3) < 1e-6,
          f"幅 {np.ptp(top[:, 0]):.4f} / {np.ptp(bottom[:, 0]):.4f} m")

    # 下側のピースは面が裏返っている(圧力が外へ向き、陰影も正しくなる)
    bag_cloth.data.calc_loop_triangles()
    normals = {}
    for poly in bag_cloth.data.polygons:
        pid = int(piece_ids[poly.vertices[0]])
        normals.setdefault(pid, []).append(poly.normal.z)
    check("上の枚の面は +Z を向く", min(normals[ids[0]]) > 0.99,
          f"最小 {min(normals[ids[0]]):.3f}")
    check("下の枚の面は -Z を向く", max(normals[ids[1]]) < -0.99,
          f"最大 {max(normals[ids[1]]):.3f}")
    check("裏返したピースは記録に残る",
          cp.flipped_pieces(cp.load_record(bag)) == {ids[1]}, str(cp.load_record(bag)["flipped"]))

    # 外周を一周する縫い目で 2 枚を縫う。画面と同じ操作(編集モードで輪郭を全部選ぶ)で作る。
    # 終点を最後の点にすると最後の 1 辺が縫われず、袋が開いたままになる
    uids_a = [p["uids"] for p in cp.load_record(bag)["pieces"]][0]
    uids_b = [p["uids"] for p in cp.load_record(bag)["pieces"]][1]
    for o in scene.objects:
        o.select_set(o is bag)
    bpy.context.view_layer.objects.active = bag
    bpy.ops.object.mode_set(mode='EDIT')
    for sp in bag.data.splines:
        for pt in sp.bezier_points:
            pt.select_control_point = True
    res = bpy.ops.muslin.curve_seam_add()
    bpy.ops.object.mode_set(mode='OBJECT')
    check("輪郭を全部選んで縫い目を作れる", res == {'FINISHED'}, str(res))
    seam = cp.load_record(bag)["seams"][0]
    check("輪郭を全部選ぶと一周する縫い目になる(始点 = 終点)",
          seam["a"][0][0] == seam["a"][0][2] == uids_a[0]
          and seam["b"][0][0] == seam["b"][0][2] == uids_b[0],
          f"{seam['a']} / {seam['b']}")
    broken = cp.apply_seams(bag)
    check("外周を一周する縫い目が張れる", broken == [], str(broken))

    # 一部だけ選んだときは、これまでどおり区間のまま(一周にはしない)
    cp.remove_seam(bag, seam["uid"])
    bpy.ops.object.mode_set(mode='EDIT')
    for si, sp in enumerate(bag.data.splines):
        for k, pt in enumerate(sp.bezier_points):
            pt.select_control_point = k < 2
    bpy.ops.muslin.curve_seam_add()
    bpy.ops.object.mode_set(mode='OBJECT')
    part = cp.load_record(bag)["seams"][0]
    check("一部だけ選んだときは区間のまま", part["a"][0][0] != part["a"][0][2],
          str(part["a"]))
    cp.remove_seam(bag, part["uid"])
    cp.add_seam(bag, [(uids_a[0], 0.0, uids_a[0], 0.0)],
                [(uids_b[0], 0.0, uids_b[0], 0.0)], name="Rim")
    cp.apply_seams(bag)

    # Rebuild しても裏返しと縫い目が保たれる(記録に残してあるため)
    bag_cloth2, _ = cp.rebuild(bpy.context, bag)
    ids2 = {}
    for poly in bag_cloth2.data.polygons:
        pid = int(bag_cloth2.data.attributes[cp.ATTR_PIECE].data[poly.vertices[0]].value)
        ids2.setdefault(pid, []).append(poly.normal.z)
    check("Rebuild しても下の枚は裏返ったまま", max(ids2[ids[1]]) < -0.99,
          f"最大 {max(ids2[ids[1]]):.3f}")
    check("Rebuild しても Rebuild Required にならない",
          cp.status(bag)["problems"] == [], str(cp.status(bag)["problems"]))

    # 袋を閉じて膨らませる(Close Bag。重力 0 で回す)
    props3 = bag_cloth2.muslin
    props3.collision_enabled = False
    props3.self_collision_enabled = False
    props3.seam_close_frames = 10
    props3.pressure = 100.0
    props3.quality = 'HIGH'
    for o in scene.objects:
        o.select_set(o is bag_cloth2)
    bpy.context.view_layer.objects.active = bag_cloth2
    check("Close Bag が押せる", bpy.ops.muslin.close_bag.poll())
    before = np.array([tuple(v.co) for v in bag_cloth2.data.vertices])
    res = bpy.ops.muslin.close_bag(max_steps=200)
    check("Close Bag が通る", res == {'FINISHED'}, str(res))
    after = np.array([tuple(v.co) for v in bag_cloth2.data.vertices])
    check("袋が膨らむ(Z の厚みが隙間より広がる)",
          np.ptp(after[:, 2]) > np.ptp(before[:, 2]), 
          f"厚み {np.ptp(before[:, 2]) * 1000:.1f} -> {np.ptp(after[:, 2]) * 1000:.1f} mm")
    # 縫い合わせた縁が合う(輪郭の環ごとに、相手の環までの距離を見る)
    rings = cp.reconstruct(bag_cloth2)["rings"]
    rim_a = after[rings[0]["vertices"]] if "vertices" in rings[0] else None
    if rim_a is None:
        rim_a = after[rings[0]["start"]:rings[0]["start"] + rings[0]["count"]]
        rim_b = after[rings[1]["start"]:rings[1]["start"] + rings[1]["count"]]
    else:
        rim_b = after[rings[1]["vertices"]]
    gaps = np.linalg.norm(rim_a[:, None, :] - rim_b[None, :, :], axis=2).min(axis=1)
    check("縫い合わせた縁が合う", float(gaps.max()) < 0.01,
          f"縁の最大の隙間 {gaps.max() * 1000:.1f} mm")
    check("重力の設定そのものは変わらない(回している間だけ 0 にする)",
          abs(props3.gravity - 9.81) < 1e-5, str(props3.gravity))
    check("閉じた形が開始姿勢として保存される", rest_shape.is_dressed(bag_cloth2))

    # 1 枚しかない型紙では袋にできない
    bpy.ops.wm.read_factory_settings(use_empty=True)
    one = make_curve("One")
    cp.initialize(one, 0.05)
    check("1 枚では Stack は押せない(poll)", bpy.ops.muslin.curve_stack.poll() is False)
    check("1 枚では Stack が理由つきで失敗する",
          _raises(lambda: cp.stack_pieces(bpy.context, one)))

    muslin.unregister()


def _maxdiff(a, b):
    return float(np.abs(np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)).max())


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


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

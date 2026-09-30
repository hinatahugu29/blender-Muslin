"""スパイク: 従来型 Curve の編集操作で、点の index / radius / tilt / weight がどう動くか。

点ごとに radius=10*(k+1), tilt=0.1*(k+1), weight_softbody=k+1 を「ID の目印」として
仕込み、各編集操作のあとに残り方を表にする。Blender の -b で走る。
  blender -b --factory-startup --python scripts/spikes/spike_legacy_curve.py
"""
import bpy, os, tempfile

def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)

def make(points_per_spline, cyclic=False, ids_start=1, name="C"):
    """splines: list of lists of (x, y). 目印は通し番号。"""
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '2D'
    k = 0
    for pts in points_per_spline:
        sp = cu.splines.new('BEZIER')
        sp.bezier_points.add(len(pts) - 1)
        for p, (x, y) in zip(sp.bezier_points, pts):
            k += 1
            p.co = (x, y, 0)
            p.handle_left_type = p.handle_right_type = 'VECTOR'
            p.radius = 10.0 * k
            p.tilt = 0.1 * k
            p.weight_softbody = float(k)
        sp.use_cyclic_u = cyclic
    obj = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj

def dump(obj, label):
    cu = obj.data
    print(f"  [{label}] splines={len(cu.splines)}")
    for i, sp in enumerate(cu.splines):
        pts = sp.bezier_points
        row = " ".join(f"({p.co.x:.1f},{p.co.y:.1f}|r{p.radius:g},t{p.tilt:.2f},w{p.weight_softbody:g})" for p in pts)
        print(f"    s{i} cyc={sp.use_cyclic_u} n={len(pts)}: {row}")

def select(obj, pred):
    """pred(spline_index, point_index) が真の制御点だけ選ぶ。"""
    for si, sp in enumerate(obj.data.splines):
        for pi, p in enumerate(sp.bezier_points):
            v = pred(si, pi)
            p.select_control_point = v
            p.select_left_handle = v
            p.select_right_handle = v

def edit(obj, fn):
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        fn()
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

LINE5 = [(0, 0), (1, 1), (2, 0), (3, 1), (4, 0)]
SQUARE = [(0, 0), (1, 0), (1, 1), (0, 1)]

def case(title, build, prep, op, note=""):
    print(f"\n== {title} {note}")
    fresh()
    obj = build()
    dump(obj, "before")
    prep(obj)
    try:
        edit(obj, op)
    except Exception as e:
        print("  ERROR:", str(e).splitlines()[0][:150])
    dump(bpy.context.view_layer.objects.active, "after")

def main():
    print("VERSION", bpy.app.version_string)
    line = lambda: make([LINE5])
    sq = lambda: make([SQUARE], cyclic=True)

    case("extrude(末尾 P5)", line, lambda o: select(o, lambda s, p: p == 4),
         lambda: bpy.ops.curve.extrude(mode='INIT'))
    case("extrude(途中 P3 の点。cyclic 正方形の1点)", sq, lambda o: select(o, lambda s, p: p == 1),
         lambda: bpy.ops.curve.extrude(mode='INIT'))
    case("subdivide(P2-P3 の区間)", line, lambda o: select(o, lambda s, p: p in (1, 2)),
         lambda: bpy.ops.curve.subdivide(number_cuts=1))
    case("subdivide(全選択 ×2 分割)", line, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.subdivide(number_cuts=2))
    case("delete VERT(P3)", line, lambda o: select(o, lambda s, p: p == 2),
         lambda: bpy.ops.curve.delete(type='VERT'))
    case("dissolve_verts(P3)", line, lambda o: select(o, lambda s, p: p == 2),
         lambda: bpy.ops.curve.dissolve_verts())
    case("split(P1..P3 を別 spline へ)", line, lambda o: select(o, lambda s, p: p in (1, 2, 3)),
         lambda: bpy.ops.curve.split())
    case("make_segment(2 spline の端をつなぐ)",
         lambda: make([LINE5[:3], [(5, 0), (6, 1), (7, 0)]]),
         lambda o: select(o, lambda s, p: (s == 0 and p == 2) or (s == 1 and p == 0)),
         lambda: bpy.ops.curve.make_segment())
    case("cyclic_toggle(開→閉)", line, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.cyclic_toggle())
    case("cyclic_toggle(閉→開)", sq, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.cyclic_toggle())
    case("switch_direction", line, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.switch_direction())
    case("switch_direction(cyclic)", sq, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.switch_direction())
    case("duplicate(P2,P3 を複製)", line, lambda o: select(o, lambda s, p: p in (1, 2)),
         lambda: bpy.ops.curve.duplicate())
    case("decimate(全選択)", line, lambda o: select(o, lambda s, p: True),
         lambda: bpy.ops.curve.decimate(ratio=0.5))

    # ---- オブジェクト単位
    print("\n== separate(P1,P2 を別オブジェクトへ)")
    fresh(); o = line(); select(o, lambda s, p: p in (1, 2))
    edit(o, lambda: bpy.ops.curve.separate())
    for ob in bpy.data.objects:
        dump(ob, ob.name)

    print("\n== object.duplicate(データも複製)")
    fresh(); o = line(); o.data["muslin_probe"] = "hello"
    bpy.ops.object.duplicate(linked=False)
    for ob in bpy.data.objects:
        dump(ob, ob.name + "/" + ob.data.name + " prop=" + str(ob.data.get("muslin_probe")))

    print("\n== object.join(2 つの Curve)")
    fresh(); a = make([LINE5[:3]], name="A"); b = make([[(5, 0), (6, 1), (7, 0)]], name="B")
    for ob in (a, b): ob.select_set(True)
    bpy.context.view_layer.objects.active = a
    bpy.ops.object.join()
    dump(a, "joined")

    # ---- Undo / Redo(背景実行で効くか)
    print("\n== undo/redo(subdivide のあと)")
    fresh(); o = line(); select(o, lambda s, p: p in (1, 2))
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.ed.undo_push(message="before")
    bpy.ops.curve.subdivide(number_cuts=1)
    bpy.ops.object.mode_set(mode='OBJECT')
    dump(o, "after subdivide")
    try:
        bpy.ops.ed.undo(); dump(bpy.context.view_layer.objects.active, "after undo")
        bpy.ops.ed.redo(); dump(bpy.context.view_layer.objects.active, "after redo")
    except Exception as e:
        print("  undo ERROR:", str(e).splitlines()[0][:150])

    # ---- 保存 / 再読込
    print("\n== .blend 保存・再読込")
    fresh(); o = sq(); o.data["muslin_probe"] = {"k": 1}
    path = os.path.join(tempfile.gettempdir(), "muslin_spike.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    ob = bpy.data.objects["C"]
    dump(ob, "reloaded"); print("  prop =", ob.data.get("muslin_probe"))

    print("\n== API: Spline / BezierSplinePoint に任意プロパティを持てるか")
    fresh(); o = line()
    sp = o.data.splines[0]
    print("  spline keys API:", hasattr(sp, "keys"), " point keys API:", hasattr(sp.bezier_points[0], "keys"))
    print("  spline is ID-property owner:", hasattr(sp, "__setitem__"))

main()

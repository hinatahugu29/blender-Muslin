"""スパイク補足: 従来型 Curve の carrier 候補の値域、cyclic での split/delete、任意プロパティの可否。"""
import bpy
exec(open(__file__.replace("spike_legacy_extra", "spike_legacy_curve")).read().split("\nmain()")[0])

print("VERSION", bpy.app.version_string)

print("\n== 値域: radius / weight_softbody / tilt にどんな値を書けるか")
fresh(); o = make([LINE5]); p = o.data.splines[0].bezier_points[0]
for name, vals in (("radius", [0.0, -5.0, 1e5, 16777216.0, 16777217.0]),
                   ("weight_softbody", [0.0, 0.001, 50.0, 100.0, 5000.0, -1.0]),
                   ("tilt", [0.0, 1000.0])):
    out = []
    for v in vals:
        try:
            setattr(p, name, v); out.append(f"{v:g}->{getattr(p, name):g}")
        except Exception as e:
            out.append(f"{v:g}->ERR")
    print(" ", name, " ".join(out))

print("\n== spline / point への任意プロパティ")
sp = o.data.splines[0]
for target, nm in ((sp, "spline"), (sp.bezier_points[0], "point")):
    try:
        target["x"] = 1; print(" ", nm, "['x'] OK")
    except Exception as e:
        print(" ", nm, "['x'] ->", type(e).__name__, str(e)[:60])

print("\n== delete SEGMENT(cyclic の 1 辺)")
fresh(); o = make([SQUARE], cyclic=True); select(o, lambda s, p: p in (1, 2))
dump(o, "before"); edit(o, lambda: bpy.ops.curve.delete(type='SEGMENT')); dump(bpy.context.view_layer.objects.active, "after")

print("\n== delete VERT(cyclic の先頭 P1)")
fresh(); o = make([SQUARE], cyclic=True); select(o, lambda s, p: p == 0)
edit(o, lambda: bpy.ops.curve.delete(type='VERT')); dump(bpy.context.view_layer.objects.active, "after")

print("\n== split(cyclic の 2 点)")
fresh(); o = make([SQUARE], cyclic=True); select(o, lambda s, p: p in (1, 2))
edit(o, lambda: bpy.ops.curve.split()); dump(bpy.context.view_layer.objects.active, "after")

print("\n== subdivide で 2 と 4 の間 → 中点が既存 ID(3) に化けるか")
fresh(); o = make([[(0, 0), (1, 0), (2, 0)]]); select(o, lambda s, p: p in (0, 2))
sp = o.data.splines[0]
# P1 と P3 の間を分割したいので 3 点を作り直す: 目印 10,20,30 → 10 と 30 の中点は 20
select(o, lambda s, p: p in (0, 1, 2))
edit(o, lambda: bpy.ops.curve.subdivide(number_cuts=1)); dump(bpy.context.view_layer.objects.active, "after")

print("\n== 複数 spline の並び: separate 後の元オブジェクトの index")
fresh(); o = make([SQUARE[:3], [(5, 0), (6, 1), (7, 0)], [(9, 0), (10, 1), (11, 0)]])
select(o, lambda s, p: s == 1)
edit(o, lambda: bpy.ops.curve.separate())
for ob in bpy.data.objects: dump(ob, ob.name)

print("\n== 3D 座標を動かしても index は不変(grab 相当)")
fresh(); o = make([LINE5]); pts = o.data.splines[0].bezier_points
for p in pts: p.co.y += 0.5
dump(o, "moved")

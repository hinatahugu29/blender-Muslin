"""GUI 確認用: 目印つきの Curve を作り、今の状態を Text ブロック 'muslin_dump' に書き出す。

Scripting ワークスペースの Text エディタで開いて Run Script。以後は 3D ビューで
F3 → "Muslin Dump Curve" を実行するたびに、その時点の点の並びが 'muslin_dump' に追記される。
"""
import bpy

PHI = 0


def _mix(u):
    x = (u * 0x9E3779B1) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & 0xFFFFFFFF
    x ^= x >> 16
    return x / 2**32


def chk(u):
    return 0.01 + 99.98 * _mix(u)


class MUSLIN_OT_dump_curve(bpy.types.Operator):
    bl_idname = "muslin.dump_curve"
    bl_label = "Muslin Dump Curve"

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'CURVE':
            self.report({'ERROR'}, "Curve を選んでください")
            return {'CANCELLED'}
        if obj.mode == 'EDIT':
            obj.update_from_editmode()
        lines = [f"--- {obj.name} mode={obj.mode}"]
        for si, sp in enumerate(obj.data.splines):
            lines.append(f"s{si} cyclic={sp.use_cyclic_u} n={len(sp.bezier_points)}")
            for pi, p in enumerate(sp.bezier_points):
                u = round(p.radius)
                state = "OK" if abs(p.radius - u) < 1e-4 and abs(p.weight_softbody - chk(u)) < 1e-3 else "NEW?"
                lines.append(f"  [{pi}] ({p.co.x:.2f},{p.co.y:.2f}) uid={p.radius:g} chk={p.weight_softbody:.3f} {state}")
        txt = bpy.data.texts.get("muslin_dump") or bpy.data.texts.new("muslin_dump")
        txt.write("\n".join(lines) + "\n")
        self.report({'INFO'}, f"muslin_dump に追記({len(lines)} 行)")
        return {'FINISHED'}


def make():
    cu = bpy.data.curves.new("Probe", 'CURVE')
    cu.dimensions = '2D'
    sp = cu.splines.new('BEZIER')
    sp.bezier_points.add(3)
    for k, (p, xy) in enumerate(zip(sp.bezier_points, [(0, 0), (1, 0), (1, 1), (0, 1)]), start=1):
        p.co = (xy[0], xy[1], 0)
        p.handle_left_type = p.handle_right_type = 'VECTOR'
        p.radius = float(k)
        p.weight_softbody = chk(k)
    sp.use_cyclic_u = True
    obj = bpy.data.objects.new("Probe", cu)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)


bpy.utils.register_class(MUSLIN_OT_dump_curve)
make()
print("Probe を作りました。F3 → Muslin Dump Curve で状態を muslin_dump に追記します")

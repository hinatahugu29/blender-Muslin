import math

import bpy

from . import cloth_core
from . import mesh_io
from . import sim_state


class CLOTHMD_OT_test_rust(bpy.types.Operator):
    """Rustコアの拡張モジュールが正しく呼べるか確認する"""

    bl_idname = "cloth_md.test_rust"
    bl_label = "Test Rust Core"

    def execute(self, context):
        message = cloth_core.hello()
        result = cloth_core.add(1.0, 2.0)
        version = cloth_core.core_version()

        print(f"[cloth_md] {message}")
        print(f"[cloth_md] add(1, 2) = {result}")
        print(f"[cloth_md] core_version = {version}")

        self.report({'INFO'}, f"cloth_core {version} OK / add(1,2)={result}")
        return {'FINISHED'}


class CLOTHMD_OT_self_test(bpy.types.Operator):
    """Blender内でコアの物理挙動を自己診断する(シーンを一切変更しない)"""

    bl_idname = "cloth_md.self_test"
    bl_label = "Run Self Test"

    def execute(self, context):
        results = []

        # 1) 自由落下が解析解に一致するか
        sim = cloth_core.ClothSim([0.0, 0.0, 0.0], [], [], [], [], 0.0)
        for _ in range(240):
            sim.step(1.0 / 240.0, -9.81, 10, 1, 0.0)
        z = sim.get_positions()[2]
        expected = -0.5 * 9.81
        ok = abs(z - expected) < 0.05
        results.append(("free fall", ok, f"z={z:.4f} (expected ~{expected:.4f})"))

        # 2) 吊り下げグリッドが発散せず伸びも小さいか
        nx = ny = 11
        spacing = 0.1
        positions = []
        for y in range(ny):
            for x in range(nx):
                positions.extend((x * spacing, y * spacing, 0.0))

        def vid(x, y):
            return y * nx + x

        edges, bending, tris = [], [], []
        for y in range(ny):
            for x in range(nx):
                if x + 1 < nx:
                    edges.append((vid(x, y), vid(x + 1, y)))
                if y + 1 < ny:
                    edges.append((vid(x, y), vid(x, y + 1)))
                if x + 2 < nx:
                    bending.append((vid(x, y), vid(x + 2, y)))
                if y + 2 < ny:
                    bending.append((vid(x, y), vid(x, y + 2)))
                if x + 1 < nx and y + 1 < ny:
                    tris.append((vid(x, y), vid(x + 1, y), vid(x + 1, y + 1)))
                    tris.append((vid(x, y), vid(x + 1, y + 1), vid(x, y + 1)))

        pinned = [vid(x, ny - 1) for x in range(nx)]
        grid = cloth_core.ClothSim(positions, edges, bending, tris, pinned, 0.2, 0.0, 1e-4)
        for _ in range(180):
            grid.step(1.0 / 60.0, -9.81, 10, 4, 0.01)
        err = grid.average_stretch_error()
        ok = grid.is_finite() and err < 0.05
        results.append(("hanging grid", ok, f"stretch error={err:.5f}"))

        # 3) 縫製制約で2辺が引き寄せられるか
        seam = cloth_core.ClothSim(
            [-0.5, 0.0, 0.0, -0.4, 0.0, 0.0, 0.4, 0.0, 0.0, 0.5, 0.0, 0.0],
            [(0, 1), (2, 3)], [], [], [], 0.0,
        )
        seam.set_seams([(1, 2)], 0.0)
        for i in range(120):
            seam.set_seam_closure(i / 119.0)
            seam.step(1.0 / 60.0, 0.0, 10, 4, 0.1)
        p = seam.get_positions()
        gap = ((p[3] - p[6]) ** 2 + (p[4] - p[7]) ** 2 + (p[5] - p[8]) ** 2) ** 0.5
        ok = gap < 0.08
        results.append(("seam closure", ok, f"gap={gap:.5f} (initial 0.8)"))

        # 4) コリジョン: 球の上に落とした布が貫通しない
        cx, cy, cz, radius = 0.4, 0.4, -0.3, 0.25
        sphere_pos, sphere_tris = [], []
        segments, rings = 16, 12
        for r in range(rings + 1):
            phi = math.pi * r / rings
            for s in range(segments):
                theta = 2.0 * math.pi * s / segments
                sphere_pos.extend((
                    cx + radius * math.sin(phi) * math.cos(theta),
                    cy + radius * math.sin(phi) * math.sin(theta),
                    cz + radius * math.cos(phi),
                ))
        for r in range(rings):
            for s in range(segments):
                s2 = (s + 1) % segments
                a, b = r * segments + s, r * segments + s2
                c, d = (r + 1) * segments + s2, (r + 1) * segments + s
                sphere_tris.append((a, d, c))
                sphere_tris.append((a, c, b))

        positions2, edges2, bending2, tris2 = [], [], [], []
        n2 = 15
        for y in range(n2):
            for x in range(n2):
                positions2.extend((x * 0.06, y * 0.06, 0.0))
        for y in range(n2):
            for x in range(n2):
                i = y * n2 + x
                if x + 1 < n2:
                    edges2.append((i, i + 1))
                if y + 1 < n2:
                    edges2.append((i, i + n2))
                if x + 2 < n2:
                    bending2.append((i, i + 2))
                if y + 2 < n2:
                    bending2.append((i, i + 2 * n2))
                if x + 1 < n2 and y + 1 < n2:
                    tris2.append((i, i + 1, i + n2 + 1))
                    tris2.append((i, i + n2 + 1, i + n2))

        drape = cloth_core.ClothSim(positions2, edges2, bending2, tris2, [], 0.2, 0.0, 1e-4)
        drape.add_collider(sphere_pos, sphere_tris)
        touched = False
        deepest = float('inf')
        for _ in range(180):
            drape.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                       False, 0.0, 0.3, True, 0.008, 0.3)
            touched = touched or drape.last_collision_count > 0
            p = drape.get_positions()
            for k in range(len(p) // 3):
                d = math.dist(p[k * 3:k * 3 + 3], (cx, cy, cz))
                deepest = min(deepest, d)
        ok = touched and deepest > radius - 0.01 and drape.is_finite()
        results.append(("sphere collision", ok,
                        f"接触={touched}, 中心からの最小距離={deepest:.4f} (半径 {radius})"))

        print("[cloth_md] ---- self test ----")
        for name, ok, detail in results:
            print(f"[cloth_md]  {'PASS' if ok else 'FAIL'}  {name}: {detail}")

        failed = [name for name, ok, _ in results if not ok]
        if failed:
            self.report({'ERROR'}, f"Self test FAILED: {', '.join(failed)}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Self test passed ({len(results)}/{len(results)})")
        return {'FINISHED'}


class CLOTHMD_OT_start_sim(bpy.types.Operator):
    """選択中のメッシュオブジェクトでクロスシミュレーションを開始する"""

    bl_idname = "cloth_md.start_sim"
    bl_label = "Start Simulation"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        props = context.scene.cloth_md_props

        try:
            info = sim_state.start_simulation(obj, props)
        except mesh_io.MeshValidationError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"シミュレーション開始に失敗: {exc}")
            return {'CANCELLED'}

        print(f"[cloth_md] start '{obj.name}': {info}")
        for warning in info.get("warnings", []):
            print(f"[cloth_md]  ! {warning}")
            self.report({'WARNING'}, warning)
        self.report(
            {'INFO'},
            "Started: {v} verts / {p} pinned / {s} seams / {c} colliders ({ct} tris)".format(
                v=info["vertices"], p=info["pinned"], s=info["seams"],
                c=len(info["colliders"]), ct=info["collider_triangles"],
            ),
        )
        return {'FINISHED'}


class CLOTHMD_OT_stop_sim(bpy.types.Operator):
    """選択中のメッシュオブジェクトのクロスシミュレーションを停止する"""

    bl_idname = "cloth_md.stop_sim"
    bl_label = "Stop Simulation"

    @classmethod
    def poll(cls, context):
        return sim_state.is_running(context.active_object)

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        self.report({'INFO'}, f"Stopped simulation on '{obj.name}'")
        return {'FINISHED'}


_classes = (
    CLOTHMD_OT_test_rust,
    CLOTHMD_OT_self_test,
    CLOTHMD_OT_start_sim,
    CLOTHMD_OT_stop_sim,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

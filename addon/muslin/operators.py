import math

import bpy

from . import cloth_core
from . import mesh_io
from . import rest_shape
from . import sim_state
from . import ui_poll


class MUSLIN_OT_test_rust(bpy.types.Operator):
    """Rustコアの拡張モジュールが正しく呼べるか確認する"""

    bl_idname = "muslin.test_rust"
    bl_label = "Test Rust Core"

    def execute(self, context):
        message = cloth_core.hello()
        result = cloth_core.add(1.0, 2.0)
        version = cloth_core.core_version()

        print(f"[muslin] {message}")
        print(f"[muslin] add(1, 2) = {result}")
        print(f"[muslin] core_version = {version}")

        self.report({'INFO'}, f"cloth_core {version} OK / add(1,2)={result}")
        return {'FINISHED'}


class MUSLIN_OT_self_test(bpy.types.Operator):
    """Blender内でコアの物理挙動を自己診断する(シーンを一切変更しない)"""

    bl_idname = "muslin.self_test"
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

        edges, tris = [], []
        for y in range(ny):
            for x in range(nx):
                if x + 1 < nx:
                    edges.append((vid(x, y), vid(x + 1, y)))
                if y + 1 < ny:
                    edges.append((vid(x, y), vid(x, y + 1)))
                if x + 1 < nx and y + 1 < ny:
                    tris.append((vid(x, y), vid(x + 1, y), vid(x + 1, y + 1)))
                    tris.append((vid(x, y), vid(x + 1, y + 1), vid(x, y + 1)))

        pinned = [vid(x, ny - 1) for x in range(nx)]
        # 曲げ制約の4頂点は三角形から導く(コア側の関数を使う)
        bending = cloth_core.bending_quads_from_triangles(tris)
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

        positions2, edges2, tris2 = [], [], []
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
                if x + 1 < n2 and y + 1 < n2:
                    tris2.append((i, i + 1, i + n2 + 1))
                    tris2.append((i, i + n2 + 1, i + n2))

        bending2 = cloth_core.bending_quads_from_triangles(tris2)
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

        print("[muslin] ---- self test ----")
        for name, ok, detail in results:
            print(f"[muslin]  {'PASS' if ok else 'FAIL'}  {name}: {detail}")

        failed = [name for name, ok, _ in results if not ok]
        if failed:
            self.report({'ERROR'}, f"Self test FAILED: {', '.join(failed)}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Self test passed ({len(results)}/{len(results)})")
        return {'FINISHED'}


class MUSLIN_OT_print_timings(bpy.types.Operator):
    """実行中のシミュレーションの時間内訳をコンソールに出す"""

    bl_idname = "muslin.print_timings"
    bl_label = "Print Timings"

    # 表示順と日本語ラベル。合計に対する割合で、どこが重いかを見る
    _ROWS = (
        ("integrate", "積分(外力・予測位置)"),
        ("stretch", "伸び制約"),
        ("bending", "曲げ制約"),
        ("seam", "縫製制約"),
        ("floor", "床衝突"),
        ("object_collision", "オブジェクト衝突"),
        ("self_collision", "自己衝突"),
        ("hash_rebuild", "  └ 空間ハッシュ再構築"),
        ("post_collision", "衝突後の伸び補正"),
        ("velocity", "速度更新・摩擦"),
    )

    @classmethod
    def poll(cls, context):
        return ui_poll.sim_running(cls, context, sim_state.is_running)

    def execute(self, context):
        obj = context.active_object
        state = sim_state.get_state(obj)
        timings = state["sim"].timings()
        total = timings.get("total", 0.0)
        if total <= 0.0:
            self.report({'WARNING'}, "まだ1フレームも計算していません")
            return {'CANCELLED'}

        threads = cloth_core.thread_count()
        print(f"[muslin] ---- '{obj.name}' 直近フレームの内訳 "
              f"(コリジョンは最大 {threads} スレッド) ----")
        for key, label in self._ROWS:
            value = timings.get(key, 0.0)
            if value < 1e-4:
                continue
            print(f"[muslin]  {label:<22}{value:8.3f} ms  {value / total * 100:5.1f}%")
        print(f"[muslin]  {'合計':<22}{total:8.3f} ms")

        self.report({'INFO'}, f"内訳をコンソールに出力しました(合計 {total:.2f} ms)")
        return {'FINISHED'}


class MUSLIN_OT_fit_thickness(bpy.types.Operator):
    """厚みをメッシュの細かさに合わせて設定する"""

    bl_idname = "muslin.fit_thickness"
    bl_label = "Fit Thickness to Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        obj = context.active_object
        props = obj.muslin

        suggestion = mesh_io.suggest_thickness(obj)
        if suggestion is None:
            self.report({'ERROR'}, "エッジが無いため厚みを決められません")
            return {'CANCELLED'}

        collision, self_collision = suggestion
        props.collision_thickness = collision
        props.self_collision_thickness = self_collision

        edge = mesh_io.median_edge_length(obj.data)
        self.report(
            {'INFO'},
            f"エッジ長 {edge:.4f} に合わせました: "
            f"Thickness {collision:.4f} / Self {self_collision:.4f}",
        )
        return {'FINISHED'}


class MUSLIN_OT_start_sim(bpy.types.Operator):
    """選択中のメッシュオブジェクトでクロスシミュレーションを開始する"""

    bl_idname = "muslin.start_sim"
    bl_label = "Start Simulation"

    @classmethod
    def poll(cls, context):
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        obj = context.active_object
        props = obj.muslin

        try:
            info = sim_state.start_simulation(obj, props)
        except mesh_io.MeshValidationError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"シミュレーション開始に失敗: {exc}")
            return {'CANCELLED'}

        print(f"[muslin] start '{obj.name}': {info}")
        for warning in info.get("warnings", []):
            print(f"[muslin]  ! {warning}")
            self.report({'WARNING'}, warning)
        self.report(
            {'INFO'},
            "Started: {v} verts / {p} pinned / {s} seams / {c} colliders ({ct} tris)".format(
                v=info["vertices"], p=info["pinned"], s=info["seams"],
                c=len(info["colliders"]), ct=info["collider_triangles"],
            ),
        )
        return {'FINISHED'}


class MUSLIN_OT_stop_sim(bpy.types.Operator):
    """選択中のメッシュオブジェクトのクロスシミュレーションを停止する"""

    bl_idname = "muslin.stop_sim"
    bl_label = "Stop Simulation"

    @classmethod
    def poll(cls, context):
        return ui_poll.sim_running(cls, context, sim_state.is_running)

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        self.report({'INFO'}, f"Stopped simulation on '{obj.name}'")
        return {'FINISHED'}


class MUSLIN_OT_reset_shape(bpy.types.Operator):
    """シミュレーションを止めて、メッシュを元の形に戻す"""

    bl_idname = "muslin.reset_shape"
    bl_label = "Reset to Rest Shape"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not ui_poll.mesh_selected(cls, context):
            return False
        if not rest_shape.has_rest(context.active_object):
            return ui_poll.reject(cls, "元の形がまだ記録されていません")
        return True

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        if not rest_shape.restore(obj):
            self.report({'ERROR'}, "元の形と頂点数が合いません(メッシュを編集しましたか)")
            return {'CANCELLED'}
        self.report({'INFO'}, f"'{obj.name}' を元の形に戻しました")
        return {'FINISHED'}


class MUSLIN_OT_set_rest_shape(bpy.types.Operator):
    """今の形を着せた姿勢として保存し、次からはこの姿勢で始める

    布の寸法(型紙)は変えない。着せた形を寸法ごと元の形にすると、
    収束しきらない伸びが焼き込まれ、し直すたびに服が大きくなるため。
    """

    bl_idname = "muslin.set_rest_shape"
    bl_label = "Save Dressed Pose"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        # 一度も走らせていない形を保存する場合は、今の形が型紙になる
        if not rest_shape.has_pattern(obj):
            rest_shape.store_pattern(obj)
        if not rest_shape.store(obj):
            self.report({'ERROR'}, "メッシュに頂点がありません")
            return {'CANCELLED'}
        rest_shape.mark_dressed(obj)
        self.report(
            {'INFO'},
            f"今の形を '{obj.name}' の開始姿勢にしました(型紙の寸法はそのまま)",
        )
        return {'FINISHED'}


class MUSLIN_OT_lock_pattern(bpy.types.Operator):
    """今の形を型紙として確定する。以後、開始しても型紙を取り直さない

    ピースを胴のまわりに曲げて配置する前に押す。確定しないまま曲げてから
    開始すると、曲げた形が型紙(布の寸法の基準)になってしまう。
    """

    bl_idname = "muslin.lock_pattern"
    bl_label = "Lock Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        if not ui_poll.mesh_selected(cls, context):
            return False
        obj = context.active_object
        if rest_shape.is_deformed(obj):
            # 計算結果の形を型紙にしてしまう事故を防ぐ
            return ui_poll.reject(cls, "シミュレーションの結果が表示されています。"
                                       "Reset で元の形に戻してから確定してください")
        if rest_shape.is_pattern_locked(obj):
            return ui_poll.reject(cls, "型紙はすでに確定しています")
        return True

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        if not rest_shape.lock_pattern(obj):
            self.report({'ERROR'}, "メッシュに頂点がありません")
            return {'CANCELLED'}
        self.report({'INFO'}, f"'{obj.name}' の今の形を型紙として確定しました")
        return {'FINISHED'}


class MUSLIN_OT_unlock_pattern(bpy.types.Operator):
    """型紙の確定を外し、型紙を作る段階に戻す(形はそのまま)

    次に開始したときに今の形が型紙になる。曲げて置いた形のまま外すと、
    曲げた形が型紙になるので注意。
    """

    bl_idname = "muslin.unlock_pattern"
    bl_label = "Unlock Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not ui_poll.mesh_selected(cls, context):
            return False
        if not rest_shape.is_pattern_locked(context.active_object):
            return ui_poll.reject(cls, "型紙は確定していません")
        return True

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        rest_shape.unlock_pattern(obj)
        self.report({'INFO'}, f"'{obj.name}' の型紙の確定を外しました")
        return {'FINISHED'}


class MUSLIN_OT_restore_pattern(bpy.types.Operator):
    """型紙の形に戻し、型紙を作る段階に戻る

    着せた姿勢を捨てる。以後の編集は再び型紙の変更として扱われる。
    """

    bl_idname = "muslin.restore_pattern"
    bl_label = "Restore Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not ui_poll.mesh_selected(cls, context):
            return False
        if not rest_shape.has_pattern(context.active_object):
            return ui_poll.reject(cls, "型紙がまだ記録されていません(一度開始すると記録されます)")
        return True

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        if not rest_shape.restore_pattern(obj):
            self.report({'ERROR'}, "型紙と頂点数が合いません(メッシュを編集しましたか)")
            return {'CANCELLED'}
        self.report({'INFO'}, f"'{obj.name}' を型紙の形に戻しました")
        return {'FINISHED'}


class MUSLIN_OT_restart_sim(bpy.types.Operator):
    """シミュレーションを組み立て直す(コライダーや縫い目の変更を反映する)

    布は開始時の形に戻る。
    """

    bl_idname = "muslin.restart_sim"
    bl_label = "Restart Simulation"

    @classmethod
    def poll(cls, context):
        return ui_poll.sim_running(cls, context, sim_state.is_running)

    def execute(self, context):
        obj = context.active_object
        sim_state.stop_simulation(obj)
        res = bpy.ops.muslin.start_sim()
        if res != {'FINISHED'}:
            return res
        # 開始し直すと start_frame が現在フレームになるので、そこへ揃える
        context.scene.frame_set(sim_state.get_state(obj)["start_frame"])
        return {'FINISHED'}


def _is_playing(context):
    screen = getattr(context, "screen", None)
    return bool(screen is not None and screen.is_animation_playing)


class MUSLIN_OT_play(bpy.types.Operator):
    """シミュレーションを再生する(止まっているときは一時停止する)

    必要ならシミュレーションの開始も兼ねる。パネルから目を離して
    タイムラインへ操作を探しに行かずに済むようにするためのもの。
    """

    bl_idname = "muslin.play"
    bl_label = "Play"

    @classmethod
    def poll(cls, context):
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        obj = context.active_object
        scene = context.scene

        if _is_playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)
            return {'FINISHED'}

        if not sim_state.is_running(obj):
            # start_frame は「開始した時点のフレーム」になる。再生の起点と
            # ずれると巻き戻し先が直感に反するので、先頭へ移してから始める。
            if scene.frame_current != scene.frame_start:
                scene.frame_set(scene.frame_start)
            res = bpy.ops.muslin.start_sim()
            if res != {'FINISHED'}:
                return res

        if getattr(context, "screen", None) is None:
            # ヘッドレスでは再生する画面が無い。シミュレーション自体は
            # 開始できているので、そこまでを成果として返す。
            self.report({'INFO'}, "開始しました(再生には画面が必要です)")
            return {'FINISHED'}
        bpy.ops.screen.animation_play()
        return {'FINISHED'}


class MUSLIN_OT_rewind(bpy.types.Operator):
    """再生を止めて開始フレームまで巻き戻し、布を元の形に戻す"""

    bl_idname = "muslin.rewind"
    bl_label = "Rewind"

    @classmethod
    def poll(cls, context):
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        obj = context.active_object
        scene = context.scene

        if _is_playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)

        state = sim_state.get_state(obj)
        # 走っていればその開始フレームへ。走っていなければシーンの先頭へ。
        target = state["start_frame"] if state is not None else scene.frame_start
        scene.frame_set(target)
        self.report({'INFO'}, f"フレーム {target} に戻しました")
        return {'FINISHED'}


_classes = (
    MUSLIN_OT_test_rust,
    MUSLIN_OT_self_test,
    MUSLIN_OT_print_timings,
    MUSLIN_OT_fit_thickness,
    MUSLIN_OT_start_sim,
    MUSLIN_OT_stop_sim,
    MUSLIN_OT_play,
    MUSLIN_OT_rewind,
    MUSLIN_OT_restart_sim,
    MUSLIN_OT_reset_shape,
    MUSLIN_OT_set_rest_shape,
    MUSLIN_OT_lock_pattern,
    MUSLIN_OT_unlock_pattern,
    MUSLIN_OT_restore_pattern,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

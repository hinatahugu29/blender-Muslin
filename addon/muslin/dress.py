"""着せ付け: 時間軸を進めずに、縫って落ち着くまで回す(M7)。

以前は縫い目がタイムライン上で閉じていたので、縫い目が閉じていく数十
フレームがアニメーションの冒頭に必ず入った。Marvelous Designer と同じく
「着せる」と「動かす」を分け、着せる方は時間の外で済ませる。

終わったら着せた姿勢として保存する(`Save Dressed Pose` と同じ扱い)。
寸法は型紙のまま。以後のアニメーションはこの姿勢から始まる。
"""

import bpy
import numpy as np

from . import grab
from . import mesh_io
from . import rest_shape
from . import sim_state

# 全頂点の最大速度がこれを下回る状態が続いたら落ち着いたとみなす。
# 布の端がわずかに揺れ続けることがあるので 0 にはしない。1cm/s は
# 24fps で1フレーム 0.4mm で、目で見て止まっている。
SETTLE_SPEED = 0.01          # m/s
SETTLE_STEPS = 12            # 続けて下回る必要のあるステップ数
DEFAULT_MAX_STEPS = 600      # 24fps で 25 秒相当。これで落ち着かなければ打ち切る

# 着せ付けの間だけ減衰を強める(1秒あたりに失う速度の割合)。欲しいのは
# 静止した形で、途中の揺れ方ではない。2つの場面で測った:
#
#   何にも触れずに吊られた布(上端の中央だけ留め、平面の中で折れる):
#     生地の減衰 0.05 のまま → 600 ステップでも振り子のように揺れ続けた
#     0.5 → 271 ステップ / 0.9 → 114 / 0.99 → 76
#   円柱(胴)に触れて着ている筒(selftest の場面):
#     0.05 → 172 ステップ、0.9 との形の差 最大 0.06mm
#     0.5 → 176 / 0.9 → 183 / 0.99 → 175(差 3.0mm)
#
# 触れて着ている場面では減衰はほとんど効かず、形もほぼ変わらない。
# 揺れ続けるのは体から離れて垂れる部分(スカートの裾など)なので、
# そちらのために 0.9 にする。0.99 は形が数 mm 変わり始める。
DRESS_DAMPING = 0.9

# つまむ強さ(XPBD のコンプライアンス)。0 は硬く引く(マウスの位置へそのまま行く)。
# 柔らかくした方が手応えが良いかは実機で確かめる(CHECKPOINTS)
GRAB_COMPLIANCE = 0.0


class _DressProps:
    """生地の設定のうち、減衰だけを着せ付け用に差し替えて見せる。"""

    def __init__(self, props):
        self._props = props

    def __getattr__(self, name):
        value = getattr(self._props, name)
        if name == "damping":
            return max(value, DRESS_DAMPING)
        return value


class Dresser:
    """1ステップずつ着せ付けを進める。モーダルでも同期でも同じものを使う。"""

    def __init__(self, obj, props, dt, max_steps=DEFAULT_MAX_STEPS):
        self.obj = obj
        self.props = _DressProps(props)
        self.dt = dt
        self.max_steps = max_steps
        # 走っているシミュレーションとは別に回す(タイムラインに触れない)
        sim_state.stop_simulation(obj)
        count = len(obj.data.vertices)
        self.before = np.empty(count * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", self.before)
        # 開始時の形(型紙を作る段階なら型紙、着せた段階なら着せた姿勢)に
        # そろえてから組み立てる。組み立て方は通常の開始と同じ
        self.state = sim_state.create_state(obj, props)
        self.steps = 0
        self.calm = 0
        self.speed = float("inf")
        self.seam_steps = props.seam_close_frames if self.state["info"]["seams"] else 0
        self._last = np.asarray(self.state["sim"].get_positions())
        # つまんでいる間の状態。離したら上限のステップ数を数え直す
        self.grabbed = None
        self._budget_start = 0
        # レイを当てる三角形(トポロジは着せ付けの間変わらない)
        mesh = obj.data
        mesh.calc_loop_triangles()
        self._triangles = [tuple(t.vertices) for t in mesh.loop_triangles]

    @property
    def warnings(self):
        return self.state["info"]["warnings"]

    @property
    def settled(self):
        return self.calm >= SETTLE_STEPS

    @property
    def done(self):
        if not self.state["sim"].is_finite():
            return True
        if self.grabbed is not None:
            return False      # つまんでいる間は止めない
        return self.settled or self.steps - self._budget_start >= self.max_steps

    def step(self):
        """1ステップ進め、終わったら True を返す。"""
        self.steps += 1
        # 縫い目の閉じ方はタイムラインのときと同じ(seam_close_frames ステップで閉じる)
        sim_state.advance_one_frame(
            self.state, self.props, self.dt, self.state["start_frame"] + self.steps
        )
        now = np.asarray(self.state["sim"].get_positions())
        moved = np.linalg.norm((now - self._last).reshape(-1, 3), axis=1)
        self._last = now
        self.speed = float(moved.max()) / self.dt if moved.size else 0.0
        # 縫い目が閉じ切るまでは、止まって見えても落ち着いたことにしない
        if self.grabbed is None and self.steps > self.seam_steps and self.speed < SETTLE_SPEED:
            self.calm += 1
        else:
            self.calm = 0
        return self.done

    # ------------------------------------------------------- つまむ

    def pick(self, origin, direction):
        """ワールド座標のレイの先にある布の頂点を返す。当たらなければ None。

        戻り値は (頂点番号, レイが面に当たった点)。
        """
        positions = self.state["sim"].get_positions()
        found = grab.ray_triangles(origin, direction, positions, self._triangles)
        if found is None:
            return None
        tri, world_hit = found
        index = grab.nearest_vertex(world_hit, self._triangles[tri], positions)
        return index, world_hit

    def begin_grab(self, index, anchor_hit):
        positions = self.state["sim"].get_positions()
        start = tuple(positions[index * 3:index * 3 + 3])
        self.grabbed = {"index": index, "start": start, "anchor": tuple(anchor_hit)}
        self.state["sim"].set_grab(index, start, GRAB_COMPLIANCE)

    def move_grab(self, now_hit):
        if self.grabbed is None:
            return
        g = self.grabbed
        target = grab.drag_target(g["start"], g["anchor"], now_hit)
        self.state["sim"].set_grab(g["index"], target, GRAB_COMPLIANCE)

    def end_grab(self):
        """離す。離した後はあらためて落ち着くまで回す。"""
        if self.grabbed is None:
            return
        self.state["sim"].clear_grab()
        self.grabbed = None
        self.calm = 0
        self._budget_start = self.steps

    def show(self):
        """途中経過をメッシュに書く(ビューポートで見せるため)。"""
        mesh_io.write_positions_to_mesh(self.obj, self.state["sim"].get_positions())

    def finish(self):
        """今の形を着せた姿勢として保存する。発散していたら元に戻して False。"""
        if not self.state["sim"].is_finite():
            self.cancel()
            return False
        self.show()
        rest_shape.store(self.obj)
        rest_shape.mark_dressed(self.obj)
        return True

    def cancel(self):
        """着せ付け前の形に戻す。"""
        self.obj.data.vertices.foreach_set("co", self.before)
        self.obj.data.update()

    def summary(self):
        if self.settled:
            return f"{self.steps} ステップで落ち着きました"
        return (
            f"{self.steps} ステップで打ち切りました"
            f"(最大速度 {self.speed * 100:.1f} cm/s。まだ動いています)"
        )


class MUSLIN_OT_dress(bpy.types.Operator):
    """時間軸を進めずに、縫って落ち着くまで回し、着せた姿勢として保存する

    寸法は型紙のまま。左ドラッグで布をつまんで動かせる。Esc で中止
    (元の形に戻る)、Enter でその場で確定する。
    """

    bl_idname = "muslin.dress"
    bl_label = "Dress"
    bl_options = {'REGISTER', 'UNDO'}

    max_steps: bpy.props.IntProperty(
        name="Max Steps",
        description="落ち着かなくてもここで打ち切る",
        default=DEFAULT_MAX_STEPS,
        min=1,
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        return ui_poll.mesh_selected(cls, context)

    def _start(self, context):
        obj = context.active_object
        self._dresser = Dresser(obj, obj.muslin, sim_state.effective_dt(context.scene),
                                self.max_steps)
        for w in self._dresser.warnings:
            self.report({'WARNING'}, w)

    def _end(self, context, commit):
        dresser = self._dresser
        if commit and dresser.finish():
            self.report({'INFO'}, "着せ付けを保存しました: " + dresser.summary())
            return {'FINISHED'}
        if commit:
            self.report({'ERROR'}, "シミュレーションが発散しました。元の形に戻します")
        else:
            dresser.cancel()
            self.report({'INFO'}, "着せ付けを中止しました")
        return {'CANCELLED'}

    def execute(self, context):
        # スクリプトやテストから呼ばれたときは、最後まで同期で回す
        self._start(context)
        while not self._dresser.step():
            pass
        return self._end(context, commit=True)

    def invoke(self, context, event):
        self._start(context)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.0, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _view_ray(self, context, event):
        """マウス位置のレイ (origin, direction, 視線方向)。3D ビューの外なら None。

        パネルのボタンから呼ぶと context.region はサイドバーなので、
        同じエリアの WINDOW 領域を探してそこで計算する。
        """
        from bpy_extras import view3d_utils
        from mathutils import Vector
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            return None
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        rv3d = area.spaces.active.region_3d
        if region is None or rv3d is None:
            return None
        x, y = event.mouse_x - region.x, event.mouse_y - region.y
        if not (0 <= x < region.width and 0 <= y < region.height):
            return None
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
        forward = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
        return tuple(origin), tuple(direction), tuple(forward)

    def _handle_grab(self, context, event):
        """左ドラッグで布をつまむ。扱ったら True。"""
        d = self._dresser
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            ray = self._view_ray(context, event)
            if ray is None:
                return False
            picked = d.pick(ray[0], ray[1])
            if picked is None:
                return False      # 布の外をクリックしたら通常どおり(選択など)に回す
            d.begin_grab(*picked)
            self._plane_normal = ray[2]
            return True
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and d.grabbed is not None:
            d.end_grab()
            return True
        if event.type == 'MOUSEMOVE' and d.grabbed is not None:
            ray = self._view_ray(context, event)
            if ray is not None:
                # つまんだ点を通り、つまんだときの視線に垂直な平面の上で追う
                hit = grab.ray_plane(ray[0], ray[1], d.grabbed["anchor"], self._plane_normal)
                if hit is not None:
                    d.move_grab(hit)
            return True
        return False

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=False)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=True)
        if self._handle_grab(context, event):
            return {'RUNNING_MODAL'}
        if event.type != 'TIMER':
            # 視点の回転やズームは Blender に任せる(着せながら回り込んで見られるように)
            return {'PASS_THROUGH'}

        # 画面が固まらないよう、1回のタイマーで回すのは短い時間だけ
        import time
        deadline = time.perf_counter() + 0.03
        done = False
        while not done and time.perf_counter() < deadline:
            done = self._dresser.step()
        self._dresser.show()
        d = self._dresser
        context.area.header_text_set(
            f"着せ付け中: {d.steps} ステップ / 最大速度 {d.speed * 100:.1f} cm/s"
            "   左ドラッグ: つまむ   Enter: 確定   Esc: 中止"
        )
        if done:
            return self._finish_modal(context, commit=True)
        return {'RUNNING_MODAL'}

    def _finish_modal(self, context, commit):
        context.window_manager.event_timer_remove(self._timer)
        if context.area is not None:
            context.area.header_text_set(None)
        return self._end(context, commit)


_classes = (MUSLIN_OT_dress,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

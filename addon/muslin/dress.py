"""着せ付け: 時間軸を進めずに、縫って落ち着くまで回す(M7)。

以前は縫い目がタイムライン上で閉じていたので、縫い目が閉じていく数十
フレームがアニメーションの冒頭に必ず入った。Marvelous Designer と同じく
「着せる」と「動かす」を分け、着せる方は時間の外で済ませる。

終わったら着せた姿勢として保存する(`Save Dressed Pose` と同じ扱い)。
寸法は型紙のまま。以後のアニメーションはこの姿勢から始まる。
"""

import bpy
import numpy as np

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
# 静止した形で、途中の揺れ方ではない。生地の減衰(Cotton で 0.05)の
# ままだと、吊った筒が振り子のように揺れ続けて 600 ステップでも落ち着かなかった。
#
# ただし減衰は静止形状をまったく変えないわけではない。摩擦のある接触では
# 止まれる位置が1つに決まらず、どこで止まるかは経路で変わる。円柱に
# 着せた筒で比べると(selftest の場面):
#
#   減衰 0.5  : 271 ステップ、0.9 との差 最大 28mm
#   減衰 0.9  : 114 ステップ
#   減衰 0.99 :  76 ステップ、0.9 との差 最大 1.9mm
#
# 0.9 より強くしても形はほとんど変わらず、速さも頭打ちに近いので 0.9 にした。
DRESS_DAMPING = 0.9


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

    @property
    def warnings(self):
        return self.state["info"]["warnings"]

    @property
    def settled(self):
        return self.calm >= SETTLE_STEPS

    @property
    def done(self):
        return self.settled or self.steps >= self.max_steps or not self.state["sim"].is_finite()

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
        if self.steps > self.seam_steps and self.speed < SETTLE_SPEED:
            self.calm += 1
        else:
            self.calm = 0
        return self.done

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

    寸法は型紙のまま。Esc で中止(元の形に戻る)、Enter でその場で確定する。
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

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=False)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=True)
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}

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
            "   Enter: 確定   Esc: 中止"
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

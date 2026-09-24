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

    def __init__(self, obj, props, dt, max_steps=DEFAULT_MAX_STEPS, auto_finish=True):
        self.obj = obj
        # False なら落ち着いても上限に達しても止まらない(整えるモード)。
        # 止まるのは人が確定か中止をしたときと、発散したときだけ
        self.auto_finish = auto_finish
        self.dt = dt
        self.max_steps = max_steps
        # 走っているシミュレーションとは別に回す(タイムラインに触れない)。
        # 重ね着のグループなら、グループの布をまとめて着せる。ソルバーの設定は
        # 組み立てと同じく先頭の布(一番内側)のものを使う
        self.members = mesh_io.group_members(obj)
        props = self.members[0].muslin
        self.props = _DressProps(props)
        self.before = []
        for m in self.members:
            sim_state.stop_simulation(m)
            co = np.empty(len(m.data.vertices) * 3, dtype=np.float32)
            m.data.vertices.foreach_get("co", co)
            self.before.append(co)
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
        # レイを当てる三角形(トポロジは着せ付けの間変わらない)。グループでは
        # 布ごとに頂点番号をずらしてつなげる
        self._triangles = []
        for m, (_key, start, _count) in zip(self.members, self.state["members"]):
            m.data.calc_loop_triangles()
            self._triangles += [tuple(start + i for i in t.vertices)
                                for t in m.data.loop_triangles]

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
        if self.grabbed is not None or not self.auto_finish:
            return False      # つまんでいる間と、整えるモードでは止めない
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

    def poll_pattern(self):
        """型紙オブジェクトの編集を流す(M8)。流したら True。

        寸法が変わると布はまた動き出すので、落ち着きの判定と上限の
        ステップ数を数え直す(直した直後に「落ち着いた」で終わらないように)。
        """
        if not sim_state.poll_pattern(self.state, self.members):
            return False
        self.calm = 0
        self._budget_start = self.steps
        return True

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
        sim_state.write_state_positions(self.state, self.state["sim"].get_positions(), mark=False)

    def finish(self):
        """今の形を着せた姿勢として保存する。発散していたら元に戻して False。"""
        if not self.state["sim"].is_finite():
            self.cancel()
            return False
        self.show()
        for m in self.members:
            rest_shape.store(m)
            rest_shape.mark_dressed(m)
        return True

    def cancel(self):
        """着せ付け前の形に戻す。"""
        for m, co in zip(self.members, self.before):
            m.data.vertices.foreach_set("co", co)
            m.data.update()

    def summary(self):
        if self.settled:
            return f"{self.steps} ステップで落ち着きました"
        if not self.auto_finish:
            # 整えるモードは人が確定したところで終わる。打ち切りではない
            return (f"{self.steps} ステップ(確定した時点で最大速度 "
                    f"{self.speed * 100:.1f} cm/s)")
        return (
            f"{self.steps} ステップで打ち切りました"
            f"(最大速度 {self.speed * 100:.1f} cm/s。まだ動いています)"
        )


class _ClothModal:
    """着せ付け(Dress)と整える(Adjust)に共通のモーダル。

    違いは「落ち着いたら自動で終わるか」と文言だけ。つまむ操作、減衰の強化、
    保存の仕方は同じものを使う。
    """

    AUTO_FINISH = True
    HEADER = ""       # 実行中のヘッダの頭
    SAVED = ""        # 確定したときの報告
    CANCELLED = ""    # 中止したときの報告

    def _make_dresser(self, context):
        obj = context.active_object
        return Dresser(obj, obj.muslin, sim_state.effective_dt(context.scene),
                       self.max_steps, auto_finish=self.AUTO_FINISH)

    def _start(self, context):
        self._dresser = self._make_dresser(context)
        for w in self._dresser.warnings:
            self.report({'WARNING'}, w)

    def _end(self, context, commit):
        dresser = self._dresser
        if commit and dresser.finish():
            self.report({'INFO'}, f"{self.SAVED}: " + dresser.summary())
            return {'FINISHED'}
        if commit:
            self.report({'ERROR'}, "シミュレーションが発散しました。元の形に戻します")
        else:
            dresser.cancel()
            self.report({'INFO'}, self.CANCELLED)
        return {'CANCELLED'}

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

    def _header(self):
        d = self._dresser
        if d.grabbed is not None:
            state = "つまんでいます"
        elif d.settled:
            state = "落ち着きました"
        else:
            state = f"最大速度 {d.speed * 100:.1f} cm/s"
        return (f"{self.HEADER}: {d.steps} ステップ / {state}"
                "   左ドラッグ: つまむ   Enter: 確定   Esc: 中止")

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=False)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._finish_modal(context, commit=True)
        if self._handle_grab(context, event):
            return {'RUNNING_MODAL'}
        if event.type != 'TIMER':
            # 視点の回転やズームは Blender に任せる(回り込んで見られるように)
            return {'PASS_THROUGH'}

        # 型紙オブジェクトを編集していれば、その寸法で続ける
        self._dresser.poll_pattern()

        # 画面が固まらないよう、1回のタイマーで回すのは短い時間だけ
        import time
        deadline = time.perf_counter() + 0.03
        done = False
        while not done and time.perf_counter() < deadline:
            done = self._dresser.step()
        self._dresser.show()
        context.area.header_text_set(self._header())
        if done:
            return self._finish_modal(context, commit=True)
        return {'RUNNING_MODAL'}

    def _finish_modal(self, context, commit):
        context.window_manager.event_timer_remove(self._timer)
        if context.area is not None:
            context.area.header_text_set(None)
        return self._end(context, commit)


class MUSLIN_OT_dress(_ClothModal, bpy.types.Operator):
    """時間軸を進めずに、縫って落ち着くまで回し、着せた姿勢として保存する

    寸法は型紙のまま。落ち着くと自動で終わる。途中で左ドラッグでつまむこともできる。
    Esc で中止(元の形に戻る)、Enter でその場で確定する。
    """

    bl_idname = "muslin.dress"
    bl_label = "Dress"
    bl_options = {'REGISTER', 'UNDO'}

    AUTO_FINISH = True
    HEADER = "着せ付け中"
    SAVED = "着せ付けを保存しました"
    CANCELLED = "着せ付けを中止しました"

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

    def execute(self, context):
        # スクリプトやテストから呼ばれたときは、最後まで同期で回す
        self._start(context)
        while not self._dresser.step():
            pass
        return self._end(context, commit=True)


class MUSLIN_OT_adjust(_ClothModal, bpy.types.Operator):
    """着せた布をつまんで整える。Enter を押すまで終わらない

    着せた姿勢から始める(縫い目は閉じている)。左ドラッグでつまみ、
    Enter で着せた姿勢として保存、Esc で始める前の形に戻す。
    """

    bl_idname = "muslin.adjust"
    bl_label = "Adjust"
    bl_options = {'REGISTER', 'UNDO'}

    AUTO_FINISH = False
    HEADER = "整えています"
    SAVED = "整えた形を保存しました"
    CANCELLED = "整えるのをやめて元の形に戻しました"

    # 自動では終わらないので上限は使わないが、Dresser の引数として渡す
    max_steps: bpy.props.IntProperty(default=DEFAULT_MAX_STEPS, options={'HIDDEN'})
    steps: bpy.props.IntProperty(
        name="Steps",
        description="スクリプトから呼んだときに回すステップ数(画面からは Enter まで回る)",
        default=120,
        min=0,
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        if not ui_poll.mesh_selected(cls, context):
            return False
        # 着せていない布は縫い目が開いたまま。整える前に着せる
        if not rest_shape.is_dressed(context.active_object):
            return ui_poll.reject(cls, "先に Dress で着せてください")
        return True

    def execute(self, context):
        # スクリプトから呼ばれたときは決まった数だけ回して保存する
        self._start(context)
        for _ in range(self.steps):
            if self._dresser.step():
                break
        return self._end(context, commit=True)


_classes = (MUSLIN_OT_dress, MUSLIN_OT_adjust)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

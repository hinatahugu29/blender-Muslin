"""着せ付け: 時間軸を進めずに、縫って落ち着くまで回す(M7)。

以前は縫い目がタイムライン上で閉じていたので、縫い目が閉じていく数十
フレームがアニメーションの冒頭に必ず入った。Marvelous Designer と同じく
「着せる」と「動かす」を分け、着せる方は時間の外で済ませる。

終わったら着せた姿勢として保存する(`Save Dressed Pose` と同じ扱い)。
寸法は型紙のまま。以後のアニメーションはこの姿勢から始まる。
"""

import bpy
import numpy as np

from . import dress_pose
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


class LostCloth(Exception):
    """着せ付けの途中で布が見つからなくなった(削除された、など)。"""


class _DressProps:
    """生地の設定のうち、減衰だけを着せ付け用に差し替えて見せる。

    設定そのものは握らず、読むたびに `lookup()` で引き直す。元に戻す
    (Ctrl+Z)などで Blender がデータを読み直すと、握っていた参照は
    無効になり ReferenceError になる(実機の Adjust 中に起きた)。

    `overrides` を渡すと、その設定だけを差し替えて見せる(袋を閉じるときの
    重力 0 など)。設定そのものは書き換えないので、終わった後に残らない。
    """

    def __init__(self, lookup, overrides=None):
        self._lookup = lookup
        self._overrides = overrides or {}

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        value = getattr(self._lookup(), name)
        if name == "damping":
            return max(value, DRESS_DAMPING)
        return value


class Dresser:
    """1ステップずつ着せ付けを進める。モーダルでも同期でも同じものを使う。"""

    def __init__(self, obj, props, dt, max_steps=DEFAULT_MAX_STEPS, auto_finish=True,
                 overrides=None):
        # False なら落ち着いても上限に達しても止まらない(整えるモード)。
        # 止まるのは人が確定か中止をしたときと、発散したときだけ
        self.auto_finish = auto_finish
        self.dt = dt
        self.max_steps = max_steps
        # 走っているシミュレーションとは別に回す(タイムラインに触れない)。
        # 重ね着のグループなら、グループの布をまとめて着せる。ソルバーの設定は
        # 組み立てと同じく先頭の布(一番内側)のものを使う
        members = mesh_io.group_members(obj)
        props = members[0].muslin
        # 布と設定は握らずに、使うたびに引き直す(members / _DressProps を参照)
        self.props = _DressProps(lambda: self.members[0].muslin, overrides)
        self.before = []
        for m in members:
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
        self.seam_steps = (sim_state.seam_close_span(self.state, props)
                           if self.state["info"]["seams"] else 0)
        self._last = np.asarray(self.state["sim"].get_positions())
        # つまんでいる間の状態。離したら上限のステップ数を数え直す
        self.grabbed = None
        self._budget_start = 0
        # レイを当てる三角形(トポロジは着せ付けの間変わらない)。グループでは
        # 布ごとに頂点番号をずらしてつなげる
        self._triangles = []
        for m, (_key, start, _count) in zip(members, self.state["members"]):
            m.data.calc_loop_triangles()
            self._triangles += [tuple(start + i for i in t.vertices)
                                for t in m.data.loop_triangles]

    @property
    def members(self):
        """着せている布(先頭が設定を使う布)。使うたびに引き直す。

        Blender は元に戻す(Ctrl+Z)などでデータを読み直すと、Python が
        持っていたオブジェクトの参照を無効にする。握り続けると次に触った
        ところで ReferenceError になるので、状態に残した session_uid から
        引き直す(再生中の状態と同じやり方)。布が消えていれば LostCloth。
        """
        members = sim_state.member_objects(self.state)
        if not members:
            raise LostCloth("着せ付けていた布が見つかりません(削除されたか、読み直されました)")
        return members

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
        moved = self._moved(now, self._last)
        self._last = now
        self.speed = float(moved.max()) / self.dt if moved.size else 0.0
        # 縫い目が閉じ切るまでは、止まって見えても落ち着いたことにしない
        if self.grabbed is None and self.steps > self.seam_steps and self.speed < SETTLE_SPEED:
            self.calm += 1
        else:
            self.calm = 0
        return self.done

    def _moved(self, now, last):
        """頂点ごとの、1 ステップの移動量。"""
        return np.linalg.norm((now - last).reshape(-1, 3), axis=1)

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
        # 再生を始めるときと同じ食い込みの解消を、保存する前に通す。着せ付けの最後は
        # 衝突のあとに伸び制約を解き直すので、布がコライダーへ少し(最大 3mm)入ったまま
        # 終わることがある。そのまま保存すると、再生を始めたときに untangle が押し出し、
        # 先頭フレームの形が着せた形とずれた(スクラブで先頭へ戻すと 3mm 動いて見えた)
        p = self.props
        self.state["sim"].untangle(
            mesh_io.UNTANGLE_ITERATIONS, p.self_collision_enabled, p.self_collision_thickness,
            p.collision_enabled, p.collision_thickness, p.floor_enabled, p.floor_z)
        self.show()
        from . import pattern_link
        for m in self.members:
            # 型紙オブジェクトを編集していれば、その寸法を布の型紙として残す
            pattern_link.sync_before_start(m)
            rest_shape.store(m)
            rest_shape.mark_dressed(m)
            # 着せたときの体のポーズを覚えておく(再生を始めるときに比べる)
            dress_pose.record(m, bpy.context.scene.frame_current)
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


# 詰め具合(Fill)を目指すとき、目標の体積を今の体積からこのステップ数かけて上げる
# (24fps で 2 秒)。いきなり最終の目標を与えると、重力 0 の軽い布は全力の圧力で
# 一気に膨らみ、勢いで目標を大きく越えた(目標 2.5L に対し 15 ステップで 4.3L)。
# 越えた分は吸っても戻らないので、圧力が目標に追いつく速さで上げる
FILL_RAMP_STEPS = 48


def rigid_align(points, ref):
    """points を、ref に最もよく重なるよう剛体として動かした座標(Kabsch 法。形は変えない)。"""
    pc, qc = points.mean(axis=0), ref.mean(axis=0)
    h = (points - pc).T @ (ref - qc)
    u, _s, vt = np.linalg.svd(h)
    d = 1.0 if np.linalg.det(vt.T @ u.T) >= 0.0 else -1.0
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return (points - pc) @ rot.T + qc


class BagDresser(Dresser):
    """袋を閉じる(Close Bag)ための Dresser。満杯の体積を測り、Volume なら詰め具合まで落ち着かせる。

    - 一定の圧力で膨らみきって落ち着いたら、その体積を「満杯の体積」として布に記録する
      (Constant で閉じたときも記録するので、あとから Volume に切り替えられる)
    - Volume で、満杯の体積がまだ無ければ: まず一定の圧力で膨らみきらせて測り(measure)、
      続けて Fill × 満杯 を目標にしてもう一度落ち着かせる(fill)
    - Volume で満杯の体積を測ってあれば、最初から目標の体積で落ち着かせる
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        wants_volume = any(m.muslin.pressure_mode == 'VOLUME' and m.muslin.pressure != 0.0
                           for m in self.members)
        measured = all(m.muslin.full_volume > 0.0 for m in self.members
                       if m.muslin.pressure != 0.0)
        self.restarted = False
        self._ramp = None
        # 開始時の配置(確定するとき、袋の向きをここへ合わせ直す)
        self._origin = np.asarray(self.state["sim"].get_positions(), dtype=np.float64).reshape(-1, 3)
        if wants_volume and not measured:
            self.phase = "measure"     # 目標 0 = 一定の圧力のまま(full_volume が 0 なので)
        elif wants_volume:
            self.phase = "fill"
            self._restart_below_target()
            self._begin_ramp()
        else:
            self.phase = "constant"

    def volumes(self):
        """布ごとの今の体積(m^3)。圧力の番号は、1 着なら 0 番、グループなら布の順。"""
        vols = self.state["sim"].material_volumes()
        members = self.members
        return [vols[k if len(members) > 1 else 0] for k in range(len(members))]

    def _restart_below_target(self):
        """今の体積が目標より大きければ、平らに重ねた配置に戻して、そこから膨らませ直す。

        満杯に膨らんだ袋を吸って縮めようとしても、張った布は曲げに強く、重力も無いので
        ほとんど縮まない(30cm 角で目標 2.5L に対し 4.3〜4.6L で止まった)。詰め具合の少ない
        クッションは「少ない空気で膨らませた」形なので、下から目標へ近づける。
        Curve Pattern の袋だけ戻せる(重ねた配置を計算できる)。戻せなければ今の形のまま。
        """
        from . import curve_pattern
        members = self.members
        targets = [mesh_io.volume_target(m.muslin) for m in members]
        if not any(t > 0.0 and v > t * 1.02 for t, v in zip(targets, self.volumes())):
            return False
        world = []
        for m in members:
            if curve_pattern.curve_of(m) is None:
                return False
            local = curve_pattern.stacked_layout(m)
            mw = np.array(m.matrix_world, dtype=np.float64)
            world.append(local @ mw[:3, :3].T + mw[:3, 3])
        flat = np.concatenate(world).ravel()
        self.state["sim"].set_positions(flat.tolist())
        self._last = flat
        self._origin = flat.reshape(-1, 3).copy()
        # 縫い目は閉じたまま(閉じ具合は経過ステップで決まり、もう 1)。重ねた隙間ぶんを引き寄せる
        self.restarted = True
        return True

    def _begin_ramp(self):
        """目標の体積を、今の体積から最終の目標へ少しずつ上げ始める。"""
        finals = [mesh_io.volume_target(m.muslin) for m in self.members]
        if not any(t > 0.0 for t in finals):
            return
        starts = [min(v, t) if t > 0.0 else 0.0 for v, t in zip(self.volumes(), finals)]
        self._ramp = {"start": starts, "final": finals, "step0": self.steps}

    def _apply_ramp(self):
        if self._ramp is None:
            return
        r = self._ramp
        frac = min(1.0, (self.steps - r["step0"]) / FILL_RAMP_STEPS)
        targets = [s + (f - s) * frac if f > 0.0 else 0.0 for s, f in zip(r["start"], r["final"])]
        # 圧力の番号は 1 着なら 0 番だけ、グループなら布の順(apply_group_materials と同じ)
        self.state["sim"].set_volume_targets(targets)
        if frac >= 1.0:
            self._ramp = None

    def _record_full_volume(self):
        for m, v in zip(self.members, self.volumes()):
            if m.muslin.pressure != 0.0 and v > 0.0:
                m.muslin.full_volume = v

    def step(self):
        self._apply_ramp()
        done = super().step()
        # 落ち着いたときに加えて、上限で打ち切ったときも記録する(そのときの体積が最善の見積もり)
        if not done or not self.state["sim"].is_finite():
            return done
        if self.phase == "measure":
            # 膨らみきった。満杯の体積を記録し、詰め具合を目標にして続ける
            self._record_full_volume()
            members = self.members
            offsets = [(s, c) for _k, s, c in self.state["members"]]
            mesh_io.apply_group_materials(self.state["sim"], members, offsets)
            self.phase = "fill"
            self._restart_below_target()
            self._begin_ramp()
            self.calm = 0
            self._budget_start = self.steps
            return False
        if self.phase == "constant":
            self._record_full_volume()
        return done

    def align_to_origin(self):
        """袋全体の向きと位置を、開始時の配置に剛体として合わせ直す(形は変えない)。

        重力 0 の袋には外から力が掛からないので本来は回らないが、計算の誤差で回転が溜まり、
        30cm 角のクッションが膨らむあいだに 90° 起き上がった。確定するときに、開始時
        (重ねて置いた向き)へ最もよく重なる回転と平行移動(Kabsch 法)で戻す。
        """
        now = np.asarray(self.state["sim"].get_positions(), dtype=np.float64).reshape(-1, 3)
        ref = self._origin
        if now.shape != ref.shape or len(now) < 3:
            return
        aligned = rigid_align(now, ref)
        if np.isfinite(aligned).all():
            self.state["sim"].set_positions(aligned.ravel().tolist())

    def _moved(self, now, last):
        """袋の頂点ごとの移動量から、袋全体の回転と平行移動(剛体の動き)を除いたもの。

        重力 0 の袋は、圧力の計算の誤差で全体がゆっくり回り続ける(マチのある 30cm 角の
        クッションで 17cm/s)。形はとうに落ち着いていても最大速度が下がらず、上限まで
        回り続けていた。形の変化だけを見て、落ち着いたかを決める(向きは確定するときに戻す)。
        """
        now3 = np.asarray(now, dtype=np.float64).reshape(-1, 3)
        last3 = np.asarray(last, dtype=np.float64).reshape(-1, 3)
        if now3.shape != last3.shape or len(now3) < 3 or not np.isfinite(now3).all():
            return super()._moved(now, last)
        return np.linalg.norm(rigid_align(now3, last3) - last3, axis=1)

    def finish(self):
        if self.state["sim"].is_finite():
            self.align_to_origin()
        return super().finish()

    def summary(self):
        text = super().summary()
        vols = [v for m, v in zip(self.members, self.volumes()) if m.muslin.pressure != 0.0]
        if vols:
            text += f"。体積 {sum(vols) * 1000:.2f} L"
        return text


class _ClothModal:
    """着せ付け(Dress)と整える(Adjust)に共通のモーダル。

    違いは「落ち着いたら自動で終わるか」と文言だけ。つまむ操作、減衰の強化、
    保存の仕方は同じものを使う。
    """

    AUTO_FINISH = True
    OVERRIDES = None  # 回している間だけ差し替える設定(袋を閉じるときの重力 0 など)
    HEADER = ""       # 実行中のヘッダの頭
    SAVED = ""        # 確定したときの報告
    CANCELLED = ""    # 中止したときの報告

    def _make_dresser(self, context):
        obj = context.active_object
        return Dresser(obj, obj.muslin, sim_state.effective_dt(context.scene),
                       self.max_steps, auto_finish=self.AUTO_FINISH,
                       overrides=self.OVERRIDES)

    def _start(self, context):
        self._dresser = self._make_dresser(context)
        self._dressing = [sim_state.obj_key(m) for m in self._dresser.members]
        sim_state._dressing.update(self._dressing)
        for w in self._dresser.warnings:
            self.report({'WARNING'}, w)

    def _release(self):
        # 布の参照は Ctrl+Z などで無効になるので、key で外す
        sim_state._dressing.difference_update(getattr(self, "_dressing", ()))
        self._dressing = []

    def _end(self, context, commit):
        self._release()
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
        try:
            return self._modal(context, event)
        except LostCloth as exc:
            # 布が消えたら、書き戻す先も無いので静かに終える
            self._stop_timer(context)
            self._release()
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

    def _modal(self, context, event):
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
        self._stop_timer(context)
        return self._end(context, commit)

    def _stop_timer(self, context):
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if context.area is not None:
            context.area.header_text_set(None)


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


class MUSLIN_OT_close_bag(_ClothModal, bpy.types.Operator):
    """縫い目を閉じて、圧力で膨らませる。クッションなど、体に着せない閉じた立体のため

    Dress と同じ回し方だが、**重力を 0 にして回す**。体もコライダーも無い袋は、
    重力があるといつまでも落ち続けて落ち着かないため。重力の設定そのものは
    変えないので、この後の再生では元の重力で落ちる。
    Esc で中止(元の形に戻る)、Enter でその場で確定する。
    """

    bl_idname = "muslin.close_bag"
    bl_label = "Close Bag"
    bl_options = {'REGISTER', 'UNDO'}

    AUTO_FINISH = True
    # 体の無い袋は、重力があると落ち続けて落ち着かない。回している間だけ 0 にする
    OVERRIDES = {"gravity": 0.0}
    HEADER = "袋を閉じています"
    SAVED = "閉じた形を保存しました"
    CANCELLED = "袋を閉じるのをやめて元の形に戻しました"

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
        if not ui_poll.mesh_selected(cls, context):
            return False
        obj = context.active_object
        if not any(s.enabled for s in getattr(obj, "muslin_seams", [])):
            return ui_poll.reject(cls, "閉じる縫い目がありません(先に縫い目を作ってください)")
        return True

    def _start(self, context):
        super()._start(context)
        # 圧力が入っていなければ、閉じるだけで膨らまない。止めはしないが伝える
        if context.active_object.muslin.pressure == 0.0:
            self.report({'WARNING'},
                        "Pressure が 0 です。膨らませるには Fabric パネルの Pressure を入れてください")

    def _make_dresser(self, context):
        obj = context.active_object
        return BagDresser(obj, obj.muslin, sim_state.effective_dt(context.scene),
                          self.max_steps, auto_finish=self.AUTO_FINISH,
                          overrides=self.OVERRIDES)

    def _header(self):
        text = super()._header()
        phase = getattr(self._dresser, "phase", "")
        if phase == "measure":
            return text.replace(self.HEADER, self.HEADER + "(満杯の体積を測っています)", 1)
        if phase == "fill":
            return text.replace(self.HEADER, self.HEADER + "(詰め具合まで落ち着かせています)", 1)
        return text

    def execute(self, context):
        # スクリプトやテストから呼ばれたときは、最後まで同期で回す
        self._start(context)
        while not self._dresser.step():
            pass
        return self._end(context, commit=True)


_classes = (MUSLIN_OT_dress, MUSLIN_OT_adjust, MUSLIN_OT_close_bag)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

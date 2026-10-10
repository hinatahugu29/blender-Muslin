import bpy

from . import mesh_io
from . import pin_ops
from . import sim_state


class _MuslinPanelBase:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Muslin"


def _mesh_selected(context):
    obj = context.active_object
    return obj is not None and obj.type == 'MESH'


class _MuslinSettingsPanel(_MuslinPanelBase):
    """布ごとの設定を出すパネルの共通部分。

    設定はオブジェクトが持つので、メッシュが選ばれていないときは
    プロパティを出しようがない。その旨だけ表示する。
    """

    @classmethod
    def poll(cls, context):
        # 編集中はシミュレーション自体を止めている(編集用の BMesh が
        # 書き戻されて計算結果が捨てられるため)。止まっている設定を
        # 並べても何も起きないので隠し、代わりに Pattern / Sewing を前に出す。
        # 布(メッシュ)を選んでいないときも隠す(Curve の型紙を描いている間に、
        # 「メッシュを選択してください」だけのパネルが並んで場所を取っていた)
        return context.mode == 'OBJECT' and _mesh_selected(context)

    def draw(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.layout.label(text="メッシュを選択してください", icon='INFO')
            return
        # 純正のプロパティパネルと同じ 2 カラム配置にする。
        # アニメーション不可のプロパティばかりなのでデコレータ(右端の◇)は消す。
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        self.draw_cloth(context, self.layout, obj.muslin)

    @staticmethod
    def buttons(layout):
        """オペレータを全幅で置くための、分割を切った列を返す。"""
        col = layout.column()
        col.use_property_split = False
        return col

    def draw_cloth(self, context, layout, props):
        raise NotImplementedError


class MUSLIN_PT_main(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Muslin"
    bl_idname = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        # 再生の操作をここに置く。タイムラインへ目を移さずに回せるように
        # するためで、押すと必要に応じて Start Simulation も兼ねる。
        playing = context.screen is not None and context.screen.is_animation_playing
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator("muslin.rewind", text="", icon='REW')
        if playing:
            row.operator("muslin.play", text="Pause", icon='PAUSE')
        else:
            row.operator("muslin.play", text="Play", icon='PLAY')

        row = layout.row(align=True)
        if obj is not None and sim_state.is_running(obj):
            row.operator("muslin.stop_sim", icon='SNAP_FACE')
        else:
            row.operator("muslin.start_sim", icon='PHYSICS')
        # メッシュに直接書くので、元の形へ戻す手段を目に見える場所に置く。
        # Blender 標準のクロスはモディファイアなので外せば戻るが、こちらは
        # 戻せることが分からないと「壊れた」ように見える。
        row.operator("muslin.reset_shape", text="", icon='LOOP_BACK')

        # 型紙を作る段階か、着せた段階か。着せた段階では編集しても寸法が
        # 変わらないので、今どちらにいるかが見えないと混乱する。
        if obj is not None and obj.type == 'MESH':
            from . import rest_shape
            # 着せ付けは時間軸の外で回す。タイムラインで回した結果を
            # 保存したいときのために Save Dressed Pose も残す
            row = layout.row(align=True)
            row.operator("muslin.dress", icon='MOD_CLOTH')
            # 整える(つまむモード)は着せた後だけ押せる。押せない理由は poll が出す
            row.operator("muslin.adjust", icon='VIEW_PAN')
            if rest_shape.is_dressed(obj):
                row.operator("muslin.restore_pattern", text="", icon='BACK')
                layout.label(text="着せた姿勢から開始(寸法は型紙)", icon='CHECKMARK')
            else:
                row.operator("muslin.set_rest_shape", text="", icon='PINNED')
            # 圧力を入れている布は、体に着せるものではなく閉じた袋。重力を 0 にして
            # 閉じる Close Bag を出す(Dress だと落ち続けて落ち着かない)
            if obj.muslin.pressure != 0.0:
                layout.operator("muslin.close_bag", icon='SPHERE')

        # 走らせたままでは効かない設定を変えたときに知らせる。
        # 黙って効かないままだと「設定が壊れている」としか見えない。
        if obj is not None:
            reasons = sim_state.restart_reasons(obj, obj.muslin)
            if reasons:
                box = layout.box()
                box.alert = True
                box.label(text="組み立て直すまで反映されません", icon='ERROR')
                for reason in reasons:
                    box.label(text=reason)
                box.operator("muslin.restart_sim", icon='FILE_REFRESH')

        state = sim_state.get_state(obj)
        if state is not None:
            info = state["info"]
            box = layout.box()
            col = box.column(align=True)
            col.label(text=f"Frame: {state['current_frame']} (start {state['start_frame']})")
            col.label(text=f"Verts {info['vertices']} / Edges {info['edges']}")
            col.label(text=f"Pinned {info['pinned']} / Seams {info['seams']}")
            if info.get("colliders"):
                col.label(
                    text=f"Colliders {len(info['colliders'])} "
                         f"({info['collider_triangles']} tris)"
                )


class MUSLIN_PT_solver(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Solver"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):
        # まず品質の段。ここだけ触れば済むようにして、生の数値は
        # 下の Advanced に畳んである。
        layout.prop(props, "quality")

        # 1フレームが表す時間はシーン全体で共通
        tools = context.scene.muslin_tools
        col = layout.column(align=True)
        col.prop(tools, "use_scene_fps")
        sub = col.column(align=True)
        sub.enabled = not tools.use_scene_fps
        sub.prop(tools, "dt")

        layout.prop(props, "use_cache")


class MUSLIN_PT_solver_advanced(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Advanced"
    bl_parent_id = "MUSLIN_PT_solver"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_cloth(self, context, layout, props):
        # ここを触ると Quality の表示は Custom に落ちる
        col = layout.column(align=True)
        col.prop(props, "iterations")
        col.prop(props, "substeps")
        col.prop(props, "chebyshev_radius")
        col.prop(props, "post_collision_iterations")
        col.prop(props, "cache_broadphase")


class MUSLIN_PT_material(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Fabric"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):

        # サムネイルで選ばせる。生地の違いはシルエットに出るので、
        # 名前より絵の方が早い。
        col = self.buttons(layout)
        col.template_icon_view(props, "fabric_preset", show_labels=True, scale=5.0)

        col = layout.column(align=True)
        col.prop(props, "density")
        col.prop(props, "stretch_compliance")
        col.prop(props, "bending_compliance")
        col.prop(props, "damping")
        col.prop(props, "pressure")

        # 部位ごとの生地(マテリアルの Override Fabric)。値はマテリアルのプロパティで細かく変えられる
        obj = context.active_object
        slots = [s for s in getattr(obj, "material_slots", []) if s.material is not None]
        if slots:
            box = layout.box()
            box.label(text="部位ごとの生地(マテリアル)", icon='MATERIAL')
            for slot in slots:
                fabric = slot.material.muslin_fabric
                row = box.row(align=True)
                row.prop(fabric, "enabled", text=slot.material.name)
                if fabric.enabled:
                    row.prop(fabric, "fabric_preset", text="")
        if props.pressure != 0.0:
            # 圧力の決め方(M11 の第 2 段階)。Volume なら詰め具合で膨らみ方を決める
            row = layout.row(align=True)
            row.prop(props, "pressure_mode", expand=True)
            if props.pressure_mode == 'VOLUME':
                layout.prop(props, "fill", slider=True)
                if props.full_volume > 0.0:
                    layout.label(text=f"満杯の体積 {props.full_volume * 1000:.2f} L"
                                      f" → 目標 {props.fill * props.full_volume * 1000:.2f} L")
                else:
                    layout.label(text="満杯の体積は Close Bag で測ります", icon='INFO')

        # 硬い生地を選んでも Substeps が低いと曲げ制約が収束せず、どの生地も
        # 同じように垂れる。Start Simulation でも警告するが、生地を選んだ
        # その場で分からないと「プリセットが効いていない」としか見えない。
        # N パネルは幅が狭いので、ここでは短く出す。
        if mesh_io.needs_more_substeps_for_bending(props):
            box = self.buttons(layout).box()
            box.alert = True
            box.label(text="この硬さは出ません", icon='ERROR')
            box.label(text="Solver の Quality を High 以上に")

        # 圧力も伸び制約と綱引きになるので、Substeps が低いと膨らみが
        # 「生地が伸びた分」になり、強すぎると発散する。
        if mesh_io.needs_more_substeps_for_pressure(props):
            box = self.buttons(layout).box()
            box.alert = True
            box.label(text="この圧力は強すぎます", icon='ERROR')
            box.label(text="Solver の Quality を上げてください")


class MUSLIN_PT_forces(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Forces"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):

        col = layout.column(align=True)
        col.prop(props, "gravity")
        col.prop(props, "wind")
        col.prop(props, "use_force_fields")


class MUSLIN_PT_collision(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Collision"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):

        self.buttons(layout).operator("muslin.fit_thickness", icon='DRIVER_DISTANCE')

        col = layout.column(align=True)
        col.prop(props, "collision_enabled")
        sub = col.column(align=True)
        sub.enabled = props.collision_enabled
        sub.prop(props, "collider_object")
        sub.prop(props, "collider_collection")
        sub.prop(props, "collider_animated")
        sub.prop(props, "collision_thickness")
        sub.prop(props, "collision_friction")

        layout.separator()
        col = layout.column(align=True)
        col.prop(props, "self_collision_enabled")
        sub = col.column(align=True)
        sub.enabled = props.self_collision_enabled
        sub.prop(props, "self_collision_thickness")
        sub.prop(props, "continuous_self_collision")

        # 重ね着: 同じ Group の布をまとめて解き、Layer の内側を優先する
        layout.separator()
        col = layout.column(align=True)
        col.prop(props, "sim_group")
        sub = col.column(align=True)
        sub.enabled = bool(props.sim_group)
        sub.prop(props, "layer")
        if props.sim_group:
            obj = context.active_object
            members = mesh_io.group_members(obj)
            note = self.buttons(layout)
            note.label(text=f"一緒に解く布: {len(members)} 着(設定は {members[0].name})",
                       icon='MOD_CLOTH')

        layout.separator()
        col = layout.column(align=True)
        col.prop(props, "floor_enabled")
        sub = col.column(align=True)
        sub.enabled = props.floor_enabled
        sub.prop(props, "floor_z")
        sub.prop(props, "friction")


class MUSLIN_PT_pinning(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Pinning"
    bl_parent_id = "MUSLIN_PT_main"

    @classmethod
    def poll(cls, context):
        # ピンの操作はどれも頂点を選んでから使うので、編集モードの時だけ出す
        # (ピンの数は親パネルの状態の欄にも出ている)
        return context.mode == 'EDIT_MESH' and _mesh_selected(context)

    def draw_cloth(self, context, layout, props):
        obj = context.active_object
        layout.prop_search(props, "pin_vertex_group", obj, "vertex_groups")

        col = self.buttons(layout)
        if obj.mode == 'EDIT':
            # 選択した頂点からグループを作れるようにする。標準機能だと
            # オブジェクトデータのプロパティとの往復になる。
            row = col.row(align=True)
            row.operator("muslin.pin_selected", icon='PINNED')
            row.operator("muslin.unpin_selected", text="", icon='UNPINNED')
            col.operator("muslin.select_pinned", icon='RESTRICT_SELECT_OFF')
            col.label(text=f"ピン留め: {pin_ops.pinned_count(obj)} 頂点")
        else:
            col.label(text="編集モードで選択して作れます", icon='INFO')


class MUSLIN_UL_seams(bpy.types.UIList):
    """縫い目リスト"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.prop(item, "name", text="", emboss=False, icon='OUTLINER_DATA_GP_LAYER')
        if item.uid:
            # 新形式は辺の属性から数える。統合や細分化の後でも今の数が出る
            from . import seams
            codes, edges = mesh_io.object_seam_codes(data)
            if codes is None:
                row.label(text="!", icon='ERROR')
            else:
                side_a = seams.chains_from_codes(codes, edges, item.uid, 0)
                side_b = seams.chains_from_codes(codes, edges, item.uid, 1)
                if len(side_a) == 1 and len(side_b) == 1:
                    row.label(text=f"{len(side_a[0])}↔{len(side_b[0])}")
                else:
                    row.label(text="!", icon='ERROR')
            row.prop(item, "invert", text="", icon='ARROW_LEFTRIGHT')
            # 折り返し(袋の縁)。入っていると縫い目をまたぐ曲げ抵抗を掛けない
            row.prop(item, "folded", text="", icon='MOD_SOLIDIFY')
        else:
            row.label(text=f"{len(item.chain_a)}↔{len(item.chain_b)}")
            row.prop(item, "flipped", text="", icon='ARROW_LEFTRIGHT')


class MUSLIN_PT_pattern(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Pattern"
    bl_parent_id = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout

        # 型紙の輪郭を手で描く(Curve Pattern のピースになる)。これからの主な作り方なので先頭に
        # 布を選んでいる時は出さない(描き足すなら Curve を選ぶ)
        active = context.active_object
        if active is None or active.type == 'CURVE':
            row = layout.row(align=True)
            row.scale_y = 1.3
            row.operator("muslin.pattern_draw", icon='GREASEPENCIL').mirror = False
            row.operator("muslin.pattern_draw", text="", icon='MOD_MIRROR').mirror = True

        # 型紙の確定。胴のまわりに曲げて置く前に押す(曲げた形が型紙に
        # なるのを防ぐ)。確定したかどうかをここで見せる
        obj = context.active_object
        if obj is not None and obj.type == 'MESH' and context.mode == 'OBJECT':
            from . import rest_shape
            layout.separator()
            if rest_shape.is_pattern_locked(obj):
                row = layout.row(align=True)
                row.label(text="型紙は確定済み", icon='LOCKED')
                row.operator("muslin.unlock_pattern", text="", icon='UNLOCKED')
                layout.operator("muslin.restore_pattern", icon='MESH_GRID')
            else:
                layout.operator("muslin.lock_pattern", icon='LOCKED')
                layout.label(text="曲げて配置する前に確定する", icon='INFO')
            layout.operator("muslin.pattern_to_uv", icon='UV')

            # 型紙オブジェクト(M8)。結び付いていれば、編集が走っている布に流れる
            from . import pattern_link, sim_state
            layout.separator()
            if pattern_link.linked_curve(obj) is not None:
                layout.label(text="型紙は Curve が決めます(Curve Pattern)", icon='CURVE_DATA')
            elif pattern_link.linked_object(obj) is not None:
                layout.prop(obj.muslin, "pattern_object", text="型紙")
                layout.label(text="編集すると走っている布の寸法が変わる", icon='INFO')
                state = sim_state.get_state(obj)
                for warning in (state or {}).get("pattern_warnings", []):
                    layout.label(text=warning, icon='ERROR')
            else:
                layout.operator("muslin.create_pattern_object", icon='MOD_MESHDEFORM')


class MUSLIN_PT_mesh_pattern(_MuslinPanelBase, bpy.types.Panel):
    """メッシュで型紙を作る道具(長方形のピース、輪郭の塗りつぶし、ピースの統合)。

    Curve で描く道(Draw Pattern / Curve Pattern)と並ぶ、もう一つの作り方。ふだんは閉じておく。
    """

    bl_label = "Mesh Pattern"
    bl_parent_id = "MUSLIN_PT_pattern"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        tools = context.scene.muslin_tools
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(tools, "pattern_width")
        col.prop(tools, "pattern_height")
        col.prop(tools, "pattern_resolution")
        layout.operator("muslin.add_pattern_piece", icon='MESH_GRID')
        layout.operator("muslin.fill_outline", icon='MOD_TRIANGULATE')
        layout.separator()
        layout.operator("muslin.join_pieces", icon='AUTOMERGE_ON')
        layout.label(text="縫うピースは事前に統合が必要", icon='INFO')


def _curve_pattern_target(context):
    """Curve Pattern のパネルが扱う Curve(Curve か、そこから作った布を選んでいるとき)。"""
    from . import curve_pattern
    obj = context.active_object
    if obj is None:
        return None
    return obj if obj.type == 'CURVE' else curve_pattern.curve_of(obj)


class _CurvePatternChild(_MuslinPanelBase):
    """Curve Pattern の小分けのパネル。型紙として初期化済みで、描いている途中でないときだけ出す。"""

    bl_parent_id = "MUSLIN_PT_curve_pattern"

    @classmethod
    def poll(cls, context):
        from . import curve_pattern, pattern_draw
        curve = _curve_pattern_target(context)
        return (curve is not None and curve_pattern.load_record(curve) is not None
                and not pattern_draw.is_drawing(curve))


class MUSLIN_PT_curve_bag(_CurvePatternChild, bpy.types.Panel):
    """袋(クッション): 重ねる・マチ・キルティング。輪郭が 2 つ以上のときだけ。"""

    bl_label = "Bag"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        from . import curve_pattern
        if not super().poll(context):
            return False
        return curve_pattern.status(_curve_pattern_target(context)).get("panels", 0) >= 2

    def draw(self, context):
        from . import curve_pattern
        layout = self.layout
        curve = _curve_pattern_target(context)
        info = curve_pattern.status(curve) if curve is not None else {}
        if not info.get("initialized"):
            return
        layout.operator("muslin.curve_stack", icon='MOD_SOLIDIFY')
        if info.get("panels", 0) != 2:
            return
        # 表と裏の 2 枚の袋には、側面のマチ(帯)と、内部線のキルティングを足せる
        if info.get("gusset"):
            layout.operator("muslin.curve_gusset_remove", icon='X')
        else:
            layout.operator("muslin.curve_gusset_add", icon='MESH_CYLINDER')
        if info.get("lines"):
            layout.label(text=f"内部線 {info['lines']} 本", icon='IPO_LINEAR')
            layout.operator("muslin.curve_quilt", icon='MOD_LATTICE')
        else:
            layout.label(text="表の内側に開いた線を描くとキルティングできます", icon='INFO')
        if info.get("quilted"):
            layout.operator("muslin.curve_quilt_remove", icon='X')


class MUSLIN_PT_curve_seams(_CurvePatternChild, bpy.types.Panel):
    """Curve の区間で持つ縫い目。"""

    bl_label = "Seams"

    def draw(self, context):
        from . import curve_pattern
        layout = self.layout
        curve = _curve_pattern_target(context)
        info = curve_pattern.status(curve) if curve is not None else {}
        if not info.get("initialized"):
            return
        for uid, name in info["seams"]:
            row = layout.row(align=True)
            row.label(text=name, icon='UV_SYNC_SELECT')
            op = row.operator("muslin.curve_seam_remove", text="", icon='X')
            op.uid = uid
        if context.active_object is curve:
            layout.operator("muslin.curve_seam_add", icon='ADD')
            layout.label(text="編集モードで 2 か所の点の連なりを選ぶ", icon='INFO')
        else:
            layout.label(text="追加は Curve を選んで", icon='INFO')


class MUSLIN_PT_curve_elastic(_CurvePatternChild, bpy.types.Panel):
    """Curve の区間で持つゴム紐(Rebuild しても保たれる)。"""

    bl_label = "Elastic"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from . import curve_pattern
        layout = self.layout
        curve = _curve_pattern_target(context)
        info = curve_pattern.status(curve) if curve is not None else {}
        if not info.get("initialized"):
            return
        cloth = info["cloth"]
        items = {e.uid: e for e in cloth.muslin_elastics} if cloth is not None else {}
        for uid, name in info.get("elastics", []):
            row = layout.row(align=True)
            item = items.get(uid)
            if item is not None:
                row.prop(item, "enabled", text="")
                row.label(text=name, icon='MOD_SCREW')
                row.prop(item, "scale", text="")
            else:
                row.label(text=name, icon='MOD_SCREW')
            op = row.operator("muslin.curve_elastic_remove", text="", icon='X')
            op.uid = uid
        if context.active_object is curve:
            layout.operator("muslin.curve_elastic_add", icon='ADD')


class MUSLIN_PT_curve_pattern(_MuslinPanelBase, bpy.types.Panel):
    """Curve Pattern。Curve を型紙の一次データにし、布のメッシュはそこから作る。"""

    bl_label = "Curve Pattern"
    bl_parent_id = "MUSLIN_PT_main"

    @classmethod
    def poll(cls, context):
        from . import curve_pattern
        obj = context.active_object
        return obj is not None and (obj.type == 'CURVE' or curve_pattern.curve_of(obj) is not None)

    def draw(self, context):
        from . import curve_pattern, sim_state
        layout = self.layout
        obj = context.active_object
        if obj is None:
            return
        curve = obj if obj.type == 'CURVE' else curve_pattern.curve_of(obj)
        if curve is None:
            return
        info = curve_pattern.status(curve)

        # 描いている途中(パターン描画モード)
        from . import pattern_draw
        if pattern_draw.is_drawing(curve):
            box = layout.box()
            box.label(text="描いています(閉じた線を描いて確定)", icon='GREASEPENCIL')
            row = box.row(align=True)
            row.operator("muslin.pattern_draw_mirror", text="Mirror",
                         icon='CHECKBOX_HLT' if curve.get(pattern_draw.MIRROR_KEY) else 'CHECKBOX_DEHLT')
            row = box.row(align=True)
            row.scale_y = 1.4
            row.operator("muslin.pattern_draw_finish", icon='CHECKMARK')
            row.operator("muslin.pattern_draw_cancel", text="", icon='X')
            return

        if obj.type != 'CURVE':
            row = layout.row()
            row.label(text=f"型紙: Curve '{curve.name}'", icon='CURVE_DATA')
        if not info["initialized"]:
            layout.label(text="閉じた Bezier の輪郭を型紙にできます", icon='INFO')
            layout.label(text="ピースは XY 平面に重ならないよう並べる")
            layout.operator("muslin.curve_pattern_init", icon='CURVE_BEZCIRCLE')
            return

        cloth = info["cloth"]
        running = cloth is not None and sim_state.is_running(cloth)
        col = layout.column(align=True)
        col.label(text=f"ピース {info['pieces']} / 目標の辺 {info['target'] * 1000:.0f} mm"
                       + (f" / 世代 {info['gen_id']}" if info["gen_id"] else ""))
        if cloth is None:
            col.label(text="布はまだありません(Rebuild で作る)", icon='INFO')

        # 状態: 追従できているか、作り直しが要るか(走行中は Shape Update だけ許す)
        problems = list(info["problems"])
        state = sim_state.get_state(cloth) if cloth is not None else None
        for warning in (state or {}).get("pattern_warnings", []):
            if warning not in problems:
                problems.append(warning)
        if problems and cloth is not None:
            box = layout.box()
            box.label(text="Rebuild Required", icon='ERROR')
            for reason in problems[:4]:
                box.label(text=reason)
            if running:
                box.label(text="走行中は作り直せません。停止してから Rebuild してください")
        elif cloth is not None:
            layout.label(text="Curve の変更は布に追従します", icon='CHECKMARK')

        layout.operator("muslin.curve_pattern_rebuild", icon='FILE_REFRESH')
        # 型紙(左・真上から固定)と布(右)を並べて見る。もう一度押すと元に戻す
        layout.operator("muslin.pattern_view", icon='WINDOW')
        if cloth is not None:
            layout.operator("muslin.curve_arrange", icon='MOD_CLOTH')
        layout.prop(context.scene.muslin_tools, "show_curve_outline")


class MUSLIN_PT_sewing(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Sewing"
    bl_parent_id = "MUSLIN_PT_main"

    @classmethod
    def poll(cls, context):
        return _mesh_selected(context)

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon='INFO')
            return
        props = obj.muslin
        tools = context.scene.muslin_tools

        from . import curve_pattern
        from_curve = curve_pattern.curve_of(obj) is not None
        if from_curve:
            # 縫い目は Curve の区間で持つ。ここに出るのは布へ展開したもので、足したり消したり
            # すると Curve の記録と食い違うので、向きと折り返しの切り替えだけにする
            layout.label(text="縫い目の追加・削除は Curve Pattern で", icon='INFO')
        else:
            layout.label(text="編集モードで2本の縫い代エッジを選択:")
            layout.operator("muslin.add_seam", icon='ADD')

        row = layout.row()
        row.template_list(
            "MUSLIN_UL_seams", "", obj, "muslin_seams", obj, "muslin_seam_active", rows=3
        )
        col = row.column(align=True)
        if not from_curve:
            col.operator("muslin.remove_seam", text="", icon='REMOVE')
            col.operator("muslin.clear_seams", text="", icon='TRASH')
        col.operator("muslin.select_seam", text="", icon='RESTRICT_SELECT_OFF')

        if not from_curve:
            layout.operator("muslin.validate_seams", icon='CHECKMARK')

        layout.separator()
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(tools, "show_seams")
        col.prop(props, "seam_close_frames")
        col.prop(props, "seam_compliance")
        col.prop(props, "weld_seams")
        if props.weld_seams:
            col.prop(props, "weld_distance")
            layout.operator("muslin.update_weld", icon='FILE_REFRESH')


class MUSLIN_UL_elastics(bpy.types.UIList):
    """ゴム紐リスト"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.prop(item, "name", text="", emboss=False, icon='MOD_WAVE')
        row.prop(item, "scale", text="")


class MUSLIN_PT_elastic(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Elastic"
    bl_parent_id = "MUSLIN_PT_sewing"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon='INFO')
            return
        from . import curve_pattern
        if curve_pattern.curve_of(obj) is not None:
            # Curve から作った布は、Curve 側で付けると Rebuild しても保たれる
            layout.label(text="Curve Pattern の布: ゴム紐は Curve で付けると保たれます", icon='INFO')
        layout.label(text="編集モードでゴムを入れる辺を選択:")
        layout.operator("muslin.add_elastic", icon='ADD')
        row = layout.row()
        row.template_list(
            "MUSLIN_UL_elastics", "", obj, "muslin_elastics", obj, "muslin_elastic_active", rows=2
        )
        row.column(align=True).operator("muslin.remove_elastic", text="", icon='REMOVE')
        layout.label(text="Length 0.8 = 2割縮める(走らせたまま変えられる)", icon='INFO')


class MUSLIN_PT_bake(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Bake"
    bl_parent_id = "MUSLIN_PT_main"

    @classmethod
    def poll(cls, context):
        return _mesh_selected(context)

    def draw(self, context):
        from . import bake_ops
        from . import cache_io
        from . import rest_shape

        layout = self.layout
        scene = context.scene
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon='INFO')
            return

        col = layout.column(align=True)
        col.label(text=f"範囲: {scene.frame_start} - {scene.frame_end}")

        if bake_ops.is_baked(obj):
            start, end = bake_ops.baked_range(obj)
            directory = obj.get("muslin_cache_dir", "")
            size_mb = cache_io.cache_size_bytes(directory) / (1024 * 1024)

            box = layout.box()
            box_col = box.column(align=True)
            box_col.label(text=f"ベイク済み: {start} - {end}", icon='CHECKMARK')
            box_col.label(text=f"サイズ: {size_mb:.1f} MB")

            layout.operator("muslin.bake", text="Re-bake", icon='FILE_REFRESH')
            layout.operator("muslin.free_bake", icon='TRASH')
            layout.separator()
            layout.operator("muslin.bake_to_shape_keys", icon='SHAPEKEY_DATA')
        else:
            layout.operator("muslin.bake", icon='PHYSICS')
            if obj.get("muslin_cache_dir", ""):
                # シェイプキー変換後など、再生は止めたがキャッシュは残っている状態
                layout.operator("muslin.free_bake", icon='TRASH')
            if not bpy.data.filepath:
                layout.label(text=".blend 未保存 → 一時領域に保存", icon='ERROR')


class MUSLIN_PT_debug(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Debug"
    bl_parent_id = "MUSLIN_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is not None and obj.type == 'MESH':
            from . import rest_shape
            col = layout.column(align=True)
            col.label(
                text="元の形: " + ("記録あり" if rest_shape.has_rest(obj) else "未記録"),
                icon='MESH_DATA',
            )
            col.label(
                text="型紙: " + ("記録あり" if rest_shape.has_pattern(obj) else "未記録")
                + (" / 着せた段階" if rest_shape.is_dressed(obj) else ""),
                icon='MESH_GRID',
            )
            layout.separator()

        state = sim_state.get_state(obj) if obj is not None else None
        if state is not None:
            col = layout.column(align=True)
            col.label(text=f"Stretch error: {state['last_error']:.4f}")
            col.label(text=f"Contacts: {state.get('last_contacts', 0)}")
            layout.separator()

        layout.operator("muslin.test_rust", icon='CONSOLE')
        layout.operator("muslin.self_test", icon='CHECKMARK')
        layout.operator("muslin.print_timings", icon='TIME')


class MUSLIN_PT_part_fabric(bpy.types.Panel):
    """マテリアルのプロパティに出す、部位ごとの生地。"""

    bl_label = "Muslin Fabric"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"
    bl_options = {'DEFAULT_CLOSED'}

    @staticmethod
    def _material(context):
        mat = getattr(context, "material", None)
        if mat is None:
            obj = getattr(context, "active_object", None)
            mat = getattr(obj, "active_material", None)
        return mat

    @classmethod
    def poll(cls, context):
        return cls._material(context) is not None

    def draw_header(self, context):
        self.layout.prop(self._material(context).muslin_fabric, "enabled", text="")

    def draw(self, context):
        mat = self._material(context)
        if mat is None:
            return
        fabric = mat.muslin_fabric
        layout = self.layout
        layout.use_property_split = True
        layout.active = fabric.enabled
        layout.label(text="このマテリアルの面だけ Muslin の生地を変える", icon='INFO')
        layout.prop(fabric, "fabric_preset")
        col = layout.column(align=True)
        col.prop(fabric, "density")
        col.prop(fabric, "stretch_compliance")
        col.prop(fabric, "bending_compliance")


# 登録した順に並ぶ。作業の順(型紙 → 縫う → 生地 → 衝突 → 力 → ピン → 解き方 → ベイク)にする
_classes = (
    MUSLIN_UL_seams,
    MUSLIN_UL_elastics,
    MUSLIN_PT_main,
    MUSLIN_PT_material,
    MUSLIN_PT_pattern,
    MUSLIN_PT_mesh_pattern,
    MUSLIN_PT_curve_pattern,
    MUSLIN_PT_curve_bag,
    MUSLIN_PT_curve_seams,
    MUSLIN_PT_curve_elastic,
    MUSLIN_PT_sewing,
    MUSLIN_PT_elastic,
    MUSLIN_PT_collision,
    MUSLIN_PT_forces,
    MUSLIN_PT_pinning,
    MUSLIN_PT_solver,
    MUSLIN_PT_solver_advanced,
    MUSLIN_PT_bake,
    MUSLIN_PT_debug,
    MUSLIN_PT_part_fabric,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

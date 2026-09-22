import bpy

from . import mesh_io
from . import pin_ops
from . import sim_state


class _MuslinPanelBase:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Muslin"


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
        return context.mode == 'OBJECT'

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
                row.operator("muslin.restore_pattern", text="", icon='MESH_GRID')
                layout.label(text="着せた姿勢から開始(寸法は型紙)", icon='CHECKMARK')
            else:
                row.operator("muslin.set_rest_shape", text="", icon='PINNED')

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
            col.label(text=f"Stretch error: {state['last_error']:.4f}")
            col.label(text=f"Contacts: {state.get('last_contacts', 0)}")


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

        # 硬い生地を選んでも Substeps が低いと曲げ制約が収束せず、どの生地も
        # 同じように垂れる。Start Simulation でも警告するが、生地を選んだ
        # その場で分からないと「プリセットが効いていない」としか見えない。
        # N パネルは幅が狭いので、ここでは短く出す。
        if mesh_io.needs_more_substeps_for_bending(props):
            box = self.buttons(layout).box()
            box.alert = True
            box.label(text="この硬さは出ません", icon='ERROR')
            box.label(text="Solver の Quality を High 以上に")


class MUSLIN_PT_forces(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Forces"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):

        col = layout.column(align=True)
        col.prop(props, "gravity")
        col.prop(props, "wind")


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
        # 頂点グループは編集モードで作るので、ここだけは編集中も出す
        return context.mode in {'OBJECT', 'EDIT_MESH'}

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
        else:
            row.label(text=f"{len(item.chain_a)}↔{len(item.chain_b)}")
            row.prop(item, "flipped", text="", icon='ARROW_LEFTRIGHT')


class MUSLIN_PT_pattern(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Pattern"
    bl_parent_id = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout
        tools = context.scene.muslin_tools

        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(tools, "pattern_width")
        col.prop(tools, "pattern_height")
        col.prop(tools, "pattern_resolution")
        layout.separator()

        layout.operator("muslin.add_pattern_piece", icon='MESH_GRID')
        layout.operator("muslin.fill_outline", icon='MOD_TRIANGULATE')
        layout.separator()
        layout.operator("muslin.join_pieces", icon='AUTOMERGE_ON')
        layout.label(text="縫うピースは事前に統合が必要", icon='INFO')

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


class MUSLIN_PT_sewing(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Sewing"
    bl_parent_id = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon='INFO')
            return
        props = obj.muslin
        tools = context.scene.muslin_tools

        layout.label(text="編集モードで2本の縫い代エッジを選択:")
        layout.operator("muslin.add_seam", icon='ADD')

        row = layout.row()
        row.template_list(
            "MUSLIN_UL_seams", "", obj, "muslin_seams", obj, "muslin_seam_active", rows=3
        )
        col = row.column(align=True)
        col.operator("muslin.remove_seam", text="", icon='REMOVE')
        col.operator("muslin.clear_seams", text="", icon='TRASH')
        col.operator("muslin.select_seam", text="", icon='RESTRICT_SELECT_OFF')

        layout.operator("muslin.validate_seams", icon='CHECKMARK')

        layout.separator()
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(tools, "show_seams")
        col.prop(props, "seam_close_frames")
        col.prop(props, "seam_compliance")


class MUSLIN_PT_bake(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Bake"
    bl_parent_id = "MUSLIN_PT_main"

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

        layout.operator("muslin.test_rust", icon='CONSOLE')
        layout.operator("muslin.self_test", icon='CHECKMARK')
        layout.operator("muslin.print_timings", icon='TIME')


_classes = (
    MUSLIN_UL_seams,
    MUSLIN_PT_main,
    MUSLIN_PT_solver,
    MUSLIN_PT_solver_advanced,
    MUSLIN_PT_material,
    MUSLIN_PT_forces,
    MUSLIN_PT_collision,
    MUSLIN_PT_pinning,
    MUSLIN_PT_pattern,
    MUSLIN_PT_sewing,
    MUSLIN_PT_bake,
    MUSLIN_PT_debug,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

import bpy

from . import mesh_io
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

    def draw(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.layout.label(text="メッシュを選択してください", icon='INFO')
            return
        self.draw_cloth(context, self.layout, obj.muslin)

    def draw_cloth(self, context, layout, props):
        raise NotImplementedError


class MUSLIN_PT_main(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Muslin"
    bl_idname = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        row = layout.row(align=True)
        if obj is not None and sim_state.is_running(obj):
            row.operator("muslin.stop_sim", icon='PAUSE')
        else:
            row.operator("muslin.start_sim", icon='PLAY')

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
        # 1フレームが表す時間はシーン全体で共通
        tools = context.scene.muslin_tools
        col = layout.column(align=True)
        col.prop(tools, "use_scene_fps")
        sub = col.column(align=True)
        sub.enabled = not tools.use_scene_fps
        sub.prop(tools, "dt")

        col = layout.column(align=True)
        col.prop(props, "iterations")
        col.prop(props, "substeps")
        col.prop(props, "chebyshev_radius")
        col.prop(props, "post_collision_iterations")
        col.prop(props, "cache_broadphase")
        col.prop(props, "use_cache")


class MUSLIN_PT_material(_MuslinSettingsPanel, bpy.types.Panel):
    bl_label = "Fabric"
    bl_parent_id = "MUSLIN_PT_main"

    def draw_cloth(self, context, layout, props):

        layout.prop(props, "fabric_preset")

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
            box = layout.box()
            box.alert = True
            box.label(text="この硬さは出ません", icon='ERROR')
            box.label(
                text=f"Solver の Substeps を {mesh_io.SUBSTEPS_FOR_STIFF_FABRIC} 以上に"
            )


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

        layout.operator("muslin.fit_thickness", icon='DRIVER_DISTANCE')

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

    def draw_cloth(self, context, layout, props):
        layout.prop_search(props, "pin_vertex_group", context.active_object,
                           "vertex_groups")


class MUSLIN_UL_seams(bpy.types.UIList):
    """縫い目リスト"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.prop(item, "name", text="", emboss=False, icon='OUTLINER_DATA_GP_LAYER')
        row.label(text=f"{len(item.chain_a)}↔{len(item.chain_b)}")
        row.prop(item, "flipped", text="", icon='ARROW_LEFTRIGHT')


class MUSLIN_PT_pattern(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Pattern"
    bl_parent_id = "MUSLIN_PT_main"

    def draw(self, context):
        layout = self.layout
        tools = context.scene.muslin_tools

        col = layout.column(align=True)
        col.prop(tools, "pattern_width")
        col.prop(tools, "pattern_height")
        col.prop(tools, "pattern_resolution")

        layout.operator("muslin.add_pattern_piece", icon='MESH_GRID')
        layout.operator("muslin.fill_outline", icon='MOD_TRIANGULATE')
        layout.separator()
        layout.operator("muslin.join_pieces", icon='AUTOMERGE_ON')
        layout.label(text="縫うピースは事前に統合が必要", icon='INFO')


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

        col = layout.column(align=True)
        col.prop(tools, "show_seams")
        col.prop(props, "seam_close_frames")
        col.prop(props, "seam_compliance")


class MUSLIN_PT_bake(_MuslinPanelBase, bpy.types.Panel):
    bl_label = "Bake"
    bl_parent_id = "MUSLIN_PT_main"

    def draw(self, context):
        from . import bake_ops
        from . import cache_io

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
        layout.operator("muslin.test_rust", icon='CONSOLE')
        layout.operator("muslin.self_test", icon='CHECKMARK')
        layout.operator("muslin.print_timings", icon='TIME')


_classes = (
    MUSLIN_UL_seams,
    MUSLIN_PT_main,
    MUSLIN_PT_solver,
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

import bpy

from . import sim_state


class _ClothMDPanelBase:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Cloth MD"


class CLOTHMD_PT_main(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Cloth MD"
    bl_idname = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        row = layout.row(align=True)
        if obj is not None and sim_state.is_running(obj):
            row.operator("cloth_md.stop_sim", icon='PAUSE')
        else:
            row.operator("cloth_md.start_sim", icon='PLAY')

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


class CLOTHMD_PT_solver(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Solver"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props

        col = layout.column(align=True)
        col.prop(props, "use_scene_fps")
        sub = col.column(align=True)
        sub.enabled = not props.use_scene_fps
        sub.prop(props, "dt")

        col = layout.column(align=True)
        col.prop(props, "iterations")
        col.prop(props, "substeps")
        col.prop(props, "use_cache")


class CLOTHMD_PT_material(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Fabric"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props

        col = layout.column(align=True)
        col.prop(props, "density")
        col.prop(props, "stretch_compliance")
        col.prop(props, "bending_compliance")
        col.prop(props, "damping")


class CLOTHMD_PT_forces(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Forces"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props

        col = layout.column(align=True)
        col.prop(props, "gravity")
        col.prop(props, "wind")


class CLOTHMD_PT_collision(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Collision"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props

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

        layout.separator()
        col = layout.column(align=True)
        col.prop(props, "floor_enabled")
        sub = col.column(align=True)
        sub.enabled = props.floor_enabled
        sub.prop(props, "floor_z")
        sub.prop(props, "friction")


class CLOTHMD_PT_pinning(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Pinning"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props
        obj = context.active_object

        if obj is not None and obj.type == 'MESH':
            layout.prop_search(props, "pin_vertex_group", obj, "vertex_groups")
        else:
            layout.label(text="メッシュを選択してください", icon='INFO')


class CLOTHMD_UL_seams(bpy.types.UIList):
    """縫い目リスト"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.prop(item, "name", text="", emboss=False, icon='OUTLINER_DATA_GP_LAYER')
        row.label(text=f"{len(item.chain_a)}↔{len(item.chain_b)}")
        row.prop(item, "flipped", text="", icon='ARROW_LEFTRIGHT')


class CLOTHMD_PT_pattern(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Pattern"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props

        col = layout.column(align=True)
        col.prop(props, "pattern_width")
        col.prop(props, "pattern_height")
        col.prop(props, "pattern_resolution")

        layout.operator("cloth_md.add_pattern_piece", icon='MESH_GRID')
        layout.operator("cloth_md.fill_outline", icon='MOD_TRIANGULATE')
        layout.separator()
        layout.operator("cloth_md.join_pieces", icon='AUTOMERGE_ON')
        layout.label(text="縫うピースは事前に統合が必要", icon='INFO')


class CLOTHMD_PT_sewing(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Sewing"
    bl_parent_id = "CLOTHMD_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.cloth_md_props
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon='INFO')
            return

        layout.label(text="編集モードで2本の縫い代エッジを選択:")
        layout.operator("cloth_md.add_seam", icon='ADD')

        row = layout.row()
        row.template_list(
            "CLOTHMD_UL_seams", "", obj, "cloth_md_seams", obj, "cloth_md_seam_active", rows=3
        )
        col = row.column(align=True)
        col.operator("cloth_md.remove_seam", text="", icon='REMOVE')
        col.operator("cloth_md.clear_seams", text="", icon='TRASH')
        col.operator("cloth_md.select_seam", text="", icon='RESTRICT_SELECT_OFF')

        layout.operator("cloth_md.validate_seams", icon='CHECKMARK')

        col = layout.column(align=True)
        col.prop(props, "show_seams")
        col.prop(props, "seam_close_frames")
        col.prop(props, "seam_compliance")


class CLOTHMD_PT_bake(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Bake"
    bl_parent_id = "CLOTHMD_PT_main"

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
            directory = obj.get("cloth_md_cache_dir", "")
            size_mb = cache_io.cache_size_bytes(directory) / (1024 * 1024)

            box = layout.box()
            box_col = box.column(align=True)
            box_col.label(text=f"ベイク済み: {start} - {end}", icon='CHECKMARK')
            box_col.label(text=f"サイズ: {size_mb:.1f} MB")

            layout.operator("cloth_md.bake", text="Re-bake", icon='FILE_REFRESH')
            layout.operator("cloth_md.free_bake", icon='TRASH')
            layout.separator()
            layout.operator("cloth_md.bake_to_shape_keys", icon='SHAPEKEY_DATA')
        else:
            layout.operator("cloth_md.bake", icon='PHYSICS')
            if obj.get("cloth_md_cache_dir", ""):
                # シェイプキー変換後など、再生は止めたがキャッシュは残っている状態
                layout.operator("cloth_md.free_bake", icon='TRASH')
            if not bpy.data.filepath:
                layout.label(text=".blend 未保存 → 一時領域に保存", icon='ERROR')


class CLOTHMD_PT_debug(_ClothMDPanelBase, bpy.types.Panel):
    bl_label = "Debug"
    bl_parent_id = "CLOTHMD_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.operator("cloth_md.test_rust", icon='CONSOLE')
        layout.operator("cloth_md.self_test", icon='CHECKMARK')


_classes = (
    CLOTHMD_UL_seams,
    CLOTHMD_PT_main,
    CLOTHMD_PT_solver,
    CLOTHMD_PT_material,
    CLOTHMD_PT_forces,
    CLOTHMD_PT_collision,
    CLOTHMD_PT_pinning,
    CLOTHMD_PT_pattern,
    CLOTHMD_PT_sewing,
    CLOTHMD_PT_bake,
    CLOTHMD_PT_debug,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

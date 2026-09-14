"""縫製ワークフロー(M3)のオペレータ: パターンピース作成とシーム編集。"""

import bmesh
import bpy
from mathutils import Vector

from . import mesh_io
from . import seams


# ---------------------------------------------------------- ヘルパー

def _bm_world_positions(bm, matrix_world):
    """bmesh の全頂点をワールド座標のフラット配列で返す。"""
    positions = [0.0] * (len(bm.verts) * 3)
    for v in bm.verts:
        co = matrix_world @ v.co
        base = v.index * 3
        positions[base] = co.x
        positions[base + 1] = co.y
        positions[base + 2] = co.z
    return positions


def _store_chain(collection, indices):
    collection.clear()
    for i in indices:
        item = collection.add()
        item.index = i


# ------------------------------------------------------ パターンピース作成

class CLOTHMD_OT_add_pattern_piece(bpy.types.Operator):
    """指定サイズの長方形パターンピースを作成する(3Dカーソル位置、正面向き)"""

    bl_idname = "cloth_md.add_pattern_piece"
    bl_label = "Add Pattern Piece"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.cloth_md_tools
        width = props.pattern_width
        height = props.pattern_height
        resolution = max(props.pattern_resolution, 1e-4)

        # 目標エッジ長から分割数を決める
        nx = max(int(round(width / resolution)), 1) + 1
        ny = max(int(round(height / resolution)), 1) + 1

        mesh = bpy.data.meshes.new("PatternPiece")
        bm = bmesh.new()

        # 衣服のパターンは体の正面に立てて配置するので XZ 平面に作る
        verts = []
        for iz in range(ny):
            for ix in range(nx):
                x = (ix / (nx - 1) - 0.5) * width if nx > 1 else 0.0
                z = (iz / (ny - 1) - 0.5) * height if ny > 1 else 0.0
                verts.append(bm.verts.new(Vector((x, 0.0, z))))
        bm.verts.ensure_lookup_table()

        for iz in range(ny - 1):
            for ix in range(nx - 1):
                a = verts[iz * nx + ix]
                b = verts[iz * nx + ix + 1]
                c = verts[(iz + 1) * nx + ix + 1]
                d = verts[(iz + 1) * nx + ix]
                bm.faces.new((a, b, c, d))

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        obj = bpy.data.objects.new("PatternPiece", mesh)
        context.collection.objects.link(obj)
        obj.location = context.scene.cursor.location

        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        self.report({'INFO'}, f"パターンピース作成: {nx}x{ny} = {nx * ny} 頂点")
        return {'FINISHED'}


class CLOTHMD_OT_fill_outline(bpy.types.Operator):
    """選択した閉じたエッジループの内側を三角形で埋め、パターンピースにする"""

    bl_idname = "cloth_md.fill_outline"
    bl_label = "Fill Outline as Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'

    def execute(self, context):
        obj = context.active_object
        props = context.scene.cloth_md_tools
        target_length = max(props.pattern_resolution, 1e-4)

        bm = bmesh.from_edit_mesh(obj.data)
        selected_edges = [e for e in bm.edges if e.select]
        if len(selected_edges) < 3:
            self.report({'ERROR'}, "閉じたエッジループを選択してください")
            return {'CANCELLED'}

        result = bmesh.ops.triangle_fill(
            bm, use_beauty=True, use_dissolve=False, edges=selected_edges
        )
        new_faces = [f for f in result.get("geom", []) if isinstance(f, bmesh.types.BMFace)]
        if not new_faces:
            self.report({'ERROR'}, "面を作れませんでした(輪郭が閉じているか確認してください)")
            return {'CANCELLED'}

        # 目標エッジ長になるまで細分化する(暴走しないよう回数を制限)
        for _ in range(8):
            long_edges = [
                e for e in bm.edges
                if e.calc_length() > target_length * 1.5 and any(f in new_faces for f in e.link_faces)
            ]
            if not long_edges:
                break
            split = bmesh.ops.subdivide_edges(bm, edges=long_edges, cuts=1, use_grid_fill=True)
            new_faces = [
                f for f in split.get("geom_split", []) + new_faces
                if isinstance(f, bmesh.types.BMFace) and f.is_valid
            ]

        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"パターン化しました({len(bm.verts)} 頂点)")
        return {'FINISHED'}


# -------------------------------------------------------------- シーム編集

class CLOTHMD_OT_join_pieces(bpy.types.Operator):
    """選択したパターンピースを1つのオブジェクトに統合する

    縫い目は同一メッシュ内の頂点インデックスで定義されるため、
    縫い合わせたいピースは事前に統合しておく必要がある。
    """

    bl_idname = "cloth_md.join_pieces"
    bl_label = "Join Pattern Pieces"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        return context.mode == 'OBJECT' and len(meshes) >= 2

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']

        # 統合すると頂点インデックスがずれるので、既存の縫い目は無効になる
        existing = sum(len(getattr(o, "cloth_md_seams", [])) for o in meshes)

        active = context.view_layer.objects.active
        if active not in meshes:
            active = meshes[0]
            context.view_layer.objects.active = active

        bpy.ops.object.join()

        if existing:
            active.cloth_md_seams.clear()
            self.report(
                {'WARNING'},
                f"{len(meshes)} ピースを統合しました。"
                f"頂点番号が変わるため既存の縫い目 {existing} 本は削除しました",
            )
        else:
            self.report({'INFO'}, f"{len(meshes)} ピースを統合しました")
        return {'FINISHED'}


class CLOTHMD_OT_add_seam(bpy.types.Operator):
    """編集モードで選択した2本のエッジ列を1本の縫い目として登録する"""

    bl_idname = "cloth_md.add_seam"
    bl_label = "Add Seam From Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'

    def execute(self, context):
        obj = context.active_object

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()

        selected = [(e.verts[0].index, e.verts[1].index) for e in bm.edges if e.select]
        if not selected:
            self.report({'ERROR'}, "エッジが選択されていません")
            return {'CANCELLED'}

        chains = seams.split_edges_into_chains(selected)
        if len(chains) != 2:
            self.report(
                {'ERROR'},
                f"繋がったエッジ列がちょうど2本必要です(検出: {len(chains)}本)。"
                "分岐のあるエッジ列は使えません",
            )
            return {'CANCELLED'}

        positions = _bm_world_positions(bm, obj.matrix_world)
        chain_a, chain_b = chains
        flipped = seams.should_flip(chain_a, chain_b, positions)

        seam = obj.cloth_md_seams.add()
        seam.name = f"Seam {len(obj.cloth_md_seams)}"
        _store_chain(seam.chain_a, chain_a)
        _store_chain(seam.chain_b, chain_b)
        seam.flipped = flipped
        obj.cloth_md_seam_active = len(obj.cloth_md_seams) - 1

        length_a = seams.seam_length(chain_a, positions)
        length_b = seams.seam_length(chain_b, positions)
        pairs = seams.pair_chains(chain_a, chain_b, positions, flipped)

        print(
            f"[cloth_md] シーム追加: {len(chain_a)}頂点({length_a:.3f}m) <-> "
            f"{len(chain_b)}頂点({length_b:.3f}m), ペア {len(pairs)}, flipped={flipped}"
        )
        self.report(
            {'INFO'},
            f"{seam.name}: {len(chain_a)} <-> {len(chain_b)} 頂点 / {len(pairs)} ペア"
            + (" (反転)" if flipped else ""),
        )
        return {'FINISHED'}


class CLOTHMD_OT_remove_seam(bpy.types.Operator):
    """選択中の縫い目を削除する"""

    bl_idname = "cloth_md.remove_seam"
    bl_label = "Remove Seam"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and len(getattr(obj, "cloth_md_seams", [])) > 0

    def execute(self, context):
        obj = context.active_object
        index = obj.cloth_md_seam_active
        if 0 <= index < len(obj.cloth_md_seams):
            obj.cloth_md_seams.remove(index)
            obj.cloth_md_seam_active = max(0, index - 1)
        return {'FINISHED'}


class CLOTHMD_OT_clear_seams(bpy.types.Operator):
    """このオブジェクトの縫い目をすべて削除する"""

    bl_idname = "cloth_md.clear_seams"
    bl_label = "Clear All Seams"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and len(getattr(obj, "cloth_md_seams", [])) > 0

    def execute(self, context):
        count = len(context.active_object.cloth_md_seams)
        context.active_object.cloth_md_seams.clear()
        self.report({'INFO'}, f"{count} 本の縫い目を削除しました")
        return {'FINISHED'}


class CLOTHMD_OT_select_seam(bpy.types.Operator):
    """選択中の縫い目に含まれる頂点を、編集モードで選択する"""

    bl_idname = "cloth_md.select_seam"
    bl_label = "Select Seam Vertices"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.mode == 'EDIT'
            and len(getattr(obj, "cloth_md_seams", [])) > 0
        )

    def execute(self, context):
        obj = context.active_object
        index = obj.cloth_md_seam_active
        if not (0 <= index < len(obj.cloth_md_seams)):
            return {'CANCELLED'}

        seam = obj.cloth_md_seams[index]
        chain_a, chain_b = mesh_io.seam_chains(seam)
        wanted = set(chain_a) | set(chain_b)

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        for v in bm.verts:
            v.select_set(v.index in wanted)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)

        self.report({'INFO'}, f"{len(wanted)} 頂点を選択しました")
        return {'FINISHED'}


class CLOTHMD_OT_validate_seams(bpy.types.Operator):
    """全シームの状態(ペア数・縫い目長・長さの食い違い)を検査して報告する"""

    bl_idname = "cloth_md.validate_seams"
    bl_label = "Validate Seams"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and len(getattr(obj, "cloth_md_seams", [])) > 0

    def execute(self, context):
        obj = context.active_object
        positions = mesh_io.get_world_positions(obj)
        vertex_count = len(obj.data.vertices)

        problems = []
        total_pairs = 0

        print("[cloth_md] ---- seam validation ----")
        for seam in obj.cloth_md_seams:
            chain_a, chain_b = mesh_io.seam_chains(seam)

            if any(i >= vertex_count for i in chain_a + chain_b):
                problems.append(f"{seam.name}: 存在しない頂点を参照(メッシュ編集後は作り直しが必要)")
                continue

            length_a = seams.seam_length(chain_a, positions)
            length_b = seams.seam_length(chain_b, positions)
            pairs = seams.pair_chains(chain_a, chain_b, positions, seam.flipped)
            total_pairs += len(pairs) if seam.enabled else 0

            ratio = (max(length_a, length_b) / min(length_a, length_b)) if min(length_a, length_b) > 1e-9 else float('inf')
            status = "OK"
            if ratio > 1.5:
                status = "長さの差が大きい(縫うと強く引っ張られます)"
                problems.append(f"{seam.name}: {status} ({length_a:.3f}m vs {length_b:.3f}m)")

            print(
                f"[cloth_md]  {seam.name}: {len(chain_a)}v/{length_a:.3f}m <-> "
                f"{len(chain_b)}v/{length_b:.3f}m, ペア {len(pairs)}, "
                f"flipped={seam.flipped}, enabled={seam.enabled} — {status}"
            )

        if problems:
            for p in problems:
                print(f"[cloth_md]  ! {p}")
            self.report({'WARNING'}, f"{len(problems)} 件の問題。詳細はコンソール")
            return {'FINISHED'}

        self.report({'INFO'}, f"{len(obj.cloth_md_seams)} 本すべて正常 / 合計 {total_pairs} ペア")
        return {'FINISHED'}


_classes = (
    CLOTHMD_OT_add_pattern_piece,
    CLOTHMD_OT_fill_outline,
    CLOTHMD_OT_join_pieces,
    CLOTHMD_OT_add_seam,
    CLOTHMD_OT_remove_seam,
    CLOTHMD_OT_clear_seams,
    CLOTHMD_OT_select_seam,
    CLOTHMD_OT_validate_seams,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

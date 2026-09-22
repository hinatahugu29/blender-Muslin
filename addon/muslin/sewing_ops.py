"""縫製ワークフロー(M3)のオペレータ: パターンピース作成とシーム編集。"""

import bmesh
import bpy
from mathutils import Vector

from . import mesh_io
from . import seams
from . import ui_poll


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


# ------------------------------------------------------ 縫い目の属性の操作

def _edge_index_map(mesh):
    return {tuple(sorted(e.vertices)): e.index for e in mesh.edges}


def _object_mode_codes(mesh, create=False):
    """オブジェクトモードのメッシュの縫い目属性を (attr, codes ndarray) で返す。"""
    import numpy as np
    attr = mesh.attributes.get(seams.SEAM_ATTRIBUTE)
    if attr is None:
        if not create:
            return None, None
        attr = mesh.attributes.new(seams.SEAM_ATTRIBUTE, 'INT', 'EDGE')
    codes = np.zeros(len(mesh.edges), dtype=np.int32)
    attr.data.foreach_get("value", codes)
    return attr, codes


def _clear_seam_codes(obj, uids):
    """指定した uid の縫い目を辺の属性から消す。編集モードでも動く。"""
    wanted = {seams.seam_code(u, s) for u in uids for s in (0, 1)}
    if not wanted:
        return
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.edges.layers.int.get(seams.SEAM_ATTRIBUTE)
        if layer is not None:
            for e in bm.edges:
                if e[layer] in wanted:
                    e[layer] = 0
            bmesh.update_edit_mesh(obj.data)
        return
    attr, codes = _object_mode_codes(obj.data)
    if attr is None:
        return
    for c in wanted:
        codes[codes == c] = 0
    attr.data.foreach_set("value", codes)
    obj.data.update()


def migrate_legacy_seams(obj):
    """頂点番号で持つ旧形式の縫い目を、辺の属性へ移す(オブジェクトモード)。

    移せなかった(頂点番号が壊れている)縫い目の名前を返す。向きは
    「移す前と同じ頂点どうしが縫われる」ように `invert` を決める。
    """
    positions = mesh_io.get_world_positions(obj)
    edge_map = _edge_index_map(obj.data)
    failed = []
    attr = None
    codes = None
    for seam in obj.muslin_seams:
        if seam.uid != 0:
            continue
        resolved = mesh_io.resolve_seam(obj, seam, positions)
        if resolved is None:
            failed.append(seam.name)
            continue
        stored_a, stored_b, flipped = resolved
        edge_ids = []
        for chain in (stored_a, stored_b):
            ids = [edge_map.get(tuple(sorted(p))) for p in zip(chain, chain[1:])]
            if None in ids:
                break
            edge_ids.append(ids)
        if len(edge_ids) != 2:
            failed.append(seam.name)
            continue
        if attr is None:
            attr, codes = _object_mode_codes(obj.data, create=True)
        uid = mesh_io.next_seam_uid()
        for side, ids in enumerate(edge_ids):
            for i in ids:
                codes[i] = seams.seam_code(uid, side)

        # 属性から作り直す列は向きが保存時と逆のことがある。それを勘定に
        # 入れて、同じ頂点どうしが縫われる向きを自動判定との差で持つ。
        edges = [tuple(e.vertices) for e in obj.data.edges]
        canon_a = seams.chains_from_codes(codes, edges, uid, 0)[0]
        canon_b = seams.chains_from_codes(codes, edges, uid, 1)[0]
        wanted = bool(flipped) ^ (canon_a[0] != stored_a[0]) ^ (canon_b[0] != stored_b[0])
        auto = seams.should_flip(canon_a, canon_b, positions)
        seam.uid = uid
        seam.invert = wanted ^ bool(auto)
        seam.chain_a.clear()
        seam.chain_b.clear()
    if attr is not None:
        attr.data.foreach_set("value", codes)
        obj.data.update()
    return failed


def _renumber_seams(obj, taken):
    """obj の縫い目の uid が `taken` と重なっていたら振り直す(複製したピース向け)。"""
    clash = [s for s in obj.muslin_seams if s.uid != 0 and s.uid in taken]
    if not clash:
        return
    attr, codes = _object_mode_codes(obj.data)
    start = max(mesh_io.next_seam_uid(), max(taken) + 1)
    for k, seam in enumerate(clash):
        new = start + k
        if codes is not None:
            for side in (0, 1):
                codes[codes == seams.seam_code(seam.uid, side)] = seams.seam_code(new, side)
        seam.uid = new
    if attr is not None:
        attr.data.foreach_set("value", codes)
        obj.data.update()


# ------------------------------------------------------ パターンピース作成


class MUSLIN_OT_add_pattern_piece(bpy.types.Operator):
    """指定サイズの長方形パターンピースを作成する(3Dカーソル位置、正面向き)"""

    bl_idname = "muslin.add_pattern_piece"
    bl_label = "Add Pattern Piece"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.muslin_tools
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


class MUSLIN_OT_fill_outline(bpy.types.Operator):
    """選択した閉じたエッジループの内側を三角形で埋め、パターンピースにする"""

    bl_idname = "muslin.fill_outline"
    bl_label = "Fill Outline as Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.in_edit_mode(cls, context)

    def execute(self, context):
        obj = context.active_object
        props = context.scene.muslin_tools
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


UV_LAYER = "MuslinPattern"


class MUSLIN_OT_pattern_to_uv(bpy.types.Operator):
    """型紙の平らな形から UV を作る(全ピースで実寸の比をそろえる)

    着せた後の形ではなく型紙から作るので、布が伸びたり曲がったりしても
    柄はゆがまない。UV マップ "MuslinPattern" に書き、UV の 1 が何メートルかを
    オブジェクトのカスタムプロパティ muslin_meters_per_uv に残す。
    """

    bl_idname = "muslin.pattern_to_uv"
    bl_label = "Pattern to UV"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        return ui_poll.mesh_selected(cls, context)

    def execute(self, context):
        import numpy as np
        from . import pattern_uv
        from . import rest_shape

        obj = context.active_object
        mesh = obj.data
        edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edges)
        edges = edges.reshape(-1, 2)

        pattern = rest_shape.load_pattern(obj)
        source = "型紙"
        if pattern is None:
            # 一度も開始していない(型紙が無い)ときは今の形から作る
            pattern = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", pattern)
            source = "今の形"
        bent = rest_shape.bent_islands(pattern, edges)

        uvs, meters_per_uv = pattern_uv.layout(pattern, edges)

        layer = mesh.uv_layers.get(UV_LAYER) or mesh.uv_layers.new(name=UV_LAYER)
        loop_vertices = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        layer.data.foreach_set("uv", uvs[loop_vertices].astype(np.float32).ravel())
        mesh.uv_layers.active = layer
        obj["muslin_meters_per_uv"] = meters_per_uv
        mesh.update()

        message = (f"{source}から UV を作りました(UV の 1 = {meters_per_uv:.3f} m、"
                   f"ピース {len(pattern_uv.islands(len(mesh.vertices), edges))} 枚)")
        if bent:
            self.report({'WARNING'}, message + f"。平らでないピースが {bent} 枚あり、"
                        "そのピースの柄はゆがみます(Lock Pattern してから曲げてください)")
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}


# -------------------------------------------------------------- シーム編集

class MUSLIN_OT_join_pieces(bpy.types.Operator):
    """選択したパターンピースを1つのオブジェクトに統合する

    縫い目は辺の属性で持つので、統合しても残る。ピースごとに縫い目を
    定義してから統合しても、統合してから定義してもよい。
    """

    bl_idname = "muslin.join_pieces"
    bl_label = "Join Pattern Pieces"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if len(meshes) < 2:
            return ui_poll.reject(
                cls, f"統合するメッシュを2つ以上選択してください (現在 {len(meshes)})"
            )
        return True

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']

        active = context.view_layer.objects.active
        if active not in meshes:
            active = meshes[0]
            context.view_layer.objects.active = active

        # 旧形式(頂点番号)の縫い目は統合で番号がずれるので、先に属性へ移す
        lost = []
        for obj in meshes:
            lost += migrate_legacy_seams(obj)

        # 複製したピースは uid が重なっているので、統合前に振り直す
        taken = set()
        for obj in meshes:
            _renumber_seams(obj, taken)
            taken |= {s.uid for s in obj.muslin_seams if s.uid}

        # 統合で残るのはアクティブのカスタムプロパティだけなので、縫い目の
        # 登録を先に写しておく(辺の属性は Blender が統合してくれる)
        carried = [
            (s.name, s.uid, s.invert, s.enabled)
            for obj in meshes if obj is not active
            for s in obj.muslin_seams if s.uid
        ]

        bpy.ops.object.join()

        # 統合後は旧形式の番号が使えないので、属性を持たない登録は捨てる
        for i in reversed(range(len(active.muslin_seams))):
            if active.muslin_seams[i].uid == 0:
                active.muslin_seams.remove(i)
        for name, uid, invert, enabled in carried:
            seam = active.muslin_seams.add()
            seam.name, seam.uid, seam.invert, seam.enabled = name, uid, invert, enabled

        if lost:
            self.report(
                {'WARNING'},
                f"{len(meshes)} ピースを統合しました。壊れていた縫い目 {len(lost)} 本"
                f"({', '.join(lost[:3])})は引き継げませんでした",
            )
        else:
            self.report(
                {'INFO'},
                f"{len(meshes)} ピースを統合しました(縫い目 {len(active.muslin_seams)} 本)",
            )
        return {'FINISHED'}


class MUSLIN_OT_add_seam(bpy.types.Operator):
    """編集モードで選択した2本のエッジ列を1本の縫い目として登録する"""

    bl_idname = "muslin.add_seam"
    bl_label = "Add Seam From Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.in_edit_mode(cls, context)

    def execute(self, context):
        obj = context.active_object

        bm = bmesh.from_edit_mesh(obj.data)
        # 層を足すと BMesh の要素の参照が作り直されるので、先に作っておく
        layer, _, _ = mesh_io.bmesh_seam_codes(bm, create=True)
        bm.verts.ensure_lookup_table()

        picked = [e for e in bm.edges if e.select]
        if not picked:
            self.report({'ERROR'}, "エッジが選択されていません")
            return {'CANCELLED'}
        selected = [(e.verts[0].index, e.verts[1].index) for e in picked]

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

        # 辺の属性に書く。すでに別の縫い目に使われていた辺は上書きになる
        uid = mesh_io.next_seam_uid()
        in_a = set(chain_a)
        overwritten = 0
        for e in picked:
            if e[layer] != 0:
                overwritten += 1
            side = 0 if (e.verts[0].index in in_a and e.verts[1].index in in_a) else 1
            e[layer] = seams.seam_code(uid, side)
        bmesh.update_edit_mesh(obj.data)

        seam = obj.muslin_seams.add()
        seam.name = f"Seam {len(obj.muslin_seams)}"
        seam.uid = uid
        seam.invert = False     # 向きは使うたびに自動で決める
        obj.muslin_seam_active = len(obj.muslin_seams) - 1

        length_a = seams.seam_length(chain_a, positions)
        length_b = seams.seam_length(chain_b, positions)
        pairs = seams.pair_chains(chain_a, chain_b, positions, flipped)

        print(
            f"[muslin] シーム追加: {len(chain_a)}頂点({length_a:.3f}m) <-> "
            f"{len(chain_b)}頂点({length_b:.3f}m), ペア {len(pairs)}, flipped={flipped}"
        )
        message = f"{seam.name}: {len(chain_a)} <-> {len(chain_b)} 頂点 / {len(pairs)} ペア"
        if overwritten:
            self.report(
                {'WARNING'},
                message + f"。{overwritten} 本の辺は別の縫い目から付け替えました",
            )
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}


class MUSLIN_OT_remove_seam(bpy.types.Operator):
    """選択中の縫い目を削除する"""

    bl_idname = "muslin.remove_seam"
    bl_label = "Remove Seam"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.has_seams(cls, context)

    def execute(self, context):
        obj = context.active_object
        index = obj.muslin_seam_active
        if 0 <= index < len(obj.muslin_seams):
            _clear_seam_codes(obj, [obj.muslin_seams[index].uid])
            obj.muslin_seams.remove(index)
            obj.muslin_seam_active = max(0, index - 1)
        return {'FINISHED'}


class MUSLIN_OT_clear_seams(bpy.types.Operator):
    """このオブジェクトの縫い目をすべて削除する"""

    bl_idname = "muslin.clear_seams"
    bl_label = "Clear All Seams"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.has_seams(cls, context)

    def execute(self, context):
        obj = context.active_object
        count = len(obj.muslin_seams)
        _clear_seam_codes(obj, [s.uid for s in obj.muslin_seams if s.uid])
        obj.muslin_seams.clear()
        self.report({'INFO'}, f"{count} 本の縫い目を削除しました")
        return {'FINISHED'}


class MUSLIN_OT_select_seam(bpy.types.Operator):
    """選択中の縫い目に含まれる頂点を、編集モードで選択する"""

    bl_idname = "muslin.select_seam"
    bl_label = "Select Seam Vertices"

    @classmethod
    def poll(cls, context):
        return ui_poll.in_edit_mode(cls, context) and ui_poll.has_seams(cls, context)

    def execute(self, context):
        obj = context.active_object
        index = obj.muslin_seam_active
        if not (0 <= index < len(obj.muslin_seams)):
            return {'CANCELLED'}

        seam = obj.muslin_seams[index]
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if seam.uid:
            # 編集中の BMesh の属性を読む(オブジェクトモードのメッシュは古い)
            _, codes, edges = mesh_io.bmesh_seam_codes(bm)
            mine = {seams.seam_code(seam.uid, 0), seams.seam_code(seam.uid, 1)}
            wanted = {v for c, e in zip(codes or [], edges or []) if c in mine for v in e}
        else:
            chain_a, chain_b = mesh_io.seam_chains(seam)
            wanted = set(chain_a) | set(chain_b)

        for v in bm.verts:
            v.select_set(v.index in wanted)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)

        self.report({'INFO'}, f"{len(wanted)} 頂点を選択しました")
        return {'FINISHED'}


class MUSLIN_OT_validate_seams(bpy.types.Operator):
    """全シームの状態(ペア数・縫い目長・長さの食い違い)を検査して報告する"""

    bl_idname = "muslin.validate_seams"
    bl_label = "Validate Seams"

    @classmethod
    def poll(cls, context):
        return ui_poll.has_seams(cls, context)

    def execute(self, context):
        obj = context.active_object
        positions = mesh_io.get_world_positions(obj)
        codes, edges = mesh_io.read_seam_codes(obj.data)

        problems = []
        total_pairs = 0

        print("[muslin] ---- seam validation ----")
        for seam in obj.muslin_seams:
            resolved = mesh_io.resolve_seam(obj, seam, positions, codes, edges)
            if resolved is None:
                problems.append(
                    f"{seam.name}: 使えません(縫い目の辺が消えたか、片側が途切れて"
                    "2本以上に分かれています。縫い直してください)"
                )
                continue
            chain_a, chain_b, flipped = resolved

            length_a = seams.seam_length(chain_a, positions)
            length_b = seams.seam_length(chain_b, positions)
            pairs = seams.pair_chains(chain_a, chain_b, positions, flipped)
            total_pairs += len(pairs) if seam.enabled else 0

            ratio = (max(length_a, length_b) / min(length_a, length_b)) if min(length_a, length_b) > 1e-9 else float('inf')
            status = "OK"
            if ratio > 1.5:
                status = "長さの差が大きい(縫うと強く引っ張られます)"
                problems.append(f"{seam.name}: {status} ({length_a:.3f}m vs {length_b:.3f}m)")

            print(
                f"[muslin]  {seam.name}: {len(chain_a)}v/{length_a:.3f}m <-> "
                f"{len(chain_b)}v/{length_b:.3f}m, ペア {len(pairs)}, "
                f"flipped={flipped}, enabled={seam.enabled} — {status}"
            )

        if problems:
            for p in problems:
                print(f"[muslin]  ! {p}")
            self.report({'WARNING'}, f"{len(problems)} 件の問題。詳細はコンソール")
            return {'FINISHED'}

        self.report({'INFO'}, f"{len(obj.muslin_seams)} 本すべて正常 / 合計 {total_pairs} ペア")
        return {'FINISHED'}


_classes = (
    MUSLIN_OT_add_pattern_piece,
    MUSLIN_OT_fill_outline,
    MUSLIN_OT_join_pieces,
    MUSLIN_OT_pattern_to_uv,
    MUSLIN_OT_add_seam,
    MUSLIN_OT_remove_seam,
    MUSLIN_OT_clear_seams,
    MUSLIN_OT_select_seam,
    MUSLIN_OT_validate_seams,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

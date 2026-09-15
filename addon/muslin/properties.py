import bpy


# 生地プリセット。
#
# `density` は実際の目付(g/m^2)を kg/m^2 に直したもので、根拠のある値。
#   シフォン 40-60 / シルク 60-90 / コットンシャツ地 110-150
#   ウール 250-350 / デニム 300-450 / 革 800-1000
# コンプライアンスは物性の実測値ではなく、XPBD 上で「らしく」見える相対値。
# 硬い生地ほど bending を小さく、伸びる生地ほど stretch を大きくしてある。
#
# bending の値はカンチレバー法(片持ちの短冊がどれだけ垂れるか)で較正した。
# 有効なのは 0 〜 0.3 の範囲で、それより大きくしても「完全に柔らかい」で頭打ちになる。
#
# **曲げ剛性は Substeps に強く依存する**。既定の Substeps 4 では、どの生地も
# ほとんど同じように垂れる。生地の違いを出したいなら Substeps を 16 以上にすること
# (実測は README「パラメータの決め方」を参照)。
FABRIC_PRESETS = {
    'CHIFFON': dict(density=0.05, stretch=0.0, bending=0.3, damping=0.02),
    'SILK': dict(density=0.08, stretch=0.0, bending=0.1, damping=0.03),
    'KNIT': dict(density=0.18, stretch=5e-4, bending=5e-2, damping=0.05),
    'COTTON': dict(density=0.15, stretch=0.0, bending=2e-2, damping=0.05),
    'WOOL': dict(density=0.30, stretch=1e-5, bending=5e-3, damping=0.08),
    'DENIM': dict(density=0.40, stretch=0.0, bending=1e-3, damping=0.08),
    'LEATHER': dict(density=0.90, stretch=0.0, bending=0.0, damping=0.12),
}


def _apply_fabric_preset(self, context):
    """プリセットが選ばれたら物性値を流し込む。"""
    values = FABRIC_PRESETS.get(self.fabric_preset)
    if values is None:      # 'CUSTOM' は何もしない
        return
    self.density = values["density"]
    self.stretch_compliance = values["stretch"]
    self.bending_compliance = values["bending"]
    self.damping = values["damping"]


class MUSLIN_PG_vertex_index(bpy.types.PropertyGroup):
    """シームのチェーンを構成する頂点インデックス(順序を保持する)。"""

    index: bpy.props.IntProperty(name="Vertex Index", default=0, min=0)


class MUSLIN_PG_seam(bpy.types.PropertyGroup):
    """1本の縫い目。2本の頂点チェーンを弧長で対応付けて縫い合わせる。"""

    name: bpy.props.StringProperty(name="Name", default="Seam")
    chain_a: bpy.props.CollectionProperty(type=MUSLIN_PG_vertex_index)
    chain_b: bpy.props.CollectionProperty(type=MUSLIN_PG_vertex_index)
    flipped: bpy.props.BoolProperty(
        name="Flip",
        description="縫い合わせる向きを反転する(縫い目がねじれている場合に切り替える)",
        default=False,
    )
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description="この縫い目を有効にする",
        default=True,
    )


class MUSLIN_PG_tools(bpy.types.PropertyGroup):
    """シーン単位の設定。ツールと表示に関わるものだけ。"""

    # --- 時間 ---
    use_scene_fps: bpy.props.BoolProperty(
        name="Use Scene FPS",
        description="シーンのフレームレートから自動で1フレームの時間を決める",
        default=True,
    )
    dt: bpy.props.FloatProperty(
        name="Time Step",
        description="1フレームあたりの経過時間(秒)。Use Scene FPS が無効なときに使用",
        default=1.0 / 24.0,
        min=0.0001,
        soft_max=0.1,
    )

    # --- 表示 ---
    show_seams: bpy.props.BoolProperty(
        name="Show Seams",
        description="ビューポートに縫い目を線で表示する",
        default=True,
    )

    # --- 表示 ---

    # --- パターン作成の道具 ---
    pattern_width: bpy.props.FloatProperty(
        name="Width",
        description="作成するパターンピースの幅",
        default=0.5,
        min=0.001,
        soft_max=3.0,
        unit='LENGTH',
    )
    pattern_height: bpy.props.FloatProperty(
        name="Height",
        description="作成するパターンピースの高さ",
        default=0.7,
        min=0.001,
        soft_max=3.0,
        unit='LENGTH',
    )
    pattern_resolution: bpy.props.FloatProperty(
        name="Resolution",
        description="パターンの目標エッジ長。小さいほど細かく、シワが細かくなるが重くなる",
        default=0.03,
        min=0.002,
        soft_max=0.2,
        unit='LENGTH',
    )


class MUSLIN_PG_cloth(bpy.types.PropertyGroup):
    """オブジェクト単位のシミュレーション設定。

    Blender 標準の Cloth モディファイアと同じく、布ごとに持つ。
    こうしないと1つのシーンで生地を作り分けられない。
    """
    # --- ソルバー ---
    iterations: bpy.props.IntProperty(
        name="Iterations",
        description="制約解決(XPBD)の反復回数。多いほど硬く安定するが重くなる",
        default=10,
        min=1,
        soft_max=50,
    )
    substeps: bpy.props.IntProperty(
        name="Substeps",
        description=(
            "1フレームを分割するサブステップ数。"
            "伸び誤差を減らすには Iterations より効率が良い(実測で約3倍)"
        ),
        default=4,
        min=1,
        soft_max=20,
    )
    chebyshev_radius: bpy.props.FloatProperty(
        name="Convergence Boost",
        description=(
            "制約の収束を加速する(Chebyshev 半復法)。0 で無効。"
            "反復そのものを速めるだけなので材質は変わらない。"
            "Substeps を上げたときに特によく効き、"
            "substeps 32 では垂れ比 0.70 が 0.40 になる(コストは +1% 未満)。"
            "0.98 を超えると振動して発散することがある"
        ),
        default=0.0,
        min=0.0,
        max=0.98,
        precision=3,
    )
    cache_broadphase: bpy.props.BoolProperty(
        name="Cache Collision Search",
        description=(
            "コライダーの探索をフレーム先頭で1回だけ行い、サブステップ間で使い回す。"
            "Substeps が 8 以上のときに効く(実測で 1.1〜1.3倍)。"
            "4 以下ではわずかに遅くなる"
        ),
        default=False,
    )
    post_collision_iterations: bpy.props.IntProperty(
        name="Post-Collision",
        description=(
            "衝突の押し出しで壊れた伸びを解き直す回数。"
            "厚みを大きく取ったときの布の膨張を抑える(0で無効)"
        ),
        default=2,
        min=0,
        soft_max=16,
    )

    # --- 外力 ---
    gravity: bpy.props.FloatProperty(
        name="Gravity",
        description="重力加速度(m/s^2、正の値で下方向に落下)",
        default=9.81,
        min=0.0,
        soft_max=20.0,
    )
    wind: bpy.props.FloatVectorProperty(
        name="Wind",
        description=(
            "一様な風。加速度ではなく面に働く圧力(N/m^2)として扱うので、"
            "同じ風でも Density が高い生地ほどなびきにくい"
        ),
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype='XYZ',
    )
    damping: bpy.props.FloatProperty(
        name="Damping",
        description="速度減衰(0で減衰なし。大きいほど布が早く静止する)",
        default=0.01,
        min=0.0,
        max=1.0,
    )

    # --- 生地物性 ---
    fabric_preset: bpy.props.EnumProperty(
        name="Fabric",
        description=(
            "生地のプリセット。選ぶと Density / Stretch / Bending / Damping が"
            "まとめて設定される"
        ),
        items=[
            ('CUSTOM', "Custom", "手動で設定する(選んでも値は変わりません)"),
            ('CHIFFON', "Chiffon", "シフォン: 非常に軽く柔らかい (50 g/m^2)"),
            ('SILK', "Silk", "シルク: 軽くなめらかに落ちる (80 g/m^2)"),
            ('COTTON', "Cotton", "コットン: 標準的なシャツ地 (150 g/m^2)"),
            ('KNIT', "Knit", "ニット: 伸びる編み地 (180 g/m^2)"),
            ('WOOL', "Wool", "ウール: 厚みがありゆったり落ちる (300 g/m^2)"),
            ('DENIM', "Denim", "デニム: 重く硬い (400 g/m^2)"),
            ('LEATHER', "Leather", "革: 非常に重く曲がりにくい (900 g/m^2)"),
        ],
        default='CUSTOM',
        update=_apply_fabric_preset,
    )
    density: bpy.props.FloatProperty(
        name="Density",
        description="面密度 kg/m^2。頂点質量を面積から算出するのに使う",
        default=0.2,
        min=0.001,
        soft_max=2.0,
    )
    stretch_compliance: bpy.props.FloatProperty(
        name="Stretch Compliance",
        description="伸び制約のコンプライアンス(0に近いほど伸びない・硬い)",
        default=0.0,
        min=0.0,
        soft_max=0.01,
        precision=6,
    )
    bending_compliance: bpy.props.FloatProperty(
        name="Bending Compliance",
        description=(
            "曲げにくさ(0 で最も硬い。大きいほど柔らかい)。"
            "効くのは 0〜0.3 の範囲。"
            "曲げ剛性は Substeps に強く依存するので、硬い生地は Substeps を上げること"
        ),
        default=0.02,
        min=0.0,
        soft_max=0.5,
        precision=5,
    )

    # --- 衝突(暫定: 床のみ。本格的なコリジョンは M2) ---
    floor_enabled: bpy.props.BoolProperty(
        name="Floor Collision",
        description="水平な床面との衝突を有効にする",
        default=False,
    )
    floor_z: bpy.props.FloatProperty(
        name="Floor Z",
        description="床面の高さ(ワールドZ)",
        default=0.0,
    )
    friction: bpy.props.FloatProperty(
        name="Friction",
        description="床面の摩擦(0で滑る、1で止まる)",
        default=0.3,
        min=0.0,
        max=1.0,
    )

    # --- コリジョンオブジェクト(M2) ---
    collision_enabled: bpy.props.BoolProperty(
        name="Object Collision",
        description="指定したオブジェクト/コレクションとの衝突を有効にする",
        default=True,
    )
    collider_object: bpy.props.PointerProperty(
        name="Collider",
        description="衝突させるメッシュオブジェクト(人体アバターなど)",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )
    collider_collection: bpy.props.PointerProperty(
        name="Collider Collection",
        description="複数のコリジョンオブジェクトをまとめて指定するコレクション",
        type=bpy.types.Collection,
    )
    collider_animated: bpy.props.BoolProperty(
        name="Animated Collider",
        description="コライダーが動く場合に有効化(毎フレーム形状を取り込む。その分重い)",
        default=False,
    )
    collision_thickness: bpy.props.FloatProperty(
        name="Thickness",
        description="布の厚み。コライダー表面からこの距離を保つ",
        default=0.005,
        min=0.0,
        soft_max=0.05,
        precision=4,
        unit='LENGTH',
    )
    collision_friction: bpy.props.FloatProperty(
        name="Surface Friction",
        description="コライダー表面の摩擦(0で滑る、1で貼り付く)",
        default=0.3,
        min=0.0,
        max=1.0,
    )

    # --- 自己衝突(M2) ---
    self_collision_enabled: bpy.props.BoolProperty(
        name="Self Collision",
        description="布同士の貫通を防ぐ(重いので必要なときだけ)",
        default=False,
    )
    self_collision_thickness: bpy.props.FloatProperty(
        name="Self Thickness",
        description="自己衝突で保つ頂点間距離。メッシュのエッジ長より小さくすること",
        default=0.01,
        min=0.0,
        soft_max=0.05,
        precision=4,
        unit='LENGTH',
    )

    # --- ピン留め ---
    pin_vertex_group: bpy.props.StringProperty(
        name="Pin Vertex Group",
        description="固定する頂点を指定する頂点グループ名(空欄なら固定なし)",
        default="",
    )

    # --- 縫製(M3) ---
    seam_close_frames: bpy.props.IntProperty(
        name="Seam Close Frames",
        description="縫い目が完全に閉じるまでのフレーム数(0で即座に閉じる)",
        default=30,
        min=0,
        soft_max=240,
    )
    seam_compliance: bpy.props.FloatProperty(
        name="Seam Compliance",
        description="縫製制約のコンプライアンス(0で強固に縫い合わせる)",
        default=0.0,
        min=0.0,
        soft_max=0.01,
        precision=6,
    )

    # --- キャッシュ ---
    use_cache: bpy.props.BoolProperty(
        name="Cache Frames",
        description="計算済みフレームをメモリに保持し、巻き戻し・スクラブを高速化する",
        default=True,
    )



_classes = (
    MUSLIN_PG_vertex_index,
    MUSLIN_PG_seam,
    MUSLIN_PG_tools,
    MUSLIN_PG_cloth,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    # シミュレーション設定は布ごとの性質なのでオブジェクトに持たせる。
    # シーンに残すのはツールと表示の設定だけ。
    bpy.types.Object.muslin = bpy.props.PointerProperty(type=MUSLIN_PG_cloth)
    bpy.types.Scene.muslin_tools = bpy.props.PointerProperty(type=MUSLIN_PG_tools)
    # シームは「どのメッシュに属するか」が本質なのでオブジェクトに持たせる
    bpy.types.Object.muslin_seams = bpy.props.CollectionProperty(type=MUSLIN_PG_seam)
    bpy.types.Object.muslin_seam_active = bpy.props.IntProperty(default=0)


def unregister():
    del bpy.types.Object.muslin_seam_active
    del bpy.types.Object.muslin_seams
    del bpy.types.Scene.muslin_tools
    del bpy.types.Object.muslin
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

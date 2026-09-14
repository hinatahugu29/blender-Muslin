import bpy


class CLOTHMD_PG_vertex_index(bpy.types.PropertyGroup):
    """シームのチェーンを構成する頂点インデックス(順序を保持する)。"""

    index: bpy.props.IntProperty(name="Vertex Index", default=0, min=0)


class CLOTHMD_PG_seam(bpy.types.PropertyGroup):
    """1本の縫い目。2本の頂点チェーンを弧長で対応付けて縫い合わせる。"""

    name: bpy.props.StringProperty(name="Name", default="Seam")
    chain_a: bpy.props.CollectionProperty(type=CLOTHMD_PG_vertex_index)
    chain_b: bpy.props.CollectionProperty(type=CLOTHMD_PG_vertex_index)
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


class CLOTHMD_PG_properties(bpy.types.PropertyGroup):
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
        description="1フレームを分割するサブステップ数。多いほど安定するが重くなる",
        default=4,
        min=1,
        soft_max=20,
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
        description="一様な風の加速度ベクトル(m/s^2)",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype='ACCELERATION',
    )
    damping: bpy.props.FloatProperty(
        name="Damping",
        description="速度減衰(0で減衰なし。大きいほど布が早く静止する)",
        default=0.01,
        min=0.0,
        max=1.0,
    )

    # --- 生地物性 ---
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
        description="曲げ制約のコンプライアンス(大きいほど柔らかく曲がりやすい)",
        default=0.0001,
        min=0.0,
        soft_max=0.1,
        precision=6,
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
    show_seams: bpy.props.BoolProperty(
        name="Show Seams",
        description="ビューポートに縫い目を線で表示する",
        default=True,
    )
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

    # --- パターン作成(M3) ---
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


_classes = (
    CLOTHMD_PG_vertex_index,
    CLOTHMD_PG_seam,
    CLOTHMD_PG_properties,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cloth_md_props = bpy.props.PointerProperty(type=CLOTHMD_PG_properties)
    # シームは「どのメッシュに属するか」が本質なのでオブジェクトに持たせる
    bpy.types.Object.cloth_md_seams = bpy.props.CollectionProperty(type=CLOTHMD_PG_seam)
    bpy.types.Object.cloth_md_seam_active = bpy.props.IntProperty(default=0)


def unregister():
    del bpy.types.Object.cloth_md_seam_active
    del bpy.types.Object.cloth_md_seams
    del bpy.types.Scene.cloth_md_props
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

"""poll が False を返すとき、その理由をツールチップに出すための共通部品。

Blender はボタンを灰色にするだけで理由を出さないので、押せない側の人には
何が足りないのか分からない。`poll_message_set` (Blender 3.0+) を使うと、
灰色のボタンにホバーしたときに赤字で理由が出る。

各関数は「条件を満たすか」を返し、満たさないときだけメッセージを設定する。
bpy に依存しないので、ヘッドレステストからもそのまま呼べる。
"""


def reject(cls, message):
    # poll_message_set は Blender 3.0 以降にしかない。古い版では黙って諦める。
    setter = getattr(cls, "poll_message_set", None)
    if setter is not None:
        setter(message)
    return False


def mesh_selected(cls, context):
    """メッシュオブジェクトがアクティブであること。"""
    obj = context.active_object
    if obj is None:
        return reject(cls, "オブジェクトが選択されていません")
    if obj.type != 'MESH':
        return reject(cls, f"'{obj.name}' はメッシュではありません")
    return True


def in_edit_mode(cls, context):
    """メッシュが編集モードで開かれていること。"""
    if not mesh_selected(cls, context):
        return False
    if context.active_object.mode != 'EDIT':
        return reject(cls, "編集モードに入ってから実行してください (Tab)")
    return True


def has_seams(cls, context):
    """このオブジェクトに縫い目が 1 本以上登録されていること。"""
    if not mesh_selected(cls, context):
        return False
    if len(getattr(context.active_object, "muslin_seams", [])) == 0:
        return reject(cls, "縫い目がまだ登録されていません")
    return True


def sim_running(cls, context, is_running):
    """このオブジェクトのシミュレーションが動いていること。"""
    obj = context.active_object
    if obj is None:
        return reject(cls, "オブジェクトが選択されていません")
    if not is_running(obj):
        return reject(cls, "シミュレーションが動いていません (Start Simulation)")
    return True

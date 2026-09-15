"""生地プリセットのサムネイル画像を Blender のアイコンとして読み込む。

画像は `scripts/make_fabric_previews.py` がシミュレータ自身に描かせたもの。
手描きだと実際の挙動とずれていくので、生地の値をいじったら描き直す。

読み込みに失敗しても致命傷にはしない。アイコンが 0 のとき、Blender は
単に絵の無い項目として描くので、プリセットの選択自体は従来どおり動く。
"""

from pathlib import Path

import bpy
import bpy.utils.previews

PREVIEW_DIR = Path(__file__).parent / "previews"

_collection = None


def icon_id(name):
    """生地名(FABRIC_PRESETS のキー)に対応するアイコン ID。無ければ 0。"""
    if _collection is None:
        return 0
    entry = _collection.get(name)
    return entry.icon_id if entry is not None else 0


def loaded_count():
    """読み込めた枚数(テストと診断用)。"""
    return 0 if _collection is None else len(_collection)


def image_size(name):
    """読み込んだ画像の大きさ。無ければ None。

    `icon_id` はヘッドレスでは常に 0 になる(アイコンの割り当てに UI が
    要る)ので、読み込めているかの確認にはこちらを使う。
    """
    if _collection is None:
        return None
    entry = _collection.get(name)
    return None if entry is None else tuple(entry.image_size)


def register():
    global _collection
    if _collection is not None:
        return
    if not PREVIEW_DIR.is_dir():
        return

    collection = bpy.utils.previews.new()
    for png in sorted(PREVIEW_DIR.glob("*.png")):
        # ファイル名(小文字)がそのままプリセットのキー(大文字)になる
        try:
            collection.load(png.stem.upper(), str(png), 'IMAGE')
        except Exception as exc:   # 壊れた PNG でアドオン全体を止めない
            print(f"[muslin] サムネイル '{png.name}' を読めません: {exc}")
    _collection = collection


def unregister():
    global _collection
    if _collection is not None:
        bpy.utils.previews.remove(_collection)
        _collection = None

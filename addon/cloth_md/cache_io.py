"""シミュレーションキャッシュのディスク入出力(M6)。

bpy に依存しないので `scripts/verify_core.py` から検証できる。

フォーマット(1フレーム1ファイル):
    magic   4 bytes  b"CMDC"
    version uint32   現在 1
    count   uint32   頂点数
    data    float32 * 3 * count  (ローカル座標 x,y,z の並び)

float32 にしているのはディスク容量のため。1万頂点で 120KB/フレーム、
250フレームで 30MB 程度に収まる。シミュレーション自体は f64 で計算しており、
保存時のみ精度を落とす(表示・レンダリング用途では十分)。
"""

import json
import os
import struct
from array import array

MAGIC = b"CMDC"
VERSION = 1
HEADER_FORMAT = "<4sII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
INFO_FILENAME = "info.json"


class CacheError(Exception):
    """キャッシュの読み書きに失敗したときに投げる。"""


def frame_filename(frame):
    return f"frame_{frame:06d}.bin"


def frame_path(directory, frame):
    return os.path.join(directory, frame_filename(frame))


def write_frame(directory, frame, positions):
    """1フレーム分の頂点座標(フラット配列)を書き出す。"""
    if len(positions) % 3 != 0:
        raise CacheError("positions length must be a multiple of 3")

    count = len(positions) // 3
    os.makedirs(directory, exist_ok=True)

    values = array("f", positions)
    # ビッグエンディアン環境でも同じファイルが読めるようにリトルエンディアンへ揃える
    if _is_big_endian():
        values.byteswap()

    path = frame_path(directory, frame)
    temp_path = path + ".tmp"
    with open(temp_path, "wb") as handle:
        handle.write(struct.pack(HEADER_FORMAT, MAGIC, VERSION, count))
        values.tofile(handle)
    # 書き込み途中のファイルが読まれないよう、完成してから置き換える
    os.replace(temp_path, path)
    return path


def read_frame(directory, frame, expected_count=None):
    """1フレーム分の頂点座標をフラットな float のリストで返す。"""
    path = frame_path(directory, frame)
    try:
        with open(path, "rb") as handle:
            header = handle.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                raise CacheError(f"キャッシュが壊れています(ヘッダ不足): {path}")

            magic, version, count = struct.unpack(HEADER_FORMAT, header)
            if magic != MAGIC:
                raise CacheError(f"キャッシュファイルではありません: {path}")
            if version != VERSION:
                raise CacheError(
                    f"未対応のキャッシュ形式です (version {version}, 対応 {VERSION}): {path}"
                )
            if expected_count is not None and count != expected_count:
                raise CacheError(
                    f"頂点数が一致しません (キャッシュ {count} / メッシュ {expected_count})。"
                    "メッシュを編集した場合はベイクし直してください"
                )

            values = array("f")
            try:
                values.fromfile(handle, count * 3)
            except EOFError as exc:
                raise CacheError(f"キャッシュが壊れています(データ不足): {path}") from exc
    except FileNotFoundError as exc:
        raise CacheError(f"キャッシュがありません: {path}") from exc

    if _is_big_endian():
        values.byteswap()
    return values.tolist()


def has_frame(directory, frame):
    return os.path.isfile(frame_path(directory, frame))


def write_info(directory, info):
    """ベイク範囲などのメタ情報を保存する。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, INFO_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(info, handle, ensure_ascii=False, indent=2)
    return path


def read_info(directory):
    """メタ情報を返す。無い/壊れている場合は None。"""
    path = os.path.join(directory, INFO_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return None


def clear_cache(directory):
    """キャッシュディレクトリ内のフレームファイルと情報ファイルを削除する。

    自分が作ったファイルだけを消し、ディレクトリ自体は他のファイルが
    無ければ削除する(ユーザーの別ファイルを巻き込まないため)。
    """
    if not os.path.isdir(directory):
        return 0

    removed = 0
    for name in os.listdir(directory):
        if (name.startswith("frame_") and name.endswith(".bin")) or name == INFO_FILENAME:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass

    try:
        if not os.listdir(directory):
            os.rmdir(directory)
    except OSError:
        pass

    return removed


def cache_size_bytes(directory):
    if not os.path.isdir(directory):
        return 0
    total = 0
    for name in os.listdir(directory):
        if name.startswith("frame_") and name.endswith(".bin"):
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


def _is_big_endian():
    return array("I", [1]).tobytes()[0] == 0

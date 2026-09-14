"""座標変換ヘルパー(bpy 非依存)。

`seams.py` / `cache_io.py` と同じ方針で、Blender を起動せずに
`scripts/verify_core.py` から検証できるよう bpy に依存させない。
numpy は Blender に同梱されているのでそのまま使える。

Python ループで変換すると 14,641 頂点で片道 5.1ms かかる。
Rust の step が 18.7ms なので往復 10ms の上乗せは無視できない(numpy 化で 1.4ms)。
"""

import numpy as np


def split_matrix(matrix):
    """4x4 行列を (3x3 回転・スケール部, 平行移動ベクトル) に分解する。

    mathutils.Matrix でも入れ子のシーケンスでも受け取れるよう、
    添字アクセスだけで読む。
    """
    rotation = np.array(
        [
            (matrix[0][0], matrix[0][1], matrix[0][2]),
            (matrix[1][0], matrix[1][1], matrix[1][2]),
            (matrix[2][0], matrix[2][1], matrix[2][2]),
        ],
        dtype=np.float64,
    )
    translation = np.array(
        (matrix[0][3], matrix[1][3], matrix[2][3]), dtype=np.float64
    )
    return rotation, translation


def transform(flat, matrix):
    """フラットな座標配列 [x0,y0,z0,...] に 4x4 行列を適用する。

    戻り値: (N, 3) の float64 配列
    """
    rotation, translation = split_matrix(matrix)
    points = np.asarray(flat, dtype=np.float64).reshape(-1, 3)
    return points @ rotation.T + translation


def transform_flat(flat, matrix):
    """`transform` の結果をフラットな Python リストで返す。"""
    return transform(flat, matrix).ravel().tolist()

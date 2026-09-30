# Curve Pattern — 最小データモデル(設計メモ・未実装)

状態: 方針確定、実装前。スパイクの結果は [curve_spike_results.md](curve_spike_results.md)。

## 方針と判断基準

判断の優先順位:

1. Curve を一次データ、Generated Mesh を派生データとする関係を崩さない
2. 状態を不完全に推測して維持するより、操作結果を予測可能にする
3. 既存の Mesh → Simulation 系を可能な限り維持する
4. 初期実装を単純にしつつ、将来の state transfer 等を塞がない

決めたこと:

- **二系統を持つ。** Mesh Pattern mode は残す。
  - Mesh Pattern: `Editable Mesh Pattern → Simulation`(現行の `pattern_link`)
  - Curve Pattern: `Editable Curve Pattern → Derived Mesh/Reference → Simulation`
- 概念は `Curve Pattern → Discretized Representation → Cloth Mesh` の三層。ただし UI 上は
  **`Curve Pattern ↔ Cloth` の 2 つだけ**を見せる。Discretized Representation は内部・派生データで、
  独立した型紙オブジェクトとして常時は出さない(デバッグ表示のみ)
- Curve Pattern モードでユーザーが操作する型紙は Curve だけ。Generated Mesh は直接編集しない
- 更新は 3 段階: **Shape Update**(現在姿勢を維持する)/ **Rebuild Required**(表示のみ)/
  **Rebuild**(topology を含む構造を作り直す。停止中に明示的に実行)
- **Rebuild で着せた姿勢は失われてよい。** 最近傍頂点による姿勢転送は行わない
  (意味のない対応で「保存できているように見える」ほうが危険)
- **再生中・Dress 中・Adjust 中は Shape Update のみ。** Rebuild 条件に入ったら
  `Rebuild Required` を表示するだけで、暗黙に実行しない
- 将来の姿勢転送は、3D の最近傍ではなく旧 Mesh 上の**型紙空間(pattern space)**を介す
  (旧三角形内の barycentric 座標で、現在の 3D 形状を新頂点へ補間する)。今回は実装しないが、
  データモデルはそれを塞がない(§4)

## 1. 点の同一性(スパイクの結論)

- Curve の点・spline の **index は恒久 ID にならない**(delete、cyclic の辺の削除で始点が回転、
  split、join、separate で動く)。spline には ID を持たせる手段自体がない
- 一方、点ごとの値は編集操作を通しても**点に付いて回る**。新しい点は複製(extrude / split /
  duplicate)か線形補間(subdivide)として生まれる
- 従来型 Curve を採り、`radius` に uid、`weight_softbody` に非線形な検算値を書く。
  読み取り時に「既知の点 / 新しい点(補間) / 複製(曖昧)」を**検出**する
- ピースの同一性は所属する点の uid から導く(spline 自体には持てない)
- 目印の読み書きは 1 モジュールに閉じ、将来 Curves データへ替えられるようにする

詳細は [curve_spike_results.md](curve_spike_results.md)。

## 2. Curve Pattern object が持つ一次データ

Curve のデータに PropertyGroup(`muslin_curve`)として持つ。

```
CurvePattern
  gen_id: int                 # 最後に生成した世代。派生データ側と突き合わせる
  target_edge_length: float
  cloth_object: Object        # 対になる布(UI 上のもう一方)
  next_point_uid: int         # 点 uid の採番
  pieces: [ Piece ]           # 1 spline の輪郭 = 1 ピース。内側の spline = 穴
    piece_uid, point_uids: [int]   # spline の点 uid の並び(index → uid の記録)
    is_hole, parent_piece_uid
    placement: 4x4 行列       # 着せる前の置き場所(Rebuild の初期姿勢)
  seams: [ SeamDef ]
    uid, name, invert, enabled
    side_a: [ CurveRange ]
    side_b: [ CurveRange ]
  elastics: [ ... ]           # 後段。seam と同じ形にする
```

`pieces[].point_uid` は spline の点の `radius` に書いた uid の記録で、読み取り時の突き合わせと
ピースの同一性の復元に使う(`radius` が壊れても、直前の記録と比べて分類できる)。

### seam の Curve 上での表現

```
CurveRange = (start_point_uid, t0, end_point_uid, t1)
```

- 点 uid で始まる区間(その点から次の点まで)を「区間」と呼ぶ。`t` は区間内の**弧長比**(0〜1)
- 初期実装では `t0`, `t1` を 0 か 1(制御点どうしの間だけ)に制限してよい
- 区間の分割(subdivide で既知の 2 点の間に新しい点)は**検出**できるので、Range を
  自動で 2 つの区間に分けて引き継げる。それ以外(複製など曖昧な場合)は Rebuild Required + 要確認

## 3. Generated Mesh(派生データ)

内部の派生データとして持ち、布(Cloth)オブジェクトの Mesh を作る。頂点の POINT 属性:

| 属性 | 型 | 意味 |
|---|---|---|
| `muslin_gen_seg` | INT | 境界頂点が乗っている区間の点 uid。内部頂点は -1 |
| `muslin_gen_u` | FLOAT | 区間内の弧長比(境界頂点のみ) |
| `muslin_gen_piece` | INT | どのピースの頂点か |
| `muslin_gen_pattern` | FLOAT_VECTOR | **型紙空間(2D)での座標**。将来の姿勢転送のために生成時に固定して持つ(§4) |

辺の `muslin_seam` 属性は**派生**。Rebuild のたびに
`SeamDef → 境界頂点の範囲 → 両端が範囲内の境界辺` と展開して書く。以後は既存の
`resolve_seam` / `build_seam_pairs` がそのまま動く。布オブジェクトの `muslin_seams`
(uid・invert・enabled)も Curve の `SeamDef` から作る。uid は Curve 側の値を使い、
`next_seam_uid` の全体一意性を守る。

生成記録(布オブジェクトのカスタムプロパティ):

```
GenerationRecord
  gen_id                    # Curve 側の gen_id と一致しなければ古い派生データ
  algorithm_version: int
  target_edge_length
  segment_counts: {区間 uid: 分割数 n}
  segment_lengths_at_gen: {区間 uid: 弧長}
  topology_fingerprint: (頂点数, 辺数, 面数)
  quality_at_gen: 辺長比の範囲、最小角、最大アスペクト比
```

## 4. 将来の姿勢転送を塞がないための記録

姿勢転送を実装するときは「旧 Mesh の型紙空間の三角形 → 新頂点の barycentric 座標 →
現在の 3D 位置を補間」となる。そのために、**生成のたびに型紙空間の座標と三角形を残す**
(`muslin_gen_pattern` と面の接続)。旧世代の分は Rebuild の時点で使い捨てにするので、
今回の最小実装ではこれを読む処理は書かない。属性を書いておくだけで足りる。

## 5. Shape Update に必要な頂点対応

離散化の約束: **制御点(および seam の端点・角)は必ず頂点にする**。区間ごとに
`n = round(L / target_edge_length)` を決め、区間内は弧長で等分する。

- 境界頂点: `muslin_gen_seg` と `muslin_gen_u` から、Curve を評価し直して座標を出す(決定的)
- 内部頂点: 境界を固定した Laplace 系の補間。初期実装は**生成時の辺長を重みにしたバネ緩和**
  (重み 1/L₀)を、現在の座標から始めた共役勾配法で解く(numpy のみ、反復回数と許容値を固定)。
  Blender に scipy は無いので使わない
- 純関数(bpy 非依存)にして `scripts/verify_core.py` の系統でテストできるようにする

## 6. 判定と状態

判定は Curve → Generated Mesh の同期関数(`pattern_curve` モジュール想定)に置く。
毎フレームではなく、Curve の指紋(点座標 + 設定の hash)が変わったときだけ走らせる。

| 状態 | 条件 | 動作 |
|---|---|---|
| Shape Update | 目印の分類がすべて「既知の点」で、目標辺長が同じで、更新後の品質が許容内 | 頂点数・接続を保ち reference のみ更新(既存の `poll_pattern` 経路) |
| Rebuild Required | 点・spline・穴の増減(新しい点・複製の検出を含む)/ 目標辺長の変更 / `algorithm_version` の違い / 更新後の辺長比・反転三角形・最小角が許容外 | reference は**最後に成功した値のまま保持**し、理由を表示 |
| Rebuild | 停止中の明示操作のみ | 派生データを作り直す。着せた姿勢は失われる |

- 点の増減は authoring 表現の変化であって Mesh の topology と必ずしも直結しない、という
  区別は概念として残す。初期実装では点が増減したら必ず Rebuild Required に倒す
- 走行中(再生・Dress・Adjust)に Rebuild Required になっても、Shape Update と表示だけを行う
- 「1.00m → 1.05m」は各区間の弧長が変わるだけなので Shape Update。
  辺長比の許容値(たとえば目標の 0.7〜1.4 倍)は実測して決める

## 7. 未決の論点

- Undo / Redo で目印が壊れないかの手動確認(バックグラウンドでは検証できなかった)
- 複数ピースと `sim_group` の対応、`pattern_uv` の Curve モードでの扱い
- ペンツールなど他の編集経路での点の追加
- Blender 4.2 での実測

## 8. 進め方

1. ✅ 実機スパイク(完了。結果は curve_spike_results.md)
2. 純関数の層: 目印の読み書きと分類、離散化(区間分割 + 内部の三角形化)、境界の再評価、
   内部の緩和、品質判定。bpy なしでテストを書く
3. Generated Mesh の生成と属性の書き込み、seam の展開(既存の seam 処理に繋ぐ)
4. `pattern_link` に Curve 経由の入力を足す(`references()` の入力元を切り替えるだけ)
5. Rebuild Required の表示と Rebuild 操作
6. elastic の Curve 側への移行(seam と同じ形)

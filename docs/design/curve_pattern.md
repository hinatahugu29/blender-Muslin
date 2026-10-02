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
- **Rebuild の姿勢の扱い**: 最初は「失われてよい」としていたが、MD に近い体験には姿勢を保つことが
  要るので、型紙空間を介した姿勢の引き継ぎを実装した(§10)。最近傍頂点による 3D 空間での転送は
  行わない(意味のない対応で「保存できているように見える」ほうが危険)
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

Curve のデータのカスタムプロパティ `muslin_curve` に **JSON** で持つ(実装。PropertyGroup にしなかったのは、
Undo と複製にそのまま乗り、登録も要らないため。UI で編集する段階になったら PropertyGroup に移す)。

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
| `muslin_gen_seg` | INT | 境界頂点が乗っている区間の**始点**の uid。内部頂点は -1 |
| `muslin_gen_seg_end` | INT | 同じ区間の**終点**の uid(向きが逆になった輪郭にも uid の組で対応するため) |
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
- 内部頂点: 境界を固定した Laplace 系の補間。初期実装は**生成時の辺長の逆数(1/L₀)を重みにした
  バネ緩和**で、座標ではなく**生成時からの変位**に対する境界値問題を共役勾配法で解く
  (numpy のみ、反復回数と許容値を固定)。変位に対して解くので、Curve が変わらなければ内部も
  生成時のまま動かない。Blender に scipy は無いので使わない
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
- 型紙は XY 平面の 2D として扱い Z は見ない。ピースを Z 方向に重ねると XY で重なるので、外周の中の穴(1 段)と解釈されるか、3 重以上ならエラーになる。ピースは XY に並べる運用
- 複数ピースと `sim_group` の対応、`pattern_uv` の Curve モードでの扱い
- ペンツールなど他の編集経路での点の追加
- Blender 4.2 での実測

## 8. 実装状況

手順 2(bpy 非依存の純関数の層)は実装済み。`scripts/verify_core.py` でテストしている。

| モジュール | 内容 |
|---|---|
| `curve_ids.py` | 目印の読み書き(radius + 非線形の検算値)、構造の比較(`diff_outline`)、ピースの対応付け |
| `curve_eval.py` | Bezier 区間の弧長評価(96 分割の折れ線で弧長比 → 座標) |
| `delaunay2d.py` | Bowyer-Watson の Delaunay、点の内外判定(偶奇則)、輪郭までの距離 |
| `curve_discretize.py` | 輪郭 → 派生データ(境界頂点・内部の六角格子・三角形)、seam の区間 → 境界の辺 |
| `curve_update.py` | Shape Update(境界の再評価 + 内部の緩和)、品質判定、Rebuild Required の判定 |

実装で決めた値(実測して見直す): 輪郭から `0.6 × 目標辺長` 未満の格子点は置かない /
辺長比(更新後 / 生成時)の許容 0.35〜2.5(最初は 0.6〜1.6 だったが、GUI で丈を 2 倍に伸ばしただけで拒否されたので広げた。2 倍に伸ばしても最小角は 17° 以上) / 最小角 8° 以上 / 共役勾配法は最大 400 回。
幅 1.00m → 1.05m(目標辺長 0.02m)は頂点数を変えずに追従し、辺長比は最大 1.05 だった。

### 手順 3(Blender 側)

`curve_pattern.py` が Blender とのつなぎを持つ。`scripts/blender_selftest_curve.py`(Blender 5.1 と 4.2 で 48/48)で確認している。

- `initialize`: 閉じた Bezier の点に uid を振り、ピース(穴は外周に対する入れ子)を記録する
- `rebuild`: 今の Curve を新しい正として取り込み(`_adopt`)、離散化して布のメッシュを作る。
  目印が合わない点と、同じ uid の 2 つ目以降には新しい uid を振る
- `structure_problems` / `current_outlines`: 記録との比較と、Shape Update への入力
- `reconstruct`: 布のメッシュの頂点属性と生成記録から、離散化の結果を組み直す
- `add_seam`: `(始点 uid, t0, 終点 uid, t1)` の区間で縫い目を定義する。Rebuild のたびに境界の辺の属性
  `muslin_seam` へ展開する。既存の `build_seam_pairs` がそのまま動く
- 演算子: `muslin.curve_pattern_init` / `muslin.curve_pattern_rebuild`(パネルはまだ無い)

実装で分かったこと:

- 区間の途中に点を足しても、seam の範囲は uid の並びから決まるので**自動で保たれる**(Rebuild 後も範囲は右辺全体のまま)。
  ただし区間を分けた各半分が別々に丸められるので、辺の本数は 1 本前後変わる
- 布のメッシュは XZ 平面(Y = 0)に立て、面は手前(-Y)を向く。Curve の (x, y) → 布の (x, 0, y)
- Rebuild は新しいメッシュに差し替える。頂点グループ(ピン)とゴム紐は失われ、警告を出す
- シミュレーション中の Rebuild は拒否する。停止すれば同じ布のオブジェクトを使い回す
- Curve と布は ID ポインタ(`muslin_cloth` / `muslin_curve_source`)で結ぶ。保存して開き直しても切れない

## 9. 進め方

1. ✅ 実機スパイク(完了。結果は curve_spike_results.md)
2. ✅ 純関数の層(§8)
3. ✅ Generated Mesh の生成と属性の書き込み、seam の展開(既存の seam 処理に繋ぐ)(§8)
4. `pattern_link` に Curve 経由の入力を足す(`references()` の入力元を切り替えるだけ)
5. Rebuild Required の表示と Rebuild 操作
6. elastic の Curve 側への移行(seam と同じ形)

## 10. Rebuild での姿勢の引き継ぎ(実装済み)

`curve_transfer.py`(bpy 非依存)と `curve_pattern.rebuild(keep_pose=True)`。

1. 旧メッシュを捨てる**前に**、旧頂点の現在の 3D 位置(着せた姿勢)と頂点グループの重み(ピン留め)を集める。
   姿勢が平らな型紙のままなら引き継がない(新しい型紙のまま作ればよい)
2. 旧メッシュの型紙空間の座標を、**今の Curve の形**に置き直す(`old_pattern_now`)。
   境界頂点は区間 uid と弧長比で今の Curve を評価し直し、内部は Shape Update と同じ緩和で決める。
   足された点(uid が未知)はまたいで弧長比で対応し、向きが逆の輪郭にも対応する。
   区間が見つからない(点が消えた)ときは生成時の座標で代用し、警告を出す
3. 新頂点ごとに、旧メッシュの三角形のどこにあるか(重心座標)を探し、旧頂点の 3D 位置を補間する。
   同じ補間で頂点グループの重みも写し、0.5 以上の頂点を新しいグループに入れる
4. 旧メッシュの外側の頂点(Curve を広げた分)は、最寄りの三角形へ寄せて補間する。
   2% を超えたら警告する
5. 新しい姿勢を元の形(rest)にする。着せた段階だったなら着せた段階のまま

テストで確認していること: 頂点数が変わる(415 → 974)のに姿勢は保たれ(円柱からの距離の範囲が ±1mm)、
縫い目は閉じたまま(0.00mm)、ピンが上端に残り、引き継いだ姿勢から再生しても崩れない。
ゴム紐はまだ失われる(辺の属性を Curve 側へ移す作業が残っている)。

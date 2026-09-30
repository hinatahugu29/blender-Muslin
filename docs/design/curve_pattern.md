# Curve Pattern — 最小データモデル案(設計メモ・未実装)

状態: 提案。コードは書いていない。ここで合意した内容を元に実装計画を切る。

## 方針

- Curve Pattern = **authoring data**(唯一の設計上の正)
- Generated Mesh = **derived simulation data**(いつでも再生成できる)
- 既存の Mesh Pattern(`Mesh Pattern → Simulation`)は残す。Curve Pattern は
  `Curve Pattern → Generated Mesh → Simulation` の**追加の系統**で、XPBD・`set_reference`・
  `build_seam_pairs` は置き換えない。
- Curve Pattern モードでは Generated Mesh をユーザーが直接編集しない。
- 更新は 3 段階に分ける: **Shape Update**(自動)/ **Rebuild Required**(表示)/ **Rebuild**(明示操作)。

## 1. Curve の制御点 index / spline index は恒久 ID に使えるか

**使えない。** Blender の(従来型)Curve には点や spline の恒久 ID がなく、点の細分化・削除・
結合・複製・「始点にする」・方向反転で index がずれる。点や spline に任意属性を持たせる手段もない
(従来型 Curve は `radius` / `tilt` / `weight_softbody` の float だけ。編集操作で複製・補間されるので
ID の運び手として信頼できない)。

推奨(段階的):

1. **サイドカーの uid 表**を Curve 側に持つ。`spline 順 × 点順` に uid を割り当てた表と、
   **構造指紋**(spline 数、各 spline の点数・cyclic・種別)を一緒に保存する。
   - 指紋が一致する間は index で対応付ける(通常の頂点移動・ハンドル編集はこれで通る)
   - 指紋が変わったら **Structure Changed**(= Rebuild Required)。seam は黙って推測せず、
     位置による再対応付けを**提案**し、ユーザーが確認する
   - 点数が同じままの「始点にする」・方向反転は指紋で検出できない。前回の座標を持っておき、
     巡回シフト / 反転で当てはめ直した方が一致するなら検出する(O(n))
2. Muslin 自身の編集オペレータ(点の追加、区間の分割など)は、表を一緒に更新する。
   Blender 標準の編集で点数が変わった場合だけ 1 の経路に落ちる。
3. 将来: Blender Curve をやめて独自表現(PropertyGroup の点列 + 自前ギズモ)にすれば完全に
   安定するが、編集 UI を丸ごと作ることになる。初期は採らない。

**事前スパイクが必要**: 従来型 Curve の各編集操作で `radius` / `tilt` の値がどう変わるか、
新しい `Curves` データ(点ごとの属性を持てる)が編集用途に耐えるかを、実機の Blender で確かめる。
結果次第で 1 の表を属性運びに置き換えられる可能性がある。

## 2. Curve Pattern object が持つ一次データ

Curve オブジェクトのデータに PropertyGroup(`muslin_curve`)として持つ。

```
CurvePattern
  gen_id: int              # 最後に生成した世代。Generated Mesh 側と突き合わせる
  target_edge_length: float
  generated_object: Object (pointer)
  structure: 構造指紋 + 点 uid 表
    points: [ (spline_ref, index, point_uid) ... ]
    last_positions: 直近の座標(巡回シフト・反転の検出用)
  pieces: [ Piece ]        # 1 spline の輪郭 = 1 ピース。内側の spline = 穴
    piece_uid, spline_ref, is_hole, parent_piece_uid
    placement: 4x4 行列     # 着せる前の置き場所(Rebuild で姿勢を失わないため。§7)
  seams: [ SeamDef ]
    uid, name, invert, enabled
    side_a: [ CurveRange ]  # 通常 1 本、複数区間の連結も許す
    side_b: [ CurveRange ]
  elastics: [ ... ]         # 後段。seam と同じ形にする
```

### seam の Curve 上での表現

```
CurveRange = (start_point_uid, t0, end_point_uid, t1)
```

- 点 uid で始まる区間(その点から次の点まで)を「区間 uid」と呼び、`t` はその区間内の
  **弧長比**(0〜1)。ベジェの媒介変数ではなく弧長比にするのは、頂点の離散化と
  Shape Update が弧長比で動くため
- 初期実装では `t0`, `t1` を 0 か 1(= 制御点どうしの間だけ)に制限してよい。
  部分区間は後から足せる形にしておく
- 制御点を区間の途中に足す(細分化)と、その区間を指す Range の意味が曖昧になる。
  これは §1 の指紋変化として検出し、位置による再対応付けの提案に回す

## 3. Generated Mesh が持つ派生データ

頂点の POINT 属性(.blend に保存され、既存の `muslin_rest` / `muslin_pattern` と同じ流儀):

| 属性 | 型 | 意味 |
|---|---|---|
| `muslin_gen_seg` | INT | 境界頂点が乗っている区間の点 uid。内部頂点は -1 |
| `muslin_gen_u` | FLOAT | 区間内の弧長比(境界頂点のみ意味を持つ) |
| `muslin_gen_piece` | INT | どのピースの頂点か(`piece_uid`) |

これだけで、境界頂点は Curve を `(区間, u)` で評価し直すだけで動かせる。
内部頂点は接続(既存の辺)から決まるので追加情報は要らない(§5)。

辺の `muslin_seam` 属性は**派生**にする。Rebuild のたびに
`SeamDef → 境界頂点の範囲 → 両端が範囲内の境界辺` と展開して書く。以後は既存の
`resolve_seam` / `build_seam_pairs` がそのまま動く。Generated Mesh のオブジェクトの
`muslin_seams`(uid・invert・enabled)も、Curve の `SeamDef` から再生成する。
uid は Curve 側の値を使い、`next_seam_uid` の全体一意性を守る。

Generated Mesh 側の生成記録(オブジェクトのカスタムプロパティか PropertyGroup):

```
GenerationRecord
  gen_id                    # Curve 側の gen_id と一致しなければ古い派生データ
  source_curve: Object (pointer)
  algorithm_version: int
  target_edge_length
  segment_counts: {区間 uid: 分割数 n}
  segment_lengths_at_gen: {区間 uid: 弧長}
  topology_fingerprint: (頂点数, 辺数, 面数)
  quality_at_gen: 辺長比の範囲、最小角、最大アスペクト比
```

## 4. 両者を結ぶ ID

- `gen_id`: Curve 側と生成記録の両方に置く。再生成のたびに +1。食い違えば派生データが古い
- `source_curve` / `generated_object`: 相互ポインタ(片方が消えたら Mesh Pattern として孤立する)
- `piece_uid` / 区間 uid(点 uid): Curve と Generated Mesh の頂点属性をつなぐ

## 5. Shape Update に必要な頂点対応

離散化の約束: **制御点(および seam の端点・角)は必ず頂点にする**。区間ごとに
`n = round(L / target_edge_length)` を決め、区間内は弧長で等分する。

- 境界頂点: `muslin_gen_seg` と `muslin_gen_u` から、Curve を評価して座標を出す(閉形式・決定的)
- 内部頂点: 境界を固定した Laplace 系の補間。初期実装は
  **生成時の辺長を重みにしたバネ緩和**(重み 1/L₀)を、現在の座標から始めた共役勾配法で解く
  (numpy のみ、反復回数と許容値を固定して決定的にする)。scipy は Blender に無いので使わない
- 純関数(bpy 非依存)にして `scripts/verify_core.py` の系統でテストできるようにする

## 6. Rebuild 判定に必要な記録と条件

判定は Curve → Generated Mesh の同期関数(`pattern_curve` モジュール想定)に置く。
毎フレームではなく、Curve の指紋(点座標 + 設定の hash)が変わったときだけ走らせる。

| 状態 | 条件 | 動作 |
|---|---|---|
| Shape Update | 構造指紋が一致し、目標辺長が同じで、更新後の品質が許容内 | 頂点数・接続を保ち reference のみ更新(既存の `poll_pattern` 経路) |
| Rebuild Required | 構造指紋の変化(点・spline・穴の増減、cyclic の変化)/ 目標辺長の変更 / `algorithm_version` の違い / 更新後の辺長比・反転三角形・最小角が許容外 | reference は**最後に成功した値のまま保持**し、パネルに理由を表示 |
| Rebuild | ユーザーの明示操作のみ | 派生データを作り直す(§7) |

- 点数の変化は authoring 表現の変化であって Mesh の topology とは直結しない、という
  区別は概念として残す。ただし初期実装では点数が変われば必ず Rebuild Required に倒す
- 「1.00m → 1.05m」は各区間の弧長が変わるだけなので Shape Update になる。
  辺長比の許容値(たとえば目標の 0.7〜1.4 倍)は実測して決める

## 7. 未決の論点

- **Rebuild で着せた姿勢が消える。** 今の布の頂点座標は「曲げて置いた姿勢」で、頂点数が
  変わると対応が取れない。ピースの `placement` を Curve 側に持つ案を仮に置いたが、
  Dress 後の姿勢の扱い(捨てる / 最近傍で写す)は決めていない
- Pattern Mesh(平らな型紙。基準)と Cloth Mesh(姿勢を持つ)の 2 つを両方生成するか。
  現行は型紙オブジェクトと布が 1 対 1 で並ぶ構造なので、そちらに寄せるのが自然
- 複数ピース(`sim_group` との対応)、`pattern_uv` の Curve モードでの扱い
- 走行中(再生・Dress・Adjust)の Rebuild を許すか。許さない方が安全

## 8. 進め方(案)

1. 実機スパイク: Curve の編集操作と `radius` / `tilt`、`Curves` の可否を確かめる
2. 純関数の層: 離散化(区間分割 + 内部の三角形化)、境界の再評価、内部の緩和、品質判定。
   bpy なしでテストを書く
3. Generated Mesh の生成と属性の書き込み、seam の展開(既存の seam 処理に繋ぐ)
4. `pattern_link` に Curve 経由の入力を足す(`references()` の入力元を切り替えるだけ)
5. Rebuild Required の表示と Rebuild 操作
6. elastic の Curve 側への移行(seam と同じ形)

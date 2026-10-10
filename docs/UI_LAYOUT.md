# サイドバーの並び(Muslin タブ)

すり合わせ用。番号は表示順、`[閉]` は既定で閉じているパネル、`(条件)` は表示条件。
順番を変えたいときは番号を書き換えるか、「3 を 1 の前へ」のように指定してください。

## 1. Muslin(親パネル)
- 1.1 再生行:Rewind / Play・Pause / Start・Stop / Reset Shape
- 1.2 着せる行:Dress / Adjust / 型紙に戻す(←) / Set Rest Shape
- 1.3 Close Bag(Bag のとき)
- 1.4 Rebuild 待ちの警告 + Restart(必要なとき)
- 1.5 状態(走らせている間):Frame / Verts・Edges / Pinned・Seams

### 2. Fabric(布を選択時)
- 2.1 プリセット / 密度・伸び・曲げ・減衰・圧力
- 2.2 部位ごとの生地(マテリアル)
- 2.3 Bag の圧力モード / Fill
- 2.4 硬さ・圧力の警告

### 3. Pattern
- 3.1 Draw Pattern / Mirror で描く(何も選んでいないか、Curve を選択時)
- 3.2 型紙の確定(Lock / Unlock / Restore Pattern)(メッシュの布)
- 3.3 Pattern to UV(メッシュの布)
- 3.4 型紙オブジェクト(Create Pattern Object)/ Curve 由来なら案内のみ
- 3.5 Mesh Pattern `[閉]`
  - 幅・高さ・解像度 / Add Pattern Piece / Fill Outline / Join Pieces

### 4. Curve Pattern
- 4.1 描画中の操作:Mirror / Finish Piece / Cancel(描いているとき)
- 4.2 未初期化なら Init Curve Pattern
- 4.3 情報(ピース数・目標の辺)/ Rebuild の警告
- 4.4 Rebuild / Pattern View / Arrange / 輪郭表示
- 4.5 Bag `[閉]`(ピース 2 枚以上):Stack / Gusset / Quilt
- 4.6 Seams:一覧 / Add Seam
- 4.7 Elastic `[閉]`:一覧 / Add Elastic

### 5. Sewing(布を選択時)
- 5.1 縫い目の一覧(Curve 由来は追加・削除なし、案内のみ)
- 5.2 Validate Seams
- 5.3 Show Seams / Close Frames / Compliance / Weld(+ Update Weld)
- 5.4 Elastic `[閉]`(メッシュの布向け)

### 6. Collision(布を選択時)
- 6.1 Fit Thickness
- 6.2 コライダー(対象・コレクション・アニメ・厚み・摩擦)
- 6.3 自己衝突(厚み・連続判定)
- 6.4 Sim Group / Layer
- 6.5 Floor

### 7. Forces(布を選択時)
- 7.1 Gravity / Wind / Use Force Fields

### 8. Pinning(編集モードで布を選択時)
- 8.1 Pin / Unpin / Select Pinned / ピン数

### 9. Solver(布を選択時)
- 9.1 Quality / Scene FPS・dt / Cache
- 9.2 Advanced `[閉]`:Iterations / Substeps / Chebyshev / Post Collision / Broadphase Cache

### 10. Bake(布を選択時)
- 10.1 範囲 / Bake・Re-bake / Free / Bake to Shape Keys

### 11. Debug `[閉]`
- 11.1 Stretch error / Contacts
- 11.2 Test Rust / Self Test / Print Timings

---
別の場所:**マテリアルのプロパティ → Muslin Fabric** `[閉]`(部位ごとの生地の設定)

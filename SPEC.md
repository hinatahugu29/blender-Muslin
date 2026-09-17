# Muslin — 仕様概要

Blender 用のクロスシミュレーションアドオン。Marvelous Designer のような
**パターンを作って縫い合わせる**衣服制作ワークフローを、Blender の中で完結させることを
目指している。物理コアは Rust (XPBD) で書き、PyO3 経由で Blender の Python から呼ぶ。

この文書は **2026-09-17 時点のまとめと今後の展望**である。
使い方・パラメータの実測値は [README.md](README.md)、進捗と退けた案は
[ROADMAP.md](ROADMAP.md)、検証項目は [CHECKPOINTS.md](CHECKPOINTS.md) にある。

- アドオン v0.5.0 / `cloth_core` v0.3.0
- 動作確認: Blender 5.2.1 LTS / Windows（物理コアは Linux・macOS も CI で検証）
- ライセンス: GPL-3.0-or-later（`bpy` と結びつくため GPL 互換が必須）

---

## 1. 何をするアドオンか

「モスリン（仮縫い）」の名のとおり、**平面のパターンを置き、辺同士を縫い、
立体に組み上がる過程をシミュレートする**。標準の Cloth モディファイアが
「既にある立体メッシュを揺らす」のに対し、こちらは**衣服の成り立ちのほう**を扱う。

ワークフローの核心は一本につながっている:

    パターン作成 → 統合 → シーム指定 → 縫製シミュレーション → 着せる → ベイク

### 現時点でできること

| 領域 | 内容 |
|---|---|
| ソルバ | XPBD。伸び制約・曲げ制約（平均曲率の一次形式）・シーム制約・ピン留め |
| 質量 | 面積 × 密度による分布（一様質量ではない） |
| 力 | 重力、減衰、風（面圧として実装。重い生地ほどなびきにくい） |
| 生地 | 7 プリセット（Chiffon / Silk / Knit / Cotton / Wool / Denim / Leather）。密度は実際の目付 g/m² 基準。サムネイルから選択 |
| 衝突 | メッシュ衝突（BVH）、自己衝突（空間ハッシュ + 頂点-三角形）、床、摩擦、Catch Fast Motion（掃過判定） |
| コライダー | モディファイア/アーマチュア適用後の評価済みメッシュに追従。アニメーション対応 |
| 縫製 | パターン生成、輪郭からの生成、エッジ選択によるシーム定義、**弧長ベース対応付け**（頂点数が違う辺同士も縫える）、ビューポートへの縫い線描画、`Validate Seams` |
| 開始処理 | 開始時点の食い込みを速度を出さずに解消する untangle |
| 出力 | ディスクへのベイク、シェイプキーへの変換（アドオン非依存の形にできる） |
| 収束 | Substeps 主導 + Convergence Boost（Chebyshev 半復法、10 反復ごとリスタート） |

**設定はオブジェクト単位**（`Object.muslin`）。1 シーンでシルクとデニムを同居させられる。
シーン単位なのは時間設定・縫い線表示・パターン作成の道具設定だけ。

---

## 2. 技術スタック

| 層 | 使っているもの | 役割 |
|---|---|---|
| 物理コア | **Rust 2021** (`rust/cloth_core`, 約 5,500 行) | XPBD ソルバ、BVH、空間ハッシュ |
| 並列化 | **rayon** | 距離制約の彩色並列求解、衝突判定（4,000 頂点以上） |
| Python 束縛 | **PyO3 0.22** (`abi3-py311`, `extension-module`) | Blender 同梱 Python から呼ぶ |
| ビルド | **maturin** → wheel → `.pyd` | Blender は `python.exe` を同梱しないので `maturin develop` は不採用 |
| アドオン層 | **Python / bpy** (`addon/muslin`, 約 4,000 行) | UI、状態管理、座標受け渡し、ベイク |
| 配列処理 | **numpy**（Blender 同梱） | ローカル↔ワールド変換。素の Python ループから 10ms → 2.8ms に短縮 |
| 描画 | **gpu モジュール** (`overlay.py`) | 縫い線のビューポート表示 |
| 配布 | **Blender Extensions**（4.2 以降、`blender_manifest.toml`）+ 従来 zip | ネイティブモジュールを wheel として宣言 |
| CI | **GitHub Actions**（ubuntu / windows / macos） | `cargo test` と wheel ビルド + `verify_core.py` |

### 設計上の要になっている判断

- **物理コアを pyo3 非依存に保つ。** `crate-type = ["cdylib", "rlib"]` と
  `--no-default-features` により、**Blender も Python も無しで物理を検証できる**。
  CI が 3 OS で回せるのはこの構造のおかげ。
- **abi3-py311 でビルドする。** Blender の同梱 Python が 3.13 に上がっても
  再ビルド不要。実際に Blender 5.1（Python 3.13）で実証済み。
- **wheel として配布する。** Windows がロード中の DLL を消せない問題
  （更新時に `.pyd` だけ残り `.~stale~` 退避でアドオンが壊れる）を Extensions 形式で回避。
- **パターンは平面メッシュで表現する。** カーブではなく通常メッシュにしたので
  Blender の編集ツールがそのまま使え、シミュレーション対象と表現が一致する。
- **bpy 非依存に切り出せるロジックは切り出す**（`seams.py` / `cache_io.py` /
  `transform.py`）。テストが速く回る。

### 構成

```
rust/cloth_core/src/
  sim.rs         ソルバ本体（XPBD、制約、風、ベイク向け step）
  collision.rs   BVH・自己衝突・掃過判定
  bending.rs     曲げ制約（平均曲率の一次形式）
  hashing.rs     GridHasher（SipHash を捨てた自前ハッシュ）
  math.rs        Vec3
  lib.rs         PyO3 束縛（ClothSim クラス + 補助関数）

addon/muslin/
  operators.py sim_state.py bake_ops.py sewing_ops.py pin_ops.py
  properties.py panels.py previews.py overlay.py ui_poll.py
  mesh_io.py transform.py cache_io.py seams.py rest_shape.py
```

UI は N パネルの Muslin タブに Solver / Advanced / Fabric / Forces / Collision /
Pinning / Pattern / Sewing / Bake / Debug の各サブパネル。状態に応じて出し分ける。

---

## 3. 検証体制

Blender を必要とする範囲を最小化した 3 層構造:

| 層 | 実行するもの | 項目数 | CI |
|---|---|---|---|
| 1. 物理コア | `cargo test --no-default-features` | 55 | ○（3 OS） |
| 2. 拡張モジュール + bpy 非依存部 | `scripts/verify_core.py` | 77 | ○（3 OS） |
| 3. アドオン層 | `blender --background --python scripts/blender_selftest.py` | 201 | ×（ローカル） |

自由落下と解析解の一致、球の貫通、縫い目の閉じ、ベイクした `.blend` の再読み込み、
リネーム追従、入力バリデーションまで自動で見る。
**自動化できていないのは、ビューポート描画とパネルの見た目・操作の感触だけ**
（[CHECKPOINTS.md](CHECKPOINTS.md) に残件表あり）。

`samples/` の 3 シーン（ドレープ・旗・縫製）はスクリプト生成で、
ヘッドレステストが毎回「開いて再生すると動く」ことを確認している。

---

## 4. 性能の現状

`iterations 10 / substeps 4`、Rust コア内部の時間（Blender との受け渡しは含まない）:

| 頂点数 | 制約のみ | + 自己衝突 |
|---|---|---|
| 1,681 | 2.0 ms | 5.5 ms |
| 4,225 | 5.1 ms | 10.2 ms |
| 14,641 | 18.8 ms | 33.5 ms |

14,641 頂点・自己衝突ありで 30fps 相当。内訳は自己衝突 40%、伸び 26%、曲げ 17%。
**距離制約の合計（伸び + 曲げ + 補正）が 48% で、集計上は制約ソルバが最大項目。**

チューニングで効いたもの／効かなかったものは実測値ごと ROADMAP に残してある。
たとえば自己衝突のコスト削減（+41.3ms → +15.0ms）の真因は
アロケーションではなく標準 HashMap の **SipHash** だった、など。

---

## 5. 既知の制約

- 完全な CCD は無い。コライダー相手（60 m/s）と布どうし（掃過判定で 12 m/s まで）は
  塞いであるが、**布どうしが互いに高速ですれ違う**場合は取りこぼす
- 自己衝突はエッジ同士の交差を見ていない（実測で違反の 0.1%）
- 摩擦は速度減衰の簡易モデルで、物理的な μ ではない
- **曲げ剛性が solver の反復数に依存する**（PBD 系共通の性質）。既定の Substeps 4 では
  7 プリセットの垂れ比の差が 0.016 しか出ず、生地の違いがほぼ見えない。
  硬さを出すには Substeps 16 以上 + Convergence Boost 0.98 が要る
- `Self Thickness` の上限はメッシュのエッジ長で決まる（細分化は逆効果）
- 別オブジェクトのパターン同士は縫えない（事前に統合が必要）
- メッシュ編集で頂点番号が変わるとシームが壊れる（警告と `Validate Seams` で検出）
- ベースメッシュの頂点を直接書き換えるため、モディファイアスタックとの相性に制約がある
- 実機（Blender に入れての）確認は Windows のみ

---

## 6. 今後の展望

### 近い将来 — M4: GPU 化（未着手）

ロードマップ上の次の大きな塊。wgpu 導入、頂点バッファ転送、XPBD ソルバの
compute shader (WGSL) 化、空間ハッシュの GPU 実装、CPU/GPU の結果一致性検証、
頂点数による自動切替。

**ただし着手前に前提が変わっている。** 計画当時は「自己衝突がボトルネックだから
GPU 化する」だったが、最適化の結果、自己衝突オフでも 14,641 頂点で 23ms かかり、
**ボトルネックは制約ソルバ本体に移った**。GPU 化するなら狙いは
「自己衝突の並列化」ではなく「距離制約の彩色 Gauss-Seidel を GPU に載せる」に変わる。
この見直しを最初にやる必要がある。

### 品質面の宿題

- **曲げ剛性の反復数依存を断つ。** これが現状いちばん体験を損ねている。
  既定設定で生地の違いが出ないのは、生地プリセットという機能の意味を薄くする。
  広い間隔の曲げ制約を足す案は**計測して退けた**（硬さは出るが球に巻き付かず板になる）。
  別の定式化（実体的な曲げモデルへの移行など）を検討する余地がある
- **エッジ-エッジ自己衝突**。違反の 0.1% なので優先度は低いが、
  潰れた接触が残る既定 Substeps 4 では効くかもしれない
- **物理的な摩擦係数**への置き換え

### 機能面の候補

- マテリアル単位の物性（1 オブジェクト内で部位ごとに生地を変える）— 要否を検討中
- Blender 標準の Force Field 連携
- モディファイアスタックとの共存の見直し（現状はベースメッシュ直接書き換え）

### 運用面

- **Windows 以外での実機確認**。物理コアは 3 OS で CI を通しており、
  Release ワークフローが 4 プラットフォームぶんの wheel を同梱した zip を作るところまで
  用意してあるが、Linux / macOS の Blender に入れての確認はまだ
- ビューポート描画とパネルの見た目の検証方法（現状は完全に手作業）
- Extensions Platform への公開

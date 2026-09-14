# Blender Cloth Simulation Addon — ロードマップ

技術スタック: Python(Blender連携) + Rust(物理コア, PyO3/maturin) + wgpu(GPU compute)

進捗管理: 各タスクの `[ ]` を `[x]` に変更して更新してください。
実機(Blender)での確認項目は [CHECKPOINTS.md](CHECKPOINTS.md) に集約しています。
`[x]` = 自動テストで検証済み / `[~]` = 実装済みだが Blender 実機確認待ち。

---

## M0: 環境構築・技術検証
**Exit条件**: Blender上のボタン押下でRust関数が呼ばれ結果がコンソールに出る

- [x] Rust開発環境セットアップ(rustup, cargo) — rustc/cargo 1.98.0-nightly 導入済み
- [x] maturin導入、Blender同梱Pythonのバージョン確認 — maturin 1.12.6。**実機のBlenderは5.1、同梱PythonはCPython 3.13**(`%APPDATA%\Blender Foundation\Blender\5.1\scripts\addons\rust_gpu_sdf_addon\__pycache__\*.cpython-313.pyc` から確認)。今回のビルドはabi3-py311(Stable ABI、3.11以降で前方互換)なので3.13でも再ビルドなしで動作する想定
- [x] PyO3で最小Rust拡張モジュール作成(例: ベクトル加算関数) — [rust/cloth_core](rust/cloth_core) に `hello()` / `add()` を実装、abi3-py311でビルド
- [x] ビルド・配布方法の暫定方針決定(dev用ローカルビルド vs 事前コンパイル済みバイナリ同梱) — Blender配布版は`python.exe`を同梱しないため`maturin develop`は不採用。`maturin build`でwheel化し`.pyd`をアドオンフォルダに同梱する方式に決定。[scripts/build.ps1](scripts/build.ps1)で自動化
- [x] Blenderアドオンの骨格作成(bl_info, register/unregister) — [addon/cloth_md/__init__.py](addon/cloth_md/__init__.py)
- [x] Operatorクラス作成、ボタン押下でRust関数呼び出し — [addon/cloth_md/operators.py](addon/cloth_md/operators.py)
- [x] Panel(UI)にボタン配置し、Rustからの戻り値をコンソール出力 — [addon/cloth_md/panels.py](addon/cloth_md/panels.py)
- [x] アドオンを実アドオンフォルダに配置 — `dist\cloth_md.zip`(scripts/package_zip.ps1生成)をBlenderの Install から導入する運用に統一
- [x] Blender 5.1(Python 3.13)実機で動作確認済み(2026-07-17) — Nパネルのボタン押下でコンソールに以下が出力されることを確認:
  ```
  [cloth_md] Hello from Rust (cloth_core)!
  [cloth_md] add(1, 2) = 3.0
  ```
  abi3-py311ビルドのままPython 3.13でも再ビルド不要で動作することを実証

**M0完了 (2026-07-17)**

---

## M1: CPUベース基本クロスシミュレーション
**Exit条件**: Blender内で平面メッシュを布のように垂らして落下させられる

- [x] シミュレーション用データ構造設計(頂点位置・速度・質量、エッジ制約リスト) — [rust/cloth_core/src/sim.rs](rust/cloth_core/src/sim.rs)。物理コアは pyo3 非依存にして `cargo test` で検証可能にした
- [x] BlenderメッシュデータをRustに渡すI/F実装(頂点座標配列の受け渡し) — [addon/cloth_md/mesh_io.py](addon/cloth_md/mesh_io.py)。`foreach_get`/`foreach_set` 使用
- [x] 重力積分(semi-implicit Euler等)実装 — 自由落下が解析解と一致することをテストで確認
- [x] 伸び制約(distance constraint)実装 — XPBD方式。λをサブステップ内で累積する正式なXPBD
- [x] 曲げ制約(bending constraint)実装 — Provot方式(隣接面の対角頂点を距離制約で結ぶ)
- [x] 固定頂点(ピン留め)機能実装 — 頂点グループ指定。inv_mass=0
- [x] フレームごとの更新頂点座標をBlenderメッシュに反映するコールバック実装 — `frame_change_post` ハンドラ
- [x] タイムライン再生・Bake再生との連携確認 — フレーム番号基準の決定的再生 + メモリキャッシュでスクラブ対応。**実機確認は CP-B5**
- [x] シミュレーションパラメータ(反復回数, dt, 重力値)をUIから調整可能に — Solver / Fabric / Forces サブパネル
- [~] 平面メッシュでの落下テストシーン作成・動作確認 — **CP-B4 で確認待ち**

### M1で追加実装した項目(当初計画外)
- [x] 面積×密度からの頂点質量算出(一様質量ではなく物理的な質量分布)
- [x] 速度減衰(damping)
- [x] 風(面積に働く力として実装。密度が高いほどなびきにくい)
- [x] 床面衝突 + 摩擦(M2の本格コリジョンまでの暫定機能)
- [x] 発散検知・例外の握りつぶし(Rust側パニックでBlenderを落とさない)
- [x] 品質指標 `average_stretch_error()` をUIに表示
- [x] Blender内自己診断オペレータ `Run Self Test`

---

## M2: 衝突判定(自己衝突 + コリジョンオブジェクト)
**Exit条件**: 布が人体メッシュに引っかからず自然に覆いかぶさる

- [x] コリジョンオブジェクト指定UI(対象メッシュをBlender上で選択) — Collision パネル。単体オブジェクト + コレクション指定。モディファイア/アーマチュア適用後の評価済みメッシュを使用
- [x] 布-コリジョン間の距離判定・押し出し応答実装 — 三角形最近接点 + 面法線で内外判定し厚み分押し出す
- [x] BVH(Bounding Volume Hierarchy)構築ロジック実装(Rust側) — [rust/cloth_core/src/collision.rs](rust/cloth_core/src/collision.rs)。中央値分割 + 明示スタック探索。総当たりとの一致をテストで確認。動くコライダー用に refit も実装
- [x] 自己衝突検出用spatial hashing実装 — 一様グリッドハッシュ
- [x] 自己衝突応答(頂点押し戻し)実装 — 制約で結ばれた頂点対は除外(伸び制約との綱引き防止)
- [x] 摩擦(簡易)実装 — 接触法線方向の食い込み速度を除去し接線速度を減衰
- [x] パフォーマンス計測基盤作成(頂点数/フレーム時間ログ出力) — `verify_core.py --bench`
- [~] 人体メッシュ(サンプルアバター)を使った被せテスト・調整 — **CP-B9 で確認待ち**(自動テストでは球コライダーで検証済み)

---

## M3: 縫製パターン機能
**Exit条件**: 2枚の布ピースを縫い合わせてシャツの袖のような立体を作れる

- [x] 2Dパターンピース表現方法の決定(平面メッシュ or カーブベース) — **平面メッシュ**を採用。Blenderの編集ツールをそのまま使え、シミュレーション対象と同一表現で済むため
- [x] パターン編集モード(専用Operator/モーダル)の実装 — `Add Pattern Piece`(サイズ・解像度指定の長方形)と `Fill Outline as Pattern`(閉じた輪郭を三角形分割し目標エッジ長まで細分化)。モーダルな独立モードは作らず、Blenderの編集モードを活かす方針
- [x] シーム(縫い目)指定ツール — エッジ選択でペア登録するUI — `Add Seam From Selection`。選択エッジを連結成分に分解し、ちょうど2本のチェーンをシームとして登録。オブジェクトに永続保存され、UIListで管理(有効/無効・反転切替・削除)。[addon/cloth_md/seams.py](addon/cloth_md/seams.py) の中核ロジックは bpy 非依存でテスト済み
- [x] シーム制約をXPBD制約として追加(縫い目の頂点を引き寄せる) — rest_length を initial→0 に補間して徐々に閉じる方式。**弧長で対応付ける**ので頂点数が違うチェーン同士も縫える
- [x] 複数パターンピース間の初期配置(3D空間への展開配置)機能 — ピースは通常のオブジェクトとして自由に配置し、`Join Pattern Pieces` で統合してから縫う運用
- [x] 縫製シミュレーション(徐々に閉じていくアニメーション)実装 — `Seam Close Frames` フレームかけて閉じる
- [x] 縫製後の縫い目破綻検知・デバッグ表示 — ビューポートに縫い線をGPU描画(アクティブなシームは強調表示)。`Validate Seams` で長さの食い違い・無効な頂点参照を検査
- [~] サンプル衣服(袖、簡易シャツ)での動作確認 — **CP-B7 で確認待ち**

---

## M4: GPU化
**Exit条件**: 同一シーンでGPU版がCPU版比◯倍以上高速、かつ結果の視覚的差異が許容範囲

- [ ] wgpu導入、Rustプロジェクトへの統合
- [ ] GPUバッファへの頂点データ転送実装
- [ ] XPBD constraint solverのcompute shader実装(WGSL)
- [ ] 衝突判定のGPU化(spatial hashingのGPU実装)
- [ ] CPU版/GPU版の結果一致性検証(許容誤差設定)
- [ ] ベンチマークスイート作成(頂点数別の処理時間比較)
- [ ] CPU/GPU自動切替ロジック(頂点数閾値 or ユーザー指定)実装
- [ ] 大規模メッシュ(数万頂点)でのリアルタイム性検証

---

## M5: マテリアル物性・品質向上

- [ ] 物性パラメータ(伸縮率、曲げ剛性、摩擦係数、密度)のデータモデル設計
- [ ] パラメータ調整UI(マテリアルごとのプリセット管理)実装
- [ ] 生地プリセット(コットン、シルク、デニム等)の値セット作成
- [x] 風力・外力エフェクトの実装 — 一様風のみ(N/m²の面圧として実装)。乱流・Force Field連携は未対応
- [ ] Blender標準のForce Field連携検討

---

## M6: UX仕上げ・安定化・ドキュメント

- [x] シミュレーションキャッシュ/ベイク機能実装(ディスク書き出し) — `Bake to Disk`。1フレーム1ファイルのバイナリ形式(float32)。ヘッダに頂点数を持ち、メッシュ編集後の食い違いを検出する。[addon/cloth_md/cache_io.py](addon/cloth_md/cache_io.py) は bpy 非依存でテスト済み
- [x] ベイク済みアニメーションの再生・編集フロー確認 — フレーム変更時にキャッシュから復元。`Convert to Shape Keys` でアドオン非依存の形にも変換可能。**ベイク中は各フレームで `frame_set` するため、動くコライダーが正しく追従する**(ライブ再生時の追いつき計算にある制約が無い)
- [x] Rust側パニック時のフォールバック・エラーメッセージ表示実装 — ハンドラで例外を捕捉し、発散検知したら自動停止
- [x] 異常入力(非多様体メッシュ等)に対するバリデーション追加 — 頂点/エッジ無し、孤立頂点、非多様体エッジ、極小エッジ、非一様スケールを検査。致命的なものは開始を中止し、それ以外は警告表示
- [ ] サンプルシーン・チュートリアル作成
- [x] README / インストール手順整備 — [README.md](README.md)
- [ ] Blender Extensions Platform向けパッケージング対応

---

## メモ
- **ビルド時の注意**: PATH 先頭の `WindowsApps\python.exe` はスタブなので pyo3 が見つけられない。
  `scripts/build.ps1` が実体のある Python を探して `PYO3_PYTHON` に設定する。
- **PowerShell スクリプトは UTF-8 BOM 付きで保存すること**。Windows PowerShell 5.1 は
  BOM無しUTF-8をANSIとして読むため、日本語コメントが文字化けして構文エラーになる。
- 現状(2026-09-14): M1・M2・M3 完了、M6 も大半を実装。自動テスト(Rust 20 + Python 55項目)通過。
  Blender実機確認のみ未実施([CHECKPOINTS.md](CHECKPOINTS.md) の CP-B1〜B13)。
  **MDらしさの核心(パターン作成 → 縫い目指定 → 縫製シミュレーション → ベイク)が一通り繋がった状態**。
- **テスト網羅の偏りに注意**: 検証が効いているのは Rust コアと、bpy 非依存に切り出した
  `seams.py` / `cache_io.py` / `transform.py` のみ。残る Blender 依存モジュール
  (`operators.py`, `panels.py`, `sim_state.py`, `bake_ops.py`, `mesh_io.py`,
  `sewing_ops.py`, `overlay.py`, `properties.py`)は **構文チェックしか通っていない**。
  ロジックを足すときは、可能な限り bpy 非依存モジュールへ切り出してテストすること。
- コードレビューで見つけて修正した問題(2026-09-14):
  - `frame_change_post` ハンドラに `@persistent` が無く、**.blend を読み込むと
    ベイク再生が黙って止まっていた**。`load_post` で状態を捨てる処理も追加。
  - `overlay.invalidate_cache()` がどこからも呼ばれておらず、指紋もチェーン長しか
    見ていなかったため、メッシュ編集後に古い頂点番号で描画して例外を出す状態だった。
  - 実行中状態の dict がオブジェクト**名**キーで、リネーム・複製で迷子になっていた
    (`session_uid` キーに変更)。
  - ローカル<->ワールド変換が素の Python ループで、14,641頂点で往復約10ms
    (Rust の step 18.7ms に対して +54%)。numpy 化して往復約2.8msに短縮。
- 2026-09-14 の実機確認で分かったこと(実測は README「パラメータの決め方」に記載):
  - 自己衝突の `Self Thickness` の上限は**メッシュのエッジ長**で決まる(斜め方向の
    非拘束ペアが押し合うため)。超えると平らな布でも座屈し、伸び誤差が3倍になる。
    **細分化は解決にならず逆効果**。根本解決には頂点-三角形の自己衝突が要る(M4/M5)。
  - 収束は `iterations` より `substeps` のほうが安く効く(誤差 1/7 に対しコスト 0.77倍)。
    衝突解決がサブステップごとに1回・制約ループの外なので、**iterations では貫通は直らない**。
  - 衝突後に伸び制約を解き直す `post_collision_iterations` を追加(既定 2)。
    貫通を増やさず伸び誤差を減らす。
- **計測して対処済み(2026-09-15)**: 自己衝突のコストを 14,641頂点で **+41.3ms → +15.0ms(-64%)**
  に削減した。仮説(アロケーションが主因)は**外れ**で、真因は標準 HashMap の **SipHash** だった。
  セルキー `(i64,i64,i64)` に暗号学的ハッシュは過剰なので、乗算とシフトだけの
  `GridHasher` に差し替えた。近傍バッファの使い回しも 16% 分効いた。
  なお「バケットを残して中身だけ空にする」案は**悪化した**(+106ms)。布が動くと
  空バケットが溜まり、毎サブステップ全走査になるため。実装して測らなければ
  気づけなかった。
- **M4(GPU化)の前提が変わった**: 自己衝突はもう最大のコストではない。
  14,641頂点で自己衝突オフでも 23ms かかっており、**ボトルネックは制約ソルバ本体**に移った。
  GPU 化に着手する前に、ここを再計測して的を決め直すこと。
- 残る大物は **M4(GPU化)** と M5(物性プリセット・二面角の曲げ制約)。
  M6 の残件はサンプルシーン作成と Extensions Platform 対応。
- 性能の目安: 6561頂点でコリジョンなし 8.3ms/f、球コライダー付き 10.4ms/f、
  自己衝突を入れると 26ms/f。**自己衝突が最大のコスト** = M4(GPU化)の第一目標。
- 優先順位: M1→M2完了で最低限「製品として意味を持つ」状態。M3(縫製)がMDらしさの核心。M4(GPU化)は機能完成後の性能最適化フェーズ。
- 各マイルストーンの見積もりはM0: 1〜2週間 / M1: 3〜4週間 / M2: 4〜6週間 / M3: 4〜6週間 / M4: 6〜8週間 / M5: 3〜4週間 / M6: 3〜4週間(目安、要調整)。

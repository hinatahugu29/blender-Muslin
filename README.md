# Cloth MD

Blender 用のクロスシミュレーションアドオン。Marvelous Designer のような
**パターンを作って縫い合わせる**衣服制作ワークフローを目指しています。

物理コアは Rust (XPBD) で実装し、PyO3 経由で Blender の Python から呼び出します。

## 現在できること

- **クロスシミュレーション** — XPBD による伸び/曲げ制約、面積ベースの質量分布、
  風(面圧として実装するので生地が重いほどなびきにくい)、減衰、ピン留め
- **コリジョン** — 人体などのメッシュとの衝突(BVH)、自己衝突(空間ハッシュ)、
  床面、摩擦。モディファイアやアーマチュアで変形した状態に追従します
- **縫製** — パターンピースの作成、エッジ選択によるシーム定義、
  弧長ベースの対応付け(頂点数が違う辺同士も縫えます)、縫い線のビューポート表示
- **ベイク** — ディスクへのキャッシュ書き出し、シェイプキーへの変換

## インストール

1. `dist/cloth_md.zip` を用意する(無ければ下記のビルド手順で作る)
2. Blender の Preferences > Add-ons > Install... から zip を選ぶ
3. "Cloth MD" を有効化する
4. 3Dビューの N パネルに "Cloth MD" タブが出る

### 更新するときの注意(Windows)

**必ず Blender を終了してから、古いバージョンを削除してください。**

Windows はロード中の DLL を削除できません。Blender を起動したまま削除・再インストール
すると `cloth_core.pyd` だけが消せず、Blender はアドオンフォルダを
`.~stale~0001` にリネームして退避します。**結果として `.py` だけが消え、
アドオンが壊れた状態になります**(UI に Debug パネルの残骸だけが出る等)。

そうなった場合の復旧:

1. Blender を完全に終了する
2. `%APPDATA%\Blender Foundation\Blender\<版>\scriptsddons\.~stale~*` を削除する
3. Blender を起動して zip を入れ直し、有効化したらもう一度再起動する

`.pyd` が新しくなったかは Debug > `Test Rust Core` のコンソール出力
(`core_version`)で確認できます。

動作確認環境: Blender 5.2 LTS / Windows。
拡張モジュールは abi3-py311 でビルドしているため Python 3.11 以降で動きます。

## 使い方

### 布を垂らす

1. 平面メッシュを用意し、編集モードで細分化する
2. 固定したい頂点を頂点グループに入れ、Pinning で指定する
3. `Start Simulation` を押してタイムラインを再生する

### 人体に着せる

1. Collision パネルで `Collider` に人体メッシュを指定する
2. `Thickness` を布のエッジ長より小さい値にする
3. 動く人体に着せる場合は `Animated Collider` を有効にする

### 縫い合わせる

1. Pattern パネルでサイズと解像度を決めて `Add Pattern Piece`(複数作る)
2. 縫いたいピースを選択して `Join Pattern Pieces`
   （**縫い目は同一オブジェクト内でしか作れません**）
3. 編集モードで、縫い合わせたい**2本のエッジ列を同時に選択**して `Add Seam From Selection`
4. ビューポートに出る縫い線が交差していたら、リストの ⇔ ボタンで向きを反転する
5. `Start Simulation` → 再生すると `Seam Close Frames` かけて縫い合わさる

### ベイクする

`Bake to Disk` でシーンのフレーム範囲を計算し、ディスクに保存します。
`.blend` を閉じても残り、スクラブが即座に効きます。
`Convert to Shape Keys` を使うと、アドオン無しで再生できる形になります。

## ビルド

必要なもの: Rust (rustup/cargo)、maturin、Python 3.11 以降

```bash
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

Rust の単体テストを実行し、`maturin build` で wheel を作り、
`cloth_core.pyd` を `addon/cloth_md/` に配置します。

配布用 zip を作る:

```bash
powershell -ExecutionPolicy Bypass -File scripts/package_zip.ps1
```

## 検証

物理コアは pyo3 に依存しない構造にしてあるため、**Blender を起動せずに検証できます**。

```bash
powershell -ExecutionPolicy Bypass -File scripts/verify_core.ps1 -Bench
```

ビルド済みの `.pyd` を Python から直接叩き、自由落下が解析解に一致するか、
布が球を貫通しないか、縫い目が閉じるか等を検査します(`--bench` で性能計測も)。

Rust 側の単体テストのみを走らせる場合:

```bash
cargo test --no-default-features --release --manifest-path rust/cloth_core/Cargo.toml
```

## 性能の目安

CPU シングルスレッド、iterations=10 / substeps=4:

| 頂点数 | ms/frame |
|--------|----------|
| 1,681 | 2.4 |
| 6,561 | 8.2 |
| 14,641 | 18.7 |

6,561頂点で、球コライダーを足すと 10.4ms、自己衝突を足すと 26.3ms。
**自己衝突が最も重い**ため、GPU化(M4)の第一目標はここです。

この表は Rust コア内部の計測で、Blender との座標受け渡しは含みません。
往復の変換コストは 14,641頂点で約 2.8ms(numpy 化前は約 10ms)です。

## ドキュメント

- [ROADMAP.md](ROADMAP.md) — マイルストーンと進捗
- [CHECKPOINTS.md](CHECKPOINTS.md) — 自動テストの内容と、Blender 実機での確認手順

## 既知の制約

- 連続衝突判定(CCD)が無いため、高速に動くと貫通することがあります
- 自己衝突は頂点同士の反発のみで、頂点-三角形の貫通は完全には防げません
- 別オブジェクトのパターン同士は縫えません(事前に統合が必要)
- メッシュを編集して頂点番号が変わると縫い目が壊れます(`Validate Seams` で検出可)
- ベースメッシュの頂点を直接書き換えるため、モディファイアスタックとの相性に制約があります

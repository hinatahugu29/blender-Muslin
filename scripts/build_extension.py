"""複数プラットフォームの wheel から Extensions 形式の zip を作る。

`package_extension.ps1` は Windows 用の wheel を1つだけ入れる。こちらは
**渡された wheel すべてを入れて、マニフェストの platforms / wheels を
それに合わせて書き換える**。CI で各 OS が作った wheel を集めて1つの
配布物にするために使う。

PowerShell ではなく Python なのは、CI の Linux / macOS でも動かすため。

使い方:
    python scripts/build_extension.py --out dist/muslin-multi.zip wheels/*.whl

wheel のファイル名からプラットフォームを判別する。Blender の
プラットフォーム名は wheel のタグと綴りが違うので対応表で変換する。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "addon" / "muslin"
MANIFEST = ADDON_DIR / "blender_manifest.toml"

# wheel のプラットフォームタグ -> Blender のプラットフォーム名。
# Blender 側の綴りは blender_manifest.toml の仕様で決まっている。
PLATFORM_TAGS: list[tuple[str, str]] = [
    (r"win_amd64", "windows-x64"),
    (r"win_arm64", "windows-arm64"),
    (r"manylinux.*_x86_64|linux_x86_64", "linux-x64"),
    (r"manylinux.*_aarch64|linux_aarch64", "linux-arm64"),
    (r"macosx_.*_arm64", "macos-arm64"),
    (r"macosx_.*_x86_64", "macos-x64"),
]

# パッケージに入れないもの(ビルド生成物やキャッシュ)
EXCLUDE_DIRS = {"__pycache__", "wheels"}
EXCLUDE_SUFFIXES = {".pyd", ".so", ".dylib", ".pyc"}


def platform_of(wheel: Path) -> str:
    """wheel のファイル名から Blender のプラットフォーム名を決める。"""
    name = wheel.name
    for pattern, blender_platform in PLATFORM_TAGS:
        if re.search(pattern, name):
            return blender_platform
    raise SystemExit(f"wheel のプラットフォームを判別できません: {name}")


def rewrite_manifest(text: str, platforms: list[str], wheels: list[str]) -> str:
    """platforms と wheels の行を差し替える。

    TOML を解析して書き戻すと、コメントと並び順が失われる。マニフェストは
    人が読む前提で注釈を入れてあるので、該当行だけを置き換える。
    """
    def fmt(values: list[str]) -> str:
        return "[" + ", ".join(f'"{v}"' for v in values) + "]"

    text, n1 = re.subn(
        r"^platforms = \[[^\]]*\]$",
        "platforms = " + fmt(platforms),
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text, n2 = re.subn(
        r"^wheels = \[[^\]]*\]$",
        "wheels = " + fmt(wheels),
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n1 != 1 or n2 != 1:
        raise SystemExit(
            f"マニフェストの platforms / wheels 行が見つかりません "
            f"(platforms={n1}, wheels={n2})"
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", nargs="+", type=Path, help="同梱する wheel")
    parser.add_argument("--out", type=Path, required=True, help="出力する zip")
    args = parser.parse_args()

    wheels = sorted({w.resolve() for w in args.wheels})
    missing = [w for w in wheels if not w.is_file()]
    if missing:
        raise SystemExit("wheel が見つかりません: " + ", ".join(map(str, missing)))

    # 同じプラットフォームの wheel が複数あると Blender が弾く
    platforms: dict[str, Path] = {}
    for wheel in wheels:
        plat = platform_of(wheel)
        if plat in platforms:
            raise SystemExit(
                f"{plat} の wheel が2つあります: "
                f"{platforms[plat].name} と {wheel.name}"
            )
        platforms[plat] = wheel

    print(f"同梱する wheel: {len(wheels)} 個")
    for plat in sorted(platforms):
        print(f"  {plat:<14} {platforms[plat].name}")

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "muslin"
        stage.mkdir(parents=True)

        # アドオンの .py とマニフェストを入れる。ネイティブモジュールは
        # wheel 側に入るので、.pyd / .so は持ち込まない
        for src in ADDON_DIR.rglob("*"):
            if any(part in EXCLUDE_DIRS for part in src.relative_to(ADDON_DIR).parts):
                continue
            if src.suffix in EXCLUDE_SUFFIXES:
                continue
            if src.is_dir():
                continue
            dst = stage / src.relative_to(ADDON_DIR)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        wheel_dir = stage / "wheels"
        wheel_dir.mkdir()
        for wheel in wheels:
            shutil.copy2(wheel, wheel_dir / wheel.name)

        manifest_text = MANIFEST.read_text(encoding="utf-8")
        manifest_text = rewrite_manifest(
            manifest_text,
            sorted(platforms),
            [f"./wheels/{platforms[p].name}" for p in sorted(platforms)],
        )
        # BOM を付けない。Blender の TOML パーサが弾く
        (stage / "blender_manifest.toml").write_text(manifest_text, encoding="utf-8")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            args.out.unlink()
        with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, Path("muslin") / path.relative_to(stage))

    size = args.out.stat().st_size
    print(f"\n作成: {args.out}  ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

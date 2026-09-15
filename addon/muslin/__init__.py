bl_info = {
    "name": "Muslin",
    "author": "",
    "version": (0, 5, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Muslin",
    "description": "Pattern-sewing cloth simulation for garments (Rust core, WIP)",
    "category": "Object",
}

# --- cloth_core (Rust 拡張モジュール) の解決 ---
#
# 従来形式のアドオン zip では cloth_core.pyd をこのパッケージに同梱するので
# `from . import cloth_core` で読める。一方 Extensions 形式では wheel から
# 入るためトップレベルのモジュールになる。どちらでも他のモジュールが
# `from . import cloth_core` と書けるよう、ここで解決して名前空間に入れる。
import sys as _sys

try:
    from . import cloth_core as _cloth_core   # 従来形式(.pyd を同梱)
except ImportError:                            # pragma: no cover
    import cloth_core as _cloth_core           # Extensions 形式(wheel から)
    _sys.modules[__name__ + ".cloth_core"] = _cloth_core

cloth_core = _cloth_core

from . import previews
from . import properties
from . import sim_state
from . import operators
from . import pin_ops
from . import sewing_ops
from . import bake_ops
from . import overlay
from . import panels

# previews は properties より先に。生地の items がアイコン ID を引くため。
_modules = (previews, properties, sim_state, operators, pin_ops, sewing_ops, bake_ops, overlay, panels)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()


if __name__ == "__main__":
    register()

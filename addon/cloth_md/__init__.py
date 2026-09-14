bl_info = {
    "name": "Cloth MD",
    "author": "",
    "version": (0, 5, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Cloth MD",
    "description": "Marvelous Designer-like cloth simulation (Rust core, WIP)",
    "category": "Object",
}

from . import properties
from . import sim_state
from . import operators
from . import sewing_ops
from . import bake_ops
from . import overlay
from . import panels

_modules = (properties, sim_state, operators, sewing_ops, bake_ops, overlay, panels)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()


if __name__ == "__main__":
    register()

import bpy
print("VERSION", bpy.app.version_string)
print("CURVE_OPS", sorted(n for n in dir(bpy.ops.curve) if not n.startswith("_")))
print("CURVES_OPS", sorted(n for n in dir(bpy.ops.curves) if not n.startswith("_")))
c = bpy.data.hair_curves.new("h")
print("CURVES_ATTR_API", [n for n in dir(c) if not n.startswith("_")][:80])

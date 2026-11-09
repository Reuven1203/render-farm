"""
Finds the simulations in the open .blend that need their frames drawn in order.

Cloth, soft bodies, fluids/smoke, emitter particles, dynamic paint, rigid bodies and Geometry Nodes simulation
zones all build frame N out of frame N-1. Once a simulation is baked its result is stored and any frame can be
drawn on any GPU. An unbaked one has to be stepped through frame by frame, so the farm renders it on one GPU.

Run inside Blender: this file is read by audit_blend.py and by farm.py's prep script, which call find_sims().
"""
import bpy


def _sim_zone(tree, depth=0):
    if tree is None or depth > 4:
        return False
    for n in tree.nodes:
        if n.bl_idname == "GeometryNodeSimulationInput":
            return True
        sub = getattr(n, "node_tree", None)
        if sub is not None and _sim_zone(sub, depth + 1):
            return True
    return False


def find_sims():
    """[{object, kind, baked}] for every simulation that is on for rendering."""
    sims = []

    def add(obj, kind, baked):
        sims.append({"object": obj, "kind": kind, "baked": bool(baked)})

    for ob in bpy.data.objects:
        for m in ob.modifiers:
            try:
                if not m.show_render:
                    continue
                t = m.type
                if t == "CLOTH":
                    add(ob.name, "Cloth", m.point_cache.is_baked)
                elif t == "SOFT_BODY":
                    add(ob.name, "Soft body", m.point_cache.is_baked)
                elif t == "FLUID" and m.fluid_type == "DOMAIN":
                    add(ob.name, "Fluid / smoke", getattr(m.domain_settings, "has_cache_baked_data", False))
                elif t == "PARTICLE_SYSTEM" and m.particle_system.settings.type == "EMITTER":
                    add(ob.name, "Particles", m.particle_system.point_cache.is_baked)
                elif t == "DYNAMIC_PAINT" and m.ui_type == "CANVAS":
                    surfaces = m.canvas_settings.canvas_surfaces
                    add(ob.name, "Dynamic paint", all(s.point_cache.is_baked for s in surfaces) if len(surfaces) else True)
                elif t == "NODES" and _sim_zone(m.node_group):
                    # Blender doesn't say whether a node simulation's bake is inside the file, so assume it isn't
                    add(ob.name, "Geometry Nodes simulation", False)
            except Exception:
                pass  # an unusual modifier must never stop the check
    for sc in bpy.data.scenes:
        try:
            rw = sc.rigidbody_world
            if rw and rw.enabled:
                add("(scene)", "Rigid body", rw.point_cache.is_baked)
        except Exception:
            pass
    return sims

"""Режим «как Corona» поверх Cycles: качество, денойз, VFB, LightMix,
материал Corona Physical, Corona Light, Corona Sun+Sky."""
import math

import bpy
from bpy.app.handlers import persistent

from . import vfb

PRESETS = {
    # проходы (0 — без лимита), шум %, время мин
    "DRAFT": (64, 5.0, 0.0),
    "MEDIUM": (0, 3.0, 0.0),
    "FINAL": (0, 2.0, 0.0),
    "HIGH": (0, 1.0, 0.0),
}
NO_LIMIT_SAMPLES = 16384


def vfb_settings(scene):
    """Настройки VFB сцены словарём (для vfb.build и для узлов сети)."""
    c = scene.corona
    s = {k: getattr(c, k) for k in vfb.DEFAULTS if k not in ("lightmix", "denoise") and hasattr(c, k)}
    s["denoise"] = c.denoise != "OFF"
    s["lightmix"] = [{"group": i.group, "enabled": i.enabled, "color": tuple(i.color),
                      "intensity": i.intensity} for i in c.lightmix]
    return s


def _set(obj, attr, value):
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
        except (TypeError, ValueError):
            pass


def apply_quality(scene):
    c = scene.corona
    if not c.enabled:
        return
    if scene.render.engine != "CYCLES":
        scene.render.engine = "CYCLES"
    cy = scene.cycles
    cy.samples = c.pass_limit if c.pass_limit > 0 else NO_LIMIT_SAMPLES
    cy.time_limit = c.time_limit * 60.0
    cy.use_adaptive_sampling = c.noise_limit > 0
    if c.noise_limit > 0:
        cy.adaptive_threshold = c.noise_limit / 100.0
        cy.adaptive_min_samples = 0
    denoise = c.denoise != "OFF"
    in_comp = denoise and c.vfb_enabled
    cy.use_denoising = denoise and not in_comp
    if denoise:
        _set(cy, "denoiser", "OPENIMAGEDENOISE")
        _set(cy, "denoising_input_passes", "RGB_ALBEDO_NORMAL")
        _set(cy, "denoising_prefilter", "ACCURATE")
        _set(cy, "denoising_quality", "HIGH")
    vl = bpy.context.view_layer if bpy.context.view_layer in scene.view_layers[:] else scene.view_layers[0]
    if in_comp or (c.lightmix_on and c.vfb_enabled):
        vl.cycles.denoising_store_passes = True
    # интерактив во вьюпорте — тоже с денойзом
    _set(cy, "use_preview_denoising", c.viewport_denoise)
    _set(cy, "preview_denoiser", "OPENIMAGEDENOISE")
    # «физичные» установки в духе Corona: много отскоков, без каустик, мягкий клэмп
    cy.max_bounces = c.max_bounces
    for a in ("diffuse_bounces", "glossy_bounces", "transmission_bounces"):
        _set(cy, a, c.max_bounces)
    _set(cy, "transparent_max_bounces", max(32, c.max_bounces))
    _set(cy, "caustics_reflective", c.caustics)
    _set(cy, "caustics_refractive", c.caustics)
    _set(cy, "blur_glossy", 0.0 if c.caustics else 1.0)
    _set(cy, "sample_clamp_direct", 0.0)
    _set(cy, "sample_clamp_indirect", c.max_sample_intensity)
    _set(cy, "use_light_tree", True)
    _set(cy, "filter_width", 1.5)
    if c.path_guiding:
        _set(cy, "device", "CPU")
    _set(cy, "use_guiding", c.path_guiding)


def apply_tonemap(scene):
    c = scene.corona
    try:
        scene.view_settings.view_transform = c.tonemap
    except TypeError:
        pass
    try:
        scene.view_settings.look = "None"
    except TypeError:
        pass


def apply_vfb(scene):
    c = scene.corona
    if not c.enabled or not c.vfb_enabled:
        return
    vl = scene.view_layers[0]
    for v in scene.view_layers:
        if v.use:
            vl = v
            break
    vfb.rebuild_scene(scene, vfb_settings(scene), vl.name)
    apply_tonemap(scene)
    for win in bpy.context.window_manager.windows if bpy.context.window_manager else []:
        for area in win.screen.areas:
            area.tag_redraw()


def apply_all(scene):
    apply_quality(scene)
    apply_vfb(scene)


def disable(scene):
    if vfb.is_tree_ours(vfb.scene_tree(scene, create=False)):
        vfb.release_scene_tree(scene)


# ---------------------------------------------------------------- материал Corona Physical
PHYS_NAME = "Corona Physical"


def corona_physical_group():
    ng = bpy.data.node_groups.get(PHYS_NAME)
    if ng and ng.get("corona_ver") == 1:
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(PHYS_NAME, "ShaderNodeTree")
    ng.nodes.clear()
    ng.interface.clear()
    ng["corona_ver"] = 1

    def inp(name, typ, default=None, lo=None, hi=None):
        s = ng.interface.new_socket(name, in_out="INPUT", socket_type=typ)
        if default is not None:
            s.default_value = default
        if lo is not None:
            s.min_value, s.max_value = lo, hi
        return s

    inp("Цвет", "NodeSocketColor", (0.7, 0.7, 0.7, 1))
    inp("Шероховатость", "NodeSocketFloat", 0.4, 0.0, 1.0)
    inp("Металл", "NodeSocketFloat", 0.0, 0.0, 1.0)
    inp("Уровень отражения", "NodeSocketFloat", 1.0, 0.0, 1.0)
    inp("IOR", "NodeSocketFloat", 1.5, 1.0, 4.0)
    inp("Преломление", "NodeSocketFloat", 0.0, 0.0, 1.0)
    inp("Просвечиваемость", "NodeSocketFloat", 0.0, 0.0, 1.0)
    inp("Непрозрачность", "NodeSocketFloat", 1.0, 0.0, 1.0)
    inp("Покрытие (clearcoat)", "NodeSocketFloat", 0.0, 0.0, 1.0)
    inp("Шероховатость покрытия", "NodeSocketFloat", 0.05, 0.0, 1.0)
    inp("Блеск ткани (sheen)", "NodeSocketFloat", 0.0, 0.0, 1.0)
    inp("Тонкая плёнка, нм", "NodeSocketFloat", 0.0, 0.0, 1000.0)
    inp("Свечение", "NodeSocketColor", (1, 1, 1, 1))
    inp("Сила свечения", "NodeSocketFloat", 0.0, 0.0, 1000.0)
    nrm = inp("Нормаль", "NodeSocketVector")
    nrm.hide_value = True
    ng.interface.new_socket("BSDF", in_out="OUTPUT", socket_type="NodeSocketShader")

    gi = ng.nodes.new("NodeGroupInput")
    gi.location = (-500, 0)
    go = ng.nodes.new("NodeGroupOutput")
    go.location = (400, 0)
    p = ng.nodes.new("ShaderNodeBsdfPrincipled")
    p.location = (0, 0)
    p.subsurface_method = "RANDOM_WALK"
    L = ng.links.new
    L(gi.outputs["Цвет"], p.inputs["Base Color"])
    L(gi.outputs["Шероховатость"], p.inputs["Roughness"])
    L(gi.outputs["Металл"], p.inputs["Metallic"])
    L(gi.outputs["IOR"], p.inputs["IOR"])
    half = ng.nodes.new("ShaderNodeMath")       # уровень отражения 1 → Specular IOR Level 0.5
    half.operation = "MULTIPLY"
    half.inputs[1].default_value = 0.5
    half.location = (-250, -150)
    L(gi.outputs["Уровень отражения"], half.inputs[0])
    L(half.outputs[0], p.inputs["Specular IOR Level"])
    L(gi.outputs["Преломление"], p.inputs["Transmission Weight"])
    L(gi.outputs["Просвечиваемость"], p.inputs["Subsurface Weight"])
    L(gi.outputs["Непрозрачность"], p.inputs["Alpha"])
    L(gi.outputs["Покрытие (clearcoat)"], p.inputs["Coat Weight"])
    L(gi.outputs["Шероховатость покрытия"], p.inputs["Coat Roughness"])
    L(gi.outputs["Блеск ткани (sheen)"], p.inputs["Sheen Weight"])
    if "Thin Film Thickness" in p.inputs:
        L(gi.outputs["Тонкая плёнка, нм"], p.inputs["Thin Film Thickness"])
    L(gi.outputs["Свечение"], p.inputs["Emission Color"])
    L(gi.outputs["Сила свечения"], p.inputs["Emission Strength"])
    L(gi.outputs["Нормаль"], p.inputs["Normal"])
    L(p.outputs[0], go.inputs[0])
    return ng


def new_physical_material(name="Corona Physical", color=None):
    m = bpy.data.materials.new(name)
    if hasattr(m, "use_nodes") and not m.node_tree:
        m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    g = nt.nodes.new("ShaderNodeGroup")
    g.node_tree = corona_physical_group()
    g.width = 240
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (350, 0)
    nt.links.new(g.outputs[0], out.inputs["Surface"])
    if color:
        g.inputs["Цвет"].default_value = color
    return m


# ---------------------------------------------------------------- свет
def kelvin_to_rgb(k):
    """Цвет абсолютно чёрного тела (приближение Таннера Хелланда), линейный RGB."""
    t = max(1000.0, min(40000.0, k)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    def lin(v):
        v = max(0.0, min(255.0, v)) / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (lin(r), lin(g), lin(b))


def set_light_kelvin(light, kelvin):
    if hasattr(light, "use_temperature"):
        light.use_temperature = True
        light.temperature = kelvin
        light.color = (1, 1, 1)
    else:
        light.color = kelvin_to_rgb(kelvin)


def sun_angles(obj):
    """Высота и поворот солнца для текстуры неба из направления объекта Sun."""
    d = obj.matrix_world.to_3x3() @ __import__("mathutils").Vector((0, 0, 1))  # к солнцу
    d.normalize()
    elevation = math.asin(max(-1.0, min(1.0, d.z)))
    rotation = math.atan2(d.y, d.x) - math.pi / 2
    return elevation, rotation % (2 * math.pi)


def sky_node(world):
    if not world or not world.node_tree:
        return None
    return next((n for n in world.node_tree.nodes if n.type == "TEX_SKY"), None)


def sync_sun_sky(scene):
    sun = scene.corona.sun_object
    sky = sky_node(scene.world)
    if not sun or not sky:
        return False
    el, rot = sun_angles(sun)
    if abs(sky.sun_elevation - el) > 1e-4 or abs(sky.sun_rotation - rot) > 1e-4:
        sky.sun_elevation = el
        sky.sun_rotation = rot
    return True


@persistent
def depsgraph_handler(scene, depsgraph):
    try:
        if scene.corona.sun_object and scene.corona.sun_sync:
            for upd in depsgraph.updates:
                if upd.is_updated_transform and getattr(upd.id, "original", None) == scene.corona.sun_object:
                    sync_sun_sky(scene)
                    break
    except (AttributeError, ReferenceError):
        pass


def create_sun_sky(scene, context):
    world = scene.world or bpy.data.worlds.new("Corona Sky")
    scene.world = world
    if hasattr(world, "use_nodes") and not world.node_tree:
        world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    sky = nt.nodes.new("ShaderNodeTexSky")
    types = [i.identifier for i in sky.bl_rna.properties["sky_type"].enum_items]
    for t in ("MULTIPLE_SCATTERING", "NISHITA", "HOSEK_WILKIE"):
        if t in types:
            sky.sky_type = t
            break
    _set(sky, "sun_disc", False)  # диск даёт лампа Sun — без двойного солнца
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = 0.25
    out = nt.nodes.new("ShaderNodeOutputWorld")
    sky.location, bg.location, out.location = (-400, 0), (-150, 0), (100, 0)
    nt.links.new(sky.outputs[0], bg.inputs[0])
    nt.links.new(bg.outputs[0], out.inputs[0])
    sun_data = bpy.data.lights.new("Corona Sun", "SUN")
    sun_data.energy = 4.0
    sun_data.angle = math.radians(0.53)
    sun = bpy.data.objects.new("Corona Sun", sun_data)
    context.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(55), 0, math.radians(35))
    scene.corona.sun_object = sun
    scene.corona.sun_sync = True
    context.view_layer.update()
    sync_sun_sky(scene)
    return sun


# ---------------------------------------------------------------- LightMix
def _safe_group_name(name, used):
    base = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in name).strip("_") or "light"
    n, i = base, 2
    while n in used:
        n = f"{base}_{i}"
        i += 1
    used.add(n)
    return n


def setup_lightmix(scene, view_layer):
    """Light group на каждый источник света + отдельная для окружения (как LightMix в Corona).
    Светящиеся материалы без группы в LightMix не попадают."""
    used = {g.name for g in view_layer.lightgroups}
    made = 0
    for o in scene.objects:
        if o.type != "LIGHT" or (o.lightgroup and o.lightgroup in used):
            continue
        name = _safe_group_name(o.name, used)
        view_layer.lightgroups.add(name=name)
        o.lightgroup = name
        made += 1
    if scene.world and hasattr(scene.world, "lightgroup") and not scene.world.lightgroup:
        name = _safe_group_name("Environment", used)
        view_layer.lightgroups.add(name=name)
        scene.world.lightgroup = name
        made += 1
    refresh_lightmix(scene, view_layer)
    return made


def refresh_lightmix(scene, view_layer):
    lm = scene.corona.lightmix
    have = {i.group: i for i in lm}
    names = [g.name for g in view_layer.lightgroups]
    for i in reversed(range(len(lm))):
        if lm[i].group not in names:
            lm.remove(i)
    for n in names:
        if n not in have:
            it = lm.add()
            it.group = n

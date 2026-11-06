"""Проверка установленного аддона (запускает tests/run_tests.sh в отдельном профиле Blender).
Режим Corona, VFB, материалы/свет, LightMix, локальный рендер и сетевой рендер через
операторы аддона: «Кадр» с Corona VFB, «Кадр» обычным рендером с компоновщиком сцены,
«Анимация» обычным рендером."""
import os
import sys
import time
import traceback

import addon_utils
import bpy

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_addon")
os.makedirs(OUT, exist_ok=True)
fails = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def mod_name():
    for m in addon_utils.modules():
        if m.__name__.endswith(".corona_dr"):
            return m.__name__
    return None


def scene_setup():
    sc = bpy.context.scene  # read_factory_settings выключил бы аддон — чистим сцену вручную
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 200, 112, 100
    bpy.ops.mesh.primitive_plane_add(size=20)
    bpy.ops.mesh.primitive_cube_add(location=(-1.2, 0, 1))
    bpy.ops.mesh.primitive_uv_sphere_add(location=(1.3, 0.3, 1))
    bpy.ops.object.camera_add(location=(0, -8, 3.2), rotation=(1.25, 0, 0))
    sc.camera = bpy.context.object
    sc.frame_start, sc.frame_end = 1, 2
    path = os.path.join(OUT, "addon_scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return sc


def wait_runner(dr_ops, timeout=300):
    r = dr_ops.RUNNER
    t0 = time.time()
    while not r.done and time.time() - t0 < timeout:
        time.sleep(0.3)
    dr_ops._update_ui()
    return r


def main():
    name = mod_name()
    check(name is not None, f"аддон установлен: {name}")
    if not name:
        return
    if name not in bpy.context.preferences.addons:
        addon_utils.enable(name, default_set=True)
    check(name in bpy.context.preferences.addons, "аддон включён")
    pkg = sys.modules[name]
    corona = sys.modules[name + ".corona"]
    dr_ops = sys.modules[name + ".dr_ops"]
    vfb = sys.modules[name + ".vfb"]
    prefs = bpy.context.preferences.addons[name].preferences
    prefs.node_port = 19420
    prefs.cache_dir = os.path.join(OUT, "node_cache")

    sc = scene_setup()
    c = sc.corona
    c.enabled = True
    c.preset = "DRAFT"
    check(sc.cycles.samples == 64 and abs(sc.cycles.adaptive_threshold - 0.05) < 1e-6,
          f"пресет «Черновик»: проходы {sc.cycles.samples}, шум {sc.cycles.adaptive_threshold}")
    check(not sc.cycles.use_denoising and sc.view_layers[0].cycles.denoising_store_passes,
          "денойз перенесён в компоновщик (albedo/normal сохраняются)")
    tree = vfb.scene_tree(sc, create=False)
    check(vfb.is_tree_ours(tree) and any(n.bl_idname == "CompositorNodeDenoise" for n in tree.nodes),
          "цепочка VFB с денойзом в компоновщике")
    c.exposure = 0.5
    c.wb_kelvin = 4500
    c.bloom_on = True
    c.glare_on = True
    c.vignette = 0.5
    c.highlight_compress = 3
    c.sharpen = 0.3
    kinds = sorted({n.bl_idname for n in vfb.scene_tree(sc).nodes})
    print("   узлы VFB:", kinds)
    check({"CompositorNodeExposure", "CompositorNodeColorBalance", "CompositorNodeGlare",
           "CompositorNodeFilter"} <= set(kinds), "все этапы VFB в дереве")

    for o in bpy.context.selected_objects:
        o.select_set(False)
    cube = [o for o in sc.objects if o.name.startswith("Cube")][0]
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube
    bpy.ops.corona.add_material()
    check(cube.active_material and any(n.type == "GROUP" for n in cube.active_material.node_tree.nodes),
          "материал Corona Physical назначен")
    bpy.ops.corona.add_light(kelvin=3200, power=500, size=1.5)
    lamp = bpy.context.object
    lamp.location = (0, -3, 4)
    check(lamp.type == "LIGHT" and lamp.data.type == "AREA", "Corona Light (Area + Kelvin) создан")
    bpy.ops.corona.add_sun_sky()
    sky = corona.sky_node(sc.world)
    check(sky is not None and sc.corona.sun_object is not None, f"Sun + Sky ({sky.sky_type if sky else '-'})")
    el, rot = corona.sun_angles(sc.corona.sun_object)
    check(abs(sky.sun_elevation - el) < 1e-4, f"небо следует за солнцем: высота {el:.3f}")
    bpy.ops.corona.lightmix_setup()
    groups = [g.name for g in bpy.context.view_layer.lightgroups]
    check(len(groups) >= 3 and len(c.lightmix) == len(groups), f"LightMix: группы {groups}")
    c.lightmix[0].color = (1.0, 0.5, 0.2)
    c.lightmix[0].intensity = 2.0
    mix_nodes = [n for n in vfb.scene_tree(sc).nodes if n.bl_idname in ("ShaderNodeMix", "CompositorNodeMixRGB")]
    check(len(mix_nodes) >= len(groups), "LightMix собран в компоновщике")

    # локальный рендер в режиме Corona
    sc.render.filepath = os.path.join(OUT, "local_corona.png")
    sc.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)
    ok = os.path.exists(sc.render.filepath)
    if ok:
        img = bpy.data.images.load(sc.render.filepath)
        px = list(img.pixels)
        mean = sum(px[i] for i in range(0, len(px), 4)) / (len(px) / 4)
        ok = mean > 0.05
    check(ok, "локальный рендер Corona не чёрный")
    bpy.ops.wm.save_mainfile()

    # сетевой рендер «Кадр» с Corona VFB через операторы
    dr = sc.corona_dr
    dr.head_renders = True
    dr.mode = "FRAME"
    dr.tiles = 4
    dr.output_dir = os.path.join(OUT, "dr_frame")
    bpy.ops.corona_dr.discover()
    print("   найдено:", [(n.name, n.host, n.port) for n in dr.nodes])
    res = bpy.ops.corona_dr.start()
    check(res == {"FINISHED"}, "сетевой рендер запущен оператором")
    r = wait_runner(dr_ops)
    check(r.ok and os.path.exists(r.final_path), f"«Кадр» (Corona): {dr.job_status}")
    check(bpy.data.images.get("Corona DR") is not None, "результат загружен как изображение «Corona DR»")

    # обычный рендер: Corona выключен, свой компоновщик сцены + денойз Cycles
    c.enabled = False
    check(not vfb.is_tree_ours(vfb.scene_tree(sc, create=False)), "Corona выключен — компоновщик сцены освобождён")
    sc.cycles.samples = 32
    sc.cycles.use_adaptive_sampling = False
    sc.cycles.use_denoising = True
    if hasattr(sc, "compositing_node_group"):
        ng = bpy.data.node_groups.new("Мой компоновщик", "CompositorNodeTree")
        ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        sc.compositing_node_group = ng
        rl = ng.nodes.new("CompositorNodeRLayers")
        inv = ng.nodes.new("CompositorNodeInvert")
        out = ng.nodes.new("NodeGroupOutput")
        ng.links.new(rl.outputs["Image"], inv.inputs["Color"])
        ng.links.new(inv.outputs[0], out.inputs[0])
    sc.render.use_compositing = True
    dr.post_mode = "AUTO"
    dr.output_dir = os.path.join(OUT, "dr_plain")
    bpy.ops.corona_dr.start()
    r = wait_runner(dr_ops)
    check(r.ok and os.path.exists(r.final_path), f"«Кадр» обычным рендером: {dr.job_status}")
    if r.ok:
        # эталон: обычный локальный рендер той же сцены (денойз Cycles + компоновщик сцены)
        sc.render.filepath = os.path.join(OUT, "local_plain.png")
        bpy.ops.render.render(write_still=True)
        a = bpy.data.images.load(r.final_path)
        b = bpy.data.images.load(sc.render.filepath)
        pa, pb = list(a.pixels), list(b.pixels)
        diff = sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)
        check(tuple(a.size) == tuple(b.size) and diff < 0.03,
              f"сетевой кадр совпадает с обычным рендером (размер {tuple(a.size)}, разница {diff:.4f})")

    # «Анимация» обычным рендером, кадры 1–2 по весам
    sc.compositing_node_group = None if hasattr(sc, "compositing_node_group") else None
    dr.mode = "ANIM"
    dr.distribution = "AUTO"
    sc.render.filepath = os.path.join(OUT, "anim", "f_")
    bpy.ops.corona_dr.start()
    r = wait_runner(dr_ops)
    files = sorted(os.listdir(os.path.join(OUT, "anim"))) if os.path.isdir(os.path.join(OUT, "anim")) else []
    check(r.ok and files == ["f_0001.png", "f_0002.png"], f"«Анимация» обычным рендером: {files}")

    # EEVEE: сетевой рендер не привязан к Cycles
    eevee = [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
             if "EEVEE" in i.identifier]
    if eevee:
        sc.render.engine = eevee[0]
        sc.render.filepath = os.path.join(OUT, "anim_eevee", "e_")
        bpy.ops.corona_dr.start()
        r = wait_runner(dr_ops)
        d = os.path.join(OUT, "anim_eevee")
        files = sorted(os.listdir(d)) if os.path.isdir(d) else []
        check(r.ok and files == ["e_0001.png", "e_0002.png"], f"«Анимация» на {eevee[0]}: {files}")
        dr.mode = "FRAME"
        dr.output_dir = os.path.join(OUT, "dr_eevee")
        bpy.ops.corona_dr.start()
        r = wait_runner(dr_ops)
        check(r.ok and os.path.exists(r.final_path), f"«Кадр» на {eevee[0]}: {dr.job_status}")
    dr_ops.stop_node()


try:
    main()
except Exception:
    traceback.print_exc()
    fails.append("исключение")
print("TEST_FAIL: " + "; ".join(fails) if fails else "TEST_OK", flush=True)
sys.stdout.flush()
os._exit(1 if fails else 0)

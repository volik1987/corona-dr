"""Автотест сетевого рендера. Запуск (из папки blender_corona):

    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tests/test_dr.py

Что проверяется:
  1. поиск узлов UDP: находятся оба локальных узла (порты 19410 и 19411);
  2. «Кадр»: два узла рендерят кадр по 6 частям, головной собирает, денойзит и
     применяет VFB; размер совпадает с цельным рендером, сырой Combined совпадает
     попиксельно, на границах частей нет швов;
  3. «Анимация»: кадры 1–4 по ручным диапазонам (узел A: 1–2, узел B: 3–4);
  4. переотправка: третий «узел» мёртв, его кадры доделывают живые.
Итог: строка TEST_OK или TEST_FAIL, результаты в tests/out/."""
import os
import shutil
import subprocess
import sys
import time
import traceback

import bpy
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "corona_dr")
sys.path.insert(0, ADDON)
sys.path.insert(0, os.path.join(ADDON, "dr"))
import protocol as P  # noqa: E402
import master as M  # noqa: E402
import vfb  # noqa: E402

OUT = os.path.join(ROOT, "tests", "out")
BLENDER = bpy.app.binary_path
PORTS = (19410, 19411)
W, H = 320, 180
fails = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def make_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 48
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.adaptive_threshold = 0.02
    sc.cycles.use_denoising = False
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = W, H, 100
    sc.render.image_settings.file_format = "PNG"
    sc.view_settings.view_transform = "AgX"
    sc.frame_start, sc.frame_end = 1, 4
    bpy.ops.mesh.primitive_plane_add(size=20)
    bpy.ops.mesh.primitive_cube_add(location=(-1.2, 0, 1))
    cube = bpy.context.object
    cube.animation_data_create()
    cube.rotation_euler = (0, 0, 0)
    cube.keyframe_insert("rotation_euler", frame=1)
    cube.rotation_euler = (0, 0, 1.2)
    cube.keyframe_insert("rotation_euler", frame=4)
    bpy.ops.mesh.primitive_uv_sphere_add(location=(1.3, 0.3, 1), segments=48, ring_count=24)
    bpy.ops.object.shade_smooth()
    sph = bpy.context.object
    m = bpy.data.materials.new("glossy")
    p = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    p.inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1)
    p.inputs["Roughness"].default_value = 0.15
    sph.data.materials.append(m)
    bpy.ops.object.light_add(type="AREA", location=(0, -3, 5))
    key = bpy.context.object
    key.data.energy = 600
    key.data.size = 2
    key.rotation_euler = (0.6, 0, 0)
    bpy.ops.object.light_add(type="POINT", location=(3, 2, 2))
    rim = bpy.context.object
    rim.data.energy = 300
    rim.data.color = (0.3, 0.5, 1.0)
    vl = sc.view_layers[0]
    vl.lightgroups.add(name="key")
    vl.lightgroups.add(name="rim")
    key.lightgroup = "key"
    rim.lightgroup = "rim"
    bpy.ops.object.camera_add(location=(0, -8, 3.2), rotation=(1.25, 0, 0))
    sc.camera = bpy.context.object
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.color = (0.05, 0.05, 0.06)
    path = os.path.join(OUT, "scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return path


VFB = {"exposure": 0.3, "wb_kelvin": 5500, "highlight_compress": 2.0, "contrast": 10,
       "saturation": 0.1, "bloom_on": True, "bloom_intensity": 0.4, "bloom_size": 0.5,
       "bloom_threshold": 0.8, "glare_on": True, "glare_intensity": 0.15,
       "vignette": 0.4, "sharpen": 0.2, "denoise": True,
       "lightmix_on": True, "lightmix": [
           {"group": "key", "enabled": True, "color": (1, 0.9, 0.8), "intensity": 1.0},
           {"group": "rim", "enabled": True, "color": (1, 1, 1), "intensity": 1.5}]}


def read(path, channels=None):
    import OpenImageIO as oiio
    buf = oiio.ImageBuf(path)
    arr = buf.get_pixels(oiio.FLOAT)
    names = list(buf.spec().channelnames)
    if channels:
        idx = [names.index(c) for c in channels]
        arr = arr[:, :, idx]
    return arr, names


def start_nodes():
    procs = []
    for i, port in enumerate(PORTS):
        cache = os.path.join(OUT, f"cache_{port}")
        shutil.rmtree(cache, ignore_errors=True)
        cmd = [BLENDER, "-b", "--factory-startup", "--python", os.path.join(ADDON, "node_cli.py"),
               "--", "--port", str(port), "--name", "ABCD"[i], "--cache", cache, "--threads", "4"]
        logf = open(os.path.join(OUT, f"node_{port}.log"), "w")
        procs.append((subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT), logf))
    t0 = time.time()
    while time.time() - t0 < 30:
        ready = 0
        for port in PORTS:
            try:
                P.connect("127.0.0.1", port, timeout=0.5).close()
                ready += 1
            except OSError:
                pass
        if ready == len(PORTS):
            return procs
        time.sleep(0.5)
    raise RuntimeError("узлы не запустились")


def run_tile_job(blend, nodes, tiles, name):
    wd = os.path.join(OUT, name)
    shutil.rmtree(wd, ignore_errors=True)
    final = os.path.join(wd, "final.png")
    job = M.build_tile_job(blend, nodes, W, H, tiles, "GRID", 1, "ViewLayer", wd, BLENDER,
                           VFB, True, final, key="", log=lambda m: print("   ", m, flush=True),
                           post_log=lambda s: open(os.path.join(OUT, name + "_post.log"), "w").write(s))
    job.start()
    job.wait(600)
    return job, wd


def test_discovery():
    print("== поиск узлов", flush=True)
    found = P.discover(timeout=1.5)
    ports = sorted(f["port"] for f in found if f["port"] in PORTS)
    print("   найдено:", [(f["name"], f["host"], f["port"]) for f in found], flush=True)
    check(ports == list(PORTS), f"UDP-поиск нашёл оба узла {ports}")


def test_tiles(blend):
    print("== режим «Кадр»: 6 частей на 2 узлах", flush=True)
    nodes = [M.NodeState("127.0.0.1", p) for p in PORTS]
    job, wd = run_tile_job(blend, nodes, 6, "tiles6")
    check(job.ok, f"задание выполнено ({job.status} {job.error})")
    if not job.ok:
        return
    by_node = {n.name: n.done for n in nodes}
    check(all(v > 0 for v in by_node.values()), f"части рендерили оба узла {by_node}")
    print("== цельный рендер (1 часть) для сравнения", flush=True)
    ref, wd1 = run_tile_job(blend, [M.NodeState("127.0.0.1", PORTS[0])], 1, "tiles1")
    check(ref.ok, "цельный рендер выполнен")
    if not ref.ok:
        return
    # прямой цельный рендер без границ — эталон «сырых» пикселей
    print("== прямой рендер без разбиения", flush=True)
    bpy.ops.wm.open_mainfile(filepath=blend)
    sc = bpy.context.scene
    sc.view_layers[0].cycles.denoising_store_passes = True
    sc.render.use_compositing = False
    sc.render.image_settings.media_type = "MULTI_LAYER_IMAGE"
    sc.render.image_settings.file_format = "OPEN_EXR_MULTILAYER"
    sc.render.image_settings.color_depth = "32"
    sc.render.filepath = os.path.join(OUT, "direct.exr")
    sc.render.use_file_extension = False
    bpy.ops.render.render(write_still=True)

    comb = ["ViewLayer.Combined.R", "ViewLayer.Combined.G", "ViewLayer.Combined.B"]
    a, names = read(os.path.join(wd, "assembled.exr"), comb)
    d, _ = read(os.path.join(OUT, "direct.exr"), comb)
    check(a.shape == d.shape == (H, W, 3), f"размер собранного {a.shape} = цельного {d.shape}")
    need = {"ViewLayer.Denoising Albedo.R", "ViewLayer.Denoising Normal.X", "ViewLayer.Combined_key.R"}
    check(need <= set(names), "в собранном EXR есть albedo, normal и light groups")
    diff = np.abs(a - d)
    rel = diff.mean() / max(d.mean(), 1e-6)
    print(f"   сырой Combined: средняя разница {diff.mean():.2e} (отн. {rel:.2e}), максимум {diff.max():.3f}")
    check(rel < 0.01, "сырые пиксели собранного кадра совпадают с цельным рендером")

    f6, _ = read(os.path.join(wd, "final.png"))
    f1, _ = read(os.path.join(wd1, "final.png"))
    check(f6.shape == f1.shape and f6.shape[:2] == (H, W), f"итог 6 частей {f6.shape} = итог цельного {f1.shape}")
    dd = np.abs(f6[:, :, :3] - f1[:, :, :3]).mean(axis=2)
    tiles = M.make_tiles(W, H, 6, "GRID")
    xs = sorted({t[0] for t in tiles if t[0] > 0})
    ys = sorted({H - t[1] for t in tiles if t[1] > 0})
    seam = np.zeros((H, W), bool)
    for x in xs:
        seam[:, max(0, x - 1):x + 1] = True
    for y in ys:
        seam[max(0, y - 1):y + 1, :] = True
    s_mean, o_mean = dd[seam].mean(), dd[~seam].mean()
    print(f"   итог: разница на швах {s_mean:.4f}, вне швов {o_mean:.4f}, общая {dd.mean():.4f}")
    check(dd.mean() < 0.01, "итог по частям совпадает с цельным (средняя разница < 1%)")
    check(s_mean < max(2.0 * o_mean, 0.004), "на границах частей нет швов")
    # «ступенька» яркости поперёк границы против соседних колонок внутри части
    g = f6[:, :, :3].mean(axis=2)
    jumps = [np.abs(g[:, x] - g[:, x - 1]).mean() for x in xs]
    inner = [np.abs(g[:, x + 5] - g[:, x + 4]).mean() for x in xs]
    print(f"   скачок через шов {np.mean(jumps):.4f}, рядом внутри части {np.mean(inner):.4f}")
    check(np.mean(jumps) < 2.0 * np.mean(inner) + 0.003, "нет скачка яркости на вертикальных швах")
    shutil.copy(os.path.join(wd, "final.png"), os.path.join(OUT, "frame_tiles.png"))
    shutil.copy(os.path.join(wd1, "final.png"), os.path.join(OUT, "frame_whole.png"))


def test_anim(blend):
    print("== режим «Анимация»: кадры 1–4, A: 1–2, B: 3–4", flush=True)
    nodes = [M.NodeState("127.0.0.1", p) for p in PORTS]
    od = os.path.join(OUT, "anim")
    shutil.rmtree(od, ignore_errors=True)
    job = M.build_anim_job(blend, nodes, [1, 2, 3, 4],
                           lambda f, ext: os.path.join(od, f"kadr_{f:04d}{ext}"),
                           os.path.join(OUT, "anim_work"), distribution="MANUAL",
                           node_frames=[M.parse_frames("1-2"), M.parse_frames("3-4")],
                           steal=False, log=lambda m: print("   ", m, flush=True))
    job.start()
    job.wait(600)
    check(job.ok, f"анимация выполнена ({job.status} {job.error})")
    files = sorted(os.listdir(od)) if os.path.isdir(od) else []
    check(files == [f"kadr_{i:04d}.png" for i in range(1, 5)], f"в output-папке кадры 1–4: {files}")
    check(nodes[0].done_labels == ["кадр 1", "кадр 2"], f"узел A сделал {nodes[0].done_labels}")
    check(nodes[1].done_labels == ["кадр 3", "кадр 4"], f"узел B сделал {nodes[1].done_labels}")
    if len(files) == 4:
        a, _ = read(os.path.join(od, files[0]))
        b, _ = read(os.path.join(od, files[3]))
        check(np.abs(a - b).mean() > 0.002, "кадры анимации отличаются (куб вращается)")


def test_failover(blend):
    print("== переотправка: узел C мёртв, ему назначены кадры 3–4", flush=True)
    nodes = [M.NodeState("127.0.0.1", p) for p in PORTS] + [M.NodeState("127.0.0.1", 19419, "C")]
    od = os.path.join(OUT, "anim_failover")
    shutil.rmtree(od, ignore_errors=True)
    job = M.build_anim_job(blend, nodes, [1, 2, 3, 4],
                           lambda f, ext: os.path.join(od, f"kadr_{f:04d}{ext}"),
                           os.path.join(OUT, "anim_work2"), distribution="MANUAL",
                           node_frames=[[1], [2], [3, 4]], steal=False,
                           log=lambda m: print("   ", m, flush=True))
    job.retry_delay = 0.3
    job.start()
    job.wait(600)
    files = sorted(os.listdir(od)) if os.path.isdir(od) else []
    check(job.ok and len(files) == 4, f"все 4 кадра готовы без узла C: {files}")
    check(not nodes[2].alive, "узел C помечен недоступным")


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    blend = make_scene()
    procs = start_nodes()
    try:
        test_discovery()
        test_tiles(blend)
        test_anim(blend)
        test_failover(blend)
    except Exception:
        traceback.print_exc()
        fails.append("исключение")
    finally:
        for p, f in procs:
            p.terminate()
            f.close()
    print("TEST_FAIL: " + "; ".join(fails) if fails else "TEST_OK", flush=True)
    sys.stdout.flush()
    os._exit(1 if fails else 0)


main()

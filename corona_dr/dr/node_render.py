"""Скрипт, который узел запускает внутри `blender -b сцена.blend -Y --python node_render.py -- task.json`.
Рендерит либо часть кадра (тайл) в многослойный EXR, либо целые кадры анимации.
Готовые файлы печатаются строками `CDR_FILE путь`, ошибки — `CDR_ERROR текст`."""
import json
import os
import sys
import traceback

import bpy


def log(msg):
    print(msg, flush=True)


def set_multilayer_exr(imf):
    if hasattr(imf, "media_type"):  # Blender 5.x
        imf.media_type = "MULTI_LAYER_IMAGE"
    imf.file_format = "OPEN_EXR_MULTILAYER"
    imf.color_depth = "32"
    imf.exr_codec = "ZIP"


def only_view_layer(scene, name):
    vl = scene.view_layers.get(name) if name else None
    if vl is None:
        vl = scene.view_layers[0]
    for other in scene.view_layers:
        other.use = other == vl
    if hasattr(scene.render, "use_single_layer"):
        scene.render.use_single_layer = False
    return vl


def render_tile(scene, task):
    r = scene.render
    W, H = task["W"], task["H"]
    t = task["tile"]
    vl = only_view_layer(scene, task.get("view_layer"))
    scene.frame_set(task["frame"])
    # пол-пикселя сдвига: border*W даёт ровно нужный пиксель и при округлении, и при отбрасывании
    r.use_border = True
    r.use_crop_to_border = True
    r.border_min_x = (t["x0"] + 0.25) / W
    r.border_max_x = (t["x1"] + 0.25) / W
    r.border_min_y = (t["y0"] + 0.25) / H
    r.border_max_y = (t["y1"] + 0.25) / H
    r.use_compositing = False          # постобработка — после сборки на головном
    if hasattr(r, "use_sequencer"):
        r.use_sequencer = False
    scene.cycles.use_denoising = False  # денойз — после сборки, иначе швы
    if task.get("denoise_passes"):
        vl.cycles.denoising_store_passes = True
    set_multilayer_exr(r.image_settings)
    r.use_file_extension = True
    path = os.path.join(task["out_dir"], "tile_%s" % task["tile_id"])
    r.filepath = path
    bpy.ops.render.render(write_still=True, scene=scene.name)
    out = path + ".exr"
    if not os.path.exists(out):
        cand = [f for f in os.listdir(task["out_dir"]) if f.startswith("tile_") and f.endswith(".exr")]
        if not cand:
            raise RuntimeError("EXR тайла не записан")
        out = os.path.join(task["out_dir"], cand[0])
    log("CDR_FILE " + out)


def render_frames(scene, task):
    r = scene.render
    imf = r.image_settings
    is_video = getattr(imf, "media_type", "") == "VIDEO" or imf.file_format in {"FFMPEG", "AVI_JPEG", "AVI_RAW"}
    if is_video:  # кадры по сети собираем картинками
        if hasattr(imf, "media_type"):
            imf.media_type = "IMAGE"
        imf.file_format = "PNG"
    r.use_file_extension = True
    r.use_overwrite = True
    r.use_placeholder = False
    for f in task["frames"]:
        scene.frame_set(f)
        name = "frame_%05d" % f
        r.filepath = os.path.join(task["out_dir"], name)
        bpy.ops.render.render(write_still=True, scene=scene.name)
        found = [x for x in os.listdir(task["out_dir"]) if x.startswith(name + ".")]
        if not found:
            raise RuntimeError("кадр %d не записан" % f)
        log("CDR_FILE " + os.path.join(task["out_dir"], found[0]))


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], encoding="utf-8") as fh:
        task = json.load(fh)
    scene = bpy.context.scene
    if task.get("scene"):
        scene = bpy.data.scenes.get(task["scene"]) or scene
        win = bpy.context.window
        if win is not None:
            win.scene = scene
    if task.get("device") == "CPU":
        scene.cycles.device = "CPU"
    if task["mode"] == "tile":
        render_tile(scene, task)
    else:
        render_frames(scene, task)


try:
    main()
except Exception as e:  # noqa
    traceback.print_exc()
    log("CDR_ERROR " + str(e).replace("\n", " "))
    sys.stdout.flush()
    os._exit(2)

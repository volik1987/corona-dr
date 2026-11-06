"""Сборка кадра на головном компьютере. Запуск:
    blender -b сцена.blend -Y --python post.py -- post.json

1) склеивает тайлы-EXR в один многослойный EXR (все проходы: Combined,
   Denoising Albedo/Normal, light groups) — OpenImageIO, встроенный в Blender;
2) по собранному EXR делает денойз (OIDN с albedo+normal) и VFB в компоновщике —
   один раз на целом кадре, поэтому швов нет;
3) сохраняет итог в формате сцены (PNG/JPEG/EXR...)."""
import json
import os
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import vfb  # noqa: E402


def log(msg):
    print(msg, flush=True)


def assemble(job):
    import OpenImageIO as oiio
    W, H = job["W"], job["H"]
    tiles = job["tiles"]
    first = tiles[0]["path"]
    n_sub = oiio.ImageInput.open(first)
    names = []
    i = 0
    while n_sub.seek_subimage(i, 0):
        spec = n_sub.spec()
        names.append((spec.get_string_attribute("name"), list(spec.channelnames)))
        i += 1
    n_sub.close()
    parts = []
    for si, (part_name, chans) in enumerate(names):
        full = oiio.ImageBuf(oiio.ImageSpec(W, H, len(chans), oiio.FLOAT))
        for t in tiles:
            buf = oiio.ImageBuf(t["path"], si, 0)
            sp = buf.spec()
            if list(sp.channelnames) != chans:  # порядок частей может отличаться — ищем по имени
                buf = None
                k = 0
                inp = oiio.ImageInput.open(t["path"])
                while inp.seek_subimage(k, 0):
                    if list(inp.spec().channelnames) == chans:
                        buf = oiio.ImageBuf(t["path"], k, 0)
                        break
                    k += 1
                inp.close()
                if buf is None:
                    raise RuntimeError("в тайле %s нет прохода %s" % (t["path"], part_name))
                sp = buf.spec()
            tw, th = t["x1"] - t["x0"], t["y1"] - t["y0"]
            if (sp.width, sp.height) != (tw, th):
                raise RuntimeError("размер тайла %dx%d, ожидался %dx%d" % (sp.width, sp.height, tw, th))
            # у Blender y снизу вверх, в EXR — сверху вниз
            if not oiio.ImageBufAlgo.paste(full, t["x0"], H - t["y1"], 0, 0, buf):
                raise RuntimeError("paste: " + oiio.geterror())
        parts.append((chans, full))
    merged = parts[0][1]
    for chans, buf in parts[1:]:
        merged = oiio.ImageBufAlgo.channel_append(merged, buf)
    all_names = [c for chans, _ in parts for c in chans]
    merged.specmod().channelnames = all_names
    merged.specmod().attribute("compression", "zip")
    out = job["out_exr"]
    if not merged.write(out, oiio.FLOAT):
        raise RuntimeError("не записать EXR: " + oiio.geterror())
    log("CDR_ASSEMBLED %s %dx%d каналов %d" % (out, W, H, len(all_names)))
    return out


def finish(job, exr):
    scene = bpy.context.scene
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = job["W"], job["H"], 100
    r.use_border = False
    r.use_crop_to_border = False
    r.pixel_aspect_x = r.pixel_aspect_y = 1.0
    if hasattr(r, "use_sequencer"):
        r.use_sequencer = False
    img = bpy.data.images.load(exr)
    if job.get("post_mode", "corona") == "corona":
        tree = vfb.scene_tree(scene)
        tree.nodes.clear()
        src = vfb.source_image(tree, img)
        vfb.build(tree, src, job.get("vfb", {}), cycles_denoised=not job.get("denoise", True))
    else:
        scene_compositor(scene, img, job.get("denoise", False))
    r.use_compositing = True
    imf = r.image_settings
    fmt = job.get("format")
    if fmt:
        if hasattr(imf, "media_type"):
            imf.media_type = "IMAGE"
        imf.file_format = fmt
    elif getattr(imf, "media_type", "") in ("VIDEO", "MULTI_LAYER_IMAGE") or imf.file_format in ("FFMPEG", "OPEN_EXR_MULTILAYER"):
        if hasattr(imf, "media_type"):
            imf.media_type = "IMAGE"
        imf.file_format = "PNG"
    r.use_file_extension = False
    r.filepath = job["out_final"]
    bpy.ops.render.render(write_still=True)
    if not os.path.exists(job["out_final"]):
        raise RuntimeError("итоговое изображение не записано")
    log("CDR_FILE " + job["out_final"])


def _comp_tree(scene):
    if hasattr(scene, "compositing_node_group"):
        return scene.compositing_node_group
    return scene.node_tree if scene.use_nodes else None


def scene_compositor(scene, img, denoise):
    """Обычный рендер: применяем собственный компоновщик сцены пользователя,
    подставив вместо Render Layers собранный EXR (сокеты сопоставляются по имени).
    Если компоновщика нет — только денойз (если он был включён в Cycles)."""
    tree = _comp_tree(scene) if scene.render.use_compositing else None
    rls = [n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"] if tree else []
    if not rls:
        tree = vfb.scene_tree(scene)
        tree.nodes.clear()
        src = vfb.source_image(tree, img)
        vfb.build(tree, src, {"denoise": bool(denoise)})
        return
    for rl in rls:
        im = tree.nodes.new("CompositorNodeImage")
        im.image = img
        im.location = rl.location
        if rl.layer and hasattr(im, "layer"):
            try:
                im.layer = rl.layer
            except TypeError:
                pass
        src = vfb.Source(im, {"image": "Combined" if im.outputs.get("Combined") else "Image"})
        main = src.image
        if denoise and src.albedo is not None and src.normal is not None:
            d = tree.nodes.new("CompositorNodeDenoise")
            d.location = (rl.location.x + 200, rl.location.y + 250)
            tree.links.new(src.image, d.inputs["Image"])
            tree.links.new(src.albedo, d.inputs["Albedo"])
            tree.links.new(src.normal, d.inputs["Normal"])
            vfb.set_menu(d, "Prefilter", "Accurate", "prefilter", "ACCURATE")
            main = d.outputs[0]
        for link in list(tree.links):
            if link.from_node != rl:
                continue
            name = link.from_socket.name
            if name in ("Image", "Combined"):
                out = main
            else:
                out = im.outputs.get(name)
            to = link.to_socket
            tree.links.remove(link)
            if out is not None:
                tree.links.new(out, to)
            else:
                log("внимание: прохода «%s» нет в собранном EXR" % name)
        tree.nodes.remove(rl)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], encoding="utf-8") as fh:
        job = json.load(fh)
    exr = assemble(job)
    if job.get("out_final"):
        finish(job, exr)


try:
    main()
except Exception as e:  # noqa
    traceback.print_exc()
    log("CDR_ERROR " + str(e).replace("\n", " "))
    sys.stdout.flush()
    os._exit(2)

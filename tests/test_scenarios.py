"""Сценарии на краях: то, чего нет в test_addon / test_dr.

Запуск — как test_addon (аддон должен быть установлен в профиль BLENDER_USER_RESOURCES):
    blender -b --python tests/test_scenarios.py

Проверяется:
  1. файл с кириллицей и пробелами в пути + внешняя (неупакованная) текстура — на узле она есть;
  2. нечётный размер, resolution_percentage 50, частей больше, чем строк, полосы;
  3. прозрачная плёнка — альфа сохраняется при склейке;
  4. узел с другим ключом сети — задание падает с понятной ошибкой, а не висит;
  5. отмена задания — головной освобождается, узел возвращается в «ожидание»;
  6. анимация при видео-формате в Output — кадры пишутся PNG;
  7. несохранённая сцена — сетевой рендер работает;
  8. узел без интерфейса (node_cli.py) принимает задание.
Итог: TEST_OK / TEST_FAIL."""
import importlib
import os
import shutil
import subprocess
import sys
import time
import traceback

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "out_scen")
fails = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def mod():
    for name in ("bl_ext.user_default.corona_dr",):
        try:
            return importlib.import_module(name)
        except ImportError:
            pass
    return None


def wait(dr_ops, timeout=240):
    r = dr_ops.RUNNER
    t0 = time.time()
    while r and not r.done and time.time() - t0 < timeout:
        time.sleep(0.3)
    dr_ops._update_ui()
    return r


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 8
    sc.cycles.use_adaptive_sampling = False
    sc.cycles.use_denoising = False
    sc.corona.enabled = False
    sc.render.film_transparent = False
    sc.render.resolution_percentage = 100
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    if hasattr(sc, "compositing_node_group"):
        sc.compositing_node_group = None
    return sc


def basic_objects(sc, texture=None):
    bpy.ops.mesh.primitive_plane_add(size=6)
    plane = bpy.context.object
    if texture:
        m = bpy.data.materials.new("tex")
        m.use_nodes = True
        nt = m.node_tree
        bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(texture)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.0
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
        plane.data.materials.append(m)
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1))
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 5))
    bpy.context.object.data.energy = 3
    bpy.ops.object.camera_add(location=(0, 0, 9), rotation=(0, 0, 0))
    sc.camera = bpy.context.object
    w = bpy.context.scene.world or bpy.data.worlds.new("w")
    sc.world = w


def make_texture(path):
    img = bpy.data.images.new("tex_src", 16, 16)
    px = []
    for y in range(16):
        for x in range(16):
            px += [0.0, 1.0, 0.0, 1.0]  # чисто зелёная
    img.pixels = px
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


def pixels(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    px = list(img.pixels)
    bpy.data.images.remove(img)
    return w, h, px


def main():
    m = mod()
    check(m is not None, "аддон установлен")
    if not m:
        return
    dr_ops = m.dr_ops
    node_mod = importlib.import_module(m.__name__ + ".dr.node")
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    p = dr_ops.prefs()
    p.node_port = 19440
    p.cache_dir = os.path.join(OUT, "кэш узла")
    p.key = ""

    # ---------- 7. несохранённая сцена (до первого сохранения файла)
    sc = clear_scene()
    basic_objects(sc)
    sc.render.resolution_x, sc.render.resolution_y = 64, 48
    dr = sc.corona_dr
    dr.head_renders = True
    dr.mode = "FRAME"
    dr.tiles = 2
    dr.post_mode = "NONE"
    dr.output_dir = os.path.join(OUT, "несохранённая")
    res = bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    check(res == {"FINISHED"} and r and r.ok and os.path.exists(r.final_path),
          f"несохранённая сцена: {dr.job_status}")
    # анимация несохранённой сцены с относительным Output → ~/corona_dr_render/<сцена>,
    # плюс ручные диапазоны с кадром вне сцены → предупреждение в журнале
    sc.render.filepath = "//rel/кадр_"
    sc.frame_start, sc.frame_end = 1, 2
    dr.mode = "ANIM"
    dr.distribution = "MANUAL"
    for n in dr.nodes:
        n.frames = "1–2, 50"
    fb = os.path.join(os.path.expanduser("~"), "corona_dr_render", bpy.path.clean_name(sc.name))
    bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    got = sorted(os.listdir(fb)) if os.path.isdir(fb) else []
    check(r and r.ok and len([g for g in got if g.startswith("кадр_")]) == 2,
          f"анимация несохранённой сцены → {fb}: {got} ({dr.job_status})")
    logs = "\n".join(r.job.log_lines) if r and r.job else ""
    check("вне диапазона" in logs and "50" in logs, "предупреждение о кадре 50 вне сцены")
    check("относительный" in logs, "предупреждение об относительном пути Output")
    shutil.rmtree(fb, ignore_errors=True)
    for n in dr.nodes:
        n.frames = ""
    dr.distribution = "AUTO"
    dr.mode = "FRAME"

    # ---------- 1. кириллица, пробелы, внешняя текстура
    sdir = os.path.join(OUT, "Моя папка", "сцена с пробелами")
    os.makedirs(os.path.join(sdir, "текстуры"))
    tex = os.path.join(sdir, "текстуры", "зелёная текстура.png")
    make_texture(tex)
    sc = clear_scene()
    basic_objects(sc, texture=tex)
    sc.render.resolution_x, sc.render.resolution_y = 80, 60
    blend = os.path.join(sdir, "Сцена №1.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend, relative_remap=True)
    img = next(i for i in bpy.data.images if i.filepath and "зелёная" in i.filepath)
    check(img.filepath.startswith("//"), f"текстура по относительному пути: {img.filepath}")
    dr = sc.corona_dr
    dr.output_dir = "//итог/"
    dr.tiles = 3
    dr.post_mode = "NONE"
    bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    ok = r and r.ok and os.path.exists(r.final_path)
    check(ok, f"кириллица и пробелы в путях: {dr.job_status}")
    if ok:
        check(os.path.dirname(r.final_path) == os.path.join(sdir, "итог"),
              f"итог в папке «//итог/» рядом со сценой: {r.final_path}")
        # эталон — локальный рендер той же сцены (без текстуры была бы розовая плоскость)
        sc.render.filepath = os.path.join(OUT, "local_tex.png")
        bpy.ops.render.render(write_still=True)
        w, h, px = pixels(r.final_path)
        _, _, lp = pixels(sc.render.filepath)
        i = (2 * w + 2) * 4
        diff = max(abs(px[i + k] - lp[i + k]) for k in range(3))
        check(diff < 0.05 and px[i + 1] > px[i] and px[i + 1] > px[i + 2],
              f"внешняя текстура доехала до узла (угол {[round(v, 2) for v in px[i:i + 3]]}, "
              f"локально {[round(v, 2) for v in lp[i:i + 3]]})")

    # ---------- 2. нечётный размер, 50 %, частей больше строк
    sc.render.resolution_x, sc.render.resolution_y = 97, 23
    sc.render.resolution_percentage = 50   # → 48 x 11
    dr.tiles = 16
    dr.tile_kind = "STRIPS"
    bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    ok = r and r.ok and os.path.exists(r.final_path)
    check(ok, f"48x11, 16 полос: {dr.job_status}")
    if ok:
        w, h, _ = pixels(r.final_path)
        check((w, h) == (48, 11), f"размер итога {w}x{h} = 48x11")
    dr.tile_kind = "GRID"
    sc.render.resolution_x, sc.render.resolution_y = 81, 61
    sc.render.resolution_percentage = 100
    dr.tiles = 7
    bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    ok = r and r.ok and os.path.exists(r.final_path)
    check(ok, f"81x61, 7 частей сеткой: {dr.job_status}")
    if ok:
        w, h, _ = pixels(r.final_path)
        check((w, h) == (81, 61), f"размер итога {w}x{h} = 81x61")

    # ---------- 3. прозрачная плёнка
    sc.render.resolution_x, sc.render.resolution_y = 64, 48
    sc.render.film_transparent = True
    for o in list(bpy.data.objects):
        if o.type == "MESH" and o.name.startswith("Plane"):
            bpy.data.objects.remove(o)
    dr.tiles = 4
    for pm in ("NONE", "SCENE"):
        dr.post_mode = pm
        bpy.ops.corona_dr.start()
        r = wait(dr_ops)
        ok = r and r.ok and os.path.exists(r.final_path)
        check(ok, f"прозрачная плёнка ({pm}): {dr.job_status}")
        if ok:
            w, h, px = pixels(r.final_path)
            corner_a = px[3]
            ci = ((h // 2) * w + w // 2) * 4
            check(corner_a < 0.05 and px[ci + 3] > 0.95,
                  f"альфа сохранена ({pm}): угол {corner_a:.2f}, центр {px[ci + 3]:.2f}")
    sc.render.film_transparent = False

    # ---------- 6. анимация при видео-формате
    sc.render.image_settings.media_type = "VIDEO"
    sc.render.image_settings.file_format = "FFMPEG"
    sc.render.filepath = "//видео/кадр_"
    sc.frame_start, sc.frame_end = 1, 3
    dr.mode = "ANIM"
    dr.distribution = "AUTO"
    bpy.ops.corona_dr.start()
    r = wait(dr_ops)
    vdir = os.path.join(sdir, "видео")
    files = sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []
    check(r and r.ok and len(files) == 3 and all(f.endswith(".png") for f in files),
          f"анимация с видео-форматом → PNG-кадры: {files} ({dr.job_status})")
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    dr.mode = "FRAME"
    dr.post_mode = "NONE"

    # ---------- 4. узел с другим ключом сети
    dr.head_renders = False
    keyed = node_mod.NodeServer(bpy.app.binary_path, port=19441, name="СКлючом",
                                cache_dir=os.path.join(OUT, "кэш2"), key="секрет", log=lambda *a: None).start()
    dr.nodes.clear()
    it = dr.nodes.add()
    it.host, it.port, it.name = "127.0.0.1", 19441, "СКлючом"
    t0 = time.time()
    bpy.ops.corona_dr.start()
    r = wait(dr_ops, timeout=60)
    took = time.time() - t0
    check(r and r.done and not r.ok and took < 50,
          f"чужой ключ: задание завершилось ошибкой за {took:.0f} с: {dr.job_status}")
    check(r is not None and ("ключ" in (r.error or "") or "ключ" in dr.job_status),
          f"в ошибке сказано про ключ: «{r and r.error}»")
    dr.manual_host, dr.manual_port = "127.0.0.1", 19441
    bpy.ops.corona_dr.node_add()
    check("ключ" in dr.nodes[-1].status, f"«добавить по IP» показывает проблему с ключом: «{dr.nodes[-1].status}»")
    keyed.stop()

    # ---------- 5. отмена
    dr.nodes.clear()
    dr.head_renders = True
    sc.render.resolution_x, sc.render.resolution_y = 400, 300
    sc.cycles.samples = 4000
    dr.tiles = 2
    bpy.ops.corona_dr.start()
    t0 = time.time()
    while dr_ops.RUNNER and (dr_ops.RUNNER.job is None or not dr_ops.NODE.busy) and time.time() - t0 < 60:
        time.sleep(0.2)
    time.sleep(1.0)
    busy_before = dr_ops.NODE.busy
    bpy.ops.corona_dr.cancel()
    r = wait(dr_ops, timeout=40)
    t1 = time.time()
    while dr_ops.NODE.busy and time.time() - t1 < 20:
        time.sleep(0.2)
    check(busy_before, "узел рендерил перед отменой")
    check(r and r.done and not r.ok, f"отмена: задание остановлено ({dr.job_status})")
    check(dr_ops.RUNNER is None, "после отмены можно запускать новое задание")
    check(not dr_ops.NODE.busy and dr_ops.NODE._proc is None,
          f"узел вернулся в «{dr_ops.NODE.status}», процесс рендера убит")
    sc.cycles.samples = 8

    # ---------- 8. узел без интерфейса
    cli = os.path.join(os.path.dirname(m.__file__), "node_cli.py")
    proc = subprocess.Popen([bpy.app.binary_path, "-b", "--factory-startup", "--python", cli, "--",
                             "--port", "19442", "--name", "Безголовый", "--cache", os.path.join(OUT, "кэш3")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    try:
        time.sleep(6)
        dr_ops.stop_node()
        dr.head_renders = False
        dr.nodes.clear()
        dr.manual_host, dr.manual_port = "127.0.0.1", 19442
        bpy.ops.corona_dr.node_add()
        check(dr.nodes[-1].name == "Безголовый", f"node_cli отвечает: {dr.nodes[-1].name} / {dr.nodes[-1].status}")
        sc.render.resolution_x, sc.render.resolution_y = 64, 48
        bpy.ops.corona_dr.start()
        r = wait(dr_ops)
        check(r and r.ok, f"node_cli отрендерил кадр: {dr.job_status}")
    finally:
        proc.kill()

    robustness(m, dr_ops, node_mod, sc, blend)


def robustness(m, dr_ops, node_mod, sc, blend):
    """Правки после обзора кода: ключ до приёма данных, sha, «занят», не-узел, отмена, пути."""
    import json
    import socket
    import threading
    dr_pkg = m.__name__ + ".dr"
    P = importlib.import_module(dr_pkg + ".protocol")
    Mst = importlib.import_module(dr_pkg + ".master")
    cache = os.path.join(OUT, "кэш4")

    # --- данные без ключа не пишутся на диск
    keyed = node_mod.NodeServer(bpy.app.binary_path, port=19443, name="К", cache_dir=cache,
                                key="секрет", log=lambda *a: None).start()
    s = P.connect("127.0.0.1", 19443)
    fake = b"x" * 1000
    try:
        P.send_msg(s, {"cmd": "blend", "sha": "a" * 40}, payload=fake)
        s.settimeout(3)
        P.recv_msg(s)
    except Exception:
        pass
    s.close()
    time.sleep(0.3)
    check(not any(f.endswith(".blend") for f in os.listdir(cache)),
          "без ключа узел не принимает сцену на диск")
    # --- с ключом, но sha не совпадает
    s = P.connect("127.0.0.1", 19443)
    P.send_msg(s, {"cmd": "hello", "key": "секрет"})
    P.recv_msg(s)
    try:
        P.send_msg(s, {"cmd": "blend", "sha": "b" * 40}, payload=fake)
        s.settimeout(3)
        P.recv_msg(s)
    except Exception:
        pass
    s.close()
    time.sleep(0.3)
    check(not os.path.exists(os.path.join(cache, "job_" + "b" * 40 + ".blend")),
          "сцена с неверным sha отклонена")
    keyed.stop()

    # --- два задания одновременно на один узел: «занят» не тратит попытки
    shared = node_mod.NodeServer(bpy.app.binary_path, port=19444, name="Общий",
                                 cache_dir=os.path.join(OUT, "кэш5"), log=lambda *a: None).start()
    wd = os.path.join(OUT, "два_задания")
    os.makedirs(wd, exist_ok=True)
    jobs = []
    for k in range(2):
        dst = os.path.join(wd, f"j{k}")
        jobs.append(Mst.build_anim_job(blend, [Mst.NodeState("127.0.0.1", 19444, "Общий")], [1, 2],
                                       lambda f, ext, d=dst: os.path.join(d, f"f_{f:04d}") + ext,
                                       os.path.join(wd, f"w{k}"), log=lambda *a: None))
    for j in jobs:
        j.start()
    for j in jobs:
        j.wait(240)
    check(all(j.ok for j in jobs), f"два головных на одном узле: оба готовы ({[j.error for j in jobs]})")
    shared.stop()

    # --- «добавить по IP» на адрес, где не узел (молчащий сервер) — не виснет
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    held = []
    threading.Thread(target=lambda: [held.append(srv.accept()) for _ in range(6)], daemon=True).start()
    dr = sc.corona_dr
    dr.manual_host, dr.manual_port = "127.0.0.1", port
    t0 = time.time()
    bpy.ops.corona_dr.node_add()
    took = time.time() - t0
    check(took < 8, f"«добавить по IP» на молчащий сервер: {took:.1f} с, «{dr.nodes[-1].status}»")
    # --- сервер, который отвечает мусором
    junk = socket.socket()
    junk.bind(("127.0.0.1", 0))
    junk.listen(4)
    jport = junk.getsockname()[1]

    def junk_loop():
        for _ in range(6):
            c, _a = junk.accept()
            c.sendall(b"SSH-2.0-OpenSSH_9.0\r\n" + b"\0" * 64)
    threading.Thread(target=junk_loop, daemon=True).start()
    dr.manual_port = jport
    bpy.ops.corona_dr.node_add()
    check("не узел" in dr.nodes[-1].status, f"мусорный ответ: «{dr.nodes[-1].status}»")

    # --- отмена задания, когда узел молчит (зависшее соединение)
    dr.nodes.clear()
    dr.head_renders = False
    it = dr.nodes.add()
    it.host, it.port, it.name = "127.0.0.1", port, "Молчун"
    dr.mode = "FRAME"
    bpy.ops.corona_dr.start()
    t0 = time.time()
    while dr_ops.RUNNER and dr_ops.RUNNER.job is None and time.time() - t0 < 60:
        time.sleep(0.2)
    time.sleep(1.5)
    bpy.ops.corona_dr.cancel()
    r = wait(dr_ops, timeout=20)
    check(r and r.done and dr_ops.RUNNER is None, f"отмена при молчащем узле: {time.time() - t0:.0f} с ({dr.job_status})")
    srv.close()
    junk.close()

    # --- длинное тире и кадры вне сцены
    check(Mst.parse_frames("1–3, 5—6") == [1, 2, 3, 5, 6], "диапазоны с длинным тире")



try:
    main()
except Exception:
    traceback.print_exc()
    fails.append("исключение")
print("TEST_OK" if not fails else "TEST_FAIL: " + "; ".join(fails), flush=True)
sys.stdout.flush()
os._exit(0)

"""Операторы сетевого рендера в интерфейсе Blender: узел, поиск, запуск задания, прогресс."""
import os
import subprocess
import tempfile
import threading
import time

import bpy

from . import corona
from .dr import master as M
from .dr import node as N
from .dr import protocol as P

NODE = None          # NodeServer, запущенный в этом Blender
RUNNER = None        # текущее задание головного
ADDON_ID = __package__


def prefs():
    try:
        return bpy.context.preferences.addons[ADDON_ID].preferences
    except (KeyError, AttributeError):
        return None


def blender_bin():
    p = prefs()
    if p and p.blender_path:
        return bpy.path.abspath(p.blender_path)
    return bpy.app.binary_path


def node_running():
    return NODE is not None and NODE.running


def start_node():
    global NODE
    if node_running():
        return NODE
    p = prefs()
    cache = bpy.path.abspath(p.cache_dir) if p and p.cache_dir else None
    NODE = N.NodeServer(blender_bin(), port=p.node_port if p else P.DEFAULT_PORT,
                        name=(p.node_name if p and p.node_name else None), cache_dir=cache,
                        threads=p.node_threads if p else 0, key=p.key if p else "",
                        log=print).start()
    NODE.preview_enabled = bool(p.node_preview) if p else True
    _ensure_timer()
    return NODE


def stop_node():
    global NODE
    if NODE:
        NODE.stop()
    NODE = None


# ---------------------------------------------------------------- задание головного
class Runner:
    """Подготовка сцены + задание. Всё тяжёлое — в потоке, UI читает поля."""

    def __init__(self):
        self.status = "подготовка сцены"
        self.job = None
        self.error = ""
        self.done = False
        self.ok = False
        self.result_image = None
        self.node_map = []    # индекс в job.nodes → (host, port) элемента таблицы
        self.thread = None
        self.cancelled = False

    @property
    def progress(self):
        return self.job.progress if self.job else 0.0

    def cancel(self):
        self.cancelled = True
        if self.job:
            self.job.cancel()


def _cleanup_old_workdirs(max_age_h=24):
    """Рабочие папки прошлых заданий (копии сцены, тайлы) старше суток удаляем."""
    import shutil
    root = tempfile.gettempdir()
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.startswith("corona_dr_") and os.path.isdir(path):
            try:
                if now - os.path.getmtime(path) > max_age_h * 3600:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


def _pack_blend(src, dst):
    """Упаковать ресурсы в копию сцены отдельным процессом Blender (файл пользователя не меняется)."""
    expr = ("import bpy\n"
            "try:\n bpy.ops.file.pack_all()\nexcept Exception as e:\n print('pack_all:', e)\n"
            "try:\n bpy.ops.file.pack_libraries()\nexcept Exception as e:\n pass\n"
            f"bpy.ops.wm.save_as_mainfile(filepath={dst!r}, compress=True)\n")
    res = subprocess.run([blender_bin(), "-b", src, "-Y", "--python-expr", expr],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if not os.path.exists(dst):
        raise RuntimeError("не удалось упаковать сцену: " + " | ".join(res.stdout.splitlines()[-4:]))


def _post_mode(scene):
    mode = scene.corona_dr.post_mode
    if mode == "AUTO":
        return "corona" if (scene.corona.enabled and scene.corona.vfb_enabled) else "scene"
    return {"SCENE": "scene", "NONE": "none"}[mode]


def start_job(context):
    global RUNNER
    scene = context.scene
    dr = scene.corona_dr
    p = prefs()
    if dr.head_renders:
        start_node()
        port = NODE.port
        if not any(n.host in ("127.0.0.1", "localhost") and n.port == port for n in dr.nodes) and \
                not any(n.info == NODE.uid for n in dr.nodes):
            it = dr.nodes.add()
            it.name, it.host, it.port, it.info = (NODE.name + " (этот ПК)"), "127.0.0.1", port, NODE.uid
    items = [n for n in dr.nodes if n.enabled]
    if not items:
        raise RuntimeError("нет включённых узлов — найдите их или добавьте вручную")
    if scene.corona.enabled:
        corona.apply_all(scene)
    if scene.camera is None:
        raise RuntimeError("в сцене нет камеры")

    _cleanup_old_workdirs()
    workdir = tempfile.mkdtemp(prefix="corona_dr_")
    raw = os.path.join(workdir, "scene_raw.blend")
    bpy.ops.wm.save_as_mainfile(filepath=raw, copy=True, relative_remap=True)
    blend = os.path.join(workdir, "scene.blend")

    r = scene.render
    nodes = [M.NodeState(n.host, n.port, n.name, n.weight) for n in items]
    runner = Runner()
    runner.node_map = [(n.host, n.port) for n in items]
    key = p.key if p else ""
    device = "CPU" if dr.force_cpu else None
    log = print

    if dr.mode == "FRAME":
        W = int(r.resolution_x * r.resolution_percentage / 100)
        H = int(r.resolution_y * r.resolution_percentage / 100)
        out_dir = bpy.path.abspath(dr.output_dir or "//dr_render/")
        if out_dir.startswith("//") or not os.path.isabs(out_dir):
            out_dir = os.path.join(os.path.expanduser("~"), "corona_dr_render")
        os.makedirs(out_dir, exist_ok=True)
        imf = r.image_settings
        fmt = None
        if getattr(imf, "media_type", "") in ("VIDEO", "MULTI_LAYER_IMAGE") or imf.file_format in ("FFMPEG", "OPEN_EXR_MULTILAYER"):
            fmt = "PNG"
        ext = {"PNG": ".png", "JPEG": ".jpg", "OPEN_EXR": ".exr", "TIFF": ".tif", "TARGA": ".tga",
               "TARGA_RAW": ".tga", "BMP": ".bmp", "HDR": ".hdr", "WEBP": ".webp", "AVIF": ".avif",
               "JPEG2000": ".jp2"}.get(fmt or imf.file_format, ".png")
        out_final = os.path.join(out_dir, f"{bpy.path.clean_name(scene.name)}_{scene.frame_current:04d}{ext}")
        post_mode = _post_mode(scene)
        if scene.render.engine == "CYCLES":
            if post_mode == "corona":
                denoise = scene.corona.denoise != "OFF"
            else:
                denoise = bool(scene.cycles.use_denoising)
        else:
            denoise = False
        vl = context.view_layer.name
        vfb_s = corona.vfb_settings(scene) if post_mode == "corona" else {}
        if post_mode == "none":
            post_mode, vfb_s = "corona", {"denoise": denoise}  # «пустой» VFB = только денойз

        def make():
            return M.build_tile_job(blend, nodes, W, H, dr.tiles, dr.tile_kind, scene.frame_current, vl,
                                    workdir, blender_bin(), vfb_s, denoise, out_final, fmt, key=key,
                                    device=device, log=log, post_mode=post_mode)
        runner.final_path = out_final
    else:
        frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
        imf = r.image_settings
        is_video = getattr(imf, "media_type", "") == "VIDEO" or imf.file_format == "FFMPEG"
        base = {}
        for f in frames:
            if is_video:
                base[f] = bpy.path.abspath(r.filepath) + f"{f:04d}"
            else:
                path = r.frame_path(frame=f)
                stem, e = os.path.splitext(path)
                base[f] = stem if r.use_file_extension and e else path
        node_frames = [M.parse_frames(n.frames) for n in items]

        def dest(f, ext):
            b = base.get(f) or os.path.join(os.path.dirname(base[frames[0]]), f"{f:04d}")
            return b + ext

        def make():
            return M.build_anim_job(blend, nodes, frames, dest, workdir, distribution=dr.distribution,
                                    node_frames=node_frames, chunk=dr.chunk, steal=dr.steal,
                                    key=key, device=device, log=log)
        runner.final_path = os.path.dirname(base[frames[0]])

    def work():
        try:
            runner.status = "упаковка сцены"
            _pack_blend(raw, blend)
            if runner.cancelled:
                raise RuntimeError("отменено")
            runner.status = "рендер"
            runner.job = make()
            runner.job.start()
            runner.job.wait()
            runner.ok = runner.job.ok
            runner.error = runner.job.error
            runner.status = runner.job.status
        except Exception as e:
            runner.error = str(e)
            runner.status = "ошибка"
        runner.done = True

    RUNNER = runner
    runner.thread = threading.Thread(target=work, daemon=True)
    runner.thread.start()
    dr.job_status = "подготовка"
    _ensure_timer()
    return runner


def show_image(path, name="Corona DR", editor=True, new_window=True):
    img = bpy.data.images.get(name)
    if img and bpy.path.abspath(img.filepath) == path:
        img.reload()
    else:
        if img:
            bpy.data.images.remove(img)
        img = bpy.data.images.load(path)
        img.name = name
    if not editor:
        return img
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = img
                area.tag_redraw()
                return img
    if not new_window:
        return img
    # нет редактора изображений — открываем новое окно
    try:
        win = wm.windows[0]
        with bpy.context.temp_override(window=win, area=win.screen.areas[0]):
            bpy.ops.wm.window_new()
        nw = wm.windows[-1]
        area = nw.screen.areas[0]
        area.type = "IMAGE_EDITOR"
        area.spaces.active.image = img
    except Exception as e:
        print("Corona DR: не открыть окно изображения:", e)
    return img


# ---------------------------------------------------------------- таймер интерфейса
_timer_on = False


def _ensure_timer():
    global _timer_on
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_tick, first_interval=0.3, persistent=True)


def _tick():
    global _timer_on
    try:
        busy = _update_ui()
    except Exception as e:
        print("Corona DR timer:", e)
        busy = True
    if not busy and not node_running():
        _timer_on = False
        return None
    return 0.5


def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type in ("PROPERTIES", "IMAGE_EDITOR"):
                area.tag_redraw()


PREVIEW_NAME = "Corona DR узел"
_preview_seen = None   # (id узла, номер предпросмотра)
_preview_window_opened = False


def _update_node_preview():
    """Показ последней готовой части/кадра, которые отрендерил узел этого компьютера."""
    global _preview_seen, _preview_window_opened
    n = NODE
    if n is None:
        return
    p = prefs()
    n.preview_enabled = bool(p.node_preview) if p else True
    key = (id(n), n.preview_seq)
    if not n.preview_enabled or key == _preview_seen or not n.preview_path:
        return
    _preview_seen = key
    # этот же компьютер — головной: не перебиваем его Image Editor, только обновляем картинку
    head_busy = RUNNER is not None
    try:
        img = show_image(n.preview_path, PREVIEW_NAME, editor=not head_busy,
                         new_window=not _preview_window_opened)
        _preview_window_opened = True
        print(f"Corona DR: узел показал «{n.preview_label}» → {img.name}")
    except Exception as e:
        print("Corona DR: предпросмотр узла:", e)


def _update_ui():
    global RUNNER
    try:
        _update_node_preview()
    except Exception as e:
        print("Corona DR: предпросмотр узла:", e)
    scene = bpy.context.scene
    if scene is None:
        return RUNNER is not None
    dr = scene.corona_dr
    r = RUNNER
    if r is None:
        _redraw()
        return False
    job = r.job
    if job:
        for i, st in enumerate(job.nodes):
            host, port = r.node_map[i]
            it = next((n for n in dr.nodes if n.host == host and n.port == port), None)
            if it is None:
                continue
            txt = st.status
            if st.current:
                txt += f": {st.current}"
            if st.error and not st.alive:
                txt += f" ({st.error})"
            it.status = txt[:120]
            it.progress = st.progress
            it.done = st.done
        dr.job_progress = job.progress
        dr.job_status = f"{job.status}: готово {job.done_count}/{len(job.tasks)}"
    else:
        dr.job_status = r.status
    if r.done:
        if r.ok:
            dr.job_status = f"готово за {job.elapsed:.0f} с → {r.final_path}"
            dr.job_progress = 1.0
            if job.mode == "tile" and job.result:
                try:
                    show_image(job.result["final"])
                except Exception as e:
                    print("Corona DR: показ результата:", e)
        else:
            dr.job_status = "ошибка: " + (r.error or r.status)
        RUNNER = None
        _redraw()
        return False
    _redraw()
    return True


def job_log_lines(n=12):
    r = RUNNER or _last_runner()
    if r and r.job:
        return list(r.job.log_lines)[-n:]
    return []


_LAST = []


def _last_runner():
    return _LAST[-1] if _LAST else None


# ---------------------------------------------------------------- операторы
class CORONA_OT_node_toggle(bpy.types.Operator):
    bl_idname = "corona_dr.node_toggle"
    bl_label = "Узел рендера"
    bl_description = "Запустить/остановить фоновый сервер рендера на этом компьютере"

    def execute(self, context):
        if node_running():
            stop_node()
            self.report({"INFO"}, "Узел рендера остановлен")
        else:
            try:
                n = start_node()
            except OSError as e:
                self.report({"ERROR"}, f"Не запустить узел: {e}")
                return {"CANCELLED"}
            self.report({"INFO"}, f"Узел «{n.name}» слушает порт {n.port}")
        return {"FINISHED"}


class CORONA_OT_discover(bpy.types.Operator):
    bl_idname = "corona_dr.discover"
    bl_label = "Найти узлы"
    bl_description = "Найти узлы рендера в локальной сети (UDP-широковещание)"

    def execute(self, context):
        dr = context.scene.corona_dr
        found = P.discover(timeout=1.2)
        added = 0
        own = NODE.uid if node_running() else None
        for f in found:
            host = "127.0.0.1" if f.get("uid") == own else f["host"]
            it = next((n for n in dr.nodes if (n.host == host and n.port == f["port"]) or
                       (n.info and n.info == f.get("uid"))), None)
            if it is None:
                it = dr.nodes.add()
                it.host, it.port = host, f["port"]
                added += 1
            it.info = f.get("uid", "")
            it.name = f.get("name", host) + (" (этот ПК)" if f.get("uid") == own else "")
            it.cores = int(f.get("cores", 0))
            it.status = "занят" if f.get("busy") else "свободен"
        self.report({"INFO"}, f"Найдено узлов: {len(found)}, новых: {added}")
        return {"FINISHED"}


class CORONA_OT_node_add(bpy.types.Operator):
    bl_idname = "corona_dr.node_add"
    bl_label = "Добавить узел по IP"

    def execute(self, context):
        dr = context.scene.corona_dr
        host = dr.manual_host.strip()
        if not host:
            return {"CANCELLED"}
        it = dr.nodes.add()
        it.host, it.port, it.name = host, dr.manual_port, host
        try:
            s = P.connect(host, dr.manual_port, timeout=2.0)
            p = prefs()
            P.send_msg(s, {"cmd": "hello", "key": p.key if p else ""})
            hdr, _ = P.recv_msg(s)
            P.send_msg(s, {"cmd": "bye"})
            s.close()
            if hdr.get("type") == "info":
                it.name, it.cores, it.info, it.status = hdr["name"], hdr.get("cores", 0), hdr.get("uid", ""), "свободен"
            else:
                it.status = hdr.get("error", "?")
        except OSError as e:
            it.status = f"нет связи: {e}"
        dr.index = len(dr.nodes) - 1
        return {"FINISHED"}


class CORONA_OT_node_remove(bpy.types.Operator):
    bl_idname = "corona_dr.node_remove"
    bl_label = "Убрать узел"

    def execute(self, context):
        dr = context.scene.corona_dr
        if 0 <= dr.index < len(dr.nodes):
            dr.nodes.remove(dr.index)
            dr.index = max(0, dr.index - 1)
        return {"FINISHED"}


class CORONA_OT_frames_split(bpy.types.Operator):
    bl_idname = "corona_dr.frames_split"
    bl_label = "Заполнить диапазоны по весам"
    bl_description = "Расписать кадры сцены по включённым узлам пропорционально весам (потом можно поправить вручную)"

    def execute(self, context):
        sc = context.scene
        items = [n for n in sc.corona_dr.nodes if n.enabled]
        frames = list(range(sc.frame_start, sc.frame_end + 1, max(1, sc.frame_step)))
        for it, part in zip(items, M.split_weighted(frames, [n.weight for n in items])):
            it.frames = f"{part[0]}-{part[-1]}" if part else ""
        sc.corona_dr.distribution = "MANUAL"
        return {"FINISHED"}


class CORONA_OT_dr_start(bpy.types.Operator):
    bl_idname = "corona_dr.start"
    bl_label = "Сетевой рендер"
    bl_description = "Отправить сцену на узлы и начать распределённый рендер"

    @classmethod
    def poll(cls, context):
        return RUNNER is None

    def execute(self, context):
        try:
            r = start_job(context)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        _LAST.append(r)
        del _LAST[:-1]
        self.report({"INFO"}, "Сетевой рендер запущен")
        return {"FINISHED"}


class CORONA_OT_dr_cancel(bpy.types.Operator):
    bl_idname = "corona_dr.cancel"
    bl_label = "Остановить"

    @classmethod
    def poll(cls, context):
        return RUNNER is not None

    def execute(self, context):
        RUNNER.cancel()
        return {"FINISHED"}


class CORONA_OT_open_output(bpy.types.Operator):
    bl_idname = "corona_dr.open_output"
    bl_label = "Открыть папку результата"

    def execute(self, context):
        r = _last_runner()
        path = getattr(r, "final_path", "") if r else ""
        if path and os.path.isfile(path):
            path = os.path.dirname(path)
        if path and os.path.isdir(path):
            bpy.ops.wm.path_open(filepath=path)
        return {"FINISHED"}


classes = (CORONA_OT_node_toggle, CORONA_OT_discover, CORONA_OT_node_add, CORONA_OT_node_remove,
           CORONA_OT_frames_split, CORONA_OT_dr_start, CORONA_OT_dr_cancel, CORONA_OT_open_output)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    if RUNNER:
        RUNNER.cancel()
    stop_node()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

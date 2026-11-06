"""Проверка в Blender С ИНТЕРФЕЙСОМ (таймеры, окна, Image Editor, вкл/выкл аддона).

    Запускать с профилем, где стоит аддон:
    BLENDER_USER_RESOURCES=<профиль> blender --python tests/test_gui.py -- снимок.png

Сценарий (всё через bpy.app.timers, как у пользователя):
  1. узел и головной в одном Blender, «Кадр» на 4 части;
  2. таймер UI сам доводит задание до конца (без ручного _update_ui);
  3. итог показан в Image Editor, предпросмотр узла загружен;
  4. «Анимация» — предпросмотр узла открывает окно один раз;
  5. аддон выключается и включается обратно без ошибок, узел остановлен;
  6. снимок окна → путь из аргументов. Итог: GUI_OK / GUI_FAIL."""
import importlib
import os
import sys
import time
import traceback

import addon_utils
import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "out_gui")
SHOT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else os.path.join(OUT, "gui.png")
MOD = "bl_ext.user_default.corona_dr"
fails = []
state = {"step": 0, "t": time.time()}


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def finish():
    print("GUI_OK" if not fails else "GUI_FAIL: " + "; ".join(fails), flush=True)
    try:
        bpy.ops.screen.screenshot(filepath=SHOT)
    except Exception as e:
        print("снимок:", e)
    bpy.ops.wm.quit_blender()


def setup():
    os.makedirs(OUT, exist_ok=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 16
    sc.render.resolution_x, sc.render.resolution_y = 160, 90
    sc.frame_start, sc.frame_end = 1, 2
    sc.render.filepath = os.path.join(OUT, "anim_")
    m = importlib.import_module(MOD)
    p = m.dr_ops.prefs()
    p.node_port = 19445
    p.cache_dir = os.path.join(OUT, "cache")
    dr = sc.corona_dr
    dr.head_renders = True
    dr.mode = "FRAME"
    dr.tiles = 4
    dr.output_dir = OUT
    return m


def step():
    try:
        return _step()
    except Exception:
        traceback.print_exc()
        fails.append("исключение")
        finish()
        return None


def _step():
    m = importlib.import_module(MOD)
    d = m.dr_ops
    sc = bpy.context.scene
    s = state["step"]
    if time.time() - state["t"] > 180:
        check(False, f"шаг {s}: истекло время")
        finish()
        return None
    if s == 0:
        setup()
        with bpy.context.temp_override(window=bpy.context.window_manager.windows[0]):
            res = bpy.ops.corona_dr.start()
        check(res == {"FINISHED"}, "«Кадр» запущен")
        state.update(step=1, t=time.time())
    elif s == 1:
        if d.RUNNER is not None:
            return 0.5
        check("готово" in sc.corona_dr.job_status, f"таймер довёл «Кадр»: {sc.corona_dr.job_status}")
        shown = [a.spaces.active.image.name for w in bpy.context.window_manager.windows
                 for a in w.screen.areas if a.type == "IMAGE_EDITOR" and a.spaces.active.image]
        check("Corona DR" in shown, f"итог в Image Editor: {shown}")
        check(bpy.data.images.get(d.PREVIEW_NAME) is not None, "предпросмотр узла загружен")
        state["windows"] = len(bpy.context.window_manager.windows)
        sc.corona_dr.mode = "ANIM"
        sc.corona_dr.head_renders = False    # узел работает «как чужой»: головного задания нет
        it = sc.corona_dr.nodes[0]
        it.enabled = True
        # второе задание — с другого «головного»: master прямо из модуля, UI-узел показывает части
        res = bpy.ops.corona_dr.start()
        check(res == {"FINISHED"}, "«Анимация» запущена")
        state.update(step=2, t=time.time())
    elif s == 2:
        if d.RUNNER is not None:
            return 0.5
        check("готово" in sc.corona_dr.job_status, f"«Анимация»: {sc.corona_dr.job_status}")
        state.update(step=3, t=time.time())
        return 1.5      # дать таймеру показать последний кадр
    elif s == 3:
        img = bpy.data.images.get(d.PREVIEW_NAME)
        check(img is not None and "last" in img.filepath, f"предпросмотр узла: {img and img.filepath}")
        wins = len(bpy.context.window_manager.windows)
        check(wins <= state["windows"] + 1, f"окон: было {state['windows']}, стало {wins} (не больше одного нового)")
        check(d.node_running(), "узел работает")
        # выключить и включить аддон
        addon_utils.disable(MOD, default_set=True)
        check(not hasattr(bpy.types.Scene, "corona_dr"), "аддон выключен, свойства сняты")
        check(not bpy.app.timers.is_registered(d._tick), "таймер интерфейса снят вместе с аддоном")
        addon_utils.enable(MOD, default_set=True)
        m2 = importlib.import_module(MOD)
        check(hasattr(bpy.types.Scene, "corona_dr") and not m2.dr_ops.node_running(),
              "аддон включён обратно, узел не висит")
        state.update(step=4, t=time.time())
        return 1.0
    else:
        finish()
        return None
    return 0.5


bpy.app.timers.register(step, first_interval=2.0)

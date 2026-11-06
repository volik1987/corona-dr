"""Снимок панелей аддона в интерфейсе: blender --python tests/ui_shot.py -- out.png"""
import os, sys, bpy
OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "/tmp/ui.png"

def step1():
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.corona.enabled = os.environ.get("CDR_CORONA", "1") == "1"
    sc.corona.bloom_on = True
    win = bpy.context.window_manager.windows[0]
    scr = win.screen
    big = max(scr.areas, key=lambda a: a.width * a.height)
    big.type = "PROPERTIES"
    # только для снимка: прячем чужие панели вкладки Render, чтобы наши были видны целиком
    for cls in list(bpy.types.Panel.__subclasses__()):
        if getattr(cls, "bl_context", "") == "render" and not cls.__name__.startswith("CORONA_") \
                and getattr(cls, "bl_space_type", "") == "PROPERTIES":
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
    it = sc.corona_dr.nodes.add(); it.name, it.host, it.status = "ПК-2", "192.168.1.20", "свободен"
    it = sc.corona_dr.nodes.add(); it.name, it.host, it.status, it.progress = "ПК-3", "192.168.1.21", "рендер: часть 3/8", 0.4
    return 1.5

def step1b():
    win = bpy.context.window_manager.windows[0]
    big = max(win.screen.areas, key=lambda a: a.width * a.height)
    big.spaces.active.context = "RENDER"
    for r in big.regions:
        r.tag_redraw()


def step2():
    # раскрыть панель сетевого рендера и пролистать вниз
    win = bpy.context.window_manager.windows[0]
    big = max(win.screen.areas, key=lambda a: a.width * a.height)
    reg = next(r for r in big.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=win, area=big, region=reg):
        for _ in range(int(os.environ.get("CDR_SCROLL", "40"))):
            bpy.ops.view2d.scroll_down(deltay=60)
    return None


def step2b():
    win = bpy.context.window_manager.windows[0]
    big = max(win.screen.areas, key=lambda a: a.width * a.height)
    reg = next(r for r in big.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=win, area=big, region=reg):
        bpy.ops.screen.screenshot_area(filepath=OUT)
    return 0.5

def step3():
    bpy.ops.wm.quit_blender()

bpy.app.timers.register(step1, first_interval=1.0)
bpy.app.timers.register(step1b, first_interval=2.5)
bpy.app.timers.register(step2, first_interval=4.0)
bpy.app.timers.register(step2b, first_interval=5.0)
bpy.app.timers.register(step3, first_interval=6.0)

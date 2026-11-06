"""Corona DR — визуализация «как Corona» поверх Cycles и сетевой рендер по локальной сети."""
import bpy
from bpy.app.handlers import persistent

from . import corona_ops, dr_ops, props, ui
from .dr import protocol


def _preview_changed(self, _context):
    if dr_ops.NODE is not None:
        dr_ops.NODE.preview_enabled = self.node_preview


class CoronaDRPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    node_name: bpy.props.StringProperty(name="Имя узла", default="",
                                        description="Пусто — имя компьютера")
    node_port: bpy.props.IntProperty(name="TCP-порт узла", default=protocol.DEFAULT_PORT, min=1024, max=65535)
    key: bpy.props.StringProperty(name="Общий ключ сети", default="", subtype="PASSWORD",
                                  description="Если задан — узел принимает задания только с тем же ключом")
    node_threads: bpy.props.IntProperty(name="Потоков рендера", default=0, min=0,
                                        description="0 — все ядра")
    cache_dir: bpy.props.StringProperty(name="Кэш узла", subtype="DIR_PATH", default="",
                                        description="Пусто — ~/.corona_dr_cache")
    blender_path: bpy.props.StringProperty(name="Blender для рендера", subtype="FILE_PATH", default="",
                                           description="Пусто — этот же Blender")
    autostart: bpy.props.BoolProperty(name="Запускать узел вместе с Blender", default=False)
    node_preview: bpy.props.BoolProperty(
        name="Показывать готовые части на узле", default=True, update=_preview_changed,
        description="Узел показывает в Image Editor последнюю готовую часть кадра или кадр анимации "
                    "(изображение «Corona DR узел»)")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "node_name")
        col.prop(self, "node_port")
        col.prop(self, "key")
        col.prop(self, "node_threads")
        col.prop(self, "cache_dir")
        col.prop(self, "blender_path")
        col.prop(self, "autostart")
        col.prop(self, "node_preview")
        col.label(text=f"Поиск узлов — UDP-порт {protocol.DISCOVERY_PORT}; откройте TCP {self.node_port} в брандмауэре.",
                  icon="INFO")


@persistent
def _autostart(*_):
    p = dr_ops.prefs()
    if p and p.autostart and not dr_ops.node_running():
        try:
            dr_ops.start_node()
        except OSError as e:
            print("Corona DR: узел не запущен:", e)
    return None


def register():
    bpy.utils.register_class(CoronaDRPreferences)
    props.register()
    corona_ops.register()
    dr_ops.register()
    ui.register()
    bpy.app.timers.register(_autostart, first_interval=1.0)


def unregister():
    ui.unregister()
    dr_ops.unregister()
    corona_ops.unregister()
    props.unregister()
    bpy.utils.unregister_class(CoronaDRPreferences)

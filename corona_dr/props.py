import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)

from . import corona
from .dr import protocol


def _upd_quality(self, context):
    corona.apply_quality(context.scene)
    if self.enabled and self.vfb_enabled:
        corona.apply_vfb(context.scene)


def _upd_vfb(self, context):
    corona.apply_vfb(context.scene)


def _upd_vfb_toggle(self, context):
    sc = context.scene
    if self.enabled and self.vfb_enabled:
        corona.apply_quality(sc)
        corona.apply_vfb(sc)
    else:
        corona.disable(sc)
        corona.apply_quality(sc)


def _upd_enabled(self, context):
    sc = context.scene
    if self.enabled:
        corona.apply_quality(sc)
        corona.apply_vfb(sc)
    else:
        corona.disable(sc)


def _upd_preset(self, context):
    if self.preset in corona.PRESETS:
        p, n, t = corona.PRESETS[self.preset]
        self["pass_limit"], self["noise_limit"], self["time_limit"] = p, n, t
    _upd_quality(self, context)


def _upd_tonemap(self, context):
    if self.enabled:
        corona.apply_tonemap(context.scene)


def _upd_viewport_comp(self, context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                sh = area.spaces.active.shading
                if hasattr(sh, "use_compositor"):
                    sh.use_compositor = "CAMERA" if self.viewport_vfb else "DISABLED"


def _tonemaps(self, context):
    items = []
    try:
        ids = [i.identifier for i in bpy.types.ColorManagedViewSettings.bl_rna.properties["view_transform"].enum_items]
    except Exception:
        ids = []
    if not ids or ids == ["NONE"]:
        ids = ["AgX", "Filmic", "Standard", "Khronos PBR Neutral"]
    names = {"AgX": "AgX (мягкий, как Filmic)", "Filmic": "Filmic", "Standard": "Стандарт (без тонмаппинга)",
             "Khronos PBR Neutral": "PBR Neutral"}
    for i in ("AgX", "Filmic", "Khronos PBR Neutral", "Standard"):
        if i in ids:
            items.append((i, names[i], ""))
    return items


class CoronaLightMixItem(bpy.types.PropertyGroup):
    group: StringProperty(name="Группа")
    enabled: BoolProperty(name="Вкл", default=True, update=_upd_vfb)
    color: FloatVectorProperty(name="Цвет", subtype="COLOR", size=3, default=(1, 1, 1), min=0, update=_upd_vfb)
    intensity: FloatProperty(name="Сила", default=1.0, min=0.0, soft_max=10.0, update=_upd_vfb)


class CoronaSettings(bpy.types.PropertyGroup):
    enabled: BoolProperty(name="Режим Corona", default=False, update=_upd_enabled,
                          description="Настроить Cycles под Corona: прогрессивный рендер с лимитами, денойз, VFB")
    preset: EnumProperty(name="Пресет", items=[
        ("DRAFT", "Черновик", "64 прохода, шум 5%"),
        ("MEDIUM", "Средне", "шум 3%"),
        ("FINAL", "Финал", "шум 2% (как Corona по умолчанию)"),
        ("HIGH", "Высокое", "шум 1%"),
        ("CUSTOM", "Свои лимиты", "")], default="FINAL", update=_upd_preset)
    pass_limit: IntProperty(name="Лимит проходов", default=0, min=0, soft_max=10000, update=_upd_quality,
                            description="Сэмплов на пиксель; 0 — без лимита (останавливают шум или время)")
    time_limit: FloatProperty(name="Лимит времени, мин", default=0.0, min=0.0, soft_max=600, update=_upd_quality,
                              description="0 — без лимита")
    noise_limit: FloatProperty(name="Лимит шума, %", default=2.0, min=0.0, max=50.0, update=_upd_quality,
                               description="Адаптивный сэмплинг: порог шума. 0 — выключен")
    denoise: EnumProperty(name="Денойз", items=[
        ("OIDN", "Intel OIDN (albedo + normal)", ""),
        ("OFF", "Выкл", "")], default="OIDN", update=_upd_quality)
    denoise_amount: FloatProperty(name="Сила денойза", default=1.0, min=0.0, max=1.0, update=_upd_vfb,
                                  description="Смешивание с исходным шумным изображением (нужен VFB)")
    viewport_denoise: BoolProperty(name="Денойз во вьюпорте", default=True, update=_upd_quality)
    max_bounces: IntProperty(name="Отскоков света", default=25, min=1, max=1024, update=_upd_quality)
    max_sample_intensity: FloatProperty(name="Макс. яркость сэмпла", default=20.0, min=0.0, update=_upd_quality,
                                        description="Подавляет «светлячков» (Clamp Indirect). 0 — выкл")
    caustics: BoolProperty(name="Каустики", default=False, update=_upd_quality)
    path_guiding: BoolProperty(name="Path guiding (как кэш UHD, только CPU)", default=False, update=_upd_quality)

    vfb_enabled: BoolProperty(name="Постобработка VFB", default=True, update=_upd_vfb_toggle,
                              description="Цепочка в компоновщике: экспозиция, баланс белого, bloom/glare…")
    viewport_vfb: BoolProperty(name="VFB во вьюпорте (вид из камеры)", default=False, update=_upd_viewport_comp)
    tonemap: EnumProperty(name="Тонмаппинг", items=_tonemaps, update=_upd_tonemap)
    exposure: FloatProperty(name="Экспозиция (EV)", default=0.0, soft_min=-5, soft_max=5, update=_upd_vfb)
    wb_kelvin: FloatProperty(name="Баланс белого, K", default=6500.0, min=1000.0, max=40000.0, soft_max=12000,
                             update=_upd_vfb, description="Какая температура света станет белой")
    wb_tint: FloatProperty(name="Оттенок (tint)", default=0.0, soft_min=-100, soft_max=100, update=_upd_vfb)
    highlight_compress: FloatProperty(name="Сжатие светов", default=1.0, min=1.0, soft_max=10.0, update=_upd_vfb)
    contrast: FloatProperty(name="Контраст", default=0.0, min=-100, max=100, update=_upd_vfb)
    saturation: FloatProperty(name="Насыщенность", default=0.0, min=-1.0, max=1.0, update=_upd_vfb)
    bloom_on: BoolProperty(name="Bloom", default=False, update=_upd_vfb)
    bloom_intensity: FloatProperty(name="Сила", default=0.3, min=0.0, max=1.0, update=_upd_vfb)
    bloom_size: FloatProperty(name="Размер", default=0.5, min=0.0, max=1.0, update=_upd_vfb)
    bloom_threshold: FloatProperty(name="Порог", default=1.0, min=0.0, update=_upd_vfb)
    glare_on: BoolProperty(name="Glare (лучи)", default=False, update=_upd_vfb)
    glare_intensity: FloatProperty(name="Сила", default=0.2, min=0.0, max=1.0, update=_upd_vfb)
    glare_streaks: IntProperty(name="Лучей", default=6, min=1, max=16, update=_upd_vfb)
    glare_angle: FloatProperty(name="Поворот, °", default=15.0, min=0, max=180, update=_upd_vfb)
    glare_threshold: FloatProperty(name="Порог", default=1.0, min=0.0, update=_upd_vfb)
    vignette: FloatProperty(name="Виньетка", default=0.0, min=0.0, max=1.0, update=_upd_vfb)
    vignette_falloff: FloatProperty(name="Мягкость", default=0.6, min=0.05, max=1.0, update=_upd_vfb)
    sharpen: FloatProperty(name="Резкость", default=0.0, min=0.0, max=1.0, update=_upd_vfb)

    lightmix_on: BoolProperty(name="LightMix", default=False, update=_upd_quality,
                              description="Смешивание источников света после рендера (light groups)")
    lightmix: CollectionProperty(type=CoronaLightMixItem)
    lightmix_index: IntProperty()

    new_light_kelvin: FloatProperty(name="Температура, K", default=4500.0, min=1000, max=40000)
    sun_object: PointerProperty(type=bpy.types.Object, name="Солнце",
                                poll=lambda self, o: o.type == "LIGHT" and o.data.type == "SUN")
    sun_sync: BoolProperty(name="Небо следует за солнцем", default=True)


# ------------------------------------------------------------------ сетевой рендер
class DRNodeItem(bpy.types.PropertyGroup):
    name: StringProperty(name="Имя")
    host: StringProperty(name="Адрес")
    port: IntProperty(name="Порт", default=protocol.DEFAULT_PORT, min=1, max=65535)
    enabled: BoolProperty(name="Вкл", default=True)
    weight: FloatProperty(name="Вес", default=1.0, min=0.0, soft_max=10.0,
                          description="Производительность узла: доля кадров при распределении «по весам»")
    frames: StringProperty(name="Кадры", default="",
                           description="Ручное назначение, например «1-50» или «1-10, 20, 30-40»")
    status: StringProperty(name="Состояние", default="—")
    progress: FloatProperty(name="Прогресс", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    done: IntProperty(name="Готово")
    cores: IntProperty(name="Ядер")
    info: StringProperty()


class DRSettings(bpy.types.PropertyGroup):
    nodes: CollectionProperty(type=DRNodeItem)
    index: IntProperty()
    mode: EnumProperty(name="Режим", items=[
        ("FRAME", "Кадр", "Один кадр делится на части, головной собирает итог"),
        ("ANIM", "Анимация", "Кадры анимации распределяются между узлами")], default="FRAME")
    tiles: IntProperty(name="Частей", default=8, min=1, max=256,
                       description="На сколько частей резать кадр (лучше в 2–4 раза больше числа узлов)")
    tile_kind: EnumProperty(name="Разбиение", items=[
        ("GRID", "Сетка", ""), ("STRIPS", "Полосы", "")], default="GRID")
    distribution: EnumProperty(name="Распределение", items=[
        ("AUTO", "Очередь", "Свободный узел берёт следующий кадр"),
        ("WEIGHTED", "По весам", "Непрерывные куски пропорционально весу узла"),
        ("MANUAL", "Вручную", "Диапазоны кадров в таблице узлов; неназначенные — в общую очередь")],
        default="AUTO")
    chunk: IntProperty(name="Кадров за раз", default=1, min=1, max=100,
                       description="Сколько кадров узел рендерит за один запуск Blender")
    steal: BoolProperty(name="Свободные узлы помогают", default=True,
                        description="Узел без работы забирает кадры из конца чужой очереди")
    post_mode: EnumProperty(name="Постобработка кадра", items=[
        ("AUTO", "Как в сцене", "Corona VFB, если включён режим Corona, иначе компоновщик сцены"),
        ("SCENE", "Компоновщик сцены", "Обычный рендер: свой компоновщик и денойз Cycles"),
        ("NONE", "Без постобработки", "Только сборка (и денойз, если включён)")], default="AUTO")
    output_dir: StringProperty(name="Папка кадра", subtype="DIR_PATH", default="//dr_render/",
                               description="Куда сохранять собранный кадр в режиме «Кадр»")
    head_renders: BoolProperty(name="Головной тоже рендерит", default=True,
                               description="Запустить узел на этом компьютере и добавить его в список")
    force_cpu: BoolProperty(name="Узлы рендерят на CPU", default=False)
    manual_host: StringProperty(name="IP", default="192.168.1.")
    manual_port: IntProperty(name="Порт", default=protocol.DEFAULT_PORT, min=1, max=65535)
    job_status: StringProperty(default="")
    job_progress: FloatProperty(default=0.0, min=0, max=1, subtype="FACTOR")
    show_log: BoolProperty(name="Журнал", default=False)


classes = (CoronaLightMixItem, CoronaSettings, DRNodeItem, DRSettings)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.corona = PointerProperty(type=CoronaSettings)
    bpy.types.Scene.corona_dr = PointerProperty(type=DRSettings)


def unregister():
    del bpy.types.Scene.corona_dr
    del bpy.types.Scene.corona
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

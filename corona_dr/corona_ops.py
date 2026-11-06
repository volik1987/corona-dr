import bpy

from . import corona


class CORONA_OT_apply(bpy.types.Operator):
    bl_idname = "corona.apply"
    bl_label = "Применить настройки Corona"
    bl_description = "Заново настроить Cycles и пересобрать цепочку VFB в компоновщике"

    def execute(self, context):
        sc = context.scene
        sc.corona.enabled = True
        corona.apply_all(sc)
        return {"FINISHED"}


class CORONA_OT_add_material(bpy.types.Operator):
    bl_idname = "corona.add_material"
    bl_label = "Материал Corona Physical"
    bl_description = "Создать упрощённый материал Corona Physical и назначить выделенным объектам"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        m = corona.new_physical_material()
        n = 0
        for o in context.selected_objects:
            if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
                if o.data.materials:
                    o.data.materials[o.active_material_index if o.active_material_index < len(o.data.materials) else 0] = m
                else:
                    o.data.materials.append(m)
                n += 1
        self.report({"INFO"}, f"Материал «{m.name}» назначен объектам: {n}")
        return {"FINISHED"}


class CORONA_OT_add_light(bpy.types.Operator):
    bl_idname = "corona.add_light"
    bl_label = "Corona Light"
    bl_description = "Прямоугольный источник (Area) с цветом по температуре в Кельвинах"
    bl_options = {"REGISTER", "UNDO"}

    kelvin: bpy.props.FloatProperty(name="Температура, K", default=4500, min=1000, max=40000)
    power: bpy.props.FloatProperty(name="Мощность, Вт", default=300, min=0)
    size: bpy.props.FloatProperty(name="Размер, м", default=1.0, min=0.001)
    visible: bpy.props.BoolProperty(name="Видим в камере", default=False)

    def invoke(self, context, event):
        self.kelvin = context.scene.corona.new_light_kelvin
        return self.execute(context)

    def execute(self, context):
        ld = bpy.data.lights.new("Corona Light", "AREA")
        ld.shape = "RECTANGLE"
        ld.size, ld.size_y = self.size, self.size
        ld.energy = self.power
        corona.set_light_kelvin(ld, self.kelvin)
        ob = bpy.data.objects.new("Corona Light", ld)
        context.collection.objects.link(ob)
        ob.location = context.scene.cursor.location
        ob.visible_camera = self.visible
        for o in context.selected_objects:
            o.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {"FINISHED"}


class CORONA_OT_light_kelvin(bpy.types.Operator):
    bl_idname = "corona.light_kelvin"
    bl_label = "Температура выделенным"
    bl_description = "Задать цветовую температуру выделенным источникам"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        k = context.scene.corona.new_light_kelvin
        n = 0
        for o in context.selected_objects:
            if o.type == "LIGHT":
                corona.set_light_kelvin(o.data, k)
                n += 1
        self.report({"INFO"}, f"Источников: {n}")
        return {"FINISHED"}


class CORONA_OT_add_sun_sky(bpy.types.Operator):
    bl_idname = "corona.add_sun_sky"
    bl_label = "Corona Sun + Sky"
    bl_description = "Солнце (лампа Sun) и физическое небо, которое следует за направлением солнца"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        corona.create_sun_sky(context.scene, context)
        return {"FINISHED"}


class CORONA_OT_sync_sky(bpy.types.Operator):
    bl_idname = "corona.sync_sky"
    bl_label = "Синхронизировать небо"

    def execute(self, context):
        if not corona.sync_sun_sky(context.scene):
            self.report({"WARNING"}, "Нужны лампа Sun (поле «Солнце») и текстура Sky в мире")
        return {"FINISHED"}


class CORONA_OT_lightmix_setup(bpy.types.Operator):
    bl_idname = "corona.lightmix_setup"
    bl_label = "Создать LightMix"
    bl_description = "Создать light group на каждый источник света и окружение и включить LightMix"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        n = corona.setup_lightmix(sc, corona.main_view_layer(sc))
        sc.corona.lightmix_on = True
        corona.apply_all(sc)
        self.report({"INFO"}, f"Новых групп: {n}. Меняйте цвет/силу после рендера — кадр пересчитается без рендера.")
        return {"FINISHED"}


class CORONA_OT_lightmix_refresh(bpy.types.Operator):
    bl_idname = "corona.lightmix_refresh"
    bl_label = "Обновить список групп"

    def execute(self, context):
        corona.refresh_lightmix(context.scene, corona.main_view_layer(context.scene))
        corona.apply_vfb(context.scene)
        return {"FINISHED"}


class CORONA_OT_vfb_reset(bpy.types.Operator):
    bl_idname = "corona.vfb_reset"
    bl_label = "Сбросить VFB"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        c = context.scene.corona
        for k in ("exposure", "wb_kelvin", "wb_tint", "highlight_compress", "contrast", "saturation",
                  "bloom_on", "glare_on", "vignette", "sharpen"):
            c.property_unset(k)
        corona.apply_vfb(context.scene)
        return {"FINISHED"}


classes = (CORONA_OT_apply, CORONA_OT_add_material, CORONA_OT_add_light, CORONA_OT_light_kelvin,
           CORONA_OT_add_sun_sky, CORONA_OT_sync_sky, CORONA_OT_lightmix_setup,
           CORONA_OT_lightmix_refresh, CORONA_OT_vfb_reset)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    if corona.depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(corona.depsgraph_handler)


def unregister():
    if corona.depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(corona.depsgraph_handler)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

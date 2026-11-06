import bpy

from . import dr_ops


class _RenderPanel:
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"


class _CoronaPanel(_RenderPanel):
    @classmethod
    def poll(cls, context):
        return context.scene.render.engine == "CYCLES"


# ------------------------------------------------------------------ Corona
class CORONA_PT_main(_CoronaPanel, bpy.types.Panel):
    bl_label = "Corona (визуализация)"
    bl_order = 0

    def draw_header(self, context):
        self.layout.prop(context.scene.corona, "enabled", text="")

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        c = context.scene.corona
        col = self.layout.column()
        if not c.enabled:
            col.label(text="Включите галочку — Cycles настроится как Corona", icon="INFO")
            return
        col.prop(c, "preset")
        sub = col.column(align=True)
        sub.prop(c, "pass_limit")
        sub.prop(c, "time_limit")
        sub.prop(c, "noise_limit")
        col.separator()
        col.prop(c, "denoise")
        row = col.row()
        row.active = c.denoise != "OFF" and c.vfb_enabled
        row.prop(c, "denoise_amount", slider=True)
        col.prop(c, "viewport_denoise")
        col.operator("corona.apply", icon="FILE_REFRESH")


class CORONA_PT_advanced(_CoronaPanel, bpy.types.Panel):
    bl_label = "Свет и производительность"
    bl_parent_id = "CORONA_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return super().poll(context) and context.scene.corona.enabled

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        c = context.scene.corona
        col = self.layout.column()
        col.prop(c, "max_bounces")
        col.prop(c, "max_sample_intensity")
        col.prop(c, "caustics")
        col.prop(c, "path_guiding")


class CORONA_PT_vfb(_CoronaPanel, bpy.types.Panel):
    bl_label = "VFB: постобработка"
    bl_parent_id = "CORONA_PT_main"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and context.scene.corona.enabled

    def draw_header(self, context):
        self.layout.prop(context.scene.corona, "vfb_enabled", text="")

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        c = context.scene.corona
        lay = self.layout
        lay.active = c.vfb_enabled
        col = lay.column()
        col.prop(c, "tonemap")
        col.prop(c, "exposure")
        col.prop(c, "highlight_compress")
        r = col.column(align=True)
        r.prop(c, "wb_kelvin")
        r.prop(c, "wb_tint")
        r = col.column(align=True)
        r.prop(c, "contrast")
        r.prop(c, "saturation", slider=True)
        col.separator()
        box = col.box()
        box.prop(c, "bloom_on")
        if c.bloom_on:
            b = box.column(align=True)
            b.prop(c, "bloom_intensity", slider=True)
            b.prop(c, "bloom_size", slider=True)
            b.prop(c, "bloom_threshold")
        box = col.box()
        box.prop(c, "glare_on")
        if c.glare_on:
            b = box.column(align=True)
            b.prop(c, "glare_intensity", slider=True)
            b.prop(c, "glare_streaks")
            b.prop(c, "glare_angle")
            b.prop(c, "glare_threshold")
        r = col.column(align=True)
        r.prop(c, "vignette", slider=True)
        r.prop(c, "vignette_falloff", slider=True)
        col.prop(c, "sharpen", slider=True)
        col.prop(c, "viewport_vfb")
        col.operator("corona.vfb_reset", icon="LOOP_BACK")
        col.label(text="Цепочка живёт в компоновщике: после рендера правки видны сразу", icon="INFO")


class CORONA_UL_lightmix(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.label(text=item.group, icon="LIGHT")
        row.prop(item, "color", text="")
        row.prop(item, "intensity", text="")


class CORONA_PT_lightmix(_CoronaPanel, bpy.types.Panel):
    bl_label = "LightMix"
    bl_parent_id = "CORONA_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return super().poll(context) and context.scene.corona.enabled

    def draw_header(self, context):
        self.layout.prop(context.scene.corona, "lightmix_on", text="")

    def draw(self, context):
        c = context.scene.corona
        col = self.layout.column()
        row = col.row(align=True)
        row.operator("corona.lightmix_setup", icon="LIGHT")
        row.operator("corona.lightmix_refresh", text="", icon="FILE_REFRESH")
        col.template_list("CORONA_UL_lightmix", "", c, "lightmix", c, "lightmix_index", rows=3)
        if c.lightmix_on and not c.vfb_enabled:
            col.label(text="LightMix работает через VFB — включите его", icon="ERROR")


class CORONA_PT_scene(_CoronaPanel, bpy.types.Panel):
    bl_label = "Материалы и свет"
    bl_parent_id = "CORONA_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        c = context.scene.corona
        col = self.layout.column()
        col.operator("corona.add_material", icon="MATERIAL")
        col.separator()
        col.prop(c, "new_light_kelvin")
        row = col.row(align=True)
        row.operator("corona.add_light", icon="LIGHT_AREA")
        row.operator("corona.light_kelvin", icon="COLOR")
        col.separator()
        col.operator("corona.add_sun_sky", icon="LIGHT_SUN")
        row = col.row(align=True)
        row.prop(c, "sun_object")
        row.operator("corona.sync_sky", text="", icon="FILE_REFRESH")
        col.prop(c, "sun_sync")


# ------------------------------------------------------------------ сетевой рендер
class CORONA_UL_nodes(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        dr = data
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        sub = row.row()
        sub.ui_units_x = 6
        sub.label(text=item.name or item.host, icon="DESKTOP")
        if dr.mode == "ANIM" and dr.distribution == "MANUAL":
            row.prop(item, "frames", text="")
        elif dr.mode == "ANIM" and dr.distribution == "WEIGHTED":
            row.prop(item, "weight", text="вес")
        if item.progress > 0:
            row.progress(factor=item.progress, text=f"{item.progress * 100:.0f}%")
        st = row.row()
        st.ui_units_x = 6
        st.label(text=f"{item.status} · {item.done}")


class CORONA_PT_dr(_RenderPanel, bpy.types.Panel):
    bl_label = "Сетевой рендер (DR)"
    bl_order = 1

    def draw(self, context):
        dr = context.scene.corona_dr
        col = self.layout.column()
        # --- этот компьютер как узел
        box = col.box()
        row = box.row()
        on = dr_ops.node_running()
        row.operator("corona_dr.node_toggle", text="Остановить узел" if on else "Включить узел рендера",
                     icon="PAUSE" if on else "PLAY", depress=on)
        if on:
            n = dr_ops.NODE
            box.label(text=f"«{n.name}» порт {n.port}: {n.status}", icon="CHECKMARK")
            if n.busy:
                box.progress(factor=n.progress, text=f"{n.progress * 100:.0f}%")
        # --- узлы
        col.separator()
        row = col.row(align=True)
        row.operator("corona_dr.discover", icon="VIEWZOOM")
        row = col.row()
        row.template_list("CORONA_UL_nodes", "", dr, "nodes", dr, "index", rows=3)
        side = row.column(align=True)
        side.operator("corona_dr.node_remove", text="", icon="REMOVE")
        row = col.row(align=True)
        row.prop(dr, "manual_host")
        row.prop(dr, "manual_port", text="")
        row.operator("corona_dr.node_add", text="", icon="ADD")
        if 0 <= dr.index < len(dr.nodes):
            it = dr.nodes[dr.index]
            b = col.box().column(align=True)
            b.label(text=f"{it.name}: {it.host}:{it.port}" + (f", ядер {it.cores}" if it.cores else ""))
            r = b.row(align=True)
            r.prop(it, "weight")
            r.prop(it, "frames")
        col.prop(dr, "head_renders")
        # --- задание
        col.separator()
        col.row().prop(dr, "mode", expand=True)
        if dr.mode == "FRAME":
            r = col.row(align=True)
            r.prop(dr, "tiles")
            r.prop(dr, "tile_kind", text="")
            col.prop(dr, "post_mode")
            col.prop(dr, "output_dir")
        else:
            col.prop(dr, "distribution")
            if dr.distribution == "MANUAL":
                col.operator("corona_dr.frames_split", icon="SEQ_STRIP_DUPLICATE")
            r = col.row()
            r.prop(dr, "chunk")
            r.prop(dr, "steal")
            col.label(text="Кадры сохраняются по пути Output сцены", icon="FILE_FOLDER")
        col.prop(dr, "force_cpu")
        col.separator()
        row = col.row(align=True)
        row.scale_y = 1.4
        row.operator("corona_dr.start", icon="RENDER_STILL" if dr.mode == "FRAME" else "RENDER_ANIMATION")
        row.operator("corona_dr.cancel", text="", icon="CANCEL")
        if dr.job_status:
            col.label(text=dr.job_status)
            if dr_ops.RUNNER is not None:
                col.progress(factor=dr.job_progress, text=f"{dr.job_progress * 100:.0f}%")
            col.operator("corona_dr.open_output", icon="FILEBROWSER")
        col.prop(dr, "show_log", icon="TEXT")
        if dr.show_log:
            b = col.box().column(align=True)
            b.scale_y = 0.7
            for line in dr_ops.job_log_lines(14):
                b.label(text=line)


classes = (CORONA_PT_main, CORONA_PT_advanced, CORONA_PT_vfb, CORONA_UL_lightmix, CORONA_PT_lightmix,
           CORONA_PT_scene, CORONA_UL_nodes, CORONA_PT_dr)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

"""VFB «как в Corona»: постобработка в компоновщике Blender.

Модуль не использует относительные импорты: его подключает и сам аддон,
и скрипт сборки dr/post.py, который работает в `blender -b` без аддона.
Настройки передаются обычным словарём (см. DEFAULTS)."""
import bpy

TREE_NAME = "Corona VFB"
TAG = "corona_vfb"

DEFAULTS = {
    "exposure": 0.0,            # EV
    "wb_kelvin": 6500.0,        # баланс белого, K (6500 — нейтрально)
    "wb_tint": 0.0,
    "highlight_compress": 1.0,  # 1 — выкл
    "contrast": 0.0,            # -100..100
    "saturation": 0.0,          # -1..1
    "bloom_on": False, "bloom_intensity": 0.3, "bloom_size": 0.5, "bloom_threshold": 1.0,
    "glare_on": False, "glare_intensity": 0.2, "glare_streaks": 6, "glare_angle": 15.0,
    "glare_threshold": 1.0,
    "vignette": 0.0,            # 0..1
    "vignette_falloff": 0.6,
    "sharpen": 0.0,             # 0..1
    "denoise": True,            # денойз в компоновщике (OIDN, albedo + normal)
    "denoise_amount": 1.0,
    "lightmix_on": False,
    "lightmix": [],             # [{"group", "enabled", "color", "intensity"}]
}


# ---------------------------------------------------------------- совместимость 4.x / 5.x
def set_menu(node, socket_name, value, attr=None, attr_value=None):
    sock = node.inputs.get(socket_name)
    if sock is not None and sock.type == "MENU":
        sock.default_value = value
    elif attr and hasattr(node, attr):
        setattr(node, attr, attr_value if attr_value is not None else value)


def set_input(node, name, value, index=None):
    socks = [s for s in node.inputs if s.name == name]
    if index is not None:
        socks = socks[index:index + 1]
    for s in socks:
        if s.enabled or index is not None:
            s.default_value = value
            return True
    if socks:
        socks[0].default_value = value
        return True
    return False


def _mix_node(tree, blend, fac=1.0):
    try:
        n = tree.nodes.new("ShaderNodeMix")
        n.data_type = "RGBA"
        n.blend_type = blend
        n.clamp_result = False
        n.clamp_factor = True
        n.inputs[0].default_value = fac
        return n, n.inputs[6], n.inputs[7], n.outputs[2], n.inputs[0]
    except RuntimeError:  # Blender 4.x без ShaderNodeMix в компоновщике
        n = tree.nodes.new("CompositorNodeMixRGB")
        n.blend_type = blend
        n.use_clamp = False
        n.inputs[0].default_value = fac
        return n, n.inputs[1], n.inputs[2], n.outputs[0], n.inputs[0]


def is_tree_ours(tree):
    return tree is not None and tree.get(TAG) is not None


def scene_tree(scene, create=True):
    """Дерево компоновщика сцены, которым управляет аддон."""
    if hasattr(scene, "compositing_node_group"):  # Blender 5.x
        ng = scene.compositing_node_group
        if is_tree_ours(ng) or not create:
            return ng
        if ng is not None:
            ng.use_fake_user = True          # чужое дерево не теряем
            scene["corona_prev_tree"] = ng.name
        ng = bpy.data.node_groups.new(TREE_NAME, "CompositorNodeTree")
        ng[TAG] = 1
        scene.compositing_node_group = ng
        return ng
    scene.use_nodes = True  # Blender 4.x
    scene.node_tree[TAG] = 1
    return scene.node_tree


def release_scene_tree(scene):
    """Отключить VFB: вернуть прежнее дерево компоновщика."""
    if hasattr(scene, "compositing_node_group"):
        ng = scene.compositing_node_group
        prev = bpy.data.node_groups.get(scene.get("corona_prev_tree", ""))
        scene.compositing_node_group = prev
        if is_tree_ours(ng):
            bpy.data.node_groups.remove(ng)
    else:
        tree = scene.node_tree
        if tree:
            for n in list(tree.nodes):
                tree.nodes.remove(n)
            rl = tree.nodes.new("CompositorNodeRLayers")
            comp = tree.nodes.new("CompositorNodeComposite")
            comp.location = (300, 0)
            tree.links.new(rl.outputs[0], comp.inputs[0])


def _output_socket(tree):
    """Вход итогового узла: Group Output (5.x) или Composite (4.x) + Viewer."""
    if hasattr(bpy.types, "CompositorNodeComposite"):
        out = tree.nodes.new("CompositorNodeComposite")
        sock = out.inputs[0]
    else:
        if not any(i.in_out == "OUTPUT" for i in tree.interface.items_tree if i.item_type == "SOCKET"):
            tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        out = tree.nodes.new("NodeGroupOutput")
        sock = out.inputs[0]
    viewer = tree.nodes.new("CompositorNodeViewer")
    return out, sock, viewer


# ---------------------------------------------------------------- источники
class Source:
    """Сокеты исходного изображения: image, alpha, albedo, normal, noisy, lightgroups{}."""

    def __init__(self, node, names):
        self.node = node
        out = node.outputs
        self.image = out.get(names.get("image", "Image")) or out[0]
        self.alpha = out.get("Alpha")
        self.albedo = out.get("Denoising Albedo")
        self.normal = out.get("Denoising Normal")
        self.lightgroups = {}
        for s in out:
            if s.name.startswith("Combined_") and s.enabled:
                self.lightgroups[s.name[len("Combined_"):]] = s


def source_rlayers(tree, scene, view_layer=None):
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.scene = scene
    if view_layer:
        rl.layer = view_layer
    return Source(rl, {"image": "Image"})


def source_image(tree, image):
    n = tree.nodes.new("CompositorNodeImage")
    n.image = image
    name = "Combined" if n.outputs.get("Combined") else "Image"
    return Source(n, {"image": name})


# ---------------------------------------------------------------- сборка цепочки
def build(tree, src, s, cycles_denoised=False):
    """Построить цепочку VFB. s — словарь настроек (недостающие берутся из DEFAULTS).
    cycles_denoised=True: картинка уже очищена Cycles (денойз в компоновщике не нужен)."""
    cfg = dict(DEFAULTS)
    cfg.update(s or {})
    s = cfg
    nodes, links = tree.nodes, tree.links
    x = [src.node.location.x + 300]

    def place(n, dy=0):
        n.location = (x[0], dy)
        x[0] += 220
        return n

    can_denoise = s["denoise"] and not cycles_denoised and src.albedo is not None and src.normal is not None

    def denoise(sock, dy=0):
        if not can_denoise:
            return sock
        d = place(nodes.new("CompositorNodeDenoise"), dy)
        links.new(sock, d.inputs["Image"])
        links.new(src.albedo, d.inputs["Albedo"])
        links.new(src.normal, d.inputs["Normal"])
        set_menu(d, "Prefilter", "Accurate", "prefilter", "ACCURATE")
        if d.inputs.get("HDR") is not None:
            d.inputs["HDR"].default_value = True
        elif hasattr(d, "use_hdr"):
            d.use_hdr = True
        out = d.outputs[0]
        amt = float(s.get("denoise_amount", 1.0))
        if amt < 0.999:
            m, a, b, o, _f = _mix_node(tree, "MIX", amt)
            place(m, dy)
            links.new(sock, a)
            links.new(out, b)
            out = o
        return out

    # 1) LightMix или обычная картинка
    groups = [g for g in s.get("lightmix", []) if g.get("group") in src.lightgroups] if s["lightmix_on"] else []
    if groups:
        acc = None
        for i, g in enumerate(groups):
            sock = denoise(src.lightgroups[g["group"]], dy=-200 * i)
            if not g.get("enabled", True):
                continue
            m, a, b, o, _f = _mix_node(tree, "MULTIPLY", 1.0)
            place(m, -200 * i)
            k = float(g.get("intensity", 1.0))
            c = g.get("color", (1, 1, 1))
            links.new(sock, a)
            b.default_value = (c[0] * k, c[1] * k, c[2] * k, 1.0)
            if acc is None:
                acc = o
            else:
                m2, a2, b2, o2, _ = _mix_node(tree, "ADD", 1.0)
                place(m2, -200 * i)
                links.new(acc, a2)
                links.new(o, b2)
                acc = o2
        if acc is None:  # все группы выключены — чёрный кадр
            m, a, b, o, _f = _mix_node(tree, "MULTIPLY", 1.0)
            place(m)
            links.new(src.image, a)
            b.default_value = (0, 0, 0, 1)
            acc = o
        cur = acc
    else:
        cur = denoise(src.image)

    # 2) экспозиция
    if abs(s["exposure"]) > 1e-4:
        n = place(nodes.new("CompositorNodeExposure"))
        links.new(cur, n.inputs[0])
        set_input(n, "Exposure", float(s["exposure"]))
        cur = n.outputs[0]

    # 3) баланс белого (Kelvin + оттенок)
    if abs(s["wb_kelvin"] - 6500.0) > 1 or abs(s["wb_tint"]) > 1e-3:
        n = place(nodes.new("CompositorNodeColorBalance"))
        links.new(cur, n.inputs[0])
        if n.inputs.get("Type") is not None and n.inputs["Type"].type == "MENU":
            n.inputs["Type"].default_value = "White Point"
            temps = [i for i in n.inputs if i.name == "Temperature"]
            tints = [i for i in n.inputs if i.name == "Tint"]
            temps[0].default_value = float(s["wb_kelvin"])   # вход: что считать белым
            tints[0].default_value = float(s["wb_tint"])
            temps[1].default_value = 6500.0                   # выход: D65
            tints[1].default_value = 0.0
        elif hasattr(n, "input_temperature"):  # 4.3–4.5
            n.correction_method = "WHITEPOINT"
            n.input_temperature = float(s["wb_kelvin"])
            n.input_tint = float(s["wb_tint"])
            n.output_temperature = 6500.0
            n.output_tint = 0.0
        cur = n.outputs[0]

    # 4) сжатие светов: c / (1 + c·(1 − 1/HC))
    hc = float(s["highlight_compress"])
    if hc > 1.0001:
        k = 1.0 - 1.0 / hc
        m1, a1, b1, o1, _ = _mix_node(tree, "MULTIPLY", 1.0)
        place(m1)
        links.new(cur, a1)
        b1.default_value = (k, k, k, 1.0)
        m2, a2, b2, o2, _ = _mix_node(tree, "ADD", 1.0)
        place(m2)
        links.new(o1, a2)
        b2.default_value = (1, 1, 1, 1)
        m3, a3, b3, o3, _ = _mix_node(tree, "DIVIDE", 1.0)
        place(m3)
        links.new(cur, a3)
        links.new(o2, b3)
        cur = o3

    # 5) контраст
    if abs(s["contrast"]) > 1e-3:
        n = place(nodes.new("CompositorNodeBrightContrast"))
        links.new(cur, n.inputs[0])
        set_input(n, "Contrast", float(s["contrast"]))
        cur = n.outputs[0]

    # 6) насыщенность
    if abs(s["saturation"]) > 1e-3:
        n = place(nodes.new("CompositorNodeHueSat"))
        links.new(cur, n.inputs[0])
        set_input(n, "Saturation", 1.0 + float(s["saturation"]))
        cur = n.outputs[0]

    # 7) bloom и glare
    def glare(kind, kind_old, strength, size, threshold, streaks=None, angle=None):
        n = place(nodes.new("CompositorNodeGlare"))
        links.new(cur, n.inputs[0])
        set_menu(n, "Type", kind, "glare_type", kind_old)
        set_menu(n, "Quality", "High", "quality", "HIGH")
        set_input(n, "Threshold", float(threshold))
        if not set_input(n, "Strength", float(strength)) and hasattr(n, "mix"):
            n.mix = float(strength) * 2 - 1
        if size is not None:
            set_input(n, "Size", float(size))
        if streaks is not None:
            set_input(n, "Streaks", int(streaks))
            set_input(n, "Streaks Angle", __import__("math").radians(float(angle)))
            set_input(n, "Fade", 0.85)
        return n.outputs[0]

    if s["bloom_on"] and s["bloom_intensity"] > 0:
        cur = glare("Bloom", "BLOOM", s["bloom_intensity"], s["bloom_size"], s["bloom_threshold"])
    if s["glare_on"] and s["glare_intensity"] > 0:
        cur = glare("Streaks", "STREAKS", s["glare_intensity"], None, s["glare_threshold"],
                    s["glare_streaks"], s["glare_angle"])

    # 8) виньетка: множитель 1 − v·smoothstep(r0, r1, |uv|)
    v = float(s["vignette"])
    if v > 1e-3 and hasattr(bpy.types, "CompositorNodeImageCoordinates"):
        ic = place(nodes.new("CompositorNodeImageCoordinates"), -250)
        links.new(cur, ic.inputs[0])
        vm = place(nodes.new("ShaderNodeVectorMath"), -250)
        vm.operation = "MULTIPLY_ADD"
        links.new(ic.outputs["Normalized"], vm.inputs[0])
        vm.inputs[1].default_value = (2.0, 2.0, 0.0)
        vm.inputs[2].default_value = (-1.0, -1.0, 0.0)
        ln = place(nodes.new("ShaderNodeVectorMath"), -250)
        ln.operation = "LENGTH"
        links.new(vm.outputs[0], ln.inputs[0])
        mr = place(nodes.new("ShaderNodeMapRange"), -250)
        mr.interpolation_type = "SMOOTHSTEP"
        fall = max(0.05, min(1.0, float(s["vignette_falloff"])))
        mr.inputs["From Min"].default_value = 1.45 - fall * 1.2
        mr.inputs["From Max"].default_value = 1.45
        mr.inputs["To Min"].default_value = 0.0
        mr.inputs["To Max"].default_value = v
        links.new(ln.outputs["Value"], mr.inputs["Value"])
        m, a, b, o, f = _mix_node(tree, "MIX", 0.0)
        place(m)
        links.new(cur, a)
        b.default_value = (0, 0, 0, 1)
        links.new(mr.outputs["Result"], f)
        cur = o

    # 9) резкость
    if s["sharpen"] > 1e-3:
        n = place(nodes.new("CompositorNodeFilter"))
        links.new(cur, n.inputs[0])
        set_menu(n, "Type", "Diamond Sharpen", "filter_type", "SHARPEN_DIAMOND")
        set_input(n, "Factor", min(1.0, float(s["sharpen"])))
        cur = n.outputs[0]

    # 10) альфа из исходника и выход
    if src.alpha is not None:
        n = place(nodes.new("CompositorNodeSetAlpha"))
        links.new(cur, n.inputs[0])
        links.new(src.alpha, n.inputs["Alpha"])
        set_menu(n, "Type", "Replace Alpha", "mode", "REPLACE_ALPHA")
        cur = n.outputs[0]
    out, sock, viewer = _output_socket(tree)
    place(out)
    viewer.location = (out.location.x, out.location.y - 200)
    links.new(cur, sock)
    links.new(cur, viewer.inputs[0])
    return cur


def rebuild_scene(scene, settings, view_layer=None):
    """Пересобрать дерево компоновщика сцены из Render Layers (локальный рендер)."""
    tree = scene_tree(scene)
    tree.nodes.clear()
    src = source_rlayers(tree, scene, view_layer)
    build(tree, src, settings)
    scene.render.use_compositing = True
    return tree

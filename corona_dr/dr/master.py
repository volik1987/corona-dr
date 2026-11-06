"""Головной компьютер: раздаёт задания узлам, следит за ними, переотправляет
задания упавших узлов и собирает результат. Не зависит от bpy (работает в потоках)."""
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque

try:
    from . import protocol as P
except ImportError:
    import protocol as P

HERE = os.path.dirname(os.path.abspath(__file__))
POST_SCRIPT = os.path.join(HERE, "post.py")


# ---------------------------------------------------------------- разбиение
def parse_frames(text):
    """'1-50, 60, 70-80' → [1..50, 60, 70..80]"""
    out = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            i = part.index("-", 1)
            a, b = int(part[:i]), int(part[i + 1:])
            step = 1 if b >= a else -1
            out.extend(range(a, b + step, step))
        else:
            out.append(int(part))
    seen, res = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            res.append(f)
    return res


def make_tiles(W, H, count, kind="GRID"):
    """Разбить кадр W×H на части. Возвращает [(x0, y0, x1, y1)] в координатах Blender
    (y снизу), правая/верхняя граница не включается."""
    count = max(1, int(count))
    if kind == "STRIPS":
        cols, rows = 1, count
    else:
        best = None
        for c in range(1, count + 1):
            if count % c:
                continue
            r = count // c
            score = abs((W / c) / (H / r) - 1.0)  # ближе к квадратам
            if best is None or score < best[0]:
                best = (score, c, r)
        _, cols, rows = best
    xs = [round(W * i / cols) for i in range(cols + 1)]
    ys = [round(H * i / rows) for i in range(rows + 1)]
    tiles = []
    for r in reversed(range(rows)):          # сверху вниз — так приятнее смотреть прогресс
        for c in range(cols):
            if xs[c + 1] > xs[c] and ys[r + 1] > ys[r]:
                tiles.append((xs[c], ys[r], xs[c + 1], ys[r + 1]))
    return tiles


def split_weighted(frames, weights):
    """Разбить список кадров на непрерывные куски пропорционально весам."""
    total = sum(weights) or 1
    n = len(frames)
    out, start, acc = [], 0, 0.0
    for i, w in enumerate(weights):
        acc += w
        end = n if i == len(weights) - 1 else int(round(n * acc / total))
        out.append(frames[start:end])
        start = end
    return out


# ---------------------------------------------------------------- задание
class Task:
    def __init__(self, tid, kind, payload, label, frames=None, pref=None):
        self.id = tid
        self.kind = kind            # 'tile' | 'frames'
        self.payload = payload      # то, что уходит на узел
        self.label = label
        self.frames = frames or []
        self.pref = pref            # индекс узла, которому назначено (или None — общая очередь)
        self.status = "ждёт"
        self.node = None
        self.attempts = 0
        self.failed_on = set()
        self.files = []
        self.progress = 0.0


class NodeState:
    def __init__(self, host, port, name=None, weight=1.0):
        self.host, self.port = host, int(port)
        self.name = name or f"{host}:{port}"
        self.weight = weight
        self.status = "ожидание"
        self.progress = 0.0
        self.current = ""
        self.done = 0
        self.error = ""
        self.alive = True
        self.done_labels = []

    def as_dict(self):
        return {k: getattr(self, k) for k in
                ("host", "port", "name", "status", "progress", "current", "done", "error", "alive")}


class Job:
    """mode: 'tile' (один кадр по частям) или 'anim' (кадры анимации)."""

    def __init__(self, blend_path, nodes, tasks, workdir, mode, key="", steal=True,
                 max_attempts=3, finalize=None, on_file=None, log=print, retry_delay=2.0):
        self.blend = blend_path
        self.sha = P.file_sha1(blend_path)
        self.nodes = nodes                  # [NodeState]
        self.tasks = tasks                  # [Task]
        self.workdir = workdir
        self.mode = mode
        self.key = key
        self.steal = steal
        self.max_attempts = max_attempts
        self.finalize = finalize            # callable(job) → None, вызывается в потоке
        self.on_file = on_file              # callable(job, task, path) → новый путь
        self.log_fn = log
        self.retry_delay = retry_delay
        self.lock = threading.Lock()
        self.personal = [deque() for _ in nodes]
        self.common = deque()
        for t in tasks:
            if t.pref is not None and 0 <= t.pref < len(nodes):
                self.personal[t.pref].append(t)
            else:
                self.common.append(t)
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.ok = False
        self.error = ""
        self.status = "запуск"
        self.started = time.time()
        self.elapsed = 0.0
        self.log_lines = deque(maxlen=200)
        self.result = None
        self._threads = []
        os.makedirs(workdir, exist_ok=True)

    # -------------------------------------------------------- служебное
    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.log_lines.append(line)
        try:
            self.log_fn(line)
        except Exception:
            pass

    @property
    def done_count(self):
        return sum(1 for t in self.tasks if t.status == "готово")

    @property
    def progress(self):
        if not self.tasks:
            return 1.0
        part = sum(1.0 if t.status == "готово" else (t.progress if t.status == "рендер" else 0.0)
                   for t in self.tasks)
        return part / len(self.tasks)

    def _all_done(self):
        return all(t.status == "готово" for t in self.tasks)

    def _alive_nodes(self):
        return [i for i, n in enumerate(self.nodes) if n.alive]

    # -------------------------------------------------------- планировщик
    def _take(self, idx):
        with self.lock:
            alive = self._alive_nodes()
            others_alive = [i for i in alive if i != idx]

            def ok(t):  # на узле, где задание уже падало, повторяем только если больше некому
                return idx not in t.failed_on or not others_alive

            for q in (self.personal[idx], self.common):
                for t in list(q):
                    if ok(t):
                        q.remove(t)
                        return t
            if self.steal:
                cands = [(len(self.personal[i]), i) for i in others_alive if self.personal[i]]
                for _, i in sorted(cands, reverse=True):
                    q = self.personal[i]
                    if len(q) >= 2 or not self.nodes[i].current:
                        for t in reversed(list(q)):
                            if ok(t):
                                q.remove(t)
                                return t
            return None

    def _requeue(self, t, idx, reason):
        with self.lock:
            t.status = "ждёт"
            t.progress = 0.0
            t.node = None
            t.failed_on.add(idx)
            t.attempts += 1
            if t.attempts >= self.max_attempts:
                t.status = "ошибка"
                self.error = f"«{t.label}» не удалось за {t.attempts} попыток: {reason}"
                self.log(self.error)
                self.cancelled.set()
                return
            self.common.appendleft(t)
        self.log(f"«{t.label}» переотправлено ({reason})")

    def _node_dead(self, idx, reason):
        n = self.nodes[idx]
        with self.lock:
            n.alive = False
            n.status = "недоступен"
            n.error = reason
            while self.personal[idx]:
                self.common.append(self.personal[idx].popleft())
            if not self._alive_nodes() and not self._all_done():
                self.error = "не осталось работающих узлов"
                self.cancelled.set()
        self.log(f"узел {n.name}: {reason} — его задания уходят другим")

    # -------------------------------------------------------- запуск
    def start(self):
        self.status = "рендер"
        self.log(f"старт: {len(self.tasks)} заданий на {len(self.nodes)} узлах")
        for i in range(len(self.nodes)):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._supervisor, daemon=True).start()
        return self

    def cancel(self):
        self.error = self.error or "отменено"
        self.cancelled.set()

    def wait(self, timeout=None):
        return self.finished.wait(timeout)

    def _supervisor(self):
        for t in self._threads:
            t.join()
        self.elapsed = time.time() - self.started
        if self._all_done() and not self.cancelled.is_set():
            try:
                if self.finalize:
                    self.status = "сборка"
                    self.finalize(self)
                self.ok = True
                self.status = "готово"
                self.elapsed = time.time() - self.started
                self.log(f"готово за {self.elapsed:.1f} с")
            except Exception as e:
                self.error = f"сборка: {e}"
                self.status = "ошибка"
                self.log(self.error)
        else:
            self.status = "отменено" if self.error == "отменено" else "ошибка"
            self.log("остановлено: " + (self.error or "?"))
        self.finished.set()

    # -------------------------------------------------------- работа с узлом
    def _connect(self, idx):
        n = self.nodes[idx]
        s = P.connect(n.host, n.port, timeout=5.0)
        P.send_msg(s, {"cmd": "hello", "key": self.key})
        hdr, _ = P.recv_msg(s)
        if hdr.get("type") == "error":
            s.close()
            raise PermissionError(hdr.get("error"))
        n.name = hdr.get("name") or n.name
        P.send_msg(s, {"cmd": "has_blend", "sha": self.sha})
        hdr, _ = P.recv_msg(s)
        if not hdr.get("have"):
            n.status = "передача сцены"
            self.log(f"{n.name}: отправляю сцену ({os.path.getsize(self.blend) // 1024} КБ)")
            P.send_msg(s, {"cmd": "blend", "sha": self.sha}, file_path=self.blend)
            hdr, _ = P.recv_msg(s)
            if hdr.get("type") != "ok":
                raise ConnectionError(hdr.get("error", "сцена не принята"))
        return s

    def _worker(self, idx):
        n = self.nodes[idx]
        sock = None
        fails = 0
        while not self.cancelled.is_set():
            if sock is None:
                try:
                    n.status = "подключение"
                    sock = self._connect(idx)
                    n.status = "ожидание"
                    n.error = ""
                except PermissionError as e:
                    self._node_dead(idx, str(e))
                    return
                except OSError as e:
                    fails += 1
                    if fails >= 3:
                        self._node_dead(idx, f"нет связи ({e})")
                        return
                    n.status = f"повтор связи {fails}"
                    time.sleep(self.retry_delay)
                    continue
            t = self._take(idx)
            if t is None:
                if self._all_done():
                    break
                n.status = "ожидание"
                time.sleep(0.3)
                continue
            try:
                self._run_task(idx, sock, t)
                fails = 0
            except (OSError, ConnectionError, ValueError, P.ProtocolError) as e:
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                n.current = ""
                if self.cancelled.is_set():
                    break
                self._requeue(t, idx, f"обрыв связи с {n.name}")
                fails += 1
                if fails >= 3:
                    self._node_dead(idx, f"обрывы связи ({e})")
                    return
                time.sleep(self.retry_delay)
            except RuntimeError as e:   # узел сообщил об ошибке рендера
                n.current = ""
                n.error = str(e)
                self._requeue(t, idx, f"{n.name}: {e}")
                fails += 1
                if fails >= 3:
                    self._node_dead(idx, f"ошибки рендера ({e})")
                    try:
                        sock.close()
                    except OSError:
                        pass
                    return
        if sock is not None:
            try:
                if self.cancelled.is_set():
                    P.send_msg(sock, {"cmd": "cancel"})
                P.send_msg(sock, {"cmd": "bye"})
                sock.close()
            except OSError:
                pass
        if n.alive:
            n.status = "готово" if self._all_done() else "остановлен"
        n.progress = 0.0

    def _run_task(self, idx, sock, t):
        n = self.nodes[idx]
        t.status = "рендер"
        t.node = n.name
        n.current = t.label
        n.status = "рендер"
        n.progress = 0.0
        self.log(f"{n.name} ← {t.label}")
        P.send_msg(sock, {"cmd": "render", "sha": self.sha, "task": t.payload})
        tdir = os.path.join(self.workdir, f"task_{t.id}")
        os.makedirs(tdir, exist_ok=True)
        files = []
        sock.settimeout(1.0)
        last = time.time()
        try:
            while True:
                if self.cancelled.is_set():
                    P.send_msg(sock, {"cmd": "cancel"})
                    raise ConnectionError("отменено")
                try:
                    hdr, data = self._recv_task_msg(sock, tdir)
                except TimeoutError:
                    if time.time() - last > 120:   # узел молчит 2 минуты — считаем упавшим
                        raise ConnectionError("узел не отвечает")
                    continue
                last = time.time()
                typ = hdr.get("type")
                if typ == "progress":
                    n.progress = t.progress = float(hdr.get("progress", 0))
                elif typ == "file":
                    path = data
                    if self.on_file:
                        path = self.on_file(self, t, path) or path
                    files.append(path)
                elif typ == "done":
                    break
                elif typ == "error":
                    raise RuntimeError(hdr.get("error", "ошибка узла"))
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        with self.lock:
            t.files = files
            t.status = "готово"
            t.progress = 1.0
            n.done += 1
            n.done_labels.append(t.label)
        n.current = ""
        n.progress = 0.0
        self.log(f"{n.name} → {t.label} готово")

    def _recv_task_msg(self, sock, tdir):
        # заголовок ждём с таймаутом, сам файл — без него
        import socket as _s
        try:
            first = sock.recv(P._HDR.size, _s.MSG_PEEK)
        except _s.timeout:
            raise TimeoutError()
        if not first:
            raise ConnectionError("узел закрыл соединение")
        sock.settimeout(None)
        try:
            magic, jlen, size = P._HDR.unpack(P._recv_exact(sock, P._HDR.size))
            if magic != P.MAGIC:
                raise P.ProtocolError("чужой протокол")
            hdr = json.loads(P._recv_exact(sock, jlen).decode("utf-8"))
            data = None
            if size:
                name = os.path.basename(hdr.get("name") or "file.bin")
                dst = os.path.join(tdir, name)
                got = 0
                with open(dst + ".part", "wb") as f:
                    while got < size:
                        part = sock.recv(min(size - got, P.CHUNK))
                        if not part:
                            raise ConnectionError("обрыв при приёме файла")
                        f.write(part)
                        got += len(part)
                os.replace(dst + ".part", dst)
                data = dst
            return hdr, data
        finally:
            sock.settimeout(1.0)


# ---------------------------------------------------------------- готовые сценарии
def build_tile_job(blend, nodes, W, H, tiles_count, tile_kind, frame, view_layer, workdir,
                   blender_bin, vfb_settings, denoise, out_final, out_format=None, key="",
                   device=None, log=print, post_log=None, post_mode="corona"):
    """post_mode: 'corona' — VFB аддона; 'scene' — обычный рендер: собственный
    компоновщик сцены (если есть) + денойз Cycles, если он был включён."""
    tiles = make_tiles(W, H, tiles_count, tile_kind)
    tasks = []
    for i, (x0, y0, x1, y1) in enumerate(tiles):
        payload = {"mode": "tile", "frame": frame, "W": W, "H": H, "tile_id": f"{i:03d}",
                   "tile": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                   "view_layer": view_layer, "denoise_passes": bool(denoise),
                   "label": f"часть {i + 1}/{len(tiles)}"}
        if device:
            payload["device"] = device
        tasks.append(Task(i, "tile", payload, f"часть {i + 1}/{len(tiles)}"))

    def finalize(job):
        post = {"W": W, "H": H, "tiles": [], "out_exr": os.path.join(workdir, "assembled.exr"),
                "out_final": out_final, "vfb": vfb_settings, "denoise": bool(denoise),
                "format": out_format, "post_mode": post_mode}
        for t in job.tasks:
            exr = [f for f in t.files if f.endswith(".exr")][0]
            post["tiles"].append(dict(path=exr, **t.payload["tile"]))
        pj = os.path.join(workdir, "post.json")
        with open(pj, "w", encoding="utf-8") as f:
            json.dump(post, f, ensure_ascii=False)
        job.log("сборка кадра и денойз на головном…")
        cmd = [blender_bin, "-b", blend, "-Y", "--python", POST_SCRIPT, "--", pj]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", stdin=subprocess.DEVNULL)
        if post_log:
            post_log(res.stdout)
        err = [l[10:] for l in res.stdout.splitlines() if l.startswith("CDR_ERROR ")]
        if res.returncode != 0 or err or not os.path.exists(out_final):
            tail = " | ".join(res.stdout.strip().splitlines()[-4:])
            raise RuntimeError(err[0] if err else f"код {res.returncode}: {tail}")
        job.result = {"final": out_final, "exr": post["out_exr"]}

    return Job(blend, nodes, tasks, workdir, "tile", key=key, steal=True,
               finalize=finalize, log=log)


def build_anim_job(blend, nodes, frames, dest_for_frame, workdir, distribution="AUTO",
                   node_frames=None, chunk=1, steal=True, key="", device=None, log=print):
    """distribution: AUTO — общая очередь; WEIGHTED — по весам узлов непрерывными кусками;
    MANUAL — node_frames[i] = список кадров узла i (остальные кадры — в общую очередь).
    dest_for_frame(frame, ext) → путь, куда головной кладёт кадр."""
    chunk = max(1, int(chunk))
    assign = {}
    if distribution == "WEIGHTED":
        parts = split_weighted(frames, [max(0.0, n.weight) for n in nodes])
        for i, part in enumerate(parts):
            for f in part:
                assign[f] = i
    elif distribution == "MANUAL" and node_frames:
        allowed = set(frames)
        for i, lst in enumerate(node_frames):
            for f in lst or []:
                if f in allowed and f not in assign:
                    assign[f] = i
    tasks = []
    # кадры подряд одного узла объединяем в пачки по chunk
    groups, cur = [], []
    for f in frames:
        if cur and (assign.get(f) != assign.get(cur[-1]) or len(cur) >= chunk):
            groups.append(cur)
            cur = []
        cur.append(f)
    if cur:
        groups.append(cur)
    for i, g in enumerate(groups):
        label = f"кадр {g[0]}" if len(g) == 1 else f"кадры {g[0]}–{g[-1]}"
        payload = {"mode": "frames", "frames": g, "label": label}
        if device:
            payload["device"] = device
        tasks.append(Task(i, "frames", payload, label, frames=g, pref=assign.get(g[0])))

    def on_file(job, task, path):
        name = os.path.basename(path)
        stem, ext = os.path.splitext(name)
        try:
            frame = int(stem.rsplit("_", 1)[-1])
        except ValueError:
            return path
        dst = dest_for_frame(frame, ext)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(path, dst)
        job.log(f"кадр {frame} сохранён: {dst}")
        return dst

    return Job(blend, nodes, tasks, workdir, "anim", key=key, steal=steal, on_file=on_file, log=log)

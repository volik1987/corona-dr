"""Узел сетевого рендера: TCP-сервер заданий + ответчик UDP-поиска.
Рендер идёт в отдельном процессе `blender -b`, поэтому UI Blender не блокируется.
Модуль не зависит от bpy: запускается и внутри Blender (поток), и из node_cli.py."""
import json
import os
import platform
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid

try:
    from . import protocol as P
except ImportError:  # запуск как отдельный модуль
    import protocol as P

HERE = os.path.dirname(os.path.abspath(__file__))
RENDER_SCRIPT = os.path.join(HERE, "node_render.py")
SAMPLE_RE = re.compile(r"Sample (\d+)/(\d+)")
TILE_RE = re.compile(r"Rendered (\d+)/(\d+) Tiles")


class NodeServer:
    def __init__(self, blender_bin, port=P.DEFAULT_PORT, name=None, cache_dir=None,
                 threads=0, key="", log=print, keep_blends=4):
        self.blender_bin = blender_bin
        self.port = int(port)
        self.name = name or platform.node().split(".")[0] or "узел"
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".corona_dr_cache", str(self.port))
        os.makedirs(self.cache_dir, exist_ok=True)
        self.threads = int(threads)
        self.key = key or ""
        self.log = log
        self.keep_blends = keep_blends
        self.uid = uuid.uuid4().hex[:12]
        self.busy = False
        self.status = "ожидание"
        self.progress = 0.0
        self.jobs_done = 0
        # предпросмотр: копия последнего готового файла (часть кадра или кадр)
        self.preview_enabled = False
        self.preview_path = None
        self.preview_label = ""
        self.preview_seq = 0
        self._render_lock = threading.Lock()
        self._proc = None
        self._stop = threading.Event()
        self._tcp = None
        self._udp = None
        self._threads = []

    # ------------------------------------------------------------ запуск/стоп
    def start(self):
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", self.port))
        self._tcp.listen(8)
        self._udp = self._make_udp()
        for target in (self._accept_loop, self._discovery_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        self.log(f"[узел {self.name}] слушаю TCP {self.port}, поиск UDP {P.DISCOVERY_PORT}")
        return self

    def _make_udp(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        u.bind(("", P.DISCOVERY_PORT))
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(P.MCAST_GROUP), socket.inet_aton("0.0.0.0"))
            u.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        u.settimeout(0.5)
        return u

    def stop(self):
        self._stop.set()
        self.kill_render()
        for s in (self._tcp, self._udp):
            try:
                s.close()
            except Exception:
                pass
        self.log(f"[узел {self.name}] остановлен")

    @property
    def running(self):
        return not self._stop.is_set() and self._tcp is not None

    def info(self):
        return {
            "magic": "corona_dr", "version": P.VERSION, "uid": self.uid,
            "name": self.name, "port": self.port, "cores": os.cpu_count() or 1,
            "busy": self.busy, "status": self.status, "progress": self.progress,
            "blender": self._blender_version(), "os": platform.system(),
            "need_key": bool(self.key),
        }

    _bv = None

    def _blender_version(self):
        if NodeServer._bv is None:
            try:
                import bpy  # внутри Blender
                NodeServer._bv = bpy.app.version_string
            except Exception:
                NodeServer._bv = "?"
        return NodeServer._bv

    # ------------------------------------------------------------ поиск
    def _discovery_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() == P.DISCOVER_REQ:
                try:
                    self._udp.sendto(json.dumps(self.info()).encode("utf-8"), addr)
                except OSError:
                    pass

    # ------------------------------------------------------------ TCP
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except OSError:
                break
            t = threading.Thread(target=self._serve, args=(conn, addr), daemon=True)
            t.start()

    def _serve(self, conn, addr):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        authed = not self.key
        try:
            while not self._stop.is_set():
                hdr, payload = self._recv(conn)
                cmd = hdr.get("cmd")
                if cmd == "hello":
                    if self.key and hdr.get("key") != self.key:
                        P.send_msg(conn, {"type": "error", "error": "неверный ключ узла"})
                        return
                    authed = True
                    P.send_msg(conn, {"type": "info", **self.info()})
                    continue
                if not authed:
                    P.send_msg(conn, {"type": "error", "error": "сначала hello с ключом"})
                    return
                if cmd == "has_blend":
                    P.send_msg(conn, {"type": "has_blend",
                                      "have": os.path.exists(self._blend_path(hdr["sha"]))})
                elif cmd == "blend":
                    # данные уже сохранены в _recv (save_to)
                    P.send_msg(conn, {"type": "ok"})
                elif cmd == "render":
                    self._handle_render(conn, hdr)
                elif cmd == "cancel":
                    self.kill_render()
                    P.send_msg(conn, {"type": "ok"})
                elif cmd == "bye":
                    return
                else:
                    P.send_msg(conn, {"type": "error", "error": f"неизвестная команда {cmd}"})
        except (ConnectionError, OSError, ValueError, P.ProtocolError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _recv(self, conn):
        # Заголовок читаем отдельно, чтобы знать, куда сохранять файл .blend
        magic, jlen, size = P._HDR.unpack(P._recv_exact(conn, P._HDR.size))
        if magic != P.MAGIC:
            raise P.ProtocolError("чужой протокол")
        hdr = json.loads(P._recv_exact(conn, jlen).decode("utf-8"))
        payload = None
        if size:
            if hdr.get("cmd") == "blend":
                dst = self._blend_path(hdr["sha"])
                tmp = dst + ".part"
                got = 0
                with open(tmp, "wb") as f:
                    while got < size:
                        part = conn.recv(min(size - got, P.CHUNK))
                        if not part:
                            raise ConnectionError("обрыв при приёме сцены")
                        f.write(part)
                        got += len(part)
                os.replace(tmp, dst)
                self._prune_cache()
                payload = dst
            else:
                payload = P._recv_exact(conn, size)
        return hdr, payload

    def _blend_path(self, sha):
        safe = re.sub(r"[^0-9a-f]", "", sha)[:40] or "x"
        return os.path.join(self.cache_dir, f"job_{safe}.blend")

    def _prune_cache(self):
        blends = sorted((os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir)
                         if f.endswith(".blend")), key=os.path.getmtime, reverse=True)
        for old in blends[self.keep_blends:]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ------------------------------------------------------------ рендер
    def kill_render(self):
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _handle_render(self, conn, hdr):
        blend = self._blend_path(hdr["sha"])
        if not os.path.exists(blend):
            P.send_msg(conn, {"type": "error", "error": "нет сцены, сначала отправьте .blend"})
            return
        if not self._render_lock.acquire(blocking=False):
            P.send_msg(conn, {"type": "error", "error": "узел занят"})
            return
        task = hdr["task"]
        out_dir = os.path.join(self.cache_dir, "out_" + uuid.uuid4().hex[:8])
        os.makedirs(out_dir, exist_ok=True)
        try:
            self.busy = True
            self.progress = 0.0
            self.status = f"рендер: {task.get('label', '')}"
            task = dict(task, out_dir=out_dir)
            task_path = os.path.join(out_dir, "task.json")
            with open(task_path, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False)
            cmd = [self.blender_bin, "-b", blend, "-Y"]
            if self.threads > 0:
                cmd += ["-t", str(self.threads)]
            cmd += ["--python", RENDER_SCRIPT, "--", task_path]
            ok, files, err = self._run(conn, cmd)
            if not ok:
                P.send_msg(conn, {"type": "error", "error": err or "рендер не удался"})
                return
            for path in files:
                P.send_msg(conn, {"type": "file", "name": os.path.basename(path)}, file_path=path)
            P.send_msg(conn, {"type": "done", "count": len(files)})
            self.jobs_done += 1
            if self.preview_enabled and files:
                self._keep_preview(files[-1], task.get("label", ""))
        finally:
            self.busy = False
            self.status = "ожидание"
            self.progress = 0.0
            self._render_lock.release()
            shutil.rmtree(out_dir, ignore_errors=True)

    def _keep_preview(self, path, label):
        try:
            pdir = os.path.join(self.cache_dir, "preview")
            os.makedirs(pdir, exist_ok=True)
            dst = os.path.join(pdir, "last" + os.path.splitext(path)[1].lower())
            shutil.copyfile(path, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
            self.preview_path, self.preview_label = dst, label
            self.preview_seq += 1
        except OSError as e:
            self.log(f"[узел {self.name}] предпросмотр не сохранён: {e}")

    def _run(self, conn, cmd):
        lines = queue.Queue()
        self._proc = proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)

        def reader():
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        files, tail, eof = [], [], False
        last_sent, last_beat = -1.0, 0.0
        err = None
        while True:
            # головной отключился или прислал cancel?
            r, _, _ = select.select([conn], [], [], 0)
            if r:
                try:
                    peek = conn.recv(1, socket.MSG_PEEK)
                except OSError:
                    peek = b""
                if not peek:
                    proc.kill()
                    raise ConnectionError("головной отключился")
                hdr, _ = self._recv(conn)
                if hdr.get("cmd") == "cancel":
                    proc.kill()
                    err = "отменено"
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                line = ""
            if line is None:
                eof = True
            elif line:
                tail = (tail + [line])[-25:]
                if line.startswith("CDR_FILE "):
                    files.append(line[9:].strip())
                elif line.startswith("CDR_ERROR "):
                    err = line[10:].strip()
                else:
                    m = SAMPLE_RE.search(line)
                    if m:
                        a, b = int(m.group(1)), int(m.group(2))
                        self.progress = a / max(b, 1)
            now = time.time()
            if (abs(self.progress - last_sent) > 0.01 and now - last_beat > 0.2) or now - last_beat > 2.0:
                P.send_msg(conn, {"type": "progress", "progress": self.progress})
                last_sent, last_beat = self.progress, now
            if eof and proc.poll() is not None:
                break
            if eof:
                proc.wait(timeout=10)
                break
        code = proc.returncode
        self._proc = None
        if code != 0 and not err:
            err = f"blender завершился с кодом {code}: " + " | ".join(tail[-5:])
        if err:
            self.log(f"[узел {self.name}] ошибка: {err}")
            return False, [], err
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return False, [], "рендер не создал файлов: " + " | ".join(tail[-5:])
        return True, files, None

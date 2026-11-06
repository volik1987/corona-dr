"""Протокол сетевого рендера: TCP-сообщения «заголовок JSON + двоичные данные»
и UDP-поиск узлов. Модуль не зависит от bpy — его используют и узел, и головной.

Кадр сообщения:  b'CDR1' | uint32 длина JSON | uint64 длина данных | JSON | данные
"""
import json
import os
import socket
import struct

MAGIC = b"CDR1"
VERSION = 1
DEFAULT_PORT = 19400          # TCP-порт узла по умолчанию
DISCOVERY_PORT = 19401        # UDP-порт поиска узлов
DISCOVER_REQ = b"CORONA_DR_DISCOVER"
MCAST_GROUP = "239.77.19.40"  # дублируем поиск мультикастом (доходит и до узлов на этой же машине)
_HDR = struct.Struct("!4sIQ")
CHUNK = 1 << 20
IO_TIMEOUT = 60.0             # сколько ждать данных посреди сообщения (обрыв Wi-Fi, выключенный ПК)

# Windows: без этого каждый запуск blender.exe из GUI открывает окно консоли
POPEN_KW = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


def set_keepalive(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


class ProtocolError(Exception):
    pass


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(min(n - len(buf), CHUNK))
        if not part:
            raise ConnectionError("соединение закрыто")
        buf += part
    return bytes(buf)


def send_msg(sock, header, payload=None, file_path=None):
    """Отправить сообщение. Данные — либо bytes (payload), либо файл (file_path)."""
    size = 0
    if payload is not None:
        size = len(payload)
    elif file_path is not None:
        size = os.path.getsize(file_path)
    data = json.dumps(header, ensure_ascii=False).encode("utf-8")
    sock.sendall(_HDR.pack(MAGIC, len(data), size) + data)
    if payload is not None:
        sock.sendall(payload)
    elif file_path is not None:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                sock.sendall(chunk)


def recv_msg(sock, save_to=None, progress=None):
    """Принять сообщение. Если save_to задан и есть данные — пишем их в файл
    (сначала во временный, потом переименовываем), иначе возвращаем bytes."""
    magic, jlen, size = _HDR.unpack(_recv_exact(sock, _HDR.size))
    if magic != MAGIC:
        raise ProtocolError("чужой протокол")
    header = json.loads(_recv_exact(sock, jlen).decode("utf-8"))
    payload = None
    if size:
        if save_to:
            tmp = save_to + ".part"
            got = 0
            with open(tmp, "wb") as f:
                while got < size:
                    part = sock.recv(min(size - got, CHUNK))
                    if not part:
                        raise ConnectionError("соединение закрыто при приёме файла")
                    f.write(part)
                    got += len(part)
                    if progress:
                        progress(got, size)
            os.replace(tmp, save_to)
            payload = save_to
        else:
            payload = _recv_exact(sock, size)
    return header, payload


def connect(host, port, timeout=5.0):
    s = socket.create_connection((host, port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    set_keepalive(s)
    s.settimeout(IO_TIMEOUT)
    return s


def file_sha1(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def local_broadcast_addrs():
    """Широковещательные адреса: общий + по подсетям /24 локальных IP (без netifaces)."""
    addrs = {"255.255.255.255"}
    for ip in local_ips():
        if ip.startswith("127."):
            continue
        parts = ip.split(".")
        if len(parts) == 4:
            addrs.add(".".join(parts[:3] + ["255"]))
    return sorted(addrs)


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # IP основного интерфейса (пакет никуда не уходит)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.add("127.0.0.1")
    return sorted(ips)


def discover(timeout=1.5, port=DISCOVERY_PORT, extra_hosts=()):
    """Разослать запрос поиска и собрать ответы узлов.
    Возвращает список dict: name, host, port, uid, cores, busy, blender, version."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    s.bind(("", 0))
    targets = list(local_broadcast_addrs()) + [MCAST_GROUP] + list(extra_hosts)
    for addr in targets:
        try:
            s.sendto(DISCOVER_REQ, (addr, port))
        except OSError:
            pass
    found = {}
    import time
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            break
        s.settimeout(left)
        try:
            data, (ip, _p) = s.recvfrom(65536)
        except ConnectionResetError:   # Windows: ICMP «порт недоступен» от прошлой рассылки
            continue
        except (socket.timeout, OSError):
            break
        try:
            info = json.loads(data.decode("utf-8"))
        except ValueError:
            continue
        if info.get("magic") != "corona_dr":
            continue
        info["host"] = ip
        key = (info.get("uid"), info.get("port"))
        prev = found.get(key)
        # один и тот же узел может ответить через несколько интерфейсов:
        # предпочитаем не-loopback адрес (его увидят и другие машины)
        if prev is None or (prev["host"].startswith("127.") and not ip.startswith("127.")):
            found[key] = info
    s.close()
    return list(found.values())

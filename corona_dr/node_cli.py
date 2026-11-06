"""Узел рендера без интерфейса (для «фермы»):

    blender -b --factory-startup --python node_cli.py -- --port 19400 --name ПК-2

Параметры: --port, --name, --cache <папка>, --threads N, --key <общий ключ>.
Работает, пока процесс не остановят (Ctrl+C)."""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "dr"))
import protocol  # noqa: E402
import node  # noqa: E402


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="corona_dr node")
    ap.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    ap.add_argument("--name", default=None)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--key", default="")
    ap.add_argument("--blender", default=None, help="путь к Blender для рендера")
    a = ap.parse_args(argv)
    try:
        import bpy
        blender = a.blender or bpy.app.binary_path
    except ImportError:
        blender = a.blender or "blender"
    srv = node.NodeServer(blender, port=a.port, name=a.name, cache_dir=a.cache,
                          threads=a.threads, key=a.key,
                          log=lambda m: print(m, flush=True)).start()
    print("CDR_NODE_READY", a.port, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


main()

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import create_source, create_destination, probe_source
from .config import Config, ConfigError
from .model import BackupError
from .service import Service
from .store import Store, worker_lock


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="媒体自动导入助手")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=os.environ.get("MEDIA_IMPORTER_CONFIG", "/config/settings.json"))
    parser.add_argument("--data", default=os.environ.get("MEDIA_IMPORTER_DATA_DIR", "/data"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "check", "scan", "status", "daemon", "health", "verify-cache"):
        sub = commands.add_parser(command)
        if command in ("check", "scan", "status"):
            sub.add_argument("--json", action="store_true")
    commands.add_parser("bot")
    commands.add_parser("bot-health")
    commands.add_parser("bot-panel")
    sync = commands.add_parser("sync")
    sync.add_argument("state", choices=("on", "off"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        if args.command == 'health':
            from .status import healthy
            return 0 if healthy(args.data) else 1
        if args.command in ("bot", "bot-health", "bot-panel"):
            from .bot import serve, healthy, Bot
            from .settings import Settings
            if args.command == "bot":
                serve(Settings(args.config), args.data)
            elif args.command == "bot-health":
                return 0 if healthy(args.data) else 1
            else:
                from .adapters.feishu import send_card
                bot = Bot(Settings(args.config), args.data)
                send_card(bot.credentials, bot.panel())
                print("控制面板已发送到本人飞书单聊")
            return 0
        if args.command == "sync":
            from .settings import Settings
            Settings(args.config).set_enabled(args.state == "on")
            print("已保存同步" + ("开启" if args.state == "on" else "关闭") + "设置；后台将在运行时应用")
            return 0
        if args.command == "daemon":
            from .runtime import daemon
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            with worker_lock(args.data) as acquired:
                if not acquired:
                    raise BackupError("已有工作进程使用此数据目录")
                daemon(args.config, args.data, stop)
            return 0
        if args.command == "status":
            from .settings import Settings
            from .status import snapshot
            result = snapshot(args.data, Settings(args.config))
            if args.json:
                emit(result)
            else:
                status, batch = result["status"], result["batch"]
                print("同步: " + ("已开启" if result["configuration"]["sync_enabled"] else "已关闭"))
                print("状态: " + status.get("message", "尚无记录"))
                print(f"导入: {batch['imported_files']} / {batch['total_files']} 个文件，{batch['import_percent']:.1f}%")
                print(f"归档: {batch['completed_files']} / {batch['total_files']} 个文件，{batch['percent']:.1f}%")
                for name, label in (("import", "相机导入"), ("upload", "目的地上传")):
                    progress = result["stages"][name]
                    if progress.get("current_file"):
                        print(f"{label}: {progress['current_file']} ({progress.get('current_bytes', 0)} / {progress.get('current_total_bytes', 0)} bytes)")
                print(result["unplug"]["message"])
                print("飞书发送：状态 / 设置 / 停止同步；或使用 docker compose exec backup python3 -m media_importer sync off")
            return 0
        if args.command == "check":
            from .preflight import run
            from .settings import Settings
            store = Store(args.data)
            try:
                from .storage import initialize_cache
                initialize_cache(store)
                result = run(Settings(args.config), store)
                emit(result)
                return 0 if result["state"] == "ok" else 2
            finally:
                store.close()
        config = Config.load(args.config)
        if args.command == "validate":
            print("配置有效；未连接相机或 目的地。")
            return 0
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        source = create_source(config, stop)
        if args.command == "scan":
            assets = source.snapshot()
            emit({"files": len(assets), "bytes": sum(a.size for a in assets), "assets": [a.to_dict() for a in assets]})
            return 0
        destination = create_destination(config, stop)
        try:
            if args.command == "verify-cache":
                # The worker must be stopped; this command cannot enable sync or delete.
                with worker_lock(args.data) as acquired:
                    if not acquired:
                        raise BackupError("请先停止传输服务，再运行缓存核验")
                    store = Store(args.data)
                    try:
                        result = Service(config, store, source, destination, stop).verify_caches()
                        emit(result)
                        return 0 if not result["pending"] else 2
                    finally:
                        store.close()
        finally:
            destination.close()
    except (ConfigError, BackupError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

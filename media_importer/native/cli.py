"""User-facing lifecycle commands; workers use the unchanged core entry point."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .. import __version__
from ..locking import file_lock
from . import lifecycle, platforms, releases


def supervise(home):
    record = lifecycle.load(home)
    lifecycle.save(home / 'startup.json', {'phase': 'checking_paths', 'observed_at': time.time(),
        'message': '正在访问配置和缓存盘；macOS 首次启动或更新后可能需要允许访问可移动卷'})
    lifecycle.assert_paths(record); lifecycle.assert_no_docker(record)
    marker = Path(record['paths']['data']) / 'deployment.json'
    if not marker.exists() or json.loads(marker.read_text()) != {'mode': 'native', 'home': str(home)}:
        raise ValueError('原生数据目录未登记到此安装，拒绝启动')
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    logger = logging.getLogger('native-workers'); logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(home / 'logs/service.log', maxBytes=5 * 1024 ** 2, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    lifecycle.save(home / 'startup.json', {'phase': 'starting_workers', 'observed_at': time.time()})
    processes = {}
    retry = {}
    stop_file = home / ('stop-' + uuid.uuid4().hex)
    env = {**lifecycle.runtime_env(record), 'MEDIA_IMPORTER_STOP_FILE': str(stop_file)}
    def log_output(process, role):
        for line in process.stdout:
            logger.info('%s %s', role, line.rstrip())
    with file_lock(home / 'service.lock', False) as free:
        if not free:
            raise RuntimeError('此原生后台服务已经运行')
        try:
            while not stop.is_set() and not (home / 'stopped').exists():
                # A detached drive must not be replaced by a new empty directory.
                available = all(Path(record['paths'][k]).is_dir() for k in ('data', 'config', 'cache')) and Path(record['paths']['cache_volume']).is_mount()
                for role in ('daemon', 'bot'):
                    process = processes.get(role)
                    if process and process.poll() is not None:
                        logger.warning('%s exited (%s); retry in 30 seconds', role, process.returncode)
                        retry[role] = time.monotonic() + 30; processes.pop(role)
                    if role not in processes and available and time.monotonic() >= retry.get(role, 0):
                        process = subprocess.Popen([str(releases.executable(Path(record['bundle']))), '_worker', role],
                            cwd=home, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace',
                            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
                        processes[role] = process
                        threading.Thread(target=log_output, args=(process, role), daemon=True).start()
                lifecycle.save(home / 'running.json', {'version': record['version'], 'pid': os.getpid(),
                    'observed_at': time.time(), 'waiting_for_disk': not available,
                    'workers': {role: bool(processes.get(role) and processes[role].poll() is None) for role in ('daemon', 'bot')}})
                stop.wait(2)
        finally:
            stop_file.write_text('stop\n')
            # The importer observes this file and cancels both transfers normally.
            # The SDK bot has no synchronous close; SIGTERM/terminate is safe for
            # its short SQLite transactions and independent of media writes.
            bot = processes.get('bot')
            if bot and bot.poll() is None:
                bot.terminate()
            deadline = time.monotonic() + 45
            for process in processes.values():
                try:
                    process.wait(timeout=max(1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            (home / 'running.json').unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)
            handler.close(); logger.removeHandler(handler)


def describe(home):
    record = lifecycle.load(home)
    result = {'deployment': 'native', 'version': record['version'], 'project': record['project'],
              'paths': {k: record['paths'][k] for k in ('config', 'data', 'cache')},
              'service': {'running': False}, 'health': {'importer': False, 'bot': False}}
    if (home / 'startup.json').is_file():
        result['startup'] = json.loads((home / 'startup.json').read_text())
    try:
        state = json.loads((home / 'running.json').read_text())
        result['service'] = {**state, 'running': time.time() - state['observed_at'] < 15}
    except (OSError, ValueError, KeyError):
        pass
    with lifecycle.environment(record):
        from ..status import healthy
        from ..bot import healthy as bot_healthy
        result['health'] = {'importer': healthy(record['paths']['data']), 'bot': bot_healthy(record['paths']['data'])}
    return result


def main(argv=None):
    # Bundled Python needs its own CA store, independent of Docker and Homebrew.
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    parser = argparse.ArgumentParser(description='素材导入助手：原生部署、Docker 共用传输核心')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MEDIA_IMPORTER_HOME', platforms.default_home())))
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install', help='安装原生后台服务；已有配置与缓存保留')
    install.add_argument('--project', type=Path, help='接管已有项目的 .env 和宿主机数据目录；须先停止 Docker')
    install.add_argument('--bundle', type=Path, help='已解压的本机安装包；默认当前程序目录')
    install.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    install.add_argument('--no-start', action='store_true', help='只安装，不启动后台服务')
    update = commands.add_parser('update', help='升级正式发行版；保留配置、任务及缓存')
    update.add_argument('--version', dest='selected')
    update.add_argument('--bundle', type=Path, help='离线升级：已解压的本机安装包')
    commands.add_parser('uninstall', help='移除后台服务与程序；保留全部配置、任务、缓存与备份')
    for name in ('start', 'stop', 'check', 'logs'):
        commands.add_parser(name)
    commands.add_parser('status').add_argument('--json', action='store_true')
    commands.add_parser('run', help='前台运行；供系统后台服务调用')
    worker = commands.add_parser('_worker', help='内部工作进程，由后台服务调用')
    worker.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == '_worker':
            from ..__main__ import main as core
            return core(args.args)
        home = args.home.expanduser().resolve()
        if args.command == 'install':
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not home.is_dir():
            raise ValueError('此位置未安装原生版，请先执行 install')
        if args.command == 'run':
            supervise(home); return 0
        if args.command == 'status':
            result = describe(home)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"原生版 {result['version']} · 后台" + ('运行中' if result['service']['running'] else '已停止'))
                print('传输服务：' + ('正常' if result['health']['importer'] else '等待或未启动') +
                      '；飞书连接：' + ('正常' if result['health']['bot'] else '等待或未连接'))
                for k, value in result['paths'].items():
                    print(k + ': ' + value)
                print('传输详情与同步开关请在飞书查看；诊断可运行 media-importer check')
            return 0
        if args.command == 'logs':
            path = home / 'logs/service.log'
            if path.exists():
                with path.open('rb') as handle:
                    handle.seek(max(0, path.stat().st_size - 32000))
                    print(handle.read().decode('utf-8', errors='replace'))
            return 0
        if args.command == 'check':
            record = lifecycle.load(home)
            lifecycle.assert_paths(record)
            lifecycle.assert_no_docker(record)
            with lifecycle.environment(record):
                from ..__main__ import main as core
                return core(['check', '--json'])
        with file_lock(home / 'install.lock', False) as free:
            if not free:
                raise RuntimeError('已有安装或维护操作正在运行')
            if args.command == 'install':
                bundle = (args.bundle or Path(sys.executable).parent).resolve()
                if not args.bundle and not getattr(sys, 'frozen', False):
                    raise ValueError('从源码运行须指定 --bundle；请先执行 scripts/build_native.py')
                record = lifecycle.install(home, bundle, args.project, args.bin_dir.expanduser().resolve(), no_start=args.no_start)
                print('原生安装完成：' + record['command'])
                print('配置、任务和缓存：' + record['project'])
                if str(Path(record['command']).parent) not in os.environ.get('PATH', '').split(os.pathsep):
                    print('请将命令所在目录加入 PATH，或使用以上完整路径。')
            elif args.command == 'update':
                if args.bundle and args.selected:
                    raise ValueError('--bundle 与 --version 不能同时指定')
                with tempfile.TemporaryDirectory(prefix='update-', dir=home) as temporary:
                    bundle = args.bundle.resolve() if args.bundle else releases.download_release(Path(temporary), args.selected)
                    record, changed = lifecycle.update(home, bundle)
                print(('已更新到 ' if changed else '已是此版本，无需重启：') + record['version'])
            elif args.command == 'uninstall':
                record, retained = lifecycle.uninstall(home)
                print('已卸载原生服务与命令；配置、任务、完整缓存与备份保留。')
                for key in ('config', 'data', 'cache'):
                    print(key + ': ' + record['paths'][key])
                if retained:
                    print('本次正在执行的程序文件仍被系统占用，退出后可移除：' + ', '.join(retained))
            elif args.command == 'start':
                if describe(home)['service']['running']:
                    print('后台服务已运行')
                else:
                    lifecycle.start(home, lifecycle.load(home)); print('后台服务已启动；同步按现有设置运行')
            elif args.command == 'stop':
                lifecycle.stop(home); print('后台服务已停止；配置、任务和完整缓存保留')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Network responses and subprocess stderr may contain credential-bearing
        # URLs. Only our own ValueError/RuntimeError messages are user-facing.
        print('操作未完成：' + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else
              '网络、文件或系统服务操作失败，请检查磁盘、网络与运行日志；原数据保留'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

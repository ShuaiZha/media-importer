#!/usr/bin/env python3
"""First-install wizard: Python stdlib on the host, connection checks in Docker."""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def literal(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
        raise ValueError('配置值不能包含换行或空字符')
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def ask(label, default='', secret=False, read=input, hidden=getpass.getpass, required=True):
    while True:
        value = (hidden if secret else read)(label + (f' [{default}]' if default else '') + '：')
        value = value if secret else value.strip()
        value = value or default
        if (value or not required) and not any(c in value for c in '\r\n\x00'):
            return value
        print('请填写有效值。')


def camera_identity(url):
    from media_importer.adapters import source_defaults, probe_source
    from media_importer.model import SourceError
    try:
        return probe_source(source_defaults()['type'], url)['serial_number']
    except (SourceError, OSError, KeyError, TypeError):
        return None


def cache_directory(value, root):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    # Never create a replacement directory on the Mac's internal disk when an SSD is absent.
    if sys.platform == 'darwin' and path.parts[:2] == ('/', 'Volumes'):
        if len(path.parts) < 4 or not Path(*path.parts[:3]).is_mount():
            raise ValueError('外接磁盘未挂载，请先连接磁盘；未创建替代目录')
    if path.is_symlink():
        raise ValueError('请填写缓存目录的真实路径，不使用符号链接')
    path = path.resolve()
    if (path / '.media-importer-cache-id').exists():
        raise ValueError('该目录已绑定其他安装；迁移请使用备份恢复，不重新初始化')
    return path


def write_new(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def save(root, cache, values):
    env, settings = root / '.env', root / 'config/settings.json'
    if env.exists() or env.is_symlink() or settings.exists() or settings.is_symlink() or (root / 'data/state.sqlite3').exists():
        raise ValueError('已有配置或任务记录，向导不会覆盖。修改连接请通过飞书设置，迁移请使用 manage.py restore')
    for path in (root / 'config', root / 'data', cache):
        path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.setup-check-', dir=cache) as probe:
        probe.write(b'media-importer\n')
        probe.flush()
        os.fsync(probe.fileno())
    free = shutil.disk_usage(cache).free
    print(f'缓存目录可读写，可用 {free / 1024 ** 3:.1f} GiB；默认保留 10 GiB。')
    mounts = {'MEDIA_IMPORTER_CONFIG_HOST_DIR': (root / 'config').as_posix(),
              'MEDIA_IMPORTER_DATA_HOST_DIR': (root / 'data').as_posix(),
              'MEDIA_IMPORTER_CACHE_HOST_DIR': cache.as_posix()}
    from media_importer.config_upgrade import convert
    from media_importer.credentials import stage
    runtime, credential = convert({'version': 4}, settings.parent, values)
    runtime['backup'].update(enabled=False, delete_source_after_import=False, stop_after_current=False)
    runtime['notifications']['enabled'] = False
    local = {key: value for key, value in values.items() if not key.startswith(('SOURCE_', 'DESTINATION_')) and key != 'PREVIEW_PUBLIC_URL'}
    document = '# 本机部署与飞书凭据；业务配置保存在 config/settings.json。不要提交到 Git。\n' + ''.join(
        key + '=' + literal(value) + '\n' for key, value in {**mounts, **local}.items())
    write_new(env, document)
    try:
        if credential:
            stage(settings.parent, *credential)
        write_new(settings, json.dumps(runtime, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        env.unlink()  # Only the file exclusively created by this invocation.
        raise


def docker(args, root):
    try:
        result = subprocess.run(['docker', *args], cwd=root, capture_output=True, text=True)
    except OSError:
        raise RuntimeError('无法运行 Docker，请安装并启动 Docker Desktop / Docker Engine') from None
    if result.returncode not in (0, 2):
        raise RuntimeError('Docker 操作失败，请检查 Docker、网络与目录共享权限；配置已保留')
    return result


def check(root):
    print('正在检查 Compose 并下载稳定版镜像…', flush=True)
    if docker(['compose', 'config', '--quiet'], root).returncode:
        raise RuntimeError('Compose 配置未通过，请检查 .env')
    if docker(['compose', 'pull'], root).returncode:
        raise RuntimeError('镜像下载失败，请检查 ghcr.io 连通性；配置已保留')
    for args in (['compose', 'run', '--rm', '--no-deps', '-T', 'backup', 'validate'],):
        if docker(args, root).returncode:
            raise RuntimeError('配置检查未通过，请检查 .env 和部署说明；未启动同步')
    result = docker(['compose', 'run', '--rm', '--no-deps', '-T', 'backup', 'check', '--json'], root)
    try:
        checks = json.loads(result.stdout)['checks']
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('未取得有效检查结果，请按部署说明运行 check --json；配置已保留') from None
    labels = {'configuration': '配置', 'database': '任务记录', 'cache': '缓存盘',
              'source': '相机', 'destination': 'SMB', 'notification': '飞书应用'}
    for name, item in checks.items():
        print(f"{labels.get(name, name)} · {item['state']}：{item['message']}")
    return result.returncode


def setup(root, *, read=input, hidden=getpass.getpass, probe=camera_identity, checks=check, native=False):
    root = root.resolve()
    if (root / '.env').exists() or (root / '.env').is_symlink() or (root / 'config/settings.json').exists() or (root / 'data/state.sqlite3').exists():
        raise ValueError('检测到已有安装，向导不会覆盖配置或记录；修改连接请通过飞书设置')
    print('首次配置：连接相机、选择缓存盘、填写 SMB 和飞书应用。密码输入时不回显。')
    from media_importer.adapters import source_defaults, validate_source
    source = source_defaults()
    values = {'SOURCE_TYPE': source['type'], 'DESTINATION_TYPE': 'smb',
              'SOURCE_TIMEOUT_SECONDS': str(source['timeout_seconds'])}
    values['SOURCE_URL'] = ask('相机 HTTP 地址（可留空，启动后自动发现）', read=read, required=False)
    serial = probe(values['SOURCE_URL']) if values['SOURCE_URL'] else None
    if serial:
        print('已读取相机序列号：' + serial)
    values['SOURCE_SERIAL'] = serial or (ask('相机序列号（可留空）', read=read, required=False) if values['SOURCE_URL'] else '')
    selected = {**source, 'url': values['SOURCE_URL'], 'serial': values['SOURCE_SERIAL']}
    validate_source(selected)
    values.update(SOURCE_URL=selected['url'], SOURCE_SERIAL=selected['serial'])
    cache = cache_directory(ask('本机缓存目录', './cache', read=read), root)
    for key, label, default, required in (
        ('DESTINATION_HOST', 'SMB 地址或域名（不含 smb://）', '', True),
        ('DESTINATION_PORT', 'SMB 端口', '445', True),
        ('DESTINATION_SHARE', 'SMB 共享名', '', True),
        ('DESTINATION_DIRECTORY', '共享内子目录（可留空，需已存在）', '', False),
        ('DESTINATION_USERNAME', 'SMB 用户名', '', True),
        ('DESTINATION_DOMAIN', 'SMB 域', 'WORKGROUP', True)):
        values[key] = ask(label, default, read=read, required=required)
    if not values['DESTINATION_PORT'].isdigit() or not 1 <= int(values['DESTINATION_PORT']) <= 65535:
        raise ValueError('SMB 端口必须是 1–65535 的整数')
    if any(c in values['DESTINATION_HOST'] for c in '/\\'):
        raise ValueError('SMB 地址不能包含协议或路径')
    values['DESTINATION_NAME'] = 'SMB 存储'
    values['DESTINATION_PASSWORD'] = ask('SMB 密码', secret=True, hidden=hidden)
    print('飞书自建应用的创建步骤见 docs/feishu.md；此处只保存凭据，不会发布应用或发送消息。')
    values['FEISHU_APP_ID'] = ask('飞书 App ID', read=read)
    values['FEISHU_APP_SECRET'] = ask('飞书 App Secret', secret=True, hidden=hidden)
    values['FEISHU_OWNER_OPEN_ID'] = ask('本人在此应用中的 Open ID', read=read)
    save(root, cache, values)
    print('已生成 .env 和 config/settings.json；同步、原片删除和传输通知默认关闭。启动后识别 USB 相机，在飞书确认后备份。', flush=True)
    code = checks(root) if checks else 0
    if native:
        print('配置已保存；原生服务启动后可运行 media-importer check 检查各端连接。')
    else:
        print('检查结束。处理等待项后执行 docker compose up -d，再按 docs/feishu.md 完成事件、菜单和发布。' if checks else
              '尚未检查连接；请按 docs/deployment.md 下载镜像并运行 check --json。')
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description='素材导入助手首次配置向导；不会覆盖现有安装')
    parser.add_argument('--no-check', action='store_true', help='只生成配置，暂不构建镜像或检查远端连接')
    args = parser.parse_args(argv)
    try:
        return setup(ROOT, checks=None if args.no_check else check)
    except (OSError, ValueError, RuntimeError) as exc:
        print('配置未完成：' + str(exc), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('\n配置已取消。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

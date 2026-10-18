"""Native release bundles contain program files only, never installation data."""
import hashlib
import json
import platform
from pathlib import Path
import re
import sys
import tarfile
import urllib.request
import urllib.error

REPOSITORY = 'ShuaiZha/media-importer'
BASE = 'https://github.com/' + REPOSITORY + '/releases'


def platform_key():
    system = {'darwin': 'macos', 'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64', 'AMD64': 'amd64'}.get(platform.machine())
    if not system or not arch:
        raise ValueError('当前系统或处理器架构尚无原生安装包，请使用 Docker 部署')
    return system + '-' + arch


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('版本必须是正式版本，例如 0.10.20')
    return tuple(map(int, value.split('.')))


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'media-importer-native'}), timeout=45)


def download_release(folder, selected=None):
    if selected:
        version(selected)
    prefix = BASE + ('/download/v' + selected if selected else '/latest/download')
    try:
        with request(prefix + '/native-release.json') as response:
            raw = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError('该正式发行版尚无原生安装包，当前安装未更改；可使用 update --bundle 进行离线更新') from None
        raise
    if len(raw) > 256 * 1024:
        raise ValueError('发行清单过大')
    manifest = json.loads(raw)
    ver = manifest.get('version'); version(ver)
    if manifest.get('format') != 1 or selected and ver != selected:
        raise ValueError('不支持的原生发行清单')
    item = manifest.get('assets', {}).get(platform_key())
    if not item or not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')):
        raise ValueError('此发行版未提供当前系统的原生安装包')
    name = f'media-importer-{ver}-{platform_key()}.tar.gz'
    if item.get('name') != name:
        raise ValueError('原生安装包名称不匹配')
    target = folder / name
    digest = hashlib.sha256()
    total = 0
    with request(BASE + '/download/v' + ver + '/' + name) as response, target.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 1024 ** 3:
                raise ValueError('原生安装包超出大小限制')
            digest.update(chunk); output.write(chunk)
    if digest.hexdigest() != item['sha256']:
        raise ValueError('安装包校验失败，未更改现有安装')
    unpack(target, folder / 'unpacked')
    bundle = folder / 'unpacked/media-importer'
    if metadata(bundle)['version'] != ver:
        raise ValueError('安装包版本与发行清单不符')
    return bundle


def unpack(archive, target):
    target.mkdir()
    with tarfile.open(archive, 'r:gz') as handle:
        members = handle.getmembers()
        if len(members) > 50000 or sum(m.size for m in members) > 2 * 1024 ** 3:
            raise ValueError('安装包解压大小超出限制')
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'media-importer':
                raise ValueError('安装包包含无效路径')
        # PyInstaller macOS bundles legitimately contain internal relative links.
        # The Python data filter rejects escaping links, devices and unsafe modes.
        handle.extractall(target, filter='data')


def metadata(bundle):
    value = json.loads((bundle / 'bundle.json').read_text())
    version(value.get('version'))
    if (value.get('format') != 1 or value.get('platform') != platform_key() or
            value.get('config_version') != 4 or value.get('database_version') != 5):
        raise ValueError('安装包平台或数据格式不兼容')
    if not executable(bundle).is_file():
        raise ValueError('安装包缺少命令程序')
    return value


def executable(bundle):
    return bundle / ('media-importer.exe' if sys.platform == 'win32' else 'media-importer')

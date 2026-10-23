"""Pin and bundle the preview engine at build time; no runtime package manager."""
import hashlib
import io
from pathlib import Path
import shutil
import sys
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_importer.native.releases import platform_key

VERSION = 'v1.9.14'
ASSETS = {
    'macos-arm64': ('go2rtc_mac_arm64.zip', '919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c'),
    'macos-amd64': ('go2rtc_mac_amd64.zip', '9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a'),
}
LIMIT = 40 * 1024 ** 2


def unpack(payload, digest, target):
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError('go2rtc 组件校验失败，未打包')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        matches = [i for i in archive.infolist() if i.filename == 'go2rtc' and not i.is_dir()]
        if len(matches) != 1 or matches[0].file_size > LIMIT:
            raise ValueError('go2rtc 压缩包结构无效')
        content = archive.read(matches[0])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content); target.chmod(0o755)


def bundle(destination, key=None):
    key = key or platform_key()
    if key not in ASSETS:
        return None  # USB-only platforms have no unused preview executable.
    name, digest = ASSETS[key]
    cached = ROOT / '.build/preview-downloads' / name
    if not cached.is_file() or hashlib.sha256(cached.read_bytes()).hexdigest() != digest:
        url = f'https://github.com/AlexxIT/go2rtc/releases/download/{VERSION}/{name}'
        with urlopen(Request(url, headers={'User-Agent': 'media-importer-build'}), timeout=60) as response:
            payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('go2rtc 下载校验失败')
        cached.parent.mkdir(parents=True, exist_ok=True); cached.write_bytes(payload)
    target = Path(destination) / 'components/go2rtc'
    unpack(cached.read_bytes(), digest, target)
    shutil.copyfile(ROOT / 'docs/licenses/go2rtc-LICENSE', target.with_name('go2rtc-LICENSE'))
    import imageio_ffmpeg
    if imageio_ffmpeg.__version__ != '0.6.0':
        raise ValueError('请安装固定版本的原生构建依赖')
    # Select only the package's pinned binary, never an environment/PATH override.
    binaries = [p for p in (Path(imageio_ffmpeg.__file__).parent / 'binaries').glob('ffmpeg-*') if p.is_file()]
    if len(binaries) != 1:
        raise ValueError('安装包缺少固定的 FFmpeg 二进制组件')
    audio = target.with_name('ffmpeg')
    shutil.copyfile(binaries[0], audio); audio.chmod(0o755)
    for name in ('ffmpeg-NOTICE.md', 'ffmpeg-COPYING.GPLv3'):
        shutil.copyfile(ROOT / 'docs/licenses' / name, target.with_name(name))
    return {'go2rtc': VERSION, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'ffmpeg_bundle': 'imageio-ffmpeg-0.6.0', 'ffmpeg_sha256': hashlib.sha256(audio.read_bytes()).hexdigest()}


if __name__ == '__main__':
    print(bundle(ROOT / '.build'))

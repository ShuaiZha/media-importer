"""Build verified ARM64/AMD64 images without relying on Docker Hub mirrors."""
import argparse
import gzip
import hashlib
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path
from build import build_context

ROOT = Path(__file__).resolve().parents[1]
ALPINE = "3.24.2"
VERSION = "0.10.8"


def download(url, path):
    print("Download:", path.name, flush=True)
    if shutil.which("curl"):
        subprocess.run(["curl", "-fsSL", "--retry", "2", "--connect-timeout", "15", "--max-time", "300", url, "-o", str(path)], check=True)
    else:
        with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output)


def verify(path, digest):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    if result.hexdigest().lower() != digest.lower():
        raise RuntimeError("Official download checksum mismatch: " + path.name)


def run(args):
    subprocess.run(args, cwd=str(ROOT), check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["arm64", "amd64", "all"], default="all")
    parser.add_argument("--export", action="store_true", help="Export per-platform Docker load archives")
    parser.add_argument("--cached-downloads", action="store_true", help="Reuse cached official dependency archives and checksum lists; fail if missing")
    args = parser.parse_args()
    build = ROOT / ".build"
    build.mkdir(exist_ok=True)
    arches = ["arm64", "amd64"] if args.arch == "all" else [args.arch]
    for arch in arches:
        alpine_arch = "aarch64" if arch == "arm64" else "x86_64"
        rootfs = build / f"alpine-minirootfs-{ALPINE}-{alpine_arch}.tar.gz"
        base_url = f"https://dl-cdn.alpinelinux.org/alpine/v{'.'.join(ALPINE.split('.')[:2])}/releases/{alpine_arch}/"
        checksum_file = rootfs.with_name(rootfs.name + ".sha256")
        if args.cached_downloads:
            if not checksum_file.is_file():
                raise RuntimeError("Missing cached official Alpine checksum list")
        else:
            download(base_url + checksum_file.name, checksum_file)
        if not rootfs.exists():
            if args.cached_downloads:
                raise RuntimeError("Missing cached Alpine archive")
            download(base_url + rootfs.name, rootfs)
        verify(rootfs, checksum_file.read_text().split()[0])
        tag = f"local/media-importer-alpine:{ALPINE}-{arch}"
        exists = subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0
        if not exists:
            run(["docker", "import", "--platform", "linux/" + arch, str(rootfs), tag])
        image = f"local/media-importer:{VERSION}-{arch}"
        with build_context(ROOT) as context:
            run(["docker", "build", "--platform", "linux/" + arch, "--build-arg", "BASE_IMAGE=" + tag, "-t", image, str(context)])
        run(["docker", "tag", image, f"local/media-importer:{VERSION}"])
        if args.export:
            destination = ROOT / "dist" / f"media-importer-{VERSION}-{arch}.tar.gz"
            destination.parent.mkdir(exist_ok=True)
            with gzip.open(destination, "wb", compresslevel=1) as output:
                process = subprocess.Popen(["docker", "save", image, f"local/media-importer:{VERSION}"], stdout=subprocess.PIPE)
                shutil.copyfileobj(process.stdout, output)
                process.stdout.close()
                if process.wait():
                    raise RuntimeError("docker save failed")
            print("Exported:", destination, flush=True)
    native = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    selected = native if native in arches else arches[-1]
    run(["docker", "tag", f"local/media-importer:{VERSION}-{selected}", f"local/media-importer:{VERSION}"])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""vxs repo index generator.

Scans debs/*.deb (pure-python ar + tar parsing, no dpkg-deb needed),
emits Packages / Packages.gz / Packages.bz2 / Release / packages.json /
repo-data.js in the repository root.

Determinism notes:
  * all text files are written as raw UTF-8 bytes with LF endings
    (Path.write_text() would emit CRLF on Windows and break hashes)
  * gzip uses mtime=0 so repeated runs produce identical bytes
"""

import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBS = ROOT / "debs"
REPO_JSON = ROOT / "repo.json"

# ---------------------------------------------------------------- utilities


def write_lf(path: Path, data) -> None:
    """Write bytes/text with LF line endings, no BOM, no newline translation."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    Path(path).write_bytes(data)


def load_config() -> dict:
    cfg = {
        "name": "vxs",
        "description": "vxs 越狱软件源",
        "maintainer": "fatekingcsr",
        "url": "https://fatekingcsr.github.io/vxs/",
        "accent": "#5B6CFF",
    }
    if REPO_JSON.exists():
        cfg.update(json.loads(REPO_JSON.read_bytes().decode("utf-8")))
    return cfg


# ---------------------------------------------------------------- ar reader


def deb_control(deb_path: Path) -> bytes:
    """Extract the raw `control` file bytes from a .deb (ar archive)."""
    data = deb_path.read_bytes()
    if data[:8] != b"!<arch>\n":
        raise ValueError(f"{deb_path.name}: not an ar archive")

    off = 8
    control_tar = None
    while off + 60 <= len(data):
        hdr = data[off : off + 60]
        name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(hdr[48:58].decode("ascii").strip())
        except ValueError:
            break
        body = data[off + 60 : off + 60 + size]
        off += 60 + size + (size % 2)

        if name.startswith("control.tar"):
            control_tar = (name, body)

    if control_tar is None:
        raise ValueError(f"{deb_path.name}: no control.tar member")

    name, blob = control_tar
    if name.endswith(".gz"):
        raw = gzip.decompress(blob)
    elif name.endswith(".xz"):
        raw = lzma.decompress(blob)
    elif name.endswith(".lzma"):
        raw = lzma.decompress(blob, format=lzma.FORMAT_ALONE)
    elif name.endswith(".zst"):
        try:
            import zstandard  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ValueError(
                f"{deb_path.name}: control.tar.zst needs `pip install zstandard`"
            ) from exc
        raw = zstandard.ZstdDecompressor().decompress(blob)
    else:
        raw = blob

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for member in tf.getmembers():
            if member.name.lstrip("./") == "control":
                return tf.extractfile(member).read()  # type: ignore[union-attr]
    raise ValueError(f"{deb_path.name}: control file not found inside archive")


def parse_control(raw: bytes) -> dict:
    """Parse an RFC822 control file, joining continuation lines."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    fields: dict[str, str] = {}
    current = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        if line[0] in " \t" and current:
            fields[current] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            current = key.strip()
            fields[current] = value.strip()
    return fields


def installed_size_kib(deb_path: Path) -> int:
    """Fallback Installed-Size: total uncompressed size of data.tar / 1024."""
    data = deb_path.read_bytes()
    off, total = 8, 0
    while off + 60 <= len(data):
        hdr = data[off : off + 60]
        name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(hdr[48:58].decode("ascii").strip())
        except ValueError:
            break
        body = data[off + 60 : off + 60 + size]
        off += 60 + size + (size % 2)
        if name.startswith("data.tar"):
            try:
                if name.endswith(".gz"):
                    blob = gzip.decompress(body)
                elif name.endswith(".xz"):
                    blob = lzma.decompress(body)
                elif name.endswith(".lzma"):
                    blob = lzma.decompress(body, format=lzma.FORMAT_ALONE)
                elif name.endswith(".zst"):
                    import zstandard  # type: ignore

                    blob = zstandard.ZstdDecompressor().decompress(body)
                else:
                    blob = body
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tf:
                    total = sum(m.size for m in tf.getmembers() if m.isfile())
            except Exception:
                total = 0
    return max(total // 1024, 1)


# ---------------------------------------------------------------- index build

# fields we always recompute rather than trusting from control
RECOMPUTED = {"Filename", "Size", "MD5sum", "SHA256"}


def build_entry(deb_path: Path) -> dict:
    control = parse_control(deb_control(deb_path))
    for key in RECOMPUTED:
        control.pop(key, None)

    rel = "./debs/" + deb_path.name
    blob = deb_path.read_bytes()

    if "Installed-Size" not in control:
        control["Installed-Size"] = str(installed_size_kib(deb_path))

    control["Filename"] = rel
    control["Size"] = str(len(blob))
    control["MD5sum"] = hashlib.md5(blob).hexdigest()
    control["SHA256"] = hashlib.sha256(blob).hexdigest()
    return control


ORDER = [
    "Package", "Name", "Version", "Architecture", "Description", "Section",
    "Depends", "Pre-Depends", "Conflicts", "Replaces", "Provides",
    "Maintainer", "Author", "Icon", "Sileodepiction",
    "Filename", "Size", "MD5sum", "SHA256", "Installed-Size",
]


def render_entry(control: dict) -> str:
    keys = [k for k in ORDER if k in control]
    keys += [k for k in control if k not in keys]

    lines = []
    for key in keys:
        value = control[key]
        parts = str(value).split("\n")
        lines.append(f"{key}: {parts[0]}")
        for extra in parts[1:]:
            lines.append(f" {extra.strip()}")
    return "\n".join(lines)


def build_architectures(controls: list[dict]) -> str:
    archs: list[str] = []
    for control in controls:
        for arch in str(control.get("Architecture", "")).split():
            if arch and arch not in archs:
                archs.append(arch)
    for fallback in ("iphoneos-arm64", "iphoneos-arm"):
        if fallback not in archs:
            archs.append(fallback)
    return " ".join(archs)


def main() -> None:
    cfg = load_config()
    DEBS.mkdir(exist_ok=True)

    debs = sorted(DEBS.glob("*.deb"))
    controls = [build_entry(d) for d in debs]
    # deterministic ordering
    controls.sort(key=lambda c: (c.get("Package", ""), c.get("Version", "")))

    if controls:
        packages = "\n".join(render_entry(c) for c in controls) + "\n"
    else:
        packages = ""

    raw = packages.encode("utf-8")
    gz = gzip.compress(raw, 9, mtime=0)
    bz = bz2.compress(raw, 9)

    write_lf(ROOT / "Packages", raw)
    write_lf(ROOT / "Packages.gz", gz)
    write_lf(ROOT / "Packages.bz2", bz)

    now = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    release = (
        f"Origin: {cfg['name']}\n"
        f"Label: {cfg['name']}\n"
        "Suite: stable\n"
        "Version: 1.0\n"
        "Codename: ios\n"
        f"Architectures: {build_architectures(controls)}\n"
        "Components: main\n"
        f"Description: {cfg['description']}\n"
        f"Maintainer: {cfg['maintainer']}\n"
        f"Date: {now}\n"
        "MD5Sum:\n"
        f" {hashlib.md5(raw).hexdigest()} {len(raw)} Packages\n"
        f" {hashlib.md5(gz).hexdigest()} {len(gz)} Packages.gz\n"
        f" {hashlib.md5(bz).hexdigest()} {len(bz)} Packages.bz2\n"
        "SHA256:\n"
        f" {hashlib.sha256(raw).hexdigest()} {len(raw)} Packages\n"
        f" {hashlib.sha256(gz).hexdigest()} {len(gz)} Packages.gz\n"
        f" {hashlib.sha256(bz).hexdigest()} {len(bz)} Packages.bz2\n"
    )
    write_lf(ROOT / "Release", release)

    # web data for the landing page (script tag, so file:// works too)
    web = {
        "name": cfg["name"],
        "description": cfg["description"],
        "maintainer": cfg["maintainer"],
        "url": cfg["url"],
        "accent": cfg.get("accent", "#5B6CFF"),
        "packages": [
            {
                "package": c.get("Package", ""),
                "name": c.get("Name", c.get("Package", "")),
                "version": c.get("Version", ""),
                "description": c.get("Description", "").split("\n")[0],
                "section": c.get("Section", ""),
                "author": c.get("Author", c.get("Maintainer", "")),
                "size": int(c.get("Size", 0) or 0),
                "filename": c.get("Filename", "").lstrip("./"),
                "depiction": c.get("Sileodepiction", ""),
                "icon": c.get("Icon", ""),
            }
            for c in controls
        ],
    }
    payload = json.dumps(web, ensure_ascii=False, indent=2)
    write_lf(ROOT / "packages.json", payload + "\n")
    write_lf(ROOT / "repo-data.js", "window.SILEO_REPO = " + payload + ";\n")

    print(f"[vxs] indexed {len(controls)} package(s)")
    for c in controls:
        print(f"  - {c.get('Package')} {c.get('Version')} [{c.get('Architecture')}]")


if __name__ == "__main__":
    main()
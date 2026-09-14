# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Post-build module signing through the vault: kbuild's in-tree key
# is destroyed right after (unverifiable), out-of-tree ships unsigned.
# This re-signs every '.ko' with the vault key instead; the private
# half never leaves it. Only touched tar members change, so a rebuild
# repacks byte-identical.

import gzip
import hashlib
import io
import lzma
import os
import re
import tarfile

_MODULE_SUFFIX = re.compile(r"\.ko(\.gz|\.xz)?$")

# The xz header's integrity-check type (low 4 bits of byte 7) must
# match the original module's, not lzma's CHECK_CRC64 default -- the
# in-kernel decompressor only understands a few check types.
def _xz_check(data):
    return data[7] & 0x0F

# gzip's own timestamp field is zeroed ('mtime=0') so recompression
# stays deterministic across runs; xz carries no such field.
def _decompress(name, data):
    if name.endswith(".gz"):
        return gzip.decompress(data), ".gz", None
    if name.endswith(".xz"):
        return lzma.decompress(data), ".xz", _xz_check(data)
    # Bare '.tar' (a tar member) or bare '.ko' (suffix-y= clears module
    # compression for the -dbg package's own modules_install pass).
    if name.endswith(".tar") or name.endswith(".ko"):
        return data, "", None
    raise ValueError("'%s': unsupported compression -- seine's vault "
                     "module-signing step only knows .gz and .xz" % name)

def _compress(suffix, data, check=None):
    if suffix == ".gz":
        return gzip.compress(data, compresslevel=9, mtime=0)
    if suffix == ".xz":
        return lzma.compress(data, format=lzma.FORMAT_XZ, preset=6,
                              check=check)
    return data

def _ar_read(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"!<arch>\n":
        raise ValueError("'%s' is not a .deb: no ar magic" % path)
    pos = 8
    members = []
    while pos < len(data):
        header = data[pos:pos + 60]
        if len(header) < 60:
            break
        name = header[0:16].decode().strip()
        mtime = header[16:28].decode().strip()
        uid = header[28:34].decode().strip()
        gid = header[34:40].decode().strip()
        mode = header[40:48].decode().strip()
        size = int(header[48:58].decode().strip())
        pos += 60
        content = data[pos:pos + size]
        pos += size + (size % 2)
        members.append([name, mtime, uid, gid, mode, content])
    return members

def _ar_write(path, members):
    with open(path, "wb") as f:
        f.write(b"!<arch>\n")
        for name, mtime, uid, gid, mode, content in members:
            f.write(("%-16s%-12s%-6s%-6s%-8s%-10d`\n"
                    % (name, mtime, uid, gid, mode, len(content))).encode())
            f.write(content)
            if len(content) % 2 == 1:
                f.write(b"\n")

def _tar_member(members, prefix):
    for i, (name, *_rest) in enumerate(members):
        if name.startswith(prefix):
            return i
    raise ValueError("no '%s*' member -- not a Debian binary package" % prefix)

def _resign_data_tar(vault, key, data_tar_bytes):
    src = tarfile.open(fileobj=io.BytesIO(data_tar_bytes), mode="r:")
    out = io.BytesIO()
    dst = tarfile.open(fileobj=out, mode="w:", format=tarfile.GNU_FORMAT)
    changed = {}
    for info in src.getmembers():
        content = src.extractfile(info).read() if info.isfile() else None
        if info.isfile() and _MODULE_SUFFIX.search(info.name):
            raw, suffix, check = _decompress(info.name, content)
            signed = vault.kmod_sign(key, raw)
            new_content = _compress(suffix, signed, check)
            if new_content != content:
                path = info.name[2:] if info.name.startswith("./") else info.name
                changed[path] = hashlib.md5(new_content).hexdigest()
                content = new_content
                info.size = len(content)
        dst.addfile(info, io.BytesIO(content) if content is not None else None)
    dst.close()
    return out.getvalue(), changed

def _repatch_md5sums(control_tar_bytes, changed):
    src = tarfile.open(fileobj=io.BytesIO(control_tar_bytes), mode="r:")
    out = io.BytesIO()
    dst = tarfile.open(fileobj=out, mode="w:", format=tarfile.GNU_FORMAT)
    for info in src.getmembers():
        content = src.extractfile(info).read() if info.isfile() else None
        if info.isfile() and info.name in ("md5sums", "./md5sums"):
            lines = []
            for line in content.decode().splitlines():
                md5, _, path = line.partition("  ")
                lines.append("%s  %s" % (changed.get(path, md5), path))
            content = ("\n".join(lines) + "\n").encode()
            info.size = len(content)
        dst.addfile(info, io.BytesIO(content) if content is not None else None)
    dst.close()
    return out.getvalue()

# Cheap pre-check so a build's -headers/-tools/-doc .debs (most of
# them) skip the unpack entirely.
def has_modules(deb_path):
    members = _ar_read(deb_path)
    data_tar, _, _ = _decompress(*_deb_member(members, "data.tar"))
    with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:") as tf:
        return any(_MODULE_SUFFIX.search(m.name)
                  for m in tf.getmembers() if m.isfile())

def _deb_member(members, prefix):
    i = _tar_member(members, prefix)
    return members[i][0], members[i][5]

# Re-signs every '.ko' inside deb_path with the vault key, in place.
# Returns changed paths -- empty if nothing needed re-signing.
def resign(deb_path, vault, key):
    members = _ar_read(deb_path)
    data_idx = _tar_member(members, "data.tar")
    data_name = members[data_idx][0]
    data_tar, data_suffix, data_check = _decompress(data_name, members[data_idx][5])

    new_data_tar, changed = _resign_data_tar(vault, key, data_tar)
    if not changed:
        return set()

    control_idx = _tar_member(members, "control.tar")
    control_name = members[control_idx][0]
    control_tar, control_suffix, control_check = _decompress(
        control_name, members[control_idx][5])
    new_control_tar = _repatch_md5sums(control_tar, changed)

    members[data_idx][5] = _compress(data_suffix, new_data_tar, data_check)
    members[control_idx][5] = _compress(control_suffix, new_control_tar, control_check)
    _ar_write(deb_path, members)
    return set(changed)

# resign() changes a .deb's bytes after dpkg-buildpackage already
# described the old ones in .changes; that file gets clearsigned next
# (packages.py._deploy), so its hashes must match first.
def patch_changes(changes_path, output_dir, filenames):
    if len(filenames) == 0:
        return
    digests = {}
    for name in filenames:
        with open(os.path.join(output_dir, name), "rb") as f:
            data = f.read()
        digests[name] = {
            "size": len(data),
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    with open(changes_path, "r") as f:
        lines = f.readlines()

    section = None
    for i, line in enumerate(lines):
        if line[:1] not in (" ", "\t"):
            section = line.split(":", 1)[0]
            continue
        parts = line.split()
        if len(parts) == 0:
            continue
        filename = parts[-1]
        digest = digests.get(filename)
        if digest is None:
            continue
        if section == "Checksums-Sha1":
            lines[i] = " %s %d %s\n" % (digest["sha1"], digest["size"], filename)
        elif section == "Checksums-Sha256":
            lines[i] = " %s %d %s\n" % (digest["sha256"], digest["size"], filename)
        elif section == "Files":
            _, _, category, priority, _ = parts
            lines[i] = " %s %d %s %s %s\n" % (
                digest["md5"], digest["size"], category, priority, filename)

    with open(changes_path, "w") as f:
        f.writelines(lines)

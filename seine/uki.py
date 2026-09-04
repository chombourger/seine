# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uki:' means: wrapping a built 'linux-image-*' and an
# 'initrd:' artifact into one Unified Kernel Image. Unlike kernel/module,
# there is no upstream to fetch -- a uki package carries no 'source:'.

import functools
import os
import shutil

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

import jinja2

from seine.utils import ContainerEngine
from seine.utils import GIT_EMAIL
from seine.utils import GIT_NAME
from seine.utils import distribution

SETTINGS = ["cmdline", "initrd", "linux-image", "signing-cert",
            "signing-key", "tool"]

TOOLS = ["ukify", "efibootguard"]

# Build-Depends that put each tool and its EFI stub into the chroot.
TOOL_BUILD_DEPENDS = {
    "ukify": "systemd-ukify, systemd-boot-efi",
    "efibootguard": "efibootguard",
}

# UEFI's own architecture names ('kernel-stubx64.efi'), not Debian's.
EFI_ARCH = {
    "amd64": "x64",
    "arm64": "aa64",
}

# Also shell-quoted before reaching debian/rules; belt and suspenders.
FORBIDDEN_CMDLINE = ["`", "$(", ";", "&", "|", "\n"]

INITRD_NAME = "initrd.img"

def is_uki_package(package):
    return getattr(package, "uki", False)

# Reads 'extends: uki:' onto the package it was written on, the way
# kernel.parse()/module.parse() do.
def parse(package, extends):
    settings = extends.get("uki", {})
    package.uki = "uki" in extends
    if package.uki == False:
        package.uki_tool = None
        package.uki_linux_image = None
        package.uki_initrd = None
        package.uki_cmdline = ""
        return

    if package.source is not None:
        raise package._error(
            "'extends: uki' packages are generated entirely by seine: "
            "name the package with 'name:' alone, without a 'source:'")
    package.source_name = package.name

    tool = settings.get("tool")
    if tool not in TOOLS:
        raise package._error(
            "'extends: uki: tool' shall be one of %s" % ", ".join(TOOLS))
    package.uki_tool = tool

    linux_image = settings.get("linux-image")
    if type(linux_image) != type("") or len(linux_image) == 0:
        raise package._error(
            "'extends: uki: linux-image' shall name the 'linux-image' "
            "package this UKI wraps, as a string")
    package.uki_linux_image = linux_image

    initrd = settings.get("initrd")
    if type(initrd) != type("") or len(initrd) == 0:
        raise package._error(
            "'extends: uki: initrd' shall name the 'initrd:' artifact "
            "this UKI is built from, as a string")
    package.uki_initrd = initrd

    cmdline = settings.get("cmdline", "")
    if type(cmdline) != type(""):
        raise package._error("'extends: uki: cmdline' shall be a string")
    for forbidden in FORBIDDEN_CMDLINE:
        if forbidden in cmdline:
            raise package._error(
                "'extends: uki: cmdline' contains '%s', which a kernel "
                "command line may not" % forbidden.strip())
    package.uki_cmdline = cmdline

    # Reserved but not implemented yet: refused rather than silently
    # building an unsigned UKI nobody asked for.
    if "signing-key" in settings or "signing-cert" in settings:
        raise package._error(
            "'extends: uki: signing-key'/'signing-cert' are not yet "
            "supported -- drop them and build unsigned for now")

def initrd_path(distro, filename):
    if os.path.isabs(filename):
        return filename
    return os.path.join(ContainerEngine.deploy_root(), distro["release"], filename)

def _require_initrd(package, distro):
    path = initrd_path(distro, package.uki_initrd)
    if os.path.isfile(path) == False:
        raise ValueError(
            "package '%s': 'extends: uki: initrd' names '%s', which is "
            "not a deployed file (%s) -- build its own specification "
            "first" % (package.name, package.uki_initrd, path))
    return path

# Checked as soon as the spec is parsed, before any bootstrap or fetch
# work starts -- the same reason kernel/module errors are caught at
# parse time rather than mid-build.
def check_initrds(packages, spec):
    distro = distribution(spec)
    for package in packages:
        if is_uki_package(package):
            _require_initrd(package, distro)

def _sh_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"

UKI_PACKAGING = os.path.join(os.path.dirname(__file__), "data", "uki-ukify")
EFIBOOTGUARD_PACKAGING = os.path.join(
    os.path.dirname(__file__), "data", "uki-efibootguard")
UKI_FILES = ["changelog", "control", "rules"]

@functools.lru_cache(maxsize=None)
def uki_packaging(tool):
    directory = UKI_PACKAGING if tool == "ukify" else EFIBOOTGUARD_PACKAGING
    templates = {}
    for name in UKI_FILES:
        with open(os.path.join(directory, name), "rb") as f:
            templates[name] = f.read().decode()
    return templates

UKI_TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)

def _write(path, content):
    with open(path, "w") as f:
        f.write(content)

def extend(builder, package, sourcedir, epoch):
    if package.uki == False:
        return

    debian = os.path.join(sourcedir, "debian")
    if os.path.isdir(debian):
        shutil.rmtree(debian)
    os.makedirs(os.path.join(debian, "source"), exist_ok=True)
    _write(os.path.join(debian, "source", "format"), "3.0 (native)\n")

    initrd = _require_initrd(package, builder.distro)
    shutil.copy(initrd, os.path.join(sourcedir, INITRD_NAME))

    architecture = builder.distro["architecture"]
    efi_arch = EFI_ARCH.get(architecture)
    if package.uki_tool == "efibootguard" and efi_arch is None:
        raise ValueError(
            "package '%s': 'extends: uki: tool: efibootguard' has no "
            "EFI stub name for architecture '%s' -- add it to "
            "uki.EFI_ARCH" % (package.name, architecture))

    context = {
        "name": package.name,
        "version": package.upstream_version,
        "maintainer": GIT_NAME,
        "email": GIT_EMAIL,
        "date": format_datetime(datetime.fromtimestamp(epoch, timezone.utc)),
        "linux_image": package.uki_linux_image,
        "tool_build_depends": TOOL_BUILD_DEPENDS[package.uki_tool],
        "initrd": INITRD_NAME,
        "efi_arch": efi_arch,
        "cmdline_arg": ("--cmdline=%s" % _sh_quote(package.uki_cmdline)
                        if package.uki_cmdline else ""),
    }
    for name, template in uki_packaging(package.uki_tool).items():
        _write(os.path.join(debian, name),
               UKI_TEMPLATE.from_string(template).render(context))

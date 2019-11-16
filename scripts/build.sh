#!/bin/bash

arch=amd64
distro=${DISTRO_NAME}
verbose=

set -e

if [ -n "${verbose}" ]; then
    set -x
fi

add_ext_pkgs() {
    reprepro -b apt -C ${distro} includedeb seine ${1}/*.deb
    reprepro -b apt -C ${distro} includedsc seine ${1}/*.dsc
    rm -f ${1}/*.deb ${1}/*.dsc ${1}/*.tar.[gx]z
}

do_build_deps() {
    local opts

    if [ -n "${verbose}" ]; then
        opts="-o Debug::pkgProblemResolver=yes -y"
    else
        opts="-qqy"
    fi
    mk-build-deps -t "apt-get ${opts} --no-install-recommends" -i -r debian/control
    apt-get purge -qqy
    rm -f *.deb
}

pkg_get_version() {
    head -n 1 debian/changelog | awk '{ print $2 }'| tr -d '()'
}

_pkg_build() {
    from=$(pwd)

    # resolve build dependencies and build our package
    cd ${1}
    do_build_deps
    dpkg-buildpackage -uc -us

    # add generated source and binary packages to repository
    cd ${from}
    add_ext_pkgs $(dirname ${1})
}

pkg_build() {
    from=$(pwd)
    pkg=$(basename ${1})

    # create source tarball
    cd ${1}
    pv=$(pkg_get_version)
    sv=${pv/%-[a-z0-9]*}
    tarball=../${pkg}_${sv}.orig.tar.gz
    if [ -e .git ]; then
        git archive --format=tar.gz HEAD >${tarball}
    else
        tar -C .. --exclude='*/debian/*' -zcf ${tarball} ${pkg}
    fi
    cd ${from}

    _pkg_build ${1}
}

apt-get update -qqy
apt-get install -qqy devscripts equivs git reprepro
apt-get purge -qqy

pkg_build external/conmon
pkg_build external/slirp4netns
pkg_build external/libpod

if [ "${DISTRO_NAME}" = "bionic" ]; then
    _pkg_build support/bionic/user-mode-linux
fi

pkg_build modules/seine

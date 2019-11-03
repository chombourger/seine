#!/bin/bash

arch=amd64
distro=${DISTRO_NAME}

set -e
set -x

add_ext_pkgs() {
    reprepro -b apt -C ${distro} includedeb seine ${1}/*.deb
    reprepro -b apt -C ${distro} includedsc seine ${1}/*.dsc
    rm -f ${1}/*.deb ${1}/*.dsc ${1}/*.tar.[gx]z
}

do_build_deps() {
    mk-build-deps -t "apt-get -o Debug::pkgProblemResolver=yes --no-install-recommends -y" -i -r debian/control
    apt-get purge
    rm -f *.deb
}

pkg_get_version() {
    head -n 1 debian/changelog | awk '{ print $2 }'| tr -d '()'
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
        tar -C .. --exclude='*/debian/*' -zcvf ${tarball} ${pkg}
    fi

    # resolve build dependencies and build our package
    do_build_deps
    debuild -uc -us

    # add generated source and binary packages to repository
    cd ${from}
    add_ext_pkgs $(dirname ${1})
}

apt-get update
apt-get install -y devscripts equivs git reprepro
apt-get purge

pkg_build external/conmon
pkg_build external/slirp4netns
pkg_build external/libpod
pkg_build modules/seine

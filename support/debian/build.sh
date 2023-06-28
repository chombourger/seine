#!/bin/bash

distro=${DISTRO_NAME}
verbose=

set -e

if [ -n "${verbose}" ]; then
    set -x
fi

deb_add_ext_pkgs() {
    reprepro -b apt -C ${distro} includedeb seine ${1}/*.deb
    reprepro -b apt -C ${distro} includedsc seine ${1}/*.dsc
    rm -f ${1}/*.deb ${1}/*.dsc ${1}/*.tar.[gx]z
}

deb_build_deps() {
    local opts

    if [ -n "${verbose}" ]; then
        opts="-o Debug::pkgProblemResolver=yes -y"
    else
        opts="-qqy"
    fi
    mk-build-deps -t "apt-get ${opts} --no-install-recommends" -i -r debian/control
    rm -f seine-build-deps_*
    apt-get clean -qqy
}

deb_build_prepare() {
    deb_build_deps
}

deb_get_pkg_version() {
    head -n 1 debian/changelog | awk '{ print $2 }'| tr -d '()'
}

_pkg_build() {
    from=$(pwd)

    # resolve build dependencies and build our package
    cd ${1}
    deb_build_prepare
    dpkg-buildpackage -uc -us -jauto

    # add generated source and binary packages to repository
    cd ${from}
    deb_add_ext_pkgs $(dirname ${1})
}

deb_build_pkg() {
    from=$(pwd)
    pkg=$(basename ${1})

    # create source tarball
    cd ${1}
    pv=$(deb_get_pkg_version)
    sv=${pv/%-[a-z0-9]*}
    tarball=../${pkg}_${sv}.orig.tar.gz
    if [ -e .git ]; then
        git archive --format=tar.gz HEAD >${tarball}
    else
        tar -C .. --exclude='*/debian/*'  -zcf ${tarball} ${pkg}
    fi
    cd ${from}

    _pkg_build ${1}
}

deb_setup_env() {
    apt-get update -qqy
    apt-get install -qqy devscripts equivs git reprepro
    apt-get clean -qqy
}

rm -rf src/seine/__pycache__
deb_setup_env
deb_build_pkg src/seine

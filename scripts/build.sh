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

apt-get update
apt-get install -y devscripts equivs git reprepro
apt-get purge

cd external/conmon
pv=$(pkg_get_version)
sv=${pv/%-[a-z0-9]*}
tarball=../conmon_${sv}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
dpkg -i external/conmon_${pv}_${arch}.deb
add_ext_pkgs external

cd external/slirp4netns
pv=$(pkg_get_version)
sv=${pv/%-[a-z0-9]*}
tarball=../slirp4netns_${sv}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd external/libpod
pv=$(pkg_get_version)
sv=${pv/%-[a-z0-9]*}
tarball=../libpod_${sv}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd external/python-podman
pv=$(pkg_get_version)
sv=${pv/%-[a-z0-9]*}
tarball=../python-podman_${sv}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd modules/seine
pv=$(pkg_get_version)
sv=${pv/%-[a-z0-9]*}
tarball=../seine_${sv}.orig.tar.gz
tar -C .. --exclude='*/debian/*' -zcvf ${tarball} seine
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs modules

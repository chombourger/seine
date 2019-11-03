#!/bin/bash

arch=amd64
distro=${DISTRO_NAME}

conmon_version=2.0.2-deb10
podman_version=1.6.2-1
python_podman_version=1.6.0-1
seine_version=0.1-1
slirp4netns_version=0.4.2-1

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

apt-get update
apt-get install -y devscripts equivs git reprepro
apt-get purge

cd external/conmon
tarball=../conmon_${conmon_version/%-[a-z0-9]*}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
dpkg -i external/conmon_${conmon_version}_${arch}.deb
add_ext_pkgs external

cd external/slirp4netns
tarball=../slirp4netns_${slirp4netns_version/%-[a-z0-9]*/}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd external/libpod
tarball=../libpod_${podman_version/%-[a-z0-9]*/}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd external/python-podman
tarball=../python-podman_${python_podman_version/%-[a-z0-9]*/}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs external

cd modules
tarball=seine_${seine_version/%-[a-z0-9]*/}.orig.tar.gz
tar --exclude='*/debian/*' -zcvf ${tarball} seine
cd seine
do_build_deps
debuild -uc -us
cd ../..
add_ext_pkgs modules


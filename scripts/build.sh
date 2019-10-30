#!/bin/bash

arch=amd64
distro=buster

conmon_version=2.0.2-deb10
slirp4netns_version=0.4.2-1

set -e
set -x

add_ext_pkgs() {
    reprepro -b apt -C main includedeb ${distro} external/*.deb
    reprepro -b apt includedsc ${distro} external/*.dsc
    rm -f external/*.deb external/*.dsc external/*.tar.[gx]z
}

do_build_deps() {
    mk-build-deps -t "apt-get -o Debug::pkgProblemResolver=yes --no-install-recommends -y" -i -r debian/control
    apt-get purge
    rm -f *.deb
}

apt-get update
apt-get install -y devscripts git reprepro
apt-get purge

cd external/conmon
tarball=../conmon_${conmon_version/%-[a-z0-9]*}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..

dpkg -i external/conmon_${conmon_version}_${arch}.deb
add_ext_pkgs

cd external/slirp4netns
tarball=../slirp4netns_${slirp4netns_version/%-[a-z0-9]*/}.orig.tar.gz
git archive --format=tar.gz HEAD >${tarball}
do_build_deps
debuild -uc -us
cd ../..

add_ext_pkgs

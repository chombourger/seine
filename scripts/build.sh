#!/bin/sh

arch=amd64
conmon_version=2.0.2-deb10
distro=buster

set -e
set -x

add_ext_pkgs() {
    reprepro -b apt -C main includedeb ${distro} external/*.deb
    reprepro -b apt includedsc ${distro} external/*.dsc
    rm -f external/*.deb external/*.dsc external/*.tar.[gx]z
}

do_build_deps() {
    mk-build-deps -t "apt-get --no-install-recommends -y" -i -r debian/control
    apt-get purge
    rm -f *.deb
}

apt-get update
apt-get install -y devscripts git reprepro
apt-get purge

cd external/conmon
git archive --format=tar HEAD > ../conmon_${conmon_version}.orig.tar
gzip -f ../conmon_${conmon_version}.orig.tar
do_build_deps
debuild -uc -us
cd ../..

dpkg -i external/conmon_${conmon_version}_${arch}.deb
add_ext_pkgs

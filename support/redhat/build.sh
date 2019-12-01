#!/bin/bash

verbose=

set -e

if [ -n "${verbose}" ]; then
    set -x
fi

rpm_build_deps() {
    dnf builddep -y *.src.rpm
}

rpm_build_prepare() {
    if [ -f .rpmbuild/Makefile ]; then
        make -f .rpmbuild/Makefile srpm
    fi
    rpm_build_deps
}

rpm_build_pkg() {
    from=$(pwd)
    pkg=$(basename ${1})

    cd ${1}
    rpm_build_prepare
    rpmbuild --rebuild *.src.rpm
    cd ${from}
}

rpm_setup_env() {
    dnf update -y
    dnf install -y createrepo dnf-plugins-core make rpm-build
    yum config-manager --set-enabled PowerTools
}

rpm_setup_env
rpm_build_pkg src/seine
rpm_build_pkg support/${DISTRO_NAME}/conmon
rpm_build_pkg support/${DISTRO_NAME}/go-md2man
rpm -ivh /root/rpmbuild/RPMS/*/go-md2man-*.rpm
rpm_build_pkg support/${DISTRO_NAME}/libpod
rpm_build_pkg support/${DISTRO_NAME}/user-mode-linux
createrepo /root/rpmbuild/RPMS

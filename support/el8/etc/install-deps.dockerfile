FROM centos:8 AS base
COPY support support
COPY rpmbuild rpmbuild
COPY support/el8/etc/seine.repo /etc/yum.repos.d/

RUN dnf install -y seine
CMD ["true"]

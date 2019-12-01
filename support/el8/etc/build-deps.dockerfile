FROM centos:8 AS base
ENV DISTRO_ID=centos
ENV DISTRO_NAME=el8

FROM base AS buildsrc
COPY .git .git
COPY support support

COPY .git      src/seine/.git
COPY .rpmbuild src/seine/.rpmbuild
COPY redhat    src/seine/redhat
COPY seine     src/seine/seine
COPY setup.py  src/seine/setup.py

FROM buildsrc AS build
RUN  bash support/redhat/build.sh

FROM base AS results
COPY --from=build root/rpmbuild rpmbuild

CMD ["true"]

FROM ubuntu:bionic AS base
ENV DISTRO_ID=ubuntu
ENV DISTRO_NAME=bionic

COPY scripts scripts

FROM base AS buildsrc
COPY .git .git
COPY apt apt
COPY support support

COPY debian   src/seine/debian
COPY seine    src/seine/seine
COPY setup.py src/seine/setup.py

FROM buildsrc AS build
RUN  bash scripts/build.sh

FROM base AS results
COPY --from=build apt apt

CMD ["true"]

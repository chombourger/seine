FROM ubuntu:bionic AS base
ENV DISTRO_ID=ubuntu
ENV DISTRO_NAME=bionic

COPY scripts scripts

FROM base AS buildsrc
COPY .git .git
COPY apt apt
COPY external external
COPY modules modules
COPY support support

FROM buildsrc AS build
RUN  bash scripts/build.sh

FROM base AS results
COPY --from=build apt apt

CMD ["true"]

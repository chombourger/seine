FROM debian:buster AS base
ENV DISTRO_ID=debian
ENV DISTRO_NAME=buster

COPY scripts scripts

FROM base AS buildsrc
COPY .git .git
COPY apt apt
COPY modules modules
COPY support support

FROM buildsrc AS build
RUN  bash scripts/build.sh

FROM base AS results
COPY --from=build apt apt

CMD ["true"]

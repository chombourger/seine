FROM debian:buster AS base
ENV DISTRO_ID=debian
ENV DISTRO_NAME=buster

FROM base AS buildsrc
COPY .git .git
COPY apt apt
COPY support support

COPY debian   src/seine/debian
COPY seine    src/seine/seine
COPY setup.py src/seine/setup.py

FROM buildsrc AS build
RUN  bash support/debian/build.sh

FROM base AS results
COPY --from=build apt apt

CMD ["true"]

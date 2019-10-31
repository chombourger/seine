FROM debian:buster AS base
COPY scripts scripts

FROM base AS buildsrc
COPY .git .git
COPY apt apt
COPY external external

FROM buildsrc AS build
RUN  bash scripts/build.sh

FROM base AS results
COPY --from=build apt apt

CMD ["true"]

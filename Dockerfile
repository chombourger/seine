FROM debian:buster AS buildenv
COPY .git .git
COPY apt apt
COPY external external
COPY scripts scripts

FROM buildenv AS seine
RUN  bash scripts/build.sh

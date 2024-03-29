FROM ubuntu:jammy AS base
COPY support support
COPY apt apt
COPY support/jammy/etc/seine.list /etc/apt/sources.list.d/

RUN apt-get update -qqy && apt-get install -qqy seine
CMD ["true"]

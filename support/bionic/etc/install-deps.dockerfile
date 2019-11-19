FROM ubuntu:bionic AS base
COPY support support
COPY apt apt
COPY support/bionic/etc/seine.list /etc/apt/sources.list.d/

RUN apt-get update -qqy && apt-get install -qqy python3-seine
CMD ["true"]

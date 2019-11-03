FROM ubuntu:bionic AS base
COPY scripts scripts
COPY apt apt
COPY scripts/bionic/seine.list /etc/apt/sources.list.d/

RUN apt-get update && apt-get install -y python3-seine
CMD ["true"]

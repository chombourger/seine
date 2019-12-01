#!/bin/sh -euf
set -x

if [ ! -e /usr/bin/git ]; then
    dnf -y install git-core
fi

git fetch --unshallow || :

COMMIT=$(git rev-parse HEAD)
COMMIT_SHORT=$(git rev-parse --short=8 HEAD)
COMMIT_NUM=$(git rev-list HEAD --count)
COMMIT_DATE=$(date +%s)


sed "s,#COMMIT#,${COMMIT},;
     s,#SHORTCOMMIT#,${COMMIT_SHORT},;
     s,#COMMITNUM#,${COMMIT_NUM},;
     s,#COMMITDATE#,${COMMIT_DATE}," \
         redhat/seine.spec.in > redhat/seine.spec

mkdir -p build/
git archive --prefix "seine-${COMMIT_SHORT}/" --format "tar.gz" HEAD -o "build/seine-${COMMIT_SHORT}.tar.gz"

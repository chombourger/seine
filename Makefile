#!/usr/bin/make -f

docker=$(shell which docker 2>/dev/null)
podman=$(shell which podman 2>/dev/null)
engine=$(if $(podman),$(podman),$(docker))

.PHONY: buster
buster: clean
	$(engine) build -t seine/$@ .
	cid="$$($(engine) create seine/$@)" && \
	$(engine) cp $$cid:apt/ . && \
	$(engine) container rm $$cid

.PHONY: clean
clean:
	rm -rf apt/db apt/dists apt/pool
	ids="$$($(engine) container ls -a|grep seine/|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) container rm $${ids}
	ids="$$($(engine) image ls -a|grep seine/|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) image rm $${ids} || true

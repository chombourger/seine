#!/usr/bin/make -f

docker=$(shell which docker 2>/dev/null)
podman=$(shell which podman 2>/dev/null)
engine=$(if $(podman),$(podman),$(docker))

distros=bionic buster
product=seine

distro=$(notdir $@)

.PHONY: all
all: build/deps

.PHONY: check
check: install/deps

.PHONY: clean
clean: clean/build/deps clean/install/deps
	rm -rf apt/db apt/dists apt/pool

.PHONY: build/deps
build/deps: $(foreach d,$(distros),build/deps/$(d))

.PHONY: install/deps
install/deps: $(foreach d,$(distros),install/deps/$(d))

.PHONY: clean/build/deps
clean/build/deps: $(foreach d,$(distros),clean/build/deps/$(d))

.PHONY: clean/install/deps
clean/install/deps: $(foreach d,$(distros),clean/install/deps/$(d))

.PHONY: build/deps/%
build/deps/%:
	$(engine) build --rm -t $@ -f scripts/$(distro)/build-deps.dockerfile .
	cid="$$($(engine) create $@)" && \
	$(engine) cp $$cid:apt/ . && \
	$(engine) container rm $$cid
	$(engine) image ls

.PHONY: install/deps/%
install/deps/%: build/deps/%
	$(engine) build --rm -t $@ -f scripts/$(distro)/install-deps.dockerfile .

.PHONY: clean/%
clean/%:
	ids="$$($(engine) container ls -a|grep $(patsubst clean/%,%,$@)|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) container rm $${ids}
	ids="$$($(engine) image ls -a|grep $(patsubst clean/%,%,$@)|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) image rm $${ids} || true

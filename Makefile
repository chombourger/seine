#!/usr/bin/make -f
# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

docker=$(shell which docker 2>/dev/null)
podman=$(shell which podman 2>/dev/null)
engine=$(if $(podman),$(podman),$(docker))

squash=$(if $(podman),--squash,)

distros=jammy bookworm el8
product=seine

distro=$(notdir $@)

pkgdir_jammy=apt
pkgdir_bookworm=apt
pkgdir_el8=rpmbuild

.PHONY: all
all: build/deps

.PHONY: check
check: install/deps

.PHONY: clean
clean: clean/build/deps clean/install/deps
	rm -rf apt/db apt/dists apt/pool rpmbuild

.PHONY: build/deps
build/deps: $(foreach d,$(distros),build/deps/$(d))

.PHONY: install/deps
install/deps: $(foreach d,$(distros),install/deps/$(d))

.PHONY: clean/build/deps
clean/build/deps: $(foreach d,$(distros),clean/build/deps/$(d))

.PHONY: clean/install/deps
clean/install/deps: $(foreach d,$(distros),clean/install/deps/$(d))

.PHONY: build/deps/%
build/deps/%: support/%/etc/build-deps.dockerfile
	$(engine) build --rm $(squash) -t $@ -f $< .
	cid="$$($(engine) create $@)" && \
	$(engine) cp $$cid:$(pkgdir_$(distro))/ . && \
	$(engine) container rm $$cid
	$(engine) image rm $@

.PHONY: install/deps/%
install/deps/%: support/%/etc/install-deps.dockerfile build/deps/%
	$(engine) build --rm $(squash) -t $@ -f $< .
	$(engine) image rm $@

.PHONY: clean/%
clean/%:
	ids="$$($(engine) container ls -a|grep $(patsubst clean/%,%,$@)|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) container rm $${ids}
	ids="$$($(engine) image ls -a|grep $(patsubst clean/%,%,$@)|awk '{ print $$1 }')"; \
	[ -z "$${ids}" ] || $(engine) image rm $${ids} || true

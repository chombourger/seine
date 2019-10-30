
.PHONY: buster

docker=$(shell which docker 2>/dev/null)
podman=$(shell which podman 2>/dev/null)
engine=$(if $(podman),$(podman),$(docker))

buster:
	$(engine) build -t seine/$@ .
	cid=$(shell $(engine) create seine/$@) && \
	$(engine) cp $$cid:apt/ . && \
	$(engine) container rm $$cid

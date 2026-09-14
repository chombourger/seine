# Custom vault image: upstream OpenBao plus our signing plugins. Podman
# builds it; dev instances and tests run it. Plugin sources compile in
# the golang stage so building the image never needs Go on the host.
FROM docker.io/library/golang:1.26.6 AS pgp-build
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY seine-pgp/go.mod seine-pgp/go.sum ./
RUN go mod download
COPY seine-pgp/ ./
RUN go build -o /out/seine-pgp.so . && chmod 755 /out/seine-pgp.so

FROM docker.io/openbao/openbao:2.6.2
COPY --from=pgp-build /out/seine-pgp.so /vault/plugins/seine-pgp.so

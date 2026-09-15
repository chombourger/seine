# Custom vault image: upstream OpenBao plus our signing plugins. Podman
# builds it; dev instances and tests run it. Plugin sources compile in
# golang stages so building the image never needs Go on the host.
FROM docker.io/library/golang:1.26.6 AS pgp-build
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY vault/openbao/plugins/pgp/go.mod vault/openbao/plugins/pgp/go.sum ./
RUN go mod download
COPY vault/openbao/plugins/pgp/ ./
RUN go build -o /out/seine-pgp.so . && chmod 755 /out/seine-pgp.so

FROM docker.io/library/golang:1.26.6 AS sbsign-build
ENV GOTOOLCHAIN=local
WORKDIR /src/sbsign
COPY vault/openbao/plugins/pkcs7/go.mod ../pkcs7/
COPY vault/openbao/plugins/sbsign/go.mod vault/openbao/plugins/sbsign/go.sum ./
RUN go mod download
COPY vault/openbao/plugins/pkcs7/ ../pkcs7/
COPY vault/openbao/plugins/sbsign/ ./
RUN go test ./... ../pkcs7/ && \
    go build -o /out/seine-sbsign.so . && chmod 755 /out/seine-sbsign.so

FROM docker.io/library/golang:1.26.6 AS kmod-build
ENV GOTOOLCHAIN=local
WORKDIR /src/kmod
COPY vault/openbao/plugins/pkcs7/go.mod ../pkcs7/
COPY vault/openbao/plugins/kmod/go.mod vault/openbao/plugins/kmod/go.sum ./
RUN go mod download
COPY vault/openbao/plugins/pkcs7/ ../pkcs7/
COPY vault/openbao/plugins/kmod/ ./
RUN go test ./... ../pkcs7/ && \
    go build -o /out/seine-kmod.so . && chmod 755 /out/seine-kmod.so

FROM docker.io/openbao/openbao:2.6.2
COPY --from=pgp-build /out/seine-pgp.so /vault/plugins/seine-pgp.so
COPY --from=sbsign-build /out/seine-sbsign.so /vault/plugins/seine-sbsign.so
COPY --from=kmod-build /out/seine-kmod.so /vault/plugins/seine-kmod.so

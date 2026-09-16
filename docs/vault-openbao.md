# Running a vault for seine

seine can sign build output (root/account passwords, kernel modules, Secure
Boot UKIs, apt repositories) through a vault instead of a key on the build
machine, via `vault:<name>` and `vault('<path>#<field>')` in a spec. A
feed's `signed-by: vault:<name>` (see [feeds](specification.md#feeds)) only
reads a key rather than signing with it, so a token scoped to that one read
is enough. This page shows how to stand up that vault:
[OpenBao](https://openbao.org/) plus seine's own signing plugins
(`seine-pgp`, `seine-sbsign`, `seine-kmod`), running as a container on a
server of your choice.

**Security disclaimer.** This page gets a vault running and reachable --
nothing more. It makes no claim about how secure that instance is. Network
exposure, authentication beyond a root token, TLS certificates, storage
encryption, backups, and key rotation are for you to review and harden
before trusting this with anything real. Treat what follows as a starting
point for an admin who already knows how to run OpenBao/Vault in
production, not as a hardening guide.

## 1. Get the vault image

Either install the prebuilt package:

```
apt-get install seine-vault-openbao
podman load -i /usr/share/seine/oci/vault/images.tar.gz
```

or build it yourself from a seine checkout (needs network access, to pull
the golang and openbao base images):

```
HOSTARCH=<amd64|arm64> ./debian/build-vault-image.py /tmp/out
podman load -i /tmp/out/vault/images.tar.gz
```

Either way this gives you `localhost/seine-vault:latest`: upstream OpenBao
plus the three signing plugins, under `/vault/plugins`.

## 2. Configure and start the server

Pick a TLS certificate for the listener -- one from your own CA, or a
self-signed one for a first try:

```
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -noenc \
    -keyout vault.key -out vault.crt \
    -subj "/CN=<your-host>" \
    -addext "subjectAltName=IP:<your-host-ip>,DNS:<your-host-name>"
```

Write a config file:

```
storage "file" {
  path = "/vault/data"
}
listener "tcp" {
  address              = "0.0.0.0:8200"
  tls_cert_file        = "/vault/tls/vault.crt"
  tls_key_file         = "/vault/tls/vault.key"
  max_request_size     = 268435456
  max_request_json_memory = 268435456
}
plugin_directory = "/vault/plugins"
disable_mlock = true
api_addr = "https://<your-host>:8200"
```

`max_request_size`/`max_request_json_memory` are raised well past OpenBao's
32MiB default: a signed UKI can be tens of MB, and the whole request body
(base64) must fit under this limit or the connection is dropped mid-upload.

Then start the container, with the config, TLS files and a data directory
bind-mounted in (`:U` chowns the data directory to the container's own
user, needed for rootless podman):

```
podman run -d --name seine-vault \
  -p 8200:8200 \
  -v ./config:/vault/config:ro \
  -v ./tls:/vault/tls:ro \
  -v ./data:/vault/data:U \
  localhost/seine-vault \
  server -config=/vault/config/config.hcl
```

## 3. Initialize and unseal

```
podman exec -e BAO_ADDR=https://127.0.0.1:8200 -e BAO_SKIP_VERIFY=1 seine-vault \
  bao operator init -key-shares=5 -key-threshold=3
```

This prints 5 unseal keys and a root token, once -- store them somewhere
safe, off this host. The vault starts sealed after every restart; unseal it
again with any 3 of the 5 keys:

```
podman exec -e BAO_ADDR=https://127.0.0.1:8200 -e BAO_SKIP_VERIFY=1 seine-vault \
  bao operator unseal <key>
```
(repeat with two more distinct keys)

## 4. Enable the secrets engines seine expects

The commands below run `bao` directly rather than through `podman exec`.
Get it onto the admin machine without a separate apt repo by copying it out
of the image you already have:

```
cid=$(podman create localhost/seine-vault)
podman cp "$cid":/usr/bin/bao /usr/local/bin/bao
podman rm "$cid"
```

A `kv:` version-2 mount, for `vault('<path>#<field>')` reads:

```
bao secrets enable -path=kv -version=2 kv
```

Each signing plugin needs registering in the catalog (its `.so`'s digest,
from inside the image) before it can be mounted:

```
for p in seine-pgp seine-sbsign seine-kmod; do
  digest=$(podman exec seine-vault sha256sum /vault/plugins/$p.so | cut -d' ' -f1)
  bao plugin register -sha256=$digest secret $p
  bao secrets enable -path=$p $p
done
```

(`bao` here needs `BAO_ADDR`/`BAO_TOKEN` pointing at your vault -- export
them, or pass `-address`/`-token` on each command.)

## 5. Create the secrets and keys your specs need

Which names to create depends on what your specs reference. Check each
spec's own `defaults: vault:` block (the dev-only fallback names) and any
`vault:<name>` or `vault('...')` in it. Typically:

```
# a plain secret, e.g. an account password hash
bao kv put kv/accounts/root hash='<crypt hash>' password='<plaintext>'

# a signing key -- generate fresh, or import an existing key/cert pair
bao write seine-sbsign/keys/<name> generate='{}'
bao write seine-kmod/keys/<name> generate='{}'
bao write seine-pgp/keys/<name> generate='{"name": "...", "email": "..."}'
```

A key's creation time matters: the plugins refuse to sign at a timestamp
before the key existed, and seine signs at the build's own epoch (the
newest spec file's mtime). Create keys before you build with them.

### Worked example: `examples/pc-uki-image`

The exact commands used to provision that spec's four vault-backed
secrets, `bao write PATH -` reading the JSON body from stdin:

```
# root/login password (examples/common/conf-accounts.yaml)
bao kv put kv/accounts/root hash='<crypt sha512 hash>' password='<plaintext>'

# kernel module signing (kernel.yaml: signing-key: vault:pc-uki-kernel-modules)
echo '{"generate": {}}' | bao write seine-kmod/keys/pc-uki-kernel-modules -

# UKI secure-boot signing (main.yaml: secure-boot.private-key: vault:pc-uki-secureboot)
echo '{"generate": {}}' | bao write seine-sbsign/keys/pc-uki-secureboot -

# apt repository signing (SEINE_SIGN_KEY=vault:pc-uki-repo)
echo '{"generate": {"name": "seine pc-uki-image repo", "email": "seine-demo@example.invalid", "key_type": "rsa3072"}}' \
  | bao write seine-pgp/keys/pc-uki-repo -
```

`generate` mints a fresh key/cert inside the vault; use `import` instead
(same shape, `{"import": {"key_pem": ..., "cert_pem": ...}}` or
`{"import": {"private_key": ...}}` for pgp) to bring in a key you already
hold rather than trusting the vault to mint one.

## 6. Point a build at it

```
export SEINE_VAULT_ADDR=https://<your-host>:8200
export SEINE_VAULT_TOKEN=<a token, root or scoped -- see below>
export SEINE_VAULT_CERT=/path/to/vault.crt   # only for a private/self-signed cert
export SEINE_SIGN_KEY=vault:<pgp key name>   # to sign the apt repository too
seine build your-spec.yaml
```

See `docs/environment.md` for the full list of `SEINE_VAULT_*` variables.

## Where to go from here

None of the above is a finished production setup:

- The root token used above can do anything to this vault. Create a
  narrower policy and token (or another auth method) for day-to-day builds.
- `disable_mlock` and a self-signed certificate are conveniences, not
  hardening. Review both for your environment.
- The file storage backend keeps everything on this one host's disk --
  back it up, and consider a different backend if that single point of
  failure matters to you.
- Firewalling: only the build machines that need it should be able to
  reach port 8200.

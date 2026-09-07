# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess

# Signs build output using the host's gpg and agent, never inside a
# container. The builder container is too privileged to trust with the
# signing key or its passphrase.
class Signer:
    def __init__(self, key):
        self.key = key
        self._fingerprint = None

    # Resolves the key (short id, long id, fingerprint, or email) to its
    # fingerprint. Called early so a missing key fails before any build
    # output exists.
    def fingerprint(self):
        if self._fingerprint is not None:
            return self._fingerprint

        listed = self._gpg(["--with-colons", "--list-secret-keys", self.key],
                           "no secret key for '%s'" % self.key)
        for line in listed.decode(errors="replace").split("\n"):
            fields = line.split(":")
            if fields[0] == "fpr" and len(fields) > 9 and len(fields[9]) > 0:
                self._fingerprint = fields[9]
                return self._fingerprint
        raise ValueError(
            "gpg found a secret key for '%s' but did not say its fingerprint"
            % self.key)

    # Keyring filename, named after the key's short id so it stays
    # identifiable next to other keyrings in the image.
    def keyring(self):
        return "%s.gpg" % self.fingerprint()[-8:]

    # Exports the public key as a keyring file, for apt's 'signed-by'.
    def export(self, path):
        with open(path, "wb") as f:
            f.write(self._gpg(["--export", self.fingerprint()],
                              "cannot export the public key for '%s'" % self.key))

    # Clearsigns a .dsc or .changes file in place. Signs to a temp file
    # first so a failure leaves the original unsigned, not half-signed.
    def clearsign(self, path):
        signed = "%s.seine-signing" % path
        try:
            with open(signed, "wb") as f:
                f.write(self._gpg(["--clearsign", "--output", "-", path],
                                  "cannot sign '%s'" % os.path.basename(path)))
            os.replace(signed, path)
        except:
            if os.path.isfile(signed):
                os.unlink(signed)
            raise

    # Signs a Release file both ways apt expects: detached Release.gpg
    # and combined InRelease.
    def sign_release(self, release):
        where = os.path.dirname(release)
        with open(os.path.join(where, "Release.gpg"), "wb") as f:
            f.write(self._gpg(
                ["--detach-sign", "--armor", "--output", "-", release],
                "cannot sign the repository index"))
        with open(os.path.join(where, "InRelease"), "wb") as f:
            f.write(self._gpg(["--clearsign", "--output", "-", release],
                              "cannot sign the repository index"))

    # --batch/--yes avoid an unattended prompt; --local-user uses the
    # resolved fingerprint so an ambiguous key name can't sign wrong.
    def _gpg(self, args, complaint):
        signing = args[0] in ["--clearsign", "--detach-sign"]
        command = ["gpg", "--batch", "--yes"]
        if signing:
            command += ["--local-user", self.fingerprint()]
        try:
            return subprocess.check_output(command + args,
                                           stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise ValueError(
                "signing was asked for but gpg is not installed on this "
                "machine. seine signs on the host so that no container ever "
                "sees the key.")
        except subprocess.CalledProcessError as e:
            said = (e.stderr or b"").decode(errors="replace").strip()
            raise ValueError("%s\n%s" % (complaint, said))

# Builds the signer for this build, or None. Key comes from options or
# SEINE_SIGN_KEY, not the spec, since who signs is per-machine.
def signer(options):
    key = options.get("sign_key") or os.environ.get("SEINE_SIGN_KEY")
    if key is None or len(key) == 0:
        return None
    return Signer(key)

# Signer for the vendor repository, kept separate from signer() so
# vendor packages (not built by seine) never share a key with ours.
def vendor_signer(options):
    key = options.get("vendor_sign_key") or os.environ.get("SEINE_VENDOR_SIGN_KEY")
    if key is None or len(key) == 0:
        return None
    return Signer(key)

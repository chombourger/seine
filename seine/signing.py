# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess

# Signing what a build produced, with a key seine never sees.
#
# Every gpg here runs on the machine seine was started on, not in any of
# the containers it builds in. That is the whole point: gpg talks to the
# agent the user already has running, the agent holds the key, and what
# crosses into a container is a signature and a public key. The builder is
# the most privileged container seine makes -- it runs build scripts
# fetched from an archive with the namespace privileges sbuild needs -- and
# handing it an agent socket would let anything it runs ask for a signature
# over anything at all.
#
# It follows that seine never needs the passphrase either: whether the
# agent asks for one, caches it, or holds the key on a smartcard, is
# between the user and their agent.
class Signer:
    def __init__(self, key):
        self.key = key
        self._fingerprint = None

    # The key as gpg knows it, from whatever was written on the command
    # line -- a short id, a long one, a fingerprint, an email address.
    # Asked once, and asked early: a key that is not there should stop a
    # build before it produces something it cannot sign.
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

    # What the keyring file is called, which is the key's own short id.
    # Named for the key rather than for seine: it is kept in the image, so
    # a keyring beside others should say which key it holds rather than
    # which tool put it there.
    #
    # The name is a label, not a credential -- what apt trusts is the
    # keyring's contents, which a short id being only 32 bits does not
    # weaken.
    def keyring(self):
        return "%s.gpg" % self.fingerprint()[-8:]

    # The public half, as apt wants to read it: a keyring rather than
    # armoured text, which is what 'signed-by' takes.
    def export(self, path):
        with open(path, "wb") as f:
            f.write(self._gpg(["--export", self.fingerprint()],
                              "cannot export the public key for '%s'" % self.key))

    # A .dsc and a .changes carry their signature inside them, wrapped
    # around the text they already were. Written beside the original and
    # renamed over it, so a signature that fails leaves the unsigned file
    # rather than half of a signed one.
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

    # A Release file is signed both ways, because apt looks for both: the
    # detached Release.gpg it has always read, and the InRelease that
    # carries the text and the signature in one file so there is no window
    # in which a mirror serves one without the other.
    def sign_release(self, release):
        where = os.path.dirname(release)
        with open(os.path.join(where, "Release.gpg"), "wb") as f:
            f.write(self._gpg(
                ["--detach-sign", "--armor", "--output", "-", release],
                "cannot sign the repository index"))
        with open(os.path.join(where, "InRelease"), "wb") as f:
            f.write(self._gpg(["--clearsign", "--output", "-", release],
                              "cannot sign the repository index"))

    # --batch and --yes so a build never stops on a prompt nobody is
    # watching; the agent still prompts for a passphrase if it needs one,
    # which is the agent's business and not this process's.
    #
    # --local-user picks the key by fingerprint rather than by whatever the
    # user wrote, so a name matching two keys signs with neither by
    # accident.
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

# The signer a build was asked for, or nothing. The key may be named on
# the command line or in the environment, so that a specification stays
# the same file for everyone who builds it: which key a machine signs with
# is a property of that machine and the person at it.
def signer(options):
    key = options.get("sign_key") or os.environ.get("SEINE_SIGN_KEY")
    if key is None or len(key) == 0:
        return None
    return Signer(key)

# The vendor repository's own signer, independent of signer() above. A
# package a 'vendor:' section pulled in was never built or audited by
# seine, unlike the 'packages:' repository signer() signs -- keeping the
# keys apart keeps that distinction visible to whatever trusts either
# repository's signature. No caller yet.
def vendor_signer(options):
    key = options.get("vendor_sign_key") or os.environ.get("SEINE_VENDOR_SIGN_KEY")
    if key is None or len(key) == 0:
        return None
    return Signer(key)

package pkcs7

import (
	"crypto/rsa"
	"crypto/x509"
	"encoding/asn1"
	"testing"
	"time"
)

func testPair(t *testing.T) (*rsa.PrivateKey, *x509.Certificate) {
	t.Helper()
	key, cert, err := Generate(2048, "test", 2)
	if err != nil {
		t.Fatal(err)
	}
	return key, cert
}

// Round trip through the storage encoding keeps a working pair.
func TestKeystoreRoundTrip(t *testing.T) {
	key, cert := testPair(t)
	keyPEM, err := EncodePrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	backKey, backCert, err := ParseKeyPair(keyPEM, EncodeCert(cert))
	if err != nil {
		t.Fatal(err)
	}
	if backKey.N.Cmp(key.N) != 0 || backCert.SerialNumber.Cmp(cert.SerialNumber) != 0 {
		t.Fatal("round trip changed the pair")
	}
}

func TestKeystoreRefusesMismatch(t *testing.T) {
	key, _ := testPair(t)
	_, otherCert := testPair(t)
	keyPEM, _ := EncodePrivateKey(key)
	if _, _, err := ParseKeyPair(keyPEM, EncodeCert(otherCert)); err == nil {
		t.Fatal("mismatched halves accepted")
	}
	if _, _, err := ParseKeyPair("not a key", "not a cert"); err == nil {
		t.Fatal("garbage accepted")
	}
}

// Same inputs sign identically: the determinism reproducible builds
// need, which ECDSA could never give.
func TestDetachedDeterministic(t *testing.T) {
	key, cert := testPair(t)
	first, err := SignDetached([]byte("module"), OIDSHA512, key, cert)
	if err != nil {
		t.Fatal(err)
	}
	second, err := SignDetached([]byte("module"), OIDSHA512, key, cert)
	if err != nil {
		t.Fatal(err)
	}
	if string(first) != string(second) {
		t.Fatal("same input signed differently")
	}
}

// The detached shape carries no certificates and no signed
// attributes, exactly like sign-file emits.
func TestDetachedShape(t *testing.T) {
	key, cert := testPair(t)
	der, err := SignDetached([]byte("module"), OIDSHA512, key, cert)
	if err != nil {
		t.Fatal(err)
	}
	var info contentInfoShape
	rest, err := asn1.Unmarshal(der, &info)
	if err != nil || len(rest) != 0 {
		t.Fatalf("unparsable ContentInfo: %s", err)
	}
	if !info.ContentType.Equal(OIDSignedData) {
		t.Fatal("not a SignedData")
	}
	var sd detachedShape
	if _, err := asn1.Unmarshal(info.Content.Bytes, &sd); err != nil {
		t.Fatalf("unparsable SignedData: %s", err)
	}
	if len(sd.Certificates) != 0 || len(sd.SignerInfos) != 1 {
		t.Fatal("unexpected certificates or signer count")
	}
	if len(sd.SignerInfos[0].AuthenticatedAttrs) != 0 {
		t.Fatal("detached signatures carry no authenticated attributes")
	}
}

type contentInfoShape struct {
	ContentType asn1.ObjectIdentifier
	Content     asn1.RawValue `asn1:"explicit,tag:0"`
}

type detachedShape struct {
	Version          int
	DigestAlgorithms []algorithmIdentifier `asn1:"set"`
	EncapContentInfo bareContent
	Certificates     []asn1.RawValue `asn1:"explicit,tag:0,optional"`
	SignerInfos      []struct {
		Version              int
		SID                  issuerAndSerial
		DigestAlgorithm      algorithmIdentifier
		AuthenticatedAttrs   []asn1.RawValue `asn1:"tag:0,optional"`
		DigestEncryptionAlgo algorithmIdentifier
		EncryptedDigest      []byte
	} `asn1:"set"`
}

// The Authenticode shape carries the certificate and four
// attributes; pinned timestamps sign identically.
func TestAuthenticodeDeterministic(t *testing.T) {
	key, cert := testPair(t)
	stamp := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	content := []byte("indirect-data")
	digest := []byte("01234567890123456789012345678901")
	first, err := SignAuthenticode(content, digest, cert, key, stamp, nil)
	if err != nil {
		t.Fatal(err)
	}
	second, err := SignAuthenticode(content, digest, cert, key, stamp, nil)
	if err != nil {
		t.Fatal(err)
	}
	if string(first) != string(second) {
		t.Fatal("same input and time signed differently")
	}
	var info contentInfoShape
	if _, err := asn1.Unmarshal(first, &info); err != nil {
		t.Fatalf("unparsable ContentInfo: %s", err)
	}
	var sd attachedShape
	if _, err := asn1.Unmarshal(info.Content.Bytes, &sd); err != nil {
		t.Fatalf("unparsable SignedData: %s", err)
	}
	if len(sd.Certificates.Bytes) == 0 || len(sd.SignerInfos) != 1 {
		t.Fatal("unexpected certificates or signer count")
	}
	if len(sd.SignerInfos[0].AuthenticatedAttrs) != 3 {
		t.Fatalf("want 3 authenticated attributes, got %d",
			len(sd.SignerInfos[0].AuthenticatedAttrs))
	}
}

type attachedShape struct {
	Version          int
	DigestAlgorithms []algorithmIdentifier `asn1:"set"`
	EncapContentInfo attachedContent
	Certificates     asn1.RawValue `asn1:"tag:0,optional"`
	SignerInfos      []struct {
		Version              int
		SID                  issuerAndSerial
		DigestAlgorithm      algorithmIdentifier
		AuthenticatedAttrs   []asn1.RawValue `asn1:"tag:0,optional"`
		DigestEncryptionAlgo algorithmIdentifier
		EncryptedDigest      []byte
	} `asn1:"set"`
}

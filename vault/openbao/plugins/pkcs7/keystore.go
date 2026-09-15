package pkcs7

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/pem"
	"fmt"
	"math/big"
	"time"
)

// SpcIndirectData for a file hash: what Authenticode signs. Bytes
// match sbsign field for field (empty flags, "<<<Obsolete>>>" file
// link), so the same input hashes identically on both sides.
func SpcIndirectData(fileHash []byte) ([]byte, error) {
	// Reproduces an sbsign quirk: it reads 28 bytes starting one byte
	// before the link string (zero-padded), dropping the last byte.
	// Verified byte-for-byte against real signed binaries.
	var strBytes []byte
	for _, c := range []byte("<<<Obsolete>>>") {
		strBytes = append(strBytes, c, 0)
	}
	strBytes = append([]byte{0x00}, strBytes[:len(strBytes)-1]...)
	inner, err := asn1.Marshal(asn1.RawValue{Class: 2, Tag: 0, Bytes: strBytes})
	if err != nil {
		return nil, err
	}
	mid, err := asn1.Marshal(asn1.RawValue{Class: 2, Tag: 2, IsCompound: true, Bytes: inner})
	if err != nil {
		return nil, err
	}
	fileLink, err := asn1.Marshal(asn1.RawValue{Class: 2, Tag: 0, IsCompound: true, Bytes: mid})
	if err != nil {
		return nil, err
	}
	type peImageData struct {
		Flags asn1.BitString
		File  asn1.RawValue
	}
	return asn1.Marshal(spcIndirectData{
		Data: spcAttributeAndValue{
			Type: asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 311, 2, 1, 15},
			Value: spcPeImageData{
				Flags: asn1.BitString{Bytes: []byte{}, BitLength: 0},
				File:  asn1.RawValue{FullBytes: fileLink},
			},
		},
		Digest: spcDigestInfo{
			Algorithm: algorithmIdentifier{Algorithm: OIDSHA256, Parameters: asn1.NullRawValue},
			Digest:    fileHash,
		},
	})
}

// A stored pair: the private key never leaves the server, the
// certificate goes out on generate/import and the cert endpoint.
type KeyPair struct {
	Key  *rsa.PrivateKey
	Cert *x509.Certificate
}

type spcDigestInfo struct {
	Algorithm algorithmIdentifier
	Digest    []byte
}

type spcIndirectData struct {
	Data   spcAttributeAndValue
	Digest spcDigestInfo
}

type spcAttributeAndValue struct {
	Type asn1.ObjectIdentifier
	// No [0] wrapper: sbsign emits the value as a plain SEQUENCE
	// and byte-identity with it needs the same.
	Value spcPeImageData
}

type spcPeImageData struct {
	Flags asn1.BitString
	File  asn1.RawValue
}

// Self-signed code-signing pair. Exact field parity with another tool
// never matters here since both sides share the certificate.
func Generate(bits int, commonName string, days int) (*rsa.PrivateKey, *x509.Certificate, error) {
	key, err := rsa.GenerateKey(rand.Reader, bits)
	if err != nil {
		return nil, nil, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, nil, err
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: commonName},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.AddDate(0, 0, days),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageCodeSigning},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, err
	}
	return key, cert, nil
}

// Parses a stored pair and refuses mismatched halves rather than
// storing garbage; RSA only, never ECDSA.
func ParseKeyPair(keyPEM, certPEM string) (*rsa.PrivateKey, *x509.Certificate, error) {
	keyBlock, _ := pem.Decode([]byte(keyPEM))
	if keyBlock == nil {
		return nil, nil, fmt.Errorf("not a PEM private key")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(keyBlock.Bytes)
	if err != nil {
		return nil, nil, fmt.Errorf("unreadable private key: %s", err)
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, nil, fmt.Errorf("only RSA keys are supported")
	}
	certBlock, _ := pem.Decode([]byte(certPEM))
	if certBlock == nil {
		return nil, nil, fmt.Errorf("not a PEM certificate")
	}
	cert, err := x509.ParseCertificate(certBlock.Bytes)
	if err != nil {
		return nil, nil, fmt.Errorf("unreadable certificate: %s", err)
	}
	pub, ok := cert.PublicKey.(*rsa.PublicKey)
	if !ok || pub.N.Cmp(key.N) != 0 {
		return nil, nil, fmt.Errorf("key and cert do not match")
	}
	return key, cert, nil
}

func EncodePrivateKey(key *rsa.PrivateKey) (string, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})), nil
}

func EncodeCert(cert *x509.Certificate) string {
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}))
}

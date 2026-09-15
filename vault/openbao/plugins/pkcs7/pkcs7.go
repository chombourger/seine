// Package pkcs7 builds detached CMS signatures for seine-sbsign (PE
// Authenticode) and seine-kmod (sign-file), byte-identical with the
// originals field for field. RSA only, never ECDSA.
package pkcs7

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/asn1"
	"fmt"
	"math/big"
	"time"
)

var (
	OIDData          = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 7, 1}
	OIDSignedData    = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 7, 2}
	OIDSHA256        = asn1.ObjectIdentifier{2, 16, 840, 1, 101, 3, 4, 2, 1}
	OIDSHA512        = asn1.ObjectIdentifier{2, 16, 840, 1, 101, 3, 4, 2, 3}
	OIDRSAEncryption = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 1, 1}
	OIDContentType   = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 9, 3}
	OIDMessageDigest = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 9, 4}
	OIDSigningTime   = asn1.ObjectIdentifier{1, 2, 840, 113549, 1, 9, 5}
	// What Authenticode signs (a hash list, for PE a single file hash).
	OIDSpcIndirectData = asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 311, 2, 1, 4}
)

// The hash sbsign and sign-file hash with, as Go knows it.
func hashFor(oid asn1.ObjectIdentifier) (crypto.Hash, error) {
	switch {
	case oid.Equal(OIDSHA256):
		return crypto.SHA256, nil
	case oid.Equal(OIDSHA512):
		return crypto.SHA512, nil
	default:
		return 0, fmt.Errorf("unsupported digest %s", oid)
	}
}

type algorithmIdentifier struct {
	Algorithm  asn1.ObjectIdentifier
	Parameters asn1.RawValue `asn1:"optional"`
}

type attribute struct {
	Type   asn1.ObjectIdentifier
	Values []asn1.RawValue `asn1:"set"`
}

// One attribute's DER: the encoded value wrapped in a
// single-element SET OF.
func marshalAttributeValue(oid asn1.ObjectIdentifier, encodedValue []byte) ([]byte, error) {
	return asn1.Marshal(attribute{
		Type:   oid,
		Values: []asn1.RawValue{{FullBytes: encodedValue}},
	})
}

// sbsign emits attributes in transmitted order (type, time, digest,
// capabilities), not DER SET OF order; byte-identity needs the same.
// Returns the SET OF form RSA signs and the [0] wire form.
func marshalAttributeSet(attrDERs ...[]byte) (set, wire, contents []byte) {
	for _, one := range attrDERs {
		contents = append(contents, one...)
	}
	set = append([]byte{0x31}, derLen(len(contents))...)
	set = append(set, contents...)
	wire = append([]byte{0xa0}, derLen(len(contents))...)
	wire = append(wire, contents...)
	return set, wire, contents
}

func derLen(n int) []byte {
	if n < 128 {
		return []byte{byte(n)}
	}
	var length []byte
	for v := n; v > 0; v >>= 8 {
		length = append([]byte{byte(v)}, length...)
	}
	return append([]byte{byte(0x80 | len(length))}, length...)
}

type issuerAndSerial struct {
	Issuer asn1.RawValue
	Serial *big.Int
}

// A [0] tag on a RawValue carrying content bytes is exactly the
// IMPLICIT [0] the spec writes; verified byte-for-byte.
type signerInfoDirect struct {
	Version              int
	SID                  issuerAndSerial
	DigestAlgorithm      algorithmIdentifier
	DigestEncryptionAlgo algorithmIdentifier
	EncryptedDigest      []byte
}

type signerInfoAttributed struct {
	Version              int
	SID                  issuerAndSerial
	DigestAlgorithm      algorithmIdentifier
	AuthenticatedAttrs   asn1.RawValue `asn1:"tag:0"`
	DigestEncryptionAlgo algorithmIdentifier
	EncryptedDigest      []byte
}

type bareContent struct {
	ContentType asn1.ObjectIdentifier
}

type attachedContent struct {
	ContentType asn1.ObjectIdentifier
	Content     asn1.RawValue `asn1:"tag:0"`
}

type detachedData struct {
	Version          int
	DigestAlgorithms []algorithmIdentifier `asn1:"set"`
	EncapContentInfo bareContent
	SignerInfos      []signerInfoDirect `asn1:"set"`
}

type attachedData struct {
	Version          int
	DigestAlgorithms []algorithmIdentifier `asn1:"set"`
	EncapContentInfo attachedContent
	Certificates     asn1.RawValue          `asn1:"tag:0"`
	SignerInfos      []signerInfoAttributed `asn1:"set"`
}

type detachedInfo struct {
	ContentType asn1.ObjectIdentifier
	Content     detachedData `asn1:"explicit,tag:0"`
}

type attachedInfo struct {
	ContentType asn1.ObjectIdentifier
	Content     attachedData `asn1:"explicit,tag:0"`
}

func sid(cert *x509.Certificate) issuerAndSerial {
	return issuerAndSerial{
		Issuer: asn1.RawValue{FullBytes: cert.RawIssuer},
		Serial: cert.SerialNumber,
	}
}

// Signs content with no authenticated attributes and no embedded
// certificates: the sign-file shape (direct RSA over the content
// hash). Returns the DER ContentInfo.
func SignDetached(content []byte, digestOID asn1.ObjectIdentifier, key *rsa.PrivateKey, cert *x509.Certificate) ([]byte, error) {
	hash, err := hashFor(digestOID)
	if err != nil {
		return nil, err
	}
	sum := hash.New()
	sum.Write(content)
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, hash, sum.Sum(nil))
	if err != nil {
		return nil, err
	}
	return asn1.Marshal(detachedInfo{
		ContentType: OIDSignedData,
		Content: detachedData{
			Version:          1,
			DigestAlgorithms: []algorithmIdentifier{{Algorithm: digestOID}},
			EncapContentInfo: bareContent{ContentType: OIDData},
			SignerInfos: []signerInfoDirect{{
				Version: 1,
				SID:     sid(cert),
				DigestAlgorithm: algorithmIdentifier{
					Algorithm: digestOID,
				},
				DigestEncryptionAlgo: algorithmIdentifier{
					Algorithm:  OIDRSAEncryption,
					Parameters: asn1.NullRawValue,
				},
				EncryptedDigest: signature,
			}},
		},
	})
}

// Signs with authenticated attributes and the signer certificate:
// the Authenticode shape. content is the DER the digest is taken
// over; smimeCaps is sbsign's pre-encoded capability list, or nil.
func SignAuthenticode(content, messageDigest []byte, cert *x509.Certificate, key *rsa.PrivateKey, signingTime time.Time, smimeCaps []byte) ([]byte, error) {
	spcOID, err := asn1.Marshal(OIDSpcIndirectData)
	if err != nil {
		return nil, err
	}
	contentType, err := marshalAttributeValue(OIDContentType, spcOID)
	if err != nil {
		return nil, err
	}
	md, err := asn1.Marshal(messageDigest)
	if err != nil {
		return nil, err
	}
	digest, err := marshalAttributeValue(OIDMessageDigest, md)
	if err != nil {
		return nil, err
	}
	stamp, err := asn1.Marshal(signingTime)
	if err != nil {
		return nil, err
	}
	when, err := marshalAttributeValue(OIDSigningTime, stamp)
	if err != nil {
		return nil, err
	}
	attrs := [][]byte{contentType, when, digest}
	if smimeCaps != nil {
		attrs = append(attrs, smimeCaps)
	}
	set, _, contents := marshalAttributeSet(attrs...)
	toSign := crypto.SHA256.New()
	toSign.Write(set)
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, toSign.Sum(nil))
	if err != nil {
		return nil, err
	}
	return asn1.Marshal(attachedInfo{
		ContentType: OIDSignedData,
		Content: attachedData{
			Version:          1,
			DigestAlgorithms: []algorithmIdentifier{{Algorithm: OIDSHA256, Parameters: asn1.NullRawValue}},
			EncapContentInfo: attachedContent{
				ContentType: OIDSpcIndirectData,
				Content:     asn1.RawValue{Class: 2, Tag: 0, IsCompound: true, Bytes: content},
			},
			Certificates: asn1.RawValue{Class: 2, Tag: 0, IsCompound: true, Bytes: cert.Raw},
			SignerInfos: []signerInfoAttributed{{
				Version: 1,
				SID:     sid(cert),
				DigestAlgorithm: algorithmIdentifier{
					Algorithm:  OIDSHA256,
					Parameters: asn1.NullRawValue,
				},
				AuthenticatedAttrs: asn1.RawValue{Class: 2, Tag: 0, IsCompound: true, Bytes: contents},
				DigestEncryptionAlgo: algorithmIdentifier{
					Algorithm:  OIDRSAEncryption,
					Parameters: asn1.NullRawValue,
				},
				EncryptedDigest: signature,
			}},
		},
	})
}

package main

import (
	"bytes"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"debug/pe"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"time"

	pkcs7 "seine-pkcs7"
)

// sbsign 0.9.4's static S/MIME capability list, as a pre-encoded
// attribute: byte-identity with sbsign needs its exact bytes.
const smimeCapsHex = "307906092a864886f70d01090f316c306a300b060960864801650304012a300b0609608648016503040116300b0609608648016503040102300a06082a864886f70d0307300e06082a864886f70d030202020080300d06082a864886f70d0302020140300706052b0e030207300d06082a864886f70d0302020128"

func smimeCapsDER() []byte {
	der, err := hex.DecodeString(smimeCapsHex)
	if err != nil {
		panic(err)
	}
	return der
}

// PE layout: the checksum field sits 88 bytes past e_lfanew on both
// 32- and 64-bit binaries; the certificate table entry 64 bytes past
// that on 32-bit, 80 on 64-bit.
func layout(peBytes []byte) (checksumOff, certOff int, err error) {
	if len(peBytes) < 0x40 || peBytes[0] != 'M' || peBytes[1] != 'Z' {
		return 0, 0, fmt.Errorf("not a PE binary")
	}
	e_lfanew := int(binary.LittleEndian.Uint32(peBytes[0x3c:0x40]))
	if len(peBytes) < e_lfanew+6 || peBytes[e_lfanew] != 'P' || peBytes[e_lfanew+1] != 'E' {
		return 0, 0, fmt.Errorf("not a PE binary")
	}
	magic := binary.LittleEndian.Uint16(peBytes[e_lfanew+4+20:])
	checksumOff = e_lfanew + 4 + 20 + 64
	switch magic {
	case 0x10b:
		certOff = e_lfanew + 4 + 20 + 128
	case 0x20b:
		certOff = e_lfanew + 4 + 20 + 144
	default:
		return 0, 0, fmt.Errorf("unknown PE magic %#x", magic)
	}
	if len(peBytes) < certOff+8 {
		return 0, 0, fmt.Errorf("truncated PE headers")
	}
	return checksumOff, certOff, nil
}

// Strips any existing signature: zeroes the certificate table entry
// and truncates the file at its offset. Signatures live at the end,
// so anything past the entry is signature, never program.
func stripSignature(peBytes []byte) ([]byte, error) {
	_, certOff, err := layout(peBytes)
	if err != nil {
		return nil, err
	}
	addr := int(binary.LittleEndian.Uint32(peBytes[certOff:]))
	size := int(binary.LittleEndian.Uint32(peBytes[certOff+4:]))
	if addr == 0 && size == 0 {
		return peBytes, nil
	}
	if addr+size != len(peBytes) {
		return nil, fmt.Errorf("unsupported layout: signature not at end of file")
	}
	stripped := append([]byte(nil), peBytes[:addr]...)
	for i := certOff; i < certOff+8; i++ {
		stripped[i] = 0
	}
	return stripped, nil
}

// SHA256 over the file per the Authenticode rule: everything except
// the checksum field, the certificate table entry, and the table
// itself (sections by content, headers to SizeOfHeaders).
func fileHash(peBytes []byte) ([]byte, error) {
	checksumOff, certOff, err := layout(peBytes)
	if err != nil {
		return nil, err
	}
	f, err := pe.NewFile(bytes.NewReader(peBytes))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var headersEnd uint32
	switch header := f.OptionalHeader.(type) {
	case *pe.OptionalHeader32:
		headersEnd = header.SizeOfHeaders
	case *pe.OptionalHeader64:
		headersEnd = header.SizeOfHeaders
	default:
		return nil, fmt.Errorf("unknown optional header")
	}
	h := sha256.New()
	h.Write(peBytes[:checksumOff])
	h.Write(peBytes[checksumOff+4 : certOff])
	h.Write(peBytes[certOff+8 : headersEnd])
	for _, section := range f.Sections {
		if section.Size == 0 {
			continue
		}
		if section.Offset+section.Size > uint32(len(peBytes)) {
			return nil, fmt.Errorf("section past end of file")
		}
		h.Write(peBytes[section.Offset : section.Offset+section.Size])
	}
	return h.Sum(nil), nil
}

// The checksum over the signed file: everything but the checksum
// field itself, signature data included, plus the file size.
func updateChecksum(signed []byte) error {
	checksumOff, _, err := layout(signed)
	if err != nil {
		return err
	}
	var sum uint32
	add := func(word uint16) {
		sum += uint32(word)
		sum = (sum & 0xffff) + (sum >> 16)
	}
	for i := 0; i < len(signed); i += 2 {
		if i >= checksumOff && i < checksumOff+4 {
			continue
		}
		if i+1 < len(signed) {
			add(uint16(signed[i]) | uint16(signed[i+1])<<8)
		} else {
			add(uint16(signed[i]))
		}
	}
	sum += uint32(len(signed))
	binary.LittleEndian.PutUint32(signed[checksumOff:], sum)
	return nil
}

// Signs a PE binary end to end: strip, hash, sign with an explicit
// timestamp, append the certificate table, refresh the checksum.
func signPE(peBytes []byte, key *rsa.PrivateKey, cert *x509.Certificate, signingTime time.Time) ([]byte, error) {
	stripped, err := stripSignature(peBytes)
	if err != nil {
		return nil, err
	}
	hash, err := fileHash(stripped)
	if err != nil {
		return nil, err
	}
	spc, err := pkcs7.SpcIndirectData(hash)
	if err != nil {
		return nil, err
	}
	// sbsign hashes the SpcIndirectData without its SEQUENCE header
	// (short form only, which ours always is); the transmitted DER
	// keeps it, byte-identically.
	if len(spc) < 2 || spc[0] != 0x30 || spc[1] >= 0x80 {
		return nil, fmt.Errorf("unexpected SpcIndirectData form")
	}
	messageDigest := crypto.SHA256.New()
	messageDigest.Write(spc[2:])
	cms, err := pkcs7.SignAuthenticode(spc, messageDigest.Sum(nil), cert, key, signingTime, smimeCapsDER())
	if err != nil {
		return nil, err
	}
	signed := append([]byte(nil), stripped...)
	certAddr := len(signed)
	// dwLength covers the header plus the CMS alone; the entry size
	// covers the alignment padding too, like sbsign writes them.
	total := 8 + len(cms)
	aligned := total
	if aligned%8 != 0 {
		aligned += 8 - aligned%8
	}
	var header [8]byte
	binary.LittleEndian.PutUint32(header[0:], uint32(total))
	binary.LittleEndian.PutUint16(header[4:], 0x0200)
	binary.LittleEndian.PutUint16(header[6:], 0x0002)
	signed = append(signed, header[:]...)
	signed = append(signed, cms...)
	signed = append(signed, make([]byte, aligned-total)...)
	_, certOff, err := layout(signed)
	if err != nil {
		return nil, err
	}
	binary.LittleEndian.PutUint32(signed[certOff:], uint32(certAddr))
	binary.LittleEndian.PutUint32(signed[certOff+4:], uint32(aligned))
	return signed, updateChecksum(signed)
}

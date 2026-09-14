package main

import (
	"bytes"
	"context"
	"crypto"
	"encoding/base64"
	"time"

	"github.com/ProtonMail/go-crypto/openpgp"
	"github.com/ProtonMail/go-crypto/openpgp/clearsign"
	"github.com/ProtonMail/go-crypto/openpgp/packet"
	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

// Inserts the CRC24 checksum line armor carries (RFC 4880 6.3) into
// a signature block that has none. Pure bytes in, bytes out.
func withArmorChecksum(signed []byte) []byte {
	const begin = "-----BEGIN PGP SIGNATURE-----\n"
	const end = "\n-----END PGP SIGNATURE-----"
	start := bytes.LastIndex(signed, []byte(begin))
	at := bytes.LastIndex(signed, []byte(end))
	if start < 0 || at < 0 || start >= at {
		return signed
	}
	var encoded []byte
	for _, line := range bytes.Split(signed[start+len(begin):at], []byte("\n")) {
		trimmed := bytes.TrimSpace(line)
		if len(trimmed) == 0 || bytes.HasPrefix(trimmed, []byte("=")) {
			continue
		}
		encoded = append(encoded, trimmed...)
	}
	raw := make([]byte, base64.StdEncoding.DecodedLen(len(encoded)))
	n, err := base64.StdEncoding.Decode(raw, encoded)
	if err != nil {
		return signed
	}
	var sum [3]byte
	crc := uint32(0xB704CE)
	for _, b := range raw[:n] {
		crc ^= uint32(b) << 16
		for i := 0; i < 8; i++ {
			crc <<= 1
			if crc&0x1000000 != 0 {
				crc ^= 0x1864CFB
			}
		}
	}
	crc &= 0xFFFFFF
	sum[0], sum[1], sum[2] = byte(crc>>16), byte(crc>>8), byte(crc)
	var out bytes.Buffer
	out.Write(signed[:at])
	out.WriteByte('\n')
	out.WriteByte('=')
	out.WriteString(base64.StdEncoding.EncodeToString(sum[:]))
	out.Write(signed[at:])
	return out.Bytes()
}

// Signs with an explicit creation time: signatures embed it, so only
// a pinned clock keeps rebuilds byte-identical.
func handleSign(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	deterministic := false
	name := data.Get("name").(string)
	mode, _ := data.Get("mode").(string)
	if mode != "clearsign" && mode != "detach-sign" {
		return logical.ErrorResponse("mode shall be 'clearsign' or 'detach-sign'"), nil
	}
	entity, err := loadKey(ctx, req.Storage, name)
	if err != nil {
		return nil, err
	}
	encoded, _ := data.Get("data_base64").(string)
	payload, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || encoded == "" {
		return logical.ErrorResponse("data_base64 shall be base64"), nil
	}
	stamp, _ := data.Get("timestamp").(string)
	creation, err := time.Parse(time.RFC3339, stamp)
	if err != nil {
		return logical.ErrorResponse("timestamp shall be RFC3339"), nil
	}
	// A signature cannot predate its key; gpg refuses the same, but
	// says so more clearly than "no valid signing keys" does.
	if creation.Before(entity.PrimaryKey.CreationTime) {
		return logical.ErrorResponse("timestamp predates the key's creation"), nil
	}
	config := &packet.Config{
		DefaultHash: crypto.SHA256,
		Time:        func() time.Time { return creation },
		// Deterministic v4 signatures: the pinned timestamp above is
		// what keeps rebuilds byte-identical, and a random salt
		// notation would undo exactly that.
		NonDeterministicSignaturesViaNotation: &deterministic,
	}

	if mode == "clearsign" {
		// Clearsign hashes one line break past the payload, so an
		// already newline-terminated payload would sign a trailing
		// blank line that gpgv rejects. Close() re-adds the newline.
		payload = bytes.TrimSuffix(payload, []byte("\n"))
		var out bytes.Buffer
		signer, err := clearsign.Encode(&out, entity.PrivateKey, config)
		if err != nil {
			return nil, err
		}
		if _, err := signer.Write(payload); err != nil {
			return nil, err
		}
		if err := signer.Close(); err != nil {
			return nil, err
		}
		// Armor must end in a newline: without it gpg reports a good
		// signature yet still fails, and so would apt on InRelease.
		signed := out.Bytes()
		if !bytes.HasSuffix(signed, []byte("\n")) {
			signed = append(signed, '\n')
		}
		// Encode skips the checksum line gpg emits; gpgv rejects
		// armor without one, so add it back deterministically.
		signed = withArmorChecksum(signed)
		return &logical.Response{
			Data: map[string]interface{}{
				"signed_data": base64.StdEncoding.EncodeToString(signed),
			},
		}, nil
	}

	var out bytes.Buffer
	if err := openpgp.ArmoredDetachSign(&out, entity, bytes.NewReader(payload), config); err != nil {
		return nil, err
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"signature": base64.StdEncoding.EncodeToString(out.Bytes()),
		},
	}, nil
}

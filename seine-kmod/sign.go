package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"

	pkcs7 "seine-pkcs7"
)

// Appended after the signature: signer metadata the kernel reads
// before the magic. Zeros bar the PKCS7 id type and the CMS length,
// exactly like sign-file writes it.
func signatureInfo(cmsLen int) []byte {
	info := make([]byte, 12)
	info[2] = 2
	binary.BigEndian.PutUint32(info[8:], uint32(cmsLen))
	return info
}

// Strips any previous signature first, so unsigned out-of-tree
// modules and already-signed in-tree modules take the same path.
func handleSign(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	name := data.Get("name").(string)
	pair, err := loadKey(ctx, req.Storage, name)
	if err != nil {
		return nil, err
	}
	encoded, _ := data.Get("ko_base64").(string)
	ko, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || encoded == "" {
		return logical.ErrorResponse("ko_base64 shall be base64"), nil
	}
	if i := bytes.Index(ko, moduleMagic); i >= 0 {
		ko = ko[:i]
	}
	cms, err := pkcs7.SignDetached(ko, pkcs7.OIDSHA512, pair.Key, pair.Cert)
	if err != nil {
		return nil, err
	}
	var signed bytes.Buffer
	signed.Write(ko)
	signed.Write(cms)
	signed.Write(signatureInfo(len(cms)))
	signed.Write(moduleMagic)
	return &logical.Response{
		Data: map[string]interface{}{
			"signed_ko_base64": base64.StdEncoding.EncodeToString(signed.Bytes()),
		},
	}, nil
}

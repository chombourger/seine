package main

import (
	"context"
	"fmt"
	"strings"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"

	pkcs7 "seine-pkcs7"
)

const defaultSubject = "seine kmod dev throwaway key"

// Appended after the signature: the kernel finds the magic at the
// end, reads the info struct before it, and parses the CMS by length.
var moduleMagic = []byte("~Module signature appended~\n")

// Key name -> stored PEM pair. Private halves never leave storage;
// the only outbound key material is the public certificate.
func loadKey(ctx context.Context, s logical.Storage, name string) (*pkcs7.KeyPair, error) {
	entry, err := s.Get(ctx, "key/"+name)
	if err != nil {
		return nil, err
	}
	if entry == nil {
		return nil, logical.CodedError(404, fmt.Sprintf("no such key '%s'", name))
	}
	var stored storedKey
	if err := entry.DecodeJSON(&stored); err != nil {
		return nil, err
	}
	key, cert, err := pkcs7.ParseKeyPair(stored.Key, stored.Cert)
	if err != nil {
		return nil, err
	}
	return &pkcs7.KeyPair{Key: key, Cert: cert}, nil
}

type storedKey struct {
	Key  string `json:"key"`
	Cert string `json:"cert"`
}

func storeKey(ctx context.Context, s logical.Storage, name string, pair *pkcs7.KeyPair) error {
	keyPEM, err := pkcs7.EncodePrivateKey(pair.Key)
	if err != nil {
		return err
	}
	entry, err := logical.StorageEntryJSON("key/"+name, storedKey{
		Key:  keyPEM,
		Cert: pkcs7.EncodeCert(pair.Cert),
	})
	if err != nil {
		return err
	}
	return s.Put(ctx, entry)
}

// Subject comes as "/CN=..." like openssl writes it; anything else
// is refused rather than guessed at.
func subjectName(subject string) (string, error) {
	if subject == "" {
		return defaultSubject, nil
	}
	name, found := strings.CutPrefix(subject, "/CN=")
	if !found || strings.ContainsAny(name, "/,") || name == "" {
		return "", fmt.Errorf("subject shall be '/CN=name'")
	}
	return name, nil
}

func handleKeyMgmt(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	name := data.Get("name").(string)
	if _, err := loadKey(ctx, req.Storage, name); err == nil {
		if overwrite, _ := data.GetOk("overwrite"); overwrite != true {
			return logical.ErrorResponse("key '%s' exists", name), nil
		}
	} else if coded, ok := err.(logical.HTTPCodedError); !ok || coded.Code() != 404 {
		return nil, err
	}

	generate, hasGenerate := data.GetOk("generate")
	imported, hasImport := data.GetOk("import")
	switch {
	case hasGenerate && hasImport:
		return logical.ErrorResponse("want 'generate' or 'import', not both"), nil
	case hasGenerate:
		pair, certPEM, err := generateKey(generate.(map[string]interface{}))
		if err != nil {
			return nil, err
		}
		if err := storeKey(ctx, req.Storage, name, pair); err != nil {
			return nil, err
		}
		return &logical.Response{
			Data: map[string]interface{}{"cert_pem": certPEM},
		}, nil
	case hasImport:
		params, ok := imported.(map[string]interface{})
		if !ok {
			return logical.ErrorResponse("want 'import': {'key_pem', 'cert_pem'}"), nil
		}
		keyPEM, _ := params["key_pem"].(string)
		certPEM, _ := params["cert_pem"].(string)
		if keyPEM == "" || certPEM == "" {
			return logical.ErrorResponse("want 'import': {'key_pem', 'cert_pem'}"), nil
		}
		key, cert, err := pkcs7.ParseKeyPair(keyPEM, certPEM)
		if err != nil {
			return nil, logical.CodedError(400, err.Error())
		}
		if err := storeKey(ctx, req.Storage, name, &pkcs7.KeyPair{Key: key, Cert: cert}); err != nil {
			return nil, err
		}
		return &logical.Response{
			Data: map[string]interface{}{"cert_pem": certPEM},
		}, nil
	default:
		return logical.ErrorResponse("want 'generate' or 'import'"), nil
	}
}

func generateKey(params map[string]interface{}) (*pkcs7.KeyPair, string, error) {
	bits := 4096
	if raw, ok := params["key_bits"]; ok && raw != nil {
		size, ok := raw.(float64)
		if !ok {
			return nil, "", logical.CodedError(400, "key_bits shall be a number")
		}
		bits = int(size)
	}
	if bits != 2048 && bits != 3072 && bits != 4096 {
		return nil, "", logical.CodedError(400, "key_bits shall be 2048, 3072 or 4096")
	}
	subject, _ := params["subject"].(string)
	name, err := subjectName(subject)
	if err != nil {
		return nil, "", logical.CodedError(400, err.Error())
	}
	days := 36500
	if raw, ok := params["days"]; ok && raw != nil {
		lifetime, ok := raw.(float64)
		if !ok || lifetime < 1 {
			return nil, "", logical.CodedError(400, "days shall be a positive number")
		}
		days = int(lifetime)
	}
	key, cert, err := pkcs7.Generate(bits, name, days)
	if err != nil {
		return nil, "", err
	}
	return &pkcs7.KeyPair{Key: key, Cert: cert}, pkcs7.EncodeCert(cert), nil
}

func handleCert(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	pair, err := loadKey(ctx, req.Storage, data.Get("name").(string))
	if err != nil {
		return nil, err
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"cert_pem": pkcs7.EncodeCert(pair.Cert),
		},
	}, nil
}

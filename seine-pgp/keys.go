package main

import (
	"bytes"
	"context"
	"crypto"
	"fmt"
	"strings"
	"time"

	"github.com/ProtonMail/go-crypto/openpgp"
	"github.com/ProtonMail/go-crypto/openpgp/armor"
	"github.com/ProtonMail/go-crypto/openpgp/packet"
	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

const (
	// No parentheses or commas: go-crypto rejects them in User IDs.
	defaultKeyName  = "seine repo dev throwaway key"
	defaultKeyEmail = "seine-dev@example.invalid"
)

type storedKey struct {
	Private string `json:"private"`
}

// Loads a named key or answers 404: sign paths never mint, and only
// explicit generate/import calls create keys.
func loadKey(ctx context.Context, s logical.Storage, name string) (*openpgp.Entity, error) {
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
	return parsePrivate(stored.Private)
}

func storeKey(ctx context.Context, s logical.Storage, name string, entity *openpgp.Entity) error {
	var buf bytes.Buffer
	w, err := armor.Encode(&buf, "PGP PRIVATE KEY BLOCK", nil)
	if err != nil {
		return err
	}
	if err := entity.SerializePrivate(w, nil); err != nil {
		w.Close()
		return err
	}
	w.Close()
	entry, err := logical.StorageEntryJSON("key/"+name, storedKey{Private: buf.String()})
	if err != nil {
		return err
	}
	return s.Put(ctx, entry)
}

func fingerprint(entity *openpgp.Entity) string {
	return fmt.Sprintf("%X", entity.PrimaryKey.Fingerprint)
}

func exportPublic(entity *openpgp.Entity) (string, error) {
	var buf bytes.Buffer
	w, err := armor.Encode(&buf, "PGP PUBLIC KEY BLOCK", nil)
	if err != nil {
		return "", err
	}
	if err := entity.Serialize(w); err != nil {
		w.Close()
		return "", err
	}
	w.Close()
	return buf.String(), nil
}

// Parses an armored private key and refuses anything without private
// material; a test signature proves the halves match and work.
func parsePrivate(privateArmor string) (*openpgp.Entity, error) {
	block, err := armor.Decode(strings.NewReader(privateArmor))
	if err != nil {
		return nil, logical.CodedError(400, fmt.Sprintf("bad armor: %s", err))
	}
	if block.Type != "PGP PRIVATE KEY BLOCK" {
		return nil, logical.CodedError(400, "not a private key block")
	}
	entity, err := openpgp.ReadEntity(packet.NewReader(block.Body))
	if err != nil {
		return nil, logical.CodedError(400, fmt.Sprintf("unreadable key: %s", err))
	}
	if entity.PrivateKey == nil {
		return nil, logical.CodedError(400, "key has no private half")
	}
	if err := testSign(entity); err != nil {
		return nil, logical.CodedError(400, fmt.Sprintf("key cannot sign: %s", err))
	}
	return entity, nil
}

func testSign(entity *openpgp.Entity) error {
	config := &packet.Config{DefaultHash: crypto.SHA256, Time: time.Now}
	var signed bytes.Buffer
	if err := openpgp.DetachSign(&signed, entity, bytes.NewReader([]byte("probe")), config); err != nil {
		return err
	}
	_, err := openpgp.CheckDetachedSignature(openpgp.EntityList{entity}, bytes.NewReader([]byte("probe")), &signed, config)
	return err
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
		entity, err := generateKey(generate.(map[string]interface{}))
		if err != nil {
			return nil, err
		}
		if err := storeKey(ctx, req.Storage, name, entity); err != nil {
			return nil, err
		}
		return keyResponse(entity)
	case hasImport:
		params, ok := imported.(map[string]interface{})
		if !ok {
			return logical.ErrorResponse("want 'import': {'private_key': ...}"), nil
		}
		private, _ := params["private_key"].(string)
		if private == "" {
			return logical.ErrorResponse("want 'import': {'private_key': ...}"), nil
		}
		entity, err := parsePrivate(private)
		if err != nil {
			return nil, err
		}
		if err := storeKey(ctx, req.Storage, name, entity); err != nil {
			return nil, err
		}
		return keyResponse(entity)
	default:
		return logical.ErrorResponse("want 'generate' or 'import'"), nil
	}
}

func keyResponse(entity *openpgp.Entity) (*logical.Response, error) {
	public, err := exportPublic(entity)
	if err != nil {
		return nil, err
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"fingerprint": fingerprint(entity),
			"public_key":  public,
		},
	}, nil
}

func generateKey(params map[string]interface{}) (*openpgp.Entity, error) {
	name, _ := params["name"].(string)
	if name == "" {
		name = defaultKeyName
	}
	email, _ := params["email"].(string)
	if email == "" {
		email = defaultKeyEmail
	}
	bits := 3072
	if keyType, _ := params["key_type"].(string); keyType != "" {
		switch keyType {
		case "rsa2048":
			bits = 2048
		case "rsa3072":
			bits = 3072
		case "rsa4096":
			bits = 4096
		default:
			return nil, logical.CodedError(400, fmt.Sprintf("unknown key_type '%s'", keyType))
		}
	}
	if expire, _ := params["expire"].(string); expire != "" && expire != "0" {
		return nil, logical.CodedError(400, "only non-expiring keys are supported")
	}
	return openpgp.NewEntity(name, "", email, &packet.Config{RSABits: bits, Time: time.Now})
}

func handlePublic(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	entity, err := loadKey(ctx, req.Storage, data.Get("name").(string))
	if err != nil {
		return nil, err
	}
	public, err := exportPublic(entity)
	if err != nil {
		return nil, err
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"public_key": public,
		},
	}, nil
}

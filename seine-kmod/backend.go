package main

import (
	"context"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

// Contract pinned by vault-plugins/kmod/poc.py. Sign paths never
// mint: unknown keys answer 404, only generate/import create keys.
func Factory(ctx context.Context, conf *logical.BackendConfig) (logical.Backend, error) {
	return Backend(), nil
}

func Backend() *framework.Backend {
	return &framework.Backend{
		Paths: []*framework.Path{
			pathKeys(),
			pathCert(),
			pathSign(),
		},
		Secrets:     []*framework.Secret{},
		BackendType: logical.TypeLogical,
	}
}

func pathKeys() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
			"generate": {
				Type:        framework.TypeMap,
				Description: "Mint a new keypair in the vault",
			},
			"import": {
				Type:        framework.TypeMap,
				Description: "Store an existing keypair in the vault",
			},
			"overwrite": {
				Type:        framework.TypeBool,
				Description: "Replace a key that already exists",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.CreateOperation: &framework.PathOperation{
				Callback: handleKeyMgmt,
			},
			logical.UpdateOperation: &framework.PathOperation{
				Callback: handleKeyMgmt,
			},
		},
	}
}

func pathCert() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)/cert",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.ReadOperation: &framework.PathOperation{
				Callback: handleCert,
			},
		},
	}
}

func pathSign() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)/sign",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
			"ko_base64": {
				Type:        framework.TypeString,
				Description: "Base64 of the kernel module to sign",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.CreateOperation: &framework.PathOperation{
				Callback: handleSign,
			},
			logical.UpdateOperation: &framework.PathOperation{
				Callback: handleSign,
			},
		},
	}
}

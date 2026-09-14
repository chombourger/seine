package main

import (
	"context"
	"encoding/base64"
	"time"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

// Signs with an explicit creation time: signatures embed it, so only
// a pinned clock keeps rebuilds byte-identical. Without one the time
// is now, like sbsign unwrapped in faketime.
func handleSign(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	name := data.Get("name").(string)
	pair, err := loadKey(ctx, req.Storage, name)
	if err != nil {
		return nil, err
	}
	encoded, _ := data.Get("pe_base64").(string)
	peBytes, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || encoded == "" {
		return logical.ErrorResponse("pe_base64 shall be base64"), nil
	}
	signingTime := time.Now()
	if stamp, _ := data.Get("signing_time").(string); stamp != "" {
		signingTime, err = time.Parse(time.RFC3339, stamp)
		if err != nil {
			return logical.ErrorResponse("signing_time shall be RFC3339"), nil
		}
	}
	signed, err := signPE(peBytes, pair.Key, pair.Cert, signingTime)
	if err != nil {
		return nil, logical.CodedError(400, err.Error())
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"signed_pe_base64": base64.StdEncoding.EncodeToString(signed),
		},
	}, nil
}

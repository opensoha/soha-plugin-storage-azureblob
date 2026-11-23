package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/container"
	"github.com/opensoha/soha/contracts/plugins/storagedriver"
)

const cursorLifetime = 15 * time.Minute

func (d azureDriver) ListPrefix(
	ctx context.Context, req storagedriver.ListPrefixRequest,
) (storagedriver.ListPrefixResult, error) {
	client, err := connect(req.Binding)
	if err != nil {
		return storagedriver.ListPrefixResult{}, err
	}
	providerPrefix := req.Binding.Prefix + req.Prefix
	scope := strings.Join([]string{req.Binding.ID, client.endpoint, req.Binding.Prefix, req.Prefix}, "\x00")
	marker, err := d.openCursor(scope, req.Cursor, time.Now())
	if err != nil {
		return storagedriver.ListPrefixResult{}, err
	}

	limit := int32(req.Limit)
	options := &container.ListBlobsFlatOptions{Prefix: &providerPrefix, MaxResults: &limit}
	if marker != "" {
		options.Marker = &marker
	}
	page, err := client.NewListBlobsFlatPager(options).NextPage(ctx)
	if err != nil {
		var response *azcore.ResponseError
		if marker != "" && errors.As(err, &response) && response.StatusCode == http.StatusBadRequest {
			return storagedriver.ListPrefixResult{}, rpcError(storagedriver.CodeInvalidCursor)
		}
		return storagedriver.ListPrefixResult{}, providerError(err)
	}
	result := storagedriver.ListPrefixResult{Keys: []string{}}
	if page.Segment != nil {
		for _, item := range page.Segment.BlobItems {
			if item == nil || item.Name == nil || !strings.HasPrefix(*item.Name, providerPrefix) {
				return storagedriver.ListPrefixResult{}, rpcError(storagedriver.CodeInternal)
			}
			result.Keys = append(result.Keys, strings.TrimPrefix(*item.Name, req.Binding.Prefix))
		}
	}
	if page.NextMarker != nil && *page.NextMarker != "" {
		if *page.NextMarker == marker {
			return storagedriver.ListPrefixResult{}, rpcError(storagedriver.CodeInvalidCursor)
		}
		result.NextCursor = d.sealCursor(scope, *page.NextMarker, time.Now())
	}
	if result.Validate(req) != nil {
		return storagedriver.ListPrefixResult{}, rpcError(storagedriver.CodeInternal)
	}
	return result, nil
}

func (d azureDriver) sealCursor(scope, marker string, now time.Time) string {
	payload := binary.BigEndian.AppendUint64(nil, uint64(now.Add(cursorLifetime).Unix()))
	payload = append(payload, marker...)
	token := append(payload, d.signCursor(scope, payload)...)
	return base64.RawURLEncoding.EncodeToString(token)
}

func (d azureDriver) openCursor(scope, token string, now time.Time) (string, error) {
	if token == "" {
		return "", nil
	}
	invalid := rpcError(storagedriver.CodeInvalidCursor)
	if len(token) > storagedriver.MaxCursorBytes {
		return "", invalid
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(token)
	if err != nil || len(raw) <= 8+sha256.Size {
		return "", invalid
	}
	payload, signature := raw[:len(raw)-sha256.Size], raw[len(raw)-sha256.Size:]
	if !hmac.Equal(signature, d.signCursor(scope, payload)) ||
		int64(binary.BigEndian.Uint64(payload)) <= now.Unix() {
		return "", invalid
	}
	return string(payload[8:]), nil
}

func (d azureDriver) signCursor(scope string, payload []byte) []byte {
	mac := hmac.New(sha256.New, d.cursorKey[:])
	mac.Write([]byte(scope))
	mac.Write([]byte{0})
	mac.Write(payload)
	return mac.Sum(nil)
}

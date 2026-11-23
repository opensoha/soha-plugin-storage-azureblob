package main

import (
	"context"
	"crypto/rand"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/azcore/streaming"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blob"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/bloberror"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blockblob"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/container"
	"github.com/opensoha/soha/contracts/plugins/storagedriver"
)

const probeOwnerField = "soha_probe_owner"

func (azureDriver) TestConnection(ctx context.Context, binding storagedriver.Binding) (err error) {
	client, err := connect(binding)
	if err != nil {
		return err
	}
	key := binding.Prefix + "_soha_probe_" + rand.Text()
	if len(key) > storagedriver.MaxKeyBytes {
		return rpcError(storagedriver.CodeInvalidRequest)
	}
	limit := int32(1)
	pager := client.NewListBlobsFlatPager(&container.ListBlobsFlatOptions{Prefix: &binding.Prefix, MaxResults: &limit})
	if _, err := pager.NextPage(ctx); err != nil {
		return providerError(err)
	}

	probe := client.NewBlockBlobClient(key)
	anyETag := azcore.ETag("*")
	ifAbsent := &blob.AccessConditions{
		ModifiedAccessConditions: &blob.ModifiedAccessConditions{IfNoneMatch: &anyETag},
	}
	// Check delete permission without deleting anything. A token without
	// permission to clean up must never create a probe.
	if _, err := probe.Delete(ctx, &blob.DeleteOptions{AccessConditions: ifAbsent}); err != nil &&
		!bloberror.HasCode(err, bloberror.BlobNotFound) {
		return providerError(err)
	}
	owner := rand.Text()
	response, uploadErr := probe.Upload(ctx, streaming.NopCloser(strings.NewReader("soha storage probe")), &blockblob.UploadOptions{
		Metadata:         map[string]*string{probeOwnerField: &owner},
		AccessConditions: ifAbsent,
	})
	if uploadErr != nil && isRejectedWrite(uploadErr) {
		return providerError(uploadErr)
	}
	// A lost upload response can hide a successful commit. Cleanup only an
	// object whose ETag or ownership metadata proves it is this call's probe.
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		err = errors.Join(err, removeProbe(cleanupCtx, probe, owner, response.ETag))
	}()
	if uploadErr != nil {
		return providerError(uploadErr)
	}
	if response.ETag == nil {
		return rpcError(storagedriver.CodeInternal)
	}
	_, readErr := probe.GetProperties(ctx, &blob.GetPropertiesOptions{AccessConditions: ifMatch(response.ETag)})
	return providerError(readErr)
}

func isRejectedWrite(err error) bool {
	var response *azcore.ResponseError
	return errors.As(err, &response) && response.StatusCode >= http.StatusBadRequest &&
		response.StatusCode < http.StatusInternalServerError && response.StatusCode != http.StatusRequestTimeout
}

func removeProbe(ctx context.Context, probe *blockblob.Client, owner string, etag *azcore.ETag) error {
	if etag == nil {
		properties, err := probe.GetProperties(ctx, nil)
		if bloberror.HasCode(err, bloberror.BlobNotFound) {
			return nil
		}
		if err != nil {
			return providerError(err)
		}
		if !ownedProbe(properties.Metadata, owner) || properties.ETag == nil {
			return rpcError(storagedriver.CodeBusy)
		}
		etag = properties.ETag
	}
	_, err := probe.Delete(ctx, &blob.DeleteOptions{AccessConditions: ifMatch(etag)})
	if bloberror.HasCode(err, bloberror.BlobNotFound) {
		return nil
	}
	return providerError(err)
}

func ownedProbe(metadata map[string]*string, owner string) bool {
	for key, value := range metadata {
		if strings.EqualFold(key, probeOwnerField) && value != nil && *value == owner {
			return true
		}
	}
	return false
}

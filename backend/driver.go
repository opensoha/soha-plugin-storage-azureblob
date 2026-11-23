package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"io"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blob"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/bloberror"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blockblob"
	"github.com/opensoha/soha/contracts/plugins/storagedriver"
)

type azureDriver struct {
	cursorKey [32]byte
}

var _ storagedriver.Driver = azureDriver{}

func (azureDriver) PutObject(
	ctx context.Context, req storagedriver.PutObjectRequest, body io.Reader,
) (storagedriver.PutObjectResult, error) {
	client, err := connect(req.Binding)
	if err != nil {
		return storagedriver.PutObjectResult{}, err
	}
	source := &uploadBody{ctx: ctx, reader: body, digest: sha256.New()}
	response, err := client.NewBlockBlobClient(req.Binding.Prefix+req.Key).
		UploadStream(ctx, source, &blockblob.UploadStreamOptions{
			BlockSize:   4 << 20,
			Concurrency: 2,
			HTTPHeaders: &blob.HTTPHeaders{BlobContentType: &req.ContentType},
		})
	if err != nil {
		return storagedriver.PutObjectResult{}, providerError(err)
	}
	if err := ctx.Err(); err != nil {
		return storagedriver.PutObjectResult{}, providerError(err)
	}
	if source.err != io.EOF || response.ETag == nil {
		return storagedriver.PutObjectResult{}, rpcError(storagedriver.CodeInternal)
	}
	return storagedriver.PutObjectResult{
		Size:   source.size,
		SHA256: hex.EncodeToString(source.digest.Sum(nil)),
		ETag:   string(*response.ETag),
	}, nil
}

type uploadBody struct {
	ctx    context.Context
	reader io.Reader
	digest hash.Hash
	size   int64
	err    error
}

func (b *uploadBody) Read(p []byte) (int, error) {
	if err := b.ctx.Err(); err != nil {
		return 0, err
	}
	if b.err != nil {
		return 0, b.err
	}
	n, err := b.reader.Read(p)
	b.size += int64(n)
	b.digest.Write(p[:n])
	b.err = err
	if n > 0 {
		// A block read can discard a simultaneous error when n fills the buffer.
		return n, nil
	}
	return n, err
}

func (azureDriver) OpenObject(
	ctx context.Context, req storagedriver.OpenObjectRequest,
) (storagedriver.OpenObjectResult, error) {
	client, err := connect(req.Binding)
	if err != nil {
		return storagedriver.OpenObjectResult{}, err
	}
	target := client.NewBlobClient(req.Binding.Prefix + req.Key)
	if req.IfNoneMatch == "" && req.Range == "" {
		return download(ctx, target, nil, nil)
	}

	// Azure supplies the current size and tag. Resolve the protocol's weak
	// comparison and suffix ranges here, then pin the selected representation.
	properties, err := target.GetProperties(ctx, nil)
	if err != nil {
		return storagedriver.OpenObjectResult{}, providerError(err)
	}
	if properties.ETag == nil || properties.ContentLength == nil || *properties.ContentLength < 0 {
		return storagedriver.OpenObjectResult{}, rpcError(storagedriver.CodeInternal)
	}
	etag := string(*properties.ETag)
	if storagedriver.MatchesIfNoneMatch(req.IfNoneMatch, etag) {
		return storagedriver.OpenObjectResult{ETag: etag, NotModified: true}, nil
	}
	span, err := storagedriver.ResolveByteRange(req.Range, *properties.ContentLength)
	if err != nil {
		return storagedriver.OpenObjectResult{}, err
	}
	return download(ctx, target, properties.ETag, span)
}

func download(
	ctx context.Context, target *blob.Client, expected *azcore.ETag, span *storagedriver.ContentRange,
) (storagedriver.OpenObjectResult, error) {
	options := &blob.DownloadStreamOptions{}
	if expected != nil {
		options.AccessConditions = ifMatch(expected)
	}
	if span != nil {
		options.Range = blob.HTTPRange{Offset: span.Start, Count: span.End - span.Start + 1}
	}
	response, err := target.DownloadStream(ctx, options)
	if err != nil {
		return storagedriver.OpenObjectResult{}, providerError(err)
	}
	if !validDownload(response, expected, span) {
		if response.Body != nil {
			_ = response.Body.Close()
		}
		return storagedriver.OpenObjectResult{}, rpcError(storagedriver.CodeInternal)
	}
	return storagedriver.OpenObjectResult{
		ETag:          string(*response.ETag),
		ContentType:   *response.ContentType,
		ContentLength: *response.ContentLength,
		ContentRange:  span,
		Body:          blobBody{ReadCloser: response.Body},
	}, nil
}

func validDownload(
	response blob.DownloadStreamResponse, expected *azcore.ETag, span *storagedriver.ContentRange,
) bool {
	if response.Body == nil || response.ContentType == nil || response.ETag == nil ||
		(expected != nil && *response.ETag != *expected) ||
		response.ContentLength == nil || *response.ContentLength < 0 {
		return false
	}
	if span == nil {
		return response.ContentRange == nil
	}
	wantRange := fmt.Sprintf("bytes %d-%d/%d", span.Start, span.End, span.Size)
	return response.ContentRange != nil && *response.ContentRange == wantRange &&
		*response.ContentLength == span.End-span.Start+1
}

func ifMatch(etag *azcore.ETag) *blob.AccessConditions {
	return &blob.AccessConditions{
		ModifiedAccessConditions: &blob.ModifiedAccessConditions{IfMatch: etag},
	}
}

type blobBody struct{ io.ReadCloser }

func (b blobBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if err == io.EOF {
		return n, err
	}
	return n, providerError(err)
}

func (b blobBody) Close() error {
	return providerError(b.ReadCloser.Close())
}

func (azureDriver) DeleteKeys(ctx context.Context, req storagedriver.DeleteKeysRequest) error {
	client, err := connect(req.Binding)
	if err != nil {
		return err
	}
	deleted := make(map[string]struct{}, len(req.Keys))
	for _, key := range req.Keys {
		if _, seen := deleted[key]; seen {
			continue
		}
		_, err := client.NewBlobClient(req.Binding.Prefix+key).Delete(ctx, nil)
		if err != nil && !bloberror.HasCode(err, bloberror.BlobNotFound) {
			return providerError(err)
		}
		deleted[key] = struct{}{}
	}
	return nil
}

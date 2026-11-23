package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/azcore/policy"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/container"
	"github.com/opensoha/soha/contracts/plugins/storagedriver"
)

var (
	accountName   = regexp.MustCompile(`^[a-z0-9]{3,24}$`)
	containerName = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`)
	azureHTTP     = newAzureHTTPClient()
)

type settings struct {
	Account  string `json:"account"`
	Endpoint string `json:"endpoint"`
}

type boundContainer struct {
	*container.Client
	endpoint string // Canonical container URL without SAS.
}

func newAzureHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func connect(binding storagedriver.Binding) (boundContainer, error) {
	var config settings
	decoder := json.NewDecoder(bytes.NewReader(binding.Settings))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&config) != nil ||
		!containerName.MatchString(binding.Container) || strings.Contains(binding.Container, "--") {
		return boundContainer{}, rpcError(storagedriver.CodeInvalidRequest)
	}

	endpoint, err := accountEndpoint(config)
	if err != nil {
		return boundContainer{}, err
	}
	query, err := containerSAS(binding.Credentials)
	if err != nil {
		return boundContainer{}, err
	}
	endpoint.Path += "/" + binding.Container
	scope := endpoint.String()
	endpoint.RawQuery = query.Encode()
	client, err := container.NewClientWithNoCredential(endpoint.String(), &container.ClientOptions{
		ClientOptions: azcore.ClientOptions{
			Transport: azureHTTP,
			Retry:     policy.RetryOptions{MaxRetries: -1},
		},
	})
	if err != nil {
		return boundContainer{}, rpcError(storagedriver.CodeInternal)
	}
	return boundContainer{Client: client, endpoint: scope}, nil
}

func accountEndpoint(config settings) (url.URL, error) {
	invalid := rpcError(storagedriver.CodeInvalidRequest)
	if !accountName.MatchString(config.Account) {
		return url.URL{}, invalid
	}
	azure := url.URL{Scheme: "https", Host: config.Account + ".blob.core.windows.net"}
	switch config.Endpoint {
	case "", azure.String(), azure.String() + "/":
		return azure, nil
	}
	if config.Account != "devstoreaccount1" {
		return url.URL{}, invalid
	}

	emulator, err := url.Parse(config.Endpoint)
	if err != nil || emulator.Scheme != "http" || emulator.Opaque != "" || emulator.User != nil ||
		emulator.ForceQuery || emulator.RawQuery != "" || emulator.Fragment != "" || emulator.RawPath != "" {
		return url.URL{}, invalid
	}
	port, err := strconv.ParseUint(emulator.Port(), 10, 16)
	if err != nil || port == 0 || (emulator.Hostname() != "127.0.0.1" && emulator.Hostname() != "::1") {
		return url.URL{}, invalid
	}
	const accountPath = "/devstoreaccount1"
	switch emulator.Path {
	case "", "/", accountPath, accountPath + "/":
		return url.URL{Scheme: "http", Host: emulator.Host, Path: accountPath}, nil
	default:
		return url.URL{}, invalid
	}
}

// Forward only SAS fields. Azure operation selectors and response overrides
// cannot be supplied through the credential reference.
func containerSAS(credentials map[string]string) (url.Values, error) {
	invalid := rpcError(storagedriver.CodeInvalidRequest)
	if len(credentials) != 1 {
		return nil, invalid
	}
	query, err := url.ParseQuery(strings.TrimPrefix(credentials["sas_token"], "?"))
	if err != nil || query.Get("sr") != "c" || query.Get("sv") == "" || query.Get("sig") == "" {
		return nil, invalid
	}
	for key, values := range query {
		if len(values) != 1 || values[0] == "" {
			return nil, invalid
		}
		switch key {
		case "sv", "sr", "sig", "sp", "st", "se", "spr", "sip", "si", "ses",
			"skoid", "sktid", "skt", "ske", "sks", "skv", "saoid", "suoid", "scid", "sduoid", "skdutid":
		default:
			return nil, invalid
		}
	}
	return query, nil
}

func rpcError(code storagedriver.ErrorCode) storagedriver.RPCError {
	return storagedriver.RPCError{Code: code}
}

func providerError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return rpcError(storagedriver.CodeDeadlineExceeded)
	}
	var response *azcore.ResponseError
	if !errors.As(err, &response) {
		return rpcError(storagedriver.CodeUnavailable)
	}
	switch response.StatusCode {
	case http.StatusBadRequest:
		return rpcError(storagedriver.CodeInvalidRequest)
	case http.StatusUnauthorized, http.StatusForbidden:
		return rpcError(storagedriver.CodeAccessDenied)
	case http.StatusNotFound:
		return rpcError(storagedriver.CodeNotFound)
	case http.StatusConflict, http.StatusPreconditionFailed, http.StatusTooManyRequests:
		return rpcError(storagedriver.CodeBusy)
	case http.StatusRequestEntityTooLarge:
		return rpcError(storagedriver.CodeTooLarge)
	case http.StatusRequestedRangeNotSatisfiable:
		return rpcError(storagedriver.CodeRangeNotSatisfiable)
	case http.StatusGatewayTimeout:
		return rpcError(storagedriver.CodeDeadlineExceeded)
	default:
		return rpcError(storagedriver.CodeUnavailable)
	}
}

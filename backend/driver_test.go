package main

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/opensoha/soha/contracts/plugins/storagedriver"
)

func TestAccountEndpoint(t *testing.T) {
	t.Parallel()
	tests := []struct {
		account, endpoint, want string
	}{
		{"testacct", "", "https://testacct.blob.core.windows.net"},
		{"testacct", "https://testacct.blob.core.windows.net/", "https://testacct.blob.core.windows.net"},
		{"devstoreaccount1", "http://127.0.0.1:10000/devstoreaccount1/", "http://127.0.0.1:10000/devstoreaccount1"},
		{"devstoreaccount1", "http://[::1]:10000", "http://[::1]:10000/devstoreaccount1"},
		{"devstoreaccount1", "http://localhost:10000", ""},
		{"devstoreaccount1", "http://127.0.0.1:10000/other", ""},
		{"devstoreaccount1", "http://127.0.0.1:10000/?sig=value", ""},
		{"devstoreaccount1", "http://user@127.0.0.1:10000/", ""},
		{"devstoreaccount1", "http://127.0.0.1:65536/", ""},
		{"testacct", "http://127.0.0.1:10000", ""},
		{"testacct", "https://other.blob.core.windows.net", ""},
		{"testacct", "https://testacct.blob.core.windows.net:443", ""},
		{"x.example", "", ""},
	}
	for _, test := range tests {
		got, err := accountEndpoint(settings{Account: test.account, Endpoint: test.endpoint})
		if test.want == "" {
			if !errors.Is(err, rpcError(storagedriver.CodeInvalidRequest)) {
				t.Errorf("accountEndpoint(%q, %q) error = %v", test.account, test.endpoint, err)
			}
			continue
		}
		if err != nil || got.String() != test.want {
			t.Errorf("accountEndpoint(%q, %q) = %q, %v; want %q", test.account, test.endpoint, got.String(), err, test.want)
		}
	}
}

func TestContainerSASAndBlobURL(t *testing.T) {
	t.Parallel()
	for _, token := range []string{
		"sv=2025-11-05&sr=b&sig=synthetic",
		"sv=2025-11-05&sr=c&sig=synthetic&ss=b",
		"sv=2025-11-05&sr=c&sig=synthetic&sig=duplicate",
		"sv=2025-11-05&sr=c&sig=synthetic&comp=list",
		"sv=2025-11-05&sr=c&sig=synthetic&versionid=unsafe",
	} {
		if _, err := containerSAS(map[string]string{"sas_token": token}); !errors.Is(err, rpcError(storagedriver.CodeInvalidRequest)) {
			t.Errorf("unsafe SAS rejected with %v", err)
		}
	}
	binding := testBinding("http://127.0.0.1:10000")
	client, err := connect(binding)
	if err != nil {
		t.Fatal(err)
	}
	key := "release/目录/file %2F?&+#.bin"
	requestURL, err := url.Parse(client.NewBlobClient(key).URL())
	if err != nil {
		t.Fatal("invalid provider URL")
	}
	if requestURL.Path != "/devstoreaccount1/sample/"+key || requestURL.Query().Get("sig") != "synthetic+/=" {
		t.Fatal("provider URL changed the blob name or SAS")
	}
}

func TestCursorScope(t *testing.T) {
	t.Parallel()
	driver := azureDriver{cursorKey: [32]byte{1}}
	otherDriver := azureDriver{cursorKey: [32]byte{2}}
	now := time.Unix(1234, 0)
	const marker = "provider marker + 女"
	token := driver.sealCursor("binding/prefix", marker, now)
	if got, err := driver.openCursor("binding/prefix", token, now); err != nil || got != marker {
		t.Fatalf("cursor marker = %q, %v", got, err)
	}
	for _, open := range []func() (string, error){
		func() (string, error) { return driver.openCursor("other/prefix", token, now) },
		func() (string, error) { return driver.openCursor("binding/other", token, now) },
		func() (string, error) { return driver.openCursor("binding/prefix", token[1:], now) },
		func() (string, error) { return driver.openCursor("binding/prefix", token, now.Add(cursorLifetime)) },
		func() (string, error) { return otherDriver.openCursor("binding/prefix", token, now) },
	} {
		if _, err := open(); !errors.Is(err, rpcError(storagedriver.CodeInvalidCursor)) {
			t.Errorf("invalid cursor rejected with %v", err)
		}
	}
}

func TestUploadReaderPreservesErrorAtBlockBoundary(t *testing.T) {
	t.Parallel()
	sourceErr := errors.New("read failed")
	reader := &uploadBody{
		ctx:    t.Context(),
		reader: &finalReadError{err: sourceErr},
		digest: sha256.New(),
	}
	var block [1]byte
	if n, err := reader.Read(block[:]); n != len(block) || err != nil {
		t.Fatalf("read returned %d, %v", n, err)
	}
	if n, err := reader.Read(block[:]); n != 0 || !errors.Is(err, sourceErr) {
		t.Fatalf("terminal read returned %d, %v", n, err)
	}
}

type finalReadError struct{ err error }

func (r *finalReadError) Read(p []byte) (int, error) {
	p[0] = 'x'
	return 1, r.err
}

func TestDownloadRejectsRedirect(t *testing.T) {
	t.Parallel()
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("redirect target received a request")
	}))
	t.Cleanup(target.Close)
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	t.Cleanup(source.Close)
	_, err := (azureDriver{}).OpenObject(t.Context(), storagedriver.OpenObjectRequest{Binding: testBinding(source.URL), Key: "blob"})
	if !errors.Is(err, rpcError(storagedriver.CodeUnavailable)) {
		t.Errorf("redirect returned %v", err)
	}
}

func TestProbeCannotCreateWithoutDeletePermission(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			w.Header().Set("Content-Type", "application/xml")
			io.WriteString(w, "<EnumerationResults><Blobs/><NextMarker/></EnumerationResults>")
		case http.MethodDelete:
			if r.Header.Get("If-None-Match") != "*" {
				t.Error("delete permission check lacked its absence condition")
			}
			w.Header().Set("x-ms-error-code", "AuthorizationPermissionMismatch")
			w.WriteHeader(http.StatusForbidden)
		default:
			t.Errorf("unexpected provider method %s", r.Method)
			w.WriteHeader(http.StatusBadRequest)
		}
	}))
	t.Cleanup(server.Close)
	if err := (azureDriver{}).TestConnection(t.Context(), testBinding(server.URL)); !errors.Is(err, rpcError(storagedriver.CodeAccessDenied)) {
		t.Errorf("TestConnection returned %v", err)
	}
}

func testBinding(endpoint string) storagedriver.Binding {
	data, _ := json.Marshal(settings{Account: "devstoreaccount1", Endpoint: endpoint})
	return storagedriver.Binding{
		ID:          "test-binding",
		Container:   "sample",
		Prefix:      "test/",
		Settings:    data,
		Credentials: map[string]string{"sas_token": "?sv=2025-11-05&sr=c&sig=synthetic%2B%2F%3D"},
	}
}

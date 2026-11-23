# Azure Blob Storage for Soha

[简体中文](./README-cn.md)

This plugin uses the Microsoft Azure SDK for Go to connect Soha to an existing Azure Blob Storage container. It uses Soha’s standard storage connection form and implements streaming upload, full and ranged downloads, ETag validation, prefix paging, and exact-key deletion.

## Configure a connection

Install and enable a signed `opensoha.azure-blob` release on a Soha host that supports local executable storage drivers. Add a storage connection and choose **Azure Blob Storage**.

| Field | Value |
| --- | --- |
| Container | Name of an existing container. |
| Storage account | Azure storage account name. |
| Object prefix | Optional scope within the container. Soha sends a slash-terminated prefix to the driver. |
| Endpoint | Leave blank for public Azure. The driver uses `https://<account>.blob.core.windows.net`. |
| Container SAS token | Select a Soha secret reference containing the query string of a container SAS (`sr=c`). |

Grant the SAS read, create, write, delete, and list permissions for the selected container. Use a short expiry and HTTPS-only SAS on Azure. The token may begin with `?`. **Test Connection** checks list and delete authorization before creating a unique probe in the configured scope. It checks the new blob and deletes it by ETag. [Microsoft container SAS documentation](https://learn.microsoft.com/en-us/rest/api/storageservices/create-service-sas)

For local development, use account `devstoreaccount1`, a dedicated Azurite container, an HTTP-enabled container SAS, and `http://127.0.0.1:10000/devstoreaccount1` or an equivalent literal IPv6 loopback URL and port. Run Azurite on the plugin host. Older Azurite builds may need `--skipApiVersionCheck` to accept the SDK’s service API version. [Microsoft Azurite documentation](https://learn.microsoft.com/en-us/azure/storage/common/storage-install-azurite)

List cursors expire after 15 minutes or a plugin restart. Start enumeration again if a cursor expires. Exact-key deletion uses Azure’s normal delete operation, so the container’s configured retention rules still apply.

## Build and test

Use Go 1.27.4 and the author tools from the unified Soha plugin template.

```sh
(cd backend && GOWORK=off go test -mod=readonly ./...)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python plugin.py build --platform linux/amd64 --platform linux/arm64
```

The driver uses the published Contracts SDK v0.2.3. The Go module can be built independently with the public Go proxy and checksum database.

The build embeds the plugin identity and records the asset sizes and digests in the manifest. Follow the [Soha plugin template release instructions](https://github.com/opensoha/soha-plugin-template#sign-and-verify-a-release) to commit the generated manifest, sign the exact assets, and verify them with a trusted publisher key.

Licensed under [Apache-2.0](./LICENSE).

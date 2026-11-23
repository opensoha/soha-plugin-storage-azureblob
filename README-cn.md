# Soha Azure Blob Storage 插件

[English](./README.md)

本插件使用 Microsoft Azure Go SDK，将 Soha 连接到已有的 Azure Blob Storage 容器。通过 Soha 标准表单配置连接，支持流式上传、全量或范围下载、ETag 条件读取、前缀分页和按对象键删除。

## 配置连接

在支持本地可执行存储驱动的 Soha 主机上安装并启用签名版 `opensoha.azure-blob`，然后创建存储连接并选择 **Azure Blob Storage**。

| 字段 | 值 |
| --- | --- |
| 容器 | 已有的 Azure Blob 容器名。 |
| 存储账户 | Azure 存储账户名。 |
| 对象前缀 | 可选的容器内范围。Soha 会向驱动传递以 `/` 结尾的非空前缀。 |
| 端点 | 公共 Azure 留空即可，驱动使用 `https://<account>.blob.core.windows.net`。 |
| 容器 SAS 令牌 | 选择包含容器级 SAS 查询字符串（`sr=c`）的 Soha 凭据引用。 |

为所选容器授予 SAS 的读取、创建、写入、删除和列举权限。生产环境使用短有效期、限定 HTTPS 的 SAS。令牌可带前导 `?`。点击 **测试连接** 后，驱动先确认列举和删除权限，再在连接范围内创建唯一探针，读取其属性并按 ETag 清理。[Microsoft 容器 SAS 文档](https://learn.microsoft.com/zh-cn/rest/api/storageservices/create-service-sas)

本地使用 Azurite 时，账户使用 `devstoreaccount1`，准备专用容器与允许 HTTP 的容器 SAS。端点可填 `http://127.0.0.1:10000/devstoreaccount1`，也可指定 IPv6 回环地址和端口。Azurite 应运行在插件宿主机上。较旧版本可能需要 `--skipApiVersionCheck` 以接受 SDK 的服务 API 版本。[Microsoft Azurite 文档](https://learn.microsoft.com/zh-cn/azure/storage/common/storage-install-azurite)

分页游标在 15 分钟后或插件重启后失效。失效时从第一页重新列举。按对象键删除遵循 Azure 容器的保留策略。

## 构建与测试

使用 Go 1.27.4 和统一 Soha 插件模板的作者工具。

```sh
(cd backend && GOWORK=off go test -mod=readonly ./...)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python plugin.py build --platform linux/amd64 --platform linux/arm64
```

驱动使用已发布的 Contracts SDK v0.2.3，可以通过公开的 Go 模块代理和校验库独立构建。

构建命令将插件身份注入二进制，并在 manifest 中记录制品大小与摘要。按 [Soha 插件模板发行说明](https://github.com/opensoha/soha-plugin-template/blob/main/README-cn.md) 提交生成的 manifest，签名并使用可信发布者公钥验签。

采用 [Apache-2.0 许可](./LICENSE)。

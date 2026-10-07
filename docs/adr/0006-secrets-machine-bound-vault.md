# ADR-0006：密钥默认用机器绑定加密 vault，而非明文配置或仅环境变量

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[配置参考](../reference/config.md)、[模型与鉴权参考](../reference/models.md)

## 背景（Context）

LLM 后端 token、`gh`/`aws` 等 CLI 的凭据需要一个安全的取得方式。三种典型做法：

1. **明文配置**——把密钥直接写进 `config.json`，简单但极易泄露（误提交、误入 trace/日志）。
2. **仅环境变量**——无状态、CI/容器友好，但本地长期开发需每次手动 `export`，体验差。
3. **机器绑定加密 vault**——密钥以 AES-256-GCM 加密落 `~/.autoverse/secrets.enc`，master_key 由机器指纹（machine-id + 用户名）派生。

`Config` 里只存密钥的**别名引用**（`api_key_alias`），绝不出现明文。问题是默认取哪种存储形态。

## 决策（Decision）

**默认 `VaultSecretProvider`（机器绑定 AES-256-GCM vault），获取时 env 优先、vault 兜底；`SecretProvider` 抽象为 Port，vault 只是其一实现。** 纯无状态部署可注入 `EnvOnlyProvider` 彻底关掉 vault。

## 理由（Rationale）

- 两种场景并存：本地长期开发需要"密钥安全静置磁盘、无需每次手动 export"，vault 解决它；CI/容器需要"env 注入、无状态"，故 `get` 永远 **env 优先**、vault 仅在 env 缺失时兜底——容器环境根本不碰 vault。
- 反方担心的"机器绑定在迁移/容器重建时解密失败比朴素 env 更脆弱"由 fail-safe 消化：解密 GCM 校验失败时 vault 视为空、env-only 继续、**不崩溃**，并给出重建步骤。
- Port 抽象当下即有两个真实实现（`VaultSecretProvider` + 降级用 `EnvOnlyProvider`），并非纯为未来 KMS 买单；窄 Port 让降级路径对调用方透明。

## 后果（Consequences）

- 本地开发零手动 export、密钥加密静置；CI/容器走 env 注入、零额外状态。
- 机器绑定意味着 `.enc` 拷到另一台机器无法解密——这是**特性**（防密钥文件被盗用），代价是迁移需重建（删 `.enc` 后用 `/secret save <KEY> <VALUE>` 重写，或改用 env 注入）。
- 多一个加密/解密与信封版本迁移的实现面，但被 Port 隔离、不外溢到调用方。

## 可证伪边界（何时重新审视）

- 若部署环境完全无本地持久态（纯无状态容器），应直接注入 `EnvOnlyProvider` 关掉 vault——vault 是默认而非强制。
- 若 `SecretProvider` 永远只有 vault 一个实现被实例化（连 `EnvOnlyProvider` 降级都不存在），则 Port 抽象属过度设计，应退回具体类。

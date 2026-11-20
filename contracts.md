# Nightly 模块分发契约（contracts.md）

本文件是 `Dounai_nightly` 仓的唯一契约。本契约定义版本号、签名密钥、源实装与自动打包的形状。启动器仓的 `contracts/` 是下游消费者：本契约约定的值必须能在不改启动器冻结契约的前提下交付。正式分发前本契约不冻结；改字段须同步启动器并重新验证。

## 1. 范围与产物位置

nightly 仓同时承担产物托管与编排中心。产物走 `raw.githubusercontent.com`，不启用 GitHub Pages。

- catalog：`https://raw.githubusercontent.com/zhipianxiaokonglong/Dounai_nightly/main/catalog.json`
- 模块包：`https://raw.githubusercontent.com/zhipianxiaokonglong/Dounai_nightly/main/packages/<文件名>.zip`

选 raw 地址的原因：白名单后缀 `githubusercontent.com` 已在启动器 `GITHUB_HOST_SUFFIXES` 中，零代码改动。Pages 域名 `github.io` 不在白名单，需要改启动器。两个域名的连接稳定性相近，不为稳定性单独改白名单。

`official-gh` 保持隐藏。`official-gh` 指向的仓库尚未建立，与 nightly 是两个独立的源，不共用 id、不共用水位、互不影响。

## 2. 版本号

版本号用点分日期 `YYYY.M.D`，例如 `2026.11.20`。

这个形状满足启动器既有的 SemVer 三段校验，无需改 `module-v2.schema.json`、`catalog-v2.schema.json` 或 `semver_cmp`。三个段都是十进制整数且不带前导零；`semver_cmp` 按数值比较，日期递增就是版本递增。变体由 `variant` 单独声明，固定为 `nightly`。

约束：同一天只发布一次。版本单调要求新版本严格大于旧版本，同日重复构建得到相同版本号，不会被判为更新。当天已有构建时，流水线跳过发布，不重签 catalog。

## 3. 密钥与签名

nightly 用独立的两把密钥，不复用官方 K1 与 K5。

| 密钥 | 用途 | 公钥落点 | 私钥落点 |
|---|---|---|---|
| K3 | 签模块包内 `manifest.json` | 启动器 trust store 内置表（A 类） | GitHub Secrets + Environment protection |
| K4 | 签 nightly catalog | 内置 nightly 源的 `catalog_pubkey` | GitHub Secrets + Environment protection |

生成方式沿用启动器的 `pack keygen`，算法 Ed25519，指纹为公钥 32 字节的 SHA-256。私钥生成一次后本地离线备份，只把密文注入 Secrets。K3 只签包，K4 只签 catalog，不得混用。

内置 K3 表示 nightly 包判 A 类、免确认自动安装。这带来明确风险：拿到 K3 私钥的人可以签出被判 A 类的假包。四条硬约束限制这个风险，缺一不可：

1. catalog 的 `expires` 设 1 天，超时 fail-closed，不给假包留长窗口。
2. 每个条目写 `signature_required=true`，删签名段的包直接硬拒。
3. 每个条目写 `expected_signer_fp` 为 K3 指纹，签名者与期望不符时硬拒。
4. K3 私钥只在 nightly 仓的受保护环境可读，泄露即按第 7 节轮换。

K3 泄露后靠 `expires` 与轮换止损，不靠用户逐次确认。用户一旦安装 nightly，即接受这个信任模型。

## 4. 源实装

启动器新增一个内置源，形状写进 `sources.rs`：

| 字段 | 值 |
|---|---|
| id | `nightly-gh` |
| name | 官方 Nightly（GitHub） |
| kind | `official` |
| base_url | `https://raw.githubusercontent.com/zhipianxiaokonglong/Dounai_nightly/main` |
| catalog_pubkey | K4 公钥 |
| catalog_fingerprint | K4 指纹 |
| host_suffixes | `github.com`、`githubusercontent.com` |
| deletable | `false` |
| auto_update | `false` |
| group | `nightly-gh`，独立于 `official` |

源公钥内置，因此 nightly 源随新版启动器下发，用户无需粘贴邀请码。社区源与邀请码机制不受影响。

`group` 独立表示 nightly 的 `sequence` 与官方源不比较。跨源同名模块允许并存，`active` 槽由用户显式切换，nightly 不抢官方实例的位置。

## 5. 模块清单与扩展

流水线读仓根 `modules.json`。每条记录声明一个待打包模块：

| 字段 | 含义 |
|---|---|
| `repo` | 上游仓库，格式 `owner/repo` |
| `module_id` | 与 manifest 一致，格式 `publisher/name` |
| `role` | 启动器五值枚举之一 |
| `variant` | 固定 `nightly` |
| `port` | 规范端口 |
| `health` | health 路径 |
| `deps_kind` | `uv_lock` 或 `pyproject` |

新增模块只加一条记录，不改 workflow 逻辑。清单驱动打包矩阵，`pack.exe` 在 workflow 内 checkout 启动器仓后 `cargo build --bin pack` 产出，不预编译入库。

## 6. 自动打包逻辑

`nightly.yml` 每天固定时刻由 `schedule` 触发，同时开放 `workflow_dispatch` 供手动补发。

每次运行按以下顺序：

1. 读 `modules.json`，得到模块清单。
2. 逐仓取默认分支最新 commit sha。
3. 与 `state/last-built.json` 的上次 sha 比对，筛出有更新的模块。
4. 无模块更新时结束运行，不重签 catalog。
5.  checkout 有更新的模块仓，生成 `uv.lock`，写 v2 manifest，`pack` 用 K3 签名产出 zip。
6. 把 zip 提交到 `packages/`，更新 `last-built.json` 的对应 sha。
7. 重签 catalog：`sequence` 加一，`generated_at` 取当前 UTC，`expires` 取 +1 天，逐条重算哈希，用 K4 签名并自验。
8. commit 并 push 本仓产物。

增量比对以仓库 commit sha 为准，不以包哈希为准。上游改了文件但没改版本号时，日期版本号仍不变，同日不会二次发布；跨日有提交则版本号随日期递增，不会撞单调校验。

## 7. 轮换与撤销

K3 与 K4 都在启动器内置，泄露后旧版启动器不会自动停止信任，必须发新版 exe。

K3 泄露时：生成新 K3，替换启动器 trust store 的内置指纹，重签全部 nightly 包，发新版启动器，仓库外公告旧指纹。K4 泄露时：生成新 K4，替换内置 nightly 源的公钥与指纹，重签 catalog，发新版启动器。

两把密钥泄露都不等同于官方 K1 或 K5 泄露。nightly 是独立源，撤销该源或升级启动器即可切断；官方源与 SourceForge 分发不受影响。

## 8. 校验与门禁

nightly 仓的规则由本文件承载，不复制启动器的 `contracts/validate.mjs`。修改本契约后，核对下列项全部成立：

1. 版本号样例通过启动器的 `module-v2.schema.json` 与 `catalog-v2.schema.json`。
2. catalog 条目通过签发侧 `preflight_catalog` 的源 id 预检。
3. nightly 源 id 不与 `official-gh` 或 `official-sf` 冲突。
4. 本文件按 `stc check contracts.md --profile for-document --anti-echo` 检查，无硬性需修改项。

## 9. 变更规则

- 版本号形状变更必须同步启动器的 schema 与 `semver_cmp`，并在 `contracts.md` 记录切换日期。
- 密钥用途变更等同换钥，按第 7 节处理。
- 新增角色或改 `variant` 取值前，先确认启动器五值枚举与端口表能容纳。
- 本契约在 nightly 正式对外分发前不冻结；改完必须同步清单、流水线与本文件，不允许只改一半。

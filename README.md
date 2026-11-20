# Dounai Nightly

Dounai 模块的每日构建分发仓。产物由 GitHub Actions 生成，托管在 raw.githubusercontent.com，供 Dounai Launcher 的 nightly 源读取。

> 这是尝鲜渠道。nightly 包在启动器中判 A 类、免确认自动安装，只适合愿意承担风险的技术用户。稳定版请继续使用 SourceForge 官方源。

## 目录

| 路径 | 说明 |
|---|---|
| `contracts.md` | 本仓唯一契约：版本号、密钥、源实装、增量打包规则 |
| `modules.json` | 待打包模块清单，流水线实装时建立 |
| `catalog.json` | nightly catalog，有模块更新时重签 |
| `packages/` | 模块 zip 包 |
| `state/last-built.json` | 各模块上次构建的 commit sha，用于增量判断 |
| `build.py` | 打包编排：筛更新、生成 manifest、签包与重签 catalog |
| `.github/workflows/nightly.yml` | 每日定时任务 |

## 用户怎么用

新版 Dounai Launcher 内置 `nightly-gh` 源，打开 “下载” 即可看到，无需粘贴邀请码。源不可删除，但用户可以切回官方源，或解除登记 nightly 实例。

catalog 有效期 1 天。断更超过 1 天后启动器 fail-closed，不会安装旧包。

## 版本与签名

- 版本号用点分日期 `YYYY.M.D`，例如 `2026.11.20`；同一天只发布一次。
- K3 签模块包，公钥内置到启动器 trust store；K4 签 catalog，公钥内置到 nightly 源配置。
- 私钥只存本仓受保护的 GitHub Secrets，本地另有离线备份。轮换规则见 `contracts.md` 第 7 节。

## 流水线

每天固定时刻运行，也支持手动触发。逐个比对上游仓库默认分支的 commit sha，只重打有更新的模块；有更新才重签 catalog。新增模块只需加一条清单记录，不改 workflow。`pack` 与 `sign_catalog` 每次从启动器 main 现编译。

当前状态：流水线已实装，首次产物由首次定时或手动触发生成。

## 启动器与契约

- 启动器：https://github.com/zhipianxiaokonglong/Dounai_launcher
- 契约：`contracts.md`

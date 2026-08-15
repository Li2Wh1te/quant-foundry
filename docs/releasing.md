# 发布 SOP

本文定义 Quant Foundry 的正式版本创建和发布流程。它的目标是保证一个正式版本始终
对应同一个 Git 提交、同一套前后端源码和唯一的 GitHub Release。

## 发布契约

一个正式版本由以下不可分割的对象组成：

```text
VERSION = 0.1.0
Git tag = v0.1.0
GitHub Release = v0.1.0
```

`VERSION` 是仓库中唯一由维护者直接维护的版本来源。后端 `pyproject.toml` 和前端
`package.json` 中的 `version` 是构建工具需要的派生副本，必须由
`scripts/release_version.py` 同步，并通过 CI 校验。

当前流程仅允许稳定版 `MAJOR.MINOR.PATCH`，例如 `0.1.0`。在需要 RC 或 beta 前，必须
先扩展发布工具，使 Git 的 SemVer 预发布标识和 Python 的 PEP 440 元数据有明确、可验证的
映射；不能临时手动修改任一副本。

## 版本决策

功能开发阶段不修改 `VERSION`。准备发布时，维护者根据自上一个 tag 以来的完整变更决定
下一个版本：

| 变更性质 | 版本变更 |
| --- | --- |
| 向后兼容的缺陷修复 | PATCH，例如 `0.1.0` → `0.1.1` |
| 向后兼容的新能力 | MINOR，例如 `0.1.0` → `0.2.0` |
| 破坏公开 API、配置或升级契约的变更 | MAJOR；在 `0.x` 阶段通常提升 MINOR，并在发布说明中标记破坏性变更 |

版本语义是维护者对使用者的兼容性承诺，不能仅靠代码差异自动推断。

## 一次性 GitHub 配置

在创建第一个 tag 前，仓库管理员必须完成以下设置：

1. 为 `main` 设置规则集：必须经 Pull Request 合并、要求 `Validate` 工作流通过、禁止直接
   push，并限制绕过者。
2. 为 `v*` 设置 tag 规则集：仅发布维护者可以创建，禁止更新和删除已有 tag。
3. 在分支规则中要求修改 `VERSION`、`.github/workflows/` 和发布脚本时经过维护者审查。
4. 确认 Actions 已启用，并允许工作流使用 `GITHUB_TOKEN` 创建 Release。`release.yml` 仅在
   `v*` tag 推送时申请 `contents: write` 权限。

GitHub Release 不是版本来源：它只能由已经存在且受保护的 `vX.Y.Z` tag 自动创建。

## 发布 v0.1.0

先将本 SOP、工作流和 `VERSION` 合并到 `main`。随后在干净的本地工作区执行：

```bash
git switch main
git pull --ff-only origin main
make release-check
make test
git status --short
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

最后一个命令会触发 `.github/workflows/release.yml`。该工作流会再次验证 tag、版本一致性和
测试结果，然后自动创建 GitHub Release，并使用 GitHub 自动生成的发布说明。

如果团队已配置 GPG 或 SSH 签名，使用 `git tag -s v0.1.0 -m "Release v0.1.0"` 替代
`git tag -a`。不要在 tag 已推送后移动、删除或重建它；发布失败时修复代码并发布新的版本号。

## 后续发布

1. 从 `main` 创建发布 PR。
2. 执行 `make release-set-version VERSION=0.1.1`。
3. 将本次用户可见变更从 `Unreleased` 移到 `CHANGELOG.md` 的新版本小节。
4. 提交版本、变更日志和必要的代码修复；CI 必须通过 `make release-check` 与 `make test`。
5. 合并发布 PR 后，按上述方式在该提交创建并推送 `v0.1.1` tag。
6. 检查 GitHub Actions 和 GitHub Release，确认 tag、Release 标题和 `VERSION` 一致。

## 部署边界

本阶段的正式发布物是经过验证的源码 tag 和 GitHub Release；自托管脚本仍从同一 tag 的源码
同时构建前端和后端。后续引入镜像仓库时，Release 工作流必须从该 tag 的单个提交构建两份
镜像，发布它们的不可变 digest，并让部署清单同时引用这两个 digest。正式环境不得混用不同
tag 构建的前端和后端。

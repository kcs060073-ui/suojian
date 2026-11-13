# 剪辑工作台 v0.9.0-beta.7 发布验收

日期：2026-09-22。Session：`20260922-170938-codex-9be5`。

## 本轮修复

- 修正粗剪页把数字人专属的“连续口播主轴”说明显示给信息流广告的问题。数字人继续提示只调整镜头级画面；信息流改为说明逐句镜头、顺序和时间边界可编辑，但不改写已锁定的广告文案与配音。
- 为路线文案增加独立行为回归，并补齐局部浏览器夹具对新模块依赖的显式注入。
- Windows 无头浏览器退出后偶发占用测试临时目录；夹具对 `EPERM` 采用有界重试和仅限测试临时目录的最佳努力清理，业务断言仍严格失败闭合。
- 完整回归发现音色试听原子缓存的临时文件名重复拼接 64 位音色哈希与 UUID，会让原本可用的 Windows 路径越过 `MAX_PATH`。临时名改为更短的独立 UUID，最终目标文件名、原子 `os.replace` 和不可猜测 URL 均保持不变。
- 候选 ZIP 首次隔离安装复现共享 `E:\Temp` 内 pip wheel 在长时间依赖展开期间被外部清理，安装器失败闭合。新增回归后把 `TEMP/TMP` 固定到包内 `runtime\install-temp`，重新构建并从全新解压目录安装成功；包内 Python/Node/FFmpeg 自检、空账本服务、授权门禁、迁移 37、SQLite 完整性和依赖隔离全部通过。
- 版本、下载链接、用户手册和发布说明统一升级到 `v0.9.0-beta.7`；项目宣传路线继续保持未开放。

## 发布门禁

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 路线文案 TDD | 修复前新增用例因函数不存在失败；修复后数字人与信息流两条文案断言通过 | `tests/workbench-ui.test.mjs` |
| Web 最终完整回归 | 97/97 通过，包含真实 Chrome 粗剪、包装、按钮防重、导航与五档视口 | 2026-09-23 本 Session 命令记录；初始 91 项基线见 `runtime/release-20260922-beta7/web-rerun.log` |
| TypeScript 最终完整回归 | 91/91 通过，包含信息流配音对象与 BGM 侧链分流 | 2026-09-23 本 Session 命令记录；初始基线见 `runtime/release-20260922-beta7/typescript-full.log` |
| Python 首轮完整回归 | 902 项：899 通过、3 个环境/路径错误、0 个断言失败；用于定位而不作为放行结论 | `runtime/release-20260922-beta7/python-full.log` |
| Python 最终完整回归 | 902/902 通过，0 错误、0 失败，2542.395 秒；独立 ASCII 临时目录、无浏览器竞争 | `runtime/release-20260922-beta7/python-full-final.log` |
| Python 最终改动专项回归 | 包装预览与发布打包 35/35 通过，覆盖失败预览恢复、包内安装临时目录和 PowerShell 5.1 解析 | 2026-09-23 本 Session 命令记录 |
| TypeScript 类型检查 | `npx tsc --noEmit` 退出码 0 | 本 Session 命令记录 |
| Python 字节码编译 | `python -m compileall -q media_catalog workbench scripts` 退出码 0 | 本 Session 命令记录 |
| 包装资产校验 | voice 1、BGM 5、SFX 28、字幕 9、文字卡 4、贴纸 27、花字 9；缺文件 0、授权错误 0 | `runtime/release-20260922-beta7/assets-validation.sqlite` 与命令输出 |
| 候选 ZIP 与隔离安装 | 334 条目、333 个 manifest 文件、禁入项 0；修复后全新安装退出码 0，迁移 37、SQLite `ok`、Python/Node 依赖来自包内目录 | `runtime/release-20260922-beta7/clean-install-smoke-beta7.json` 与本 Session 安装输出 |
| Git 差异检查 | `git diff --check` 通过 | 本 Session 命令记录 |

最终代码的 Web 与 TypeScript 全量共 188 项通过；Python 完整基线 902/902 通过，之后发生改动的两个 Python/安装模块再以 35/35 专项回归放行（专项与完整基线有重叠，不重复合计）。测试使用隔离账本、本地供应商替身和合成媒体，没有调用付费供应商接口。

## 发布与安装包

源码提交后必须重新构建 `剪辑工作台-v0.9.0-beta.7-windows-x64.zip` 及同名 `.sha256`，校验包内 `release-manifest.json` 的最终提交、逐文件哈希、禁入路径和凭据边界，并在隔离目录执行安装与本地服务冒烟。GitHub 上传、远端附件摘要和重新下载核验的最终收据保存在 `runtime/release-20260922-beta7/release-state.json`；这些动作完成前不能仅凭本文宣称线上发布完成。

发布目标为 [剪辑工作台 v0.9.0-beta.7](https://github.com/qnwhageqlavs-jpg/cutroom/releases/tag/v0.9.0-beta.7)，沿用私有仓库内部 prerelease 流程。

## 尚未验收的边界

普通同事电脑、公司真实网络与权限策略、移动硬盘现场重连、真实云端首片，以及普通同事本人完成四道人工作业门禁仍需现场验收；本次自动验证不替代真人签收。项目宣传路线不在本版范围内，继续保持未开放。

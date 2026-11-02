# 溯剪 SuoJian

这是溯剪在 2026-11-02 的本机项目源码快照，包含主工作台、园区 Vibe 和 Cutroom 本机适配代码。

## 目录

- `main/`：溯剪主工作台、视频分析、真人口播复刻和剪映草稿适配。
- `vibe/`：园区 Vibe、分镜渲染、风格模板功能、视觉组件、测试及依赖清单。
- `cutroom/`：本机 Cutroom beta.8 的应用代码、依赖清单、默认资源及文档。已有本机安装包括额外状态，不能用仓库直接覆盖。

## 不包含的内容

出于用户数据和凭据保护，此快照不包含历史任务、用户上传的参考图与视频、生成的成片、剪映草稿、个人模板库图片、本机数据库、模型权重、依赖目录、虚拟环境或密钥。模板库功能的代码在仓库里，但本机收藏的图片和任务不会随仓库恢复。

仓库保留了项目内的代码、依赖声明、默认字体及公开组件的非视频资源；部分上游示例因版权或媒体排除原则未收录。请保留各项目自己的 LICENSE 与来源说明。

## 运行与迁移

**此仓库是源码备份，不是解压即运行的完整安装包。** 本项目以 Windows 本机路径、Python、Node.js、FFmpeg、语音识别模型、剪映和独立 Cutroom 为基础。实际环境还需要重新安装依赖、准备模型和素材，并按目标电脑调整路径；本机凭据需要在目标电脑重新配置。

现有本机配置使用这些工作目录：`C:\Users\Tanikawa\suojian`、`C:\Users\Tanikawa\Documents\ChatGPT\溯剪终端` 和 `C:\Users\Tanikawa\CUTROOM`。更换电脑或账号时，应先调整代码和配置中的相关路径，再验证各模块；不要覆盖已有安装、数据库、素材或草稿。参考 `main/README.md`、`vibe/workbench/STORYBOARD_STUDIO.md`、`cutroom/README.md` 及各目录下的依赖文件。

园区 Vibe 使用 Remotion 4.0.539，相关 npm 依赖版本记录在 `vibe/work/runtime/package.json` 与 `package-lock.json`。本机浏览器引擎和 node_modules 不在仓库中。

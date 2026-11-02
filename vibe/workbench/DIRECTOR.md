# 一句话导演与聊天入口

本机服务：`http://127.0.0.1:8796/`。默认工作目录是此文件的父目录再上一层。

## 简洁工作台的制作任务（优先）

首页 `/` 是“准备任务 → 对话明确启动 → 影片与版本”；原工具保留在 `/advanced#edit`。这里的任务与旧“一句话导演”的配置修改计划是两种对象。保存任务不会自动运行模型、配音或渲染。不要把两种入口重复执行。

用户明确说“启动园区 Vibe 任务 V…”后：

1. `python -B -m workbench.production show V实际编号` 读取当前输入、`revision`、`inputHash`、`inputFile` 和历史版本；任务根目录以 API 返回为准，不能假设当前 checkout 就是运行目录。
2. 将 `executor: dialogue`、`expectedRevision`、`inputHash`、`status`、`note` 写到本机临时 JSON，然后 `python -B -m workbench.production progress V实际编号 --file work/进度.json`。状态可用 `checking`、`planning`、`previewing`、`rendering`、`needs_user`、`failed`、`interrupted`；不能用网页保存代表启动，也不能提前写完成。
3. 按本次输入调用 `park-vibe-producer` 制作。模板固定输入里的实际包与 SHA256；自动搭配遵循主题和 `autoSelection.seed`，不改写脚本事实。制作在独立目录进行，保留旧输出。联网补素材遵循本次 `externalServices.publicAssetSearch`。
4. 检查实际视频后将 MP4 放到运行模块的 `outputs` 内。用 `deliver` 回写真实交付 JSON。以下只是字段示例，路径和编号必须来自实际任务：

```json
{
  "executor": "dialogue",
  "expectedRevision": 1,
  "inputHash": "读取的完整哈希",
  "video": "outputs/productions/任务编号/实际成片.mp4",
  "title": "候选一",
  "kind": "full",
  "editingMode": "native-project",
  "sourceProject": "实际本机源工程绝对路径",
  "note": "实际制作与检查结果；写明编辑能力和未完成事项"
}
```

`python -B -m workbench.production deliver V实际编号 --file work/交付.json` 会核实视频流、尺寸、时长和 SHA256 后进入审看。`kind` 可为 `preview` 或 `full`。仅成片用 `finished-cut`；真实独立源工程用 `native-project`；只有符合本模块镜头格式的真实 JSON 工程可用 `shot-timeline`，后者会同时登记旧影片编辑器。原生动画工程不等于剪映多轨草稿。

接口：`GET /api/production/tasks`、`GET /api/production/tasks/V…`、`POST /api/production/tasks`、`POST /api/production/tasks/V…/revise|progress|deliver`。修订需要当前 `expectedRevision`，冲突为 409。旧输入和成片保留；“换一套搭配”只产生待启动的新输入修订，不暗中渲染。

运行服务默认 `http://127.0.0.1:8796`；CLI 可通过 `--url` 指定。进度只能在执行阶段真实回写，半小时无更新会显示“等待对话继续”。本接口不新增公共首页或其他工作室的共享任务协议。

## 在聊天里操作

当前助手可用自己的理解把自然语言转为受限的操作JSON，再调用共享入口；不需要第二个模型API。
这不是自动注册到其他聊天产品的MCP插件。其他助手需有本机工具权限并遵循本协议。

1. 用 `python -B -m workbench.director state` 读取当前工程。不要读取 `work/secrets/`。
2. 把用户要求转成 `SCHEMA` 支持的操作，写到 `work/` 下的临时JSON。
3. `python -B -m workbench.director propose --file work/修改.json` 生成可预览的修改记录。
4. 用户已授权直接修改时，添加 `--apply` 即可应用；否则让用户在导演台查看并应用。
5. 可用 `python -B -m workbench.director undo --id <计划ID>` 撤销。若工程又发生变化，先重新读取，不绕过冲突保护。

示例文件：

```json
{
  "instruction": "第二镜缩短为5秒，标题改成设备先行",
  "summary": "缩短第二镜并更新标题",
  "warnings": [],
  "operations": [
    {"op": "set", "path": "shots.1.duration", "value_json": "150"},
    {"op": "set", "path": "shots.1.title", "value_json": "\"设备先行\""}
  ]
}
```

镜头索引从0开始；时长为30fps帧数。提交前可加 `baseHash`，其算法是 `director.digest(当前工程)`。
HTTP路径：`GET /api/config`，`POST /api/director/propose`、`/apply`、`/undo`。
服务端再次验证字段白名单、参数范围、素材来源及当前工程版本；原MP4始终保留。

## 在工具台内操作

页面的“一句话导演”把相同任务交给用户配置的模型，受相同验证、版本冲突保护和撤销机制约束。
远程模型不能执行代码或自动发布，不接收密钥、视频内容和本机绝对路径。
当前支持文本/配置层面的修改、剧本转分镜和结构审查；新素材、配音、剪映与成片沿用现有制作工具。

接口选择使用 OpenAI Responses 的严格JSON schema，或 Chat Completions 兼容接口的 JSON 模式。
调度按任务角色选首选/备用，默认一次只请求一个接口，最多并发4个任务。
只有用户开启备用切换且出现限流/服务端错误/连接故障，才切到所选备用；不自动重复请求同一接口。
排队任务可取消；运行中任务不会因页面刷新而重复请求。服务重启后的旧任务标记中断，需手动重试。

Windows 密钥用当前用户的DPAPI加密保存在 `work/secrets/ai-settings.json`；本机文件服务拒绝读取该目录，工程打包也排除它。
非Windows移植时当前回退为权限受限的本地文件，不宣称跨平台加密；此版本已在Windows验证。

官方接口依据：
- https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
- https://api-docs.deepseek.com/guides/json_mode/
# 生成后的自动编辑入口

工具台的完整影片导出成功后，会保存本次镜头工程并自动进入对应影片编辑。渲染期间如果又改了工程，会保留后来修改，并在编辑页提示可切换到导出版本。短段试渲和截帧不会触发整片自动切换。

聊天内使用独立制作脚本生成影片时，完成后还须把真实输出及对应编辑工程提交给同一工作台：`POST /api/films/register`，JSON 字段 `video` 为本任务 outputs 内的 MP4 路径，`config` 为该影片实际使用的可编辑镜头工程。不可把无关的旧工程关联到新 MP4。登记成功同样触发自动进入影片编辑。


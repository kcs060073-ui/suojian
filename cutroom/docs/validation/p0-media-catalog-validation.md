# P0 素材目录验收报告

日期：2026-08-13

结论：P0 本地执行引擎及 10GB 级真实目录技术验收已通过，WorkBuddy 端到端实操也已通过。代码可由 Codex / WorkBuddy 调用；正式团队发布仍保留普通同事电脑、实际移动硬盘及真实长路径/权限环境三项现场门禁。2026-08-14 用户确认目标存储只有本机硬盘和移动硬盘，NAS 不再属于正式需求或验收门禁。

## 1. 方案边界

- 用户入口仍是 Codex 或 WorkBuddy 对话，不新增本地浏览器操作页。
- 不新增大模型 API；Agent 只调用本地命令并读取稳定 JSON。
- 素材原片只读，不复制、不改名、不转码、不覆盖、不删除。
- 保留已定稿的“主工作流 + 7 个子工作流”，素材目录只作为其共享底座。
- 数据库：SQLite；L1 负责快速建档，L2 显式执行技术探测。
- 同一时间只允许一个外部重任务；L2 内部并发限制为 1—4 个工作线程。

## 2. 自动验证

执行：

```powershell
python -m unittest discover -s tests
python -m compileall -q media_catalog tests
npx tsc --noEmit
```

结果：

- 52 项 Python 测试全部通过，0 failure，0 error。
- Python 编译检查通过。
- TypeScript 类型检查通过。
- 覆盖素材身份、改名重连、快速指纹碰撞、完整 SHA-256、同内容双副本归一、目录离线、断点游标、失败重试、单重任务锁、图片进程内探测、有界并发、ffprobe 错误、Windows 超长中文路径、备份恢复、CLI 中文错误摘要和稳定 JSON。

## 3. 10GB 真实目录

验证目录：`E:\我的AI项目\产品宣传视频`

| 项目 | 结果 |
| --- | --- |
| 目录总量 | 10,324,213,750 字节 |
| 媒体文件 | 23,086 个 |
| 文件构成 | PNG 21,880；MP4 920；MP3 242；JPG 43；WAV 1 |
| 原片处理方式 | 只读；未复制、改名、转码、覆盖或删除 |
| 验证数据库 | `runtime/media-catalog/p0-10gb-validation.sqlite` |

## 4. L1 快速扫描和恢复

- 首次扫描登记 23,086 个素材。
- 扫描在第 465 个素材附近主动中止后，可从游标继续，不从零重复。
- L2 旧串行方案在 64 个文件后停止；改为有界并发、图片进程内探测后重新执行。
- L2 使用 2 个工作线程主动中止，在约 22,310 个成功项后记录为 `interrupted`；随后用 4 个线程恢复并完成。
- 恢复段处理耗时 923.918 秒，Python `tracemalloc` 峰值 1.593 MiB。
- 当前状态：23,086 个素材可用，23,082 个技术探测成功，4 个失败，0 个等待。
- 4 个失败均为真实损坏或无效媒体：3 个 MP3 无法找到连续 MPEG 音频帧，1 个 MP4 缺少 `moov atom`。任务没有因坏文件整体中断。

说明：`tracemalloc` 只记录 Python 管理的内存，不等同于操作系统进程总工作集；它用于证明实现没有把 23,086 条待处理记录或媒体内容一次性装入 Python 内存。

## 5. 离线、重连和身份稳定性

- 将素材库挂载路径改为不存在目录后，扫描返回稳定错误码 `LIBRARY_OFFLINE`，`retryable=true`。
- 离线状态下数据库仍保留 23,086 个素材，未误标记为删除或缺失。
- 恢复真实路径后可继续扫描；同尺寸、同修改时间和同文件身份的素材不再重新计算指纹。
- 重连恢复段耗时 422.505 秒，`tracemalloc` 峰值 1.257 MiB。
- 重连前后素材 ID 数均为 23,086；有序 ID 摘要一致：`6392f737a78fd3ab1404c48a813dba6ae601ef9496db576d63c14ab90ba79eee`。
- 自动化测试另覆盖同卷改名后 ID 保持、挂载路径更新、离线不误删和无文件身份环境下的重复扫描。

## 6. 备份与恢复

- 在线备份：`runtime/media-catalog/backups/catalog-20260813-195550-835916.sqlite`。
- 恢复数据库：`runtime/media-catalog/p0-10gb-restored-final.sqlite`。
- 原数据库和恢复数据库的 `PRAGMA integrity_check` 均为 `ok`。
- 恢复后仍为 23,086 个素材、23,082 个技术探测成功、4 个失败、0 个等待。
- 恢复库的素材 ID 摘要与源库一致。

## 7. 本轮新增的可靠性处理

- PNG/JPG/WebP 由 Pillow 在进程内读取元数据，不再为大量图片逐个启动 ffprobe。
- 视频和音频继续使用 ffprobe；探测结果在主线程逐批写入 SQLite。
- 批量上限固定，有界工作线程为 1—4，失败项可单独重试，成功项不会重复处理。
- Windows 文件访问自动使用扩展长度路径；数据库仍保存普通、可读、可迁移的相对路径。
- 完整 SHA-256 确认的双副本合并为同一素材 ID，并保留多个位置；本机和移动硬盘统一按 `local` 管理，解析时只选择在线位置并保持确定性顺序。
- Agent 错误统一为 `code / retryable / user_message / next_action`，便于面向剪辑小白解释和恢复。

## 8. 尚待完成的外部门禁

| 门禁 | 当前状态 | 通过条件 |
| --- | --- | --- |
| 普通同事电脑 | 待验证 | ffprobe 自动发现、中文控制台和权限环境通过 |
| 实际移动硬盘 | 待验证 | 以 `local` 登记，断开不误删、盘符变化后重连及恢复流程在真实设备上复核 |
| 真实长路径/权限环境 | 待验证 | 在真实同事环境完成超长路径与磁盘权限现场复核 |

跨 Agent 合同见 `docs/operations/media-catalog-agent-contract.md`。上述三项属于现场设备与使用验收，不再要求增加本地网页或额外模型 API；普通同事不需要理解命令行、SQLite、素材 ID 或 ffprobe。移动硬盘断开不误删、重新挂载和同内容双副本能力已有自动化覆盖；正式同事验收时再使用实际移动硬盘复核。
## 9. WorkBuddy 独立复核（2026-08-14）

目的：不单边信任 codex 验收，由 WorkBuddy 在真实媒体上重跑 P0 全链路，独立确认行为符合共享 Agent 合同与验收门禁。

### 9.1 真实目录端到端（public/，241 个媒体文件）

命令序列：`doctor --json` → `init` → `library add` → `scan --catalog-only --json` → `probe --workers 4 --json` → `status --json` → `search --json` → `backup --json` → `restore --json`。验证库写到 `runtime/media-catalog/p0-workbuddy-verify.sqlite`，原片只读。

| 步骤 | 结果 |
| --- | --- |
| doctor | python 3.11.15、ffprobe 已发现、磁盘剩余约 158 GiB、无重任务运行 → `ok` |
| scan --catalog-only | discovered 241 / completed 241 / failed 0 |
| probe（4 线程，真实 ffprobe + Pillow 进程内读图） | discovered 241 / completed 241 / failed 0 |
| status | assets 241；technical_probe completed 241 / failed 0 / waiting 0；jobs completed 2 |
| search "厂房" | 正确返回周王庙空镜厂房视频，含 `asset_id` / `relative_path` / `size_bytes` |
| backup | 生成 `backups/catalog-20260814-*.sqlite`，`verified:true` |

补充验证：

- **断点不重复**：再次 `probe --workers 2` → discovered 0 / completed 0，已完成 241 项不被重复处理。
- **备份恢复**：`restore` 后 `PRAGMA integrity_check = ok`，assets 241 / probed 241 与源库一致。
- **离线不误删**：`library mount` 指向不存在路径后，`scan` 返回稳定错误 `LIBRARY_OFFLINE`（`retryable:true`，中文指引）；`status` 显示 `online 0 / offline 1` 且 `missing 0`；恢复真实挂载后重扫回到 241。

### 9.2 10GB 验证库续跑复核（`runtime/media-catalog/p0-10gb-validation.sqlite`）

- `status --json`：assets 23,086；technical_probe completed 23,082 / failed 4 / waiting 0。
- `probe --workers 2` 续跑：discovered 4（仅已知失败的 4 个）/ completed 0 / failed 4。
- 结论：已完成 23,082 项不被重复处理，证实 codex 初验数字（23,086 / 23,082 / 4）真实可信。

### 9.3 自动化测试现状

- 52 项中 51 项通过；1 项环境副作用失败：`test_scan_supports_long_chinese_path_when_os_allows_it` 在测试 teardown 阶段因本沙箱 safe-delete 对超长 `\\?\` 临时路径 `unlink` 拦截（recycle bin unavailable）报错，scan 本体已正常完成。属沙箱限制，**非产品缺陷**；需在未拦截环境复跑该项以关闭门禁。

### 9.4 门禁状态（更新）

| 门禁 | 状态 | 说明 |
| --- | --- | --- |
| 真实 10GB 目录 | 通过 | codex 初验 + WorkBuddy 续跑复核一致 |
| 断点续跑不重复 | 通过 | 真实目录（241）与 10GB（23,082）均证实 |
| 离线不误删 / 重连 | 通过 | 真实目录实测 |
| 备份恢复 integrity_check | 通过 | 真实目录实测 ok |
| 长路径（>260 字符） | 待真实环境复跑 | 自动化 1 项因沙箱 safe-delete 拦截失败，非产品缺陷 |
| 普通同事电脑 | 待验证 | 见第 8 节 |
| WorkBuddy 实操 | 通过 | 本次即 WorkBuddy 实操，全程用共享 Agent 合同命令，无需理解命令行 |

# CUTROOM 安装、升级与恢复维护说明

适用：CUTROOM Windows 编译发行版的维护人员；具体版本以安装目录 release-manifest.json 为准。普通同事请使用[操作手册](../user-guide/cutroom-user-guide.md)。

首次安装使用发行 ZIP 中的安装入口；正常启动使用同目录启动入口。本页说明已有任务的保全、迁移、回退和卸载，不要求普通同事运行命令。

## 常规程序更新与公开发行

普通同事使用安装目录的“产品更新升级.bat”；兼容升级保持原安装路径、任务库和MCP配置。Beta.11 首次采用编译载荷与小型加载桩，业务开发源码及Git历史不随公开安装包分发，浏览器资源仍可获得，不能保证无法逆向。

快捷更新按清单检查现有程序、版本、包摘要、路径、依赖与schema兼容信息；用户数据路径只忽略发行包中的空占位，不覆盖任务与素材。运行中拒绝更新；写入失败恢复原程序，中断保留journal供同一入口恢复。备份位于runtime/software-updates，不会自动清理。若备份损坏、文件被另改或环境不兼容，保留现场维护。

编译发行版改变实际执行文件签名；已有预览需要正常重新生成，不修改旧receipt。跨目录历史渲染迁移尚需专项验收；不要把原位更新成功当作跨目录迁移通过。

下面是换目录、数据迁移和维护回退流程，不属于普通程序原位更新。

## 维护前检查


- 不直接编辑生产 SQLite。
- 不把 API Key、Token、Cookie、客户素材或个人隐私写入文档和 Git。
- 每台新电脑由火山账号/IAM 管理员录入一次豆包新版 API Key 和 AK/SK；旧版 AppID + Access Token 仅作兼容，可以留空。不要经微信、文档、Git 或 ZIP 分发密钥。
- 不手工删除任务目录或发布包。
- 新增包装素材时补齐资产 ID、来源、授权、版本、文件路径和预览。
- 核心素材可进入 `assets/`；团队扩展进入 `asset-packs/`；个人试用素材进入设置页显示的个人目录下 `<分类>/inbox/`。
- 团队/个人素材自助扫描尚未接通前，由维护人员登记，不向普通同事宣称复制文件即可使用。

## 14. 升级、回退和卸载

### 14.1 升级

当前编译发行版使用 Windows ZIP 安装；只有满足兼容条件的旧安装可用快捷更新，跨数据库结构或依赖版本须独立安装并维护恢复。当前源码的数据库迁移终点为 schema 97；具体安装兼容性由升级工具核验。旧任务须按以下流程保全，不能用旧版直接打开已迁移数据库。

以下由维护人员执行，普通同事不要自行移动安装目录或复制数据库。

1. 核对原安装和服务归属，停止该安装的服务及运行作业；若已登记登录自启，在原目录移除属于本目录的注册。未知或另一安装的服务/注册保持不动，不覆盖原目录。
2. 保留旧目录，并做一致数据库备份。当前版本的备份命令只读源库，不先升级或回填源库；备份成功不代表新版支持该库的版本，恢复副本仍须单独核验兼容性。除账本和个人包装根，还需盘点实际任务原稿、转写、托管声音包（`runtime/workbench/tasks/`、`runtime/voice/`）、口播底片、预览、候选成片、交付文件及其引用；登记在外盘的素材另保留原文件与目录身份。SQLite 不是媒体备份，不在运行时只复制一个可能仍有 WAL 的库文件。
3. 新版 ZIP 解压到新目录并完成依赖安装。安装器不自动迁移旧数据；由维护人员按受支持迁移/恢复流程处理上述文件与引用，不直接改 SQLite，不伪改文件签名或将缺失素材重新登记为不同资产。
4. 核验数据库版本、完整性及需保留的旧任务：原稿可读、托管声音可试听、素材和候选/交付文件能按原身份回读；地址或资源失效时使用现有重连/恢复，旧预览或候选因代码变更失效时保留历史，重新生成当前版本。
5. 旧任务回读及代表性新任务完成后，再改用新目录 `启动CUTROOM.bat`；需要自启时由维护人员核对后登记。未完成数据盘点、回读和恢复前不删除旧目录；新任务成功本身不能证明旧任务已经迁移。

模型密钥保存在 Windows 凭据管理器，不应复制到新目录或写入文档。同一电脑、同一 Windows 用户升级并复制原数据库时，已有凭据引用继续可用；换电脑或换 Windows 用户后必须由管理员重新录入。新版会执行需要的数据库迁移，启动前必须保留升级前备份。


安装器会把依赖安装临时文件放在当前 CUTROOM 目录的 `runtime\install-temp` 下，避免系统共享临时目录在首次安装过程中被清理。安装完成后该目录可保留；安装失败时不要手动删除，维护人员可据此排查。

当前版本提供维护命令 `scripts/upgrade_cutroom.py plan / snapshot / restore`。维护人员使用含本批代码的新版目录专用 Python，明确传入源账本；不要把旧版仅SQLite备份当成完整恢复。示例路径必须改为实际安装路径：

```powershell
.\.venv\Scripts\python.exe scripts/upgrade_cutroom.py plan --source-root "E:\CUTROOM-old" --db "E:\CUTROOM-old\runtime\workbench\catalog.sqlite" --destination-root "E:\CUTROOM-new"
.\.venv\Scripts\python.exe scripts/upgrade_cutroom.py snapshot --source-root "E:\CUTROOM-old" --db "E:\CUTROOM-old\runtime\workbench\catalog.sqlite" --destination-root "E:\CUTROOM-new" --snapshot-root "E:\CUTROOM-backup\upgrade-20261003" --source-stopped
.\.venv\Scripts\python.exe scripts/upgrade_cutroom.py restore --snapshot-root "E:\CUTROOM-backup\upgrade-20261003" --destination-root "E:\CUTROOM-new"
```

编译发行包的渲染依赖同时包含加载器与执行文件。维护工具核对旧任务保存的组合身份，并按实际字节备份两份文件；旧程序文件保留在原目录和备份中，不覆盖新安装的程序。恢复前后都会核对目标目录自己的发行清单。若提示程序文件变化，应检查对应安装，不能手动改签名或复制旧程序盖过新版。

连续升级时，任务素材和字体沿用各自冻结的原地址，不统一改指向最早安装目录。已完成任务的观看、批准和 AI 检查失败状态原样保留；维护恢复不会代为批准、补查或重新收费。此流程仍要求保留历史原目录，不是可删除旧目录的数据搬迁。

先只读盘点。仅当 `snapshotInputsComplete=true`、没有未解决引用，且维护人员已核明并停止**原安装**服务/作业，才传 `--source-stopped`；此标志是维护人员的停机声明，程序不会杀进程。`--out` 可选报告必须是新旧安装之外的新文件，不覆盖已有报告。源账本必须位于源目录内。

盘点会核原稿、托管声音及祖先、旧广告声音、已知冻结渲染/QA/候选/交付、本地库/音乐、包装预览/实例/个人模板和终止作业请求。历史签名与备份当前观察分开：当前观察不回填原素材SHA、不批准模板/包装或使陈旧预览重新有效。无可信原签名、未知版本/字段、离线/缺失、内容或归属变化时先解决具体缺口；不重新签名或猜测全局路径。

已停止且结果未知的付费调用，在请求、归属和必要文件均可核验时可原样备份；保留未知结果与费用状态，不自动重发，也不转成已确认结果。缺失原签名文件、未知的人工修改字段或跨任务引用仍会阻止完整快照，报告具体缺口并保留原安装。

快照目录必须尚不存在，程序使用SQLite一致备份，复制数据文件并核摘要，最后才写完成回执。**快照会复制盘点中已登记的引用文件，可能包括外盘媒体；先预留相应空间**，不会递归扫描未登记目录。凭据、源代码和根`.env`不作为任务恢复文件复制；新目录的配置由维护人员另核对。

恢复目标可包含新安装代码，但待恢复的数据地址须全部不存在；任何已有主库、任务文件或SQLite事务文件都拒绝，不清空/覆盖。中断后没有完成回执时保留现场，再选独立数据空目录，不直接覆盖半成品重试。历史文件原地址仍必须可读且与原摘要一致；恢复副本不代表旧根或外盘可以删除。恢复后记录原引用基准，未把历史地址改为新目录。

新解压包在 `assets-local` 等数据目录带有空 `.gitkeep` 占位；源快照也包含同名占位时，恢复会明确拒绝 `RESTORE_CONFLICT`。维护人员须先核对新包 `release-manifest.json`：仅将待恢复地址中、清单登记且大小为0、SHA256为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` 的 `.gitkeep` 移到安装目录外的独立保留目录，再恢复。目标必须尚无业务数据，操作路径须留在指定新安装内；不递归清空 `assets-local`、`runtime` 或 `out`，不移开原安装或其他同名文件。不是空占位的冲突须保留现场另查。

`restoreComplete=true` 仅表示已支持的数据和引用复制、回读通过。`canSwitchInstallation=false` 始终提醒维护人员另做副本迁移兼容、新任务、服务归属、切换/回退和同事验收；不得据此自动启动服务、重放历史付费作业或转移旧批准。新目录恢复后按第4—5步核声音试听、媒体解码和完整任务。没有完成这些检查，继续保留旧目录。

### beta.83 精细包装修订的维护安装

版本号保持 beta.83，但新增包装文字分层字段，数据库从 schema 96 升至 97。同版本自动更新不会执行本次维护安装；只替换程序再启动旧库不属于已验证的恢复方法。

维护人员先让使用者保存或取消正在编辑的内容，确认后台任务空闲，停止明确归属的服务。保存原程序清单、SHA 和包含已提交 WAL 内容的一致数据库备份，记录任务数量与关键内容摘要。使用新包迁移并核验旧任务、正常修改和真实渲染，再开放使用。

开放前失败时，停止新服务，同时恢复升级前数据库和原程序，核验 schema 96、内容摘要及旧包启动。开放后若已有新编辑，不能用升级前数据库覆盖现状；保全当前数据并前向修复，或另行验证能保留新数据的兼容回退。仅恢复公开下载的旧 ZIP 不会降级已经升级的本地数据库。

升级前已请求但未应用的包装建议，若生成规则已变化，可能无法继续直接应用。保留原有方案，需要新建议时由使用者按正常费用提示重新发起；维护过程不自动重新收费调用。
### 14.2 回退

#### 维护人员只读回看恢复副本

恢复完成后，可用新版安装目录中的普通服务入口回看。仅用于原服务已停止、无待处理 WAL/事务日志且数据库版本与程序一致的恢复副本。会话状态另放在安装目录外；该模式不会迁移数据库、恢复中断作业或执行后台任务，保存、批准、选文件及生成请求均会拒绝。

```powershell
.\.venv\Scripts\python.exe -m workbench.server --db "E:\CUTROOM-new\runtime\workbench\catalog.sqlite" --port 8794 --maintenance-read-only --session-state-dir "E:\CUTROOM-maintenance-session" --no-open
```

服务就绪后，在同一新版安装目录另开终端，通过原登录归属验证打开页面：

```powershell
.\.venv\Scripts\python.exe -m workbench.local_entrypoint --db "E:\CUTROOM-new\runtime\workbench\catalog.sqlite" --port 8794 --maintenance-read-only --session-state-dir "E:\CUTROOM-maintenance-session" --open --wait 30
```

普通监督入口 `workbench/start-local.ps1` 同样支持 `-DatabasePath`、`-Port`、`-MaintenanceReadOnly` 与 `-SessionStateDirectory`，使用安装目录已有专用 Python。维护回看期间不要双击默认可写的 `启动CUTROOM.bat`。关闭本次维护服务后再按已验证切换流程操作；只读回看不表示可以删除旧根、迁移旧批准或允许生产切换。缺失库、非空事务日志或版本不匹配时保留具体错误并处理一致恢复，不自动修复。

可通过相同目录、数据库、端口和模式停止本次维护监护：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\workbench\start-local.ps1 -DatabasePath "E:\CUTROOM-new\runtime\workbench\catalog.sqlite" -Port 8794 -MaintenanceReadOnly -SessionStateDirectory "E:\CUTROOM-maintenance-session" -Stop
```

默认最多等待40秒，可用 `-StopWaitSeconds` 指定5–60秒。会话/控制目录及其父目录须为真实目录，不能使用目录链接或 junction。只停止匹配监护自己创建的进程树；原本已运行的服务仅复用，不取得关闭权。无匹配监护时不按端口杀服务。可写模式不支持 `-Stop` 或强制停止；有运行中的剪辑或付费作业时仍须按任务流程协调关闭。Windows进程树回收不等于业务优雅停机。

关闭新版后，重新打开旧版目录中的 `启动CUTROOM.bat`。若数据库已由新版升级，应由维护人员先恢复升级前备份，不能让旧版直接打开不兼容的新数据库。

### 14.3 卸载

由维护人员核对安装归属、停止其服务及作业，并移除属于本目录的登录自启。按升级第2—4步完整保全需要留存的数据和引用并核验可恢复后，才处理该安装目录。未完成保全或归属未知时保留目录。卸载不自动删除外盘素材或自定义个人包装根；账本备份不能代替任务原稿、声音、候选和交付媒体备份。

### 14.4 第三方素材署名

使用 Scott Buckley 的 BGM 发布视频时，必须在视频发布文案、平台说明、片尾或其他随作品展示的位置加入对应署名；不要求一定写在视频封面。具体曲目文字见根目录 `THIRD_PARTY_NOTICES.md`。如果发布渠道完全没有可展示署名的位置，就不要使用这些曲目。


## 报错日志与反馈

Beta.10 起自动记录已捕获的错误到安装目录 `runtime/diagnostics/`，不依赖业务库迁移。接口和后台错误使用 `errors.jsonl` 及最多三份轮转文件；安装/启动的 PowerShell 备用记录使用 `lifecycle.jsonl`。不要用原始终端输出、`.env`、凭据文件或数据库替代反馈包。

网页通过正常授权后可用“导出报错日志”下载 ZIP；离线入口为 `导出报错日志.bat`，或 `scripts/export-error-report.ps1`。Python 不可用时只导出经过字段筛选的安装/启动日志。浏览器断线时另导出 browser.json，与离线 ZIP 一起提供。

反馈包保留诊断编号、错误类型、相对代码路径及行号，不保存原始异常消息、源代码行或局部变量。分析时结合包内版本、用户描述及对应源码。任务/作业只保留散列关联标识；接口参数及实际路径段不进入日志。维护只读回看模式不新增诊断文件，不改变恢复副本。

升级时保留已有 `runtime/diagnostics`，用于关联升级前后的问题；没有新增诊断数据库表或修改费用/重试规则。日志目录不可写时不阻断业务，可能缺少记录，需结合截图排查。自动收集不等于自动外发。

## 性能记录与耗时报告（候选版本）

正常可写启动时，工作台自动在 `runtime/diagnostics/performance.jsonl` 记录后台处理和接口耗时；最多保留当前文件及三份轮转文件，每份上限 256 KiB。记录仅在本机保存，不发送服务；只读维护模式不新增记录。日志写入失败不阻断制作，也不触发重试或收费调用。

维护人员需要分析慢在哪里时，在实际安装目录运行：

```powershell
.\.venv\Scripts\python.exe -m workbench.performance --root . --output performance-report.json
```

输出文件必须尚不存在。报告最多含 4000 条清洗后的记录及按阶段、版本、输入摘要、规模和缓存命中分组的中位数（p50）、95% 分位（p95）、最大值、样本数与失败数。`fingerprint_scope` 区分作业请求、API 请求和渲染动作状态摘要，旧记录为 unknown；这些摘要不含所有输入文件字节，不能仅凭相同摘要宣称完整同输入，正式对比需另核验内容清单。记录保留输入镜头数、时间轴长度、候选数和缓存命中等可获得指标；未取得的指标为 `null`，不当作零。任务与作业使用散列标识，不保存原文案、素材路径、接口正文或密钥。“导出报错日志”维持原用途，性能报告由上面命令单独导出。

排队从本进程观察到入队开始计时，进程重启或未观察到入队时保持未知。计算使用单调时钟；后台 handler 覆盖处理函数，未包含之后的队列结果持久化；render、qa 等子阶段包含于 handler，不应相加当总耗时。用户等待与整片批准等待分开记录，仅以首次到最后一次确认仍在等待的观察间隔为下界，总等待时间保持未知。报告只代表保留样本；同输入、同电脑的首次执行和复用执行应分别比较，不能据此承诺每条视频固定几分钟完成。

## 编译发行构建与数据保护

普通用户发行包以网页工作台和随包操作手册为使用入口，账号在“设置”中配置。开发者源码保留 `.env.example` 和 `广告出片指南.md` 供历史命令行脚本维护，但这两份文件不再随普通发行包分发；运行脚本、原有环境变量兼容和升级器对旧包文件的识别保持。升级请走正常产品更新入口，不手工删除已有安装中的配置或文件。

实际版本和发布状态以 package.json、发行清单及发布记录为准。源码目录先运行 `npm ci`，使用 Python 3.11 执行：

```powershell
$releaseVersion = (Get-Content package.json -Raw | ConvertFrom-Json).version
python scripts/build_public_release.py --output out/release --version $releaseVersion
```

构建只使用 package.json 锁定的 TypeScript/esbuild，版本必须与包版本一致；缺依赖时先完成 `npm ci`。正式构建会逐项核对发行输入和构建脚本是否与指定 Git 提交一致，并在清单写入完整提交编号；无关的未跟踪文件不影响核验。不能将临时补丁冒充为已提交版本。安装包另有逐文件清单及 ZIP 校验值。

从 Git 归档构建时，使用该归档内同一提交的构建脚本，并提供可核验该提交的源码仓库：

```powershell
python "$archiveRoot/scripts/build_public_release.py" --root $archiveRoot --source-repository $sourceRepository --commit $releaseCommit --output $releaseOutput --version $releaseVersion
```

`SOURCE_COMMIT` 表示提交不可核验，`SOURCE_DRIFT` 表示文件内容不匹配，`SOURCE_INPUTS` 表示发行文件集合有差异。应修正来源并重新构建，不绕过检查或手改清单。

构建器将 Python 编译输入和随包 JSON 的换行固定为 Windows CRLF；生成的兼容加载器仍使用 LF，源码文件不会被改写。这样，从 Git 的 LF 归档和 Windows 的 CRLF 检出构建，会得到相同的 Python 字节码和 JSON 内容，避免仅换行差异使已审候选失效。Python 文件按固定顺序交给新启动的编译子进程，进程启动时设置 `PYTHONHASHSEED=0`，稳定常量集合的序列化顺序；修改父进程环境变量不能替代此步骤。其他二进制资产保留原始字节。

已审运行身份由 `media_catalog/render-runtime-compat-v1.json` 保存：Python 必须匹配完整程序语义，编译头、调试位置及常量集合的序列化顺序不能掩盖程序变化；本版 TypeScript 类型库由 ES2020 升至 ES2022 的配置仅接受登记的完整文件 SHA，运行编译目标仍为 ES2020。修改 target、module 或其他字段不能借此沿用旧审核。该兼容记录只用于候选依赖核验，发行清单与 ZIP 校验始终核验实际分发字节；不得手工修补安装产物。

启动提示 `STORAGE_DATABASE_MISSING` 时，保留 `%LOCALAPPDATA%/CUTROOM/task-protection` 中与该数据库对应的保护目录。停止对应实例后，核对历史清单的原数据库地址、时间及备份文件，优先使用 `last-nonempty.json` 指向的最后含任务快照。使用同版本安装内 Python 与既有恢复命令：

```powershell
.\.venv\Scripts\python.exe scripts/media_catalog.py --db "E:\CUTROOM\runtime\workbench\catalog.sqlite" restore --from "E:\verified-backup\catalog.sqlite"
```

请替换为实际数据库和备份地址。恢复工具执行数据库完整性校验，已有目标不会无备份覆盖。恢复后再正常启动核对任务与文件。数据库快照不能替代媒体文件备份；不要清空保护目录或伪造初始化记录。

开发验证同样使用项目自己的依赖：先用 Python 3.11 在项目根目录建立 `.venv`，再用 `.venv/Scripts/python.exe -m pip install -r requirements.txt` 安装声明的依赖；真实渲染测试会调用该环境，不能只依赖其他安装目录的 Python。全量后台验证包含 MCP 安装测试，另需用同一 Python 安装 `requirements-mcp.txt`；普通网页使用不要求这项可选依赖。执行 `npm ci` 后执行 `npm run test:web`（全部前端文件串行）、`npm run test:ts`（TypeScript 串行），以及 Python 3.11 环境中的 `python scripts/test_backend.py`。后台入口按完整测试 ID 去除重复导入的同一用例，不省略独立测试；`--list` 可查看发现清单；执行模式未发现任何用例时会报错退出，不能算作通过。前端 Windows 目录选择测试可用 `CUTROOM_TEST_PYTHON` 指向当前安装的专用 Python，无需 Codex 的模块路径。

## 首次安装的本地语音识别模型

从 beta79 起，安装入口会准备 small 语音识别模型（首次约 500 MB），并实际离线加载后才报告安装完成。数字人转写和首条广告的本地声音证据共用此缓存；无需先制作数字人来预热。此模型下载不调用用户的付费配音或视觉服务。

缓存按当前 Windows 用户的 Hugging Face 设置解析。安装、启动应使用同一 Windows 用户；不要在安装时临时设置缓存目录、启动时又移除该设置。已有完整缓存只做离线验证，不重复下载。首次下载失败、缓存损坏或内存不足会明确失败，不会假报就绪，也不会在本次流程中无限重试。

修复网络、空间或内存问题后，在实际安装目录执行以下命令，再重新运行安装入口完成自检：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-whisper.ps1
```

默认单次准备上限 600 秒；到时停止本次进程并保留已下载缓存。错误只显示安全的类型和处理提示，不打印可能含凭证的服务响应。损坏缓存由维护人员核对模型缓存位置后定向处理，不删除任务、原片或交付目录。

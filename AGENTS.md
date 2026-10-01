# 给在本仓库里干活的 agent 的约定

这个仓库是一条美术资源流水线，**四个阶段**：原画生成（AI）→ 原画处理（本机）→
视频生成（AI）→ 视频处理（本机）。细节都在 `README.md`。下面是**动手时要遵守的仓库约定**，
比 README 里的一般性说明更硬。

## 阶段是 4 个，步骤是 5 个（先看这条）

`src/stages.py` 的 `STAGES` 是这条规则的**唯一真源**，别在别处再写一份映射：

| 阶段 | 引擎 | 步骤 | 目录 |
| --- | --- | --- | --- |
| 1 原画生成 | AI | 1 | `01_artwork` |
| 2 原画处理 | 本机 | 2 + 3 | `02_artwork_ready` + `01_video_input` |
| 3 视频生成 | AI | 4 | `02_video` |
| 4 视频处理 | 本机 | 5 | `03_frames` |

- **磁盘布局是步骤，不是阶段**：run 文件夹里还是五个目录（`STAGE_DIRS`，key 是步骤号 1-5）。
  这是为了让老 run 照样读得出来、也让任何一步能被单独重跑。**别为了对齐阶段去合并目录。**
- 页面上的数字（页签、地址栏 `?stage=`、请求体里的 `stage`）是**阶段**，1-4；前端存的是
  `state.stage` / `stagesByRoot`。Python 侧收在 `server.stage_of()`，`STAGES` / `stage_of()` /
  `stage_steps()` / `stage_states()` 都从 `src/stages.py` 的 `STAGES` 取，别另写一份映射。
- 命令行 `--from` / `--to` 和 `stage1`~`stage5` 是**步骤**，1-5。阶段 2 那条命令是
  `run --from 2 --to 3`（绝不越界去调视频模型），阶段 3 是 `stage4 --run <run>`，
  阶段 4 是 `stage5 --run <run>`。要让「从阶段 N 起跑到底」，把阶段翻成步骤号给 `--from`。
- 一个阶段的完成度按阶段算：阶段 2 要它那两个目录**都有产物**才算亮（`stage_states()`）。

## 临时东西一律放 `temp/`

临时脚本、调试图、探针输出、量数据用的 JSON、后台跑服务的日志，全部放 `temp/`
（代码里取路径用 `src.paths.load_paths().ensure_scratch()`；换位置的话有环境变量
`ART_SCRATCH_DIR`）。**不要**把 `_foo.py`、`_debug.png`、`_ui.log` 这种文件丢在项目根——
`temp/` 已经被 git-ignore（只留 `.gitkeep`），清理起来一个目录就够。

流水线自己的产物不许写进 `temp/`：一次 run 必须能只靠 `resource/` 复现。

## 路径

- 不写绝对路径：目录只在 `src/paths.py` 里决定，别的模块用 `load_paths()`。
- 写进元数据、日志、URL 的路径一律相对项目根、正斜杠（由 `relativize_paths()` 兜底）。
  项目要开源、要能整体挪到别的机器。

## 目录的语义

- `resource/`：工作区，**全部可再生**，随时可以覆盖或删掉。
- `resource/image/<年月日>/<时间_名字>/` 原画（阶段 1~2 写在这里）；`resource/video/.../` 视频与帧（阶段 3~4）。
- `resource/video/<...>/03_frames/<动作>/<模型>/` 是**全量帧**（永不删）；
  旁边的 `kept/` 是**挑出来要用的帧**，导入 Unity 的是它。
- `origin/`：手工挑过的母版，**唯一长期保留**的目录；阶段 3 的输入是 `origin/image`。
  列表永远是**新的在最上面**（`library._recency()`：`meta.json.promoted` > `promoted_from` 里的
  run 时间 > 文件夹 mtime），别在页面或 server 里再排一遍。
- `resource/old/`：改版前的归档，**只读**，不要动。
- `hareness/prompts/`：给视频模型的**附加提示词**（不在代码里）。`common` 是常驻，其余按动作勾；
  文件名就是预设名。它拼在 `src/video.py` 内置提示词**后面**，只加不改 —— 内置那段里的
  「镜头锁死、角色钉在原地」是抠帧动画的前提，不要用预设去推翻它。

## 命名（`src/naming.py`）

- **一个名字只有两样东西**：实体 + 动作。`origin/image/<实体>.png`、
  `origin/video/<实体>_<动作>/<实体>_<动作>_001.png`。模型名、时间、批次号（`_01`）、`_kept`
  这类来源标记**不进名字**，进 `meta.json`。
- 所以**任何地方要拼 origin 里的名字，都去问 `src/naming.py` 或后端给的 `name`**，不要在前端
  或 server 里再拼一次（以前 `app.js` 和 server 各拼一遍，同一个动作落成了 `monster_imp_walk`
  和 `monster_imp_walk_kept` 两个文件夹）。`app.js` 的 `cutName()` 现在只负责「抄服务器给的」。
- **读宽容，写严格**：`origin` 里任意帧名（`1.png`、`frame_003.png`）都能列表、播放、导入；
  帧顺序走 `naming.cut_frames()`（按数字比较），名字里有 `sheet` 的算拼图不算帧。
  重写名字只发生在 `library.promote`（从 resource 来）和 `library.normalize`
  （已在 origin 里、从别处拷来的）两条路上 —— 所以 `normalize` 是「外部拷入」的唯一入口。
- 动作名来自 `hareness/common.yaml` 的 `pipeline.video.clips` + 已用过的动作文件夹
  （`naming.known_clips()`）；加动作不用改代码。提示词预设名（`walk_side`）**不是**动作名（`walk`）。
- 并行版本由人加 `_v2` / `_alt` 尾巴，代码不自动区分哪个是最终版。

## 界面（`src/ui/`）

- 左栏页签：`原画`/`视频` 是工作区两棵树，`保留-原画`/`保留-视频` 是 `origin` 的两半
  （`kept_image` / `kept_video`，服务端 `KEPT_HALVES`）。`归档`（`resource/old`）不占页签，
  从顶栏按钮进；只有正在看它的时候才补出那个页签，否则出不来。
  `?root=kept` 仍然给两半合起来的老视图，别删 —— 老链接和 `browse_roots()` 都还认它。
- 侧栏每行的「→ 保留」是一键转录：候选由 `server._promote_candidates()` 算好（原画取
  阶段 2 的 512 副本；帧取 `kept/`，**有 `kept/` 就只给 `kept/`** —— 挑出来的那一批就是这段动作，
  整段留在面板里那颗「转录到 origin」上），`name` / `entity` / `clip` 一起发过去，所以页面
  不弹输入框。别让它去猜路径，也别让它拼名字 —— 命名和阶段约定都只有后端知道。
- 从别处拷进 `origin/video` 的帧目录用「整理」（`POST /api/normalize`）就地规范化：帧重编号、
  补 512/256、补两张拼图、写 `meta.json`。是否规范由 `library.is_normalized()` 说了算，
  行上标 `未整理`、给按钮，都是读它。
- 右栏永远是「四个阶段 + 全流程」。**保留区也有这四个阶段**：点一件保留物，右栏显示的是它
  出处那条 run（`describe_kept` 从转录时记下的路径里把 run key 读回来）。别把这条栏对保留区藏起来。
- 「当前针对哪条 run」只有一个来源：`state.runPath`（`day/name`）。表单、命令预览、产出面板、
  产物目录都读它，**不要**拿 `state.selected` 当 run —— 保留区里它是 `origin/...`。
- 阶段 2 的表单只有三样要紧的：原画（`origin/image` 的缩略图挑选器）、提示词预设、补充提示词，
  剩下的 `fill` / `tolerance` / `sizes` 是本机的裁剪补绿开关。阶段 2 = 步骤 2 + 3，所以它一条
  命令跑两步、写两个目录，产出面板也画两块。
  后两个字段**阶段 2 自己不用**：它不调任何模型，这两个值是 `stage3_video_input` 顺手记进
  `01_video_input/prompts.json` 的，阶段 3 再去读（见 `stage3_video_input` 的 docstring）。
  所以字段标题上写着「给『3 视频生成』」—— 一个本机阶段问提示词，不写清楚谁看都懵。
  它**不挑 run**：没选 run 时阶段 2 自己开一条，跑完前端会跟过去。
- 「提示词预设」那个计数字段（`buildPresetsField` 的 `sync`）**只数非 common 的**：common 由
  resolver 每次自己带上，命令行上永远没有 `--preset common`，把它算进去就会出现"写着带上 1 个、
  命令里一个 `--preset` 都没有"。
- 阶段 3 的「提示词预设」默认沿用阶段 2 记在这条 run 上的那几个（`01_video_input/prompts.json`）。
  补的是"显示的和跑的不是一回事"：阶段 3 不带 `--preset` 时命令行会回落到那份记录，所以表单必须
  把它显示出来。`presetsFilledFor` 按 run 去重，灌一次就够，之后以人改的为准。
- 「动作」是 `clips` 字段，能点的是 `common.yaml` 列的默认动作 + 这条 run 已经出过视频的动作。
  **提示词预设的名字不进这个名单**：预设叫 `walk_side`、动作叫 `walk`，把预设名当动作点下去只会
  多出一个叫 `walk_side` 的 clip。
- 每棵树各记各的阶段（`state.stagesByRoot`），切树不互相带；默认落点见 `DEFAULT_STAGE`。
- 顶栏那排路径**给前端的一律是相对项目根的**（`PATHS.describe_relative()`，绝对路径只进 tooltip，
  走 `project_abs`）。要加一颗新的路径标签，就在 `describe_relative()` 里加，别在前端截字符串。
  要打开目录一律走 `reveal('<根相对路径>')`，别拼绝对路径。
- 播放器（`animPlayer`）第一帧要靠 `show(0)` 画上去：`paint()` 只写标签，`src` 是在 `show()` 里设的，
  只 paint 会得到一个空播放器（停在"帧 1 / N"，画面是裂图）。整段帧后到时也要走 `show`。
  `setFrames` 不许再收回"只许变长"这条限制 —— 挑帧右池每挑一次就换一批，可长可短可清空。
- 挑帧（`framePools`）是**两个池子**：左「总帧」是这一段的全量帧，右「选用」是挑中要写进 `kept/`
  的那批。两边共用 `pickGrid(list, {labels, marked, onPick, title, aspect})`，格子上的号一律是
  **原帧号**（所以右池第 17 格和左池第 17 格是同一张）。右池整块由 `refreshPicked()` 重建，
  头上那几个数、格子、播放器的帧都从 `apply()` 一个口子出去 —— 加功能就加在 `apply()` 里，
  别在点击回调里各写各的。变量名别用 `clear`（会遮住全局那个清 DOM 的 `clear()`，已踩过）。
- 右池底下那行「磁盘上的 kept/ 和现在挑的是不是同一批」决定「转录选用帧到 origin」按不按得动：
  不一致时转录送走的是上一批，所以按钮 disabled。判等靠帧名（`synced()`），别拿数量比。
- 「生成选用帧」走 `startSelectJob`，落到 `select --run ... --clips ... --models ... --frames ...`。
  挑帧命令枚举的是**第 5 阶段**的目录（`stages._cut_jobs`），不是第 4 阶段的 —— 帧在哪就查哪儿，
  别人拷进来的帧目录才选得了。
- `--clear`（界面上的「删掉 kept/」）**不许改回 `rmtree`**：走 `stages._retire()` 挪到
  `temp/trash/`。里面是手工挑出来的帧，一次误点不该是永久的 —— 这是踩过的坑，idle 那 60 帧
  就这么没过一次（后来从全量帧按 `--frames` 重建回来了，逐字节相同）。
- 转录（`promote`）成功后要调 `refreshArtwork()` + `refreshEverything()`：保留区和阶段 2 的
  原画挑选器是两张不同的清单，都得立刻看到新东西。别用 `loadState()` 代替 —— 它会清掉日志扫描
  状态，日志白闪一下。

## 动手前后

- 改 JS 跑 `node --check src\ui\static\app.js`；改 Python 跑 `python -m compileall -q src tools`
  （别把 `main.py` 编进去，那会在项目根留下一个 `__pycache__`）。
- 抠图 / 截帧 / 对齐这类东西改完要**真跑一遍并给出量化的前后对比**（残留绿像素、前景面积、
  画布、高度浮动、漂移），不要只说"应该好了"。
- 调 API 会花钱（只有阶段 1 和阶段 3 花钱）：先干跑（`show` / `probe`），再跑单段（`stageN`），
  最后才 `run` 整条。
- 不要 `git commit`、不要建分支，除非明确要求。
- 注释风格跟现有文件一致：`app.js` / CSS 用中文注释，`stages.py` / `library.py` / `server.py`
  是英文 docstring + 中文界面文案。

# 游戏美术资源生成管线（GPT 图像模型 + 绿幕抠图）

用无端大模型网关的 GPT 图像模型（`gpt-image-2.5-sunburst` / `gpt-image-2` / `gpt-image-2.5-flare`）
批量生成游戏美术资源：角色、怪物、道具图标、场景地块、UI、特效。

核心工作流：**提示词强制固定色号纯绿幕背景 → 代码抠绿成透明 → 裁剪到内容边界**，直接产出 Unity 能用的 Sprite 尺寸。
提示词和代码分离：改提示词只动 `hareness/`，改逻辑只动 `src/`，结果全部落在 `resource/`。

**改规格只改一个文件**：`hareness/common.yaml` 是全项目唯一真源，它同时定义
「发给模型的规则」和「代码执行的参数」。换对话、换人、换机器，只要这个文件不变，
出图效果和尺寸规则就完全一致（`python main.py common` 可以打印当前生效的全部规则）。

**画风由参考图说了算**：角色 spec 里写一行 `reference:` 指向原画，这一次请求就变成
图生图，并自动带上 `common.yaml` 的 `reference_lock`（参考图是唯一裁判，不许加细节）。
立绘 → 512px 副本 → 动作帧，整条链都锚在同一张原画上，见
[立绘与参考图](#立绘与参考图风格一致性)。

**这个项目里没有一个绝对路径**：目录只由 `src/paths.py` 从「自己所在的位置」推出来，
写进元数据、日志、页面 URL 的路径全部相对项目根。所以整个文件夹可以随便挪、随便复制，
放到别人的机器上，克隆下来就能跑。

## 两个文件夹，两种东西

| 文件夹 | 里面是什么 | 谁放进去的 | 目录层级 |
| --- | --- | --- | --- |
| `resource/` | 工作区：正在生成的、随时可以重画的一切 | 流水线自动写 | `image` / `video` → `<年月日>` → `<时间_名字>` |
| `origin/` | 母版：手工挑出来、要长期留着的 | **人**挑的 | `image/<实体>.png`、`video/<实体>_<动作>/` |

规则只有一句话：**看到好的就转录（promote）到 `origin`，然后只对 `origin` 负责。**

- `resource` 是可以再生的。原画不满意？提示词还在，重画一张。帧不好？`stage5` 不花钱，重切一遍。
  所以它按「哪天、哪一次」堆得很细，方便回头找，也不会堆成一坨。
- `origin` 是挑出来的那一份，不再分层，就是名字。阶段 3（视频生成）**只读 `origin/image`** ——
  一个 run 之所以开始，是因为有一张图被人看中了。
- 挑好之后的去向也是 `origin`：原画转录成 `origin/image/<实体>.png`，截好的帧转录成
  `origin/video/<实体>_<动作>/`（连 512 / 256 副本和两张拼图一起搬过去）。
  名字只由「实体 + 动作」两样拼出来，模型名和生成时间只进 `meta.json` —— 规则写在
  `src/naming.py` 一个地方，见「转录到 origin」那一节。

`resource/old/` 是目录改成 `image` / `video` 之前的历史产物，只用来浏览，不参与流水线。

## 目录结构

```
art/
├── hareness/                 提示词库
│   ├── common.yaml           唯一真源：公共提示词 + 全局限定参数（绿幕色号/抠图/裁剪/尺寸基准）
│   ├── animation.yaml        动作库：有哪些动作、每套几帧、每帧什么姿势
│   ├── _template.yaml        新建资源时复制的模板
│   ├── README.md             提示词写法说明
│   ├── style/                风格预设（像素风 / 手绘 / 3D / 图标 / 特效 / 场景 / 国风墨线）
│   ├── characters/           每个资源一个 yaml
│   └── prompts/              视频提示词预设：common 常驻，其余按动作勾（页面上阶段 2 勾的就是它）
├── resource/                 工作区（可再生，见上一节）
│   ├── image/<年月日>/<时间_名字>/   LLM 画的原画 + 处理后的图（阶段 1~2 写在这里）
│   ├── video/<年月日>/<时间_名字>/   LLM 出的视频 + 截出来的帧（阶段 3~4 写在这里）
│   │   └── 03_frames/<动作>/<模型>/   全量帧，旁边 kept/ 是挑出来要用的那几帧
│   ├── manifest.jsonl        每张生成的图一行
│   └── old/                  改版前的历史产物（只读浏览）
├── origin/                   母版（手工挑的，长期保留）
│   ├── image/<实体>.png      挑好的原画 —— 阶段 3 读的就是这里
│   └── video/<实体>_<动作>/  挑好的帧序列（+ 512/ 256/ 拼图 + meta.json）
├── src/
│   ├── paths.py              全项目唯一决定目录的地方（根 / hareness / resource / origin）
│   ├── config.py             配置读取（.env / 环境变量 / 命令行）
│   ├── api.py                三种调用方式 + 响应解析（含去重）
│   ├── harness.py            读取并拼装提示词
│   ├── generator.py          批量生成、保存、写元数据
│   ├── postprocess.py        绿幕抠图（含去溢色）+ 裁剪 + 序列帧对齐
│   ├── chroma.py             纯色背景抠像 + 补绿扩边 + 裁剪（阶段 2 和阶段 4 共用）
│   ├── prompts.py            读 hareness/prompts：预设文件 -> 拼给视频模型的附加提示词
│   ├── video.py              Seedance 视频调用 + 提示词 + 截帧（阶段 3 和阶段 4）
│   ├── stages.py             四个阶段（五个步骤）的编排：Run 对象、<年月日>/<名字> 规则都在这里
│   ├── naming.py             命名：origin 里一个名字长什么样，只在这里决定
│   ├── library.py            转录 + 整理：把 resource 里的东西复制进 origin，并写成规范名字
│   ├── animation.py          用 512px 立绘当参考图，出连续动作帧
│   ├── cli.py                命令行入口（含 ui / promote / runs 子命令）
│   └── ui/                   网页控制台：server.py + static/（index.html / style.css / app.js）
├── temp/                     临时文件（探针输出 / 一次性脚本 / 后台服务日志；git-ignored）
├── tools/mock_server.py      离线假接口，测试管线不花钱
├── main.py                   入口：python main.py <命令>
└── .env                      API Key 与网关地址（git-ignored）
```

## 路径（先看这里）

目录只在一个文件里决定：`src/paths.py`。它从自己所在的位置往上找带 `hareness/` 和
`main.py` 的文件夹当项目根，所以从哪个目录启动都对（`python -m src.ui` 也一样）。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ART_PROJECT_ROOT` | 自动找到的项目根 | 整个项目换位置 |
| `ART_RESOURCE_DIR` | `<根>/resource` | 生成结果（旧名 `OUTPUT_DIR` 仍然认） |
| `ART_HARNESS_DIR` | `<根>/hareness` | 提示词与 `common.yaml`（旧名 `HARNESS_DIR`） |
| `ART_ORIGIN_DIR` | `<根>/origin` | 母版（`origin/image` 是阶段 2 后半段和阶段 3 的输入） |
| `ART_SCRATCH_DIR` | `<根>/temp` | 临时文件（探针输出、一次性脚本、后台日志；`temp/` 已 git-ignore） |
| `ART_ENV_FILE` | `<根>/.env` | 令牌文件 |

命令行 `--out` 优先级最高。界面上真正生效的覆盖，会以橙色「环境变量」标签显示在顶栏。

顶栏那排路径也是**相对的**：`根目录 art` / `工作区 resource` / `母版 origin` / `提示词库 hareness` /
`视频预设 hareness/prompts`。它由 `paths.describe_relative()` 生成，去掉的正是「这台机器的盘符」——
截图发给别人、或者把界面录进文档时，不会带出你自己的目录名。完整路径还在，悬停一下就看见
（`project_abs` 那份只用于提示气泡）。第一颗给的是根目录的**文件夹名**而不是 `.`，因为它回答的是
「我现在看的是哪一份项目」。唯一会显示绝对路径的还是那颗橙色「环境变量」标签 —— 它要说的恰恰是
「你把某个目录指到别处去了」。

`temp/` 是唯一放「用完就扔」的东西的地方：探针输出、量数据的一次性脚本、后台起的服务日志都往
那里丢，所以项目根和 run 目录都不会攒下杂物（代码里取这个目录用 `paths.ensure_scratch()`）。
流水线自己的产物**不写这里** —— 一次 run 必须只靠 `resource/` 就能复现。

**写下来的路径一律相对项目根**（`resource/image/20260923/...`，正斜杠）。元数据侧车、
`manifest.jsonl`、日志、页面 URL、`--run` 参数都是这个规则；收口点是 `src/paths.py` 的
`relativize_paths()`：报告组装完以后再扫一遍，把里面的绝对路径全换成相对的。所以克隆到
别的机器、别的盘符，侧车里的路径依然指得对。

## 快速开始

```powershell
# 1) 依赖
python -m pip install -r requirements.txt

# 2) 确认 .env 里的 RELAY_API_KEY / RELAY_BASE_URL（默认已填好 token.wd.com）

# 3) 验证令牌与模型列表
python main.py probe

# 4) 看看有哪些提示词，以及当前生效的全局规则
python main.py list
python main.py common

# 5) 先干跑，只看拼好的提示词，不花钱
python main.py gen prop_spellbook_red --dry-run -v

# 6) 正式生成（默认自动抠绿 + 裁剪成透明 PNG）
python main.py gen prop_spellbook_red
python main.py gen-all -v

# 7) 挑一张满意的原画，转录成母版 —— 后面生成视频读的就是它
python main.py runs
python main.py promote "<上面列出的 run>/02_artwork_ready/512/<名字>.png"
#   不写 --name 就用规范名（monster_imp_01 -> origin/image/monster_imp.png）

# 8) 走动作资源流水线：四个阶段（原画生成 -> 原画处理 -> 视频生成 -> 视频处理）
python main.py run hero_idle --from 2 --to 3   # 阶段 2：抠绿 + 裁剪 + 补绿扩边（纯本机，不调 API）
python main.py stage4 --clips idle,walk,hit    # 阶段 3：Seedance 出视频
python main.py stage5                          # 阶段 4：截帧 + 抠图 + 裁剪 + 出 512/256
```

## 网页控制台

不想记命令的时候用界面点。界面不是另一套实现：按按钮时拼出来的就是上面这些
`python main.py ...`，面板底部实时显示将要执行的那一行，复制下来能直接贴进终端。

```powershell
python main.py ui                 # 起在 http://127.0.0.1:8765/ 并自动开浏览器
python main.py ui --no-browser --port 9000
python -m src.ui                  # 等价写法，从任何目录都能起
```

### 左边栏：页签

| 页签 | 看的是 | 能干什么 |
| --- | --- | --- |
| 原画 | `resource/image` | 挑原画；行上的「→ 保留」一键转录进 `origin/image`，缩略图上的「做视频」直接跳到阶段 2 |
| 视频 | `resource/video` | 看视频、看帧；行上的「→ 保留」把挑好的帧搬进 `origin/video` |
| 保留-原画 | `origin/image` | 已经挑出来的原画；点一件，右栏那四个阶段显示的就是它出处那条 run，并顺手把这张填进阶段 2 的表单 |
| 保留-视频 | `origin/video` | 已经挑出来的帧序列；同上，点一件就看它是从哪条 run 挑出来的 |
| 归档 | `resource/old` | 改版前的历史，只读 —— **不占页签**，从顶栏那个「归档」按钮进；在里面时它才作为页签出现，这样随时点得回去 |

保留拆成两个页签，是因为 `origin` 一个文件夹装两种东西（原画、帧序列），
而「哪张原画」和「哪段动作」是两个时候问的两个问题。`?root=kept` 仍然有效，
给的是两半合起来的老视图。

**每行右边那颗「→ 保留」是一键转录**：路径和名字都是后端算好的（原画取阶段 2 的 512 副本，
帧取 `kept/`，没有 `kept/` 就是整段），所以点一下直接进 `origin`、不弹输入框；
一条 run 下有多个动作 / 多个模型时它先展开一列候选，点哪个转哪个。
候选的名字一律是 `<实体>_<动作>`（`monster_imp_walk`）—— 模型名、时间、批次号都不进名字。
挑过帧的那一段，行上给的**只有挑出来的那一批**：它就是这段动作；整段原始帧在面板里那颗
「转录到 origin」上，要转随时能转。

**列出来永远是「新的在上面」**：`resource` 按 run 文件夹名里的时间戳排，`origin` 按转录时间排
（`meta.json` 里的 `promoted`，没有就用 `promoted_from` 那条 run 的时间，再没有就用文件夹自己的
修改时间 —— 所以刚从别处拷进来的也知道该排哪儿）。刚保留的那一件就在最上面。

左栏按天分组列 run，点一个 run；右边是四个阶段各一个面板，再外加一个「全流程」。
**每棵树各记各的停在哪一阶段**：原画树停在 1、视频树停在 3，来回切不会互相带跑
（切回一棵没看过的树时，原画树落 1、视频树落 3、保留-原画落 2、保留-视频落 4）。

每个面板上半是这一段的参数，下半是这一段实际写出来的东西：

| 阶段 | 面板里能看什么 |
| --- | --- |
| 1 原画生成（AI） | 绿幕原画，点开看大图；角色**可以多选**（勾几个就一次画几个，用的是同一套风格约束） |
| 2 原画处理（本机） | 上面填三样：**原画**（origin/image 里挑，带缩略图）、**提示词预设**（common 勾死，其余按动作勾）、**补充提示词**，再加裁剪 / 补绿的开关（`fill` / `tolerance` / `sizes`）。后两个**这一步一个字都不发给模型** —— 阶段 2 全是本机处理；勾它们是记进这条 run 的 `01_video_input/prompts.json`，给下面「3 视频生成」当默认值。运行不用先选 run —— 没选 run 时会照原画的名字新开一条，跑完自动跳到它。下面看产物：抠好裁好的图 + 512 / 256 副本 + **后半步补好纯绿的视频输入图** |
| 3 视频生成（AI） | **动作**按预设胶囊点选、**提示词预设**默认沿用阶段 2 在这条 run 上勾的那几个（能改、能点「看会加什么」把要发出去的话读一遍）；每个模型一个 `source.mp4`，能播、能 0.25× 慢放（看走路是不是原地） |
| 4 视频处理（本机） | 每个动作一个**播放器**（逐帧 / 精灵图两种片源）+ 画布 / 帧数 / 高度浮动 / 左右漂移 / 贴边帧 / 首末差异，外加接触表、**挑帧的两个池子**（见下） |
| 全流程 | 上面四个阶段拼成一个整跑的总览 |

### 预览与播放

一段 walk 有 119 帧，一屏放不下，所以是三层：

- **播放器**（面板里就有）—— 直接播，不只是把帧摆出来看。`▶ 播放` / 空格开关，
  `⏮ ◀ ▶ ⏭` 走帧，fps 可改（默认取 `meta.json` 里量出来的那个），
  `0.25× / 0.5× / 1× / 2×` 变速（慢放最容易看出走路是不是原地），可循环，
  下面一条滑杆拖到任意帧，右边实时写 `帧 44 / 119 · 1.79s / 4.96s`。
  开播会往前多备 12 帧；没备齐时右边会写 `载入 k/N` —— 等帧，不跳帧。
- **片源可以切**：`逐帧` 播的是 `512/` 里那批 PNG，就是最终要导入 Unity 的那批；`精灵图` 播的是
  `sprite_sheet.png` 切出来的片。整张精灵图有四万到七万像素宽，浏览器解不动，所以每一片由后端
  `/api/slice` 现切现送 —— 按顺序播出来就等于在播这张精灵图本身。两个模式按同一套序号对齐，
  切换停在原地。旧产物没有切片信息时不提供这个模式。
- **大图**：点「放大播放」进全屏。在那里也能播（空格开关，`m` 在逐帧 / 精灵图之间切），
  另外滚轮缩放、按住拖动平移、`←` `→` 翻帧（会去后端把整段 119 帧都取回来）、
  `+` `-` `0` 缩放、`Esc` 或点空白处关闭。
- **挑帧**在下面那块「挑帧」里做，它自带两个播放器 —— 整段一个、选用池一个，都不用进大图。

播放会缓存：URL 里带着文件的 mtime，没改过的文件浏览器直接命中缓存，改了的就是新 URL。
所以一段 119 帧的动画播第二遍是流畅的，重跑过 `stage5` 的也不会给你看旧帧。

### 挑帧：全量帧先全劈出来，再挑要用的

一段 119 帧里真正能用的往往只有十几帧（模型出的帧本来就不稳），所以帧分**两份**：

- **全量帧** = `03_frames/<动作>/<模型>/`，一帧不少，永远不删。它是「这段视频到底切出了什么」的
  证据，也是回头重挑的底子。
- **选用帧** = 同一层目录下的 `kept/`，只有你勾中的那些帧，重新裁到刚好包住角色、按这批重建画布、
  再缩成 512 / 256。**要导入 Unity 的是 `kept/`。**

网页上在「4 视频处理」的面板里，「挑帧」是两个**池子**并排：

- **左池「总帧」** —— 这一段切出来的 119 帧，一张不少。点一张是加进右池，再点一张是拿出来。
  标出来的帧号是**原帧号**，和 `--frames`、和 `帧 44 / 119` 里那个数是同一个。
- **右池「选用」** —— 挑中的那批，点一张就从右池拿出去，`清空`一键清光（只清页面上的选择，
  磁盘不动）。**这个池子自带播放器**：`▶`/空格播放、`⏮ ◀ ▶ ⏭` 走帧、`0.25× / 0.5× / 1× / 2×`
  变速、fps 可改、可循环，下面一条滑杆。挑成什么样、循环接不接得上，写盘之前就看得到。
  池子上面写着 `选用 60 / 119 帧 · 约 2.50s` —— 帧数 ÷ fps 就是这段动画播一遍要多久。

两个池子上面横着一条工具条，改的都是右池：`全选 / 全不选 / 反选 / 隔一帧留一帧`，在输入框里写
`0-40,45,50-70` 这样的范围再点「按范围选」（`加进选择`是不动已选的往里加）。另外两个按钮是给
「帧太多」这件事用的：**`均匀取 N 帧`** 在一整段里均匀挑 N 帧（119 帧压到十几帧时常这么干），
**`截到最佳循环帧`** 按统计里那个「最佳循环帧」把尾巴剪掉 —— 第 N 帧和第 0 帧最像，那 `0..N-1`
就是一个闭合的循环，后面的都白转，Unity 里的循环动画就该按这个截。

右池底下那行写着**磁盘上的 `kept/` 和现在挑的是不是同一批**。不是同一批时「转录选用帧到 origin」
是按住点不动的 —— 那时候转录走的是上一批，先点「生成选用帧」（就是下面那条命令）把当前这批写进
`kept/`，按钮才放开。`kept/` 已经存在时，打开面板会按 `meta.json` 里记的 `selected_frames` 把
右池填回原样，改一改再生成就是覆盖。

`删掉 kept/` 是**拿走，不是销毁**：这一批会被挪到 `temp/trash/<run>_<动作>_<模型>_kept_<时间>/`，
全量帧更不动。挑错了、点错了都能搬回来 —— 挑一批帧是手工活，一次误点不该让它没了。

命令行等价写法：

```powershell
# 留第 0~40 帧、第 45 帧、第 50~70 帧（帧号从 0 数，和面板里的序号一致）
python main.py select --frames "0-40,45,50-70"

# 只挑某一个动作 / 某一个模型
python main.py select --clips walk --models doubao-seedance-2-5-260628 --frames "0-40"

# 留到最后一帧：0- 就是「从 0 到最后」
python main.py select --frames "17-"

# 不写 --frames = 全部选中（等于这一批先用整段）
python main.py select

# 清掉选用帧（全量帧不动）
python main.py select --clear
```

- 帧号是**从 0 数**的（面板里显示 `帧 44 / 119`，写进 `--frames` 就是 `44`）。
- 挑完记得重新转录：`python main.py promote "resource/video/<年月日>/<时间_名字>/03_frames/walk/doubao-seedance-2-5-260628/kept" --name walk`
  （转 `03_frames/walk/<模型>` 就是把 119 帧全搬走，一般不想要这个）。
- 重跑 `stage5` 不会动 `kept/`（只会重新切全量帧），所以顺序无所谓：先挑后重切都行。

其他：

- 地址栏跟着走（`?root=video&run=...&stage=4`），还能用 `?shot=<项目相对路径>` 直接开某一张图；
  刷新不丢，也能直接发给别人。
- 底部是日志：实时滚动、可折叠、**高度能用鼠标拖着改**（抓最上面那条把手，双击回默认高度，
  高度记在浏览器里），跑的时候可以「停止」。
- 「打开目录」在资源管理器里打开对应文件夹 —— 119 帧还是一帧一帧看省事。
- 一次只跑一个任务：四个阶段共用一个 run 目录，同时写会写出半成品。
- 界面不开新窗口就不占地方：`python main.py ui` 是个本地 HTTP 服务，纯标准库，没有额外依赖。

## 四阶段流水线（原画 -> 动作资源）

**「步骤」和「阶段」不是一回事**，这是全项目最容易搞混的地方：

- **步骤（step）= 5 个**：一个步骤 = 一条命令 = 一个目录，可以单独重跑。它就是 run 文件夹里
  那五个子目录。
- **阶段（stage）= 4 个**：一个阶段 = 干活的人真正在想的「这一件事」。页面上的四个页签、
  run 行上四颗点、`--from` / `--to` / `stage1`~`stage5` 里的数字都是**步骤**号。

| 阶段 | 引擎 | 由哪几个步骤组成 | 目录 |
| --- | --- | --- | --- |
| 1 原画生成 | AI | 1 | `01_artwork` |
| 2 原画处理 | 本机 | 2 + 3 | `02_artwork_ready` + `01_video_input` |
| 3 视频生成 | AI | 4 | `02_video` |
| 4 视频处理 | 本机 | 5 | `03_frames` |

阶段 2 之所以是二合一：抠绿裁剪和补绿扩边都是**本机的图片处理**，中间不需要人做决定，
所以一条 `run --from 2 --to 3` 就跑完；磁盘上照样是两条命令、两个目录，单独重跑哪一个都行。
这样分下来的好处是**阶段名正好就是你要付钱的那两件事**（画原画、出视频），
另外两个是免费的本机活，重跑多少次都不心疼。

四个阶段可以一次跑完，也可以只跑其中一段：**一个 run = 一个键 `<年月日>/<名字>`**，它同时对应
两个文件夹（原画在 `resource/image` 下，视频和帧在 `resource/video` 下）。后面的阶段直接读
前面的产物，所以改一个参数重跑一段就行，不用从头来。

```powershell
# 一次跑完：LLM 画原画 -> 抠图裁剪 -> 补绿扩边 -> Seedance 出视频 -> 截帧
python main.py run hero_idle --clips idle,walk,hit --models doubao-seedance-2-0-fast-260128

# 只跑某一个阶段（--from / --to 写步骤号；--run 写 <年月日>/<名字>）
python main.py stage1 hero_idle                 # 阶段 1  步骤 1     LLM 出纯绿背景原画
python main.py run --from 2 --to 3 --run 20260923/20260923-172725_monster_imp
                                                # 阶段 2  步骤 2 + 3  抠绿裁剪 + 补绿扩边（不调 API）
python main.py stage4 --clips idle,walk,hit     # 阶段 3  步骤 4     Seedance 出视频
python main.py stage5                           # 阶段 4  步骤 5     截帧 + 抠图 + 裁剪 + 出 512/256

# 从某个阶段接着往下跑：把阶段的起点翻成步骤号给 --from
python main.py run --run 20260923/20260923-172725_monster_imp --from 2

# 不带 --run 时，目标是「最新的那个 run」（会打印出来是哪一个）
python main.py runs                             # 先看看有哪些 run、各自跑到哪个阶段（四颗点）
```

| 阶段 | 做什么 | 命令 | 产物（相对 `resource/`） |
| --- | --- | --- | --- |
| 1 原画生成 | LLM 按 common.yaml 的风格约束，画纯绿 `#00F700` 背景的立绘 | `stage1 <spec id> [<spec> ...]` | `image/<年月日>/<run>/01_artwork/` |
| 2 原画处理 | 抠掉绿幕、裁到刚好包围非透明区域、出 512/256；再读 `origin/image` 里挑好的原画，把透明处补成精确的 `#00F700`、角色缩到画布 70% | `run --from 2 --to 3 [--source <png>]` | `image/<年月日>/<run>/02_artwork_ready/` + `video/<年月日>/<run>/01_video_input/` |
| 3 视频生成 | 拿 512 参考图调 Seedance 出 3~5 秒视频，按模型分目录 | `stage4 --run <run> --clips ...` | `video/<年月日>/<run>/02_video/<动作>/<模型>/` |
| 4 视频处理 | 截帧、抠绿幕、裁剪、整段对齐、出 512/256；挑过帧再生成 `kept/` | `stage5 --run <run>` | `video/<年月日>/<run>/03_frames/<动作>/<模型>/` |

步骤 2 和步骤 5 是**同一段代码**（`src/stages.py` 的 `prepare_sprites`），只是输入不同：
一个是 LLM 画的立绘，一个是从视频里切出来的帧。这样切出来的帧不可能和它的原画用两套规则处理。

一个 run 文件夹里的五个子目录就是上面那五个步骤留下的；而 `runs` 和页面上的四颗点按**阶段**算
——阶段 2 要步骤 2、3 两个目录都有产物才算亮。老 run 的目录结构完全没变，照样读得出来。

### 提示词预设：风格一致性的调参入口

阶段 3（视频生成）发给 Seedance 的提示词是**三段拼起来**的，从固定到灵活依次叠加：

```
src/video.py 内置的那段   （风格锁 / 镜头锁死 / 绿幕 / 循环 —— 代码里有，不要动）
  + hareness/prompts/common.xml     常驻，每次自动带上
  + hareness/prompts/<你勾的>.xml   按动作勾
  + 你手写的那一句                   --extra-prompt
```

`hareness/prompts/` 里现在有：

| 文件 | 用途 |
| --- | --- |
| `common.xml` | 常驻。线稿外框、平涂、角色同一性、不加料、绿幕不动 |
| `walk_side.xml` | 侧面行走循环，把 contact / down / passing / up 四个关键姿势写死 |
| `idle_breath.xml` | 站立呼吸，只允许呼吸和极小的微动 |
| `hit_knockback.xml` | 受击后仰再回到站姿，首尾同姿势 |
| `_template.xml` | 样板，复制着改 |

文件名（不带后缀）就是预设的名字；`_` 开头和 `README` 不会被当成预设，所以这个文件夹能自己写
文档。也可以用现成的 `.txt` / `.md` / `.yaml` / `.json`。

```powershell
# 阶段 2 把预设记进这条 run，阶段 3 自己会读回来，不用再写一遍
python main.py run --from 2 --to 3 --source origin/image/main.png --preset walk_side --extra-prompt "披风再飘一点"
python main.py stage4 --run 20260924/20260924-101530_main
```

记在哪里：`video/<年月日>/<run>/01_video_input/prompts.json`。阶段 3 写提示词时会先看有没有
`--preset` / `--extra-prompt`，没有就用这份记录；两者都没有才只用内置提示词。
写好的提示词落在 `02_video/<动作>/prompt.txt`，可以直接改那个文件（改了就优先用文件里的）。

### 为什么阶段 2 一定要「补绿 + 扩边」

视频模型**复制的是参考图的构图**，提示词里写「四周留白」它基本不听，实测发髻顶和脚会被裁掉
35~84 像素。所以留白必须做进图片里：阶段 2 的后半步先把 512 原画的透明区域填成**精确的** `#00F700`，
再把角色缩到画布高度的 70% 并居中，四周自然留出一圈等宽的纯绿。这样做之后 119 帧
`frames_touching_edge` 全是 0，一次都没被裁。

`--fill 0.7` 可以调，值越小留白越多。

### 为什么阶段 4 要先「压平背景」

H.264 在纯色区域会留 ±20 的噪点，所以视频帧里**从来不存在**一个干净的绿。视频处理先读边框的
实际主色，把容差（`--flatten-tolerance`，默认 40）内的像素全部压成那**一个**色号，再交给抠像。
实测同一帧：直接把提示词里的色号当背景色去抠，83% 的像素变成半透明（角色周围一圈绿雾）；
先压平再抠，半透明像素 0%，边缘干净。

抠像本身也是按「边框实测颜色」来的，不是按提示词里写的色号：模型返回的绿和你要的绿本来就不一样，
用实测色才不会把整片背景变成一层淡淡的东西。

### 抠像还得扛住五件实测出来的麻烦

「抠掉纯绿」听起来是一行代码的事，实际跑下来有五个坑，都在 `src/chroma.py` 里处理掉了：

1. **模型和编码器会把绿画偏。** 同一个 `#00F700`，原画回来是 `#02F804`（差 4），视频回来能到
   `#2ED113`（**差 46**）。所以「这是不是绿幕」不能只拿一个色号的容差去卡：实测一条 walk 视频里
   119 帧有 60 多帧被判成「不是绿幕」，整帧没抠、还是整块画布，对齐跟着一起崩（画布从
   `412x693` 涨到 `960x960`、高度波动 30%）。现在按「这是不是一个浓的亮绿色」判断
   （`is_screen_green`）：白纸、灰格、暗 vignette、奶油色皮肤、淡蓝这些仍然会被拒绝，不会误伤。
2. **角色会把背景围成口袋。** 两腿之间、指缝里、胳膊和身体的夹缝，这些绿背景跟画布边缘不连通，
   而抠像只保留「和边缘连通」的像素（这是为了不误删角色内部的绿色），于是剩下一块块绿斑粘在
   角色上。现在被围住、颜色就是实测背景色、且面积够大的连通块会被补成背景（`_fill_holes`）。
3. **边缘是混出来的。** 背景和黑色描边之间那 1~2 像素是两者混合的结果，颜色既不是背景也不是描边。
   如果只把「容差以内的像素」当作要处理的像素，这些混合像素就会被当成角色、原样带着绿留下来——
   也就是「边缘去不干净」。现在软 alpha 会覆盖背景外一小圈（`EDGE_BAND_PIXELS = 3`），而且是跟
   **最近的一个确定属于角色**的像素去解混，不是跟最近的非背景像素（那样等于拿它自己跟自己比，
   结果恒为全不透明）。实测一帧里明显的绿像素从 1640 个降到 608 个，交付尺寸（512、白底）下
   已经看不出来。
4. **容差切不断那条带。** 背景不是「到角色为止」，它是**渗进**角色的：视频里模型能把绿幕整片抹到
   腿上（实测 walk 第 34 帧，两条小腿全糊着绿）。那些像素比纯绿暗，按颜色卡就会被当成描边、卡在
   那里不动。所以这里反过来做：**由边界往内走** —— 从已经确定的背景出发，一像素一像素地吃「还带绿」的
   邻居，**撞到黑描边就停**（`_grow_through_spill`）。这一招成立全靠这套美术的风格——角色都带
   一圈浓黑的描边，描边既不绿、又比背景暗得多，所以走不过去，也就吃不到角色本身。走多少步是有
   上界的（`GROW_STEPS = 16`），描边破个口子也只是多花几像素，不会把角色掏空。走完只保留贴着
   角色的 3 像素做软过渡（`COLLAR_PIXELS`），再往里就是背景本身、直接全透明——否则那圈半透明的
   「雾」在浅色底上会读成一道灰边。
5. **走不到的地方，按绿色占比反解。** 模型把绿盖在腿上、或者画一道半透明特效时，那里是一块**暗**
   绿，走路会把它误当成描边停下来，于是留下一块绿斑。这种像素没法用颜色阈值分开——它本来就是
   「背景 + 前景」混出来的，但**混合比例是可以量的**：`绿 - max(红, 蓝)` 在纯背景上最大、在描边、
   皮肤、布料上接近 0，所以拿它跟背景上的同一个值比，就是这个像素里含多少背景。这个占比一次做
   两件事：**降 alpha**（一半背景就是一半不透明），并且解 `p = a·F + (1-a)·B` 把颜色还原
   （`_despill`）。于是 hit 里那条白色刀光会还原成「半透明的白刀光」，而不是一条绿线；小腿上那层
   绿雾会还原成「半透明的腿影」。占比只在离背景 `SPILL_REACH = 32` 像素以内才信——再往里绿的
   像素就是美术自己的绿，不该动；再加上「绿通道够亮」和「占比够大」两道闸，编码器在暗描边上留的
   那点绿噪声不会被误伤。
   实测：只用第 4 招还有残留的帧，再加上第 5 招，一帧里可见的绿像素是 **0**（walk 第 34 帧从 6135
   降到 0），而角色的可见面积不变——是「把绿还原成前景 + 降低不透明度」，不是把像素删掉。

顺带：`--tolerance`（`common.yaml` 的 `pipeline.background.tolerance`）现在阶段 2 和阶段 4
**共用同一个值**，原画和视频帧不会用两套容差。

### 阶段 4 会告诉你四个数字

| 数字 | 含义 | 好的值 |
| --- | --- | --- |
| `height spread` | 角色高度波动，大了就是动画在「跳」 | < 3% |
| `drift` | 角色在画面里左右漂移多少像素（应该钉住不动） | < 10px |
| `frames on the edge` | 有几帧角色贴到画布边缘（说明被裁了） | 0 |
| `loop` | 首帧和末帧的差距，越小越容易做循环 | < 5% |

另外会给出 `the cycle closes on frame N`：和首帧最像的那一帧。要做循环动画，按这一帧裁断最合适
（这段信息在 `03_frames/<动作>/<模型>/meta.json` 里，字段名 `loop_best_frame`）。

这些数字都是在**模型刚出的原始帧**上量的。对齐对整套帧只施加**同一个**平移量（见下面「对齐 = 不抖」），
帧与帧之间的相对位移原样保留，所以量出来的漂移就是模型自己的漂移，不会因为对齐而变好看。

### 动作是可选的

- `--clips` 决定生成哪些动作，默认 `idle,walk,hit`（`common.yaml` 的 `pipeline.video.clips`）；
- `stage4` 默认用三个 Seedance 模型各出一版方便对比，只想花一份钱就
  `--models doubao-seedance-2-0-fast-260128`；
- 已经渲染过的视频**不会重复生成**，`stage5` 随时可以重切帧、不花钱；要重出加 `--force`；
- 每个动作的提示词第一次会写到 `02_video/<动作>/prompt.txt`，之后就从那个文件读。想单独调某个
  动作的指令，直接改这个文件再 `stage4 --clips <动作> --force`；
- 某个动作想要一张专门的参考图（比如走要侧身），用
  `--reference walk=origin/image/walk.png`，它同样会被自动补绿扩边。

## 转录到 origin（promote）

`resource` 里的东西随时会被覆盖或删掉，所以「这张就是我要的」这件事得显式说一次 ——
说的方式就是转录。

```powershell
# 原画 -> origin/image/<实体>.png（连提示词侧车一起搬）
python main.py promote "resource/image/<年月日>/<时间_名字>/02_artwork_ready/512/monster_imp_01.png"
#   -> origin/image/monster_imp.png + monster_imp.json（批次号 01 不进名字）

# 帧序列 -> origin/video/<实体>_<动作>/（帧 + 512/ 256/ + 两张拼图 + meta.json）
# 挑过帧就转 kept/（只有选中的那些），没挑过就是整段；**两者落到的名字是同一个**
python main.py promote "resource/video/<年月日>/<时间_名字>/03_frames/walk/doubao-seedance-2-5-260628/kept"
#   -> origin/video/monster_imp_walk/
python main.py promote "resource/video/<年月日>/<时间_名字>/03_frames/walk/doubao-seedance-2-5-260628" --name monster_imp_walk

# 名字撞了会拒绝（返回码 1）；确认要换掉就加 --force
python main.py promote "<路径>" --force

# 已经在 origin 里、从别处拷进来的帧目录：就地整理成规范形状
python main.py normalize "origin/video/knight_attack"
```

- 给的是文件 → 进 `origin/image`；给的是文件夹 → 进 `origin/video`。不用自己说是哪一类。
- 名字能自己起（`--name`），但**不给就是规范名**：`<实体>_<动作>`。名字只能是**一段**
  （不能有斜杠、不能用 Windows 不认的字符、不能以点开头）—— 它会变成 Unity 里的 sprite 前缀。
- 转录帧序列时只搬**交付要用的那些**：帧、512 / 256 副本、两张拼图、`meta.json`。
  未抠图的原始帧 `source_frames/` 留在 `resource` 里（重跑 `stage5` 就能再生成）。
- 网页上就是「转录」「做视频」「转录到 origin」「整理」那几个按钮，等价于这几条命令。

### 名字里有什么、没有什么

一个名字只由**实体**和**动作**两样拼出来，规则只写在 `src/naming.py`：

```
origin/image/<实体>.png                一个角色一张立绘
origin/video/<实体>_<动作>/             一套动作 = Unity 里一张 Multiple 精灵图
    <实体>_<动作>_001.png …             帧，从 001 起，按播放顺序（三位补零，超 999 滚四位）
    512/  256/                          同名同前缀的缩放副本，整套同一个系数
    <实体>_<动作>.png                   精灵图 sprite sheet（横向平铺）
    <实体>_<动作>_contact.png           接触表（名字里带 contact / sheet，不算帧）
    meta.json                           出处 run / 模型 / fps / 帧数 / 画布 / 转录时间
```

- **不进名字的**：模型名（`doubao-seedance-2-5-260628`）、生成时间、批次号（`monster_imp_**01**`）、
  `_kept` 这类来源标记。它们是生成参数，进 `meta.json`。用更好的模型重画一遍走路，就该**替换**掉
  旧的，而不是并排躺着一个谁也说不清该用哪个的文件。
- **动作名**来自 `hareness/common.yaml` 的 `pipeline.video.clips`（默认 `idle` / `walk` / `hit`）；
  这个项目已经用过的动作名会被自动认出来（`resource/video/.../03_frames/<动作>/` 里的文件夹名），
  所以加一个新动作不用改代码，用一次之后它就进了名单。
- 确实要并存两版时，由人加上 `_v2` / `_alt` 之类的尾巴，哪个进最终版由人决定。
- 「从右边剥动作名」是有意为之：所以一个手拷进来的 `knight_walk/` 不用 `meta.json` 也知道
  它是 `knight` 的走路；而不在名单里的名字保持完整（`item_sword_icon` 不会被当成
  `item_sword` 的 `icon`）。

### 别处拷来的东西，能直接读吗

能。**读宽容，写严格。**

- 扔进 `origin` 的任何文件夹，不管帧叫 `frame_003.png` 还是 `1.png`，都能被列出来、被播放、
  被导入；顺序按数字理解（`1, 2, 10`，不是 `1, 10, 2`），名字里有 `sheet` 的当成拼图不算帧。
  没有 `meta.json`、没有 `512/` 也照读不误。
- 只有走过 `promote`（从 `resource` 来）或 `normalize`（已经躺在 `origin` 里）才会被写一次名字：
  出来就是上面那套形状，和 pipeline 写了半年的是同一套，谁也分不出谁。
- 侧栏会把还不规范的帧目录标一句 `未整理`，并给一颗「整理」按钮 —— 点它等价于
  `python main.py normalize "<那个目录>"`。

## 目录长什么样

### 生成（`gen`，不走流水线）

落在 `resource/image/` 下，两级目录：**第一级是当天日期（`YYYYMMDD`），第二级才是这一次生成**
（文件夹名 = 生成时间 + 资源名）。所以 `resource/` 不会越堆越乱：先按天分流，再一看名字就知道
是什么时候出的什么图。

```
resource/image/
  20260920/                               # ① 第一级 = 当天日期
    20260920-223340_prop_spellbook_red/   # ② 第二级 = 这一次生成（时间_资源名）
      prop_spellbook_red_01.png           #    全分辨率裁剪结果（已抠绿 + 已裁剪）
      prop_spellbook_red_01.json          #    prompt / 模型 / 请求体 / 尺寸 / 后处理结果
      512/                                #    最长边 512px
        prop_spellbook_red_01.png         #      412x512
      256/                                #    最长边 256px
        prop_spellbook_red_01.png         #      206x256
      anim/                               #    动作序列帧（可选，只有要动作的资源才有）
        idle/
          idle_01.png ... idle_04.png     #      已抠绿 + 已裁剪 + 已按整套对齐
          512/  256/                      #      整套动作按同一系数缩到 512 / 256
          idle_sheet.png                  #      横向序列图，Unity 切成多帧用
          idle.json                       #      帧数 / 参考图 / 画布 / 每帧 prompt
        walk/  hit/ ...
  20260921/                               # 下一天再来一个文件夹
    ...
  manifest.jsonl                          # 每张图一行（含所在文件夹）
```

同一批 `gen` 多个资源时，共用同一个时间前缀，方便把它们认成一次生成：

```
resource/image/20260920/
  20260920-230012_hero_knight_2d/
  20260920-230012_monster_slime/
  20260920-230012_item_sword_icon/
```

### 流水线产物（五个步骤 / 四个阶段）

产物是**同名、分居两棵树**的两个文件夹：

```
resource/image/20260923/20260923-172725_monster_imp/
├── 01_artwork/                 阶段 1（步骤 1）：纯绿背景原画（+ 同名 .json 提示词侧车）
└── 02_artwork_ready/           阶段 2（步骤 2）：抠好、裁好
    ├── monster_imp_01.png
    └── 512/  256/              长边 512 / 256 的副本
resource/video/20260923/20260923-172725_monster_imp/
├── 01_video_input/             阶段 2（步骤 3）：补了精确纯绿、留了白边，交给视频模型的那张图
├── 02_video/<动作>/<模型>/      阶段 3（步骤 4）：source.mp4  +  prompt.txt
└── 03_frames/<动作>/<模型>/     阶段 4（步骤 5）：frame_001.png ... + 512/ + 256/
    │                           + sprite_sheet.png  + contact_sheet.png  + meta.json
    └── kept/                   挑帧：只把选中的帧重新裁剪、对齐、缩到 512/256
        ├── frame_017.png ...   选中的帧（和全量帧同一套编号，一眼能对上）
        ├── 512/  256/          交付副本，画布按选中的这批重建
        └── meta.json           选了哪些帧 / 一共多少帧 / fps
```

### 转录之后

```
origin/
  image/
    main.png                # 挑好的原画（阶段 3 的输入）
    main.json               # 连着提示词一起搬过来的侧车
  video/
    monster_imp_walk/                     # 一段动作 = Unity 里一张 Multiple 精灵图
      monster_imp_walk_001.png ...        # 帧，从 001 起，按播放顺序
      512/  256/                          # 同名同前缀的交付副本（整套同一个系数）
      monster_imp_walk.png                # 精灵图（横向平铺）
      monster_imp_walk_contact.png        # 接触表
      meta.json                           # 出处 run / 模型 / fps / 帧数 / 画布 / 转录时间
```

## 生成后处理（默认开启）

参数全部来自 `hareness/common.yaml` 的 `pipeline` 块，命令行只是临时覆盖（优先级：命令行 > common.yaml > 代码默认）。

| 步骤 | 说明 | 对应字段 |
| --- | --- | --- |
| 原图限幅 | 请求里就写不超过 1024px 的尺寸；网关如果还回大图，收到后立刻在本地缩到 1024 | `pipeline.source.max_side` |
| 抠绿 | 把纯绿幕变透明；带**去溢色**，边缘不留绿边 | `pipeline.background.key_color` / `tolerance` / `despill` |
| 裁剪 | 把画布裁到实际有像素的范围，输出尺寸就是资源真实尺寸 | `pipeline.crop.trim` / `trim_padding` / `alpha_threshold` |
| 交付尺寸 | 裁完再按「最长边」缩放，输出 `512/` 与 `256/` 两份副本，原图保留 | `pipeline.sizes.max_sides` |
| 尺寸基准 | 标准人类角色可见高度 512px，其余资源按系数缩放 | `pipeline.scale` |
| 保护 | 边缘颜色对不上绿幕时**自动跳过抠图**（避免误吃画面），只在日志里警告 | |
| 幂等 | 已经透明的图再跑一次不会二次破坏，也不会重复裁剪 | |
| 双通道 | 如果模型这次直接返回了真 alpha（`gpt-image-2.5-sunburst` 有时会给），就跳过抠绿直接裁剪 | |

**整幅就是资源本身的贴图（地块、UI 底板）走 `--opaque`：上表里的抠绿、裁剪、交付尺寸三步都不执行**，见下面「全幅贴图」那一节。

```powershell
python main.py gen hero_knight_2d --count 4                  # 出 4 张，自动抠绿+裁剪
python main.py gen ui_button_panel --key-color auto          # 背景色不是绿时自动探测
python main.py gen boss_dragon --no-cutout                   # 保留绿幕原图
python main.py gen item_potion_icon --trim-padding 8         # 裁剪后留 8px 透明边
python main.py gen hero_mage_2d --sizes 512,256,128          # 额外再出一份 128px
python main.py gen hero_mage_2d --no-sizes                   # 只要全分辨率裁剪结果
python main.py gen hero_mage_2d --max-px 768                 # 临时把原图上限收到 768
python main.py gen hero_mage_2d --no-max-px                  # 不限制原图分辨率
python main.py cutout "resource/image/20260920/20260920-223340_hero_knight_2d" -v   # 对已有图重跑抠图+裁剪
```

### 全幅贴图：`--opaque`（不抠绿、不裁边）

有些资源**整幅就是资源本身**：四边就是接缝，抠绿与裁剪对它都是破坏。

```powershell
python main.py gen env_floor_stone --opaque       # 整幅即资源
python main.py gen ui_button_panel --opaque       # 边框画在图里，裁掉就废了
python main.py gen env_floor_stone --no-opaque    # 临时按普通资源走一遍
```

- 走 `--opaque` 时拼提示词换用 `common.yaml` 的 `opaque_preamble` / `opaque_requirements`
  （整幅即资源、四边无缝、环境光均匀、俯视正交、明暗压窄），固定负面词也换成 `opaque_negative`
  ——默认那套里有 `floor / ground / platform / scenery / environment`，那是给「不许踩在地上的角色」
  写的，对地块正好相反。
- 输出侧同时关掉：抠绿、去溢色、裁剪，以及 `512/256` 副本（留三份只会让人问「哪份才是真的地砖」）。
  命令行显式写 `--sizes` 仍然优先。
- 写进规格就是永久生效：`opaque: true`；`negative_remove: [词, …]` 再从负面词里摘掉几条本项目特有的。
- **无缝是画出来的，不是后处理补得出来的**：出图之后量一遍再接
  （`ResourceSource/Tools/measure-tile-seams.ps1`，看 seam 是否接近 1）。人眼对 1024 原图上的一条
  淡缝不敏感，缩进游戏里反而明显。
## 立绘与参考图（风格一致性）

风格会漂，根因是「用文字描述画风」时，模型每次都要按自己的理解重画一遍。
解决办法：**把裁判权从文字挪到图上。**

角色 spec 里写一行 `reference:` 指向原画，这次请求就走 image-to-image
（`POST /v1/images/edits`），并自动带上 `hareness/common.yaml` 的 `reference_lock`：

> 参考图是唯一裁判——线条粗细、上色平涂、细节量、五官画法、比例、配色全部照抄；
> 不许加头发丝、衣褶、高光、渐变、阴影、材质；文字描述和参考图打架时以参考图为准。

```yaml
reference: [origin/image/main.png]   # 相对项目根目录写；也可以写绝对路径
reference_mode: edit      # edit = 照着参考图重画一张（默认）
                          # copy = 参考图本身就是立绘，只做抠图/裁剪/512-256 副本
```

| 模式 | 行为 | 什么时候用 |
| --- | --- | --- |
| `edit`（默认） | 参考图 + `reference_lock` 一起发给模型，重画一张 | 想要一张干净的正面立绘，但画风必须和原画一模一样 |
| `copy` | **完全不调 API**，参考图直接进后处理 | 原画本身就能当立绘，零风险、零成本 |

`--reference` / `--reference-mode` 可以临时覆盖 spec，给没写 `reference:` 的资源补一张参考图：

```powershell
python main.py gen hero_idle --reference origin/image/main.png               # 照着重画（默认）
python main.py gen hero_idle --reference origin/image/main.png --reference-mode copy
python main.py show hero_idle                                  # 确认提示词里带上了 reference_lock
python main.py gen hero_idle --anim                            # 立绘 + 默认三套动作一起出
```

参考图同样管住动作帧：立绘是照着原画出的，动作帧的参考图是立绘的 **512px 副本**，
所以原画 → 立绘 → 每一帧，每一步都锚在同一张图上。

> 换了原画，只改 spec 里的 `reference:`，`reference_lock` 和尺寸规则都不用动。

## 动作资源（序列帧，可选）

**立绘就是参考图。** 先用 `gen` 出角色立绘（自动抠绿 + 裁剪），代码再拿它的
**512px 副本**当参考图，一帧一次 img2img 请求，画出连续的动作序列。

动作是**可选**的，不是每个资源都要出动作图：

| 方式 | 命令 | 什么时候用 |
| --- | --- | --- |
| 一步到位 | `python main.py gen hero_idle --anim` | 立绘 + 默认三套动作一起出 |
| 指定动作 | `python main.py gen hero_idle --anim idle,walk` | 只要其中几套 |
| 单独补图 | `python main.py anim hero_idle` | 立绘已经有了，回头补动作 |
| 写进 spec | spec 里写 `animations: [idle, walk, hit]` | 这个资源固定要哪几套 |

**既没写 `animations:`、也没加 `--anim` 的资源，永远不会生成动作图。**

默认三套动作定义在 `hareness/animation.yaml`（改动作、加动作只改这一个文件）：

| clip | 帧数 | 循环 | 内容 |
| --- | --- | --- | --- |
| `idle` | 4 | 是 | 站立呼吸循环 |
| `walk` | 6 | 是 | 六帧行走循环，原地走不位移 |
| `hit` | 3 | 否 | 受击后仰 → 仰到顶 → 收招 |

（`gen --anim` / `anim` 这条是「一帧一次 img2img」，画出来的帧数就是上表的帧数；
走视频那条路（阶段 3~4）是另一个办法，帧数由视频决定，见上文「四阶段流水线」。）

### 怎么保证「连续」

- 每次请求都带上 **512px 立绘**：锁死「是谁」——脸、衣服、配色、线条粗细、比例都不许变；
- 再带上 **已经画好的前几帧**：锁死「动作接到哪了」（默认带 1 帧，`--history` 可调）；
- `animation.yaml` 的 `continuity` 段一次写死机位、角色大小、地平线、光向、构图，
  并明确「一帧一次请求，不画 sprite sheet」；
- 每帧的姿势写在同一文件的 `frames_text` 里，一帧一句。

### 对齐 = 不抖

每帧都是独立渲染的，模型不会把角色放在完全相同的位置，所以出完帧以后代码会：

1. **对齐**：取整套帧 alpha 包围盒的**并集**当共享画布，每帧只做**同一个**平移，
   模型自己画的构图（角色在画面里的位置、地平线）原样保留 —— 不重新居中、不重新贴底；
2. **整体缩放**：`512/` `256/` 副本按**同一个系数**缩放整套动作
   （不是每帧单独缩放，那样会把对齐毁掉）；
3. **拼条**：额外写一张 `<clip>_sheet.png` 横向序列图，Unity 里 `Sprite Mode = Multiple` 直接切。

```powershell
python main.py anim hero_idle --clips idle,walk -v     # 指定动作 + 看日志
python main.py anim hero_idle --frames 6               # 临时改每套动作的帧数
python main.py anim hero_idle --from "resource/image/<年月日>/<run>/512/<名字>.png"   # 换一张图当参考图
python main.py anim hero_idle --history 2              # 多带一帧做连续性参考
python main.py anim hero_idle --no-align --no-sheet    # 不要对齐 / 不要拼条
python main.py anim hero_idle --dry-run -v             # 只看提示词，不调 API
```

单个角色的额外设置写在 spec 里：

```yaml
animations: [idle, walk]        # 这个资源要哪几套动作
animation_hint: |               # 每一帧都必须保持的特征
  keep the black topknot and the cream robe exactly as in the portrait
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python main.py list` | 列出全部提示词、风格预设与当前网关配置 |
| `python main.py common` | 打印每次请求都会带上的公共描述 |
| `python main.py show <id>` | 打印拼装后的 prompt（不调用 API） |
| `python main.py gen <id> [<id> ...]` | 生成指定资源 |
| `python main.py gen <id> --reference origin/image/main.png` | 拿原画当参考图生成（见上文），`--reference-mode copy` 则直接用原画 |
| `python main.py gen-all` | 生成 `hareness/characters/` 里的全部资源 |
| `python main.py probe` | 检查令牌 / 网关 / 模型列表 |
| `python main.py gen <id> --anim` | 生成立绘后接着出动作帧（可选加 `idle,walk`） |
| `python main.py anim <id> [<id> ...]` | 用已有立绘的 512px 副本出动作帧 |
| `python main.py cutout <路径...>` | 对已有 PNG 抠绿 + 裁剪 |
| `python main.py runs [--json]` | 列出 `resource/image` 和 `resource/video` 里的 run，以及各自跑到哪个阶段（四颗点）；底部一行是「阶段名 <-> 步骤号」的对照 |
| `python main.py select [--frames "0-40,45"] [--clips walk] [--clear]` | 挑帧：把选中的帧写进这一段旁边的 `kept/`（`--clear` 是挪到 `temp/trash`，不删） |
| `python main.py promote <路径> [--name <名字>] [--force]` | 转录：把一个原画或一段帧序列搬进 `origin/` |
| `python main.py run <id> --clips ...` | 一次跑完四个阶段（五个步骤） |
| `python main.py run --from 2 --to 3 --run <run>` | 只跑阶段 2（原画处理：两条命令、两个目录，纯本机不调 API） |
| `python main.py stage1 ... stage5` | 只跑某一个步骤（`--from` / `--to` 也是步骤号；`--run <年月日>/<名字>` 指定目标） |
| `python main.py ui` | 打开网页控制台，见上文「网页控制台」 |

## 调用方式

| api-style | 端点 | 适用模型 |
| --- | --- | --- |
| `images`（默认） | `POST {BASE}/v1/images/generations` | GPT 图像模型 |
| `openai` | `POST {BASE}/v1/chat/completions` | 走 Chat 接口出图的模型 |
| `gemini` | `POST {BASE}/v1beta/models/{model}:generateContent` | Gemini 原生接口 |
| `images`（带参考图） | `POST {BASE}/v1/images/edits` | 动作帧用的图生图接口 |

动作帧走的是图生图：`images` 走 `/v1/images/edits`（multipart 上传），`openai` 走
`/v1/chat/completions` 的 `image_url` data URI，`gemini` 走 `inlineData`。
首选路线不支持时会自动换另一条（日志里会写「trying ...」）。

`--extra-json '{"...": ...}'` 可以往请求体里合并任意字段，用来适配不同网关。

## 配置（.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RELAY_API_KEY` | 你的令牌 | 网关令牌（也兼容 `OPENAI_API_KEY` / `GEMINI_API_KEY`） |
| `RELAY_BASE_URL` | `http://token.wd.com` | 网关地址（只写域名，路径由代码拼接） |
| `DEFAULT_MODEL` | `gpt-image-2.5-sunburst` | 默认模型 |
| `DEFAULT_API_STYLE` | `images` | `images` / `openai` / `gemini` |
| `AUTH_STYLE` | `bearer` | `bearer` 或 `x-goog-api-key` |
| `REQUEST_TIMEOUT` / `MAX_RETRIES` / `RETRY_BACKOFF` | 300 / 2 / 5 | 超时与重试 |
| `HTTP_PROXY_URL` | 空 | 需要代理时填写 |
| `HARNESS_DIR` / `OUTPUT_DIR` / `COMMON_FILE` | `hareness` / `resource` / `common.yaml` | 目录配置（新名见「路径」一节） |

出图尺寸上限、抠图、裁剪、交付尺寸这些参数不放在 `.env`，统一写在
`hareness/common.yaml` 的 `pipeline` 块里（唯一真源）。

## 排错

| 现象 | 处理 |
| --- | --- |
| `401` / 令牌无效 | 检查 `.env` 的 `RELAY_API_KEY` |
| `404` | `RELAY_BASE_URL` 不对，或模型名不在网关上（用 `probe` 看列表） |
| `400 This model is not supported on the Chat Completions endpoint` | 该模型要走 `--api-style images` |
| `400 Transparent background is not supported` | 这些模型不支持直接出透明，用绿幕 + 抠图 |
| 改了规则但出图没变化 | 确认改的是 `hareness/common.yaml`，并用 `python main.py common` 核对生效值 |
| `run 要写成 <日期>/<名字>` | `--run` 的写法：`--run 20260923/20260923-172725_monster_imp`；`resource/`、`image/` 前缀可以带也可以不带 |
| `resource 里还没有 run` | 先 `stage1` 画一张，或 `promote` 一张母版；`python main.py runs` 看现在有什么 |
| `origin/image 里也没有原稿` | 阶段 3 读的是 `origin/image`。先在页面上挑一张点「转录」，或 `promote <png>`（默认名就是实体名） |
| `origin 里已经有 ...` | 目标名字被占了（返回码 1）。换个 `--name`，确认要覆盖就加 `--force` |
| 拷进来的帧目录名字/帧号不统一 | 页面上点那颗「整理」，或 `python main.py normalize "<origin/video 下的目录>"` |
| 日志说 "the border is not ... key colour, keying skipped" | 这张没生成绿幕，原图保留；可 `--key-color auto` 或重跑 |
| 一张请求返回多张图 | 网关自己决定返回数量（会忽略 `n`），代码会全部保存 |
| 想省额度 | 先 `--dry-run -v` 看提示词，或 `python tools/mock_server.py` 本地假接口测试 |

## Unity 使用建议

- 生成的 PNG 已经带 alpha，导入后 `Texture Type = Sprite (2D and UI)`、`Alpha Is Transparency = 勾选`。
- 像素风资源记得 `Filter Mode = Point`、`Compression = None`。
- 帧序列直接用 `origin/video/<实体>_<动作>/` 里的 `512/`：整套帧已经对齐、按同一系数缩放过，
  拖进去就是一串连续的 Sprite；或者用同名的 `<实体>_<动作>.png` 配 `Sprite Mode = Multiple` 一次切开。
  文件前缀就是 `<实体>_<动作>`，所以 Unity 里的 sprite 名（`monster_imp_walk_001`）和磁盘上的名字一致。
- 元数据 `.json` 里记录了 prompt、模型、最终尺寸与后处理结果（以及帧数、画布、漂移、循环点），
  路径都是相对项目根的，方便复现与批量对账。
- 想统一规格，可用 `--trim-padding` 留出固定透明边，再按 `common.yaml` 里的 512×512 人类基准做缩放。

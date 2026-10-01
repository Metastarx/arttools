# hareness —— 提示词库

存放"提示词"的文件夹，和代码完全分离，改 prompt 不需要动 Python。

```
hareness/
  common.yaml       公共提示词 + 全局参数（唯一真源，所有资源共享）
  style/
    _common.yaml    风格基底：全项目统一的视觉语言，所有预设自动继承
    inkline_2d.yaml 各风格预设：只写自己特有的部分
    ...
  characters/       每个资源一个 yaml（角色、怪物、道具、地块、UI、特效）
  _template.yaml    新建资源时复制这个文件
  animation.yaml    动作库：有哪些动作、每套几帧、每帧什么姿势（可选，不用动作可以不管）
```

两层公共描述，分工不重叠：

| 文件 | 管什么 | 例子 |
| --- | --- | --- |
| `common.yaml` | **工程规则**：背景色、尺寸基准、构图、输出要求 | 绿幕 `#00FF00`、512px 人类基准、裁剪规则 |
| `style/_common.yaml` | **美术风格**：画风、线条、配色、文化元素 | 国风墨线、粗黑轮廓、平涂、朱红/墨黑/玉绿配色 |

## common.yaml —— 全项目唯一真源

**换对话、换人、换机器，只要这个文件不变，出图规格就不变。**
它同时管两件事，改的时候两边要一起改：

| 半边 | 字段 | 作用 |
| --- | --- | --- |
| 发给模型 | `preamble` | 固定开场：背景规则 + 尺寸比例 + 构图规则 |
| 发给模型 | `requirements` | 固定收尾：交付前自检清单 |
| 发给模型 | `negative` | 固定负面词，和 style / spec 的 negative 合并去重 |
| 发给模型 | `reference_lock` | **参考图锁**：这次请求带了参考图时才会发，声明「图是唯一裁判，不许加细节」 |
| 发给模型 | `send_as_system` | `false`（默认）并进 prompt 文本，兼容所有中转平台；`true` 时 gemini 走 `systemInstruction`、openai 走 system 消息 |
| 给代码 | `pipeline.background` | 绿幕色号 `#00FF00`、抠图容差、去溢色开关、是否自动抠图 |
| 给代码 | `pipeline.crop` | 是否裁剪到内容边界、裁剪后留多少透明边、alpha 阈值 |
| 给代码 | `pipeline.sizes` | 交付尺寸：裁完再按最长边缩放，输出 `512/` `256/` 子目录副本 |
| 给代码 | `pipeline.reference_mode` | 有参考图时怎么用：`edit` 照着重画（默认）/ `copy` 直接采用 |
| 给代码 | `pipeline.scale` | 比例基准（512px 人类基准 + 各类资源系数） |
| 给代码 | `defaults` | 画布兜底参数（`aspect_ratio` / `image_size`） |
| 给人看 | `reuse` | 新对话开场白模板 + 常用命令，不发给模型 |

### 固定的三条硬规则

1. **背景固定纯绿 `#00FF00`**，实心平涂，模型不输出真透明就靠它；
2. **抠绿成透明**（`pipeline.background.tolerance`，带去溢色，边缘不留绿边）；
3. **裁剪到内容边界**（`pipeline.crop.trim`，输出尺寸就是资源真实尺寸）；
4. **交付尺寸固定**（`pipeline.sizes.max_sides`，默认再出最长边 512px 和 256px 两份）。

### 尺寸约束怎么用

`pipeline.scale.human_reference_px = 512` 是全项目的尺子：标准人类角色的**可见高度**是 512px，
其他资源按系数换算（判的是可见包围盒，不是画布大小）：

| 类别 | 系数 | 对应可见高度 |
| --- | --- | --- |
| `small_prop` 手持小道具 | 0.25 - 0.50 | 128 - 256 px |
| `standard_prop` 常规道具 / 图标 | 0.50 - 0.75 | 256 - 384 px |
| `human_character` 人类角色 | 1.00 | ≈ 512 px |
| `large_monster` 大型怪物 | 1.25 - 2.00 | 640 - 1024 px |
| `boss` Boss | 2.00 - 3.00 | 1024 - 1536 px |

### 换对话时怎么说

把 `reuse.tell_new_session` 里那句话直接发给新对话即可（`python main.py common` 会打印出来）：

> 以 hareness/common.yaml 为唯一规范生成美术资源：背景固定纯绿 #00FF00，
> 抠绿成透明后裁剪到内容边界，标准人类角色可见高度 512px，其余资源按此比例缩放。
> 不要修改背景色和尺寸规则，也不要手工改图。

### 自检与预览

- 看当前生效的全部规则：`python main.py common`
- 打印最终 prompt：`python main.py show <id>`
- 预览时先不看公共段：`python main.py show <id> --no-common`

组装顺序：

```
common.preamble
-> [_common.prompt_prefix + style.prompt_prefix]   # 风格基底 + 预设自己的部分
-> common.reference_lock                           # 只有带了参考图才会插进来
-> spec.prompt
-> style.quality_suffix
-> common.requirements
-> Avoid(common.negative + spec.negative + [_common.negative + style.negative])
```

优先级：**命令行参数 > common.yaml 的 pipeline 块 > 代码内默认值**。
所以平时不用加任何参数，命令行参数只用于临时实验。

## style/_common.yaml —— 风格基底（所有预设自动继承）

把「全项目统一的视觉语言」写在这里一次，所有 `style/*.yaml` 预设都会自动继承，
角色、敌人、道具、场景因此共享同一套画风。

- 文件名以下划线开头，不会被当成可选风格，也不会出现在 `list` 里。
- 合并顺序：`_common.prompt_prefix` → 预设自己的 `prompt_prefix` → 角色 prompt → 预设 `quality_suffix`。
- 负面词去重合并：`common.negative` + 角色 negative + `_common.negative` + 预设 negative。

预设里可以微调继承行为：

| 预设字段 | 作用 |
| --- | --- |
| `inherit_common: false` | 完全不继承基底（用于像素风、3D 渲染这类另一种媒介） |
| `negative_remove: [pixel art]` | 只剔除基底的某几条负面词，其他照旧继承 |

`python main.py list` 会逐个标出「继承 _common.yaml」还是「独立风格」。

## reference —— 参考原画（风格一致性的开关）

画风漂移的根因是「用文字描述画风」：模型每次都会按自己的理解重画一遍。
所以让 spec 直接挂一张原画，把裁判权交给图：

```yaml
reference: [origin/image/main.png]   # 相对项目根目录，可以写多张
reference_mode: edit      # edit = 照着参考图重画一张（默认）；copy = 直接用参考图当资源
```

有这一行时：

1. 请求走 image-to-image（`images` 走 `/v1/images/edits`，multipart 上传原画）；
2. prompt 里自动插入 `common.yaml` 的 `reference_lock`，位置在 style 之后、角色描述之前，
   所以它压得住写死的画风描述和角色描述；
3. 元数据 `.json` 里会记下 `references` 和 `reference_mode`，方便复盘。

`copy` 模式完全不调 API：原画直接进抠图 / 裁剪 / 512-256 那一套后处理，风格 100% 不变，
适合「原画本身就能当立绘」的情况。

命令行可以临时覆盖：`python main.py gen <id> --reference origin/image/main.png --reference-mode copy`。

## animation.yaml —— 动作库（序列帧）

只管「动作本身」，背景色 / 尺寸 / 抠图 / 裁剪仍然全部来自 `common.yaml`，不重复写。
立绘（`gen` 出来的那张）的 **512px 副本**就是动作帧的参考图，一帧一次请求。

| 字段 | 说明 |
| --- | --- |
| `default_clips` | spec 写 `animations: all` 时展开成哪几套，默认 `[idle, walk, hit]` |
| `continuity` | **帧间连续性规则**，每一帧请求都会带上：锁机位、锁大小、锁地平线、锁画风，并说明「一张图 = 一帧，不画 sprite sheet」 |
| `negative` | 动作帧额外的负面词，和 `common.negative` / style / spec 的一起合并去重 |
| `clips.<id>.frames` | 这套动作几帧 |
| `clips.<id>.loop` | `true` 首尾要能无缝循环；`false` 一次性动作（受击这种） |
| `clips.<id>.motion` | 这套动作整体在干嘛，会写进每一帧 |
| `clips.<id>.frames_text` | 逐帧姿势，一条一帧；数量可以和 `frames` 不同，会循环取用 |

加一套新动作 = 在 `clips:` 下面加一个块，写清 `frames` / `loop` / `motion` / `frames_text`，
再用 `python main.py anim <id> --clips <新动作>` 就能出图，不用改 Python。

动作相关的 spec 字段（写在 `hareness/characters/*.yaml` 里）：

| 字段 | 说明 |
| --- | --- |
| `animations` | 这个资源要哪几套动作，例如 `[idle, walk, hit]` 或 `all`；**不写就不出动作图** |
| `animation_hint` | 可选，每一帧都必须保持的角色特征（发型、服装、配色等），会写进每一帧的提示词 |

动作帧的参考图永远是**立绘的 512px 副本**，而 `common.reference_lock` 同样会插进每一帧的
提示词里，所以「原画 → 立绘 → 动作帧」这条链上每一步都锚在同一张原画上。

## 一个 yaml 的结构

| 字段 | 说明 |
| --- | --- |
| `id` | 唯一 id，也是输出文件夹名，例如 `hero_knight_2d` |
| `title` | 英文名，供 prompt 里的 `{name}` 使用 |
| `name` | 中文备注，只出现在日志和 `list` 输出里 |
| `category` | `character` / `monster` / `item` / `environment` / `ui` / `vfx` |
| `style` | 引用 `hareness/style/<id>.yaml` |
| `tags` | 标签数组，可用 `{tags}` 占位符 |
| `aspect_ratio` | `1:1`、`3:4`、`16:9` ...（覆盖 style 里的默认值） |
| `image_size` | 可选，`1K` / `2K` / `4K`（只有部分模型支持） |
| `model` / `api_style` | 可选，单独指定模型或调用方式 |
| `count` | 生成几张，缺省等于 variations 的数量 |
| `reference` | 可选，参考原画（相对仓库根目录）；写了就走图生图，见上一节 |
| `reference_mode` | 可选，`edit` 照着重画（默认）/ `copy` 直接采用原画 |
| `prompt` | 主提示词，`{xxx}` 会被 variations 的同名字段替换 |
| `variations` | 一组组参数，每组一次请求 |
| `negative` | 额外要避免的内容，会和 style 的 negative 合并 |
| `animations` | 可选，要出哪几套动作（`[idle, walk, hit]` / `all`）；不写就不出动作图 |
| `animation_hint` | 可选，每一帧都必须保持的角色特征 |

## 新增一个角色

1. 复制 `_template.yaml` 成 `hareness/characters/my_hero.yaml`；
2. 填 `id` / `title` / `style` / `prompt` / `variations`；
3. 想让画风和某张原画一致，加一行 `reference: [原画.png]`（不加就是纯文生图）；
4. 预览不花钱：`python main.py show my_hero`；
5. 正式生成：`python main.py gen my_hero`（要动作就加 `--anim`）。

## 新增一个风格预设

复制 `style/handpainted_2d.yaml` 改成自己的 `style/my_style.yaml`，
里面写 `prompt_prefix`（风格定语）、`quality_suffix`（画质要求）、`negative`（要避免的），
然后在 spec 里 `style: my_style` 引用。

## prompts/ —— 视频提示词预设（给动的那个模型）

`common.yaml` 那一套管的是**画原画**的图像模型；`prompts/` 管的是**第 4 步出视频**的 Seedance。
两边分开是有原因的：原画要的是「画成什么样」，视频要的是「动起来什么样」，条数、寿命、改法都不一样。

拼装顺序（从固定到灵活）：

```
src/video.py 内置   （风格锁 / 镜头锁死 / 绿幕 / 循环 —— 抠帧动画的前提，别用预设推翻）
  + prompts/common.xml        常驻，每次自动带上
  + prompts/<勾的那些>.xml    按动作勾
  + --extra-prompt 手写的那句
```

- 文件名（不带后缀）就是预设名：`walk_side.xml` 就是 `--preset walk_side`。
- `common` 常驻，页面上勾死、命令行不用写；`_` 开头和 `README` 不算预设（这个文件夹可以自己写文档）。
- 也吃 `.txt` / `.md` / `.yaml` / `.yml` / `.json`，用现成文件就行；`.xml` 里 `<prompt title="...">`
  的 `title` 当显示名。
- 页面上在第 3 步勾，记进 `01_video_input/prompts.json`；第 4 步自动读回来。

细节和现有几个预设的用途见 `prompts/README.md`。

## 写提示词的几个经验

- **不要在角色 prompt 里再写画风**：画风由 `style/_common.yaml` 统一规定，role prompt 只描述「这是什么」。 
- **不要在角色 prompt 里再写背景色**：背景由 `common.yaml` 统一规定，重复写容易和固定色号冲突。
- **不要在角色 prompt 里再定尺寸**：尺寸由 `pipeline.scale` 的 512px 基准统一控制。
- 同一角色的多个动作/配色放进 `variations`，风格一致性靠 `style` 预设保证。
- **有原画就挂 `reference:`**：文字描述画风永远不如直接给图，参考图 + `reference_lock` 是最硬的锁风格手段。
- 像素风要写清尺寸感（`readable at 64x64 pixels`），不要让模型自己猜分辨率。
- 想让模型换配色时，把颜色写成变量（`{colour}`），比写死更容易批量出图。

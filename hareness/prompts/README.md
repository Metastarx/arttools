# hareness/prompts —— 提示词预设

这一层不写代码，只放提示词片段。页面上「阶段 3 · 视频输入」里的**提示词预设**读的就是这个
文件夹，勾中的文件会拼在 `src/video.py` 内置提示词（风格锁、固定镜头、绿幕、循环）**后面**，
最后才是页面上手写的那一段。

## 怎么用

- 文件名（不带后缀）就是预设的名字：`walk_side.xml` 勾选时叫 `walk_side`。
  **预设名不是动作名**：动作是 `idle` / `walk` / `hit` 这类（`common.yaml` 的
  `pipeline.video.clips`），预设只是「给这个动作挑的说法」。所以 `walk_side` 是给 `walk` 用的，
  `idle_breath` 给 `idle`，`hit_knockback` 给 `hit`；把预设名当动作点下去只会多出一个叫
  `walk_side` 的动作，而 `origin` 里落下的名字会是 `实体_walk_side`。
- `common` 是**常驻**的，每次生成都会带上，页面上勾死了取消不掉，命令行也不用写。
- 其余的都是「有特点的」，按这次要生成的动作勾：`--preset walk_side --preset loop_soft`。
- 支持 `.xml` `.txt` `.md` `.yaml` `.yml` `.json`，用你手头已有的文件就行。
- 以 `_` 或 `.` 开头的文件、以及 `README` / `INDEX` 会被跳过，所以这个文件夹可以自己写文档、
  可以放样板（`_template.xml`）。
- `<prompt title="...">` 的 `title` 会当成显示名；没有就用文件名。

## 命令行

```bash
python main.py stage3 --source origin/image/main.png --preset walk_side --extra-prompt "披风再飘一点"
python main.py stage4 --run 20260924/20260924-101530_main       # 自动带上阶段 3 记下的预设
```

`--extra-prompt` 是给这一次单独补的话，写在最后。什么都不给时，`stage4` 会用 `stage3` 记在
`01_video_input/prompts.json` 里的那一份，所以「页面里挑好 → 命令行跑」不会把预设丢掉。

## 这里都放了什么

| 文件 | 用途 |
| --- | --- |
| `common.xml` | 常驻。线稿外框、平涂、角色同一性、不加料、绿幕不动 —— 全是「风格一致性」的硬约束 |
| `walk_side.xml` | 侧面行走循环，把 contact / down / passing / up 四个关键姿势写死 |
| `idle_breath.xml` | 站立呼吸，只允许呼吸和极小的微动 |
| `hit_knockback.xml` | 受击后仰再回到站姿，首尾同姿势 |
| `_template.xml` | 样板，复制着改 |

## 注意

预设是**加在**内置提示词后面的，不要在这里写和内置提示词冲突的话（换背景色、改分辨率、
让角色往某个方向位移之类）。内置提示词里「镜头锁死、角色钉在原地」是不能推翻的那一条 ——
推翻了他就不再适合抠帧了。

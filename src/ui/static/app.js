'use strict';

/* 美术流水线控制台 —— 前端
   五步各占一个面板，面板里的每个字段都只是拼一条 python main.py 命令。
   命令由后端 build_command 拼，前端拿到的就是将要执行的那一行。 */

/* ---------------------------------------------------------------- 小工具 */

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function clear(node) { node.textContent = ''; return node; }

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let n = bytes, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + units[i];
}

function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
}

function savePref(key, value) {
  try { localStorage.setItem('art.ui.' + key, JSON.stringify(value)); } catch (err) { /* 无痕模式 */ }
}

function loadPref(key, fallback) {
  try {
    const raw = localStorage.getItem('art.ui.' + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (err) { return fallback; }
}

async function readJSON(response) {
  let body = {};
  try { body = await response.json(); } catch (err) { body = { error: response.statusText }; }
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

const getJSON = (url) => fetch(url, { cache: 'no-store' }).then(readJSON);
const postJSON = (url, payload) => fetch(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
}).then(readJSON);

/* 带上文件的 mtime：URL 变了，浏览器才会重新取，没变就可以放心缓存。
   播放一段 119 帧的动画，靠的就是这个。 */
const fileURL = (item, width) =>
  '/api/file?path=' + encodeURIComponent(item.path)
  + (width ? '&w=' + width : '')
  + (item.mtime ? '&v=' + Math.round(item.mtime) : '');

const defaultModels = () => (state.defaults.models || []).slice();

/* ---------------------------------------------------------------- 状态 */

const STATUS_TEXT = { running: '运行中', done: '完成', failed: '失败', stopped: '已停止' };

/* 左侧的浏览根：流水线的两棵树，加上 origin 的两半。
   origin 一个文件夹装两种东西（挑好的原画、挑好的帧序列），所以拆成两个页签 ——
   「哪张原画」和「哪段动作」本来就是在两个时候问的两个问题。
   kept / old 只是还能按名字读（老链接不至于掉进空视图），归档从顶栏那颗按钮进。 */
const ROOT_LABELS = {
  image: '原画', video: '视频', kept_image: '保留-原画', kept_video: '保留-视频',
  kept: '保留', old: '归档',
};
/* 切到一棵树、而这棵树还没被停过时，落在哪个阶段：原画树从头画起，视频树直接去
   「视频生成」，保留区看的是别人的四个阶段 —— 挑好的原画停在「原画处理」，挑好的
   帧停在「视频处理」，归档是只读的旧目录。 */
const DEFAULT_STAGE = {
  image: 1, video: 3, kept: 2, kept_image: 2, kept_video: 4, old: 1,
};

const ROOT_HINTS = {
  image: 'resource/image —— LLM 画的原画，和抠好、裁好的 512 / 256',
  video: 'resource/video —— Seedance 生成的视频，和切出来的帧',
  kept_image: 'origin/image —— 挑好留下的原画；阶段 2 就是拿这里的东西做视频输入',
  kept_video: 'origin/video —— 挑好留下的帧序列，Unity 直接导这批',
  kept: 'origin —— 挑好留下的原画和帧序列',
  old: 'resource/old —— 旧目录结构留下的归档，只读',
};

/* 保留区那几个视图（合并的那个 + 拆开的两半）看的是同一批东西，只是范围不同。 */
const KEPT_ROOTS = ['kept', 'kept_image', 'kept_video'];
const VIEW_TITLE = {
  kept: '保留 · origin',
  kept_image: '保留 · origin/image',
  kept_video: '保留 · origin/video',
  old: '归档 · resource/old',
};

const state = {
  paths: {}, pathsAbs: {}, env: {}, defaults: {}, specs: [], styles: [], stages: [],
  presets: [], artwork: [], promptDir: '', promptDirAbs: '',
  rootKeys: ['image', 'video', 'kept_image', 'kept_video'],
  root: 'image', q: '',
  runs: null, selected: null, selectedRun: null,
  /* runPath 是右栏此刻针对的 run（day/name）。在「原画 / 视频」两棵树里它就是选中的
     run；在「保留」里它是这件保留物出处的那条 run，可能没有。表单和产出面板都读它，
     所以点了保留里的原画，右栏照样是那一条 run 的四个阶段，而不是只剩一张大图。 */
  runPath: null, kept: null,
  jobRun: null,
  /* 哪一行的「→ 保留」展开着候选（一行有多个可转录的东西时才需要展开）。 */
  promoteOpen: null,
  /* 阶段 2 记下来的预设只往阶段 3 的表单里灌一次，按 run 去重：灌完再让人改，
     切走再切回来不该把改过的东西又按回原样。 */
  presetsFilledFor: null,
  detail: null, detailError: null,
  stage: 1,
  /* 每棵树各记各的阶段。原画树停在哪一阶段、视频树停在哪一阶段，是两回事。 */
  stagesByRoot: {},
  forms: {},
  job: null, scannedJob: null, scan: 0,
  previewBox: null, previewToken: 0, previewTimer: null,
  refsRedraw: null,
  shot: '',
};
/* 每一步要填什么。kind: text / number / select / check / checks / spec / refs */
const FIELDS = {
  1: [
    { key: 'spec', label: '角色（可多选）', kind: 'specs',
      help: 'hareness/characters 下的 id，勾几个就跑几个，各画各的（阶段 1 不针对某个 run）' },
    { key: 'count', label: '张数', kind: 'number', ph: '留空 = spec 自己定的张数' },
    { key: 'style', label: '风格预设', kind: 'style', ph: '留空 = spec 里写的那个' },
    { key: 'dry_run', label: '只打印提示词（--dry-run）', kind: 'check',
      help: '不调用 API，不花钱，用来检查提示词' },
  ],
  2: [
    /* 这一步是「画完到能生成视频之间」的全部本机活，要的是三样东西：哪张原画、
       勾哪些提示词预设、还想补一句什么 —— 剩下几个是本机的裁剪开关。
       run 是可选的：没选 run 时会照原画的名字新开一条。 */
    { key: 'source', label: '原画', kind: 'artwork',
      help: 'origin/image 里挑好的原画。左边「原画」树里看中的那张，点「转录到 origin」'
            + '就会出现在这里；留空 = 用左边这条 run 自己的 01_artwork' },
    /* 这两个字段这一步自己一个字都不用：阶段 2 全是本机处理（抠绿、裁剪、补绿），
       不调任何模型。勾在这里是为了**记下来** —— 它们会写进这条 run 的
       01_video_input/prompts.json，下面的「3 视频生成」默认就照这几个走。
       所以标题上写明是给谁的，不然一个本机阶段问提示词，谁看都懵。 */
    { key: 'presets', label: '提示词预设 · 给「3 视频生成」', kind: 'presets',
      help: '这一步全是本机处理，一个提示词都不发给模型。勾在这里是把它记进这条 run 的'
            + ' 01_video_input/prompts.json，「3 视频生成」默认就照这几个走。'
            + 'common 是常驻的，勾死取消不掉' },
    { key: 'extra_prompt', label: '补充提示词 · 给「3 视频生成」', kind: 'textarea', rows: 4,
      ph: '这次单独想补的话，写在这里，会加在所有预设的最后面',
      help: '写给视频模型的一句人话，例如「披风再飘一点」。这一步自己不用，是存给'
            + '「3 视频生成」的；留空就是只用预设' },
    { key: 'fill', label: '角色占画布比例', kind: 'number', ph: '0.7', step: 0.05,
      def: () => String(state.defaults.fill === undefined ? 0.7 : state.defaults.fill),
      help: '补绿的时候把角色缩到画布的这个比例，四周剩下的就是纯绿留白 —— 防的是视频模型'
            + '把发髻或脚裁掉' },
    { key: 'tolerance', label: '抠图容差', kind: 'number', ph: '留空 = common.yaml 里的 48' },
    { key: 'sizes', label: '交付尺寸', kind: 'text', ph: '512,256',
      def: () => (state.defaults.sizes || []).join(','), help: '裁剪之后再按最长边出副本' },
    { key: 'no_sizes', label: '不出尺寸副本（--no-sizes）', kind: 'check' },
    { key: 'no_trim', label: '不裁剪，保留整张画布（--no-trim）', kind: 'check' },
  ],
  3: [
    { key: 'clips', label: '动作', kind: 'clips', ph: 'idle,walk,hit',
      def: () => (state.defaults.clips || []).join(','),
      help: '一个动作就是一条 clip，也是一条提示词；名字会和 hareness/prompts 里的预设对上' },
    { key: 'models', label: '视频模型', kind: 'checks',
      def: defaultModels, optionsFor: defaultModels,
      help: '每个模型单独一个子目录，方便对比；全不勾 = 用 common.yaml 的默认' },
    { key: 'seconds', label: '秒数', kind: 'number', ph: '5',
      def: () => String(state.defaults.seconds === undefined ? 5 : state.defaults.seconds),
      help: '3-5 秒就够一个循环' },
    { key: 'facing', label: '朝向', kind: 'select', options: ['left', 'right'],
      def: () => state.defaults.facing || 'left' },
    { key: 'resolution', label: '分辨率', kind: 'text', ph: '720p',
      def: () => state.defaults.resolution || '720p' },
    /* 阶段 2 勾过的预设记在这条 run 里。这里默认就是那几个 —— 显示的和跑起来用的是
       同一份，不然界面上写着"只用内置提示词"，命令又悄悄带上了阶段 2 的预设。 */
    { key: 'presets', label: '提示词预设', kind: 'presets',
      help: '默认沿用阶段 2 在这条 run 上勾的那几个；这里改只影响这一次' },
    { key: 'references', label: '逐个动作指定参考图（--reference）', kind: 'refs',
      help: '留空 = 用阶段 2 补好绿的那张（01_video_input）' },
    { key: 'force', label: '已经有视频也重跑（--force）', kind: 'check',
      help: '已经生成过的会再花一次钱' },
  ],
  4: [
    { key: 'clips', label: '动作', kind: 'clips', ph: 'idle,walk,hit',
      help: '这一段里已经有视频的动作；留空 = 全部' },
    { key: 'models', label: '只处理这些模型', kind: 'checks', def: () => [], optionsFor: runModels,
      help: '默认全部；这里列的是这个 run 里真的切过帧的模型' },
    { key: 'skip_frames', label: '丢掉开头几帧', kind: 'number', ph: '2',
      def: () => String(state.defaults.skip_frames === undefined ? 2 : state.defaults.skip_frames),
      help: '开头这几帧其实就是参考图本身' },
    { key: 'flatten_tolerance', label: '背景压平容差', kind: 'number', ph: '40',
      def: () => (state.defaults.flatten_tolerance === undefined ? '' : String(state.defaults.flatten_tolerance)),
      help: 'H.264 在纯色上有噪点，压平回同一个色号再抠' },
    { key: 'no_flatten', label: '不压平背景（--no-flatten）', kind: 'check' },
    { key: 'sizes', label: '交付尺寸', kind: 'text', ph: '512,256',
      def: () => (state.defaults.sizes || []).join(',') },
    { key: 'no_sizes', label: '不出尺寸副本（--no-sizes）', kind: 'check' },
    { key: 'no_trim', label: '不裁剪（--no-trim）', kind: 'check' },
  ],
  all: [
    { key: 'spec', label: '角色（可多选）', kind: 'specs',
      help: '只有没选 run 的时候才用得上：用它从头画阶段 1' },
    { key: 'from_stage', label: '从第几阶段开始', kind: 'select', options: ['1', '2', '3', '4'],
      def: () => '1', help: '选了 run 就从这一阶段往后跑，一直到阶段 4' },
    { key: 'clips', label: '动作', kind: 'text', ph: 'idle,walk,hit',
      def: () => (state.defaults.clips || []).join(',') },
    { key: 'models', label: '视频模型', kind: 'checks',
      def: defaultModels, optionsFor: defaultModels },
    { key: 'seconds', label: '秒数', kind: 'number', ph: '5' },
    { key: 'facing', label: '朝向', kind: 'select', options: ['', 'left', 'right'], def: () => '' },
    { key: 'fill', label: '角色占画布比例', kind: 'number', ph: '0.7', step: 0.05 },
    { key: 'resolution', label: '分辨率', kind: 'text', ph: '720p' },
    { key: 'skip_frames', label: '丢掉开头几帧', kind: 'number', ph: '2' },
    { key: 'sizes', label: '交付尺寸', kind: 'text', ph: '512,256' },
    { key: 'force', label: '已经有视频也重跑（--force）', kind: 'check' },
  ],
};


function formOf(stage) {
  const key = String(stage);
  if (!state.forms[key]) {
    const form = {};
    for (const field of FIELDS[key] || []) {
      if (field.def) form[field.key] = field.def();
      else if (field.kind === 'check') form[field.key] = false;
      else if (field.kind === 'checks') form[field.key] = [];
      else if (field.kind === 'specs') form[field.key] = [];
      else if (field.kind === 'presets') form[field.key] = commonPresets();
      else form[field.key] = '';
    }
    // 提示词预设里 common 是常驻的，每次都默认带上。
    if ((FIELDS[key] || []).some((field) => field.key === 'presets')) {
      form.presets = commonPresets();
    }
    // 记住上次勾了哪些角色：画同一批东西是常事，不该每回都重勾一遍。
    if ((FIELDS[key] || []).some((field) => field.key === 'spec')) {
      const saved = loadPref('spec', []);
      const list = Array.isArray(saved) ? saved : (saved ? [saved] : []);
      form.spec = list.filter((id) => state.specs.some((spec) => spec.id === id));
    }
    state.forms[key] = form;
  }
  return state.forms[key];
}

function requestBody() {
  const body = Object.assign({ stage: state.stage }, formOf(state.stage));
  /* 送出去的 run 一定是 day/name 形式的那条 run，不是保留区的 origin/... 路径：
     保留视图里 state.selected 是后者，直接当 run 发过去服务器不认。 */
  if (state.runPath) body.run = state.runPath;
  return body;
}

/* 常驻预设：hareness/prompts 里那个叫 common 的文件。 */
function commonPresets() {
  return state.presets.filter((preset) => preset.common).map((preset) => preset.name);
}

function presetOf(name) {
  return state.presets.find((preset) => preset.name === name) || null;
}

/* 常驻预设的正文。它是风格锁，谁都取消不掉，所以预览里单独摆出来。 */
function commonText() {
  const preset = state.presets.find((item) => item.common);
  return preset ? preset.text : '（hareness/prompts 里没有 common）';
}

/* 勾上的预设 + 补充提示词 + 常驻的 common，拼成一段读得下去的文字。
   预设是写给视频模型看的话，能提前读一遍才敢按"运行"。 */
function promptPreviewText(form) {
  const blocks = [];
  for (const name of form.presets || []) {
    const preset = presetOf(name);
    if (preset && preset.text) blocks.push('【' + preset.name + '】\n' + preset.text);
  }
  const extra = String(form.extra_prompt || '').trim();
  if (extra) blocks.push('【补充提示词】\n' + extra);

  const lines = [];
  const recorded = (state.detail && state.detail.prompt_presets) || null;
  const recordedNames = (recorded && recorded.presets) || [];
  if (recordedNames.length) {
    lines.push('这条 run 在阶段 2 记下来的：' + recordedNames.join('、')
      + '（阶段 3 不带 --preset 时就用它）');
    lines.push('');
  }
  lines.push(blocks.length ? blocks.join('\n\n') : '（一个预设都没勾，只用内置提示词）');
  lines.push('');
  lines.push('—— 下面这段是 common，每次都会自动带上，取消不掉 ——');
  lines.push(commonText());
  return lines.join('\n');
}

function clipsOf(form) {
  return String(form.clips || '').split(/[\s,]+/).filter(Boolean);
}

/* 这个 run 里已经出过视频的模型，按出场顺序去重。 */
function runModels() {
  const found = [];
  const clips = state.detail && state.detail.steps && state.detail.steps['4'].clips;
  for (const clip of clips || []) {
    for (const model of clip.models || []) {
      if (!found.includes(model.model)) found.push(model.model);
    }
  }
  return found.length ? found : (state.defaults.models || []).slice();
}
/* ---------------------------------------------------------------- 表单 */

function buildField(field) {
  const form = formOf(state.stage);
  if (field.kind === 'check') return buildCheck(field, form);
  if (field.kind === 'checks') return buildChecks(field, form);
  if (field.kind === 'refs') return buildRefs(field, form);
  if (field.kind === 'spec') return buildSpecField(field, form);
  if (field.kind === 'specs') return buildSpecsField(field, form);
  if (field.kind === 'artwork') return buildArtworkField(field, form);
  if (field.kind === 'presets') return buildPresetsField(field, form);
  if (field.kind === 'clips') return buildClipsField(field, form);
  if (field.kind === 'textarea') return buildTextField(field, form);

  const wrap = el('div', 'field');
  wrap.append(el('label', null, field.label));
  let input;

  if (field.kind === 'select') {
    const values = (field.optionsFor ? field.optionsFor() : field.options) || [];
    input = el('select');
    for (const value of values) {
      const option = el('option', null, value === '' ? '（不指定，用默认）' : value);
      option.value = value;
      input.append(option);
    }
    input.value = form[field.key] === undefined ? '' : String(form[field.key]);
    input.addEventListener('change', () => { form[field.key] = input.value; schedulePreview(); });
  } else {
    input = el('input');
    input.type = field.kind === 'number' ? 'number' : 'text';
    if (field.step) input.step = String(field.step);
    input.placeholder = field.ph || '';
    input.value = form[field.key] === undefined ? '' : String(form[field.key]);
    input.addEventListener('input', () => { form[field.key] = input.value; schedulePreview(); });
  }
  input.dataset.key = field.key;
  wrap.append(input);
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

function buildCheck(field, form) {
  const wrap = el('div', 'field check');
  const input = el('input');
  input.type = 'checkbox';
  input.checked = !!form[field.key];
  input.addEventListener('change', () => { form[field.key] = input.checked; schedulePreview(); });
  const label = el('label', null, field.label);
  label.addEventListener('click', () => input.click());
  wrap.append(input, label);
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

function buildChecks(field, form) {
  const wrap = el('div', 'field');
  wrap.append(el('label', null, field.label));
  const box = el('div', 'checks');
  const values = (field.optionsFor ? field.optionsFor() : field.options) || [];
  if (!values.length) box.append(el('span', 'note', '没有可选的'));
  for (const value of values) {
    const item = el('label');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = (form[field.key] || []).includes(value);
    input.addEventListener('change', () => {
      const list = new Set(form[field.key] || []);
      if (input.checked) list.add(value); else list.delete(value);
      form[field.key] = Array.from(list);
      schedulePreview();
    });
    item.append(input, el('span', null, value));
    box.append(item);
  }
  wrap.append(box);
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

function buildSpecField(field, form) {
  const wrap = el('div', 'field');
  wrap.append(el('label', null, field.label));
  const select = el('select');
  const blank = el('option', null, '— 选一个 —');
  blank.value = '';
  select.append(blank);
  for (const spec of state.specs) {
    const option = el('option', null, spec.id + (spec.title ? '  ·  ' + spec.title : ''));
    option.value = spec.id;
    select.append(option);
  }
  select.value = form.spec || '';
  const note = el('div', 'help');
  const describe = () => {
    const spec = state.specs.find((item) => item.id === form.spec);
    if (!spec) { note.textContent = field.help || ''; return; }
    const bits = [];
    if (spec.name) bits.push(spec.name);
    if (spec.style) bits.push('风格 ' + spec.style);
    if (spec.count) bits.push(spec.count + ' 张');
    if (spec.reference_mode) bits.push('参考方式 ' + spec.reference_mode);
    if (spec.references && spec.references.length) bits.push('参考图 ' + spec.references.join(', '));
    note.textContent = bits.join(' · ') || field.help || '';
  };
  select.addEventListener('change', () => {
    form.spec = select.value;
    savePref('spec', select.value);
    describe();
    schedulePreview();
  });
  describe();
  wrap.append(select, note);
  return wrap;
}

/* 原画阶段一次挑几个角色。
   一条命令能带好几个 spec，画出来的东西一模一样；分开跑只是多按几次按钮，
   所以这里给的是勾选列表，不是下拉框。 */
function buildSpecsField(field, form) {
  const wrap = el('div', 'field');
  const head = el('div', 'field-head');
  head.append(el('label', null, field.label));
  const count = el('span', 'tag');
  head.append(count);
  wrap.append(head);

  const tools = el('div', 'spec-tools');
  const list = el('div', 'spec-list');
  const boxes = [];

  const sync = () => {
    const chosen = state.specs.filter((spec) => (form.spec || []).includes(spec.id));
    count.textContent = chosen.length ? ('已选 ' + chosen.length + ' 个') : '还没选';
    savePref('spec', form.spec || []);
  };
  const button = (text, act) => {
    const node = el('button', 'btn ghost tiny', text);
    node.type = 'button';
    node.addEventListener('click', () => { act(); schedulePreview(); });
    tools.append(node);
    return node;
  };
  button('全选', () => {
    form.spec = state.specs.map((spec) => spec.id);
    for (const box of boxes) box.input.checked = true;
    sync();
  });
  button('清空', () => {
    form.spec = [];
    for (const box of boxes) box.input.checked = false;
    sync();
  });

  for (const spec of state.specs) {
    const row = el('label', 'spec-row');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = (form.spec || []).includes(spec.id);
    input.addEventListener('change', () => {
      const set = new Set(form.spec || []);
      if (input.checked) set.add(spec.id); else set.delete(spec.id);
      form.spec = state.specs.map((spec2) => spec2.id).filter((id) => set.has(id));
      sync();
      schedulePreview();
    });
    const text = el('span', 'spec-name');
    text.append(el('b', null, spec.id));
    const bits = [];
    if (spec.name) bits.push(spec.name);
    if (spec.style) bits.push(spec.style);
    if (spec.count) bits.push(spec.count + ' 张');
    if (spec.references && spec.references.length) bits.push('参考 ' + spec.references.join(', '));
    text.append(el('span', 'sub', bits.join(' · ')));
    row.append(input, text);
    boxes.push({ input, spec });
    list.append(row);
  }
  if (!state.specs.length) list.append(el('span', 'note', 'hareness/characters 里还没有 spec'));

  wrap.append(tools, list);
  if (field.help) wrap.append(el('div', 'help', field.help));
  sync();
  return wrap;
}

/* 多行文本：补充提示词。 */
function buildTextField(field, form) {
  const wrap = el('div', 'field');
  wrap.append(el('label', null, field.label));
  const input = el('textarea');
  input.rows = field.rows || 3;
  input.placeholder = field.ph || '';
  input.value = form[field.key] === undefined ? '' : String(form[field.key]);
  input.addEventListener('input', () => { form[field.key] = input.value; schedulePreview(); });
  input.dataset.key = field.key;
  wrap.append(input);
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

/* 动作名能点的那几个：common.yaml 里列的默认动作，和这条 run 里已经出过视频的动作。
   提示词预设的名字不上这个名单 —— 预设叫 walk_side，动作叫 walk，两者同名不同物；
   把预设名当动作名点下去，只会多出一个叫 walk_side 的 clip 目录。 */
function clipChoices() {
  const found = [];
  const add = (name) => {
    const text = String(name || '').trim();
    if (text && !found.includes(text)) found.push(text);
  };
  for (const name of state.defaults.clips || []) add(name);
  const clips = (state.detail && state.detail.steps && state.detail.steps['4'].clips) || [];
  for (const clip of clips) add(clip.clip);
  return found;
}

/* 动作：值得点的几个按一下就填，另外留一个框随便写。
   动作名就是 clip 目录名，也是提示词预设的名字，所以能点的那几个是从那两处凑出来的。 */
function buildClipsField(field, form) {
  const wrap = el('div', 'field');
  const head = el('div', 'field-head');
  head.append(el('label', null, field.label));
  const note = el('span', 'tag');
  head.append(el('span', 'spacer'), note);
  wrap.append(head);

  const box = el('div', 'chips');
  const input = el('input');
  input.type = 'text';
  input.placeholder = field.ph || '';
  input.dataset.key = field.key;

  const paint = () => {
    const names = clipsOf(form);
    note.textContent = names.length ? (names.length + ' 个动作') : '还没填';
    for (const chip of box.children) {
      chip.classList.toggle('on', names.includes(chip.dataset.clip));
    }
    if (state.refsRedraw) state.refsRedraw();
  };
  const sync = () => { input.value = clipsOf(form).join(','); paint(); };

  for (const name of clipChoices()) {
    const chip = el('button', 'chip', name);
    chip.type = 'button';
    chip.dataset.clip = name;
    const preset = state.presets.find((item) => item.name === name && !item.common);
    chip.title = '点一下把 ' + name + ' 加进来，再点一下去掉；也可以在下面手写别的'
      + (preset ? '（hareness/prompts 里有同名的预设 ' + name + '，在下面勾上它）' : '');
    chip.addEventListener('click', () => {
      const names = clipsOf(form);
      const at = names.indexOf(name);
      if (at < 0) names.push(name); else names.splice(at, 1);
      form.clips = names.join(',');
      sync();
      schedulePreview();
    });
    box.append(chip);
  }
  if (!box.childElementCount) box.append(el('span', 'note', '没有可点的动作名 —— 直接在下面写'));

  /* 输入框不重画自己：一边打字一边被 value 覆盖会把光标打回开头。 */
  input.addEventListener('input', () => {
    form.clips = input.value;
    paint();
    schedulePreview();
  });

  wrap.append(box, input);
  if (field.help) wrap.append(el('div', 'help', field.help));
  sync();
  return wrap;
}

/* 提示词预设：hareness/prompts 下的文件。
   common 是常驻的，勾选框直接 disabled 打勾；其余按动作勾。
   鼠标停在名字上能看到这个文件到底写了什么。 */
function buildPresetsField(field, form) {
  const wrap = el('div', 'field');
  const head = el('div', 'field-head');
  head.append(el('label', null, field.label));
  const count = el('span', 'tag');
  head.append(count);
  const peek = linkButton('看会加什么', () => openOverlay(
    el('pre', 'prompt', promptPreviewText(form)),
    '这次会带给视频模型的提示词 · 点任意处关闭'));
  peek.title = '把勾上的预设、补充提示词，和常驻的 common 拼起来读一遍';
  head.append(el('span', 'spacer'), peek);
  wrap.append(head);

  const box = el('div', 'presets');
  const boxes = [];
  /* common 是常驻的：resolver 每次自己带上，命令行上永远不会出现 --preset common
     （服务端的 _preset_selection 会把它滤掉）。所以"带上几个"只能数**会真的上
     命令行**的那几个 —— 把 common 也算进去，就会出现"写着带上 1 个、命令里一个
     --preset 都没有"，看着像勾丢了。 */
  const isCommon = (name) => state.presets.some((one) => one.common && one.name === name);
  const sync = () => {
    const extra = (form[field.key] || []).filter((name) => !isCommon(name));
    count.textContent = extra.length
      ? ('另加 ' + extra.length + ' 个 · 常驻 common')
      : '只带常驻的 common';
  };

  if (!state.presets.length) {
    box.append(el('span', 'note', 'hareness/prompts 里还没有预设文件'));
  }
  for (const preset of state.presets) {
    const item = el('label', 'preset' + (preset.common ? ' common' : ''));
    const input = el('input');
    input.type = 'checkbox';
    input.checked = (form[field.key] || []).includes(preset.name);
    if (preset.common) {
      /* 常驻：勾上就不能取消。内置提示词里风格锁的一部分，去掉它只会让画风飘。 */
      input.disabled = true;
      input.checked = true;
      item.title = '常驻预设，每次都会带上';
    } else {
      input.addEventListener('change', () => {
        const set = new Set(form[field.key] || []);
        if (input.checked) set.add(preset.name); else set.delete(preset.name);
        form[field.key] = state.presets.map((one) => one.name).filter((one) => set.has(one));
        sync();
        schedulePreview();
      });
    }
    const text = el('span', 'preset-name');
    text.append(el('b', null, preset.name));
    text.append(el('span', 'sub', preset.title || ''));
    const chars = el('span', 'tag', preset.chars + ' 字');
    item.append(input, text, chars);
    if (!preset.common) item.title = preset.text;
    box.append(item);
    boxes.push(input);
  }
  wrap.append(box);
  if (field.help) wrap.append(el('div', 'help', field.help));
  sync();
  return wrap;
}

/* 原画：origin/image 里留下来的那几张，配好缩略图直接点。
   阶段 2 能动的就是转录到 origin 的图，和这条 run 自己的 01_artwork。 */
function buildArtworkField(field, form) {
  const wrap = el('div', 'field');
  const head = el('div', 'field-head');
  head.append(el('label', null, field.label));
  const note = el('span', 'tag');
  head.append(note);
  const tools = el('div', 'spec-tools');
  tools.append(linkButton('打开 origin/image', () => reveal('origin/image')));
  tools.append(linkButton('刷新清单', async () => {
    await loadState();
    renderStageBody();
  }));
  head.append(el('span', 'spacer'));
  head.append(tools);
  wrap.append(head);

  const box = el('div', 'picker-grid');
  const chosen = String(form[field.key] || '');
  const known = state.artwork.some((item) => item.path === chosen);
  if (chosen && !known) {
    /* 选中的不是 origin/image 里的图（比如在「原画」树里直接点了「做视频」）。
       命令是合法的，但这里要说清楚它还没转录，不然挑选器看起来像空的。 */
    const odd = el('div', 'art-odd');
    odd.append(el('b', null, '已选（还没转录到 origin）'));
    odd.append(el('span', null, chosen));
    box.append(odd);
  }
  if (!state.artwork.length) {
    box.append(el('span', 'note',
      'origin/image 里还没有原画 —— 先在左边「原画」树里挑一张，点「转录到 origin」'));
  }
  for (const item of state.artwork) {
    const pick = el('button', 'art' + (item.path === chosen ? ' on' : ''));
    pick.type = 'button';
    const img = el('img');
    img.loading = 'lazy';
    img.src = fileURL(item, 160);
    img.alt = item.name;
    pick.append(img);
    pick.append(el('span', 'art-name', item.name.replace(/\.png$/i, '')));
    pick.title = item.path;
    pick.addEventListener('click', () => {
      // 再点一次 = 取消，省得手选错了还要去别处清空
      form[field.key] = (chosen === item.path) ? '' : item.path;
      renderStageBody();
      schedulePreview();
    });
    box.append(pick);
  }
  wrap.append(box);
  note.textContent = chosen ? ('已选 ' + chosen.split('/').pop()) : '还没选';
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

function buildRefs(field, form) {
  const wrap = el('div', 'field');
  wrap.append(el('label', null, field.label));
  const box = el('div', 'refs');
  wrap.append(box);

  const draw = () => {
    clear(box);
    const clips = clipsOf(form);
    if (!clips.length) { box.append(el('span', 'note', '先把动作填上，再按动作给参考图')); return; }
    for (const clip of clips) {
      const row = el('div', 'ref');
      row.append(el('span', null, clip));
      const input = el('input');
      input.type = 'text';
      input.placeholder = 'origin/image/main.png（留空 = 用阶段 2 补好绿的那张）';
      input.value = (form.references || {})[clip] || '';
      input.addEventListener('input', () => {
        form.references = form.references || {};
        const text = input.value.trim();
        if (text) form.references[clip] = text; else delete form.references[clip];
        schedulePreview();
      });
      row.append(input);
      box.append(row);
    }
  };
  draw();
  state.refsRedraw = draw;
  if (field.help) wrap.append(el('div', 'help', field.help));
  return wrap;
}

/* ---------------------------------------------------------------- 预览 */

function schedulePreview() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(refreshPreview, 160);
}

async function refreshPreview() {
  const box = state.previewBox;
  if (!box) return;
  const token = (state.previewToken += 1);
  let result;
  try {
    result = await postJSON('/api/command', requestBody());
  } catch (err) {
    result = { error: err.message };
  }
  if (token !== state.previewToken || box !== state.previewBox) return;
  clear(box);
  if (result.error) {
    box.className = 'cmd bad';
    box.append(el('span', null, '× ' + result.error));
    return;
  }
  box.className = 'cmd';
  box.append(el('span', 'label', result.label));
  box.append(document.createTextNode('$ python main.py ' + result.command));
}
/* ---------------------------------------------------------------- 面板 */

function stageMeta(stage) {
  if (stage === 'all') {
    /* 全流程 = 五个步骤全跑，也就是四个阶段全跑；steps 给的是它要开哪几个目录。 */
    return {
      n: 'all', title: '全流程', engine: '', steps: [1, 2, 3, 4, 5],
      hint: '从阶段 1 一路跑到阶段 4（五步全跑）；选了 run 就从「从第几阶段开始」'
        + '接着往下跑，右边是这条 run 的四个阶段总览。',
    };
  }
  return state.stages.find((item) => item.n === stage)
    || { n: stage, title: '', engine: '', steps: [], hint: '' };
}

function emptyState(title, text) {
  const box = el('div', 'empty-state');
  box.append(el('b', null, title));
  box.append(document.createTextNode(text || ''));
  return box;
}

function buildFormPanel(meta) {
  const panel = el('section', 'panel');
  const head = el('div', 'panel-head');
  const titleRow = el('div', 'head-row');
  titleRow.append(el('h2', null, meta.n === 'all' ? '全流程' : '阶段 ' + meta.n + ' · ' + meta.title));
  if (meta.engine) {
    /* AI / 本机 是这一栏里最该先说的一件事：AI 那两步要花钱，本机那两步不花。 */
    const tag = el('span', 'tag', meta.engine);
    tag.title = meta.engine === 'AI'
      ? '这一步调模型，会花钱' : '这一步全在本机跑，不花钱';
    titleRow.append(tag);
  }
  head.append(titleRow);
  head.append(el('p', 'hint', meta.hint));
  panel.append(head);

  const target = targetBar(meta);
  if (target) panel.append(target);

  const form = el('div', 'form');
  for (const field of FIELDS[String(meta.n)] || []) form.append(buildField(field));
  panel.append(form);

  const clipsInput = form.querySelector('input[data-key="clips"]');
  if (clipsInput) clipsInput.addEventListener('input', () => { if (state.refsRedraw) state.refsRedraw(); });

  const busy = !!(state.job && state.job.status === 'running');
  const foot = el('div', 'panel-foot');
  const run = el('button', 'btn', busy ? '正在跑…' : '运行这一阶段');
  run.disabled = busy;
  run.addEventListener('click', startJob);
  foot.append(run);
  panel.append(foot);

  const cmd = el('div', 'cmd', '…');
  panel.append(cmd);
  state.previewBox = cmd;
  refreshPreview();
  return panel;
}

/* 顶上那条「这一阶段作用在谁身上」。
   阶段 1 是对着 harness 画的，不看 run，所以不显示；阶段 2 的 run 是可选的（没选就
   照原画的名字新开一条），阶段 3、4 必须有一条 run。保留视图里显示的是「留下来的
   这一件 + 它的来源 run」—— 因为右栏这四个阶段就是那条 run 的。 */
function targetBar(meta) {
  const mode = modeOf();
  if (mode === 'kept') {
    const kept = state.kept;
    const bar = el('div', 'target' + (kept ? '' : ' empty'));
    bar.append(el('b', null, kept ? (kept.kind === 'image' ? '保留 · 原画' : '保留 · 视频') : '保留'));
    bar.append(el('span', null, kept ? kept.path : '左边点一件留下来的东西'));
    if (kept && state.runPath) {
      const from = el('span', 'tag', '来自 run ' + state.runPath);
      from.title = '右栏这四个阶段显示的就是这条 run，保留的这件东西是从它里面挑出来的';
      bar.append(from);
    } else if (kept) {
      const none = el('span', 'tag', '没有来源 run');
      none.title = '它是直接放进 origin 的，resource 里没有对应的 run，所以只有阶段 2 起得了作用';
      bar.append(none);
    }
    return bar;
  }
  if (meta.n === 1) return null;
  const bar = el('div', 'target' + (state.runPath ? '' : ' empty'));
  bar.append(el('b', null, 'run'));
  if (state.runPath) {
    bar.append(el('span', null, state.runPath));
  } else if (String(meta.n) === '2') {
    /* 阶段 2 不挑 run 也能跑：它照原画的名字开一条新的，这是最常走的那条路。 */
    bar.append(el('span', null, '没选 —— 会照原画的名字新开一条'));
  } else {
    bar.append(el('span', null, '还没选 —— 点左边任意一个 run 目录'));
  }
  return bar;
}

function badge(label, value, kind) {
  const node = el('span', 'badge' + (kind ? ' ' + kind : ''));
  node.append(document.createTextNode(label + ' '));
  node.append(el('b', null, String(value)));
  return node;
}

function shot(item, width, opts) {
  const options = opts || {};
  const box = el('div', 'shot');
  const img = el('img');
  img.loading = 'lazy';
  img.src = fileURL(item, width);
  img.alt = item.name;
  box.append(img);
  box.append(el('span', 'cap', item.name));
  box.title = options.title
    || (item.name + '  ·  ' + fmtSize(item.size) + '  ·  点开看大图');
  box.addEventListener('click', () => {
    if (options.onClick) { options.onClick(item); return; }
    openLightbox(item, options.siblings, options.cut, options.light);
  });
  if (options.actions && options.actions.length) {
    const tools = el('div', 'shot-tools');
    for (const action of options.actions) {
      tools.append(linkButton(action.label, (event) => {
        event.stopPropagation();
        action.run();
      }, action.title));
    }
    box.append(tools);
  }
  return box;
}

function imagesBlock(title, items, width, actionsFor) {
  if (!items || !items.length) return null;
  const block = el('div', 'block');
  block.append(el('h3', null, title + ' · ' + items.length));
  const grid = el('div', 'grid-imgs');
  for (const item of items) {
    grid.append(shot(item, width, {
      siblings: items,
      actions: actionsFor ? actionsFor(item) : null,
    }));
  }
  block.append(grid);
  return block;
}

function linkButton(text, onClick, title) {
  const button = el('button', 'btn ghost tiny', text);
  if (title) button.title = title;
  button.addEventListener('click', onClick);
  return button;
}
/* 一张原画能做的事：转录到 origin，或者直接拿它去做视频输入。 */
function stillActions(item) {
  return [
    { label: '转录', title: '转录到 origin：把这张原画留下来',
      run: () => promote(item.path, entityOf(item.name)) },
    { label: '做视频', title: '拿它做视频输入：跳到阶段 2，把这张原画补绿扩边，'
      + '再在阶段 3 生成动作视频',
      run: () => useForVideo(item) },
  ];
}

function stemOf(name) {
  return String(name || '').replace(/\.[^.]+$/, '');
}

/* 原画在 run 里的名字是「实体_批次号」（monster_imp_01），转录进 origin 时批次号会被
   去掉，所以这里预填的名字也要先去一次 —— 否则输入框里填的是服务器不会用的那个名，
   点一下确定就落下一个和预期不一样的名字。规则和 src/naming.py 的 entity_of_artwork 一致。 */
function entityOf(name) {
  return stemOf(name).replace(/_\d{1,2}$/, '');
}

/* 右栏那块「产出」。键是*阶段*号，看的是磁盘上那几个步骤目录里的东西：
   阶段 2 是两步合起来的，所以它一块里画两组图。 */
const STAGE_VIEWS = {
  1: (detail) => imagesBlock(
    '原画（纯绿背景）', detail.steps['1'].images, 256, stillActions),
  2: (detail) => {
    const box = el('div');
    /* 先摆这一步真正的产出：补好纯绿、留了白的视频输入图 —— 阶段 3 吃的是这张。
       不给它「转录」按钮：它带着绿底，不是能进 origin/image 的东西。 */
    const input = imagesBlock('补好纯绿、留了白的视频输入图（阶段 3 用这张）',
      detail.steps['3'].images, 256);
    if (input) box.append(input);
    const images = imagesBlock('抠好、裁好的原画', detail.steps['2'].images, 256, stillActions);
    if (images) box.append(images);
    const dist = detail.steps['2'].dist || {};
    for (const key of Object.keys(dist)) {
      /* 512 那张就是后半步拿去补绿的底，256 是给 Unity 的交付副本。 */
      const note = key === '512' ? '（后半步用它补绿）' : '';
      const copy = imagesBlock(key + 'px 交付副本' + note, dist[key], 200, stillActions);
      if (copy) box.append(copy);
    }
    const sizes = detail.steps['2'].sizes || {};
    const keys = Object.keys(sizes);
    if (keys.length) {
      const block = el('div', 'block');
      block.append(el('h3', null, '交付尺寸'));
      const badges = el('div', 'badges');
      for (const key of keys) badges.append(badge(key + 'px', sizes[key] + ' 张'));
      block.append(badges);
      box.append(block);
    }
    return box.childElementCount ? box : null;
  },
  3: (detail) => videosBlock(detail.steps['4'].clips),
  4: (detail) => cutsBlock(detail.steps['5'].cuts),
  /* 全流程这一页不重复造轮子：它就是四个阶段各来一次，当成一个整跑的总览。 */
  all: (detail) => {
    const box = el('div');
    for (const stage of [1, 2, 3, 4]) {
      const node = STAGE_VIEWS[stage](detail);
      if (node) box.append(node);
    }
    return box.childElementCount ? box : null;
  },
};

/* 面板右上角那颗「打开 …」该开哪几层。
   用服务器给的 dir（resource/image/<日>/<run>/01_artwork 这种整条相对路径），
   不要自己拿 run key 拼 —— run key 是「日/名字」，拼出来少了 resource 那一层。
   一个阶段可能写到两个目录里（阶段 2 就是），所以这里回一串。 */
function stageDirs(meta) {
  const detail = state.detail;
  if (!detail) return [];
  if (meta.n === 'all') {
    /* 阶段 1 起头画出来的 run 只有原画那一半，从 origin 开出来的 run 只有视频那一半
       —— 开目录要开真的存在的那一边，不然资源管理器会报"找不到"。 */
    if (detail.has_video && !detail.has_image) return [detail.video_dir].filter(Boolean);
    return [detail.image_dir || detail.video_dir].filter(Boolean);
  }
  const dirs = [];
  for (const number of meta.steps || []) {
    const step = (detail.steps || {})[String(number)];
    if (step && step.dir) dirs.push(step.dir);
  }
  return dirs;
}

function buildArtifactPanel(meta) {
  const panel = el('section', 'panel');
  const head = el('div', 'panel-head');
  const row = el('div', 'head-row');
  row.append(el('h2', null, '产出'));
  row.append(el('span', 'spacer'));
  /* 一个阶段写到哪儿，按钮上就写着哪一层目录名（01_artwork / 02_artwork_ready…）。 */
  for (const dir of stageDirs(meta)) {
    row.append(linkButton('打开 ' + dir.split('/').pop(), () => reveal(dir), dir));
  }
  head.append(row);
  panel.append(head);

  /* 保留视图：先把留下来的这一件本身压在最上面，再看它那条 run 的产出。 */
  const kept = modeOf() === 'kept' ? state.kept : null;
  if (kept) panel.append(kept.kind === 'image' ? keptStillBlock(kept) : keptCutBlock(kept));

  if (!state.selected) {
    panel.append(emptyState('还没选 run', '左边点一个 run 目录，这里就会显示每个阶段实际写出来的东西。'));
    return panel;
  }
  if (!state.detail) {
    panel.append(el('div', 'empty-state', state.detailError
      || (kept ? '这件东西没有对应的 run —— 它是直接放进 origin 的，上面就是它的全部。'
               : '读取中…')));
    return panel;
  }
  const node = STAGE_VIEWS[meta.n] ? STAGE_VIEWS[meta.n](state.detail) : null;
  if (node) panel.append(node);
  else if (!kept) panel.append(emptyState('这一阶段还没有产出', '在上面填好参数，点「运行这一阶段」。'));
  return panel;
}

function videoCard(model) {
  const card = el('div', 'vid');
  if (model.video) {
    const video = document.createElement('video');
    video.src = fileURL(model.video);
    video.controls = true;
    video.loop = true;
    video.muted = true;
    video.autoplay = true;
    video.preload = 'metadata';
    card.append(video);
  } else {
    card.append(el('div', 'empty-state', '没有 source.mp4'));
  }
  const bar = el('div', 'bar');
  const name = el('span', 'name', model.model);
  name.title = model.model;
  bar.append(name);
  if (model.video) {
    const rate = el('select');
    for (const value of ['1', '0.5', '0.25']) {
      const option = el('option', null, value + '×');
      option.value = value;
      rate.append(option);
    }
    rate.title = '播放速度（慢放看走路是不是原地）';
    rate.addEventListener('change', () => {
      const video = card.querySelector('video');
      if (video) video.playbackRate = parseFloat(rate.value);
    });
    bar.append(rate);
  }
  card.append(bar);
  return card;
}

function videosBlock(clips) {
  if (!clips || !clips.length) return null;
  const box = el('div');
  for (const clip of clips) {
    const block = el('div', 'block');
    const head = el('h3');
    head.append(el('span', 'clip', clip.clip));
    head.append(el('span', 'model', clip.models.length + ' 个模型'));
    block.append(head);

    const grid = el('div', 'videos');
    for (const model of clip.models) grid.append(videoCard(model));
    block.append(grid);

    const tools = el('div', 'panel-tools');
    if (clip.prompt) tools.append(linkButton('看提示词 prompt.txt', () => openText(clip.prompt)));
    for (const model of clip.models) {
      if (!model.meta) continue;
      tools.append(linkButton('看 ' + model.model + ' 的参数', () => openText(model.meta)));
    }
    block.append(tools);
    box.append(block);
  }
  return box;
}

function cutBadges(cut) {
  const stats = cut.stats || {};
  const badges = el('div', 'badges');
  if (cut.canvas) badges.append(badge('画布', cut.canvas.join('×'), ''));
  const sizes = Object.keys(cut.sizes || {});
  if (sizes.length) badges.append(badge('副本', sizes.map((key) => key + '×' + cut.sizes[key]).join('  '), ''));
  if (stats.frames) badges.append(badge('帧', stats.frames, ''));
  if (cut.fps) badges.append(badge('fps', cut.fps, ''));

  const spread = stats.height_spread_pct;
  if (spread !== undefined) badges.append(badge('高度浮动', spread + '%', spread <= 6 ? 'ok' : 'warn'));
  const drift = stats.drift_px;
  if (drift !== undefined) badges.append(badge('左右漂移', drift + 'px', drift <= 8 ? 'ok' : 'warn'));
  const edge = stats.frames_touching_edge;
  if (edge !== undefined) badges.append(badge('贴边帧', edge, edge ? 'bad' : 'ok'));
  if (stats.frames_empty) badges.append(badge('空帧', stats.frames_empty, 'bad'));
  const loop = stats.first_vs_last_changed_pct;
  if (loop !== undefined) badges.append(badge('首末差异', loop + '%', loop <= 15 ? 'ok' : 'warn'));
  if (stats.loop_best_frame !== undefined && stats.loop_best_frame !== null) {
    badges.append(badge('最佳循环帧', stats.loop_best_frame + '（' + stats.loop_best_changed_pct + '%）', ''));
  }
  if (cut.skipped_frames) badges.append(badge('丢掉开头', cut.skipped_frames + ' 帧', ''));
  return badges;
}

function cutsBlock(cuts) {
  if (!cuts || !cuts.length) return null;
  const box = el('div');
  for (const cut of cuts) {
    const block = el('div', 'block');
    const head = el('h3');
    head.append(el('span', 'clip', cut.clip));
    head.append(el('span', 'model', cut.model));
    block.append(head);
    block.append(cutBadges(cut));

    const player = cutPlayer(cut);
    block.append(el('h3', null, '全量播放 · ' + (cut.frames || '?') + ' 帧'
      + (cut.fps ? ' · ' + cut.fps + 'fps' : '')));
    block.append(player);

    const tools = el('div', 'panel-tools');
    tools.append(linkButton('放大播放', () => openCutLightbox(cut, player),
      '大图里播：左右翻帧、空格播放，还能切到精灵图'));
    if (cut.contact) tools.append(linkButton('接触表 contact_sheet', () => openLightbox(cut.contact)));
    if (cut.sheet && cut.sheet_slice) {
      tools.append(linkButton('看精灵图（逐片播）', () => openCutLightbox(cut, player, 'sheet'),
        '整张 sprite_sheet.png 有几万像素宽，浏览器解不动，所以由后端一片一片切好再播'));
    } else if (cut.sheet) {
      tools.append(el('span', 'tag', 'sprite_sheet.png（旧产物，没有切片信息）'));
    }
    if (cut.dir) {
      tools.append(linkButton('转录到 origin',
        () => promote(cut.dir, cutName(cut)),
        '把这整段帧（含 512 / 256 副本、精灵图、统计）拷到 origin/video，'
        + '名字是 ' + cutName(cut)));
      tools.append(linkButton('打开帧目录', () => reveal(cut.dir)));
    }
    block.append(tools);

    /* 帧样本条撤了：左边的总帧池摆的就是每一帧，再来一条只挑十二张的样本
       纯属重复，还占一屏。 */
    block.append(framePools(cut));
    box.append(block);
  }
  return box;
}
/* ---------------------------------------------------------------- 挑帧 */

/* 段里的帧号写成人看的那种写法：0-40,50-70。 */
function rangesOf(indices) {
  const list = Array.from(new Set(indices)).sort((left, right) => left - right);
  const parts = [];
  let start = null;
  let prev = null;
  for (const value of list) {
    if (start === null) { start = value; prev = value; continue; }
    if (value === prev + 1) { prev = value; continue; }
    parts.push(start === prev ? String(start) : start + '-' + prev);
    start = value;
    prev = value;
  }
  if (start !== null) parts.push(start === prev ? String(start) : start + '-' + prev);
  return parts.join(',');
}

/* 把 "0-3,7" 读回帧号，和命令行里那个 --frames 是同一套写法。 */
function parseRanges(text, total) {
  const out = [];
  for (const piece of String(text || '').split(/[,\s]+/)) {
    const item = piece.trim();
    if (!item) continue;
    const dash = item.indexOf('-');
    if (dash < 0) {
      const one = Number(item);
      if (Number.isInteger(one)) out.push(one);
      continue;
    }
    const head = Number(item.slice(0, dash));
    const tail = Number(item.slice(dash + 1) || item.slice(0, dash));
    if (!Number.isInteger(head) || !Number.isInteger(tail)) continue;
    const step = tail >= head ? 1 : -1;
    for (let value = head; step > 0 ? value <= tail : value >= tail; value += step) out.push(value);
  }
  return out.filter((value) => value >= 0 && value < total);
}

/* 挑帧网格的几何常量，和 CSS 里的 .picker-grid / .pick 对齐。 */
const PICK_GAP = 5;      // 和 CSS 里 .picker-grid 的 padding / 间距对齐
const PICK_MIN = 74;     // 一格的最小宽度，和原来 CSS 的 minmax 一致
const PICK_SKIN = 6;     // .pick 自己的边框 + 内边距（box-sizing: border-box）

const PICK_GRIDS = new Set();
/* 窗口一变宽窄，列数和格子边长都得重算。一个监听管所有网格，
   顺手把已经不在页面上的网格摘掉 —— 每换一次面板就挂一个监听会越积越多。 */
window.addEventListener('resize', () => {
  for (const item of Array.from(PICK_GRIDS)) {
    if (item.view.isConnected) item.paint();
    else PICK_GRIDS.delete(item);
  }
});

/* 挑帧网格：坐标自己算，一屏只画看得见的那几十张。
   一段 119 帧、三个动作就是 357 张，一次全塞进 DOM 里，滚动和每次重画都会卡。
   所以外层是固定高度的滚动容器，里面只有一块撑高度的占位块，真正的格子在滚动时
   按行增删、绝对定位 —— 位置是算出来的，不去问浏览器排版，滚动才不用等它回流。

     list         摆出来的帧，长度就是格子个数（可以是个还在往里 push 的数组）
     opts.labels  (i) -> 格子上写的字，默认写它在 list 里的位置
     opts.marked  Set 或者 (i)=>bool：哪几格算"已经在右边了"
     opts.onPick  (i) -> 点一下干什么
     opts.title   (i) -> 悬停提示
     opts.aspect  画布宽高比，用来定格子多高
   两个池子共用这一套：标出来的都是**原帧号**，所以右边第 17 帧和左边第 17 帧是同一张。 */
function pickGrid(list, opts) {
  const options = opts || {};
  const view = el('div', 'picker-grid');
  const space = el('div', 'picker-space');
  view.append(space);
  const nodes = new Map();
  const ratio = (options.aspect > 0.2 && options.aspect < 5) ? options.aspect : 0.75;
  const marked = options.marked;
  const isOn = (index) => (typeof marked === 'function'
    ? !!marked(index) : !!(marked && marked.has && marked.has(index)));
  const labelAt = (index) => String(options.labels ? options.labels(index) : index);
  const titleAt = (index) => (options.title ? options.title(index) : '第 ' + index + ' 帧');
  let cols = 1;
  let cell = PICK_MIN;
  let boxH = PICK_MIN;
  let rowH = PICK_MIN + PICK_GAP;
  let queued = false;

  /* 只算一次几何。格子宽高固定，图片用 contain 装进框里，形状差的帧也不会把行高带跑。 */
  const measure = () => {
    const width = view.clientWidth || 340;
    cols = Math.max(1, Math.floor((width + PICK_GAP) / (PICK_MIN + PICK_GAP)));
    cell = Math.max(34, Math.floor((width - PICK_GAP * (cols - 1)) / cols));
    const inner = Math.max(24, cell - PICK_SKIN);
    boxH = Math.round(Math.min(inner * 2.4, Math.max(inner * 0.6, inner / ratio)));
    rowH = boxH + PICK_SKIN + PICK_GAP;
  };

  const make = (index) => {
    const node = el('button', 'pick');
    node.type = 'button';
    // 帧号挂在节点上：整批重画高亮时靠它把 DOM 和选择对上。
    node.dataset.index = String(index);
    const thumb = el('img');
    thumb.loading = 'lazy';
    thumb.src = fileURL(list[index], 96);
    thumb.alt = labelAt(index);
    node.append(thumb, el('span', 'n', labelAt(index)));
    node.title = titleAt(index);
    if (options.onPick) node.addEventListener('click', () => options.onPick(index));
    thumb.style.height = boxH + 'px';
    thumb.style.objectFit = 'contain';
    node.style.position = 'absolute';
    node.style.width = cell + 'px';
    node.style.height = (boxH + PICK_SKIN) + 'px';
    node.style.left = ((index % cols) * (cell + PICK_GAP)) + 'px';
    node.style.top = (Math.floor(index / cols) * rowH) + 'px';
    node.classList.toggle('on', isOn(index));
    return node;
  };

  const paint = () => {
    measure();
    const total = list.length;
    const rows = Math.max(1, Math.ceil(total / cols));
    space.style.height = (rows * rowH) + 'px';
    const height = view.clientHeight || 430;
    const first = Math.max(0, Math.floor(view.scrollTop / rowH) - 1);
    const last = Math.min(rows, Math.floor((view.scrollTop + height) / rowH) + 2);
    const want = new Set();
    for (let row = first; row < last; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        const index = row * cols + col;
        if (index < total) want.add(index);
      }
    }
    for (const [index, node] of Array.from(nodes)) {
      if (want.has(index)) continue;
      node.remove();
      nodes.delete(index);
    }
    for (const index of want) {
      let node = nodes.get(index);
      if (node && node.style.width !== cell + 'px') {
        // 网格变宽窄了：留着旧的坐标还不如重画一个
        node.remove();
        nodes.delete(index);
        node = null;
      }
      if (!node) {
        node = make(index);
        nodes.set(index, node);
        space.append(node);
      }
      node.classList.toggle('on', isOn(index));
      node.title = titleAt(index);
    }
  };

  view.addEventListener('scroll', () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; paint(); });
  });
  /* 选用池是整块重建的，每点一下就留一条记录。挂监听之前先把已经不在页面上的摘掉，
     否则一次挑帧能攒下几百条，resize 的时候白跑一遍。 */
  for (const item of Array.from(PICK_GRIDS)) if (!item.view.isConnected) PICK_GRIDS.delete(item);
  PICK_GRIDS.add({ view, paint });
  return { view, paint };
}

/* 「生成选用帧」和「删掉 kept/」都走一个后台任务：和别的阶段一样，
   命令行怎么跑页面就怎么跑，日志区里看得到那一条。 */
async function startSelectJob(payload, label) {
  try {
    const result = await postJSON('/api/job/start', Object.assign({ stage: 'select' }, payload));
    state.job = result.job;
    state.scan = 0;
    state.scannedJob = null;
    clear($('log-lines'));
    setLogCollapsed(false);
    renderLog();
    toast('\u25b6 ' + (label || result.job.label), 'ok');
  } catch (err) {
    toast('\u00d7 ' + err.message, 'bad');
  }
}

/* 挑帧：两个池子。
   左边是这一段切出来的**总帧**，永远都在 —— 它是"这段视频到底切出了什么"的证据，
   也是回头重挑的底子；右边是**选用**，真要导入 Unity 的就是这批。点左边加进来、
   点右边拿出去，右边随时能播、能变速，所以"挑成什么样"在写盘之前就看得到。
   写盘是「生成选用帧」，落在这一段旁边的 kept/ 里；全量帧原样不动。 */
function framePools(cut) {
  const box = el('div', 'picker');
  const head = el('h3');
  head.append(el('span', null, '挑帧'));
  /* 一段的挑帧块离它自己的标题有一屏远，滚到这儿就不知道在挑哪个动作了。
     动作名写进标题里，模型名挂在悬停上 —— 一行放不下两个长名字。 */
  const which = el('span', 'tag', cut.clip);
  which.title = cut.clip + ' / ' + cut.model;
  head.append(which);
  const status = el('span', 'tag');
  const seconds = el('span', 'tag');
  head.append(status, seconds);
  const bar = el('div', 'picker-bar');
  const note = el('div', 'help');
  const pools = el('div', 'pools');
  box.append(head, bar, pools, note);

  const sel = new Set();          // 选中的帧在 frames 里的下标
  const frames = [];              // 整段帧，读盘之后才有内容
  const fps = Math.max(1, Math.round(cut.fps || 24));
  const aspect = (cut.canvas && cut.canvas[0] && cut.canvas[1])
    ? cut.canvas[0] / cut.canvas[1] : 0.75;
  /* 顺序按原帧号排。挑选是"留下 / 拿走"，不是重新排序 —— 播放顺序就是这一段自己的顺序。 */
  const picked = () => Array.from(sel).sort((left, right) => left - right);
  const selectedNames = () => picked().map((index) => (frames[index] || {}).name).filter(Boolean);
  const onDisk = () => (cut.selected_frames || (cut.kept && cut.kept.selected_frames) || []).slice();
  /* 磁盘上的 kept/ 和现在勾的是不是同一批。不是的话，「转录到 origin」送走的是上一批，
     所以那个按钮先按住，提示先去点「生成选用帧」。 */
  const synced = () => {
    if (!cut.kept) return false;
    const mine = selectedNames().slice().sort();
    const disk = onDisk().slice().sort();
    return mine.length === disk.length && mine.every((name, index) => name === disk[index]);
  };

  /* ---- 左池：总帧 ---- */
  const left = el('div', 'pool');
  const lhead = el('div', 'pool-head');
  lhead.append(el('b', null, '总帧'));
  const ltag = el('span', 'tag');
  lhead.append(ltag);
  const grid = pickGrid(frames, {
    aspect,
    marked: sel,
    onPick: (index) => {
      if (sel.has(index)) sel.delete(index); else sel.add(index);
      apply();
    },
    title: (index) => '第 ' + index + ' 帧 · 点一下'
      + (sel.has(index) ? '从右边拿出去' : '加进右边'),
  });
  left.append(lhead, grid.view);

  /* ---- 右池：选用 ---- */
  const right = el('div', 'pool');
  const rhead = el('div', 'pool-head');
  rhead.append(el('b', null, '选用'));
  const rtag = el('span', 'tag');
  const rtime = el('span', 'tag');
  rhead.append(rtag, rtime);
  rhead.append(el('span', 'spacer'));
  const clearBtn = linkButton('清空', () => {
    if (!sel.size) return;
    sel.clear();
    apply('右边清空了。磁盘上的 kept/ 没动 —— 重新挑完点「生成选用帧」才会覆盖它。');
  }, '把右边这个池子清空。磁盘上的 kept/ 不动，全量帧更不动。');
  rhead.append(clearBtn);

  /* 选用池的播放器：帧就是右边这批。和整段播放器同一套控件，
     所以 0.25× / 0.5× / 1× / 2× 变速、拖滑杆、逐帧翻都是现成的。 */
  const player = animPlayer({ frames: [], canvas: cut.canvas, fps: cut.fps });
  const pickedHost = el('div', 'pool-picked');
  /* 磁盘上那份 kept/ 和现在勾的是不是同一批，要一直看得见 —— 它决定「转录到 origin」
     按得按不得。所以单独一行，不去挤头部那排标签。 */
  const syncTag = el('div', 'pool-sync');
  const foot = el('div', 'pool-foot');
  right.append(rhead, player, pickedHost, syncTag, foot);

  const save = el('button', 'btn tiny', cut.kept ? '生成选用帧（覆盖 kept/）' : '生成选用帧');
  save.type = 'button';
  save.title = '把右边这批写进 kept/，导入 Unity 的就是它。全量帧不动，之后还能改。';
  save.addEventListener('click', () => {
    if (!sel.size) { note.textContent = '右边一帧都没有 —— 先从左边点几张进来'; return; }
    startSelectJob({
      run: state.selected,
      clips: [cut.clip],
      models: [cut.model],
      frames: rangesOf(picked(), frames.length),
    }, '挑帧 · ' + cutName(cut) + ' · ' + sel.size + ' 帧');
  });
  foot.append(save);

  const promoteName = cutName(cut.kept) || cutName(cut);
  const promoteTitle = '只把这批选出来的帧拷到 origin/video/' + promoteName
    + '，全量帧留在 resource 里';
  const promoteBtn = linkButton('转录选用帧到 origin',
    () => promote(cut.kept.dir, promoteName), promoteTitle);
  foot.append(promoteBtn);

  if (cut.kept) {
    foot.append(linkButton('删掉 kept/', () => {
      if (!window.confirm('把这段的 kept/ 整个拿走？\n\n全量帧不动，之后还能重新挑。'
        + '\n拿走的这批挪到 temp/trash/ 里放一天，不会当场销毁。')) return;
      startSelectJob(
        { run: state.selected, clips: [cut.clip], models: [cut.model], clear: true },
        '清空选用帧 · ' + cutName(cut));
    }, '把磁盘上这份选用帧删掉（全量帧不动）'));
  }

  pools.append(left, right);

  /* 右边整块重建：格子、播放器的帧、头上那几个数都跟着选择走。
     先摘掉旧网格再建新的，PICK_GRIDS 那边才知道旧的可以扔了。 */
  let pickedGrid = null;
  const refreshPicked = () => {
    const list = picked();
    const keepScroll = pickedGrid ? pickedGrid.view.scrollTop : 0;
    if (pickedGrid) { pickedGrid.view.remove(); pickedGrid = null; }
    clear(pickedHost);
    const items = list.map((index) => frames[index]).filter(Boolean);
    player.setFrames(items);
    player.style.display = items.length ? '' : 'none';
    if (!items.length) {
      pickedHost.append(el('div', 'pool-empty', '还没挑 —— 点左边任意一张加进来'));
      return;
    }
    pickedGrid = pickGrid(items, {
      aspect,
      marked: () => true,
      labels: (position) => list[position],
      onPick: (position) => { sel.delete(list[position]); apply(); },
      title: (position) => '第 ' + list[position] + ' 帧 · 点一下从右边拿出去',
    });
    pickedHost.append(pickedGrid.view);
    pickedGrid.paint();
    pickedGrid.view.scrollTop = keepScroll;
  };

  const apply = (message) => {
    grid.paint();
    status.textContent = '已选 ' + sel.size + ' / ' + frames.length;
    seconds.textContent = sel.size ? ('约 ' + (sel.size / fps).toFixed(2) + 's @ ' + fps + 'fps') : '';
    ltag.textContent = '全段 ' + frames.length + ' 帧';
    rtag.textContent = sel.size + ' / ' + frames.length + ' 帧';
    rtime.textContent = sel.size ? ('约 ' + (sel.size / fps).toFixed(2) + 's') : '';
    save.disabled = !sel.size;
    const isSynced = synced();
    syncTag.textContent = cut.kept
      ? (isSynced ? '磁盘上的 kept/ 就是这批' : '磁盘上的 kept/ 是上一批（要转录先重新生成）')
      : '还没写进 kept/ —— 这台机器上只有这段全量帧';
    syncTag.className = 'pool-sync' + (cut.kept ? (isSynced ? ' ok' : ' warn') : '');
    promoteBtn.disabled = !isSynced;
    promoteBtn.title = isSynced ? promoteTitle
      : '右边挑的和磁盘上的 kept/ 不是同一批 —— 先点「生成选用帧」，'
        + '否则转录到 origin 的是上一批';
    if (message) note.textContent = message;
    refreshPicked();
  };

  /* ---- 全宽那条工具条：改的都是右边那个池子 ---- */
  const button = (text, title, act) => {
    const node = el('button', 'btn ghost tiny', text);
    node.type = 'button';
    node.title = title || '';
    node.addEventListener('click', act);
    bar.append(node);
    return node;
  };
  const pick = (list) => { sel.clear(); for (const value of list) sel.add(value); };

  button('全选', '整段都留着', () => { pick(frames.map((_, index) => index)); apply(); });
  button('全不选', '一个都不留', () => { pick([]); apply(); });
  button('反选', '留没勾过的那些', () => {
    pick(frames.map((_, index) => index).filter((index) => !sel.has(index)));
    apply();
  });
  button('隔一帧留一帧', '帧数砍一半，循环快一倍', () => {
    pick(frames.map((_, index) => index).filter((index) => index % 2 === 0));
    apply();
  });

  /* 一段五秒的剪出来一百二十帧，可游戏里要的常常只有十来帧。按段里已有的帧号
     均匀地取，比"隔一帧留一帧"砍得动，也比一个一个点省事。 */
  const want = el('input', 'picker-count');
  want.type = 'number';
  want.min = '1';
  want.placeholder = '12';
  want.title = '想要多少帧';
  bar.append(want);
  button('均匀取 N 帧', '在一整段里均匀地挑 N 帧 —— 一百多帧压到十几帧时常这么干', () => {
    if (!frames.length) { note.textContent = '这段还没有帧'; return; }
    const count = Math.max(1, Math.min(frames.length, Math.round(Number(want.value) || 0)));
    if (!count) { note.textContent = '先在左边那个框里填一个帧数'; return; }
    const list = [];
    for (let step = 0; step < count; step += 1) {
      const index = Math.min(frames.length - 1, Math.floor(step * frames.length / count));
      if (!list.includes(index)) list.push(index);
    }
    pick(list);
    apply('在 ' + frames.length + ' 帧里均匀挑了 ' + sel.size + ' 帧');
  });

  /* 循环在哪儿闭合是量出来的：第 N 帧和第 0 帧最像，那 0..N-1 就是一个闭合的循环，
     后面的都是白转。Unity 里的循环动画就该按这个截。 */
  const loopAt = (cut.stats || {}).loop_best_frame;
  if (loopAt) {
    button('截到最佳循环帧', '第 ' + loopAt + ' 帧和第 0 帧最像，所以前 ' + loopAt
      + ' 帧就是一个闭合的循环，后面的都白转', () => {
      const list = [];
      for (let index = 0; index < Math.min(loopAt, frames.length); index += 1) list.push(index);
      if (!list.length) { note.textContent = '这段还没有帧'; return; }
      pick(list);
      apply('循环在第 ' + loopAt + ' 帧闭合，于是留了前 ' + list.length + ' 帧');
    });
  }

  const range = el('input', 'picker-range');
  range.type = 'text';
  range.placeholder = '0-40,50-70';
  range.title = '按帧号选，写法和命令行的 --frames 一样';
  const applyRange = () => {
    const list = parseRanges(range.value, frames.length);
    if (!list.length) { note.textContent = '这段里没有这些帧号'; return; }
    pick(list);
    apply('按 ' + range.value.trim() + ' 选了 ' + list.length + ' 帧');
  };
  range.addEventListener('keydown', (event) => { if (event.key === 'Enter') applyRange(); });
  bar.append(range);
  button('按范围选', '把输入框里的帧号选中', applyRange);
  button('加进选择', '不动已选的，把输入框里的帧号加进来', () => {
    for (const value of parseRanges(range.value, frames.length)) sel.add(value);
    apply();
  });

  note.textContent = '载入这一段的所有帧…';
  cutFrames(cut).then((list) => {
    frames.push(...list);
    if (!frames.length) {
      note.textContent = '这段还没有帧';
      apply();
      return;
    }
    // 磁盘上已经有选用帧就照着它勾，页面打开时右边看到的就是现在在用的那批。
    const names = new Set(onDisk());
    if (names.size) frames.forEach((item, index) => { if (names.has(item.name)) sel.add(index); });
    apply(names.size
      ? '右边就是磁盘上 kept/ 里那 ' + sel.size + ' 帧。改完点「生成选用帧」覆盖它。'
      : '点左边的缩略图加进右边；右边随时能播，满意了点「生成选用帧」。');
  }).catch(() => { note.textContent = '读不到这一段的帧'; });

  return box;
}

/* ---------------------------------------------------------------- 播放器 */

/* 一段帧的播放器。两种片源共用一套控制：
     - 逐帧：直接放 512/ 里那批 PNG，就是最终要导入 Unity 的那批；
     - 精灵图：放 sprite_sheet.png 的每一片。整张精灵图有四万到七万像素宽，浏览器
       解不动，所以每一片都由后端 /api/slice 现切现送，按顺序播就是在播这张图本身。
   播不动的时候只有一个原因：下一帧还没到。那就等它，而不是跳过去。 */
function animPlayer(opts) {
  let frames = (opts.frames || []).slice();
  const sheet = opts.sheet || null;
  const canvas = opts.canvas || null;
  const defaultFps = Math.max(1, Math.round(opts.fps || 12));

  const state = {
    i: 0, playing: false, fps: defaultFps, speed: 1, loop: true,
    mode: 'frames', timer: null,
  };
  const images = {};                       // url -> 预载好的 Image
  const player = el('div', 'anim');

  const stage = el('div', 'anim-stage');
  stage.tabIndex = 0;
  if (canvas && canvas[0] && canvas[1]) stage.style.aspectRatio = canvas[0] + ' / ' + canvas[1];
  const img = el('img', 'anim-img');
  img.draggable = false;
  img.alt = 'animation';
  stage.append(img);
  player.append(stage);

  const count = () => (state.mode === 'sheet' && sheet ? sheet.frames : frames.length);
  const urlAt = (index) => {
    if (state.mode === 'sheet' && sheet) {
      return '/api/slice?path=' + encodeURIComponent(sheet.path)
        + '&i=' + index + '&w=' + sheet.width;
    }
    return frames[index] ? fileURL(frames[index]) : '';
  };
  const preload = (index) => {
    const url = urlAt(index);
    if (!url || images[url]) return;
    const image = new Image();
    image.addEventListener('load', schedulePaint);
    image.src = url;
    images[url] = image;
  };
  const ready = (index) => {
    const url = urlAt(index);
    if (!url) return false;
    const image = images[url];
    return !!image && image.complete && image.naturalWidth > 0;
  };
  const loadedCount = () => {
    const total = count();
    let done = 0;
    for (let index = 0; index < total; index += 1) if (ready(index)) done += 1;
    return done;
  };

  const delayMs = () => 1000 / Math.max(0.05, state.fps * state.speed);

  /* 往前多备几帧。精灵图的每一片都要后端现切，一次只备下一帧就等于每帧都停下来
     等一次；备一窗口就把切片的活儿排到前面去了。 */
  const AHEAD = 12;
  const preloadAhead = (from, span) => {
    const total = count();
    if (!total) return;
    for (let step = 0; step < Math.min(span || AHEAD, total); step += 1) {
      preload(((from + step) % total + total) % total);
    }
  };
  const show = (index) => {
    const total = count();
    if (!total) return;
    state.i = ((index % total) + total) % total;
    const url = urlAt(state.i);
    if (url && img.getAttribute('src') !== url) img.src = url;
    if (state.playing) preloadAhead(state.i + 1);
    paint();
  };
  const tick = () => {
    if (!state.playing) return;
    const total = count();
    const next = state.i + 1;
    if (next >= total && !state.loop) { pause(); return; }
    const target = ((next % total) + total) % total;
    if (!ready(target)) {
      preloadAhead(state.i, AHEAD);
      state.timer = setTimeout(tick, Math.min(120, delayMs()));
      paint();
      return;
    }
    show(target);
    state.timer = setTimeout(tick, delayMs());
  };

  function play() {
    if (state.playing || count() < 2) return;
    state.playing = true;
    preloadAhead(state.i, AHEAD);
    state.timer = setTimeout(tick, delayMs());
    paint();
  }
  function pause() {
    state.playing = false;
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    paint();
  }
  const toggle = () => (state.playing ? pause() : play());
  const seek = (index) => { show(index); if (state.playing) preload(state.i + 1); };
  /* 换片源不换位置：逐帧和精灵图两边的第 N 帧本来就是同一帧，切过去应该停在原地。 */
  const setMode = (mode) => {
    if (mode === state.mode || (mode === 'sheet' && !sheet)) return;
    const was = state.i;
    state.mode = mode;
    show(Math.min(was, Math.max(0, count() - 1)));
    preloadAhead(state.i, AHEAD);
    if (state.playing) { pause(); play(); }
  };
  /* 换片源：整段帧是后到的（样本 -> 119 帧），选用池的播放器则是每挑一次就换一批
     —— 可长可短可清空，所以不能只许变长。位置尽量留在原地，超界了就贴到最后一帧。 */
  const setFrames = (list) => {
    if (!list) return;
    frames = list.slice();
    /* 走 show 而不是 paint：整段帧是后到的，画面得跟着换成第 0 帧。
       只 paint 的话标签会写"帧 1 / 119"，画面却是空的。 */
    show(Math.min(state.i, frames.length - 1));
  };

  /* -- 控制条 ---------------------------------------------------------- */

  const bar = el('div', 'anim-bar');
  const playBtn = el('button', 'btn tiny anim-play');
  playBtn.title = '播放 / 暂停（空格）';
  playBtn.addEventListener('click', toggle);
  const stepBtn = (text, delta, title) => {
    const button = el('button', 'btn ghost tiny', text);
    button.title = title;
    button.addEventListener('click', () => { pause(); seek(state.i + delta); });
    return button;
  };
  bar.append(playBtn);
  bar.append(stepBtn('\u23ee', -(1e6), '回到第一帧'));
  bar.append(stepBtn('\u25c0', -1, '上一帧（←）'));
  bar.append(stepBtn('\u25b6', 1, '下一帧（→）'));
  bar.append(stepBtn('\u23ed', 1e6, '跳到最后一帧'));

  const fpsInput = el('input', 'anim-fps');
  fpsInput.type = 'number';
  fpsInput.min = '1';
  fpsInput.max = '60';
  fpsInput.value = String(defaultFps);
  fpsInput.title = '播放帧率（meta.json 里量出来的那个）';
  fpsInput.addEventListener('change', () => {
    const value = Math.max(1, Math.min(60, Math.round(Number(fpsInput.value) || defaultFps)));
    fpsInput.value = String(value);
    state.fps = value;
    paint();
  });
  bar.append(el('span', 'anim-tag', 'fps'));
  bar.append(fpsInput);

  const speed = el('select', 'anim-speed');
  for (const value of ['1', '0.5', '0.25', '2']) {
    const option = el('option', null, value + '\u00d7');
    option.value = value;
    speed.append(option);
  }
  speed.title = '播放速度：慢放到 0.25\u00d7 最容易看出走路是不是原地';
  speed.addEventListener('change', () => { state.speed = parseFloat(speed.value); paint(); });
  bar.append(speed);

  const loopBox = el('input', 'anim-loop');
  loopBox.type = 'checkbox';
  loopBox.checked = true;
  loopBox.title = '循环播放（不勾就播到最后一帧停住）';
  loopBox.addEventListener('change', () => { state.loop = loopBox.checked; paint(); });
  const loopLabel = el('label', 'anim-tag');
  loopLabel.append(loopBox);
  loopLabel.append(el('span', null, '循环'));
  bar.append(loopLabel);

  bar.append(el('span', 'spacer'));
  const readout = el('span', 'anim-readout');
  bar.append(readout);

  let modeBtn = null;
  if (sheet) {
    modeBtn = el('button', 'btn ghost tiny');
    modeBtn.title = '逐帧看 512/ 里的 PNG，或者直接播 sprite_sheet.png 切出来的片';
    modeBtn.addEventListener('click', () => { setMode(state.mode === 'sheet' ? 'frames' : 'sheet'); });
    bar.append(modeBtn);
  }
  player.append(bar);

  const scrub = el('input', 'anim-scrub');
  scrub.type = 'range';
  scrub.min = '0';
  scrub.value = '0';
  scrub.title = '拖到任意一帧';
  scrub.addEventListener('input', () => { pause(); seek(Number(scrub.value) || 0); });
  player.append(scrub);

  function paint() {
    const total = count();
    playBtn.textContent = state.playing ? '\u275a\u275a 暂停' : '\u25b6 播放';
    scrub.max = String(Math.max(0, total - 1));
    scrub.value = String(state.i);
    scrub.disabled = total < 2;
    const seconds = total / Math.max(0.05, state.fps * state.speed);
    let text = '帧 ' + (total ? state.i + 1 : 0) + ' / ' + total
      + '   \u00b7   ' + (state.i / Math.max(0.05, state.fps * state.speed)).toFixed(2) + 's / '
      + seconds.toFixed(2) + 's';
    if (state.playing && total) {
      const done = loadedCount();
      if (done < total) text += '   \u00b7   载入 ' + done + '/' + total;
    }
    readout.textContent = text;
    if (modeBtn) {
      modeBtn.textContent = state.mode === 'sheet' ? '精灵图' : '逐帧';
      modeBtn.title = state.mode === 'sheet'
        ? '正在播 sprite_sheet.png 切出来的片，点一下换回逐帧'
        : '正在播 512/ 里的 PNG，点一下改播 sprite_sheet.png';
    }
  }

  let paintTimer = null;
  function schedulePaint() {
    if (paintTimer) return;
    paintTimer = setTimeout(() => { paintTimer = null; paint(); }, 120);
  }

  player.addEventListener('keydown', (event) => {
    if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); toggle(); return; }
    if (event.key === 'ArrowRight') { event.preventDefault(); pause(); seek(state.i + 1); return; }
    if (event.key === 'ArrowLeft') { event.preventDefault(); pause(); seek(state.i - 1); }
  });
  stage.addEventListener('click', () => stage.focus());

  player.play = play;
  player.pause = pause;
  player.seek = seek;
  player.index = () => state.i;
  player.setMode = setMode;
  player.setFrames = setFrames;

  preload(0);
  preload(1);
  /* 第一帧要真画上去。原来这里只有 paint()，而 src 是在 show() 里设的，于是播放器
     一直空着 —— 停在"帧 1 / 119"，画面是一张裂图，非得先按一次播放或翻一帧才出来。 */
  if (count()) show(0); else paint();
  if (opts.autoplay) play();
  return player;
}

/* 一个 cut 的播放器：帧来自 512/，精灵图几何来自 meta.json 旁边的那个 PNG。
   一屏只给 12 张样本时，会顺手把整段帧拉回来，这样播放才是完整的 119 帧。 */
const CUT_FRAMES = {};

/* 一屏只送 12 张样本，但整段帧是按需取的。播放器和挑帧面板要的是同一份，
   所以按目录缓存一次，谁先要谁去拿。 */
function cutFrames(cut) {
  if (!cut || !cut.dir) return Promise.resolve((cut && cut.samples) || []);
  if (!CUT_FRAMES[cut.dir]) {
    CUT_FRAMES[cut.dir] = getJSON('/api/cut?path=' + encodeURIComponent(cut.dir))
      .then((detail) => detail.samples || [])
      .catch(() => (cut.samples || []));
  }
  return CUT_FRAMES[cut.dir];
}

function cutPlayer(cut, opts) {
  const options = opts || {};
  const player = animPlayer({
    frames: cut.samples || [],
    sheet: cut.sheet && cut.sheet_slice
      ? { path: cut.sheet.path, frames: cut.sheet_slice.frames,
          width: cut.sheet_slice.width, height: cut.sheet_slice.height }
      : null,
    canvas: cut.canvas,
    fps: cut.fps,
    autoplay: options.autoplay,
  });
  if ((cut.frames || 0) > (cut.samples || []).length) {
    // 拿不到全量就播样本，够看个大概
    cutFrames(cut).then((list) => player.setFrames(list));
  }
  return player;
}

/* ---------------------------------------------------------------- 保留区 / 归档 */

/* 一段帧在 origin 里叫什么，只有 src/naming.py 说了算 —— 服务器把算好的名字放在
   cut.name 里，这里照抄就是。前端再自己拼一次（clip + model、或者加个 _kept 尾巴）
   就会出现两套默认名：同一个动作从行上转录叫 monster_imp_walk，从大图上转录叫
   monster_imp_walk_kept，最后 origin 里躺着两个文件夹，谁也不知道哪个是最终版。 */
function cutName(cut) {
  if (!cut) return '';
  if (cut.name) return String(cut.name);
  return [cut.entity, cut.clip].filter(Boolean).join('_')
    || String(cut.dir || cut.path || '').replace(/\/$/, '').split('/').pop() || '';
}

/* 把 resource 里的一件东西拷进 origin：要留的那一份。
   重名时先问一句，再带 force 重发一次。 */
async function promote(path, suggested) {
  const answer = window.prompt('转录到 origin，用这个名字：', suggested || '');
  if (answer === null) return;
  const payload = { path: path, name: String(answer).trim() };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response;
    try {
      response = await fetch('/api/promote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      toast('× 转录失败：' + err.message, 'bad');
      return;
    }
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      toast('✔ 已转录到 ' + body.kept.target, 'ok');
      /* 转录改了两张清单：保留区多了一件东西，阶段 2 的原画挑选器多了一张图。
         两张都要马上看得到 —— 转录的下一步就是拿它做视频输入。 */
      await refreshArtwork();
      await refreshEverything();
      return;
    }
    if (body.conflict && attempt === 0) {
      if (!window.confirm(body.error + '　覆盖它？')) { toast('已取消'); return; }
      payload.force = true;
      continue;
    }
    toast('× ' + (body.error || response.statusText), 'bad');
    return;
  }
}

/* 一键转录：路径和名字都是服务器随行一起给的，所以不弹输入框。只有重名才问一句，
   和「转录到 origin」那颗按钮一样。 */
async function promoteNow(item) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response;
    try {
      response = await fetch('/api/promote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: item.path,
          name: item.name,
          entity: item.entity || undefined,
          clip: item.clip || undefined,
          force: attempt > 0,
        }),
      });
    } catch (err) {
      toast('\u00d7 转录失败：' + err.message, 'bad');
      return;
    }
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      toast('\u2714 已转录到 ' + body.kept.target, 'ok');
      state.promoteOpen = null;
      /* 转录改了两张清单：保留区多了一件东西，阶段 2 的原画挑选器多了一张图。 */
      await refreshArtwork();
      await refreshEverything();
      return;
    }
    if (body.conflict && attempt === 0) {
      if (!window.confirm(body.error + '\u3000覆盖它？')) { toast('已取消'); return; }
      continue;
    }
    toast('\u00d7 ' + (body.error || response.statusText), 'bad');
    return;
  }
}

/* 「整理」：把一个从别处拷进来的帧目录改成规范形状 —— 帧重命名成
   <实体>_<动作>_001.png、补上 512 / 256 副本、补精灵图和接触表、写 meta.json。
   读不需要它（拷进来就能列出来、能播、能导入），它只是让外面来的和 pipeline 做的
   分不出来 —— 也就是 Unity 里看到的 sprite 前缀是同一个规则。 */
async function normalizeNow(path) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response;
    try {
      response = await fetch('/api/normalize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path, force: attempt > 0 }),
      });
    } catch (err) {
      toast('\u00d7 整理失败：' + err.message, 'bad');
      return;
    }
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      const done = body.normalized || {};
      toast('\u2714 已整理成 ' + done.name + '（' + (done.frames || '?') + ' 帧）', 'ok');
      await refreshEverything();
      return;
    }
    if (body.conflict && attempt === 0) {
      if (!window.confirm(body.error + '\u3000覆盖它？')) { toast('已取消'); return; }
      continue;
    }
    toast('\u00d7 ' + (body.error || response.statusText), 'bad');
    return;
  }
}

/* 「做视频」落在阶段 2：原画要先补绿、扩留白，才轮得到阶段 3 去调视频模型 ——
   这两件事现在分在两个阶段里，不能像以前那样一步跳到生成。
   没选 run 的时候，阶段 2 会照原画的名字新开一条。
   item 可能是 resource 里还没转录的图 —— 阶段 2 收得下（--source 认项目内的文件），
   只是挑选器里会把它单列出来提醒一句。 */
function useForVideo(item) {
  formOf(2).source = item.path;
  if (modeOf() !== 'kept') {
    state.selected = null; state.selectedRun = null; state.runPath = null; state.detail = null;
  }
  selectStage(2);
  toast('已经填好阶段 2：用 ' + item.name + ' 做视频输入', 'ok');
}

/* 点保留里的一张原画 = 多半就是想拿它做视频，所以顺手把阶段 2 的原画填好。
   只是填好，不跳转、不运行。 */
function prefillStage2(run) {
  if ((run.kept || '') !== 'image' || !run.cover || !run.cover.path) return;
  formOf(2).source = run.cover.path;
}

function keptStillBlock(detail) {
  const box = el('div');
  const meta = detail.meta || {};
  const badges = el('div', 'badges');
  if (detail.image) badges.append(badge('大小', fmtSize(detail.image.size), ''));
  if (meta.size) badges.append(badge('尺寸', meta.size.join('×'), ''));
  if (meta.character) badges.append(badge('角色', meta.character, ''));
  if (meta.promoted) badges.append(badge('转录于', meta.promoted, ''));
  box.append(badges);

  const grid = el('div', 'grid-imgs big');
  grid.append(shot(detail.image, 512, {
    siblings: [detail.image],
    actions: [{ label: '用它生成视频', run: () => useForVideo(detail.image) }],
  }));
  box.append(grid);

  if (detail.source) {
    const from = el('div', 'block');
    from.append(el('h3', null, '来自'));
    from.append(el('div', 'path-line', detail.source));
    box.append(from);
  }
  if (detail.prompt) {
    const block = el('div', 'block');
    block.append(el('h3', null, '当时的提示词'));
    block.append(el('pre', 'prompt', detail.prompt));
    box.append(block);
  }
  return box;
}

function keptCutBlock(detail) {
  const box = el('div');
  box.append(cutBadges(detail));

  const player = cutPlayer(detail);
  box.append(el('h3', null, '播放 · ' + (detail.frames || '?') + ' 帧'
    + (detail.fps ? ' · ' + detail.fps + 'fps' : '')
    + '  ·  已经挑到 origin'));
  box.append(player);

  const tools = el('div', 'panel-tools');
  tools.append(linkButton('放大播放', () => openCutLightbox(detail, player),
    '大图里播：左右翻帧、空格播放，还能切到精灵图'));
  if (detail.contact) tools.append(linkButton('接触表 contact_sheet', () => openLightbox(detail.contact)));
  if (detail.sheet && detail.sheet_slice) {
    tools.append(linkButton('看精灵图（逐片播）', () => openCutLightbox(detail, player, 'sheet'),
      '整张 sprite_sheet.png 有几万像素宽，浏览器解不动，所以由后端一片一片切好再播'));
  }
  if (detail.dir) tools.append(linkButton('打开帧目录', () => reveal(detail.dir)));
  if (detail.normalized === false) {
    tools.append(linkButton('整理命名', () => normalizeNow(detail.path || detail.dir),
      '帧改成 <实体>_<动作>_001.png，补上 512 / 256 副本和精灵图，写 meta.json —— '
      + '从别处拷进来的帧目录靠这一步变得和 pipeline 写的一模一样'));
  }
  if (detail.promoted_from) {
    const from = el('span', 'tag', '来自 ' + detail.promoted_from);
    from.title = detail.promoted_from;
    tools.append(from);
  }
  box.append(tools);

  if (detail.samples && detail.samples.length) {
    const all = detail.samples;
    const shown = all.length > 240 ? all.slice(0, 240) : all;
    const title = '全部 ' + (detail.frames || all.length) + ' 帧'
      + (shown.length < all.length ? '（只列前 ' + shown.length + ' 帧）' : '')
      + '（点一张跳到那一帧）';
    box.append(el('h3', null, title));
    const strip = el('div', 'strip');
    shown.forEach((item, index) => {
      strip.append(shot(item, 128, { onClick: () => player.seek(index) }));
    });
    box.append(strip);
  }
  return box;
}

function folderBlock(detail) {
  const box = el('div');
  const badges = el('div', 'badges');
  badges.append(badge('文件', detail.total, ''));
  if (detail.truncated) badges.append(badge('只列了', detail.files.length, 'warn'));
  box.append(badges);

  const files = detail.files || [];
  const images = files.filter((item) => item.kind === 'image');
  const videos = files.filter((item) => item.kind === 'video');
  const texts = files.filter((item) => item.kind === 'text');
  const others = files.filter((item) => item.kind !== 'image' && item.kind !== 'video' && item.kind !== 'text');

  const imgBlock = imagesBlock('图片', images, 128);
  if (imgBlock) box.append(imgBlock);

  if (videos.length) {
    const block = el('div', 'block');
    block.append(el('h3', null, '视频 · ' + videos.length));
    const grid = el('div', 'videos');
    for (const item of videos) {
      const card = el('div', 'vid');
      const video = document.createElement('video');
      video.src = fileURL(item);
      video.controls = true;
      video.muted = true;
      video.loop = true;
      video.preload = 'metadata';
      card.append(video);
      card.append(el('div', 'bar', item.name));
      grid.append(card);
    }
    block.append(grid);
    box.append(block);
  }

  const block = el('div', 'block');
  block.append(el('h3', null, '其他文件 · ' + (texts.length + others.length)));
  const list = el('div', 'panel-tools');
  for (const item of texts) list.append(linkButton(item.name, () => openText(item)));
  for (const item of others) list.append(el('span', 'tag', item.name));
  block.append(list);
  box.append(block);
  return box;
}

function buildViewerPanel() {
  const panel = el('section', 'panel');
  const head = el('div', 'panel-head');
  const row = el('div', 'head-row');
  row.append(el('h2', null, VIEW_TITLE[state.root] || '归档'));
  row.append(el('span', 'spacer'));
  const dir = state.detail && (state.detail.dir || (state.detail.image && state.detail.image.path));
  if (dir) row.append(linkButton('打开目录', () => reveal(dir)));
  head.append(row);
  head.append(el('p', 'hint', ROOT_HINTS[state.root] || ''));
  panel.append(head);

  if (!state.selected) {
    panel.append(emptyState('还没选东西', isKeptRoot()
      ? '左边点一张原画或一段帧序列。在「原画」和「视频」里选好之后点「转录到 origin」，就会出现在这里。'
      : '左边点一个目录，这里列出它下面的文件。'));
    return panel;
  }
  if (!state.detail) {
    panel.append(el('div', 'empty-state', state.detailError || '读取中…'));
    return panel;
  }
  if (state.detail.kind === 'image') panel.append(keptStillBlock(state.detail));
  else if (state.detail.kind === 'video') panel.append(keptCutBlock(state.detail));
  else panel.append(folderBlock(state.detail));
  return panel;
}

/* ---------------------------------------------------------------- 左侧 */

function renderRootTabs() {
  const host = clear($('root-tabs'));
  const roots = state.runs && state.runs.roots;
  const keys = (roots ? Object.keys(roots) : state.rootKeys).slice();
  /* 归档平时不占页签（从顶栏那颗按钮进），可是进来了就得知道自己在哪儿、也得点得
     回去，所以正看着它的时候补一个。 */
  if (isArchiveRoot() && keys.indexOf('old') < 0) keys.push('old');
  for (const key of keys) {
    const button = el('button', key === state.root ? 'on' : null, ROOT_LABELS[key] || key);
    button.title = (ROOT_HINTS[key] || '') + '   ' + (roots ? roots[key] : '');
    button.addEventListener('click', () => switchRoot(key));
    host.append(button);
  }
}

/* 换一棵树。每棵树各记各的阶段：在视频树挑完帧切回原画，就该回到画和抠。 */
function switchRoot(key) {
  if (state.root === key) return;
  state.stagesByRoot[state.root] = state.stage;
  state.root = key;
  savePref('root', key);
  state.selected = null; state.selectedRun = null; state.detail = null;
  state.runPath = null; state.kept = null; state.promoteOpen = null;
  state.stage = state.stagesByRoot[key] || DEFAULT_STAGE[key] || 1;
  savePref('stage.' + key, state.stage);
  syncURL();
  loadRuns();
  renderRunHead(); renderStageTabs(); renderStageBody();
}

/* 右栏这四个阶段的完成情况从哪来：先看当前显示的 run，再退回列表行。
   服务器两边都给 stages（四个阶段的合计），所以这里不用自己数步骤目录。 */
function stageStates() {
  if (state.detail && state.detail.stages) return state.detail.stages;
  const run = state.selectedRun || {};
  return run.stages || null;
}

/* 一颗点一个阶段。阶段 2 是两个步骤目录合起来的，所以在磁盘上它是两个文件夹、
   在这里是一个点 —— 两个都有产物才算亮。 */
function stagePips(run) {
  const pips = el('span', 'pips');
  const stages = run.stages || {};
  for (const stage of state.stages) {
    const info = stages[String(stage.n)];
    const pip = el('i', info && info.ok ? 'on' : null);
    pip.title = '阶段 ' + stage.n + ' ' + stage.title
      + (info ? '：' + info.files + ' 个文件' : '：还没有产出');
    pips.append(pip);
  }
  return pips;
}

function runRow(run) {
  /* 行是 div 不是 button：里面还要放「→ 保留」。按钮不能嵌在按钮里，键盘可达性
     用 tabindex + 回车补回来。 */
  const row = el('div', 'run' + (run.path === state.selected ? ' on' : ''));
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  const cover = run.cover || (run.kept === 'image' ? run : null);
  if (cover) {
    const img = el('img', 'cover');
    img.loading = 'lazy';
    img.src = fileURL(cover, 96);
    img.alt = run.name;
    row.append(img);
  }
  const body = el('span', 'row-body');
  body.append(el('span', 'name', run.name));
  if (run.kept) {
    /* 保留区和归档里的条目没有四个阶段，只报数量和出处。 */
    const bits = [];
    if (run.frames) bits.push(run.frames + ' 帧');
    else if (run.files) bits.push(run.files + ' 个文件');
    if (run.size) bits.push(fmtSize(run.size));
    /* 动作名单独一段，用 naming 的「实体 · 动作」写法读起来更像一句话；名字本身
       （monster_imp_walk）就在上一行，不用重复。 */
    if (run.label) bits.push(run.label);
    else if (run.clip) bits.push(run.clip);
    /* 从别处拷进来的帧目录：能看能播，只是名字和帧号还不规范，标一句好让「整理」
       那颗按钮出现得有理有据。 */
    if (run.normalized === false) bits.push('未整理');
    if (run.models && run.models.length) bits.push(run.models.join('/'));
    body.append(el('span', 'sub', bits.join(' · ') || '已保留'));
    /* 这一件是从哪儿挑出来的。找回来后想再切一遍、或者想拿同一条 run 里的别的帧，
       都要靠这一行。 */
    if (run.from) {
      const from = el('span', 'from', '来自 ' + run.from);
      from.title = run.from;
      body.append(from);
    }
  } else {
    body.append(stagePips(run));
    const bits = [];
    if (run.models && run.models.length) bits.push(run.models.length + ' 个模型');
    if (run.clips && run.clips.length) bits.push('动作 ' + run.clips.join('/'));
    body.append(el('span', 'sub', bits.join(' · ') || '只有原画'));
  }
  /* 一键转录：只在两棵树里出现，保留和归档里的东西本来就在 origin 了。
     候选是服务器算的（阶段 2 的 512 副本 / 阶段 4 的 kept），所以这里不用猜路径，
     名字也跟着候选一起来 —— 不用弹输入框，点一下就是转录完。 */
  const candidates = (state.root === 'image' || state.root === 'video')
    ? (run.promote || []) : [];
  if (candidates.length) {
    const keep = el('button', 'promote');
    keep.type = 'button';
    keep.append(el('span', null, '\u2192 保留'));
    if (candidates.length === 1) {
      keep.title = '转录到 origin：' + candidates[0].label + ' → ' + candidates[0].name;
      keep.addEventListener('click', (event) => {
        event.stopPropagation();
        promoteNow(candidates[0]);
      });
    } else {
      keep.title = '这条 run 下有 ' + candidates.length + ' 段，点开挑一段转录到 origin';
      keep.addEventListener('click', (event) => {
        event.stopPropagation();
        state.promoteOpen = state.promoteOpen === run.path ? null : run.path;
        renderRunList();
      });
    }
    /* 挂在 row-body 的第二行右端，不是整行的末尾：名字那一行就不用给按钮让位
       （侧栏窄，让出来的几个字正好是 run 名被截掉的那一截）。 */
    body.classList.add('has-promote');
    body.append(keep);
  }
  row.append(body);

  if (candidates.length && state.promoteOpen === run.path) {
    const list = el('div', 'promote-list');
    for (const item of candidates) {
      const pick = el('button', 'pick');
      pick.type = 'button';
      pick.append(el('b', null, item.label));
      pick.append(el('span', null, item.name));
      pick.title = '转录到 origin/video/' + item.name;
      pick.addEventListener('click', (event) => {
        event.stopPropagation();
        promoteNow(item);
      });
      list.append(pick);
    }
    row.append(list);
    row.classList.add('open');
  }

  /* 拷贝进来的帧目录：整理按钮。列表和播放都不需要它，所以它只在真需要的时候出现。 */
  if (run.kept === 'video' && run.normalized === false) {
    const tidy = el('button', 'promote');
    tidy.type = 'button';
    tidy.title = '整理命名：把 ' + run.name + ' 的帧改成 <实体>_<动作>_001.png，'
      + '补上 512 / 256 副本和精灵图';
    tidy.append(el('span', null, '整理'));
    tidy.addEventListener('click', (event) => {
      event.stopPropagation();
      normalizeNow(run.path);
    });
    body.classList.add('has-promote');
    body.append(tidy);
  }

  row.title = run.name + '   ' + run.path;
  row.addEventListener('click', () => selectRun(run));
  row.addEventListener('keydown', (event) => {
    if (event.target !== row) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectRun(run);
    }
  });
  return row;
}

function renderRunList() {
  const host = clear($('run-list'));
  if (!state.runs) { host.append(el('div', 'side-empty', '读取中…')); return; }
  let runs = 0;
  for (const group of state.runs.groups) {
    const wrap = el('div', 'run-group');
    const head = el('div', 'date');
    head.append(el('span', null, group.date));
    if (!group.is_date && (isArchiveRoot() || isKeptRoot())) {
      /* 保留区分成「原画」和「视频」两组，标题本身就是分类，再贴「保留」两个字是废话；
         这里报件数，空的那一组不报。 */
      if (group.runs.length) head.append(el('span', 'tag', group.runs.length + ' 件'));
    }
    wrap.append(head);
    for (const run of group.runs) { wrap.append(runRow(run)); runs += 1; }
    host.append(wrap);
  }
  if (state.runs.loose.length) {
    const wrap = el('div', 'run-group');
    wrap.append(el('div', 'date', state.runs.loose_title || '单个文件'));
    for (const item of state.runs.loose) {
      if (isKeptRoot()) { wrap.append(runRow(item)); runs += 1; continue; }
      const row = el('button', 'run');
      row.append(el('span', 'name', item.name));
      row.append(el('span', 'sub', fmtSize(item.size)));
      row.addEventListener('click', () => openLightbox(item));
      wrap.append(row);
    }
    host.append(wrap);
  }
  if (!runs && !state.runs.loose.length) {
    host.append(el('div', 'side-empty', state.q ? '没有匹配的 run' : '这个目录是空的'));
  }
}

function renderRunHead() {
  const host = clear($('run-head'));
  const mode = modeOf();
  const run = state.selectedRun || {};
  if (!state.selected) {
    const tips = {
      run: '左边选一个 run —— 阶段 2 到 4 都是对它操作；想生成视频，先把原画「转录到 origin」',
      kept: '左边点一件留下来的东西 —— 右栏是它本身，和它出处那条 run 的四个阶段',
      folder: '左边点一个目录，这里列出它下面的文件',
    };
    host.append(el('span', 'missing', tips[mode] || tips.folder));
    return;
  }
  host.append(el('span', 'title', run.name || state.selected.split('/').pop()));
  host.append(el('span', 'rel', state.selected));
  if (mode === 'kept' && state.runPath) {
    host.append(el('span', 'tag', 'run ' + state.runPath));
  }
  host.append(el('span', 'spacer'));
  if (mode !== 'folder' && state.detail && state.detail.stages) {
    host.append(stagePips(state.detail));
  } else {
    host.append(el('span', 'tag', ROOT_LABELS[state.root] || ''));
  }
  const dir = state.detail && (state.detail.dir || state.detail.image_dir || state.detail.video_dir);
  if (dir) host.append(linkButton('打开目录', () => reveal(dir)));
}
/* ---------------------------------------------------------------- 阶段 */

function renderStageTabs() {
  const host = clear($('stage-tabs'));
  /* 归档（resource/old）是旧目录结构留下的，没有四个阶段可点，整条阶段栏收起来。
     保留区有：那四个阶段就是这件东西出处那条 run 的阶段。 */
  host.hidden = modeOf() === 'folder';
  if (host.hidden) return;
  const stages = stageStates();
  for (const stage of state.stages) {
    const info = stages && stages[String(stage.n)];
    const button = el('button', (state.stage === stage.n ? 'on ' : '') + (info && info.ok ? 'done' : ''));
    button.append(el('span', 'idx', String(stage.n)));
    button.append(el('span', null, stage.title));
    /* AI / 本机：哪两步要花钱，一眼就该看得出来。 */
    button.append(el('span', 'engine', stage.engine));
    button.title = stage.hint;
    button.addEventListener('click', () => selectStage(stage.n));
    host.append(button);
  }
  const all = el('button', state.stage === 'all' ? 'on ' : '');
  all.append(el('span', 'idx', '\u25b6'));
  all.append(el('span', null, '全流程'));
  all.title = '五个步骤一路跑到底（= 四个阶段全跑），或者从某个阶段接着往下跑';
  all.addEventListener('click', () => selectStage('all'));
  host.append(all);
}

function selectStage(stage) {
  state.stage = stage;
  state.stagesByRoot[state.root] = stage;
  savePref('stage.' + state.root, stage);
  syncURL();
  renderStageTabs();
  renderStageBody();
}

function renderStageBody() {
  state.refsRedraw = null;
  const host = clear($('stage-body'));
  if (modeOf() === 'folder') {
    /* 归档：没有表单，只有这一件东西本身。 */
    host.append(buildViewerPanel());
    return;
  }
  const meta = stageMeta(state.stage);
  const grid = el('div', 'stage-grid');
  grid.append(buildFormPanel(meta));
  grid.append(buildArtifactPanel(meta));
  host.append(grid);
}

/* ---------------------------------------------------------------- 动作 */

async function loadState() {
  const data = await getJSON('/api/state');
  state.paths = data.project;
  state.pathsAbs = data.project_abs || {};
  state.promptDirAbs = data.prompt_dir_abs || '';
  state.env = data.env_overrides || {};
  state.defaults = data.defaults || {};
  state.specs = data.specs || [];
  state.styles = data.styles || [];
  state.stages = data.stages || [];
  state.presets = data.presets || [];
  state.artwork = data.artwork || [];
  state.promptDir = data.prompt_dir || '';
  state.job = data.job || null;
  state.scan = 0;
  state.scannedJob = null;
  renderPaths();
}

/* 只把「origin 里现在有什么」重读一遍。转录之后要刷的就是这一样，
   走完整的 loadState 会把磁盘上那份日志扫描状态也一起清掉，日志会白闪一下。 */
async function refreshArtwork() {
  try {
    const data = await getJSON('/api/state');
    if (data.artwork) state.artwork = data.artwork;
    if (data.presets) state.presets = data.presets;
  } catch (err) { /* 读不到就先用手上这份 */ }
}

/* 顶栏那排路径。**显示的一律是相对项目根的**：这个项目要能整体拷贝、要开源，
   截图里不该出现别人机器上的盘符；完整路径放在 title 里，悬停才看得到。
   第一颗给的是根目录的文件夹名 —— 它回答的是「我现在看的是哪一份项目」，
   所以另一颗「视频预设」用不着再报一次 hareness 的全名。 */
function renderPaths() {
  const host = clear($('paths'));
  const abs = state.pathsAbs || {};
  const items = [
    ['根目录', state.paths.root, abs.root],
    ['工作区', state.paths.resource, abs.resource],
    ['母版', state.paths.origin, abs.origin],
    ['提示词库', state.paths.harness, abs.harness],
    ['视频预设', state.promptDir, state.promptDirAbs],
  ];
  for (const [label, value, full] of items) {
    if (!value) continue;
    const chip = el('div', 'path-chip');
    chip.append(el('b', null, label));
    chip.append(el('span', null, value));
    chip.title = label + '：' + (full || value);
    host.append(chip);
  }
  const overrides = Object.keys(state.env || {});
  if (overrides.length) {
    const chip = el('div', 'path-chip warn');
    chip.append(el('b', null, '环境变量'));
    chip.append(el('span', null, overrides.map((key) => key + '=' + state.env[key]).join('   ')));
    chip.title = '这些环境变量正在覆盖默认路径';
    host.append(chip);
  }
}

async function loadRuns() {
  const url = '/api/runs?root=' + encodeURIComponent(state.root) + '&q=' + encodeURIComponent(state.q);
  try {
    state.runs = await getJSON(url);
    state.rootKeys = Object.keys(state.runs.roots || {});
  } catch (err) {
    toast('\u00d7 ' + err.message, 'bad');
  }
  renderRootTabs();
  renderRunList();
}

/* 是不是在保留区（合并的那个视图或拆开的两半都算）。 */
function isKeptRoot(root) {
  return KEPT_ROOTS.indexOf(root === undefined ? state.root : root) >= 0;
}

function isArchiveRoot() {
  return state.root === 'old';
}

/* 选中的是什么，决定右半边显示什么：run 是流水线，kept 是留下来的东西，
   folder 是归档里的普通目录。 */
function modeOf() {
  if (isKeptRoot()) return 'kept';
  if (isArchiveRoot()) return 'folder';
  return 'run';
}

function detailURL() {
  const mode = modeOf();
  const prefix = mode === 'kept'
    ? '/api/kept?path='
    : (mode === 'folder' ? '/api/folder?path=' : '/api/run?path=');
  return prefix + encodeURIComponent(state.selected);
}

/* 右栏要两样东西才画得出来：这件东西本身，和它出处那条 run。
   保留区是唯一需要两样一起读的地方 —— 只读第一样，右栏就只剩一张大图了。 */
async function loadDetail() {
  state.detail = null;
  state.detailError = null;
  if (!state.selected) return;
  const mode = modeOf();
  if (mode === 'kept') {
    try {
      state.kept = await getJSON(detailURL());
    } catch (err) {
      state.kept = null;
      state.runPath = null;
      state.detailError = '读不出来：' + err.message;
      return;
    }
    // 出处 run 是从转录时记下的路径里读回来的，可能是空（直接放进 origin 的东西）。
    state.runPath = state.kept.run || null;
    if (!state.runPath) return;
    try {
      state.detail = await getJSON('/api/run?path=' + encodeURIComponent(state.runPath));
    } catch (err) {
      state.detail = null;
      state.detailError = '读不出来它出处的 run（' + state.runPath + '）：' + err.message;
    }
    inheritPresets();
    return;
  }
  state.kept = null;
  try {
    state.detail = await getJSON(detailURL());
  } catch (err) {
    state.detail = null;
    state.detailError = '读不出来：' + err.message;
  }
  inheritPresets();
}

/* 阶段 3 的提示词预设：阶段 2 在这条 run 上勾过哪几个，就默认勾哪几个。
   补的是"显示的和跑的不是一回事"这件事 —— 阶段 3 不带 --preset 时，命令行会回落到
   这条 run 记下的那几份，可表单上原来一个字也不提。 */
function inheritPresets() {
  const detail = state.detail;
  if (!detail || !detail.run) return;
  if (state.presetsFilledFor === detail.run) return;
  state.presetsFilledFor = detail.run;
  const recorded = (detail.prompt_presets || {}).presets || [];
  if (!recorded.length) return;
  const known = new Set(state.presets.map((preset) => preset.name));
  const merged = new Set(formOf(3).presets || []);
  for (const name of recorded) if (known.has(name)) merged.add(name);
  formOf(3).presets = state.presets.map((preset) => preset.name)
    .filter((name) => merged.has(name));
}

function findRun(path) {
  if (!state.runs) return null;
  for (const group of state.runs.groups || []) {
    for (const run of group.runs) if (run.path === path) return run;
  }
  for (const item of state.runs.loose || []) if (item.path === path) return item;
  return null;
}

async function selectRun(run) {
  const mode = modeOf();
  state.selected = run.path;
  state.selectedRun = run;
  state.kept = null;
  state.detail = null;
  state.detailError = null;
  /* 保留区的 path 是 origin/... ，不是 run key；跟着它出处那条 run 走。
     点一张保留的原画顺手把阶段 2 的原画填好 —— 多半就是为了做视频才点它的。 */
  state.runPath = mode === 'run' ? run.path : (run.run || null);
  if (mode === 'kept') {
    prefillStage2(run);
    /* 保留区里那四个阶段是"它出处那条 run"的阶段，可阶段 1 是画 —— 已经挑进 origin
       的东西早过了那一步，停在上面只会看到空面板。所以看情况抬一手：原画抬到
       阶段 2（接下来是把它做成视频输入），帧抬到阶段 4（接下来是看帧、挑帧）。
       本来就在 2/3/4 的人不动他的位置。 */
    if (state.stage === 1) {
      const landing = run.kept === 'video' ? 4 : 2;
      state.stage = landing;
      state.stagesByRoot[state.root] = landing;
      savePref('stage.' + state.root, landing);
    }
  }
  syncURL();
  renderRunList(); renderRunHead(); renderStageTabs(); renderStageBody();
  await loadDetail();
  renderRunHead(); renderStageTabs(); renderStageBody();
}

async function refreshEverything() {
  await loadRuns();
  if (state.selected) {
    state.selectedRun = findRun(state.selected);
    if (modeOf() === 'run') state.runPath = state.selected;
    await loadDetail();
  }
  renderRunHead(); renderStageTabs(); renderStageBody();
}

/* 阶段 2 会替原画开一条新 run。跑完就跟着它走，否则还停在阶段 2 上，
   接下来阶段 3 没有 run 可点。只在「本来就没指定 run」的时候跟，
   不然重跑某条 run 会被带到别的 run 上去。 */
async function followNewRun() {
  if (state.jobRun) return;
  if (String(state.stage) !== '2' && state.stage !== 'all') return;
  if (state.root !== 'video') {
    state.root = 'video';
    savePref('root', 'video');
  }
  await loadRuns();
  const first = ((state.runs.groups || [])[0] || {}).runs || [];
  const newest = first[0];
  if (!newest) return;
  toast('跳到新 run：' + newest.name, 'ok');
  await selectRun(newest);
}

async function startJob(body) {
  try {
    const result = await postJSON('/api/job/start', body || requestBody());
    state.job = result.job;
    state.jobRun = state.runPath;
    state.scan = 0;
    state.scannedJob = null;
    clear($('log-lines'));
    setLogCollapsed(false);
    renderLog(); renderStageBody();
    toast('\u25b6 ' + result.job.label, 'ok');
  } catch (err) {
    toast('\u00d7 ' + err.message, 'bad');
  }
}

async function stopJob() {
  try { await postJSON('/api/job/stop', {}); toast('已请求停止', 'ok'); }
  catch (err) { toast('\u00d7 ' + err.message, 'bad'); }
}

async function reveal(path) {
  try { await postJSON('/api/reveal', { path }); }
  catch (err) { toast('\u00d7 ' + err.message, 'bad'); }
}
/* ---------------------------------------------------------------- 日志 */

async function poll() {
  try {
    const result = await getJSON('/api/job/log?scan=' + state.scan);
    const job = result.job;
    const wasRunning = state.job && state.job.status === 'running';
    if (job) {
      if (job.id !== state.scannedJob) {
        state.scannedJob = job.id;
        state.scan = 0;
        clear($('log-lines'));
      }
      appendLog(job.lines);
      state.scan = job.total;
    }
    state.job = job;
    renderLog();
    if (wasRunning && job && job.status !== 'running') {
      const ok = job.status === 'done';
      toast((ok ? '\u2714 ' : '\u00d7 ') + job.label + ' · ' + (STATUS_TEXT[job.status] || job.status), ok ? 'ok' : 'bad');
      await refreshEverything();
      if (ok) await followNewRun();
      state.jobRun = null;
    }
  } catch (err) {
    /* 服务关了就别刷屏 */
  }
}

function lineClass(line) {
  if (/error|traceback|exception|failed|失败/i.test(line)) return 'l-err';
  if (/warn|警告/i.test(line)) return 'l-warn';
  return null;
}

function appendLog(lines) {
  if (!lines || !lines.length) return;
  const host = $('log-lines');
  const stick = host.scrollTop + host.clientHeight >= host.scrollHeight - 30;
  for (const line of lines) host.append(el('span', lineClass(line), line + '\n'));
  if (stick) host.scrollTop = host.scrollHeight;
}

function renderLog() {
  const job = state.job;
  const status = job ? job.status : null;
  $('busy-dot').className = 'dot'
    + (status === 'running' ? ' busy' : status === 'done' ? ' done' : status === 'failed' ? ' failed' : '');
  const label = $('log-status');
  label.className = 'log-status' + (status ? ' ' + status : '');
  label.textContent = job
    ? job.label + ' · ' + (STATUS_TEXT[status] || status) + ' · ' + fmtTime(job.elapsed)
    : '空闲';
  $('log-cmd').textContent = job ? '$ python main.py ' + job.command : '';
  $('btn-stop').disabled = !(job && status === 'running');
}

/* 日志面板的高度：拖顶边那条把手改，双击回默认，存进 localStorage。
   折叠和拖拽抢的是同一个 height，所以两边要成对地设 —— 折叠时把行内样式清掉让 CSS
   的 34px 生效，展开时再把记下来的高度写回去（行内样式会盖过 .logbar.collapsed）。 */
const LOG_HEIGHT_DEFAULT = 210;
const LOG_HEIGHT_MIN = 80;

/* 当前高度记在这儿，不从 DOM 读回来：拖着的时候 height 上有过渡，动画还没跟上的
   话 getBoundingClientRect 读到的是上一帧的值，松手就会存下一个过时的高度。 */
let logHeightNow = LOG_HEIGHT_DEFAULT;

function logHeightMax() {
  return Math.max(LOG_HEIGHT_MIN + 60, window.innerHeight - 240);
}

function setLogHeight(px, save) {
  const bar = $('logbar');
  if (bar.classList.contains('collapsed')) return;
  logHeightNow = Math.round(Math.max(LOG_HEIGHT_MIN, Math.min(px, logHeightMax())));
  bar.style.height = logHeightNow + 'px';
  if (save !== false) savePref('logHeight', logHeightNow);
}

function setLogCollapsed(collapsed) {
  const bar = $('logbar');
  if (collapsed) {
    if (!bar.classList.contains('collapsed')) {
      savePref('logHeight', logHeightNow);
    }
    bar.style.height = '';
  } else {
    const saved = Number(loadPref('logHeight', LOG_HEIGHT_DEFAULT)) || LOG_HEIGHT_DEFAULT;
    logHeightNow = saved;
    bar.style.height = saved + 'px';
  }
  bar.classList.toggle('collapsed', collapsed);
}

function bindLogResize() {
  const bar = $('logbar');
  const grip = $('log-grip');
  const saved = loadPref('logHeight', null);
  if (saved) setLogHeight(Number(saved), false);
  let from = 0; let start = 0;
  /* 抓住把手往上拖 = 面板变高，所以差值是反的。 */
  grip.addEventListener('pointerdown', (event) => {
    if (bar.classList.contains('collapsed')) return;
    event.preventDefault();
    grip.setPointerCapture(event.pointerId);
    bar.classList.add('dragging');
    from = event.clientY;
    start = bar.getBoundingClientRect().height;
  });
  grip.addEventListener('pointermove', (event) => {
    if (!bar.classList.contains('dragging')) return;
    setLogHeight(start + (from - event.clientY), false);
  });
  const stop = () => {
    if (!bar.classList.contains('dragging')) return;
    bar.classList.remove('dragging');
    savePref('logHeight', logHeightNow);
  };
  grip.addEventListener('pointerup', stop);
  grip.addEventListener('pointercancel', stop);
  grip.addEventListener('dblclick', () => setLogHeight(LOG_HEIGHT_DEFAULT));
  /* 窗口变矮的时候别让日志把主区挤没了。 */
  window.addEventListener('resize', () => {
    if (bar.classList.contains('collapsed')) return;
    if (logHeightNow > logHeightMax()) setLogHeight(logHeightMax(), true);
  });
}

function toast(message, kind) {
  const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
  $('toasts').append(node);
  setTimeout(() => {
    node.classList.add('out');
    setTimeout(() => node.remove(), 320);
  }, 3600);
}

/* ---------------------------------------------------------------- 大图 */

function openOverlay(child, caption) {
  const inner = clear($('lightbox-inner'));
  inner.append(child);
  $('lightbox-cap').textContent = caption || '';
  $('lightbox').hidden = false;
}

const light = {
  items: [], index: 0, zoom: 1, x: 0, y: 0, drag: null,
  sheet: null, cut: null, mode: 'frames', playing: false, fps: 12, speed: 1, timer: null,
};

/* 一张图，或者一整段帧。滚轮缩放、拖动平移、左右翻帧；一段帧还能直接播（空格）。
   精灵图模式播的是 sprite_sheet.png 的每一片，由后端切好送过来：整张图四万到七万
   像素宽，浏览器根本解不动。两种模式共用同一个位置，所以切换的时候停的是同一帧。 */
function openLightbox(item, siblings, cut, opts) {
  const options = opts || {};
  const list = siblings && siblings.length ? siblings.slice() : (item ? [item] : []);
  light.items = list;
  light.index = Math.max(0, list.findIndex((entry) => entry.path === item.path));
  if (options.index !== undefined) {
    light.index = Math.max(0, Math.min(Number(options.index) || 0, Math.max(0, list.length - 1)));
  }
  light.zoom = 1; light.x = 0; light.y = 0; light.drag = null;
  light.sheet = options.sheet || null;
  /* 大图里要能直接转录。整段从 cut.samples 点进来的带着整个 cut 对象，
     单独一张图（原画树里的原画）只有 item 本身 —— 两种都收。 */
  light.cut = options.cut || null;
  light.mode = options.sheet && options.mode === 'sheet' ? 'sheet' : 'frames';
  light.fps = Math.max(1, Math.round(options.fps || 12));
  light.speed = 1;
  pauseLightbox();
  if (cut) loadAllFrames(cut, item);
  renderLightbox();
  $('lightbox').hidden = false;
}

/* 从一段帧的播放器点进来：接着它正在播的那一帧继续看。
   带 mode 'sheet' 就是直接进精灵图模式。 */
function openCutLightbox(cut, player, mode) {
  const list = (cut.samples || []).slice();
  if (!list.length) return;
  const at = player ? Math.min(player.index(), list.length - 1) : 0;
  openLightbox(list[at], list, cut.dir, {
    cut: cut,
    sheet: cut.sheet && cut.sheet_slice
      ? { path: cut.sheet.path, frames: cut.sheet_slice.frames,
          width: cut.sheet_slice.width, height: cut.sheet_slice.height, mtime: cut.sheet.mtime }
      : null,
    fps: cut.fps,
    index: mode === 'sheet' ? at : at,
    mode: mode === 'sheet' ? 'sheet' : 'frames',
  });
}

async function loadAllFrames(cut, item) {
  try {
    const detail = await getJSON('/api/cut?path=' + encodeURIComponent(cut));
    if (!detail.samples || detail.samples.length <= light.items.length) return;
    const here = light.items[light.index] && light.items[light.index].path;
    light.items = detail.samples;
    const at = light.items.findIndex((entry) => entry.path === here);
    light.index = at < 0 ? 0 : at;
    renderLightbox();
  } catch (err) { /* 翻不了就算了，样本已经够看 */ }
}

function lightboxItem() { return light.items[light.index] || null; }

/* 大图里这一帧（或者这一整段）能不能转录，能的话按钮长什么样。
   判据只有一条：东西在 resource 里。origin 里的已经在 origin 了。 */
function lightboxPromotable() {
  if (light.cut) {
    const name = String(light.cut.path || light.cut.dir || '').replace(/\/$/, '').split('/').pop();
    return {
      label: '转录到 origin',
      title: '把这整段帧（含 512 / 256 副本、精灵图、统计）拷到 origin/video',
      run: () => promote(light.cut.dir, cutName(light.cut) || name),
    };
  }
  const item = lightboxItem();
  if (!item || !item.path || item.path.indexOf('resource/') !== 0) return null;
  /* 拼图和统计不是原画。它们也是 resource 里的 PNG，误转录过去就会有一张
     名叫 contact_sheet 的"原画"躺在 origin/image 里等着被拿去做视频。判据和
     src/naming.py 一样：名字里有 sheet 就不是帧、也不是原画。 */
  if (/sheet/i.test(item.name)) return null;
  return {
    label: '转录到 origin',
    title: '把这一张拷到 origin/image，之后阶段 2 就能拿它做视频输入',
    run: () => promote(item.path, entityOf(item.name)),
  };
}

function lightCount() {
  if (light.mode === 'sheet' && light.sheet) return light.sheet.frames;
  return light.items.length;
}

function lightURL() {
  if (light.mode === 'sheet' && light.sheet) {
    return '/api/slice?path=' + encodeURIComponent(light.sheet.path)
      + '&i=' + light.index + '&w=' + light.sheet.width
      + (light.sheet.mtime ? '&v=' + Math.round(light.sheet.mtime) : '');
  }
  const item = lightboxItem();
  return item ? fileURL(item) : '';
}

const lightImage = document.createElement('img');
lightImage.draggable = false;
lightImage.alt = 'frame';

function renderLightbox() {
  const total = lightCount();
  if (!total) return;
  const inner = clear($('lightbox-inner'));
  const url = lightURL();
  if (url && lightImage.getAttribute('src') !== url) lightImage.src = url;
  lightImage.style.transform = 'translate(' + light.x + 'px, ' + light.y + 'px) scale(' + light.zoom + ')';
  inner.append(lightImage);

  const item = lightboxItem();
  const cap = clear($('lightbox-cap'));
  if (light.mode === 'sheet' && light.sheet) {
    cap.append(el('span', 'strong', 'sprite_sheet.png'));
    cap.append(el('span', null, '   ·   第 ' + (light.index + 1) + ' 片 / ' + total
      + '   ·   ' + light.sheet.width + '×' + light.sheet.height + ' 一片'));
  } else {
    cap.append(el('span', 'strong', item ? item.name : ''));
    if (item && item.size !== undefined) cap.append(el('span', null, '   ·   ' + fmtSize(item.size)));
    if (total > 1) cap.append(el('span', null, '   ·   ' + (light.index + 1) + ' / ' + total));
  }
  if (light.zoom !== 1) cap.append(el('span', null, '   ·   ' + Math.round(light.zoom * 100) + '%'));

  /* 看中了就顺手转录，不用关掉大图回列表里找按钮。已经在 origin 里的东西
     （保留区点开的）不给这颗按钮 —— 它本来就在那儿了。 */
  const source = lightboxPromotable();
  if (source) {
    const keep = linkButton(source.label, () => source.run(), source.title);
    cap.append(keep);
  }

  if (total > 1) {
    const playBtn = el('button', 'btn tiny', light.playing ? '❚❚ 暂停' : '▶ 播放');
    playBtn.title = '空格播放 / 暂停';
    playBtn.addEventListener('click', (event) => { event.stopPropagation(); toggleLightbox(); });
    cap.append(playBtn);
    if (light.sheet) {
      const modeBtn = el('button', 'btn ghost tiny',
        light.mode === 'sheet' ? '精灵图' : '逐帧');
      modeBtn.title = '在逐帧 PNG 和 sprite_sheet.png 的切片之间切换';
      modeBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        setLightboxMode(light.mode === 'sheet' ? 'frames' : 'sheet');
      });
      cap.append(modeBtn);
    }
    cap.append(el('span', 'hint', light.playing
      ? '   空格暂停   ·   滚轮缩放   ·   点空白处关闭'
      : '   空格播放   ·   ← → 翻帧   ·   滚轮缩放'));
  } else {
    cap.append(el('span', 'hint', '   滚轮缩放   ·   点空白处关闭'));
  }
}

const lightDelay = () => 1000 / Math.max(0.05, light.fps * light.speed);

function tickLightbox() {
  if (!light.playing) return;
  stepLightbox(1, true);
  light.timer = setTimeout(tickLightbox, lightDelay());
}

function playLightbox() {
  if (light.playing || lightCount() < 2) return;
  light.playing = true;
  light.timer = setTimeout(tickLightbox, lightDelay());
  renderLightbox();
}

function pauseLightbox() {
  light.playing = false;
  if (light.timer) { clearTimeout(light.timer); light.timer = null; }
  if (!$('lightbox').hidden) renderLightbox();
}

const toggleLightbox = () => (light.playing ? pauseLightbox() : playLightbox());

function setLightboxMode(mode) {
  if (mode === light.mode) return;
  if (mode === 'sheet' && !light.sheet) return;
  const before = lightCount();
  light.mode = mode;
  const after = lightCount();
  light.index = Math.min(light.index, Math.max(0, after - 1));
  if (before !== after) light.zoom = 1;
  renderLightbox();
}

function zoomLightbox(factor, clientX, clientY) {
  const before = light.zoom;
  light.zoom = Math.min(12, Math.max(1, light.zoom * factor));
  const box = $('lightbox').getBoundingClientRect();
  const cx = (clientX === undefined ? box.left + box.width / 2 : clientX) - (box.left + box.width / 2);
  const cy = (clientY === undefined ? box.top + box.height / 2 : clientY) - (box.top + box.height / 2);
  const ratio = light.zoom / before;
  light.x = cx - (cx - light.x) * ratio;
  light.y = cy - (cy - light.y) * ratio;
  if (light.zoom === 1) { light.x = 0; light.y = 0; }
  renderLightbox();
}

function stepLightbox(delta, keepZoom) {
  const total = lightCount();
  if (total < 2) return;
  light.index = ((light.index + delta) % total + total) % total;
  if (!keepZoom) { light.zoom = 1; light.x = 0; light.y = 0; }
  renderLightbox();
}

function closeLightbox() {
  pauseLightbox();
  $('lightbox').hidden = true;
  light.cut = null;
  light.items = []; light.index = 0; light.zoom = 1; light.x = 0; light.y = 0; light.drag = null;
  light.sheet = null; light.mode = 'frames'; light.speed = 1; light.playing = false;
}

async function openText(item) {
  const pre = el('pre', null, '读取中…');
  openOverlay(pre, item.name + '   ·   点任意处关闭');
  try {
    const response = await fetch(fileURL(item), { cache: 'no-store' });
    if (!response.ok) throw new Error(response.statusText);
    pre.textContent = await response.text();
  } catch (err) {
    pre.textContent = '读不出来：' + err.message;
  }
}

/* ---------------------------------------------------------------- 启动 */

/* 地址栏带着 run / stage / root，刷新不丢、也能直接发给别人。
   这是前端唯一回写地址栏的地方，改视图的每一处都调它。 */
function syncURL() {
  const params = new URLSearchParams();
  if (state.root !== 'image') params.set('root', state.root);
  if (String(state.stage) !== '1') params.set('stage', String(state.stage));
  if (state.selected) params.set('run', state.selected);
  if (state.shot) params.set('shot', state.shot);
  const query = params.toString();
  try {
    history.replaceState(null, '', query ? '?' + query : location.pathname);
  } catch (err) { /* file:// 打开时不允许 */ }
}

function readURL() {
  const params = new URLSearchParams(location.search);
  state.root = params.get('root') || loadPref('root', state.root);
  const stage = params.get('stage');
  if (stage) {
    state.stage = stage === 'all' ? 'all' : (Number(stage) || 1);
  } else {
    /* 地址栏没点名哪个阶段，就回到这棵树上一次停的那一阶段。 */
    const saved = loadPref('stage.' + state.root, null);
    state.stage = saved === null || saved === undefined ? (DEFAULT_STAGE[state.root] || 1) : saved;
  }
  state.stagesByRoot[state.root] = state.stage;
  state.shot = params.get('shot') || '';
  return params.get('run') || '';
}

function bindLightbox() {
  const box = $('lightbox');
  box.addEventListener('click', () => { if (light.zoom === 1) closeLightbox(); });
  box.addEventListener('contextmenu', (event) => event.preventDefault());
  box.addEventListener('wheel', (event) => {
    event.preventDefault();
    zoomLightbox(event.deltaY < 0 ? 1.25 : 0.8, event.clientX, event.clientY);
  }, { passive: false });
  box.addEventListener('mousedown', (event) => {
    if (light.zoom === 1) return;
    light.drag = { x: event.clientX, y: event.clientY, ox: light.x, oy: light.y };
    event.preventDefault();
  });
  window.addEventListener('mousemove', (event) => {
    if (!light.drag) return;
    light.x = light.drag.ox + (event.clientX - light.drag.x);
    light.y = light.drag.oy + (event.clientY - light.drag.y);
    const img = $('lightbox-inner').firstElementChild;
    if (img) img.style.transform = 'translate(' + light.x + 'px, ' + light.y + 'px) scale(' + light.zoom + ')';
  });
  window.addEventListener('mouseup', () => { light.drag = null; });
  // 说明栏是"安全区"：点上面的按钮不该把整个大图关掉。
  $('lightbox-cap').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeLightbox(); return; }
    if ($('lightbox').hidden) return;
    // 焦点在输入框里的时候，空格和方向键归输入框。
    const tag = (event.target && event.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); toggleLightbox(); return; }
    if (event.key === 'ArrowRight') { event.preventDefault(); stepLightbox(1); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); stepLightbox(-1); }
    if (event.key === 'm' || event.key === 'M') { event.preventDefault(); setLightboxMode(light.mode === 'sheet' ? 'frames' : 'sheet'); }
    if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomLightbox(1.4); }
    if (event.key === '-' || event.key === '_') { event.preventDefault(); zoomLightbox(0.7); }
    if (event.key === '0') { event.preventDefault(); light.zoom = 1; light.x = 0; light.y = 0; renderLightbox(); }
  });
}

function bindChrome() {
  window.addEventListener('error', (event) => toast('\u00d7 ' + (event.message || '页面出错'), 'bad'));
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    toast('\u00d7 ' + (reason && reason.message ? reason.message : String(reason)), 'bad');
  });
  $('btn-refresh').addEventListener('click', () => { refreshEverything(); toast('已重新读取磁盘', 'ok'); });
  $('btn-archive').addEventListener('click', () => switchRoot('old'));
  $('btn-run-all').addEventListener('click', () => selectStage('all'));
  $('btn-stop').addEventListener('click', stopJob);
  $('btn-clear').addEventListener('click', () => clear($('log-lines')));

  let searchTimer = null;
  $('search').addEventListener('input', (event) => {
    state.q = event.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadRuns, 220);
  });

  $('log-head').addEventListener('click', (event) => {
    if (event.target.closest('button')) return;
    setLogCollapsed(!$('logbar').classList.contains('collapsed'));
  });

  bindLogResize();
  bindLightbox();
}

async function boot() {
  bindChrome();
  const saved = loadPref('stage', 1);
  state.stage = saved === 'all' ? 'all' : (Number(saved) || 1);
  const wanted = readURL();
  try {
    await loadState();
  } catch (err) {
    toast('\u00d7 读不到 /api/state：' + err.message, 'bad');
    return;
  }
  if (state.stage !== 'all' && !state.stages.some((item) => item.n === state.stage)) {
    state.stage = 1;
  }
  await loadRuns();
  if (wanted && findRun(wanted)) state.selected = wanted;
  if (state.selected) {
    state.selectedRun = findRun(state.selected);
    if (modeOf() === 'run') state.runPath = state.selected;
    else state.runPath = (state.selectedRun && state.selectedRun.run) || null;
    if (modeOf() === 'kept' && state.selectedRun) prefillStage2(state.selectedRun);
    await loadDetail();
  }
  syncURL();
  renderRunList(); renderRunHead(); renderStageTabs(); renderStageBody(); renderLog();
  if (state.shot) {
    /* ?shot=<项目内的相对路径> 直接把那张图打开。看着某一帧想发给别人时，
       地址栏里那一串就是这个用途。 */
    openLightbox({ path: state.shot, name: state.shot.split('/').pop() });
  }
  await poll();
  setInterval(poll, 1200);
}

boot();

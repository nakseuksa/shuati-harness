# P9-D 任务契约：双轨 Elo 看板拆分为独立同级页签

## 上下文

用户需求（2026-11-16）：整个「覆盖分」模块（即双轨 Elo 看板：覆盖分/实战分曲线、水分区、托管明细、账本解释）不再显示在「知识网 Elo」页签内，拆出为与「知识网」同级的独立页签。
现状勘察：
- `frontend/index.html`：nav 四页签（ingest/training/drill/graph），graph 页签文案「🕸️ 知识网 · Elo」；`view-graph` 内含 `view-head`（h1「三层知识网 · 双轨 Elo 看板」）、刷新按钮「🔄 刷新知识网与账本」（data-action="graph-refresh"）、`<div id="graph-root">` 与 `<div id="elo-root" style="margin-top:18px">`（~181 行）。
- `frontend/app.js`：`switchTab`（~396 行，graph 分支懒加载 `loadGraphAndElo`）；`loadGraphAndElo`（~1608 行，Promise.all 并发拉 /knowledge-graph 与 /elo/ledger，renderGraph+renderElo）；`renderElo`（~1743 行，挂 `#elo-root`，内含模块过滤 select）；委托 `case 'graph-refresh'`；`elo-module-select` change 监听（~1946 行，调 loadGraphAndElo）；init 的 marked 就绪重渲染（~2008 行，`state.tab==='graph'` 时 renderGraph+renderElo）。
- P9-C 刚交付红边折叠（renderGraph 内），不得破坏。

## 修改方案

### 1. `frontend/index.html`

1. nav：graph 页签文案改「🕸️ 知识网」；其后新增同级页签
   `<button class="tab-btn" role="tab" id="tab-elo" data-tab="elo" aria-selected="false" aria-controls="view-elo">📈 双轨 Elo</button>`；
   nav 的 aria-label「四大视图」改「五大视图」。
2. `view-graph`：h1 改「三层知识网看板」；sub 去掉覆盖分/实战分水分区表述（保留 L1/L2/L3 与 Compaction 可回滚描述）；刷新按钮文案改「🔄 刷新知识网」；**移除** `<div id="elo-root">`。
3. `view-graph` 之后新增：
   ```html
   <section class="view" id="view-elo" role="tabpanel" aria-labelledby="tab-elo" hidden>
     <div class="view-head">
       <h1>双轨 Elo 看板</h1>
       <p class="sub">按 L1 模块独立核算 · 覆盖分 / 实战分水分区可视化 · Escrow 待兑现明细 · 事件溯源账本解释</p>
     </div>
     <div class="btn-row" style="margin-bottom:16px">
       <button class="btn" type="button" data-action="elo-refresh">🔄 刷新 Elo 账本</button>
     </div>
     <div id="elo-root"></div>
   </section>
   ```

### 2. `frontend/app.js`

1. `state` 增加 `eloLoaded: false`（graphLoaded 旁）。
2. `loadGraphAndElo(btn)` 拆为两个独立函数（沿用既有 runTask/apiFetch 错误重试模式）：
   - `loadGraph(btn)`：仅 GET `/knowledge-graph` → normalizeGraph → `state.graphLoaded = true` → renderGraph()；
   - `loadElo(btn)`：仅 GET `/elo/ledger${state.eloModule ? ?l1_module=... : ''}` → normalizeElo → 既有 eloModuleOptions 填充逻辑 → `state.eloLoaded = true` → renderElo()。
   - 删除 loadGraphAndElo，**全文件 grep 确认零残留引用**。
3. `switchTab`：graph 分支改调 loadGraph()；新增 `if (name === 'elo' && !state.eloLoaded) { state.eloLoaded = true; loadElo(); }`。
4. 委托：`case 'graph-refresh'` 改调 loadGraph(el)；新增 `case 'elo-refresh': loadElo(el); break;`。
5. `elo-module-select` change 监听：改调 loadElo()（模块过滤不再连带刷新知识网）。
6. init marked 就绪重渲染：graph 分支仅 renderGraph()；新增 `if (state.tab === 'elo' && state.eloLoaded) renderElo();`。
7. `renderElo` 内部内容一字不动（`#elo-root` 选择器不变，元素已迁入新视图）；renderGraph/P9-C 折叠逻辑不动。

### 3. `frontend/styles.css`

- 预计无需改动（.view/.tab-btn/.view-head 均为通用类）；如新页签必需微调才允许改，汇报说明。

### 4. 明确不做

- 不改后端/API/normalizeElo/normalizeGraph；不改 Elo 卡片内容与交互（模块过滤/曲线/托管表/解释条）；不做 localStorage 持久化；不改其他页签。

## 验收

- `node --check frontend/app.js` 通过。
- 自查清单（交付汇报逐项确认）：五页签 aria 属性齐全且切换显隐正确；首次进 Elo 页签仅请求 /elo/ledger、首次进知识网页签仅请求 /knowledge-graph（懒加载各自独立）；两刷新按钮互不连带；模块过滤仅刷新 Elo；marked 就绪重渲染按当前页签各管各；loadGraphAndElo 零残留；elo-root 仅存在于 view-elo；P9-C 红边折叠行为不受影响。
- PM 将做代码审查 + 语法复验 + 本地服务端到端冒烟。

## 文件白名单

- `frontend/index.html`
- `frontend/app.js`
- `frontend/styles.css`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**任何网络调用（本地 127.0.0.1 冒烟由 PM 执行）。
契约与设计冲突时以 `docs/designs/adaptive_practice_harness.md` 为准并向 PM 汇报。

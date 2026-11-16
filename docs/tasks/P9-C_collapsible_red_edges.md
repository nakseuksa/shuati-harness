# P9-C 任务契约：知识网看板「跨考点标红边」列表折叠显示

## 上下文

用户需求（2026-11-16）：「知识网 Elo」页签的「⚡ 跨考点标红边」列表太长，希望可折叠显示。
现状：`frontend/app.js` `renderGraph()` 中红边卡片为 `<div class="card-title">⚡ 跨考点标红边 …</div><div class="edge-list">${edgesHtml}</div>`，全量平铺；`.edge-list { display: grid; gap: 8px; }`（styles.css ~931 行）。
应用模式：中央 click 委托 `switch (el.dataset.action)`（app.js ~1848 行）；全局 `state` 对象（~76 行）；看板刷新/页签切换会重新 `renderGraph()`。

## 修改方案

### 1. `frontend/app.js`

1. `state` 增加 `redEdgesCollapsed: true`（**默认折叠**，用户痛点即列表过长；仅内存态，不做 localStorage 持久化）。
2. `renderGraph()` 红边卡片改造：
   - 标题行改为 flex 布局：左侧原标题「⚡ 跨考点标红边」+ hint，右侧折叠开关按钮：
     `<button type="button" class="btn btn-sm btn-ghost edge-toggle" data-action="toggle-red-edges" aria-expanded="${!collapsed}" aria-controls="red-edge-list">${collapsed ? '▸ 展开' : '▾ 收起'}</button>`
     及统计徽章 `<span class="badge badge-gray">共 N 条 · 活跃 M 条</span>`（N=edgesSorted.length，M=active>0 的条数）。
   - 列表容器 `<div class="edge-list" id="red-edge-list" ${collapsed ? 'hidden' : ''}>`，内容 edgesHtml 不变。
   - **空列表（N=0）**：不渲染开关按钮与统计徽章，保留现有「暂无跨考点组合错题标红边」空态文案，行为与现状一致。
3. 中央委托新增 `case 'toggle-red-edges'`：翻转 `state.redEdgesCollapsed`，**原地更新 DOM**（不整卡重渲染，避免滚动跳动）：列表 `hidden` 属性、按钮 `aria-expanded`、按钮文案 ▸ 展开/▾ 收起。`renderGraph()` 重渲染时按 state 呈现折叠态（刷新/切页签后状态保持）。

### 2. `frontend/styles.css`

- 新增 `.edge-toggle` 必要样式（与既有 btn-ghost/btn-sm 协调，标题行 flex + gap + spacer 可复用既有工具类）。
- **必须**新增 `.edge-list[hidden] { display: none; }` —— 既有 `.edge-list { display: grid }` 会覆盖 UA 的 `[hidden]` 默认样式，漏掉此条折叠将失效。

### 3. 明确不做

- 不改后端/API/数据归一化（normalizeGraph 不动）；不改其他卡片（三层树/双指标/合并历史/Elo 曲线）。
- 不做分页/虚拟滚动/搜索过滤（超出需求）；不做 localStorage 持久化。

## 验收

- `node --check frontend/app.js` 通过。
- 自查清单（交付汇报中逐项确认）：默认折叠且统计徽章正确；点击展开/收起原地切换且 aria-expanded 同步；空列表无开关、空态文案不变；看板刷新/页签往返后折叠态保持；`.edge-list[hidden]` 样式存在。
- PM 将做代码审查 + 语法复验。

## 文件白名单

- `frontend/app.js`
- `frontend/styles.css`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**任何网络调用。
契约与设计冲突时以 `docs/designs/adaptive_practice_harness.md` 为准并向 PM 汇报。

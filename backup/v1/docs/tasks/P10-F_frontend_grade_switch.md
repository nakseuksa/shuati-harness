# P10-F 任务契约：前端学段学科全局切换 + Settings 弹窗

## 上下文

P10 分区最后切片（依赖 P10-E 已交付，API 形状以其契约为准）。用户六项裁决：①自由单字符串条目 ②**全局顶栏下拉框** ③**齿轮+弹窗 Settings** ④盲测配额按学段各自≤3（后端已实现）⑤有题目禁止删除条目（后端 409）⑥不加题库视图。

**API 契约（P10-E）**：
- `GET /api/v1/settings/grade-subjects` → `[{grade_subject, question_count, created_at}]`
- `POST /api/v1/settings/grade-subjects` body `{grade_subject}` → `{item, created}`；400 空名
- `PATCH /api/v1/settings/grade-subjects/{name}` body `{new_name}` → `{renamed, affected}`；400
- `DELETE /api/v1/settings/grade-subjects/{name}` → 204；**409** `{detail}`（有题目）
- `GET /api/v1/knowledge-graph?grade_subject=X` / `GET /api/v1/elo/ledger?grade_subject=X[&l1_module=Y]` / `GET /api/v1/drills/daily?grade_subject=X[&active_date=...]`（grade 必填，缺失 422）
- ingest `target_grade` Form 字段不变；sessions/drills POST/revert 不带 grade。

**现状勘察**：topbar-right 仅主题按钮；录入视图 `#grade-input` 自由文本（index.html ~114 行，app.js ~536/643 行读取）；`switchTab` 懒加载 graph/elo/drill；`loadGraph/loadElo`（P9-D 拆分）；`LS_SESSION` 续传横幅；中央 click 委托 + change 监听（elo-module-select 模式）。

## 修改方案

### 1. `frontend/index.html`

1. topbar-right（主题按钮左侧）新增：
   `<select id="grade-select" class="grade-select" aria-label="当前学段学科"></select>`
   `<button class="icon-btn" id="settings-btn" type="button" data-action="open-settings" title="学段学科设置" aria-label="打开学段学科设置">⚙️</button>`
2. 录入视图**移除** `#grade-input` 的 label+input 块（学段归属随全局选择）。
3. body 尾部新增 Settings 弹窗骨架：
   `<div class="modal-overlay" id="settings-modal" hidden role="dialog" aria-modal="true" aria-labelledby="settings-title">` 内含 card：标题「⚙️ 学段学科设置」、`<div id="settings-list">`、新增行（`<input id="settings-new-name" placeholder="如：小学奥数 / 高中物理-高二">` + `<button data-action="settings-add">添加</button>`）、关闭按钮 `data-action="close-settings"`。条目行由 app.js 渲染。

### 2. `frontend/app.js`

1. 常量 `LS_GRADE = 'shuati_grade_subject'`；state 增 `gradeSubject: ''`、`gradeOptions: []`、`sessionGrade: null`。
2. `loadGradeOptions()`：GET settings → state.gradeOptions → 填充 `#grade-select`（option value/text 均条目名）；恢复 LS_GRADE（∈列表则用，否则取首项，无条目则 ''）→ state.gradeSubject + 写 LS + select.value 同步。
3. `init()`：**先 await loadGradeOptions()** 再走既有页签懒加载（保证 grade 就绪）；无条目时各 scoped 视图渲染引导文案「请先在 ⚙️ 设置中创建学段学科」。
4. `changeGrade(g)`：写 state+LS；**重置全部 scoped 状态**：graph=null/graphLoaded=false、elo=null/eloLoaded=false/eloModule=''、drill 重置（items/outcomes/loaded=false）；stopPrewarmPoll()；若 `state.session && state.sessionGrade !== g` → state.session=null/feedback=null（服务端会话不动，切回可续）；renderTraining()；按当前页签重新懒加载（graph/elo/drill）；renderResumeBanner()。
5. `#grade-select` change 监听（并入既有 document change 处理器）→ changeGrade(value)。
6. `loadGraph/loadElo/loadDrill`：URL 追加 `grade_subject=${encodeURIComponent(state.gradeSubject)}`；`!state.gradeSubject` 时跳过请求渲染引导文案。
7. `submitIngest`：移除 `#grade-input` 读取；`grade = state.gradeSubject`；为空 → toast 提示并 openSettings()，不提交；`form.append('target_grade', grade)`。
8. **LS_SESSION  grade 感知**：写入处（~785 行）payload 增 `grade: state.gradeSubject`；`renderResumeBanner` 仅当 payload.grade === state.gradeSubject 时显示（**legacy payload 无 grade 字段 → 兼容显示**）；resume 时 state.sessionGrade = payload.grade ?? state.gradeSubject。submitIngest/confirmGate 建立会话时 state.sessionGrade = state.gradeSubject。
9. Settings 弹窗：
   - `openSettings()`：GET settings 刷新 → renderSettingsList() → modal.hidden=false；`closeSettings()`：hidden=true；overlay 自身点击关闭（e.target===overlay）。
   - `renderSettingsList()`：每条目一行：名称 + `badge` 题目计数 + ✏️ 重命名（data-action="settings-rename" data-name）+ 🗑 删除（data-action="settings-delete" data-name，count>0 时仍渲染，点击后由 409 toast 解释）；空列表引导文案。
   - settings-add：读 `#settings-new-name` → POST → 成功 toast + 清空输入 + 刷新列表与下拉框（新条目不自动切换激活）；400 toast detail。
   - settings-rename：`window.prompt('新名称', old)` → PATCH → 成功：若 old===state.gradeSubject → changeGrade(new)（changeGrade 内写 LS）；刷新列表/下拉框；toast affected 摘要；400 toast。
   - settings-delete：`window.confirm` → DELETE → 204：刷新列表/下拉框；若删的是激活项 → changeGrade(剩余首项或 '')；**409：toast 后端 detail（含题目计数），不关闭弹窗**。
10. 委托新增 cases：open-settings/close-settings/settings-add/settings-rename/settings-delete。
11. P9-C 折叠 / P9-D 页签拆分 / renderElo 内容 / marked 就绪重渲染逻辑**不动**。

### 3. `frontend/styles.css`

- `.grade-select`（顶栏紧凑下拉，复用既有变量）、`.modal-overlay`（fixed 全屏半透明遮罩 + 居中）、`.modal-card`（复用 card 风格，max-width 520px，max-height 80vh overflow auto）、`.settings-row`（flex 名称+计数+操作按钮）、新增行样式。深浅主题均适配（用既有 CSS 变量）。

### 4. 明确不做

- 不加题库视图（裁决⑥）；不做条目拖拽排序/导入导出；不改后端；不做多浏览器同步（激活项 localStorage  per-browser）。

## 验收

- `node --check frontend/app.js` 通过。
- 离线 vm 冒烟（stub DOM/fetch）：changeGrade 重置 scoped 状态与 LS 写入；settings 渲染/增删改分支；无条目引导；legacy LS_SESSION 兼容。
- 自查清单逐项汇报：顶栏下拉框+齿轮存在；录入页无 grade-input；切换学段后 graph/elo/drill 请求均带 grade_subject 且数据重置；Settings 增/改名/删（409 toast）全链；激活项改名/删除后状态正确；续传横幅 grade 感知；P9-C/D 行为不受影响。
- PM 将做代码审查 + 语法复验 + 真实服务冒烟（重启服务触发真实库迁移后）。

## 文件白名单

- `frontend/index.html`
- `frontend/app.js`
- `frontend/styles.css`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**任何网络调用（本地冒烟由 PM 执行）。
契约与设计冲突时以设计文档为准并向 PM 汇报。

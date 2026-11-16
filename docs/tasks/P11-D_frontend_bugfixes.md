# P11-D 任务契约：前端 Bug 修复（盲测核验交互对齐 / 重训轮询 / 学段改名会话保持）

## 上下文

Review 发现 `frontend/app.js` 三处前端 Bug：
1. **Bug 1（前端部分）**：`drillCardHtml` 中，当 `item.requires_intermediate === true`（原题回退）且 `verification_mode === 'equation_skeleton'`（$L \ge 2$）时，前端将最终答案输入框标签错误渲染为「核心方程组（无需化简）」与 `多个方程用 ; 或换行分隔`，且 `allowed_symbols` 符号芯片绑定到了 `drill-fin-${uid}` 而不是中间关系式 `drill-mid-${uid}`。实际上原题回退时，中间框 `drill-mid-${uid}` 核验的是曾卡壳步的关系式，底框 `drill-fin-${uid}` 核验的是原题最终答案 `standard_answer_latex`。
2. **Bug 2**：`openRetrainSession` 调用了未定义的 `startPrewarmPoll()`，在重训会话处于 `IDLE` / `RUNNING` 时必抛 `ReferenceError: startPrewarmPoll is not defined`。
3. **Bug 6（前端部分）**：`renameGradeSubject` 在把当前激活学段 `old` 改为 `renamed` 后直接调用 `changeGrade(renamed)`，但未先同步更新 `state.sessionGrade`、`state.gateGrade` 与 `localStorage[LS_SESSION].grade`，导致 `changeGrade` 误判当前训练会话属于旧学段而将其清空，断点续传横幅也同时消失。

## 修改方案

### `frontend/app.js`

1. **`drillCardHtml(item, idx)`**：
   - `const isEq = item.verification_mode === 'equation_skeleton';`
   - `const isVariantEq = isEq && !item.requires_intermediate;`（仅变式呈现在核心列式模式下，主输入框为方程组骨架；原题回退时，中间框为卡壳步关系式，主输入框恒为「最终答案」）。
   - 顶部角标：
     - `item.requires_intermediate`：`✍️ 双重核验（中间式 + 最终答案）`；
     - 否则 `isEq ? '🧮 核心列式核验' : '✍️ 最终答案核验'`。
   - 若 `item.requires_intermediate && isEq && item.allowed_symbols.length`：将 `allowed_symbols` 符号芯片组渲染在 `drill-mid-${esc(uid)}` 上方，且 `data-target="drill-mid-${esc(uid)}"`。
   - 主输入框（`drill-fin-${esc(uid)}`）：
     - 仅当 `isVariantEq` 时渲染「核心方程组（无需化简）」与 `textarea` + 绑定到 `drill-fin-${esc(uid)}` 的 `allowed_symbols` 芯片；
     - 其余情形（包括所有 `item.requires_intermediate === true`）渲染「最终答案」与 `<input type="text" id="drill-fin-${esc(uid)}" placeholder="输入最终答案（数值 / 表达式）" />`。
2. **`openRetrainSession(btn, sessionId)`**：
   - 删除未定义的 `startPrewarmPoll()` 调用；`switchTab('training')` 内已调用 `renderTraining()` → `syncPrewarmPoll()`，在 `switchTab('training')` 之后再调用 `syncPrewarmPoll()` 即可确保幂等按需启动轮询。
3. **`renameGradeSubject(btn, old)` 与 `persistSession()`**：
   - 在 `renameGradeSubject` 成功获取 `renamed` 后（调用 `changeGrade` 之前）：
     - 若 `state.gateGrade === old`，更新 `state.gateGrade = renamed`；
     - 若 `state.sessionGrade === old`，更新 `state.sessionGrade = renamed`；
     - 读取 `loadPersistedSession()`，若其 `grade === old`，同步更新 `localStorage[LS_SESSION]` 中的 `grade` 为 `renamed`（`try/catch` 保护）。
   - 在 `persistSession()` 中将 `grade` 字段改为 `state.sessionGrade || state.gradeSubject`（与当前会话实际所属学段保持一致）。

## 文件白名单

- `frontend/app.js`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**网络调用。

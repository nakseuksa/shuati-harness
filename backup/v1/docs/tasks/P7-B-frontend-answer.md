# P7-B 任务契约：录入答题区与掌握判定（前端）

## 上下文
项目：数理自适应刷题 Harness。用户需求：录入页「题干」下方增加文字答题区 + 「已掌握」选项；勾选/回答「已掌握」/填入正确答案 → 直接入库跳过练习。后端 P7-A 并行开发，按下方锁定契约对接。
必读：docs/tasks/P7-A-ingest-answer.md 的「锁定接口契约」节；现有 frontend/app.js 录入页实现（双槽 paste、submit 构造 multipart、GateCard 渲染）。

## 锁定接口（与 P7-A 一致，不得更改字段名）
- POST /api/v1/questions/ingest multipart 新增：`user_answer`（字符串）、`mastered_claim`（"true"/"false"）
- GateCardDTO 新增：`auto_mastered: bool`、`mastery_explanation: str|null`

## 实现规格
1. **答题区 UI**（题干槽正下方，草稿槽之前）：
   - textarea#ingest-answer：placeholder「在此输入你的答案（可选）。填入正确答案或勾选下方选项，将按已掌握直接入库。」
   - checkbox#mastered-check + label「✅ 已掌握此题（跳过练习，直接入库并提取知识点）」
   - 视觉与现有稿纸网格主题一致；答题区折叠可选（默认展开）。
2. **提交**：multipart 追加 user_answer（textarea 值 trim）、mastered_claim（checkbox ? "true" : "false"）。旧 initial_state 开关移除，统一恒发 initial_state="错题"，掌握语义由 mastered_claim 承载（避免双控件歧义）。
3. **响应分流**：
   - `auto_mastered === true` → 渲染「已掌握入库」成功卡：✅ 标题 + mastery_explanation（Elo 解释条，renderRich）+ base_elo/tier 徽章 + 「继续录入下一题」按钮（清空表单）；**不渲染门禁卡、不切训练页签**。
   - 否则 → 现有 GateCard 门禁流程完全不变。
4. 交互细节：勾选「已掌握」时 textarea 禁用并置灰（语义互斥提示）；取消勾选恢复。提交按钮加载态/错误重试条沿用现有 apiFetch 封装。
5. 断点续传/暂存逻辑不受影响（auto_mastered 会话无训练进度，不写 LS_SESSION）。

## 你拥有的文件
- `frontend/index.html`、`frontend/app.js`、`frontend/styles.css`

**禁止**触碰 backend/ 与 tests/（P7-A 并行所有）。**禁止** git commit/push。**禁止任何网络调用**（node --check 与 vm 冒烟即可，真实联调由 PM 验收）。

## 验收
- `node --check frontend/app.js` 通过
- vm 冒烟：multipart 构造含新字段；auto_mastered 分流逻辑（true→成功卡路径 / false→门禁路径）；勾选禁用联动
- 最终汇报：文件改动、交互覆盖清单、冒烟结果

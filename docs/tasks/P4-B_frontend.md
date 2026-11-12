# P4-B 任务契约：本地 Web 工作台（零构建 Vanilla ES Modules）

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 4 的前端工作台：四大视图页签，覆盖 CUJ 1~4 的全部交互。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **Target File #14**、**CUJ 1~4**、§4 API 契约表、NFR Token 经济学（cas:// 重写）。设计文档是唯一权威规格。
后端 API 以 docs/tasks/P4-A_api.md 的端点契约为准（P4-A 并行开发中，按契约对接，勿改后端）。

## 你拥有的文件
- `frontend/index.html`
- `frontend/app.js`
- `frontend/styles.css`

**禁止**触碰其他文件。**禁止** git commit/push。

## 技术约束
- 零构建：原生 ES Modules + CDN（KaTeX 渲染公式、marked 渲染 Markdown），无 npm/打包器
- `node --check frontend/app.js` 语法必须通过（用系统 node 或 .venv 无关）
- 所有 API 调用经统一 fetch 封装：错误显示重试条（设计 Risks 表要求），502 提示「模型网关暂时不可用，点击重试」
- Markdown 渲染前正则重写 `cas://([a-f0-9]{64})` → `/assets/$1`；公式 `$...$`/`$$...$$` 交 KaTeX

## 四页签规格
### ① 录入（CUJ 1）
- 双槽分区：`[题干与配图槽]` / `[手写草稿截图槽]`，各自拦截 paste 事件（clipboardData.items 图片）与文件选择；槽内缩略图预览，严禁混槽
- target_grade 输入、initial_state 切换（错题/已掌握）；提交 → POST /api/v1/questions/ingest（multipart）
- **步骤对齐微确认条**：渲染 GateCardDTO——每步一行 `Step N [目标]: ✅已独立完成 / ❌思路中断 / ⬜未触及`；保底水位展示；**回车键一键确认**；卡点步骤可点击微调；`思路断层 ↔ 审题/计算粗心` 一键切换按钮 → POST confirm-gate → 自动切到训练页签
### ② 训练（CUJ 2/3）
- 按 SessionStepResponse.micro_stage 渲染：STAGE_1 遮罩视图（仅高亮 masked_conditions + probe_question_md，隐藏原题目标）；STAGE_2 微母题题干；STAGE_3 原题完整题干；STAGE_4 变式（equation_skeleton 模式显示 allowed_symbols 符号芯片 + 方程输入区；final_answer 模式普通输入）
- prewarm_status=READY_FALLBACK → 显示 `⚠️ 未经符号沙盒强校验` 徽章 + 跳过/换题按钮（换题=重新提交触发热备）
- 作答提交 → POST step-submit；explanation_text 渲染为加分解释条；STAGE_COOLDOWN_DEFERRED 显示骨架对比卡与次日提示；「暂存进度」按钮=保留会话直接回首页（断点续传）
### ③ 每日盲测（CUJ 3）
- 首页独立入口 `🎯 每日综合盲测（Due Drill）`；GET /api/v1/drills/daily 渲染题单（≤3，无来源/知识点标签）
- requires_intermediate=true → 同时渲染 probe_question_md 中间量输入 + 最终答案输入；提交 → POST → 结果视图（pass/fail、连带销账列表、重训提示）
### ④ 知识网与 Elo 看板（CUJ 4）
- GET /api/v1/knowledge-graph：三层树（L1→L2→L3，节点状态色 🔴/🟡/🟢）、跨考点标红边列表（active/historical）、各模块版图攻克率 + 收敛度仪表
- 合并历史面板：批次列表 + `[一键解绑回滚 (Un-merge)]` → POST revert → 刷新视图
- GET /api/v1/elo/ledger：模块双轨 Elo 曲线（原生 SVG 折线，覆盖分/实战分双线 + 水分区着色）、托管池明细表、算分解释流水

## 验收
- `node --check` 通过；四页签 DOM 结构与 API 对接完整（P4-C E2E 与人工走查验证行为）
- 样式：深色/浅色不强制，简洁工具风；移动端基本可用（响应式栅格）
- 最终消息汇报：文件、行数、交互覆盖清单（对照 CUJ 逐条）

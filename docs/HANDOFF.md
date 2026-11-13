# 项目状态与交接备忘录（HANDOFF）

**更新**: 2026-11-13 会话结束
**用途**: 新会话记忆入口。开始工作前先读本文件。

## 一、项目概况

- **名称**: 数理自适应刷题 Harness（刷题 harness）
- **定位**: 面向数学/物理的 Local-First 单机 Web 自适应刷题与验证框架，零预设考纲冷启动
- **架构**: FastAPI + 原生 ES Modules 前端（KaTeX，零构建）+ SQLite(WAL) + SHA-256 CAS 资产库 + 受限 Python/SymPy 子进程沙盒 + OpenAI 兼容 LLM 网关
- **双层五模块**: 微观单题闭环（录入诊断 / 三阶脚手架 / 拆拐杖盲测）+ 宏观全局导航（三层知识网并查集 / 分模块双轨 Elo）
- **仓库**: https://github.com/nakseuksa/shuati-harness（公开）
- **权威文档**: `docs/designs/adaptive_practice_harness.md`（设计）、`理论基础.md`（理论）、`docs/dev_plan.md`（计划+编排日志）、`docs/tasks/P*-*.md`（worker 契约）

## 二、当前状态（截至 2026-11-13）

- 设计全部功能已交付（M1~M4）+ 交付后 6 轮增强/修复（见第七节提交链）
- 最新 commit: `5e74337`；**828 测试全绿，覆盖率 97.24%**，ruff/mypy 零告警
- 服务由用户以开发模式自管: `uvicorn --reload`（127.0.0.1:8000）；若未运行需手动启动
- **真实网关已配置**（`.env`，已 gitignore）:
  - `LLM_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`（来源: pi `models.json` 真实 provider 配置，非代理）
  - `VLM_MODEL_NAME=qwen4-max-preview`（视觉，手写转录）
  - `SOLVER_LLM_MODEL_NAME=deepseek-v4.1-flash-1017`（双盲求解器）
  - `LLM_TIMEOUT_SECONDS=300`（推理模型需长思考窗口；60s 会 ReadTimeout）
  - `USE_MOCK_LLM=false`；改 `true` 即全离线闭环
- venv: `.venv`（Python 3.14.6）；依赖 `pip install -e ".[dev]"`

## 三、常用命令

```bash
cd /Users/wanghaichen/0_work/刷题harness
# 启动（开发模式，用户偏好前台 --reload）
.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
# 后台方式
nohup .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 > /tmp/harness_web.log 2>&1 & echo $! > /tmp/harness_web.pid
# 停止: kill $(lsof -ti :8000)
# 测试 / 质量门禁
.venv/bin/python -m pytest -q --cov=backend --cov-fail-under=85
.venv/bin/ruff check backend/ tests/ && .venv/bin/mypy backend/
```

- 服务日志: `/tmp/harness_web.log`；**行为审计日志**: `data/logs/harness.jsonl`（JSONL，5MB 旋转×5，request_id 贯穿，30+ 事件，密钥自动脱敏）
- 数据库: `data/harness.db`（WAL）；备份: `data/harness.db.bak-*`（删除操作产生两份）
- 审计: `grep <session_id|question_uid|request_id> data/logs/harness.jsonl`

## 四、协作约定（用户明确要求，必须遵守）

1. **分工**: 所有代码修改委派 subagent worker；主会话（PM）只负责根因分析、解决方案设计、最终验收（独立复跑门禁 + 真实用例验证 + 提交交付）
2. **设计文档是唯一权威**；实现中有问题向用户确认，不自作主张
3. **URL 纪律**: 工具 URL 必须与当前任务目标一致；严禁擅自替换为 routify 等临时代理；不盲从历史调用地址；每次调用重新核对目标与来源；必要调用照常执行，不遗漏、不无故增加
4. **subagents 需要 tmux**: pi 不在 tmux 内时派发失败；重启命令 `tmux new -A -s pi 'pi --session <id>'`
5. **Worker 编排模式**: 契约放 `docs/tasks/P*-*.md`（上下文/文件白名单/API 契约/测试要求/门禁命令）；worker 禁止 git commit/push 与网络调用；PM 验收后统一提交；并行时文件所有权互斥；契约与设计冲突以设计为准并汇报；测试期望随批准的行为演进更新（需 PM 明确授权）

## 五、关键技术决策

- **难度合成**（设计文档版，与旧 exercise_grading 仓库不同）: `base_elo = min(2800, max(800, 750+85·(2^C−1)+105S+115T+65L))` + 8 档阶梯；C≤2 封顶 L4-A
- **等价引擎红线**: 自由符号守卫（纯数字 5≠7、5≠-5）；复合数量在归一化前权威解析（9小时36分=576分；`\text` 剥离会拼接成 "936"，必须拒绝）；单位豁免（15米≡15，但 15*m 代数语境不剥离）；量纲感知拉丁后缀
- **掌握判定（P7）**: 勾选 / 「已掌握」文本 / 正确答案 → 录入单事务即结算（MASTERED_INIT + MASTERED + SESSION_COMPLETED），跳过练习；判定异常不击穿录入
- **递归降阶**: STAGE_2「不会/请求铺垫」→ 降一层（单调性硬锁保证 elo 严格递减；深度上限 4 / 原子题 / 800 触底 → 标准答案演示卡）；答对沿 SCAFFOLD_PRE 链升回原题
- **question_uid 内容确定性**（UUIDv5）: 同内容重录复活同 uid（幂等设计，不是 bug）
- **数据删除流程**: 备份（sqlite backup API）→ 单事务 FK 顺序删除（先子题后主题）→ 标红边 JSON 清理 → 孤儿节点判定（无步骤/审计/重定向/边引用）→ 账本重基（如有事件）→ FK + 残留验证
- **Elo**: 事件溯源（无可变标量表）、Escrow 100% 冻结零预支、守恒 floor+escrow≡target、水位线高斯门控、衰减只碰托管池
- **知识网**: 同域可逆并查集（禁跨 L1/L2）、回滚逐字段恢复且与 Elo 正交

## 六、已知局限与待办

- [ ] 重入库卡 `auto_mastered` 恒 False（零漂移决策）；如需回显掌握态属后续增量
- [ ] Compaction 同秒平局以 uuid 决序（测试钉住时间戳）；方向不确定但无害
- [ ] 多方程「消去中间变量比对解集」完全体延后（现为切分+逐方程等价）
- [ ] 题目 `a01b47d7` 题干含「答：9小时36分」（用户粘贴时答案泄漏进题干，数据质量问题，未清理）
- [ ] 真实 VLM 转录/诊断质量待校准：建议 3~5 道真实错题（含真实手写照片）走查 CUJ1~3
- [ ] 前端「跳过」降级题 `self_assessment='SKIP'` 后端未识别（按错答停留，不击穿）
- [ ] Future（设计 Future Improvements）: AnkiConnect 防坑闪卡同步、KST 日志校准（>300 题后）

## 七、今日提交链（2026-11-13）

`a733a58` M1 基座 → `44466d3` M2 引擎 → `fa62e21` M3 调度 → `34e73ae` docs → `09b551b` M4 交付 → `3428653` 网关超时 → `ba6e244` 二阶死循环修复 → `b1305ed` 递归降阶 → `af7621f` 单位修复+行为日志 → `a243088` 复合数量 → `5e74337` 录入答题区

## 八、会话参考

- 本次会话 pi ID: `01a1d003-9f43-7779-a79e-96c51d27e7f8`
- 转录: `~/.pi/agent/sessions/--Users-wanghaichen--/2026-11-12T13-47-40-740Z_01a1d003-9f43-7779-a79e-96c51d27e7f8.jsonl`
- 恢复上下文: `pi --session 01a1d003`（subagents 需 tmux 环境）

# P4-A 任务契约：FastAPI 入口与全量 REST 路由

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 4 的服务暴露层：把已验收的全部服务组装为设计 §4 的 7 个 API 端点 + CAS 静态路由 + 前端挂载。路由保持轻薄，业务逻辑一律委托服务层。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§4 核心内部 API 契约表**、Target File **#13**、CUJ 1~4（交互时序）。设计文档是唯一权威规格。
前置服务（全部已验收或收口中）：ingest_service / scaffold_engine / blind_drill_scheduler / knowledge_graph / elo_engine / cas_store / schema。

## 你拥有的文件
- `backend/main.py`
- `tests/test_api_routes.py`

**禁止**触碰其他文件（服务层 bug 一律汇报 PM，不得自行修改）。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_api_routes.py -v` + ruff/mypy 零告警 + 全量无回归
- 测试用 httpx.AsyncClient(transport=ASGITransport(app)) 或 TestClient；USE_MOCK_LLM=True；monkeypatch settings.SQLITE_PATH/CAS_ASSETS_DIR 到 tmp_path；零真实网络

## 端点契约（设计 §4 逐条）
| 端点 | 方法 | 实现 |
| :--- | :--- | :--- |
| `/api/v1/questions/ingest` | POST | multipart：`stem_md`(Form)、`target_grade`(Form)、`initial_state`(Form, 默认'错题')、`stem_images`/`draft_images`(UploadFile 列表可空)；上传文件落 tmp 目录后调 `ingest_question`；返回 GateCardDTO |
| `/api/v1/sessions/{session_id}/confirm-gate` | POST | body `{stalled_step_index:int, error_category:str}` → `confirm_diagnostic_gate`；未知 session → 404；非法 category → 422 |
| `/api/v1/sessions/{session_id}/step-submit` | POST | body `{user_answer:str, self_assessment:str\|null}` → `submit_stage_answer`；非法阶段 → 400/409（ValueError 映射） |
| `/api/v1/drills/daily` | GET | query `active_date`(可选 ISO date，缺省 today) → `get_daily_blind_drill_queue` |
| `/api/v1/drills/daily` | POST | body `{question_uid, final_answer, intermediate_sub_answer?, active_date?}` → `evaluate_blind_drill_submission` |
| `/api/v1/knowledge-graph` | GET | 组装：resolve 后的活跃三层节点树 + 标红边（含 active/historical 计数）+ 各 L1 模块 GraphMetricsDTO + 合并历史批次列表（compaction_audit_ledger，含 is_reverted） |
| `/api/v1/knowledge-graph/compaction/{batch_id}/revert` | POST | `revert_compaction_batch`；未知/已回滚批次 → 404/409 |
| `/api/v1/elo/ledger` | GET | query `l1_module`(可选)：各模块当前 DualEloState + 事件流水（含 explanation_text）+ 待兑现托管池明细 + 历史曲线点（event_id, cov, combat） |
| `/assets/{cas_hash}` | GET | `resolve_cas_uri(f"cas://{hash}")` → FileResponse(media_type='image/webp')；非法 hash → 400，缺失 → 404 |
| `/` 静态 | - | `app.mount("/", StaticFiles(directory="frontend", html=True))`（frontend 目录由 P4-B 并行创建；测试期目录可能为空，挂载不得崩溃） |

## 实现规格
1. lifespan：启动时 `init_db()`；DB 访问统一 `get_db_connection()` 上下文。
2. Pydantic 请求/响应模型集中定义在本文件；服务层 DTO 直接复用。
3. 异常映射：ValueError→400、KeyError/LookupError→404、LLMGatewayError→502（含重试提示文案）；统一 JSON 错误体 `{detail}`。
4. CORS 不需要（同源本地）；不加鉴权（NG2 单机）。
5. `active_date` 注入贯穿 drills 端点（E2E 时间旅行依赖）。

## 测试要求
- ingest：multipart 全链路（Pillow 合成图）→ 200 + gate 卡；重入库幂等
- confirm-gate 三分支（MASTERED/CONCEPT/TRAP）→ 状态与解释条；404/422
- step-submit：一阶→二阶→三阶→四阶全链路（Mock）；非法阶段错误码
- drills：GET 幂等题单 + POST 销账（变式/原题双核验）；active_date 时间旅行
- knowledge-graph：节点树/标红边/指标/批次列表结构断言；revert 往返
- elo/ledger：模块过滤、曲线点、托管明细、解释条
- assets：合法 hash 200 + image/webp；非法 400；缺失 404
- 前端目录缺失/为空时应用仍可启动

## 完成标准
门禁全绿 + 全量无回归。最终消息汇报：文件、测试数、门禁摘要、端点覆盖清单。

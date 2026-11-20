# P9-E 任务契约：静态前端脚本/样式版本号查询参数（缓存击穿）

## 上下文

用户报告 P9-C 折叠开关「看不到」，PM 排查证实服务端代码/数据/渲染全正常（真实数据 vm 验证 7/7），根因为浏览器缓存旧 `app.js`：静态响应仅 ETag/Last-Modified、无 Cache-Control，浏览器启发式缓存。用户裁决（2026-11-16）：**给静态挂载加脚本版本号查询参数**（而非 Cache-Control 头方案）。

现状：`backend/main.py` `create_app(frontend_dir)` 末尾 `app.mount("/", StaticFiles(directory=frontend, html=True))`（目录缺失容错）；`frontend/index.html` 本地引用仅两处：`href="./styles.css"`（~13 行）、`src="./app.js"`（~214 行）；KaTeX 为 CDN 锁版本（0.16.11），不涉及。

## 修改方案（仅 `backend/main.py`）

1. 在静态挂载**之前**注册显式根路由（仅当 `frontend.is_dir()` 且 `(frontend / "index.html").is_file()` 时）：
   ```python
   @app.get("/", response_class=HTMLResponse)
   def serve_index() -> HTMLResponse: ...
   ```
   - 读取 `frontend/index.html` 文本；
   - 版本号 = 对应文件**内容 SHA-256 前 12 位 hex**（`hashlib.sha256(path.read_bytes()).hexdigest()[:12]`），`app.js` 与 `styles.css` 各自独立计算；每次请求时计算（本地单机、文件小，无需缓存层）；
   - 精确字符串替换注入：`href="./styles.css"` → `href="./styles.css?v={css_v}"`；`src="./app.js"` → `src="./app.js?v={js_v}"`；
   - **容错**：某资源文件缺失/读取异常 → 该引用保持原样不注入（不崩溃，与挂载容错语义一致）；index.html 读取失败 → 抛 404（HTTPException）；
   - 内容哈希确定性：内容不变 → URL 不变（缓存友好）；内容一变 → URL 变（自动击穿）。
2. `frontend/index.html` **不改**（服务端注入保持零构建，且 file:// 直开仍可用）。
3. 挂载与其他路由一律不动；根路由路径精确为 `/`，不得遮蔽 `/api/v1/*`、`/assets/*`（FastAPI 路由先于 mount 匹配，注册顺序保持「API → assets → 根路由 → mount」）。
4. `HTMLResponse` 从 `fastapi.responses` 导入；遵循既有代码风格与类型注解（mypy 严格）。

## 测试（`tests/test_api_routes.py`）

1. `GET /` → 200，HTML 含 `app.js?v=<12hex>` 且等于 `sha256(frontend/app.js 字节)[:12]`；`styles.css?v=` 同理（期望值从真实文件内容推导，不得硬编码哈希）。
2. 用 `create_app(frontend_dir=tmp_path 复制目录)`：修改副本 `app.js` 内容 → 新版本号变化且等于新内容哈希；`styles.css` 版本号不受影响（独立）。
3. 副本中删除 `app.js` → `GET /` 仍 200，`app.js` 引用保持无版本号原样（容错）。
4. 既有根页面/静态资源/API 测试全部不受影响（含 frontend 目录缺失容错用例）。

## 文件白名单

- `backend/main.py`
- `tests/test_api_routes.py`

**禁止**修改白名单之外任何文件（含 frontend/*）；**禁止** git commit/push；**禁止**任何网络调用。
允许本地 targeted pytest / ruff / mypy 自检；PM 独立复跑全量门禁 + 真实服务冒烟验收。
契约与设计冲突时以 `docs/designs/adaptive_practice_harness.md` 为准并向 PM 汇报。

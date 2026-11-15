# 任务契约 P8-3A：前端/CAS/上传安全边界 + VLM 诊断校准与大图优化

- **所属阶段**：Phase 8（Review 修复 · 批次 3A）
- **权威设计**：`docs/designs/adaptive_practice_harness.md` §3.1 第 1/3/4 条、Target File #5/#8/#11
- **允许修改的文件白名单**：
  - `backend/config.py`
  - `backend/main.py`
  - `backend/services/cas_store.py`
  - `backend/services/vlm_diagnosis.py`
  - `frontend/app.js`
  - `tests/test_api_routes.py`
  - `tests/test_cas_store.py`
  - `tests/test_vlm_diagnosis.py`

## 修复目标

1. **`frontend/app.js`（XSS / URI 注入防护）**：
   - 新增 `isSafeUrl(url, { allowDataImage })`：拒绝 `javascript:` / `vbscript:` / `data:text/html` / `data:image/svg+xml` 及未知危险协议；仅放行 `/...`、#锚点、`http:`、`https:`、`mailto:`（非图片）及图片允许的 `data:image/{png,jpeg,webp,gif,bmp};base64,...`；
   - 在 `miniMarkdown` 渲染链接与图片前检验 URL；在 `markdownToHtml` 返回后（`renderMath` 注入 KaTeX 前）调用 `sanitizeHtml(html)`，移除 `<script>/<style>/<iframe>/<object>/<embed>/<form>/<input>/<button>/<textarea>/<select>/<svg>/<math>`、所有 `on*` 事件属性与危险 URI。
2. **`backend/services/cas_store.py`（路径穿越与解压炸弹防护）**：
   - `_rewrite_local_images` 对相对路径计算 `candidate.resolve()` 并验证 `is_relative_to(base_dir.resolve())`，越界路径（如 `../../.env` 或指到外部的符号链接）一律原样保留而不读取；
   - `ingest_image_bytes` 捕获 `Image.DecompressionBombError` / 超大像素异常并收敛为 `ValueError`。
3. **`backend/config.py` & `backend/main.py`（上传体大小上限）**：
   - `Settings.MAX_UPLOAD_IMAGE_BYTES: int = 20 * 1024 * 1024`（可通过 `settings` / 环境变量配置）；
   - `_save_uploads` 分块流式读取上传字节，超限即抛 `ValueError`（路由收敛 HTTP 400）并清理临时文件。
4. **`backend/services/vlm_diagnosis.py`（替代分支校验 + 陷阱合法性 + 大图缩放）**：
   - `_postprocess` 先完成 `matched_branch_id` / `alt_branch_steps` 回退校验，再基于生效分支步骤数（替代分支或 `trace.steps`）钳制 `stalled_step_index` 与 `last_valid_step_index`，并校验 `alt_branch_steps` 各步四维子分及 `trap_taxonomy_tag in TRAP_TAXONOMY`；
   - `_build_messages` 对超边长（`> settings.CAS_MAX_IMAGE_SIDE`）或非 WebP 的图片做等比缩放 + WebP 转码后再生成 `data:image/webp;base64,...`；已是合尺寸 WebP 时原样走 `image_path_to_data_url`，非可解码图字节亦平滑回退。

## 验收测试

- `tests/test_cas_store.py`：新增 `../` 越界 Markdown 本地路径不触发 CAS 入库；
- `tests/test_api_routes.py`：新增超限上传返回 HTTP 400；
- `tests/test_vlm_diagnosis.py`：新增 `user_alt_1` 1 步分支下 `stalled_step_index` 钳制至 1、`alt_branch_steps` 越界子分拒绝、非法 `trap_taxonomy_tag` 拒绝、大图 PNG 发送前转码缩放且小 WebP 字节不变。

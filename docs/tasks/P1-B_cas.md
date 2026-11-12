# P1-B 任务契约：CAS 内容寻址资产库

## 上下文
项目：数理自适应刷题 Harness（Local-First）。你负责 Phase 1 的图片归档与 Markdown 规范化服务。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.1 第 1 条（分区剪贴板导入与 SHA-256 CAS 归档）** 与 Target File **#5** 条目。设计文档是唯一权威规格。

## 你拥有的文件（只允许创建/修改这些）
- `backend/services/cas_store.py`
- `tests/test_cas_store.py`

**禁止**触碰其他任何文件。**禁止** git commit/push。

## 环境
- 项目根目录：`/Users/wanghaichen/0_work/刷题harness`
- Python 一律用 `.venv/bin/python`
- 测试命令：`.venv/bin/python -m pytest tests/test_cas_store.py -v`
- 质量门禁：`.venv/bin/ruff check backend/services/cas_store.py tests/test_cas_store.py` 与 `.venv/bin/mypy backend/services/cas_store.py` 零告警
- conftest 已提供 `cas_dir` 夹具（把 settings.CAS_ASSETS_DIR 指向临时目录），测试必须用它

## API 契约
```python
def ingest_image_bytes(raw_bytes: bytes, max_side: int | None = None) -> str: ...
# max_side 缺省取 settings.CAS_MAX_IMAGE_SIDE (1536)
# Pillow 解码 → 长边等比缩放 <= max_side → 转 RGB → WebP 编码 →
# 对编码后字节算 SHA-256 → 落盘 {CAS_ASSETS_DIR}/{h[:2]}/{h[2:4]}/{h}.webp → 返回 "cas://{h}"
# 幂等：同内容重复入库返回同 URI 且不重复写盘；写盘用临时文件+rename 原子替换

def resolve_cas_uri(uri: str) -> Path: ...
# 校验 "cas://" + 64 位小写 hex，非法抛 ValueError；文件不存在抛 FileNotFoundError
# （Phase 4 的 /assets/{hash} 静态路由将复用此函数）

def normalize_markdown_with_frontmatter(
    stem_md: str,
    question_uid: str | None,
    target_grade: str,
    base_dir: Path | None = None,
) -> tuple[str, str, str]: ...
# 返回 (规范化后的完整 Markdown, question_uid, stem_hash)
```

## normalize 语义（PM 批准的细化，base_dir 为签名扩展）
1. **Frontmatter**：解析/注入 `---` 包裹的头部，字段 `question_uid` 与 `target_grade_and_subject`。已有 uid 一律保留（不可变）；传入 uid 与已有冲突时以已有为准。无 frontmatter 且 question_uid=None 时生成确定性 uid：`uuid.uuid5(uuid.NAMESPACE_URL, "shuati-harness:" + sha256(规范化正文))`，保证同内容重导入幂等。不引入 PyYAML 依赖，用简单行解析。
2. **图片改写**：`![alt](path)` 中本地相对路径若能在 base_dir 下找到文件 → 调 ingest_image_bytes 改写为 `cas://{hash}`；已是 `cas://` 或 http(s) 的保持不动；找不到的本地路径保持原样（不报错）。
3. **stem_hash**：对"剥离 frontmatter 后的规范化正文"计算 SHA-256 hex。同一正文多次规范化结果必须稳定。

## 测试要求（tests/test_cas_store.py）
- 用 Pillow 在测试内合成图片（如 2000×1000 纯色/噪声图），不依赖二进制 fixture
- 长边缩放 ≤1536、输出为 WebP、URI 格式正确、磁盘路径分桶正确
- 幂等：同 bytes 两次 ingest 同 URI；文件只有一份
- 小图（<1536）不放大
- RGBA/P 模式图可正常转码
- resolve_cas_uri：合法/非法 URI、缺失文件三类分支
- normalize：uid 注入与保留、stem_hash 稳定性、本地图片改写（tmp base_dir）、cas:// 与 http 不动、缺失路径不动

## 完成标准
测试全绿 + ruff/mypy 零告警。最终消息汇报：创建的文件、测试通过数、门禁输出摘要。

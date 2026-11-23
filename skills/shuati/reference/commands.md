# Shuati CLI 交互命令完整参考

所有命令均默认支持 `--json`（stdout 输出单行 UTF-8 JSON，stderr 打印诊断日志），遵循退出码约定：
- `0`: 成功 (`ok: true`)
- `1`: 内部未预期错误 (`INTERNAL_ERROR`)
- `2`: 参数/门禁/歧义 UID (`PRECONDITION_FAILED` / `AMBIGUOUS_UID`)
- `3`: 资源不存在 (`NOT_FOUND`)
- `4`: 外部 LLM/Embedding 网关错误 (`LLM_ERROR`)

---

## 1. 角色与画像

```bash
shuati learner list --json
shuati learner create <learner_id> --name "小明" --subject 小学奥数 --current --json
shuati learner use <learner_id> --json
shuati profile [--learner <id>] --grade-subject 小学奥数 --json
```

## 2. 知识树、检索与拓扑推题

```bash
shuati taxonomy --grade-subject 小学奥数 [--learner <id>] [--l1 平面几何] --json
shuati search [--grade-subject 小学奥数] [--l1 ...] [--l2 ...] [--l3 ...] [--motif ...] [--method ...] [--trap ...] [--tier ...] [--elo-min N] [--elo-max N] [--query 关键词] [--semantic 骨架描述] [--exclude-solved] [--reveal quiz|hint|full] --limit 20 --json
shuati related <uid_or_source_id> [--types SCAFFOLD,ISOMORPHIC,CLONE,CONTRASTIVE,SHARED_TRAP] [--direction pre|post|both] [--exclude-solved] [--reveal quiz|hint|full] --json
```

> 注：`SCAFFOLD` 有向边约定 `source = 低阶子题 → target = 升维母题`，因此 `--direction pre` 返回当前题的前置铺垫子题，`--direction post` 返回升维母题。

## 3. 组卷、出题、判题与记录

```bash
shuati compose-quiz --grade-subject 小学奥数 [--learner <id>] [--l1/--l2/--l3/--motif/--method/--trap] [--count 5] [--mix 2:2:1] [--target-elo N] [--reveal quiz] --json
shuati detail <uid_or_source_id> [--reveal quiz|hint|full] [--hint-level 1] [--include-visual-facts] --json
shuati check <uid_or_source_id> --answer "(1)9:4;(2)156" [--learner <id>] [--hint-level 0] [--log] [--request-id <id>] [--quiz-batch-id <id>] [--judge-verdict JSON] --json
shuati log <uid_or_source_id> --outcome SOLVED_SOLO|SOLVED_WITH_HINT|WRONG|SKIPPED [--score-ratio 0.5] [--stuck-step 2] [--hint-level 1] [--note "..."] [--request-id <id>] --json
shuati log-batch --quiz-batch-id <id> --entries <json_or_@path> [--request-id <prefix>] --json
shuati log-void <log_id> [--reason "..."] --json
```

## 4. 练习单渲染、外部错题与人工覆写

```bash
shuati render-sheet [--quiz-batch-id <id> | --uids u1,u2] [--out data/sheets/my.html] [--embed-images/--no-embed-images] [--print-answers-at-end/--no-print-answers-at-end] --json
shuati diagnose --text "题干" [--image a.jpg] --grade-subject 小学奥数 [--answer "已知答案"] [--from-json JSON_or_@path] --json
shuati diagnose --file questions.md --grade-subject 小学奥数 [--concurrency 1] [--force] --json
shuati ingest-mistake [--draft-id drf_xxx | --text "题干" --grade-subject 小学奥数] [--answer "已知答案"] [--from-json ...] [--secondary-json ...] [--log-wrong] [--learner <id>] [--dry-run] --json
shuati override <uid_or_source_id> --field <field_name> [--value "..."] [--revoke] [--reconfirm] [--dry-run] [--reason "..."] --json
shuati skill install [--target .pi/skills/shuati] [--symlink/--copy] --json
```

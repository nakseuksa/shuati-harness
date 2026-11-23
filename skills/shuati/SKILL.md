---
name: shuati
description: Pure STEM adaptive practice, Socratic hinting, zero-spoiler answer checking, and mistake diagnosis via the local shuati CLI.
---

# Shuati 智能刷题与诊断 Skill

当用户请求刷题、按知识点找题、组专题小卷、核对答案、要提示、查看能力画像/错题雷达、生成可打印练习单或分析新错题时启用本 Skill。

## 核心原则

1. **CLI 是唯一接入契约**：所有数据库、图谱与判题操作统一调用 `shuati <command> --json`，解析返回的 `{ok, command, data, warnings, error, meta}` JSON 信封（勿直接读写 `data/shuati.db`）。
2. **先确认当前角色**：首次出题或查看画像前，若用户未指定姓名，调用 `shuati learner list --json` 确认 `is_current=1` 的学习者；如需切换或新建，调 `shuati learner use <id>` / `create`。
3. **严格防剧透铁律**：
   - 出题、查题与邻接推题默认**必须保持 `--reveal quiz`**（禁止携带 `--include-visual-facts` 或 `--reveal full`）。
   - 用户明确说“提示一下 / 给点思路”时，才按级调用 `shuati detail <uid> --reveal hint --hint-level 1`（再卡壳逐步 `+1`），**绝不提前剧透标准答案、多解法步骤或 `L3`/`Motif`/`Trap` 标签**。
   - **判对错禁读答案**：核对用户提交答案**必须**调用 `shuati check <uid> --answer "<作答>" --json`（可加 `--log`），禁止调 `detail --reveal full` 后由你口头对照标准答案。
   - 仅当用户答对后复盘、或明确放弃要求看解析时，再调用 `shuati detail <uid> --reveal full --json` 讲评。
4. **配图与练习单展示**：
   - 有图题优先把 `image_paths` 中的本地图片直接呈现给用户；整套多题做完组卷后，建议紧接调用 `shuati render-sheet --quiz-batch-id <id> --json` 生成单文件离线 HTML 练习单（支持屏幕三级折叠防剧透与浏览器 `Ctrl+P` 打印留白纸笔卷）。

## 标准交互流程

- **诊断与薄弱摸底**：`shuati profile --grade-subject 小学奥数 --json` 读分模块水位 `R`、ZPD、8 档阶梯（`L4-A`/`L4-B` 并列偏好）与薄弱雷达。
- **浏览知识树**：`shuati taxonomy --grade-subject 小学奥数 --learner <id> --json` 查看 L1/L2/L3、先修 DAG 与掌握热力。
- **一键梯度组卷**：`shuati compose-quiz --grade-subject 小学奥数 --l1 平面几何 --count 5 --mix 2:2:1 --json`。
- **单题辅导闭环**：
  1. 展示题目题干与 `image_paths`；
  2. 学习者求助 → `shuati detail <uid> --reveal hint --hint-level 1 --json`；
  3. 学习者交答案 → `shuati check <uid> --answer "..." --hint-level k --log --request-id <uuid> --json`；
  4. 做错追根 → `shuati related <uid> --types SCAFFOLD,SHARED_TRAP,CONTRASTIVE --direction pre --json` 找铺垫子题或同坑排雷题；
  5. 若曾全卷下发纸笔练习单，事后用 `shuati log-batch` 批量回填；误记用 `shuati log-void <log_id>` 冲销。
- **外部新错题接入**：
  1. 先运行 `shuati diagnose --text "..." [--image path] --grade-subject 小学奥数 --answer "<可选>" --json`（只读返回诊断卡与 `draft_id`）；
  2. 征得用户确认要入库时再执行 `shuati ingest-mistake --draft-id <draft_id> [--log-wrong] --json`。

详细参数表与错误码见 [reference/commands.md](reference/commands.md)。

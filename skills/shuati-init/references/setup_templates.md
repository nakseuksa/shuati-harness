# `shuati-init` 外部配置与新学段初始化模板

本文档供 [`SKILL.md`](../SKILL.md) 按需按步引用，保持主流程上下文精简。

---

## 1. `.env` 环境变量模板（按场景选用）

由项目根目录 `.env.example` 复制为 `.env` 后填入对应字段（`.env` 已被 `.gitignore` 忽略，不会入库提交）。

### 场景 A：AGY Swarm 免推理 Key + 本地 Ollama Embedding（推荐零外部付费 Key 起步）

```dotenv
# 推理全部由 AGY Swarm 子代理（默认 gemini-4-neon-medium）执行，以下 LLM Key 留空即可
VLM_PRIMARY_API_KEY=
VLM_SECONDARY_API_KEY=
TEXT_LLM_API_KEY=
JUDGE_LLM_API_KEY=

# 本地回环 Ollama Embedding（localhost / 127.0.0.1 免填 API Key）
EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=bge-m3
```

> 常见 Ollama 中文数学适用向量模型：`bge-m3`、`nomic-embed-text`、`shaw/dmeta-embedding-zh`。启动前确保 `ollama serve` 已运行且已 `ollama pull <model>`。

### 场景 B：AGY Swarm 免推理 Key + 云端兼容 Embedding（OpenAI / 阿里云百炼 DashScope）

```dotenv
# 阿里云百炼 OpenAI 兼容模式示例
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=sk-your-dashscope-key
EMBEDDING_MODEL=text-embedding-v3

# 或 OpenAI 官方 / 兼容中转
# EMBEDDING_BASE_URL=https://api.openai.com/v1
# EMBEDDING_API_KEY=sk-your-openai-key
# EMBEDDING_MODEL=text-embedding-3-large
```

### 场景 C：全量 `.env` OpenAI 兼容 API 直连模式（用于 `--mode api`）

```dotenv
VLM_PRIMARY_BASE_URL=https://api.openai.com/v1
VLM_PRIMARY_API_KEY=sk-...
VLM_PRIMARY_MODEL=gemini-2.5-pro

VLM_SECONDARY_BASE_URL=https://api.openai.com/v1
VLM_SECONDARY_API_KEY=sk-...
VLM_SECONDARY_MODEL=claude-3-7-sonnet

TEXT_LLM_BASE_URL=https://api.deepseek.com/v1
TEXT_LLM_API_KEY=sk-...
TEXT_LLM_MODEL=deepseek-chat

# 裁判模型留空时自动复用 TEXT_LLM（强制 temperature=0.0）
JUDGE_LLM_BASE_URL=
JUDGE_LLM_API_KEY=
JUDGE_LLM_MODEL=

EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-large
```

### 场景 D：纯离线本地 `sentence-transformers`（`local_st`）

无需在 `.env` 配置任何 `EMBEDDING_*` 密钥，直接修改 `config/settings.yaml`：

```yaml
llm:
  embedding:
    provider: "local_st"
    model: "BAAI/bge-m3"   # 或本地模型目录绝对路径
    batch_size: 32
```

---

## 2. `config/settings.yaml` 外部题源与关键项片段

> 全新 `git clone` 仓库默认**不包含**已被 `.gitignore` 忽略的 `./题库` 目录。接入外部题库时，务必**替换掉** `config/settings.yaml` 中默认的 `"题库"` 占位项（仅当本地确实存在 `./题库` 目录时才保留），否则 `shuati ingest` 与 `shuati doctor` 会因找不到该目录报错。

```yaml
ingest:
  roots:
    - "D:/我的题库/小学奥数"   # 替换默认的 "题库" 占位项；支持多个只读外部目录（绝对或相对路径）
    - "D:/我的题库/初中物理"
  dedup_near_default: true

pipeline:
  allow_pi_314_subjects:
    - 小学奥数
    # 若新学段允许把 π 近似为 3.14，再追加对应 grade_subject；初高中理科通常保留精确符号 pi

subagent:
  default_model: "${SUBAGENT_DEFAULT_MODEL:-gemini-4-neon-medium}"
  max_parallel_workers: 4
  stage_models: {}
```

---

## 3. `config/collections.yaml` 目录映射规则模板

`source_collection` 由扫描到的 `problems.jsonl` 所在目录**相对于其所属 `ingest.roots` 根目录的 POSIX 相对路径**生成，按自上而下顺序匹配首条命中的 `match` glob 规则：

```yaml
rules:
  - match: "ch*"
    grade_subject: 小学奥数
  - match: "xsc_ch*"
    grade_subject: 小学奥数
  - match: "初中物理/**"
    grade_subject: 初中物理
  - match: "mechanics_*"
    grade_subject: 初中物理
```

> 若根目录本身直接包含 `problems.jsonl`（相对路径为 `.`），建议将题目放在带章节前缀的子目录（如 `初中物理/ch01_声现象/problems.jsonl`），或在导入单集合时显式传入 `--grade-subject`。

---

## 4. 新学段提示词接缝模板 `config/prompts/subjects/<slug>.yaml`

仅需填写与 `config/prompts/default.yaml` 不同的学科专有约定（使用 `append` / `append_file` 增量叠加，切勿覆盖核心防泄漏与 Schema 硬契约）。

```yaml
schema_version: 1
grade_subject: "初中物理"
slug: "junior_physics"

blocks:
  persona:
    append: |
      你特别精通初中物理课程标准与中考压轴教研体系，熟悉力学（受力分析/压强/浮力/杠杆滑轮/功和机械能）、电学（串并联/欧姆定律/动态电路/电功电热）、光学与热学实验探究。

  solving_conventions:
    append: |
      初中物理专有约定：
      - 涉及重力加速度 g 时，若题干明确给定 g=10N/kg 或 g=9.8N/kg 则严格按题干取值；未明示时在 sympy_expr 中按标准教学默认 g=10 处理并在过程说明单位制（SI）。
      - 多小问或带单位的数值结果，在 final_answers 的单位说明与纯数值校验间保持一致，禁止使用高中微积分超纲解法。

  steps_and_socratic_hint:
    append: |
      初中物理步骤与原子算子风格：
      - raw_l3 示例：“受力分析正交平衡列式”“阿基米德原理浮力替换”“动态电路滑动变阻器分压判断”“滑轮组有效绳段数识别”。

  difficulty_rubric_notes:
    append: |
      初中物理标尺校准提醒：
      - 单一公式直代题归入 L1/L2，即使计算小数较多也须控制 C=1~2；
      - 需结合受力模型转换、极值比例或电路状态切换的综合压轴题才可给 C>=3 或 T=3。

stages:
  stage3_propose:
    append: |
      初中物理 L1 建议围绕声现象、光现象、热学、力学基础、压强浮力、简单机械、电路与欧姆定律、电功率等主干模块组织。

  stage3d_cluster_assign:
    append: |
      同域物理算子须区分适用条件（如“漂浮条件二力平衡”与“阿基米德排液求浮力”不可混并）。
```

完成创建或编辑后务必运行：

```bash
shuati prompt lint --json
shuati prompt render --stage unified --grade-subject "初中物理" --json
```

---

## 5. 黄金难度锚点模板 `config/anchors/anchors_<slug>.yaml`

### D4 确定性公式与 8 档速查表

- **连续分**：`base_elo = min(2800, max(800, 750 + 85 * (2**C - 1) + 105 * S + 115 * T + 65 * L))`
- **四维取值范围**：`C ∈ [1..4]`，`S ∈ [1..3]`，`T ∈ [1..3]`，`L ∈ [1..3]`
- **8 档判定规则**（`anchors_<slug>.yaml` 中的 `base_elo` 和 `target_tier` 必须与脚本计算完全一致）：
  - `C=1, S=1, T=1` → `L1_基础直达`
  - `C<=2` 且 `elo < 1400` → `L2_常规巩固`；`1400 <= elo < 1600` → `L3_综合中档`；`elo >= 1600` → `L4-A_进阶拔高(繁)`
  - `C=3` 且 `T=3, L<=2` → `L4-B_进阶拔高(巧)`；否则 `elo >= 1850` → `L5-1_压轴挑战`，其余 → `L3_综合中档`
  - `C=4`：`elo < 2400` → `L5-1_压轴挑战`；`2400 <= elo < 2600` → `L5-2_竞赛超难`；`elo >= 2600` → `L5-3_殿堂极限`

### 文件结构示例（至少 4 道，建议 4～5 道跨梯度已验真题目）

```yaml
grade_subject: 初中物理
version: "1.0"
anchors:
  - question_uid: "junior_physics:ch01_力学基础:q003"
    target_tier: "L1_基础直达"
    scores:
      C: 1
      S: 1
      T: 1
      L: 1
    base_elo: 1120
    rationale: "单一密度公式直代求解，单步直达、无技巧转化、基础运算。"

  - question_uid: "junior_physics:ch05_压强浮力:q012"
    target_tier: "L3_综合中档"
    scores:
      C: 2
      S: 2
      T: 2
      L: 1
    base_elo: 1510
    rationale: "液体压强与柱体切割叠放转化（2 个核心概念、2 步推演、需识别底面积不变量）。"

  - question_uid: "junior_physics:ch08_电学综合:q019"
    target_tier: "L4-A_进阶拔高(繁)"
    scores:
      C: 2
      S: 3
      T: 2
      L: 3
    base_elo: 1745
    rationale: "多开关切换下串并联状态追踪与电功比例连列（步骤繁琐 L=3，概念跨度受控于 C=2）。"

  - question_uid: "junior_physics:ch05_压强浮力:q027"
    target_tier: "L4-B_进阶拔高(巧)"
    scores:
      C: 3
      S: 2
      T: 3
      L: 2
    base_elo: 2030
    rationale: "容器液面变化与整体法隔离法巧妙联立消元（技巧转化 T=3 且运算简洁）。"

  - question_uid: "junior_physics:ch09_压轴专题:q035"
    target_tier: "L5-2_竞赛超难"
    scores:
      C: 4
      S: 2
      T: 2
      L: 1
    base_elo: 2530
    rationale: "力热电多物理场耦合极值边界分析（四概念综合 C=4）。"
```

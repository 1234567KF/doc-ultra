# doc-ultra

文档超融合处理工具 — 多视角并行优化 + 串行拷问检查流水线，产出无 AI 痕迹的高质量 Markdown。

## 安装

```bash
pip install -e .
```

安装后可通过 `doc-ultra` 命令调用，或使用：

```bash
python -m doc_ultra.cli <输入文件> -p <预设> -o <输出文件>
```

## 配置文件

`doc-ultra.config.yaml` — 在项目根目录或 `~/.doc-ultra.config.yaml`（自动查找）

### 配置结构

```yaml
# Provider 注册（可选，默认使用直接模型配置）
providers:
  pro:
    type: openai
    model: gpt-4o
    api_key: ${OPENAI_API_KEY}
    capabilities: [high_quality, fast, precise, structured]
  deepseek:
    type: deepseek
    model: deepseek-chat
    api_key: ${DEEPSEEK_API_KEY}
    base_url: https://api.deepseek.com
    capabilities: [economical, long_context, structured]

# 默认阶段模型分配
models:
  parser: gpt-4o
  optimizer_radical: claude
  optimizer_conservative: gpt-4o
  optimizer_balanced: deepseek-chat
  fuser: claude
  checker: gpt-4o
  expander: deepseek-chat
  polisher: gpt-4o-mini
```

### 环境变量

在 `doc-ultra.config.yaml` 中使用 `${VAR_NAME}` 语法引用环境变量：

```bash
export OPENAI_API_KEY=sk-...
export DEEPSEEK_API_KEY=...
```

## 使用方式

### CLI 完整流水线

```bash
doc-ultra 输入.md -p bid -o 输出.md
```

### 查看可用预设

```bash
doc-ultra --list-presets
```

### 预设说明

| 预设 | 适用场景 |
|------|---------|
| `bid` | 招投标文档 |
| `patent` | 专利文档 |
| `software_copyright` | 软件著作权 |
| `standard` | 团体/行业标准 |
| `project_application` | 项目申报书 |
| `knowledge_base` | 知识库文档 |
| `general` | 通用文档 |
| `economical` | 低成本模式 |

## 项目结构

```
.qoder/skills/doc-ultra/
├── SKILL.md                        ← Qoder Skill 定义
├── reference.md                     ← 流水线协议 + System Prompt
├── presets.md                      ← 预设速查
├── evals.md                        ← 触发评估用例
├── assets/output-template.md       ← 输出模板
├── memory/pipeline-log.md          ← 运行日志
└── reference/doc_ultra/            ← CLI 源码
    ├── agents/                     ← 6 个 Agent
    ├── prompts/                    ← 9 个 Prompt 模板
    ├── providers/                  ← 4 个 Provider 适配器
    ├── cli.py
    ├── config.py
    └── pipeline.py
```

## 开发

```bash
# 干运行测试
python -m doc_ultra.cli <输入> --dry-run

# 指定配置文件
python -m doc_ultra.cli <输入> --config <路径>

# 仅检查模式
python -m doc_ultra.cli <输入> --check-only

# 自动检测文档类型
python -m doc_ultra.cli <输入> --auto-preset
```

## 依赖

- Python >= 3.10
- click >= 8.0
- pyyaml >= 6.0
- openai >= 1.0
- anthropic >= 0.30
- rich >= 13.0
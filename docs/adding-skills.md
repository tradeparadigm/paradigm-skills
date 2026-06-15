# Adding a New Skill

Step-by-step guide for contributing a new skill to this repo.

## 1. Check for overlap

Review the [existing skills](../README.md#skills) to make sure your idea is distinct.
Skills should have a clear, non-overlapping purpose:

- **paradigm-rfq-trader**: Block-trade RFQ lifecycle (build, benchmark, submit, manage)
- **block-analyst**: Post-trade analysis and fill benchmarking of cleared blocks
- **data-discovery**: Historical market-data catalog and DuckDB query launcher

## 2. Create the directory

```bash
mkdir -p skills/your-skill-name/references
```

## 3. Write SKILL.md

Create `skills/your-skill-name/SKILL.md`:

```yaml
---
name: paradigm-your-skill-name
description: >
  What this skill does. When to use it. Include trigger phrases like
  "check my X", "analyze Y", "show me Z" so agents can discover it.
---

# Paradigm Your Skill Name

One-liner: what this skill does and why it exists.

## Available MCP Tools

| Tool | What it provides |
|------|-----------------|
| `paradigm_tool_name` | Description of data used |

## Capabilities

### 1. First Capability
How to use the MCP tools to deliver this capability.
Include the process, key calculations, and decision logic.

## Output Format

### Quick Check
[Template for brief responses]

### Full Report
[Template for detailed responses]

## Caveats

- Limitations of the analysis
- What this skill does NOT do
- Disclaimer: not financial advice
```

## 4. Naming rules

| Rule | Example |
|------|---------|
| Directory: lowercase, hyphens, no `paradigm-` prefix | `skills/block-scanner/` |
| Name field: include `paradigm-` prefix for discoverability | `name: paradigm-block-scanner` |
| Max 64 characters for name | — |
| Description: max 1024 characters | — |
| Description must include WHAT it does and WHEN to use it | — |

## 5. Add references (optional)

For detailed material that would bloat the main SKILL.md (formulas, large tables, query cookbooks), create reference files:

```
skills/your-skill-name/
├── SKILL.md
└── references/
    └── detailed-methodology.md
```

Link from SKILL.md:
```markdown
See [detailed-methodology.md](references/detailed-methodology.md) for the full calculation reference.
```

Keep references one level deep — don't chain reference files to other reference files.

## 6. Body guidelines

- Keep SKILL.md body under **500 lines**
- Include an MCP tools table listing every tool the skill uses
- Provide concrete output format examples with realistic market data
- State caveats clearly — what the skill can't do, data limitations, not financial advice
- Use a conversational but precise tone
- Match the style of existing skills

## 7. Test your skill

1. Connect the [Paradigm MCP server](https://github.com/tradeparadigm/mcp-paradigm-py)
2. Copy your skill folder into your agent's skills directory
3. Ask questions that should trigger your skill
4. Verify the skill activates and produces useful output
5. Test edge cases (no data available, single position, many positions)

## 8. Submit a PR

1. Fork the repo
2. Create a branch: `git checkout -b add-your-skill-name`
3. Add your skill directory under `skills/`
4. Update the skills table in `README.md`
5. Open a PR with a description of what the skill does and example usage

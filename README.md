# Paradigm Skills

Intelligence skills for AI agents trading on Paradigm.

Built on top of the [Paradigm MCP server](https://github.com/tradeparadigm/mcp-paradigm-py), which handles transport, authentication, and request signing. These skills add the workflow and analytical layer on top.

## Skills

| Skill | What it does |
|-------|-------------|
| [paradigm-rfq-trader](./skills/paradigm-rfq-trader/) | Trigger institutional block trades via Paradigm's DRFQv2 flow — resolve instruments, build the RFQ, benchmark, confirm, submit, verify settlement |
| [block-analyst](./skills/block-analyst/) | Cross-venue analysis of Paradigm RFQ block trades using live market data from Deribit, OKX, and Bybit |
| [data-discovery](./skills/data-discovery/) | Catalog and query-launcher for historical market data in S3 — returns an S3 path plus a ready-to-run DuckDB query |

## Quick start

### Claude (via MCP)

These skills work with the Paradigm MCP server connected to Claude. Add the MCP server first:

```json
{
  "mcpServers": {
    "paradigm": {
      "command": "mcp-paradigm",
      "env": {
        "PARADIGM_ACCESS_KEY": "<key>",
        "PARADIGM_SIGNING_KEY": "<base64>",
        "PARADIGM_ENVIRONMENT": "testnet"
      }
    }
  }
}
```

Then point Claude at this repo or copy individual skill folders into your workspace.

### OpenClaw

```
install the paradigm-rfq-trader skill from https://github.com/tradeparadigm/paradigm-skills
```

Or install individually:

```
install the block-analyst skill from https://github.com/tradeparadigm/paradigm-skills
```

### Claude Code

```bash
# Clone into your skills directory
git clone https://github.com/tradeparadigm/paradigm-skills.git ~/.agents/skills/paradigm-skills

# Or install a single skill
cp -r skills/paradigm-rfq-trader ~/.agents/skills/paradigm-rfq-trader
```

### skills.sh

```bash
npx skills add tradeparadigm/paradigm-skills --skill paradigm-rfq-trader
```

## Prerequisites

- [Paradigm MCP server](https://github.com/tradeparadigm/mcp-paradigm-py) connected to your agent

Each skill is self-contained — copy any `skills/<name>/` folder into your agent's skill directory and it works independently.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for details on adding new skills or improving existing ones.

Skills follow the [AgentSkills](https://agentskills.so) open standard — compatible with Claude, OpenClaw, Cursor, Windsurf, and other SKILL.md-compatible agents.

## License

MIT
</content>

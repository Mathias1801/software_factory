# Cost overview

What a factory run costs in API fees, for each build mode and model set.
Prices checked on 26 September 2026. They change often, so check them before budgeting.

## Cost per project

Each cell shows **typical cost / ceiling**. The ceiling is the most the mode's token budgets allow
before the factory stops and asks you.

| Model set | Simple | Prototype | Full-stack |
|---|---|---|---|
| `anthropic` (default) | $0.33 / $0.73 | $0.83 / $1.47 | $2.58 / $5.55 |
| `anthropic_lean` | $0.21 / $0.49 | $0.55 / $0.98 | $1.66 / $3.51 |
| `anthropic_max` | $0.77 / $1.72 | $1.94 / $3.43 | $5.89 / $12.48 |
| `openai` | $0.26 / $0.55 | $0.62 / $1.09 | $2.04 / $4.58 |
| `gemini` | $0.20 / $0.46 | $0.52 / $0.92 | $1.79 / $4.14 |
| `mixed_budget` | $0.14 / $0.30 | $0.34 / $0.60 | $1.04 / $2.21 |

## What the modes and sets mean

| Mode | Typical project | Tokens (typical / ceiling) |
|---|---|---|
| Simple | Script, CLI or small tool, 1 to 3 tasks | 73k / 181k |
| Prototype | Demo or proof of concept, 4 to 6 tasks | 204k / 370k |
| Full-stack | Several components, 10 to 15 tasks | 646k / 1.41M |

| Model set | Cheap tier (builder, critic, README) | Mid tier (tests, review) | Strong tier (planning, escalations) |
|---|---|---|---|
| `anthropic` | Claude Haiku 4.5 | Claude Sonnet 5 | Claude Opus 5.5 |
| `anthropic_lean` | Claude Haiku 4.5 | Claude Haiku 4.5 | Claude Sonnet 5 |
| `anthropic_max` | Claude Sonnet 5 | Claude Opus 5.5 | Claude Fable 5.1 |
| `openai` | GPT-5.6 Luna | GPT-5.6 Terra | GPT-5.6 Sol |
| `gemini` | Gemini 3.8 Flash | Gemini 3.1 Pro | Gemini 3.1 Pro |
| `mixed_budget` | GPT-5.6 Luna | Gemini 3.8 Flash | Claude Sonnet 5 |

## Model prices

USD per million tokens (input / output).

| Provider | Model | Price | Note |
|---|---|---|---|
| Anthropic | Claude Haiku 4.5 | $1 / $5 | |
| Anthropic | Claude Sonnet 5 | $2 / $10 | |
| Anthropic | Claude Opus 5.5 | $4 / $20 | |
| Anthropic | Claude Fable 5.1 | $10 / $50 | |
| OpenAI | GPT-5.6 Luna | $0.20 / $1.20 | |
| OpenAI | GPT-5.6 Terra | $2 / $12 | |
| OpenAI | GPT-5.6 Sol | $4 / $20 | Promotional until at least 21 Nov 2026, then $5 / $30 |
| OpenAI | GPT-6 Astra | $10 / $50 | |
| Google | Gemini 3.1 Flash-Lite | $0.25 / $1.50 | |
| Google | Gemini 3.8 Flash | $0.75 / $3.75 | Introductory until 31 Dec 2026, then $1.50 / $7.50 |
| Google | Gemini 3.1 Pro | $2 / $12 | Higher rate for prompts above 200K tokens |

## How to switch

In your profile:

```yaml
model_set: openai
```

Or for one run:

```bash
python -m factory run --models openai --idea-file idea.md
```

Check that keys and packages are ready first:

```bash
python -m factory models --models openai
```

## How the numbers are calculated

- **Estimates, not measurements:** token volumes come from the factory's design, not from real runs yet. After a run, `PROJECT.md` in the project folder shows the actual tokens and cost.
- **Assumed mix:** 75% of tokens are input and 25% output, and 20% of the building is escalated to the strong tier.
- **Ceiling:** equals the token budgets per mode in `profiles/default.yaml`.
- **Not included:**
  - prompt caching, which lowers the cost
  - batch discounts
  - web search fees
  - hidden reasoning tokens, which OpenAI and Gemini bill as output, so real costs on those models are likely somewhat higher than shown
- **Keeping it up to date:** when prices change, update the `pricing:` table in `profiles/default.yaml` (used for `PROJECT.md`) and this file.

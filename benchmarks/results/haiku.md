# Claude Haiku 4.5 as the model tier

Model tier: `claude-haiku-4-5`, given schemagate's standard prompt for the columns the deterministic
tiers could not resolve, with the 0.75 cap applied. Baseline: the offline heuristic provider on the
same variants. "Held by cap" counts model mappings whose own confidence reached the 0.85 gate but
were capped into per-column review.

## synthetic (20 variants)

| model tier | precision | recall | decoy FP rate | gate queue | wrong in gate queue | held by cap | wrong among held | model-tier precision |
|---|---|---|---|---|---|---|---|---|
| heuristic | 0.935 | 0.886 | 0.082 | 111 | 0 | 0 | 0 | 0.876 (n=121) |
| claude-haiku-4-5 | 0.992 | 1.000 | 0.041 | 111 | 0 | 134 | 0 | 0.985 (n=136) |

## valentine (28 variants)

| model tier | precision | recall | decoy FP rate | gate queue | wrong in gate queue | held by cap | wrong among held | model-tier precision |
|---|---|---|---|---|---|---|---|---|
| heuristic | 0.617 | 0.850 | 0.412 | 60 | 0 | 13 | 0 | 0.529 (n=261) |
| claude-haiku-4-5 | 0.916 | 0.888 | 0.066 | 60 | 0 | 147 | 6 | 0.885 (n=166) |


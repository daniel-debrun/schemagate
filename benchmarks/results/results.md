Command: `python -m benchmarks.run --variants 50 --seed 7`

Suite: 200 sender variants over 4 schemas, 2971 source columns (2444 with a true target, 527 decoys). Gate threshold: confidence >= 0.85.

| config | deterministic resolution | precision | recall | decoy FP rate | gate queue | wrong in gate queue | precision at gate |
|---|---|---|---|---|---|---|---|
| schemagate | 0.425 | 0.938 | 0.883 | 0.076 | 1039 | 0 | 1.000 |
| model_uncapped | 0.000 | 0.938 | 0.883 | 0.076 | 954 | 0 | 1.000 |
| schemagate_overconfident | 0.425 | 0.938 | 0.883 | 0.076 | 1039 | 0 | 1.000 |
| model_overconfident_uncapped | 0.000 | 0.938 | 0.883 | 0.076 | 1507 | 8 | 0.995 |

### schemagate: by tier

| tier | mapped | precision |
|---|---|---|
| dictionary | 412 | 1.000 |
| exact | 336 | 1.000 |
| model | 1261 | 0.887 |
| synonym | 291 | 1.000 |

### schemagate: recall by header perturbation

| operation | columns | recall |
|---|---|---|
| abbreviation | 349 | 0.914 |
| alias | 598 | 0.992 |
| canonical | 578 | 0.986 |
| foreign | 75 | 0.680 |
| paraphrase | 1268 | 0.784 |
| reorder | 288 | 0.899 |
| typo | 230 | 0.796 |
| unit_suffix | 185 | 0.930 |

### model_uncapped: by tier

| tier | mapped | precision |
|---|---|---|
| model | 2300 | 0.938 |

### model_uncapped: recall by header perturbation

| operation | columns | recall |
|---|---|---|
| abbreviation | 349 | 0.914 |
| alias | 598 | 0.992 |
| canonical | 578 | 0.986 |
| foreign | 75 | 0.680 |
| paraphrase | 1268 | 0.784 |
| reorder | 288 | 0.899 |
| typo | 230 | 0.796 |
| unit_suffix | 185 | 0.930 |

### schemagate_overconfident: by tier

| tier | mapped | precision |
|---|---|---|
| dictionary | 412 | 1.000 |
| exact | 336 | 1.000 |
| model | 1261 | 0.887 |
| synonym | 291 | 1.000 |

### schemagate_overconfident: recall by header perturbation

| operation | columns | recall |
|---|---|---|
| abbreviation | 349 | 0.914 |
| alias | 598 | 0.992 |
| canonical | 578 | 0.986 |
| foreign | 75 | 0.680 |
| paraphrase | 1268 | 0.784 |
| reorder | 288 | 0.899 |
| typo | 230 | 0.796 |
| unit_suffix | 185 | 0.930 |

### model_overconfident_uncapped: by tier

| tier | mapped | precision |
|---|---|---|
| model | 2300 | 0.938 |

### model_overconfident_uncapped: recall by header perturbation

| operation | columns | recall |
|---|---|---|
| abbreviation | 349 | 0.914 |
| alias | 598 | 0.992 |
| canonical | 578 | 0.986 |
| foreign | 75 | 0.680 |
| paraphrase | 1268 | 0.784 |
| reorder | 288 | 0.899 |
| typo | 230 | 0.796 |
| unit_suffix | 185 | 0.930 |

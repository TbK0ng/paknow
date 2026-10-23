# Claim → concept alignment

- database: `sqlite:///E:/PAknow/data/paknow.db`
- threshold: 0.8 (model pass)
- last script run was a no-op dry run (891 aligned / 0 new / 997 residual);
  the applied run history is recorded here by hand.

## Applied run history

| run | originally aligned | newly aligned | residual |
| --- | ---: | ---: | ---: |
| 1. deterministic dry run | 278 | 504 (planned) | 1106 |
| 2. deterministic `--apply` | 278 | **504** | 1106 |
| 3. deterministic `--apply` again (idempotency) | 782 | 0 | 1106 |
| 4. model `--apply --model --batch-size 40` | 782 | **109** | 997 |
| 5. deterministic `--apply` again (idempotency) | 891 | 0 | 997 |

Final state: 891 aligned / 1888 claims, 997 residual across 721 distinct subjects.
New alignments: **613** total (504 deterministic + 109 model).
Deterministic coverage: 501 distinct subjects / 782 claims (278 pre-existing + 504 new).
Model coverage: 77 distinct subjects / 109 claims, all above the 0.8 threshold;
no model errors, so the fail-open path was not exercised on this run.

By method (planned): alias 0, substring 504, model 109. Exact normalized alias
never fired because the pre-existing 278 already consumed every exact alias;
the deterministic gain came from single-concept substring matches.

## Invariants checked

- Claims remain `extraction_status='pending'` (one pre-existing `reviewed` claim
  untouched); nothing was promoted to citable evidence.
- `evidence_units` stayed at 91,235 rows; no evidence/provenance rows were added.
- All 1,888 `SUPPORTED_BY` edges remain `origin='claim_extract'`.
- No claim with an existing `subject_concept_id` was overwritten; a second
  `--apply` writes zero new alignments.
- Model-created concept nodes: 39 (139 → 178), so model-aligned ids are reachable
  by `concept_lookup`.

## Mechanism query sample

```bash
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  .venv/Scripts/python.exe -c "
from paknow.graph.retrieve import concept_lookup
rows = concept_lookup(['扩大三角形'], limit=40)
print([c['concept_id'] for c in rows if c['type']=='concept'])
for c in rows:
    if c['type']=='claim' and c['subject_concept_id']=='auto_expanding_triangle':
        print(c['subject'], '|', c['predicate'], '|', c['qualifiers'], '|', c['supporting_evidence'])
"
```

Sample output (model-aligned claims, conditions + supporting evidence):

```text
concept hits: ['auto_expanding_triangle']
扩张三角形底部 | 随后出现反向形态 | {'context': ['扩张三角形底部'], 'modality': 'is'} | ['e2d9f0c0f494b201300badd2f25dd2ce']
扩张三角形 | 在K线5高点完成 | {'context': ['图10.10的Globex美国线图', '缩图是日盘，编号相同'], 'modality': 'is'} | ['17a5241afb0176e2eb21c3a57a368e8f']
扩张三角形 | 提前识别后应留意跌破K线 | {'context': ['若提前识别'], 'modality': 'should'} | ['b99588a2e2a5c3a44f15715f1d0d43e8']
EVIDENCE e2d9f0c0f494b201300badd2f25dd2ce | 图8.30｜扩张三角形底部与随后反向形态。
```

The residual subject list is in `claim-residuals.json` (721 entries, with claim
counts, match reason, and dictionary candidates).

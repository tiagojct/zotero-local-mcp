---
days: 7
max_per_query: 20
---
Saved searches for the weekly literature alert (see [[Zotero MCP]]). Every Monday a script runs these searches, drops works already in Zotero or already reported, and writes Inbox/Literature alerts YYYY-MM-DD.md. No AI model is used.

Format: one search per bullet, in backticks, then a label. PubMed searches use PubMed syntax. OpenAlex searches are plain words.

Draft. Edit the searches to match what you follow.

## pubmed

- `("exhaled nitric oxide"[tiab] OR FeNO[tiab]) AND (asthma[tiab] OR asthma[mh])` FeNO in asthma
- `spirometry[tiab] AND ("reference equations"[tiab] OR "reference values"[tiab] OR GLI[tiab])` Spirometry reference equations
- `"clinical decision support"[tiab] AND (respiratory[tiab] OR asthma[tiab] OR COPD[tiab])` Respiratory decision support
- `("large language model*"[tiab] OR LLM[tiab]) AND ("clinical decision"[tiab] OR "medical education"[tiab])` LLMs in clinical decisions and education

## openalex

- `health data literacy medical education` Health data literacy
- `digital health maturity index hospitals` Digital maturity of health units

---
required_facets: topic, status
single_facets: status
---
Controlled tag vocabulary for the Zotero library. The zotero MCP server reads this file and refuses any tag that is not listed here. Rules for agents: [[Zotero agent]].

Format: one tag per bullet, in backticks, then a short description. Put old or alternative names after "aliases:". The agent uses aliases to map old tags.

Draft. Replace this list with the one built from the existing tags (see [[Zotero agent]], Cleaning existing tags).

## topic

What the item is about. 1 to 4 per item.

- `topic/respiratory-physiology` Physiology of breathing and gas exchange. aliases: respiratory physiology, pulmonary physiology
- `topic/lung-function` Lung function testing in general. aliases: lung function tests, pulmonary function tests, PFT
- `topic/spirometry` Spirometry. aliases: Spirometry, FEV1, FVC
- `topic/feno` Exhaled nitric oxide. aliases: FeNO, exhaled nitric oxide, nitric oxide
- `topic/reference-values` Reference values and equations for physiological measures. aliases: reference equations, GLI, normal values
- `topic/asthma` Asthma. aliases: Asthma
- `topic/copd` Chronic obstructive pulmonary disease. aliases: COPD, emphysema
- `topic/digital-health` Digital health, eHealth and mHealth. aliases: eHealth, mHealth, telemedicine
- `topic/medical-informatics` Health and medical informatics. aliases: health informatics, medical informatics
- `topic/clinical-decision-support` Clinical decision support systems. aliases: CDSS, decision support
- `topic/artificial-intelligence` AI in health, general. aliases: AI, artificial intelligence
- `topic/generative-ai` Large language models and generative AI. aliases: LLM, ChatGPT, large language models
- `topic/health-data` Health data, electronic health records and data literacy. aliases: EHR, electronic health records, data literacy
- `topic/medical-education` Medical and health professions education. aliases: medical education
- `topic/scientific-writing` Scientific writing and publishing. aliases: academic writing, publishing
- `topic/research-integrity` Research integrity, misinformation and citation quality. aliases: misinformation, retractions
- `topic/history-of-medicine` History of medicine and physiology. aliases: history

## method

How the work was done. Only when it applies.

- `method/statistical-modelling` Regression and other statistical models. aliases: regression, GAMLSS
- `method/machine-learning` Machine learning. aliases: ML, machine learning
- `method/nlp` Natural language processing. aliases: NLP, text mining
- `method/survey` Questionnaire survey. aliases: questionnaire
- `method/interviews` Interviews and focus groups. aliases: focus groups
- `method/delphi` Delphi or consensus method. aliases: consensus
- `method/psychometrics` Instrument development and validation. aliases: validation
- `method/usability-testing` Usability and user testing. aliases: usability
- `method/simulation` Simulation or modelling study.

## type

What kind of study or document it is.

- `type/rct` Randomised controlled trial. aliases: RCT, Randomized Controlled Trial
- `type/cohort` Cohort study. aliases: Cohort Studies, longitudinal
- `type/case-control` Case-control study.
- `type/cross-sectional` Cross-sectional study. aliases: Cross-Sectional Studies
- `type/diagnostic-accuracy` Diagnostic accuracy study.
- `type/validation-study` Validation study (instrument, model or equation).
- `type/qualitative` Qualitative study.
- `type/mixed-methods` Mixed-methods study.
- `type/systematic-review` Systematic review. aliases: Systematic Review
- `type/meta-analysis` Meta-analysis. aliases: Meta-Analysis
- `type/scoping-review` Scoping review.
- `type/narrative-review` Narrative review. aliases: Review
- `type/guideline` Guideline, statement or standard. aliases: Practice Guideline
- `type/protocol` Study protocol.
- `type/editorial` Editorial, commentary or letter. aliases: Editorial, Comment, Letter
- `type/case-report` Case report.
- `type/technical-report` Technical or institutional report.
- `type/textbook` Textbook.
- `type/monograph` Monograph or non-fiction book.
- `type/edited-book` Edited book or book chapter.

## status

Reading status. Exactly one per item.

- `status/to-read` Not read yet.
- `status/reading` Reading now.
- `status/read` Read.

# Analysis Code: Self-Reported Side Effects of Semaglutide and Tirzepatide in Online Communities

This repository contains the analysis code for:

> **Self-Reported Side Effects of Semaglutide and Tirzepatide in Online Communities**  
> Sehgal NKRS, Shaw Tronieri J, Ungar L, Guntuku SC.

## Overview

The pipeline processes Reddit posts mentioning GLP-1 receptor agonists (semaglutide and tirzepatide) to identify and characterize self-reported side effects. Posts are classified for self-use, side effects are extracted and mapped to MedDRA Preferred Terms, and frequencies are tabulated.

## Pipeline

| Step | Script | Description |
|------|--------|-------------|
| 1 | `01_data_collection.py` | Collect and filter Reddit posts from relevant subreddits |
| 2 | `02_self_use_classification.py` | Classify posts for self-disclosed medication use (GPT-4o-mini) |
| 3 | `03_side_effect_extraction.py` | Extract side effects and map to MedDRA PTs (GPT-4.1-mini with RAG) |
| 4 | `04_generate_tables.py` | Generate Table 1 (PT frequencies by SOC), co-occurrence analysis, and semaglutide vs. tirzepatide sub-analysis |

## Requirements

```
pandas
numpy
openai
scipy
statsmodels
```

## Data

Raw Reddit data were collected from publicly available posts via [Pushshift](https://github.com/pushshift/api) and [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) for the period January 2015 – June 2025. Due to Reddit's Terms of Use, raw data are not shared.

MedDRA terminology files (version 28.0) are required for Step 4 and are available under license from [MedDRA MSSO](https://www.meddra.org/).

## Subreddits

Posts/comments were collected from nine subreddits:
- r/mounjaro, r/ozempic, r/Semaglutide
- r/fasting, r/intermittentfasting, r/keto
- r/loseit, r/SuperMorbidlyObese, r/PlusSize

## Notes

- Steps 2 and 3 use the OpenAI Batch API and require an API key.
- The side effect extraction step (Step 3) uses retrieval-augmented generation with MedDRA terminology files uploaded to an OpenAI vector store.
- All analyses were conducted in Python 3.11.

# Benchmark: retrieval, citations and offline answers

Measured 2026-11-20 on Windows 11 (AMD64), Python 3.12.3, pypdf 6.19.0. Command: `python scripts/benchmark.py`. Offline and deterministic: no API key, no network.

Corpus: 5 PDFs, 16 pages, 66 questions (59 answerable, 7 not answered by the document). See eval/build_corpus.py.

## Retrieval and answers

| Metric | pdfqa (layout-aware) | Naive baseline (pypdf text, 600-char windows) |
|---|---:|---:|
| Recall@1 | 94.9% | 84.7% |
| Recall@3 | 100.0% | 96.6% |
| Recall@5 | 100.0% | 96.6% |
| MRR | 0.972 | 0.907 |
| Answer accuracy (answer + page) | 94.9% | 61.0% |
| Exact match (short answer) | 55.9% | 33.9% |
| Citation correct (page + verified quote) | 94.9% | 62.7% |
| False abstentions | 1.7% | 11.9% |
| Abstained on unanswerable | 100.0% | 100.0% |

Answer accuracy by question type (pdfqa / naive):

| Type | pdfqa | Naive |
|---|---:|---:|
| table | 11/11 (100.0%) | 8/11 (72.7%) |
| text | 30/32 (93.8%) | 22/32 (68.8%) |
| two-column | 15/16 (93.8%) | 6/16 (37.5%) |

## Citation verifier

Each verified quote from the pipeline is perturbed and re-checked against the page text.

| Case | Result |
|---|---:|
| Unmodified quotes pass | 58/58 (100.0%) |
| Quote cited on the wrong page: flagged as moved | 53/53 (100.0%) |
| ... and the correct page recovered | 53/53 (100.0%) |
| A number in the quote changed: flagged | 52/52 (100.0%) |
| One word swapped: flagged | 57/58 (98.3%) |

Two-column report (meridian-annual-report.pdf): of 18 quotes, 18 are found verbatim in the layout-ordered page text and 4 in pypdf's plain text, whose content-stream order interleaves the two columns.

## Speed

| Measure | Value |
|---|---:|
| Ingest, pdfqa (parse + chunk + index), corpus | 52.0 pages/s |
| Ingest, naive baseline, corpus | 124.4 pages/s |
| Ingest, pdfqa, 100-page document | 77.3 pages/s |
| Query latency p50 (retrieve + answer + verify) | 2.81 ms |
| Query latency p95 | 7.38 ms |

## Remaining misses (pdfqa)

- How many passenger journeys did Meridian carry in 2025? expected `112.4 million`, got `100,000`
- What is the mileage rate for using your own car? expected `EUR 0.32`, got `(abstained)`
- What VAT rate is applied on the invoice? expected `20%`, got `2026`

## Notes

- The three generated documents and the questions were written for this benchmark, and the abstention threshold (0.38) and answer heuristics were tuned on the same 66 questions. Treat the numbers as a regression baseline for this pipeline, not as a claim about arbitrary PDFs.
- The two-column report is written to the PDF row by row across both columns, a pattern some producers use; that is the case where plain extraction breaks. PDFs written column by column are easier for the baseline.
- Claude mode (chat with the Citations API, document review) is not part of this benchmark: not measured.

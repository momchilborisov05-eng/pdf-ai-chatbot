"""
Retrieval, citation and answer benchmark for the offline pipeline (pdfqa).

    python scripts/benchmark.py            # full run, writes examples/benchmark.json and .md
    python scripts/benchmark.py --quick    # CI smoke: fewer timing repeats, no 100-page stress test

Everything runs locally and deterministically: no API key, no network. The
corpus is eval/corpus (built by eval/build_corpus.py) with the ground truth in
eval/questions.json: for each question the accepted answers and the page they
are on, or no answer for questions the document does not answer.

Two pipelines are measured on the same questions with the same BM25 ranking and
the same answer extractor, so the difference is the PDF handling alone:

- layout: pdfqa (layout-aware parsing, structure-aware chunks, BM25F with
  section and table-header context).
- naive: pypdf extract_text, fixed 600-character windows with 100 overlap
  (never crossing a page), plain BM25, no section context.

Metrics
- recall@k / MRR: a retrieved chunk is relevant when it is on the gold page and
  contains one of the accepted answers.
- answer accuracy: the short answer contains an accepted answer (word-bounded,
  normalized) and the cited page is the gold page.
- exact match: the short answer, normalized, equals an accepted answer.
- citation correctness: the cited page is the gold page, the quote contains an
  accepted answer, and the quote is verified on that page.
- abstention: unanswerable questions answered with "not in the document";
  false abstentions are answerable questions the pipeline declined.
- verifier: quotes from the system are perturbed (moved to the wrong page, a
  number changed, a word swapped) and the checker must flag each one; the
  unmodified quotes must all pass.
- speed: ingest pages/s (parse + chunk + index) and query latency (retrieve +
  extract + verify) on this machine.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pypdf  # noqa: E402

from pdfqa import DocumentQA, raw_page_texts  # noqa: E402
from pdfqa.answer import answer  # noqa: E402
from pdfqa.chunks import Chunk  # noqa: E402
from pdfqa.index import BM25Index  # noqa: E402
from pdfqa.text import normalize_quote  # noqa: E402
from pdfqa.verify import PageIndex  # noqa: E402

CORPUS = ROOT / "eval" / "corpus"
QUESTIONS = ROOT / "eval" / "questions.json"
OUT = ROOT / "examples"
KS = (1, 3, 5)


# ---------- pipelines ----------


class NaiveQA:
    """Baseline: pypdf plain text, fixed character windows, the same BM25 and answerer."""

    def __init__(self, data: bytes, size: int = 600, overlap: int = 100):
        self.raw = raw_page_texts(data)
        chunks = []
        for page_no, text in enumerate(self.raw, start=1):
            start = 0
            while start < len(text):
                piece = text[start:start + size]
                chunks.append(Chunk(len(chunks), page_no, "text", piece, ""))
                if start + size >= len(text):
                    break
                start += size - overlap
        self.chunks = chunks
        self.index = BM25Index(chunks, bigram_bonus=0.0)
        self.pages = PageIndex(self.raw)

    def search(self, question: str, k: int):
        return self.index.search(question, k)

    def ask(self, question: str):
        result = answer(question, self.index)
        if not result.abstained:
            result.verified, result.page = self.pages.check(result.quote, result.page)
        return result


# ---------- scoring helpers ----------


def contains(haystack: str, needle: str) -> bool:
    h, n = normalize_quote(haystack), normalize_quote(needle)
    return bool(n) and f" {n} " in f" {h} "


def any_contains(text: str, answers: list[str]) -> bool:
    return any(contains(text, a) for a in answers)


def exact(text: str, answers: list[str]) -> bool:
    return any(normalize_quote(text) == normalize_quote(a) for a in answers)


def pct(x: float) -> float:
    return round(100 * x, 1)


def evaluate(pipelines: dict[str, object], questions: list[dict], kind: str) -> dict:
    hits_at = {k: 0 for k in KS}
    rr = []
    acc = em = cite = false_abstain = 0
    abstain_ok = 0
    by_kind: dict[str, list[int]] = {}
    failures = []
    answerable = [q for q in questions if q["answers"]]
    unanswerable = [q for q in questions if not q["answers"]]

    for q in questions:
        qa = pipelines[q["doc"]]
        result = qa.ask(q["question"])
        if not q["answers"]:
            abstain_ok += result.abstained
            if not result.abstained:
                failures.append({"question": q["question"], "expected": "abstain", "got": result.answer[:80],
                                 "page": result.page})
            continue
        ranked = qa.search(q["question"], max(KS))
        rank = next((i for i, h in enumerate(ranked, start=1)
                     if h.chunk.page == q["page"] and any_contains(h.chunk.text, q["answers"])), None)
        for k in KS:
            hits_at[k] += rank is not None and rank <= k
        rr.append(1 / rank if rank else 0.0)

        correct = (not result.abstained and result.page == q["page"]
                   and any_contains(result.answer, q["answers"]))
        acc += correct
        em += (not result.abstained and result.page == q["page"] and exact(result.answer, q["answers"]))
        cite += (not result.abstained and result.page == q["page"] and result.verified == "verified"
                 and any_contains(result.quote, q["answers"]))
        false_abstain += result.abstained
        by_kind.setdefault(q["kind"], [0, 0])
        by_kind[q["kind"]][0] += correct
        by_kind[q["kind"]][1] += 1
        if not correct:
            failures.append({"question": q["question"], "expected": q["answers"][0], "expected_page": q["page"],
                             "got": "(abstained)" if result.abstained else result.answer[:80],
                             "page": result.page})

    n = len(answerable)
    return {
        "pipeline": kind,
        "answerable": n,
        "unanswerable": len(unanswerable),
        "recall": {f"@{k}": pct(hits_at[k] / n) for k in KS},
        "mrr": round(statistics.fmean(rr), 3),
        "answer_accuracy": pct(acc / n),
        "exact_match": pct(em / n),
        "citation_correct": pct(cite / n),
        "false_abstention": pct(false_abstain / n),
        "abstention_on_unanswerable": pct(abstain_ok / len(unanswerable)) if unanswerable else None,
        "accuracy_by_kind": {k: {"correct": c, "total": t, "accuracy": pct(c / t)}
                             for k, (c, t) in sorted(by_kind.items())},
        "failures": failures,
    }


# ---------- verifier stress test ----------


def _change_number(quote: str) -> str | None:
    m = re.search(r"\d+", quote)
    if not m:
        return None
    digits = m.group(0)
    changed = str(int(digits) + 7) if len(digits) < 4 else digits[:-1] + str((int(digits[-1]) + 3) % 10)
    return quote[:m.start()] + changed + quote[m.end():]


def _swap_word(quote: str, vocabulary: list[str]) -> str | None:
    words = re.findall(r"[A-Za-z]{5,}", quote)
    if not words:
        return None
    target = max(words, key=len)
    replacement = next((v for v in vocabulary if v.lower() != target.lower() and len(v) >= 5), None)
    if replacement is None:
        return None
    return quote.replace(target, replacement, 1)


def verifier_test(docs: dict[str, DocumentQA], questions: list[dict]) -> dict:
    counts = {"exact_passed": 0, "exact_total": 0, "moved_detected": 0, "moved_page_fixed": 0, "moved_total": 0,
              "number_detected": 0, "number_total": 0, "word_detected": 0, "word_total": 0}
    for q in questions:
        if not q["answers"]:
            continue
        qa = docs[q["doc"]]
        result = qa.ask(q["question"])
        if result.abstained or result.verified != "verified":
            continue
        page_count = len(qa.parsed.pages)
        quote, page = result.quote, result.page
        counts["exact_total"] += 1
        counts["exact_passed"] += qa.pages.check(quote, page)[0] == "verified"
        if page_count > 1:
            wrong = page + 1 if page < page_count else page - 1
            status, fixed = qa.pages.check(quote, wrong)
            counts["moved_total"] += 1
            counts["moved_detected"] += status == "moved"
            counts["moved_page_fixed"] += status == "moved" and fixed == page
        altered = _change_number(quote)
        if altered and altered != quote:
            counts["number_total"] += 1
            counts["number_detected"] += qa.pages.check(altered, page)[0] == "not_found"
        vocabulary = sorted({w for c in qa.chunks for w in re.findall(r"[A-Za-z]{5,}", c.text)}, key=str.lower)
        swapped = _swap_word(quote, vocabulary[len(vocabulary) // 2:])
        if swapped and swapped != quote:
            counts["word_total"] += 1
            counts["word_detected"] += qa.pages.check(swapped, page)[0] == "not_found"

    def rate(a: str, b: str) -> float | None:
        return pct(counts[a] / counts[b]) if counts[b] else None

    return {
        **counts,
        "exact_pass_rate": rate("exact_passed", "exact_total"),
        "wrong_page_detected": rate("moved_detected", "moved_total"),
        "wrong_page_corrected": rate("moved_page_fixed", "moved_total"),
        "changed_number_detected": rate("number_detected", "number_total"),
        "swapped_word_detected": rate("word_detected", "word_total"),
    }


def two_column_quotes(doc: DocumentQA, questions: list[dict], name: str) -> dict:
    """On the two-column report: are the system's quotes on the page word for word, raw vs layout text?"""
    raw = [normalize_quote(p) for p in raw_page_texts(_read(name))]
    layout = [normalize_quote(p.text) for p in doc.parsed.pages]

    def verbatim(quote: str, pages: list[str], page: int) -> bool:
        return 1 <= page <= len(pages) and f" {normalize_quote(quote)} " in f" {pages[page - 1]} "

    total = raw_ok = layout_ok = 0
    for q in questions:
        if q["doc"] != name or not q["answers"]:
            continue
        result = doc.ask(q["question"])
        if result.abstained:
            continue
        total += 1
        raw_ok += verbatim(result.quote, raw, result.page)
        layout_ok += verbatim(result.quote, layout, result.page)
    return {"document": name, "quotes": total, "found_in_raw_pypdf_text": raw_ok, "found_in_layout_text": layout_ok}


# ---------- speed ----------


def _read(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


def ingest_speed(datas: dict[str, bytes], repeats: int) -> dict:
    out = {}
    for label, build in (("layout", DocumentQA.from_pdf), ("naive", NaiveQA)):
        rates = []
        pages = sum(len(pypdf.PdfReader(io.BytesIO(d)).pages) for d in datas.values())
        for _ in range(repeats):
            t0 = time.perf_counter()
            for d in datas.values():
                build(d)
            rates.append(pages / (time.perf_counter() - t0))
        out[label] = {"pages": pages, "pages_per_s": round(statistics.median(rates), 1)}
    return out


def stress_document() -> bytes:
    from eval.build_corpus import POLICY
    from pdfqa.pdfgen import render
    return render(POLICY * 20, header="Halden Group - Staff Travel Policy (stress copy)")


def query_latency(docs: dict, questions: list[dict], repeats: int) -> dict:
    times = []
    for _ in range(repeats):
        for q in questions:
            qa = docs[q["doc"]]
            t0 = time.perf_counter()
            qa.ask(q["question"])
            times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return {"queries": len(times), "p50_ms": round(statistics.median(times), 2),
            "p95_ms": round(times[int(0.95 * (len(times) - 1))], 2), "max_ms": round(times[-1], 2)}


# ---------- report ----------


def layout_stats(docs: dict[str, DocumentQA]) -> dict:
    return {name: {**qa.parsed.stats(), "chunks": len(qa.chunks),
                   "table_row_chunks": sum(c.kind == "table_row" for c in qa.chunks)}
            for name, qa in docs.items()}


def to_markdown(report: dict) -> str:
    lay, nai = report["layout"], report["naive"]
    lines = [
        "# Benchmark: retrieval, citations and offline answers",
        "",
        f"Measured {report['date']} on {report['machine']['platform']}, Python {report['machine']['python']}, "
        f"pypdf {report['machine']['pypdf']}. Command: `python scripts/benchmark.py`. "
        "Offline and deterministic: no API key, no network.",
        "",
        f"Corpus: {report['corpus']['documents']} PDFs, {report['corpus']['pages']} pages, "
        f"{report['corpus']['questions']} questions ({lay['answerable']} answerable, "
        f"{lay['unanswerable']} not answered by the document). See eval/build_corpus.py.",
        "",
        "## Retrieval and answers",
        "",
        "| Metric | pdfqa (layout-aware) | Naive baseline (pypdf text, 600-char windows) |",
        "|---|---:|---:|",
    ]
    for k in KS:
        lines.append(f"| Recall@{k} | {lay['recall'][f'@{k}']}% | {nai['recall'][f'@{k}']}% |")
    rows = [("MRR", "mrr", ""), ("Answer accuracy (answer + page)", "answer_accuracy", "%"),
            ("Exact match (short answer)", "exact_match", "%"),
            ("Citation correct (page + verified quote)", "citation_correct", "%"),
            ("False abstentions", "false_abstention", "%"),
            ("Abstained on unanswerable", "abstention_on_unanswerable", "%")]
    for label, key, unit in rows:
        lines.append(f"| {label} | {lay[key]}{unit} | {nai[key]}{unit} |")
    lines += ["", "Answer accuracy by question type (pdfqa / naive):", "",
              "| Type | pdfqa | Naive |", "|---|---:|---:|"]
    for kind, v in lay["accuracy_by_kind"].items():
        n = nai["accuracy_by_kind"].get(kind, {"correct": 0, "total": v["total"], "accuracy": 0})
        lines.append(f"| {kind} | {v['correct']}/{v['total']} ({v['accuracy']}%) | "
                     f"{n['correct']}/{n['total']} ({n['accuracy']}%) |")
    v = report["verifier"]
    tc = report["two_column_quotes"]
    lines += [
        "",
        "## Citation verifier",
        "",
        "Each verified quote from the pipeline is perturbed and re-checked against the page text.",
        "",
        "| Case | Result |",
        "|---|---:|",
        f"| Unmodified quotes pass | {v['exact_passed']}/{v['exact_total']} ({v['exact_pass_rate']}%) |",
        f"| Quote cited on the wrong page: flagged as moved | {v['moved_detected']}/{v['moved_total']} "
        f"({v['wrong_page_detected']}%) |",
        f"| ... and the correct page recovered | {v['moved_page_fixed']}/{v['moved_total']} "
        f"({v['wrong_page_corrected']}%) |",
        f"| A number in the quote changed: flagged | {v['number_detected']}/{v['number_total']} "
        f"({v['changed_number_detected']}%) |",
        f"| One word swapped: flagged | {v['word_detected']}/{v['word_total']} ({v['swapped_word_detected']}%) |",
        "",
        f"Two-column report ({tc['document']}): of {tc['quotes']} quotes, {tc['found_in_layout_text']} are found "
        f"verbatim in the layout-ordered page text and {tc['found_in_raw_pypdf_text']} in pypdf's plain text, "
        "whose content-stream order interleaves the two columns.",
        "",
        "## Speed",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Ingest, pdfqa (parse + chunk + index), corpus | "
        f"{report['speed']['ingest']['layout']['pages_per_s']} pages/s |",
        f"| Ingest, naive baseline, corpus | {report['speed']['ingest']['naive']['pages_per_s']} pages/s |",
    ]
    stress = report["speed"].get("stress")
    if stress:
        lines.append(f"| Ingest, pdfqa, {stress['pages']}-page document | {stress['pages_per_s']} pages/s |")
    else:
        lines.append("| Ingest, 100-page document | not measured (--quick) |")
    q = report["speed"]["query"]
    lines += [
        f"| Query latency p50 (retrieve + answer + verify) | {q['p50_ms']} ms |",
        f"| Query latency p95 | {q['p95_ms']} ms |",
        "",
        "## Remaining misses (pdfqa)",
        "",
    ]
    for f in lay["failures"]:
        lines.append(f"- {f['question']} expected `{f['expected']}`, got `{f['got']}`")
    lines += [
        "",
        "## Notes",
        "",
        "- The three generated documents and the questions were written for this benchmark, and the abstention "
        "threshold (0.38) and answer heuristics were tuned on the same 66 questions. Treat the numbers as a "
        "regression baseline for this pipeline, not as a claim about arbitrary PDFs.",
        "- The two-column report is written to the PDF row by row across both columns, a pattern some producers "
        "use; that is the case where plain extraction breaks. PDFs written column by column are easier for the "
        "baseline.",
        "- Claude mode (chat with the Citations API, document review) is not part of this benchmark: not measured.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true", help="fewer timing repeats, skip the 100-page stress test")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    names = sorted({q["doc"] for q in questions})
    datas = {n: _read(n) for n in names}
    docs = {n: DocumentQA.from_pdf(d) for n, d in datas.items()}
    naive = {n: NaiveQA(d) for n, d in datas.items()}

    repeats = 1 if args.quick else 5
    speed = {"ingest": ingest_speed(datas, repeats), "query": query_latency(docs, questions, repeats)}
    if not args.quick:
        big = stress_document()
        pages = len(pypdf.PdfReader(io.BytesIO(big)).pages)
        runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            DocumentQA.from_pdf(big)
            runs.append(pages / (time.perf_counter() - t0))
        speed["stress"] = {"pages": pages, "pages_per_s": round(statistics.median(runs), 1)}

    report = {
        "date": date.today().isoformat(),
        "machine": {"platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
                    "processor": platform.processor(), "python": platform.python_version(),
                    "pypdf": pypdf.__version__},
        "corpus": {"documents": len(names), "pages": sum(len(d.parsed.pages) for d in docs.values()),
                   "questions": len(questions)},
        "layout": evaluate(docs, questions, "layout"),
        "naive": evaluate(naive, questions, "naive"),
        "verifier": verifier_test(docs, questions),
        "two_column_quotes": two_column_quotes(docs["meridian-annual-report.pdf"], questions,
                                               "meridian-annual-report.pdf"),
        "layout_stats": layout_stats(docs),
        "speed": speed,
        "quick": args.quick,
    }
    args.out.mkdir(exist_ok=True)
    (args.out / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (args.out / "benchmark.md").write_text(to_markdown(report), encoding="utf-8")
    lay, nai = report["layout"], report["naive"]
    print(f"pdfqa: recall@1 {lay['recall']['@1']}%  recall@5 {lay['recall']['@5']}%  MRR {lay['mrr']}  "
          f"answer {lay['answer_accuracy']}%  citation {lay['citation_correct']}%  "
          f"abstain {lay['abstention_on_unanswerable']}%")
    print(f"naive: recall@1 {nai['recall']['@1']}%  recall@5 {nai['recall']['@5']}%  MRR {nai['mrr']}  "
          f"answer {nai['answer_accuracy']}%  citation {nai['citation_correct']}%  "
          f"abstain {nai['abstention_on_unanswerable']}%")
    print(f"ingest {speed['ingest']['layout']['pages_per_s']} pages/s, "
          f"query p50 {speed['query']['p50_ms']} ms p95 {speed['query']['p95_ms']} ms")
    print(f"wrote {args.out / 'benchmark.json'} and benchmark.md")
    return report


if __name__ == "__main__":
    main()

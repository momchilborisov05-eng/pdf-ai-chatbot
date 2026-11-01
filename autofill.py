"""
Questionnaire autofill without the browser: import the answer library once,
then drop PDFs into a folder and collect filled-in Excel answer sheets.

    python autofill.py import answers.xlsx           replace the answer library
    python autofill.py run questionnaire.pdf          answer one file
    python autofill.py watch inbox --out outbox       answer every PDF that lands in inbox/

The watcher moves each processed PDF to inbox/done/ (or inbox/failed/, with
the reason in a .txt next to it), so a file is never answered twice and the
folder can be a shared drive, a OneDrive/SharePoint sync folder or an email
rule's attachment folder. Answers, cost and history go to the same database
as the web app (PDF_CHAT_DB), under the command line's "local" owner.

DEMO_MODE=1 runs without an API key, with keyword matching instead of Claude.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

import anthropic

from pdf_chat import PdfError, PdfFile
from questionnaire import LibraryError, parse_library, run_autofill, to_xlsx
from review import ReviewError
from storage import DEFAULT_URL, LOCAL_OWNER, Storage


def make_client():
    if os.environ.get("DEMO_MODE") == "1":
        from web.demo import DemoClient
        return DemoClient()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY (or DEMO_MODE=1 to try it without one).")
    return anthropic.Anthropic(max_retries=3)


def import_library(store: Storage, path: Path) -> int:
    count = store.replace_library(LOCAL_OWNER, parse_library(path.name, path.read_bytes()))
    print(f"Imported {count} approved answers from {path.name}.")
    return count


def answer_file(client, store: Storage, path: Path, out_dir: Path, language: str) -> Path:
    """Answer one questionnaire and write <name>-answers.xlsx; returns the sheet's path."""
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(pdf.path), len(pdf.data), pdf.page_count)
    store.save_pages(doc_id, pdf.page_texts())
    result = None
    for kind, value in run_autofill(client, store, pdf, doc_id, LOCAL_OWNER, language):
        if kind == "done":
            result = value
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / f"{path.stem}-answers.xlsx"
    sheet.write_bytes(to_xlsx(result.report))
    g = result.report["grounding"]
    cost = f"${result.usage.cost_usd:.4f}" if result.usage.cost_usd is not None else "n/a"
    print(f"{path.name}: {g['answered']} answered, {g['partial']} partly, {g['needs_input']} need input "
          f"({cost}) -> {sheet}")
    return sheet


def watch(client, store: Storage, inbox: Path, out_dir: Path, language: str,
          interval: float, once: bool = False):
    """Answer every PDF in inbox/, then keep checking for new ones every `interval` seconds."""
    done, failed = inbox / "done", inbox / "failed"
    print(f"Watching {inbox} for questionnaires (Ctrl+C to stop). Answers go to {out_dir}.")
    while True:
        for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"):
            if not _settled(path):
                continue  # still being copied in
            try:
                answer_file(client, store, path, out_dir, language)
                _move(path, done)
            except (PdfError, LibraryError, ReviewError, anthropic.APIError) as e:
                print(f"{path.name}: failed: {e}", file=sys.stderr)
                target = _move(path, failed)
                target.with_suffix(".txt").write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")
        if once:
            return
        time.sleep(interval)


def _settled(path: Path, wait: float = 1.0) -> bool:
    """True once the file has stopped growing (a copy into the folder has finished)."""
    size = path.stat().st_size
    if time.time() - path.stat().st_mtime > wait:
        return True
    time.sleep(wait)
    return path.exists() and path.stat().st_size == size


def _move(path: Path, folder: Path) -> Path:
    folder.mkdir(exist_ok=True)
    target = folder / path.name
    if target.exists():
        target = folder / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
    shutil.move(str(path), target)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fill in questionnaires from a library of approved answers.")
    parser.add_argument("--db", default=os.environ.get("PDF_CHAT_DB", DEFAULT_URL), help="database URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="replace the answer library with an .xlsx or .csv file")
    p.add_argument("file", type=Path)

    for name, help_text in (("run", "answer one questionnaire PDF"),
                            ("watch", "answer every PDF that lands in a folder")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", type=Path)
        p.add_argument("--out", type=Path, help="folder for the answer sheets (default: next to the PDFs)")
        p.add_argument("--language", default="English", help="language of the answers")
        if name == "watch":
            p.add_argument("--interval", type=float, default=10, help="seconds between checks")
            p.add_argument("--once", action="store_true", help="process the folder once and exit")
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    store = Storage(args.db)
    try:
        if args.command == "import":
            import_library(store, args.file)
        elif args.command == "run":
            answer_file(make_client(), store, args.path, args.out or args.path.parent, args.language)
        else:
            args.path.mkdir(parents=True, exist_ok=True)
            watch(make_client(), store, args.path, args.out or args.path / "answers", args.language,
                  args.interval, args.once)
    except (PdfError, LibraryError, ReviewError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print()
    finally:
        store.close()


if __name__ == "__main__":
    main()

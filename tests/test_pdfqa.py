"""
Unit tests for the offline PDF understanding pipeline (pdfqa): layout parsing,
structure-aware chunks, BM25F retrieval, the extractive answerer and the
citation verifier. PDFs are generated with pdfqa.pdfgen, so nothing is downloaded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdfqa import DocumentQA, PageIndex, build_chunks, classify, from_page_texts, parse_pdf  # noqa: E402
from pdfqa.answer import _pick, _typed  # noqa: E402
from pdfqa.pdfgen import render  # noqa: E402
from pdfqa.text import normalize_number, normalize_quote, sentences, stem, tokens  # noqa: E402
from pdfqa.verify import found  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "eval" / "corpus"

LEFT = ("The northern depot handles all overnight maintenance for the regional fleet and employs "
        "two hundred and forty technicians across three shifts.")
RIGHT = ("Customer complaints fell sharply after the new timetable was introduced, mostly because "
         "connections at the central interchange became reliable.")


@pytest.fixture(scope="module")
def two_column_pdf() -> bytes:
    page = [("full", ("h1", "Annual Overview")), ("h2", "Depots"), ("p", LEFT), ("p", LEFT),
            ("colbreak",), ("h2", "Customers"), ("p", RIGHT), ("p", RIGHT)]
    # "rows" order writes the content stream left line, right line, left line...:
    # what a naive extractor reads straight across both columns.
    return render([page, page], columns=2, header="Meridian Rail - Annual Report", order="rows")


@pytest.fixture(scope="module")
def table_pdf() -> bytes:
    rows = [["City", "Daily allowance", "Hotel cap"],
            ["Vienna", "EUR 52", "EUR 160"],
            ["Sofia", "EUR 38", "EUR 95"],
            ["Berlin", "EUR 49", "EUR 150"]]
    page = [("h1", "Travel Policy"), ("h2", "Allowances"),
            ("p", "Allowances are paid per calendar day of travel."), ("table", rows)]
    return render([page])


@pytest.fixture(scope="module")
def meridian() -> DocumentQA:
    return DocumentQA.from_pdf((CORPUS / "meridian-annual-report.pdf").read_bytes())


# ---------- layout ----------

def test_two_columns_are_read_column_by_column(two_column_pdf):
    doc = parse_pdf(two_column_pdf)
    page = doc.pages[0]
    assert page.columns == 2
    text = " ".join(page.text.split())
    # Each column's paragraph survives intact, which a row-order extraction breaks.
    assert normalize_quote(LEFT) in normalize_quote(text)
    assert normalize_quote(RIGHT) in normalize_quote(text)
    assert text.index("Depots") < text.index("Customers")


def test_running_header_and_page_numbers_are_removed_as_furniture(two_column_pdf):
    doc = parse_pdf(two_column_pdf)
    for page in doc.pages:
        assert any("Meridian Rail" in f for f in page.furniture)
        assert any("Page" in f for f in page.furniture)
        assert "Meridian Rail - Annual Report" not in " ".join(b.text for b in page.blocks)


def test_headings_get_levels_from_font_size(two_column_pdf):
    doc = parse_pdf(two_column_pdf)
    headings = {b.text: b.level for b in doc.pages[0].blocks if b.kind == "heading"}
    assert headings["Annual Overview"] == 1
    assert headings["Depots"] == 2


def test_table_is_detected_with_a_bold_header_row(table_pdf):
    doc = parse_pdf(table_pdf)
    tables = [b for b in doc.pages[0].blocks if b.kind == "table"]
    assert len(tables) == 1
    assert tables[0].rows[0] == ["City", "Daily allowance", "Hotel cap"]
    assert tables[0].rows[2] == ["Sofia", "EUR 38", "EUR 95"]
    assert tables[0].header


def test_two_cell_table_is_not_mistaken_for_two_columns():
    rows = [["Interval", "Task"], ["Daily", "Check seal for leaks"], ["Monthly", "Clean the strainer"],
            ["Yearly", "Replace bearings"], ["Every 5 years", "Replace impeller"]]
    doc = parse_pdf(render([[("h1", "Maintenance"), ("table", rows)]]))
    page = doc.pages[0]
    assert page.columns == 1
    tables = [b for b in page.blocks if b.kind == "table"]
    assert tables and ["Monthly", "Clean the strainer"] in tables[0].rows


def test_words_hyphenated_at_line_ends_are_joined():
    long_word = "internationalisation"
    text = " ".join(["word"] * 14 + [long_word] + ["tail"] * 10)
    doc = parse_pdf(render([[("p", text)]]))
    assert long_word in doc.pages[0].text


def test_parsed_document_round_trips_through_a_dict(table_pdf):
    doc = parse_pdf(table_pdf)
    again = type(doc).from_dict(doc.to_dict())
    assert again.pages[0].text == doc.pages[0].text
    assert again.stats() == doc.stats()


def test_from_page_texts_makes_unpositioned_paragraphs():
    doc = from_page_texts(["First page text.\n\nSecond paragraph.", ""])
    assert len(doc.pages) == 2
    assert not doc.pages[0].positioned
    assert doc.stats()["unpositioned_pages"] == 2
    assert [b.kind for b in doc.pages[0].blocks] == ["paragraph", "paragraph"]


def test_broken_bytes_do_not_crash_the_parser():
    with pytest.raises(Exception):
        parse_pdf(b"not a pdf")


# ---------- chunks and retrieval ----------

def test_each_table_row_becomes_a_chunk_with_its_header(table_pdf):
    chunks = build_chunks(parse_pdf(table_pdf))
    rows = [c for c in chunks if c.kind == "table_row"]
    assert len(rows) == 3
    sofia = next(c for c in rows if c.cells[0] == "Sofia")
    assert sofia.header == ["City", "Daily allowance", "Hotel cap"]
    assert "Allowances" in sofia.section
    assert "Hotel cap" in sofia.context


def test_chunks_carry_their_section_path_and_page(two_column_pdf):
    chunks = build_chunks(parse_pdf(two_column_pdf))
    depot = next(c for c in chunks if "northern depot" in c.text)
    assert depot.section == "Annual Overview > Depots"
    assert {c.page for c in chunks} == {1, 2}


def test_search_ranks_the_matching_row_first(table_pdf):
    qa = DocumentQA.from_pdf(table_pdf)
    hits = qa.search("hotel cap in Sofia", k=3)
    assert hits[0].chunk.cells[0] == "Sofia"
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


# ---------- answering ----------

@pytest.mark.parametrize("question,kind", [
    ("How much is the annual fee?", "money"),
    ("What percentage of trains were on time?", "percent"),
    ("When does the contract end?", "date"),
    ("In what year was the depot opened?", "year"),
    ("How long is the notice period?", "duration"),
    ("How many stations are there?", "number"),
    ("Which audit firm signed the accounts?", "entity"),
    ("Who is the data protection officer?", "entity"),
    ("Describe the escalation procedure.", "span"),
])
def test_classifier_maps_questions_to_answer_types(question, kind):
    assert classify(question) == kind


def test_table_answer_is_the_cell_under_the_named_column(table_pdf):
    qa = DocumentQA.from_pdf(table_pdf)
    result = qa.ask("What is the hotel cap in Sofia?")
    assert result.answer == "EUR 95"
    assert result.page == 1
    assert result.verified == "verified"


def test_column_named_first_wins_a_tie(table_pdf):
    qa = DocumentQA.from_pdf(table_pdf)
    assert qa.ask("What is the daily allowance in Vienna?").answer == "EUR 52"


def test_value_is_taken_from_the_clause_that_matches_the_question():
    sentence = "EUR 210 million is reserved for rolling stock and EUR 95 million for signalling."
    matches = _typed("money", sentence)
    picked = _pick(matches, sentence, set(tokens("budget for signalling")), "budget for signalling", None)
    assert picked.group(0) == "EUR 95 million"


def test_answers_from_the_bundled_two_column_report(meridian):
    result = meridian.ask("How much of the capital programme is reserved for new rolling stock?")
    assert not result.abstained
    assert result.answer == "EUR 210 million"  # not the EUR 95 million for signalling next to it
    assert result.page == 1
    assert result.verified == "verified"
    assert meridian.ask("Which firm audited the financial statements?").answer == "Calder & Wynn LLP"


def test_abstains_when_the_document_does_not_answer(meridian):
    result = meridian.ask("What is the airline baggage allowance for cabin crew?")
    assert result.abstained
    assert result.answer == "" and result.page is None


def test_outline_and_structure_report_what_was_parsed(meridian):
    structure = meridian.structure()
    assert structure["stats"]["pages"] == 4
    assert structure["stats"]["two_column_pages"] >= 1
    assert structure["outline"] and structure["outline"][0]["page"] == 1
    assert {"page", "columns", "furniture", "blocks"} <= structure["pages"][0].keys()


# ---------- citation verification ----------

PAGE = normalize_quote("The notice period is 30 days. Fees rise by 4.2% each year from 1 March 2027.")


def test_verbatim_quote_is_found():
    assert found("the notice period is 30 days", PAGE)


def test_changed_number_is_rejected():
    assert not found("The notice period is 60 days.", PAGE)


def test_swapped_word_is_rejected():
    assert not found("Fees fall by 4.2% each year from 1 March 2027.", PAGE)


def test_ellipsis_allows_a_gap():
    assert found("The notice period is ... from 1 March 2027", PAGE)


def test_words_split_by_extraction_still_match():
    assert found("configuration of the pump station", normalize_quote("con figuration of the pump station"))


def test_quote_interleaved_with_the_other_column_is_found():
    page = normalize_quote("the depot handles LEFT overnight maintenance RIGHT for the regional fleet today")
    assert found("the depot handles overnight maintenance for the regional fleet", page)
    assert not found("the depot handles overnight repairs for the regional fleet", page)


def test_page_index_corrects_a_wrong_page():
    index = PageIndex(["Nothing here.", "The warranty lasts 24 months."])
    assert index.check("The warranty lasts 24 months.", 1) == ("moved", 2)
    assert index.check("The warranty lasts 24 months.", 2) == ("verified", 2)
    assert index.check("The warranty lasts 36 months.", 2) == ("not_found", 2)
    assert PageIndex(["", ""]).check("anything", 1) == ("unchecked", 1)


def test_quote_matches_any_rendering_of_the_page():
    index = PageIndex([["layout order text about pumps", "raw order text about valves"]])
    assert index.on_page("raw order text about valves", 1)
    assert index.on_page("layout order text about pumps", 1)


# ---------- text helpers ----------

@pytest.mark.parametrize("words", [("booking", "bookings", "booked"), ("rate", "rated", "rates", "rating"),
                                   ("policy", "policies")])
def test_stemmer_merges_word_forms(words):
    assert len({stem(w) for w in words}) == 1


def test_stemmer_leaves_numbers_and_short_words_alone():
    assert stem("4.2") == "4.2" and stem("bus") == "bus"


def test_numbers_keep_their_value():
    assert normalize_number("1,250.00") == "1250.00"
    assert "1250" in tokens("a total of 1,250 units")


def test_sentence_splitter_keeps_decimals_and_abbreviations():
    parts = sentences("Growth was 4.2 percent, e.g. in Sofia. The fleet grew.")
    assert parts == ["Growth was 4.2 percent, e.g. in Sofia.", "The fleet grew."]

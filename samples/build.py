"""
Builds the sample PDFs in this folder (no dependencies beyond the standard library):

    python samples/build.py

- service-agreement.pdf: a supplier contract with the clauses a review should catch
  (automatic renewal, uncapped liability for the client, penalties, IP kept by the supplier).
- invoice-2026-114.pdf: an invoice whose total doesn't add up and that announces
  "new bank details", a common payment-fraud pattern.
- supplier-questionnaire.pdf: a customer's security and compliance questionnaire, to be
  answered from answer-library.csv (some questions are covered, some partly, some not at all).

The files are fictional. text_pdf() is also used by the tests to make PDFs with real text.
"""

import textwrap
from pathlib import Path

HERE = Path(__file__).parent


def text_pdf(pages: list[str], width: int = 95, lines_per_page: int = 60) -> bytes:
    """A minimal PDF with Helvetica text; long pages flow onto the next page."""
    laid_out = []
    for page in pages:
        lines = []
        for paragraph in page.split("\n"):
            lines += textwrap.wrap(paragraph, width) or [""]
        laid_out += [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]

    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for lines in laid_out:
        body = "".join(f"({_escape(line)}) Tj T*\n" for line in lines)
        stream = f"BT /F1 10 Tf 13 TL 50 800 Td\n{body}ET".encode("cp1252")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(len(objects) + 1)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        data = obj if isinstance(obj, bytes) else obj.encode("latin-1")
        out += b"%d 0 obj\n" % number + data + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


SERVICE_AGREEMENT = [
    """IT SERVICES AGREEMENT No. 2026-047

This IT Services Agreement (the "Agreement") is entered into on 3 November 2026 between \
Northwind Cloud Ltd., registered in Sofia, Bulgaria, company number 204551873 (the "Supplier"), \
and Brightline Retail AD, company number 175664213 (the "Client").

1. Subject
1.1. The Supplier shall provide hosting, monitoring and maintenance of the Client's e-commerce platform \
(the "Services") as described in Annex 1.
1.2. The Supplier may change the scope of the Services or the subcontractors used to provide them at its \
own discretion by notifying the Client by e-mail.

2. Term and renewal
2.1. This Agreement enters into force on 15 November 2026 and is concluded for an initial term of 24 months.
2.2. The Agreement shall automatically renew for successive periods of 24 months unless either party \
gives written notice of non-renewal at least 120 days before the end of the current term.
2.3. The Client may terminate this Agreement before the end of the term only upon payment of all fees \
remaining for the current term.

3. Fees and payment
3.1. The Client shall pay a monthly fee of EUR 4,800 excluding VAT, invoiced in advance.
3.2. Invoices are payable within 10 days of the invoice date.
3.3. Late payments bear interest of 2% per month on the outstanding amount.
3.4. The Supplier may increase the fees once per calendar year by notice to the Client.""",
    """4. Service levels
4.1. The Supplier shall use reasonable efforts to keep the platform available.
4.2. Planned maintenance may be carried out at any time with 24 hours' notice.

5. Liability
5.1. The Supplier's total liability under this Agreement shall not exceed the fees paid by the Client in \
the one month preceding the event giving rise to the claim.
5.2. The Client shall be liable without limitation for any damage caused to the Supplier, including lost \
profits.
5.3. The Client shall indemnify the Supplier against all claims of third parties related to the content \
hosted on the platform.

6. Intellectual property
6.1. All software, configurations and scripts developed by the Supplier under this Agreement, including \
those paid for by the Client, remain the exclusive property of the Supplier.

7. Exclusivity
7.1. During the term of this Agreement the Client shall not engage any other provider for hosting or \
maintenance services.
7.2. In case of breach of clause 7.1 the Client shall pay a penalty of EUR 50,000.

8. Confidentiality
8.1. Each party shall keep confidential all information received from the other party.

9. Governing law
9.1. This Agreement is governed by the laws of the Republic of Bulgaria. Disputes shall be resolved by \
the competent court in Sofia.

10. Kick-off
10.1. The Client shall provide administrator access to the current platform by 20 November 2026.
10.2. The first monthly report shall be delivered by 5 January 2027.

Supplier: Northwind Cloud Ltd.                Client: Brightline Retail AD""",
]

INVOICE = [
    """INVOICE No. 2026-114

Supplier: Northwind Cloud Ltd., Sofia, Bulgaria, VAT No. BG204551873
Buyer: Brightline Retail AD, Plovdiv, Bulgaria
Issue date: 2026-11-02
Due date: 2026-11-05

Description | Quantity | Unit price (EUR) | Amount (EUR)
Platform hosting, November 2026 | 1 | 4,800.00 | 4,800.00
Emergency support hours | 12 | 85.00 | 1,020.00
SSL certificate renewal | 2 | 150.00 | 300.00
Data migration | 1 | 2,500.00 | 2,500.00

Subtotal: 8,620.00 EUR
VAT 20%: 1,724.00 EUR
Total due: 10,544.00 EUR

IMPORTANT: Please note that our bank details have changed. Pay only to the new account:
IBAN LT12 3250 0123 4567 8901, beneficiary Northwind Payments UAB.
The previous account BG80 BNBG 9661 1020 3456 78 is no longer in use.

Payment terms: bank transfer, 3 days.""",
]

QUESTIONNAIRE = [
    """SUPPLIER SECURITY AND COMPLIANCE QUESTIONNAIRE
Issued by: Brightline Retail AD, Procurement Department
Supplier: Northwind Cloud Ltd.
Please answer every question. Attach evidence where requested. Return by 2026-11-20.

Section 1. Company and certifications
1.1 What is the full legal name and registration number of your company?
1.2 Is your company certified to ISO/IEC 27001? Please state the certification body and the expiry date.
1.3 Do you hold a SOC 2 Type II report? If yes, for which period?
1.4 How many employees does your company have?

Section 2. Data protection
2.1 In which countries is our data stored and processed?
2.2 Is customer data encrypted at rest and in transit? Describe the algorithms used.
2.3 Do you use subprocessors? Provide the current list.
2.4 Will you sign our Data Processing Agreement under Article 28 GDPR?
2.5 How quickly do you notify customers of a personal data breach?""",
    """Section 3. Operations and continuity
3.1 What uptime do you guarantee, and what service credits apply when it is missed?
3.2 How often are backups taken, and how long are they kept?
3.3 What are your recovery time objective (RTO) and recovery point objective (RPO)?
3.4 When was your last disaster recovery test, and what was the result?
3.5 Do you perform annual penetration tests by an independent company?

Section 4. People and access
4.1 Is multi-factor authentication enforced for all staff with access to customer data?
4.2 Are background checks performed on employees before hiring?
4.3 How often do employees receive security awareness training?
4.4 Describe your process for removing access when an employee leaves.

Section 5. Insurance
5.1 Do you carry cyber liability insurance? State the insurer and the amount of cover.""",
]

LIBRARY = HERE / "answer-library.csv"


if __name__ == "__main__":
    (HERE / "service-agreement.pdf").write_bytes(text_pdf(SERVICE_AGREEMENT))
    (HERE / "invoice-2026-114.pdf").write_bytes(text_pdf(INVOICE))
    (HERE / "supplier-questionnaire.pdf").write_bytes(text_pdf(QUESTIONNAIRE))
    print("Wrote service-agreement.pdf, invoice-2026-114.pdf and supplier-questionnaire.pdf")

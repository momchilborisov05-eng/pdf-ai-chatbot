"""
Builds the retrieval and citation evaluation set:

    python eval/build_corpus.py

Writes eval/corpus/*.pdf and eval/questions.json. Three documents are generated
here (fictional content, written for this benchmark) and two are the existing
samples from samples/:

- meridian-annual-report.pdf: 4 pages, two columns, running header and footer,
  hyphenated line ends, a full-width table. Text is written to the PDF row by
  row across both columns, as some producers do, so content-stream order
  interleaves the columns.
- hx200-pump-manual.pdf: 4 pages, numbered headings, a spec table, a maintenance
  interval table and a troubleshooting table.
- travel-policy.pdf: 5 pages of similar-sounding policy sections (distractors)
  and a per-city rate table.
- service-agreement.pdf, invoice-2026-114.pdf: plain single-column samples, the
  invoice with a pipe-delimited table.

Each question has the accepted answers and the page they are on. Questions with
"answers": [] are not answered by the document: the right output is to abstain.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pdfqa.pdfgen import render  # noqa: E402

CORPUS = HERE / "corpus"

MERIDIAN = [
    [
        ("full", ("h1", "Meridian Rail Annual Report 2025")),
        ("h2", "Chair's letter"),
        ("p", "2025 was the year our network returned to growth. Passenger journeys rose to 112.4 million, "
              "up 7.8 percent on the previous year, and every one of our four lines carried more people "
              "than in 2019, the last year before the pandemic."),
        ("p", "The board approved a capital programme of EUR 640 million for 2026 to 2030, of which EUR 210 "
              "million is reserved for new rolling stock and EUR 95 million for the resignalling of the "
              "Harbour Line. The programme is financed from operating cash flow and a green bond."),
        ("p", "I would like to thank our employees for an extraordinary year, and our passengers for "
              "their patience during the engineering works on the western branch."),
        ("colbreak",),
        ("h2", "Operations"),
        ("p", "Punctuality, measured as trains arriving within five minutes of schedule, reached 94.2 "
              "percent, the highest since the electrification of the western branch in 2011."),
        ("p", "Cancellations fell to 1.3 percent of planned services. The largest single cause of delay "
              "remained signalling failures on the Harbour Line, which accounted for 31 percent of all "
              "delay minutes during the year."),
        ("p", "We opened two new stations, Kestrel Park and Old Mill Road, bringing the network to 64 "
              "stations. Step-free access is now available at 41 of them, and the remaining stations "
              "will be upgraded by the end of 2029 under the accessibility programme."),
        ("p", "Timetable changes in December added late evening services on weekdays and a fifteen "
              "minute frequency on Sunday mornings across the whole network."),
    ],
    [
        ("h2", "Safety"),
        ("p", "The rate of passenger injuries fell to 0.42 per million journeys. There were no fatalities "
              "among passengers or staff for the third consecutive year."),
        ("p", "Our safety training programme reached 2,350 employees, with particular attention to track "
              "access procedures, platform-train interface risks and fatigue management for drivers."),
        ("h2", "People"),
        ("p", "At year end Meridian employed 3,912 people, of whom 29 percent were women. Staff turnover "
              "was 8.6 percent, down from 10.1 percent a year earlier."),
        ("p", "The apprenticeship scheme took on 85 new apprentices, mostly in electrical maintenance "
              "and signalling, and a new driver academy opened in the Riverside depot."),
        ("colbreak",),
        ("h2", "Environment"),
        ("p", "Traction energy consumption fell to 19.6 kWh per train kilometre thanks to regenerative "
              "braking on the new fleet. All electricity purchased since March 2025 comes from certified "
              "renewable sources."),
        ("p", "Diesel units still operate the Northern Loop; they will be replaced by battery trains by "
              "2028. Noise barriers were installed along 3.4 kilometres of track near residential areas."),
        ("p", "Water consumption at depots fell by a fifth after the installation of a recycling plant "
              "for train washing in the Riverside depot."),
    ],
    [
        ("full", ("h2", "Ridership by line")),
        ("table", [["Line", "Journeys 2025", "Change vs 2024", "Punctuality"],
                   ["Harbour Line", "38.9 million", "+6.2%", "91.8%"],
                   ["Western Branch", "31.5 million", "+9.4%", "95.6%"],
                   ["Northern Loop", "22.7 million", "+4.1%", "96.3%"],
                   ["Airport Express", "19.3 million", "+12.5%", "97.1%"]]),
        ("h2", "Fares and revenue"),
        ("p", "Fare revenue reached EUR 486 million. The average fare per journey was EUR 4.32, slightly "
              "lower than in 2024 because more passengers travelled on discounted monthly passes."),
        ("p", "Non-fare income from advertising, retail units in stations and property rentals added "
              "EUR 57 million, and the regional government's public service compensation was EUR 132 "
              "million, unchanged in real terms."),
        ("p", "Contactless payment now accounts for 61 percent of single tickets. Paper tickets will be "
              "withdrawn from sale at staffed stations during 2027."),
        ("colbreak",),
        ("h2", "Customer satisfaction"),
        ("p", "The independent passenger survey gave Meridian an overall satisfaction score of 82 out of "
              "100. Cleanliness and information during disruption remain the weakest scores."),
        ("p", "Complaints fell to 38 per 100,000 journeys, and 96 percent of them were answered within "
              "the ten working days required by our passenger charter."),
    ],
    [
        ("h2", "Financial results"),
        ("p", "Operating profit before depreciation (EBITDA) was EUR 118 million, a margin of 17.6 percent. "
              "Net profit was EUR 23 million after depreciation of EUR 81 million and interest of EUR 14 "
              "million."),
        ("p", "Net debt stood at EUR 402 million at 31 December 2025, equal to 3.4 times EBITDA. The green "
              "bond issued in June was three times oversubscribed."),
        ("h2", "Outlook for 2026"),
        ("p", "We expect passenger journeys of between 116 and 119 million in 2026. The first of 24 new "
              "battery-electric trains will enter service on the Northern Loop in the autumn of 2027."),
        ("p", "Fares will rise by 2.9 percent on 1 March 2026, below the rate of inflation, and the price "
              "of the monthly pass will be frozen for students."),
        ("colbreak",),
        ("h2", "Auditor"),
        ("p", "The financial statements were audited by Calder & Wynn LLP, who issued an unqualified "
              "opinion on 14 March 2026."),
        ("p", "The full financial statements, the corporate governance report and the sustainability "
              "report are published separately on the investor relations website."),
    ],
]

PUMP = [
    [
        ("h1", "HX-200 Circulation Pump"),
        ("h2", "1 Safety"),
        ("p", "Read this manual before installing the pump. Only qualified electricians may connect the "
              "pump to the mains supply."),
        ("p", "Disconnect the power supply and wait at least 5 minutes before opening the terminal box, "
              "because the frequency converter capacitors stay charged."),
        ("p", "The pump must not run dry. Running without fluid for more than 30 seconds damages the "
              "mechanical seal."),
        ("h2", "2 Technical data"),
        ("table", [["Parameter", "Value", "Unit"],
                   ["Rated power", "2.2", "kW"],
                   ["Supply voltage", "400", "V"],
                   ["Maximum flow", "36", "m3/h"],
                   ["Maximum head", "28", "m"],
                   ["Maximum operating pressure", "10", "bar"],
                   ["Fluid temperature range", "-10 to 110", "C"],
                   ["Weight", "41", "kg"],
                   ["Sound pressure level", "58", "dB(A)"]]),
        ("p", "The values apply to water at 20 C. For water and glycol mixtures, the maximum flow is "
              "reduced; see the selection software."),
    ],
    [
        ("h2", "3 Installation"),
        ("h2", "3.1 Location"),
        ("p", "Install the pump indoors in a frost-free, ventilated room. Leave at least 300 mm of free "
              "space above the motor for cooling air and service access."),
        ("h2", "3.2 Pipework"),
        ("p", "Fit isolating valves on both sides of the pump. The suction pipe must rise continuously "
              "towards the pump to avoid air pockets."),
        ("p", "Tighten the flange bolts crosswise to a torque of 45 Nm. Support the pipes so that their "
              "weight does not rest on the pump housing."),
        ("h2", "3.3 Electrical connection"),
        ("p", "Protect the supply with a 16 A type C circuit breaker. The motor has built-in thermal "
              "protection and does not require an external overload relay."),
        ("p", "Earth the pump before connecting the phases. Use cable glands rated IP55 or better."),
    ],
    [
        ("h2", "4 Commissioning"),
        ("p", "Fill and vent the pump before the first start. Open the vent screw until fluid without "
              "bubbles comes out, then close it again."),
        ("p", "Check the direction of rotation: the motor fan must turn clockwise when seen from the fan "
              "end. Swap two phases if it turns the wrong way."),
        ("h2", "5 Maintenance"),
        ("h2", "5.1 Maintenance intervals"),
        ("table", [["Task", "Interval"],
                   ["Check for leaks at the shaft seal", "Every 3 months"],
                   ["Clean the motor cooling fins", "Every 6 months"],
                   ["Replace the mechanical seal", "Every 16,000 operating hours"],
                   ["Replace the motor bearings", "Every 30,000 operating hours"]]),
        ("p", "Record every maintenance task in the log at the back of this manual."),
    ],
    [
        ("h2", "6 Troubleshooting"),
        ("table", [["Fault", "Cause", "Remedy"],
                   ["Pump does not start", "Thermal protection tripped", "Let the motor cool for 20 minutes"],
                   ["Noise from the pump", "Cavitation", "Increase the inlet pressure"],
                   ["Leak at the shaft", "Worn mechanical seal", "Replace the seal"]]),
        ("h2", "7 Warranty"),
        ("p", "The pump is covered by a warranty of 24 months from the date of installation, or 30 months "
              "from the date of delivery, whichever ends first."),
        ("p", "The warranty does not cover wear parts such as the mechanical seal and the bearings."),
        ("p", "Spare parts are available for at least 10 years after production of the model ends."),
    ],
]

POLICY = [
    [
        ("h1", "Staff Travel Policy"),
        ("h2", "1 Purpose and scope"),
        ("p", "This policy applies to all employees of Halden Group and its subsidiaries who travel on "
              "company business, and to contractors whose contract refers to it."),
        ("p", "Travel must be approved in advance by the traveller's line manager in the expense system. "
              "Trips outside the European Economic Area also need approval from a division director."),
        ("h2", "2 Booking"),
        ("p", "All flights, trains and hotels must be booked through the company travel agency, Northstar "
              "Travel, at least 14 days before departure where possible."),
        ("p", "Bookings made directly with airlines or hotels are reimbursed only if they are at least 10 "
              "percent cheaper than the agency's offer for the same trip."),
    ],
    [
        ("h2", "3 Air travel"),
        ("p", "Economy class is the standard for all flights. Premium economy may be booked for flights "
              "with a scheduled duration of more than 6 hours. Business class requires approval from the "
              "chief financial officer."),
        ("p", "Checked baggage is reimbursed for trips longer than three nights."),
        ("h2", "4 Rail and ground transport"),
        ("p", "Rail is preferred over air for journeys under 4 hours door to door. First class rail travel "
              "is allowed for journeys longer than 2 hours."),
        ("p", "Taxis are reimbursed between 22:00 and 06:00, or when public transport is not reasonably "
              "available. Ride-sharing is treated as a taxi."),
        ("p", "Employees who use their own car on business are reimbursed EUR 0.32 per kilometre, which "
              "covers fuel, insurance and wear."),
    ],
    [
        ("h2", "5 Accommodation"),
        ("p", "Hotels are booked up to the city rate in the table below, per night, including breakfast "
              "and taxes."),
        ("table", [["City", "Hotel rate per night", "Daily meal allowance"],
                   ["Berlin", "EUR 160", "EUR 52"],
                   ["London", "EUR 230", "EUR 64"],
                   ["Paris", "EUR 210", "EUR 60"],
                   ["Sofia", "EUR 110", "EUR 38"],
                   ["Stockholm", "EUR 190", "EUR 58"],
                   ["Other cities", "EUR 140", "EUR 45"]]),
        ("p", "If the rate is exceeded because of a trade fair or a major event, the traveller must attach "
              "two alternative quotes to the expense claim."),
    ],
    [
        ("h2", "6 Meals and allowances"),
        ("p", "The daily meal allowance in the table in section 5 covers breakfast, lunch, dinner and "
              "incidental expenses. When a meal is provided free of charge, the allowance is reduced by "
              "20 percent for breakfast and 35 percent each for lunch and dinner."),
        ("p", "Alcohol is never reimbursed, except when hosting external guests with prior approval."),
        ("h2", "7 Expense claims"),
        ("p", "Expense claims must be submitted within 30 days of the end of the trip, with an itemised "
              "receipt for every expense above EUR 25."),
        ("p", "Claims are paid with the next monthly salary when approved by the 20th day of the month."),
        ("p", "Lost receipts must be replaced by a signed declaration; no more than two declarations are "
              "accepted per calendar year."),
    ],
    [
        ("h2", "8 Insurance and safety"),
        ("p", "All business travellers are covered by the group travel insurance with Aurora Assurance, "
              "policy number TRV-55-0192. Medical emergencies abroad are covered up to EUR 2 million."),
        ("p", "Travellers must register every trip outside the European Economic Area with the security "
              "desk at least 48 hours before departure."),
        ("h2", "9 Sustainability"),
        ("p", "Each division has an annual travel carbon budget. Video calls should replace internal "
              "meetings that would require a flight of less than 90 minutes."),
        ("h2", "10 Policy owner"),
        ("p", "This policy is owned by the Group Finance department and is reviewed every two years. "
              "Questions go to the travel policy mailbox."),
    ],
]


def q(doc, question, answers, page, kind="text"):
    return {"doc": doc, "question": question, "answers": answers, "page": page, "kind": kind}


M, P, T, S, INV = ("meridian-annual-report.pdf", "hx200-pump-manual.pdf", "travel-policy.pdf",
                 "service-agreement.pdf", "invoice-2026-114.pdf")

QUESTIONS = [
    q(M, "How many passenger journeys did Meridian carry in 2025?", ["112.4 million"], 1, "two-column"),
    q(M, "What overall punctuality did Meridian reach?", ["94.2 percent"], 1, "two-column"),
    q(M, "What share of delay minutes was caused by signalling failures on the Harbour Line?",
      ["31 percent"], 1, "two-column"),
    q(M, "How many stations does the network have now?", ["64"], 1, "two-column"),
    q(M, "How much of the capital programme is reserved for new rolling stock?", ["EUR 210 million"], 1,
      "two-column"),
    q(M, "How many people did Meridian employ at year end?", ["3,912"], 2, "two-column"),
    q(M, "What was the staff turnover rate?", ["8.6 percent"], 2, "two-column"),
    q(M, "How much traction energy was used per train kilometre?", ["19.6 kWh"], 2, "two-column"),
    q(M, "By when will the diesel units on the Northern Loop be replaced?", ["2028"], 2, "two-column"),
    q(M, "How many journeys did the Airport Express carry in 2025?", ["19.3 million"], 3, "table"),
    q(M, "What was the punctuality of the Western Branch?", ["95.6%"], 3, "table"),
    q(M, "What was the average fare per journey?", ["EUR 4.32"], 3, "two-column"),
    q(M, "How much non-fare income came from advertising, retail units and property rentals?",
      ["EUR 57 million"], 3, "two-column"),
    q(M, "What was net debt at 31 December 2025?", ["EUR 402 million"], 4, "two-column"),
    q(M, "What EBITDA margin did Meridian report?", ["17.6 percent"], 4, "two-column"),
    q(M, "When will fares rise?", ["1 March 2026"], 4, "two-column"),
    q(M, "Which firm audited the financial statements?", ["Calder & Wynn LLP"], 4, "two-column"),
    q(M, "How many new battery-electric trains will enter service?", ["24"], 4, "two-column"),
    q(M, "What is the chief executive's annual salary?", [], None, "unanswerable"),
    q(M, "How many buses does Meridian operate in the suburbs?", [], None, "unanswerable"),

    q(P, "What is the rated power of the HX-200?", ["2.2 kW"], 1, "table"),
    q(P, "What is the maximum operating pressure?", ["10 bar"], 1, "table"),
    q(P, "What is the weight of the pump?", ["41 kg"], 1, "table"),
    q(P, "How long must you wait before opening the terminal box?", ["5 minutes"], 1),
    q(P, "How long can the pump run dry before the mechanical seal is damaged?", ["30 seconds"], 1),
    q(P, "How much free space is needed above the motor?", ["300 mm"], 2),
    q(P, "What torque should the flange bolts be tightened to?", ["45 Nm"], 2),
    q(P, "Which circuit breaker should protect the supply?", ["16 A type C"], 2),
    q(P, "Which way must the motor fan turn?", ["clockwise"], 3),
    q(P, "How often should the mechanical seal be replaced?", ["16,000 operating hours"], 3, "table"),
    q(P, "How often should the motor cooling fins be cleaned?", ["6 months"], 3, "table"),
    q(P, "What is the remedy for noise caused by cavitation?", ["Increase the inlet pressure"], 4, "table"),
    q(P, "How long is the warranty from the date of installation?", ["24 months"], 4),
    q(P, "For how many years are spare parts available?", ["10 years"], 4),
    q(P, "What is the list price of the HX-200 pump?", [], None, "unanswerable"),
    q(P, "Is the pump certified for use with drinking water?", [], None, "unanswerable"),

    q(T, "How far in advance must trips be booked through the travel agency?", ["14 days"], 1),
    q(T, "Which travel agency must be used for bookings?", ["Northstar Travel"], 1),
    q(T, "When may premium economy be booked?", ["more than 6 hours"], 2),
    q(T, "Who must approve business class flights?", ["chief financial officer"], 2),
    q(T, "What is the mileage rate for using your own car?", ["EUR 0.32"], 2),
    q(T, "Above what journey time is first class rail allowed?", ["2 hours"], 2),
    q(T, "What is the hotel rate per night in London?", ["EUR 230"], 3, "table"),
    q(T, "What is the daily meal allowance in Sofia?", ["EUR 38"], 3, "table"),
    q(T, "By how much is the allowance reduced when lunch is provided?", ["35 percent"], 4),
    q(T, "Within how many days must expense claims be submitted?", ["30 days"], 4),
    q(T, "Above what amount is an itemised receipt required?", ["EUR 25"], 4),
    q(T, "How many lost-receipt declarations are accepted per calendar year?", ["two"], 4),
    q(T, "Up to what amount are medical emergencies abroad covered?", ["EUR 2 million"], 5),
    q(T, "What is the travel insurance policy number?", ["TRV-55-0192"], 5),
    q(T, "How often is the travel policy reviewed?", ["every two years"], 5),
    q(T, "Can employees bring their family on business trips?", [], None, "unanswerable"),
    q(T, "How many days of parental leave do employees get?", [], None, "unanswerable"),

    q(S, "What is the monthly fee?", ["EUR 4,800"], 1),
    q(S, "How long is the initial term of the agreement?", ["24 months"], 1),
    q(S, "How many days before the end of the term must notice of non-renewal be given?", ["120 days"], 1),
    q(S, "What interest is charged on late payments?", ["2% per month"], 1),
    q(S, "What penalty applies for breaching the exclusivity clause?", ["EUR 50,000"], 2),
    q(S, "By what date must administrator access to the platform be provided?", ["20 November 2026"], 2),
    q(S, "Which laws govern the agreement?", ["Republic of Bulgaria"], 2),
    q(S, "What is the supplier's response time for critical incidents?", [], None, "unanswerable"),

    q(INV, "What is the total due on the invoice?", ["10,544.00 EUR"], 1),
    q(INV, "What is the due date of the invoice?", ["2026-11-05"], 1),
    q(INV, "How many emergency support hours were billed?", ["12"], 1, "table"),
    q(INV, "What is the new IBAN for payment?", ["LT12 3250 0123 4567 8901"], 1),
    q(INV, "What VAT rate is applied on the invoice?", ["20%"], 1),
]

GENERATED = {
    M: dict(pages=MERIDIAN, columns=2, header="Meridian Rail - Annual Report 2025", order="rows"),
    P: dict(pages=PUMP, columns=1, header="Hydrex HX-200 - Installation and Maintenance Manual",
            footer="HXM-200-EN rev. C   |   page {n}"),
    T: dict(pages=POLICY, columns=1, header="Halden Group - Staff Travel Policy (v3.2, effective 1 January 2026)"),
}


def build() -> list[Path]:
    CORPUS.mkdir(exist_ok=True)
    paths = []
    for name, spec in GENERATED.items():
        spec = dict(spec)
        data = render(spec.pop("pages"), **spec)
        path = CORPUS / name
        path.write_bytes(data)
        paths.append(path)
    for name in (S, INV):
        shutil.copyfile(ROOT / "samples" / name, CORPUS / name)
        paths.append(CORPUS / name)
    (HERE / "questions.json").write_text(json.dumps(QUESTIONS, indent=1, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    return paths


if __name__ == "__main__":
    for p in build():
        print(f"wrote {p.relative_to(ROOT)}")
    print(f"wrote eval/questions.json ({len(QUESTIONS)} questions)")

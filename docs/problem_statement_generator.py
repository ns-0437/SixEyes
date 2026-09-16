"""Generates a company-style problem statement document as a PDF."""

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, ListFlowable, ListItem, KeepTogether,
)

INK = colors.HexColor("#10202E")
INK_SOFT = colors.HexColor("#4A5C6C")
ACCENT = colors.HexColor("#A8662E")
RULE = colors.HexColor("#D8DEE4")
BAND = colors.HexColor("#EEF1F4")
CRIT_BG = colors.HexColor("#FBEEE9")

OUT_PATH = "SixEyes_Problem_Statement.pdf"

styles = getSampleStyleSheet()

doc_title = ParagraphStyle("DocTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                            fontSize=22, leading=26, textColor=INK, spaceAfter=2, alignment=TA_LEFT)
doc_subtitle = ParagraphStyle("DocSubtitle", parent=styles["Normal"], fontName="Helvetica",
                               fontSize=11.5, leading=15, textColor=INK_SOFT, spaceAfter=0)
eyebrow = ParagraphStyle("Eyebrow", parent=styles["Normal"], fontName="Helvetica-Bold",
                          fontSize=8.5, leading=11, textColor=ACCENT, spaceAfter=4,
                          spaceBefore=0, tracking=1)
h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold",
                     fontSize=13.5, leading=17, textColor=INK, spaceBefore=10, spaceAfter=5)
h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontName="Helvetica-Bold",
                     fontSize=10.5, leading=14, textColor=ACCENT, spaceBefore=6, spaceAfter=2)
body = ParagraphStyle("Body", parent=styles["Normal"], fontName="Helvetica",
                       fontSize=9.3, leading=13.1, textColor=INK, alignment=TA_JUSTIFY, spaceAfter=5)
body_tight = ParagraphStyle("BodyTight", parent=body, spaceAfter=2)
bullet = ParagraphStyle("Bullet", parent=body, spaceAfter=4, leftIndent=0)
small_soft = ParagraphStyle("SmallSoft", parent=styles["Normal"], fontName="Helvetica",
                             fontSize=8.5, leading=12, textColor=INK_SOFT)
cell = ParagraphStyle("Cell", parent=body, fontSize=9.3, leading=13, spaceAfter=0, alignment=TA_LEFT)
cell_label = ParagraphStyle("CellLabel", parent=cell, fontName="Helvetica-Bold", textColor=INK_SOFT,
                             fontSize=8.3)
flag_style = ParagraphStyle("Flag", parent=body, fontSize=9.5, leading=13.5, textColor=INK, spaceAfter=0)


def P(text, style=body):
    return Paragraph(text, style)


def rule(thickness=0.8, color=RULE, space_before=4, space_after=10):
    return HRFlowable(width="100%", thickness=thickness, color=color,
                       spaceBefore=space_before, spaceAfter=space_after)


def bullets(items, style=bullet):
    return ListFlowable(
        [ListItem(P(t, style), leftIndent=0, value="—") for t in items],
        bulletType="bullet", start="—", bulletFontName="Helvetica",
        bulletFontSize=9, leftIndent=14, spaceBefore=1, spaceAfter=4,
    )


story = []

# ---------------------------------------------------------------- header block
meta_table = Table(
    [
        [P("DOCUMENT ID", cell_label), P("PROJECT STATUS", cell_label), P("CLASSIFICATION", cell_label)],
        [P("PS-SIXEYES-001", cell), P("Pre-development / Design", cell), P("Internal — Confidential", cell)],
        [P("OWNER", cell_label), P("DATE", cell_label), P("VERSION", cell_label)],
        [P("Founder / Product Owner", cell), P("16 September 2026", cell), P("1.1", cell)],
    ],
    colWidths=[2.15 * inch, 2.15 * inch, 2.15 * inch],
)
meta_table.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 0.8, RULE),
    ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
    ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ("BACKGROUND", (0, 2), (-1, 2), BAND),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]))

story.append(P("PROBLEM STATEMENT &nbsp;·&nbsp; TECHNICAL BRIEF", eyebrow))
story.append(P("Project SixEyes", doc_title))
story.append(P("AI execution-cost forensics — read-only waste detection for agentic LLM workloads", doc_subtitle))
story.append(Spacer(1, 10))
story.append(meta_table)
story.append(Spacer(1, 4))
story.append(rule(space_before=6, space_after=2))

# ---------------------------------------------------------------- 1. background
story.append(P("1. Background", h2))
story.append(P(
    "Organizations running LLM-based applications — chat assistants, retrieval-augmented "
    "generation (RAG) systems, and increasingly autonomous coding/research agents — are "
    "seeing inference cost grow faster than infrastructure teams can track it. Industry "
    "data through 2026 puts agentic workflows at 5–30x the token cost of a simple chat "
    "query, driven by tool calls, multi-step reasoning, retries, and repeatedly re-sent "
    "context. Independent audits attribute the majority of this overhead to structural "
    "inefficiency rather than genuine task complexity — most notably, broken prompt-cache "
    "reuse, which alone has been shown to move total LLM spend by 50–80% in production "
    "systems with no change to output quality.", body
))
story.append(P(
    "Provider-native tooling is starting to close part of this gap for a single request pair "
    "on a single provider — Anthropic, for example, ships a beta API that fingerprints "
    "consecutive requests and reports the exact divergence point at no cost. What remains "
    "unaddressed by any provider-native feature or observability platform (Langfuse, "
    "Helicone, Datadog LLM Observability) is aggregation across providers, prioritization by "
    "dollar impact across a fleet of traffic over time, and the non-cache waste categories "
    "(redundant tool calls, re-sent context, retry storms) that a single-request cache check "
    "does not touch. That narrower, honest gap is what this project tests.", body
))

# ---------------------------------------------------------------- 2. problem statement
story.append(P("2. Problem Statement", h2))
problem_box = Table([[P(
    "Teams running LLM applications at meaningful scale are overpaying for inference — "
    "often by 30–70% — because of structural, unintentional waste in how requests are "
    "constructed and executed (unstable prompt prefixes breaking provider caching, "
    "redundant tool calls, re-sent conversation history, retry storms). This waste is "
    "invisible in standard dashboards, which report aggregate cost and latency but not "
    "<b>root cause</b> or <b>a specific, verifiable fix</b>. Teams either overpay silently, "
    "or attempt manual optimization that risks degrading output quality with no way to "
    "measure that risk before shipping.", body
)]], colWidths=[6.6 * inch])
problem_box.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 1, ACCENT),
    ("BACKGROUND", (0, 0), (-1, -1), BAND),
    ("TOPPADDING", (0, 0), (-1, -1), 12),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
]))
story.append(problem_box)

# ---------------------------------------------------------------- 3. objective
story.append(P("3. Objective", h2))
story.append(P(
    "Build a system that ingests a team's LLM execution traces and automatically:", body
))
story.append(bullets([
    "<b>Detects</b> specific, named categories of recoverable waste (starting with broken "
    "prompt-cache prefixes, redundant tool calls, and re-sent context) — deterministically, "
    "with no LLM involved in the detection logic itself.",
    "<b>Localizes</b> the exact cause — the field, position, or code path responsible — not "
    "just the aggregate metric.",
    "<b>Quantifies</b> the recoverable cost in dollars, using a fully auditable calculation.",
    "<b>Prescribes</b> a concrete fix, and later verifies the savings after the customer "
    "applies it.",
]))
story.append(P(
    "The governing constraint: the system diagnoses and recommends — it never modifies a "
    "production request. That boundary is provable and is the thing this is a diagnostic "
    "tool, not a quality/cost trade-off tool, rests on. What it does <b>not</b> claim: that "
    "applying a recommended fix is guaranteed to leave model output unchanged. Reordering a "
    "tool array or moving a timestamp to repair a broken cache prefix is itself a change to "
    "what the model sees. Every reported number is labelled as one of four tiers — measured "
    "(a real before/after), derived (exact arithmetic on observed data), estimated (modelled), "
    "or speculative (advisory text only) — and only \"measured\" is ever presented as a result.",
    body
))

# ---------------------------------------------------------------- 4. scope
story.append(P("4. Scope", h2))

scope_table = Table(
    [
        [P("IN SCOPE (first build)", cell_label), P("OUT OF SCOPE (for now)", cell_label)],
        [
            P("• Ingest execution traces from common formats (OpenTelemetry GenAI "
              "conventions, direct provider SDK logs, plain exported logs)", cell),
            P("• Acting on findings automatically (no changes made on the team's behalf)", cell),
        ],
        [
            P("• Deterministic detection of: unstable cache prefixes, redundant tool calls, "
              "duplicated/re-sent context, retry waste", cell),
            P("• Model routing, prompt rewriting, or any capability that changes model "
              "behavior or output", cell),
        ],
        [
            P("• Dollar-cost attribution per finding, using versioned, dated provider "
              "pricing", cell),
            P("• Semantic/fuzzy caching (approximate-match reuse) — quality risk, deferred", cell),
        ],
        [
            P("• A report/output format a human can act on directly (finding, evidence, "
              "fix, dollar impact)", cell),
            P("• Multi-tenant hosted service, billing, and account management", cell),
        ],
        [
            P("• A way to confirm, after a fix is applied, that the saving actually "
              "happened", cell),
            P("• Support for every model provider on day one — start with the two or three "
              "most common", cell),
        ],
    ],
    colWidths=[3.3 * inch, 3.3 * inch],
)
scope_table.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 0.8, RULE),
    ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
    ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ("TOPPADDING", (0, 0), (-1, -1), 7),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]))
story.append(scope_table)

# ---------------------------------------------------------------- 5. requirements
story.append(P("5. Key Technical Requirements", h2))

story.append(P("5.1 Correctness &amp; trust", h3))
story.append(bullets([
    "Detection logic must be fully deterministic and reproducible — same input trace "
    "always produces the same findings. No probabilistic/LLM-based judgment in the "
    "detection path.",
    "Every reported finding must carry machine-checkable evidence, not just a conclusion "
    "(e.g. \"divergence at message index 2, token offset 1,847\" — not \"cache looks broken\").",
    "Dollar figures must use exact arithmetic (no floating-point currency) and cite which "
    "priced-per-token table version produced them, since provider prices change monthly.",
]))

story.append(P("5.2 Data handling", h3))
story.append(bullets([
    "The system must never need to store or transmit a customer's actual prompt or "
    "completion text to produce a finding — detection should work on structural "
    "fingerprints (hashes, token counts, position offsets) derived from the trace, not "
    "the trace content itself.",
    "This needs to be provable, not just claimed — i.e., an engineer should be able to "
    "audit exactly what leaves the customer's environment.",
]))

story.append(P("5.3 Extensibility", h3))
story.append(bullets([
    "New waste-detection categories will be added over time (redundant tool calls, retry "
    "storms, oversized context, etc.). Adding one must not require touching or re-running "
    "the existing ones.",
    "Given that traces can be large, re-analysis after adding a new detector should reuse "
    "prior computation rather than reprocessing everything from scratch.",
]))

story.append(P("5.4 Explainability of fixes", h3))
story.append(bullets([
    "A human-written or human-readable explanation of <i>how to fix</i> a finding is "
    "useful, but must be clearly separated from the deterministic finding itself — the "
    "dollar figure and evidence must never depend on this explanation being correct.",
]))

# ---------------------------------------------------------------- 6. deliverables
story.append(P("6. Deliverables &amp; Definition of Done", h2))
deliv_table = Table(
    [
        [P("#", cell_label), P("Deliverable", cell_label), P("Definition of done", cell_label)],
        [P("1", cell), P("Trace ingestion", cell),
         P("Can read a real trace export and normalize it into one internal format, "
           "regardless of source.", cell)],
        [P("2", cell), P("Content-free fingerprinting", cell),
         P("Two requests differing by one dynamic field (e.g. a timestamp) are shown to "
           "diverge at the correct position — with an auditable proof that no prompt "
           "content was retained to do it.", cell)],
        [P("3", cell), P("Waste detectors (v1 set)", cell),
         P("Each detector finds a deliberately-inserted defect in a test case, 100% of "
           "the time, and produces zero false positives on a clean trace.", cell)],
        [P("4", cell), P("Cost attribution", cell),
         P("Reported recoverable-dollar total never exceeds the trace's actual total "
           "spend; overlapping causes are not double-counted.", cell)],
        [P("5", cell), P("Report output", cell),
         P("A person with no context can open the output and understand: what's wrong, "
           "proof it's wrong, what to change, and how many dollars it's worth.", cell)],
        [P("6", cell), P("Validation on real data", cell),
         P("Run against at least 5 real workloads (not synthetic). At least 3 must show "
           "meaningful recoverable spend before this is considered validated.", cell)],
    ],
    colWidths=[0.35 * inch, 1.75 * inch, 4.5 * inch],
)
deliv_table.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 0.8, RULE),
    ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
    ("BACKGROUND", (0, 0), (-1, 0), BAND),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]))
story.append(deliv_table)

# ---------------------------------------------------------------- 7. constraints
story.append(P("7. Constraints &amp; Assumptions", h2))
story.append(bullets([
    "<b>No quality trade-offs in v1.</b> Anything that could change model output quality "
    "(model swapping, prompt rewriting, semantic caching) is explicitly deferred — it "
    "requires an evaluation framework that does not exist yet.",
    "<b>No production traffic interception.</b> The system reads exported/logged traces; "
    "it does not sit between the application and the model provider in this phase.",
    "<b>Small team, limited timeline.</b> Build order matters — the core analysis engine "
    "must be solid before adding detectors, and detectors must be validated on real data "
    "before adding a user-facing product layer around them.",
    "<b>Pricing data will go stale.</b> Provider token pricing must be treated as versioned, "
    "dated input, not a hardcoded constant.",
]))

# ---------------------------------------------------------------- 8. open questions
story.append(P("8. Open Questions for Engineering Review", h2))
oq_box = Table([[P(
    "&#8226;&nbsp; What's the right internal architecture for a system with a fixed core "
    "pipeline (ingest → fingerprint) but a growing, independently-testable set of detection "
    "modules that must run in parallel and support incremental re-analysis?<br/><br/>"
    "&#8226;&nbsp; What's the most defensible way to prove, technically, that no raw prompt "
    "content is ever retained or transmitted by the fingerprinting step?<br/><br/>"
    "&#8226;&nbsp; How should confidence be represented when a dollar figure is a measured "
    "fact (proven by before/after data) versus a modeled estimate — and how do we stop the "
    "two from being presented the same way?<br/><br/>"
    "&#8226;&nbsp; What trace formats and providers should the first version support, given "
    "limited engineering time?",
    body
)]], colWidths=[6.6 * inch])
oq_box.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 0.8, RULE),
    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
    ("TOPPADDING", (0, 0), (-1, -1), 12),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ("LEFTPADDING", (0, 0), (-1, -1), 14),
    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
]))
story.append(oq_box)

story.append(Spacer(1, 8))
story.append(rule(space_before=0, space_after=4))
story.append(P(
    "End of document. Prepared as an internal technical brief for engineering review.",
    small_soft
))


def _footer(canvas, doc_):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.6)
    canvas.line(0.75 * inch, 0.65 * inch, LETTER[0] - 0.75 * inch, 0.65 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(INK_SOFT)
    canvas.drawString(0.75 * inch, 0.48 * inch, "Project SixEyes — Problem Statement — Internal — Confidential")
    canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.48 * inch, f"Page {doc_.page}")
    canvas.restoreState()


doc = SimpleDocTemplate(
    OUT_PATH, pagesize=LETTER,
    leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    topMargin=0.55 * inch, bottomMargin=0.75 * inch,
    title="SixEyes — Problem Statement", author="Internal",
)
doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
print("done")

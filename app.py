import os
import re
import io
import csv
import json
import requests
import pdfplumber
import streamlit as st
from openai import OpenAI

# =========================
# Page + Theme (Blue & Gold)
# =========================
st.set_page_config(page_title="Discussion Finding & Summary Checker", layout="wide")

BLUE = "#0B2D5C"
GOLD = "#D4AF37"
LIGHT_BG = "#F7F9FC"

st.markdown(
    f"""
    <style>
      body {{ background: {LIGHT_BG}; }}
      .block-container {{ padding-top: 1.6rem; }}
      h1, h2, h3 {{ color: {BLUE}; }}
      .card {{
        background: white;
        border: 2px solid {BLUE};
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06);
        margin-bottom: 1rem;
      }}
      .pill {{
        display:inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid {GOLD};
        color: {BLUE};
        background: rgba(212,175,55,0.15);
        font-weight: 700;
        font-size: 12px;
      }}
      .warn {{
        border-left: 6px solid {GOLD};
        background: rgba(212,175,55,0.12);
        padding: 10px 12px;
        border-radius: 10px;
        margin: 0.5rem 0;
      }}
      .bad {{
        border-left: 6px solid #B00020;
        background: rgba(176,0,32,0.08);
        padding: 10px 12px;
        border-radius: 10px;
        margin: 0.5rem 0;
      }}
      .good {{
        border-left: 6px solid #0F7B0F;
        background: rgba(15,123,15,0.08);
        padding: 10px 12px;
        border-radius: 10px;
        margin: 0.5rem 0;
      }}
      .small {{ color: #4b5563; font-size: 13px; }}
      textarea {{
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      }}
      .metricbox {{
        border: 1px solid rgba(11,45,92,0.25);
        border-radius: 12px;
        padding: 10px 12px;
        background: rgba(11,45,92,0.03);
      }}
      code {{ background: rgba(11,45,92,0.07); padding: 2px 6px; border-radius: 8px; }}
    </style>
    """,
    unsafe_allow_html=True
)

st.title("Discussion Finding & Summary Checker")

st.markdown(
    f"""
    <div class="card">
      <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
        <div>
          <b>Paste one Excel row (6 cells) horizontally.</b><br/>
          <span class="small">
            Uses only: <b>Cell 1</b> (Discussion quote), <b>Cell 4</b> (student summary), <b>Cell 6</b> (PDF link).
            Cells 2, 3, and 5 are ignored.
          </span>
        </div>
        <div class="pill">Blue &amp; Gold</div>
      </div>
      <div class="warn">
        <b>Streamlined grading:</b> Copy the row → paste → click Evaluate.
        This app will auto-detect the first URL anywhere in your paste and will auto-convert SharePoint links to direct-download.
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

# =========================
# OpenAI Client
# =========================
def get_openai_client():
    key = None
    try:
        key = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        key = None
    if not key:
        key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key)

client = get_openai_client()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# =========================
# Excel Row Parsing + URL Detection
# =========================
URL_RE = re.compile(r"https?://[^\s<>\]]+", re.IGNORECASE)

def looks_like_url(s: str) -> bool:
    if not s:
        return False
    s = s.strip().lower()
    return s.startswith("http://") or s.startswith("https://")

def find_first_url_anywhere(text: str) -> str:
    if not text:
        return ""
    m = URL_RE.search(text)
    if not m:
        return ""
    url = m.group(0).strip()
    # common trailing punctuation
    return url.rstrip(").,;:!?\"'")

def split_excel_row(text: str):
    """
    Excel horizontal row copy -> tab-separated values.
    Take first non-empty row; normalize to 6 cells.
    """
    if not text:
        return [""] * 6, []

    buf = io.StringIO(text)
    reader = csv.reader(buf, delimiter="\t")
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    warnings = []

    if len(rows) > 1:
        warnings.append(f"Detected {len(rows)} rows pasted. Using the first row only.")

    row = rows[0] if rows else []
    row = [(c or "").strip() for c in row]

    if len(row) < 6:
        row += [""] * (6 - len(row))
    if len(row) > 6:
        warnings.append(f"Detected {len(row)} columns; using the first 6.")
        row = row[:6]

    return row, warnings

# =========================
# SharePoint Direct Download Normalization
# =========================
def normalize_sharepoint_download_url(url: str) -> str:
    """
    Convert SharePoint/OneDrive links into a download-friendly URL by adding download=1.
    Works for many share links. If the URL already has download=1, leave it.
    """
    if not url:
        return url
    lower = url.lower()
    if ("sharepoint.com" in lower) or ("onedrive" in lower):
        if "download=1" not in lower:
            return url + ("&download=1" if "?" in url else "?download=1")
    return url

# =========================
# PDF Download + Text Extraction
# =========================
def download_pdf_bytes(url: str, timeout=25) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AcademicQuoteChecker/1.0)"}
    url = normalize_sharepoint_download_url(url)

    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()

    content = r.content or b""
    ct = (r.headers.get("Content-Type") or "").lower()

    # PDF signature
    if content[:4] == b"%PDF":
        return content

    # Sometimes PDFs arrive as application/octet-stream; still OK if content is PDF-like
    if "pdf" in ct:
        return content

    # HTML means you got a viewer/login page, not the file bytes
    head = content[:300].lower()
    if b"<html" in head or b"<!doctype html" in head:
        raise ValueError(
            "The link returned an HTML page (SharePoint viewer/login), not a direct PDF download. "
            "Try using a Share link to the file (not a folder), and the app will append download=1 automatically."
        )

    raise ValueError(f"Link did not return a PDF (Content-Type: {ct or 'unknown'}).")

def extract_text_from_pdf(pdf_bytes: bytes, max_pages=25) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = min(len(pdf.pages), max_pages)
        for i in range(n):
            t = pdf.pages[i].extract_text() or ""
            if t.strip():
                text_parts.append(t)
    return "\n\n".join(text_parts).strip()

def extract_discussion_section(full_text: str) -> str:
    """
    Heuristic:
    - Find a 'Discussion' header
    - Stop at common next sections
    If no Discussion header is found, return a large slice to let the model decide (may reduce confidence).
    """
    if not full_text:
        return ""

    t = full_text
    tl = t.lower()

    start_patterns = [
        r"\n\s*discussion\s*\n",
        r"\n\s*general\s*discussion\s*\n",
        r"\n\s*discussion\s*and\s*conclusion\s*\n",
    ]

    start_idx = -1
    for pat in start_patterns:
        m = re.search(pat, tl, flags=re.IGNORECASE)
        if m:
            start_idx = m.start()
            break

    if start_idx == -1:
        # fallback: provide a big chunk for model, but warn user
        return t[:18000]

    stop_patterns = [
        r"\n\s*conclusion\s*\n",
        r"\n\s*limitations\s*\n",
        r"\n\s*implications\s*\n",
        r"\n\s*references\s*\n",
        r"\n\s*bibliography\s*\n",
        r"\n\s*appendix\s*\n",
        r"\n\s*acknowledg",
    ]

    after = tl[start_idx + 1:]
    stop_rel = None
    for pat in stop_patterns:
        m = re.search(pat, after, flags=re.IGNORECASE)
        if m:
            stop_rel = m.start()
            break

    end_idx = (start_idx + 1 + stop_rel) if stop_rel is not None else len(t)
    section = t[start_idx:end_idx].strip()
    return section[:20000]

# =========================
# LLM Evaluation (JSON Output)
# =========================
def safe_json_loads(s: str) -> dict:
    s2 = re.sub(r"^```json\s*|\s*```$", "", s.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
    return json.loads(s2)

def llm_grade(quote: str, student: str, discussion_text: str, pdf_ref: str) -> dict:
    """
    If quote is not supported, model also:
    - identifies likely Discussion findings
    - provides short verbatim excerpts (<=25 words each)
    - provides suggestions/feedback
    """
    if not client:
        return {"error": "OPENAI_API_KEY is not set. Add it to Streamlit Secrets (OPENAI_API_KEY)."}

    discussion_excerpt = discussion_text[:14000] if discussion_text else ""

    system = (
        "You are a strict academic grading assistant. "
        "You only use the provided DISCUSSION_EXCERPT and the quote/student text. "
        "CRITICAL: If you provide verbatim excerpts from the excerpt, each excerpt must be <= 25 words. "
        "Return STRICT JSON only. No markdown."
    )

    user = f"""
You will grade two things:

A) SOURCE CHECK (Discussion finding?)
Decide whether QUOTE is actually supported by the DISCUSSION_EXCERPT as a finding/claim.
Return: "yes" / "no" / "uncertain"
- Evidence must be short.
- If "no" or "uncertain", identify what the Discussion *does* claim as key findings, based only on the excerpt.

B) REPRESENTATION CHECK
Is STUDENT_SUMMARY a fair representation of QUOTE?
Return: "yes" / "partly" / "no"
Also list concrete issues: overclaiming, missing qualifiers, adding new ideas, causal leaps, etc.

C) PROFESSOR SUGGESTIONS
Give brief suggestions the professor can use for feedback and improvement.

IMPORTANT VERBATIM RULE:
If you quote anything from the excerpt, provide up to 3 excerpts, each <= 25 words (hard limit).

Inputs:
PDF_REF: {pdf_ref}

QUOTE:
{quote}

STUDENT_SUMMARY:
{student}

DISCUSSION_EXCERPT:
{discussion_excerpt}

Output STRICT JSON exactly like this:
{{
  "quote_is_discussion_finding": "yes|no|uncertain",
  "source_evidence": "brief reasoning",
  "verbatim_discussion_excerpts": [
    {{"excerpt": "<=25 words", "why_it_matters": "short"}}
  ],
  "likely_discussion_findings_paraphrase": [
    "short paraphrase 1",
    "short paraphrase 2"
  ],
  "student_summary_fair_representation": "yes|partly|no",
  "student_summary_accuracy_score_1_to_5": 1,
  "student_summary_issues": ["..."],
  "professor_feedback_suggestion": "1-3 sentences you would write to the student",
  "confidence": "low|medium|high"
}}
"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.2,
    )

    raw = resp.choices[0].message.content.strip()
    try:
        return safe_json_loads(raw)
    except Exception:
        return {"error": "Model returned non-JSON output.", "raw": raw}

# =========================
# UI Inputs
# =========================
paste = st.text_area("Paste the Excel row here (one horizontal row, 6 cells)", height=160)

left, right = st.columns([1, 1])
with left:
    st.caption("PDF upload fallback (only if the link won’t download).")
    uploaded_pdf = st.file_uploader("Upload PDF (fallback)", type=["pdf"])
with right:
    st.caption("Optional: paste a PDF URL here to override Cell 6.")
    manual_url = st.text_input("PDF URL override (optional)", value="")

cells, warnings = split_excel_row(paste)
cell1 = cells[0]
cell4 = cells[3]
cell6 = cells[5]

# Determine PDF URL
auto_url = find_first_url_anywhere(paste) if not looks_like_url(cell6) else ""
pdf_url = ""

if looks_like_url(manual_url.strip()):
    pdf_url = manual_url.strip()
elif looks_like_url(cell6):
    pdf_url = cell6
elif auto_url:
    pdf_url = auto_url

# Preview
with st.expander("Preview (Cells 1, 4, 6 detected)", expanded=True):
    if warnings:
        st.markdown('<div class="warn"><b>Notes:</b><br/>' + "<br/>".join(warnings) + "</div>", unsafe_allow_html=True)

    c1, c4, c6 = st.columns(3)
    with c1:
        st.subheader("Cell 1 (Quote)")
        st.text_area("cell1_preview", cell1 or "[empty]", height=200, label_visibility="collapsed")
    with c4:
        st.subheader("Cell 4 (Student summary)")
        st.text_area("cell4_preview", cell4 or "[empty]", height=200, label_visibility="collapsed")
    with c6:
        st.subheader("Cell 6 (PDF link detected)")
        display = pdf_url if pdf_url else (cell6 if cell6 else "[empty]")
        st.text_area("cell6_preview", display, height=80, label_visibility="collapsed")

# Validate minimum
missing = []
if len(cell1.strip()) < 10:
    missing.append("Cell 1 (quote)")
if len(cell4.strip()) < 10:
    missing.append("Cell 4 (student summary)")
if not pdf_url and not uploaded_pdf:
    missing.append("Cell 6 URL (https://...) OR uploaded PDF")

if missing:
    st.markdown('<div class="bad"><b>Missing required input:</b><br/>' + "<br/>".join(missing) + "</div>", unsafe_allow_html=True)

st.divider()

# =========================
# Evaluate
# =========================
if st.button("Evaluate", type="primary", use_container_width=True, disabled=bool(missing)):
    # Acquire PDF
    pdf_bytes = None
    pdf_ref = ""

    if uploaded_pdf is not None:
        pdf_bytes = uploaded_pdf.read()
        pdf_ref = "uploaded PDF"
        st.markdown('<div class="good"><b>PDF source:</b> uploaded</div>', unsafe_allow_html=True)
    else:
        pdf_ref = pdf_url
        try:
            with st.spinner("Downloading PDF…"):
                pdf_bytes = download_pdf_bytes(pdf_url)
            st.markdown('<div class="good"><b>PDF source:</b> link download succeeded</div>', unsafe_allow_html=True)
        except Exception as e:
            st.markdown(
                f'<div class="bad"><b>Could not download PDF from link.</b><br/>'
                f'Reason: <code>{str(e)}</code><br/><br/>'
                f'<b>Fix:</b> Use a file share link directly to the PDF (not a folder view). '
                f'This app auto-adds <code>download=1</code> for SharePoint. If it still fails, upload the PDF.</div>',
                unsafe_allow_html=True
            )
            st.stop()

    # Extract text
    with st.spinner("Extracting text from PDF (first pages)…"):
        full_text = extract_text_from_pdf(pdf_bytes, max_pages=25)

    if not full_text.strip():
        st.markdown(
            '<div class="bad"><b>Could not extract text from the PDF.</b> '
            'This is common if it’s a scanned image PDF. Consider uploading a text-based PDF.</div>',
            unsafe_allow_html=True
        )
        st.stop()

    discussion = extract_discussion_section(full_text)
    if not discussion.strip():
        discussion = full_text[:18000]

    # Grade via LLM
    with st.spinner("Grading (quote vs discussion; student summary vs quote)…"):
        result = llm_grade(cell1, cell4, discussion, pdf_ref)

    st.subheader("Results")

    if "error" in result:
        st.markdown('<div class="bad"><b>Error:</b><br/>' + str(result["error"]) + "</div>", unsafe_allow_html=True)
        if "raw" in result:
            st.code(result["raw"])
        st.stop()

    # Headline metrics
    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown("<div class='metricbox'><b>Quote is Discussion finding?</b><br/>"
                    f"{result.get('quote_is_discussion_finding','uncertain').upper()}</div>", unsafe_allow_html=True)
    with m2:
        st.markdown("<div class='metricbox'><b>Summary fair representation?</b><br/>"
                    f"{result.get('student_summary_fair_representation','partly').upper()}</div>", unsafe_allow_html=True)
    with m3:
        sc = result.get("student_summary_accuracy_score_1_to_5", "—")
        st.markdown("<div class='metricbox'><b>Accuracy score (1–5)</b><br/>"
                    f"{sc}</div>", unsafe_allow_html=True)

    st.markdown("### A) Source check (Discussion finding?)")
    st.write(result.get("source_evidence", ""))

    excerpts = result.get("verbatim_discussion_excerpts", []) or []
    if excerpts:
        st.markdown("**Short verbatim excerpts from Discussion (<= 25 words each):**")
        for item in excerpts[:3]:
            ex = (item.get("excerpt") or "").strip()
            why = (item.get("why_it_matters") or "").strip()
            if ex:
                st.write(f'• “{ex}”')
                if why:
                    st.write(f"  - {why}")
    else:
        st.markdown("<div class='warn'><b>No excerpts returned.</b> The model may be uncertain or the excerpt lacked clear findings.</div>", unsafe_allow_html=True)

    st.markdown("**Likely Discussion findings (paraphrase):**")
    findings = result.get("likely_discussion_findings_paraphrase", []) or []
    if findings:
        for f in findings[:6]:
            st.write(f"- {f}")
    else:
        st.write("—")

    st.markdown("### B) Student summary check (Cell 4 vs Cell 1)")
    issues = result.get("student_summary_issues", []) or []
    if issues:
        st.markdown("**Issues detected:**")
        for i in issues[:8]:
            st.write(f"- {i}")
    else:
        st.write("No major issues detected.")

    st.markdown("### C) Suggested professor feedback")
    st.write(result.get("professor_feedback_suggestion", ""))

    st.caption(f"Confidence: {result.get('confidence','low').upper()}")

    with st.expander("Discussion excerpt used (for transparency)"):
        st.text_area("Discussion excerpt", discussion[:20000], height=320)

st.caption(
    "Speed tip: for best results, make Cell 6 contain the actual URL text (starts with https://), not only a clickable label. "
    "This app auto-adds download=1 for SharePoint/OneDrive links."
)

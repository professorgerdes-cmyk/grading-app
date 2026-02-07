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
# Page + Theme
# =========================
st.set_page_config(page_title="Discussion Finding & Summary Checker", layout="wide")

BLUE = "#0B2D5C"
GOLD = "#D4AF37"
LIGHT_BG = "#F7F9FC"

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 2rem; }}
      body {{ background: {LIGHT_BG}; }}
      h1, h2, h3, h4 {{ color: {BLUE}; }}
      .card {{
        background: white;
        border: 2px solid {BLUE};
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06);
      }}
      .pill {{
        display:inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid {GOLD};
        color: {BLUE};
        background: rgba(212,175,55,0.15);
        font-weight: 600;
        font-size: 12px;
      }}
      .warn {{
        border-left: 6px solid {GOLD};
        background: rgba(212,175,55,0.12);
        padding: 10px 12px;
        border-radius: 10px;
      }}
      .bad {{
        border-left: 6px solid #B00020;
        background: rgba(176,0,32,0.08);
        padding: 10px 12px;
        border-radius: 10px;
      }}
      .good {{
        border-left: 6px solid #0F7B0F;
        background: rgba(15,123,15,0.08);
        padding: 10px 12px;
        border-radius: 10px;
      }}
      .small {{ color: #4b5563; font-size: 13px; }}
      textarea {{
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      }}
    </style>
    """,
    unsafe_allow_html=True
)

# =========================
# OpenAI client
# =========================
def get_openai_client():
    # Prefer Streamlit secrets; fall back to env var
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

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # change if you want

# =========================
# Robust Excel row parsing
# =========================
URL_RE = re.compile(r"https?://[^\s<>\]]+", re.IGNORECASE)

def looks_like_url(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    return s.lower().startswith("http://") or s.lower().startswith("https://")

def find_first_url_anywhere(text: str) -> str:
    if not text:
        return ""
    m = URL_RE.search(text)
    return m.group(0).strip() if m else ""

def split_excel_row(text: str):
    """
    Excel copies a horizontal row as TAB-separated values.
    This parser:
      - reads TSV safely
      - takes the first non-empty row
      - pads to 6 cells
    """
    if not text:
        return [""] * 6, []

    buf = io.StringIO(text)
    reader = csv.reader(buf, delimiter="\t")
    rows = [r for r in reader if any((c or "").strip() for c in r)]

    # If user pasted multiple rows, take the first and warn
    warnings = []
    if len(rows) > 1:
        warnings.append(f"Detected {len(rows)} rows pasted. Using the first row only.")

    row = rows[0] if rows else []
    # Normalize length to 6
    while len(row) < 6:
        row.append("")
    if len(row) > 6:
        # Keep first 6 cells; note extras
        warnings.append(f"Detected {len(row)} columns; using the first 6.")
        row = row[:6]

    # Strip whitespace
    row = [(c or "").strip() for c in row]
    return row, warnings

# =========================
# PDF download + text extraction
# =========================
def download_pdf_bytes(url: str, timeout=20) -> bytes:
    """
    Attempt to download a PDF.
    Many SharePoint links require auth and will return HTML instead of a PDF.
    We'll detect that and raise a helpful error.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AcademicQuoteChecker/1.0)"
    }
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()

    content = r.content or b""
    ct = (r.headers.get("Content-Type") or "").lower()

    # Most reliable PDF signature check
    if content[:4] == b"%PDF":
        return content

    # Sometimes PDFs come as octet-stream; still might be PDF
    if "pdf" in ct:
        # Try signature anyway
        return content

    # If it looks like HTML, it's probably a login or preview page
    snippet = content[:200].lower()
    if b"<html" in snippet or b"<!doctype html" in snippet:
        raise ValueError(
            "The link returned an HTML page (likely a login/SharePoint preview page), not a downloadable PDF."
        )

    raise ValueError(f"Link did not return a PDF (Content-Type: {ct or 'unknown'}).")

def extract_text_from_pdf(pdf_bytes: bytes, max_pages=20) -> str:
    """
    Extract text with pdfplumber. Limit pages for speed.
    """
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = min(len(pdf.pages), max_pages)
        for i in range(n):
            page = pdf.pages[i]
            t = page.extract_text() or ""
            if t.strip():
                text_parts.append(t)
    return "\n\n".join(text_parts).strip()

def extract_discussion_section(full_text: str) -> str:
    """
    Heuristic discussion extraction:
    - find 'Discussion' header
    - stop at 'Conclusion', 'Limitations', 'References', etc.
    Not perfect, but works well for many journal PDFs.
    """
    if not full_text:
        return ""

    t = full_text
    t_lower = t.lower()

    # Find discussion start
    start_candidates = [
        r"\n\s*discussion\s*\n",
        r"\n\s*discussion\s*and\s*conclusion\s*\n",
        r"\n\s*general\s*discussion\s*\n",
    ]
    start_idx = -1
    for pat in start_candidates:
        m = re.search(pat, t_lower, flags=re.IGNORECASE)
        if m:
            start_idx = m.start()
            break

    if start_idx == -1:
        # If no header, return first chunk (better than nothing)
        return t[:12000]

    # Find stop markers after start
    stop_markers = [
        r"\n\s*conclusion\s*\n",
        r"\n\s*limitations\s*\n",
        r"\n\s*implications\s*\n",
        r"\n\s*references\s*\n",
        r"\n\s*bibliography\s*\n",
        r"\n\s*appendix\s*\n",
        r"\n\s*acknowledg",
    ]
    after = t_lower[start_idx + 1:]
    stop_idx_rel = None
    for pat in stop_markers:
        m = re.search(pat, after, flags=re.IGNORECASE)
        if m:
            stop_idx_rel = m.start()
            break

    end_idx = (start_idx + 1 + stop_idx_rel) if stop_idx_rel is not None else len(t)
    section = t[start_idx:end_idx].strip()

    # Keep it within a reasonable size for the model
    return section[:16000]

# =========================
# LLM Evaluation
# =========================
def evaluate_with_llm(quote: str, student_expl: str, discussion_text: str, pdf_ref: str) -> dict:
    """
    Returns a structured dict.
    """
    if not client:
        return {
            "error": "No OpenAI API key configured. Add OPENAI_API_KEY to Streamlit secrets or environment variables."
        }

    # Keep discussion excerpt bounded
    discussion_excerpt = discussion_text[:12000] if discussion_text else ""

    system = (
        "You are a careful academic grader. "
        "You ONLY use the provided discussion section excerpt (or lack of it) and the quote/explanation. "
        "If evidence is insufficient, say 'uncertain' rather than guessing."
    )

    user = f"""
TASKS
1) Determine whether the QUOTE is actually a finding/claim presented in the DISCUSSION section of the article.
   - Answer: yes / no / uncertain
   - Provide brief evidence: point to matching language or explain why it is not supported.

2) Determine whether the STUDENT EXPLANATION (Cell 4) is a fair representation of the QUOTE (Cell 1).
   - Rate accuracy: 1 (poor) to 5 (excellent)
   - List any distortions, additions, or missing nuances.

INPUTS
PDF reference/link (Cell 6 or detected): {pdf_ref}

QUOTE (Cell 1):
{quote}

STUDENT EXPLANATION (Cell 4):
{student_expl}

DISCUSSION SECTION EXCERPT (best-effort):
{discussion_excerpt}

OUTPUT FORMAT (STRICT JSON)
{{
  "quote_is_discussion_finding": "yes|no|uncertain",
  "evidence": "short evidence or reason",
  "student_summary_accuracy_1_to_5": 1,
  "summary_issues": ["..."],
  "suggested_professor_feedback": "short feedback you would write to the student",
  "confidence": "low|medium|high"
}}
"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )

    content = resp.choices[0].message.content.strip()

    # Parse JSON safely
    try:
        # Some models wrap JSON in code fences
        content_clean = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.IGNORECASE | re.MULTILINE).strip()
        return json.loads(content_clean)
    except Exception:
        return {
            "error": "Model returned non-JSON output. Here is the raw response:",
            "raw": content
        }

# =========================
# UI
# =========================
st.title("Discussion Finding & Summary Checker")

st.markdown(
    f"""
    <div class="card">
      <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
        <div>
          <b>Paste one Excel row (6 cells) horizontally.</b><br/>
          <span class="small">Only these are used: <b>Cell 1</b> (quote), <b>Cell 4</b> (student explanation), <b>Cell 6</b> (PDF link/reference). Cells 2, 3, 5 are ignored.</span>
        </div>
        <div class="pill">Blue &amp; Gold</div>
      </div>
      <hr style="border:none; border-top:1px solid #e5e7eb; margin:12px 0;">
      <div class="small">
        <b>Pro tip for speed (200 rows):</b> keep the PDF link as a plain URL in Excel (starts with <code>https://</code>).  
        If Excel only pastes a filename instead of the link, this app will also try to auto-detect the first URL anywhere in the pasted text.
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

paste = st.text_area("Paste the Excel row here (one row)", height=160, placeholder="Copy one horizontal row (6 cells) from Excel and paste here...")

colA, colB = st.columns([1, 1])
with colA:
    st.caption("If the PDF link is blocked (SharePoint login), upload the PDF here (optional fallback).")
    uploaded_pdf = st.file_uploader("Upload PDF (fallback)", type=["pdf"])
with colB:
    st.caption("If you want, you can paste the PDF URL directly here (overrides Cell 6).")
    manual_url = st.text_input("PDF URL override (optional)", value="")

st.divider()

# Parse
cells, warnings = split_excel_row(paste)

# Pull the required cells
cell1_quote = cells[0]
cell4_expl = cells[3]
cell6_ref = cells[5]

# If Cell 6 isn't a URL, search the entire pasted blob for any URL
auto_url = ""
if not looks_like_url(cell6_ref):
    auto_url = find_first_url_anywhere(paste)

# Allow manual override
pdf_ref = manual_url.strip() if looks_like_url(manual_url.strip()) else ""
if not pdf_ref:
    pdf_ref = cell6_ref if looks_like_url(cell6_ref) else auto_url

# Preview (so you can trust what it detected)
with st.expander("Preview (what the app detected for Cells 1, 4, 6)", expanded=True):
    if warnings:
        st.markdown('<div class="warn"><b>Notes:</b><br/>' + "<br/>".join(warnings) + "</div>", unsafe_allow_html=True)

    c1, c4, c6 = st.columns(3)
    with c1:
        st.subheader("Cell 1 (Quote)")
        st.text_area(" ", cell1_quote or "[empty]", height=180, label_visibility="collapsed")
    with c4:
        st.subheader("Cell 4 (Student explanation)")
        st.text_area("  ", cell4_expl or "[empty]", height=180, label_visibility="collapsed")
    with c6:
        st.subheader("Cell 6 (PDF link / reference)")
        st.text_area("   ", pdf_ref or "[empty]", height=70, label_visibility="collapsed")

# Validate minimal requirements
missing = []
if not cell1_quote:
    missing.append("Cell 1 (quote)")
if not cell4_expl:
    missing.append("Cell 4 (student explanation)")
# For cell6: either URL or uploaded PDF is acceptable
if not pdf_ref and not uploaded_pdf:
    missing.append("Cell 6 (PDF link) OR uploaded PDF")

if missing:
    st.markdown('<div class="bad"><b>Missing required input:</b><br/>' + "<br/>".join(missing) + "</div>", unsafe_allow_html=True)

st.divider()

if st.button("Evaluate", type="primary", use_container_width=True, disabled=bool(missing)):
    # Step 1: get PDF bytes (URL or upload)
    pdf_bytes = None
    pdf_source = ""

    if uploaded_pdf is not None:
        pdf_bytes = uploaded_pdf.read()
        pdf_source = "uploaded PDF"
    elif pdf_ref:
        pdf_source = pdf_ref
        try:
            with st.spinner("Downloading PDF from link..."):
                pdf_bytes = download_pdf_bytes(pdf_ref)
        except Exception as e:
            st.markdown(
                f"""
                <div class="bad">
                  <b>Could not download the PDF from the link.</b><br/>
                  Reason: {str(e)}<br/><br/>
                  <b>What to do:</b><br/>
                  • If this is SharePoint/OneDrive: it may require login and Streamlit can't access it.<br/>
                  • Use a direct downloadable link (not a preview page), OR upload the PDF using the uploader above.
                </div>
                """,
                unsafe_allow_html=True
            )
            st.stop()

    # Step 2: extract text + discussion section
    with st.spinner("Extracting text from PDF (first pages)..."):
        full_text = extract_text_from_pdf(pdf_bytes, max_pages=20)
        discussion = extract_discussion_section(full_text)

    # Step 3: LLM evaluation
    with st.spinner("Evaluating quote + student explanation against Discussion section..."):
        result = evaluate_with_llm(
            quote=cell1_quote,
            student_expl=cell4_expl,
            discussion_text=discussion,
            pdf_ref=pdf_source
        )

    # Step 4: display results
    st.subheader("Results")

    if "error" in result:
        st.markdown('<div class="bad"><b>Error:</b><br/>' + str(result["error"]) + "</div>", unsafe_allow_html=True)
        if "raw" in result:
            st.code(result["raw"])
        st.stop()

    q = result.get("quote_is_discussion_finding", "uncertain")
    acc = result.get("student_summary_accuracy_1_to_5", None)
    conf = result.get("confidence", "low")

    top = st.columns([1, 1, 1])
    with top[0]:
        st.metric("Quote is a Discussion finding?", str(q).upper())
    with top[1]:
        st.metric("Student summary accuracy (1–5)", acc if acc is not None else "—")
    with top[2]:
        st.metric("Confidence", str(conf).upper())

    st.markdown("**Evidence / rationale**")
    st.write(result.get("evidence", ""))

    st.markdown("**Summary issues (if any)**")
    issues = result.get("summary_issues", [])
    if issues:
        for i in issues:
            st.write(f"- {i}")
    else:
        st.write("None noted.")

    st.markdown("**Suggested professor feedback**")
    st.write(result.get("suggested_professor_feedback", ""))

    with st.expander("Discussion excerpt used (for transparency)"):
        st.text_area("Discussion excerpt", discussion or "[No discussion section detected — model may report 'uncertain']",
                     height=260)

st.caption("Tip: If your Excel ‘PDF link’ cell is a clickable hyperlink with a friendly title, Excel may paste only the title. For fastest grading, store the PDF URL as plain text in the cell (starts with https://).")

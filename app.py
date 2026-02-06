import os
import re
import io
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

st.markdown(
    f"""
    <style>
      :root {{
        --blue: {BLUE};
        --gold: {GOLD};
      }}
      .block-container {{ padding-top: 2rem; }}
      h1, h2, h3 {{ color: var(--blue); }}
      .card {{
        border-left: 10px solid var(--blue);
        background: #ffffff;
        padding: 1rem 1.25rem;
        border-radius: 14px;
        box-shadow: 0 2px 10px rgba(0,0,0,.06);
      }}
      .subtle {{
        color: #384152;
        font-size: 0.95rem;
      }}
      .pill {{
        display:inline-block;
        padding: .15rem .55rem;
        border-radius: 999px;
        background: rgba(212,175,55,.18);
        border: 1px solid rgba(212,175,55,.45);
        color: #3a2d00;
        font-size: .85rem;
        margin-left: .35rem;
      }}
      .preview {{
        border: 1px solid rgba(11,45,92,.25);
        border-radius: 12px;
        padding: .75rem .9rem;
        background: rgba(11,45,92,.03);
        white-space: pre-wrap;
      }}
      .ok {{
        border-left: 8px solid #1f7a1f;
        background: rgba(31,122,31,.08);
        padding: .75rem 1rem;
        border-radius: 12px;
      }}
      .warn {{
        border-left: 8px solid #b45309;
        background: rgba(180,83,9,.08);
        padding: .75rem 1rem;
        border-radius: 12px;
      }}
    </style>
    """,
    unsafe_allow_html=True
)

st.title("Discussion Finding & Summary Checker")

st.markdown(
    """
    <div class="card">
      <div class="subtle">
        Paste <b>six Excel cells</b> (one horizontal row). Only these are used:
        <ul>
          <li><b>Cell 1</b>: direct quote from the <b>Discussion</b></li>
          <li><b>Cell 4</b>: student 1–2 sentence explanation</li>
          <li><b>Cell 6</b>: PDF link (preferred) or PDF filename (upload fallback)</li>
        </ul>
        Cells 2, 3, and 5 are ignored.
        <span class="pill">Blue & Gold</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

# =========================
# Helpers
# =========================
def split_cells(text: str):
    """
    Robust splitter for Excel/Sheets pastes.
    - splits on tabs
    - splits on newlines
    - splits on 2+ spaces (when tabs get collapsed by browser)
    IMPORTANT: Do NOT delete empty cells — we must preserve positions.
    """
    t = (text or "").replace("\r", "").strip()

    # Split on: tabs OR newlines OR 2+ spaces
    parts = re.split(r"\t+|\n+|\s{2,}", t)

    # Preserve empties by not filtering them out; just strip each
    parts = [p.strip() for p in parts]

    # If user pasted fewer than 6 chunks, pad to 6
    if len(parts) < 6:
        parts = parts + [""] * (6 - len(parts))

    # If they pasted more than 6, keep first 6 (extra usually comes from weird wrap)
    # BUT: if the quote itself contains extra splits, we still want first 6 columns.
    return parts[:6]


def looks_like_url(s: str) -> bool:
    return bool(re.match(r"^https?://", (s or "").strip(), re.I))


def looks_like_pdf_ref(s: str) -> bool:
    s = (s or "").strip().lower()
    return (".pdf" in s)


def download_pdf(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content


def extract_text_from_pdf(pdf_bytes: bytes, max_pages: int = 12) -> str:
    text_chunks = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            txt = page.extract_text() or ""
            if txt.strip():
                text_chunks.append(txt)
    return "\n\n".join(text_chunks).strip()


def extract_discussion_snippet(full_text: str) -> str:
    """
    Best-effort: find the Discussion section and return a chunk.
    If we can't find it, return the tail end of the paper.
    """
    if not full_text:
        return ""

    t = full_text

    # Try to find "Discussion" heading
    m = re.search(r"\n\s*discussion\s*\n", t, re.I)
    if m:
        start = m.start()
        # Try to stop at next big section like Conclusion/Limitations/References
        m2 = re.search(r"\n\s*(conclusion|limitations|implications|references|bibliography|appendix)\s*\n", t[start:], re.I)
        end = start + (m2.start() if m2 else min(len(t[start:]), 8000))
        return t[start:end].strip()

    # Fallback: last chunk
    return t[-8000:].strip()


def openai_client():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return OpenAI(api_key=key)


# =========================
# Input
# =========================
raw = st.text_area("Paste six Excel cells here", height=160)

cells = split_cells(raw)
cell1 = cells[0]
cell4 = cells[3]
cell6 = cells[5]

with st.expander("🔎 Preview (what the app detected for Cells 1, 4, 6)", expanded=True):
    c1, c4, c6 = st.columns(3)
    with c1:
        st.markdown("**Cell 1 (Quote)**")
        st.markdown(f"<div class='preview'>{cell1 if cell1 else '[empty]'}</div>", unsafe_allow_html=True)
    with c4:
        st.markdown("**Cell 4 (Student explanation)**")
        st.markdown(f"<div class='preview'>{cell4 if cell4 else '[empty]'}</div>", unsafe_allow_html=True)
    with c6:
        st.markdown("**Cell 6 (PDF link / reference)**")
        st.markdown(f"<div class='preview'>{cell6 if cell6 else '[empty]'}</div>", unsafe_allow_html=True)

# Optional upload fallback
uploaded_pdf = st.file_uploader("Upload PDF (optional fallback if Cell 6 is not a direct URL)", type=["pdf"])

# =========================
# Validation (meaningful)
# =========================
def fail(msg: str):
    st.markdown(f"<div class='warn'><b>Fix needed:</b> {msg}</div>", unsafe_allow_html=True)
    st.stop()

# Quote must be substantive
if len(cell1.strip()) < 10:
    fail("Cell 1 (direct quote) appears missing. Paste a real Discussion quote into Cell 1.")

# Student explanation must be substantive
if len(cell4.strip()) < 10:
    fail("Cell 4 (student explanation) appears missing. Put the 1–2 sentence explanation in Cell 4.")

# Need PDF source: URL preferred, upload fallback allowed
pdf_bytes = None
pdf_source_desc = ""

if looks_like_url(cell6) and looks_like_pdf_ref(cell6):
    try:
        pdf_bytes = download_pdf(cell6.strip())
        pdf_source_desc = "PDF downloaded from Cell 6 URL."
    except Exception as e:
        st.markdown(
            f"<div class='warn'><b>Could not download PDF from Cell 6.</b><br/>"
            f"Error: {str(e)}<br/>"
            f"Use a direct PDF URL in Cell 6, or upload the PDF below.</div>",
            unsafe_allow_html=True
        )

if pdf_bytes is None and uploaded_pdf is not None:
    pdf_bytes = uploaded_pdf.read()
    pdf_source_desc = "PDF uploaded via file uploader."

if pdf_bytes is None:
    fail("Cell 6 must contain a direct PDF URL (ending in .pdf), OR upload the PDF using the uploader.")

# =========================
# Evaluate
# =========================
st.write("")  # spacer
if st.button("Evaluate"):
    st.markdown(f"<div class='ok'><b>PDF source:</b> {pdf_source_desc}</div>", unsafe_allow_html=True)

    # Extract PDF text
    with st.spinner("Reading PDF…"):
        full_text = extract_text_from_pdf(pdf_bytes)

    if not full_text:
        fail("Could not extract text from the PDF. Try a different PDF or upload a text-based PDF (not scanned images).")

    discussion_text = extract_discussion_snippet(full_text)

    # OpenAI analysis
    client = openai_client()
    if client is None:
        st.markdown(
            "<div class='warn'><b>OPENAI_API_KEY not set.</b> "
            "Your app can parse cells and PDFs, but it can't evaluate the quote/summary until you add the API key in Streamlit Secrets.</div>",
            unsafe_allow_html=True
        )
        st.stop()

    prompt = f"""
You are helping a professor grade academic writing.

TASK A (Source check):
Determine whether the quoted sentence in Cell 1 appears to be an actual finding/claim stated in the Discussion section of the provided article text.
- If the quote is not found verbatim, judge whether it is strongly supported by the Discussion text anyway.
- Return: FOUND_VERBATIM (yes/no), DISCUSSION_SUPPORT (strong/moderate/weak), and a 2–4 sentence justification.

TASK B (Student summary check):
Evaluate whether Cell 4 is a fair representation of the meaning of Cell 1.
- Return: FAIR_REPRESENTATION (yes/partly/no)
- List 1–3 specific reasons, focusing on overclaiming, missing qualifiers, or introducing new ideas.

CELL 1 QUOTE:
{cell1}

CELL 4 STUDENT EXPLANATION:
{cell4}

DISCUSSION TEXT (extracted):
{discussion_text}
"""

    with st.spinner("Evaluating…"):
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a precise academic grading assistant. Be strict about overclaims and missing qualifiers."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2
        )

    output = resp.choices[0].message.content

    st.subheader("Results")
    st.write(output)

    st.subheader("Quick rubric suggestion (optional)")
    st.markdown(
        """
- **A (Source check):**  
  - Strong + Found verbatim = likely acceptable  
  - Moderate support = check context  
  - Weak support = likely not a valid “finding from Discussion”
- **B (Representation check):**  
  - Yes = faithful summary  
  - Partly = some drift/overclaim  
  - No = misrepresents the quote
"""
    )

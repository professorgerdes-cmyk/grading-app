import os
import re
import io
import requests
import pdfplumber
import streamlit as st
from openai import OpenAI

# ---------- Page setup ----------
st.set_page_config(page_title="Academic Quote Checker", layout="wide")

# ---------- Colors ----------
BLUE = "#0B2D5C"
GOLD = "#D4AF37"

st.markdown(f"""
<style>
body {{ background-color: #f7f9fc; }}
h1, h2, h3 {{ color: {BLUE}; }}
.card {{
    background: white;
    padding: 16px;
    border-radius: 10px;
    border-left: 6px solid {BLUE};
    margin-bottom: 16px;
}}
.badge {{
    display:inline-block;
    padding:6px 10px;
    border-radius:20px;
    background:{GOLD};
    font-weight:700;
}}
</style>
""", unsafe_allow_html=True)

st.title("Discussion Finding & Summary Checker")

st.markdown("""
<div class="card">
Paste <b>six Excel cells</b>.  
Only these are used:
<ul>
<li><b>Cell 1</b>: direct quote</li>
<li><b>Cell 4</b>: student summary</li>
<li><b>Cell 6</b>: PDF link</li>
</ul>
Cells 2, 3, and 5 are ignored.
</div>
""", unsafe_allow_html=True)

# ---------- Helpers ----------
def split_cells(text):
    parts = re.split(r"\t+|\r?\n+|\s{2,}", text.strip())
    parts = [p.strip() for p in parts]
    while len(parts) < 6:
        parts.append("")
    return parts[:6]

def fetch_pdf(url):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.content if r.content[:4] == b"%PDF" else None

def extract_text(pdf_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:30]:
            text += page.extract_text() or ""
    return text

def discussion_section(text):
    t = text.lower()
    if "discussion" not in t:
        return None
    start = t.find("discussion")
    end = min(
        [i for i in [
            t.find("conclusion", start),
            t.find("references", start)
        ] if i != -1] + [len(t)]
    )
    return text[start:end]

# ---------- Input ----------
cells_input = st.text_area("Paste six Excel cells here", height=150)

if st.button("Evaluate"):
    cells = split_cells(cells_input)
    quote = cells[0]
    summary = cells[3]
    link = cells[5]

    if not quote or not summary or not link:
        st.error("Cells 1, 4, and 6 must not be empty.")
        st.stop()

    st.markdown("## Results")

    # ----- PDF check -----
    try:
        pdf = fetch_pdf(link)
        if not pdf:
            st.warning("Link does not appear to be a direct PDF.")
        else:
            text = extract_text(pdf)
            discussion = discussion_section(text)
            if discussion and quote.lower() in discussion.lower():
                st.success("Quote appears in the Discussion section.")
            else:
                st.warning("Quote not clearly found in the Discussion section.")
    except Exception as e:
        st.error(f"PDF error: {e}")

    # ----- Summary grading -----
    if not os.getenv("OPENAI_API_KEY"):
        st.warning("OPENAI_API_KEY not set. Summary grading skipped.")
    else:
        client = OpenAI()
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=f"""
QUOTE:
{quote}

STUDENT SUMMARY:
{summary}

Is the summary a fair representation of the quote?
Return:
- fair (true/false)
- score 0-10
- what is correct
- what is missing
- what is incorrect
"""
        )
        st.markdown("### Summary Evaluation")
        st.code(response.output_text)

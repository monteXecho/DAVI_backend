import fitz  # PyMuPDF
import re
import os
from rapidfuzz import fuzz
import tempfile
import shutil


def split_snippet(snippet_text):
    """
    Smartly splits a snippet into meaningful segments:
    - Keeps dialogue pairs
    - Handles sentence boundaries
    - Extracts headings and numbered sections
    - Preserves order and context
    """
    if not snippet_text:
        return []

    # Normalize
    snippet_text = snippet_text.replace("\f", " ").replace("\n", " ").strip()

    # Step 1: Break on speaker turns (e.g., A: "...", B: "...")
    dialogue_chunks = re.split(r'(?=\s[AB]:)', snippet_text)

    segments = []
    buffer = ""

    for chunk in dialogue_chunks:
        if not chunk or not isinstance(chunk, str):
            continue
        chunk = chunk.strip()
        if re.match(r"^[AB]:", chunk):
            if buffer:
                segments.extend(re.split(r'(?<=[.?!])\s+(?=[A-Z])', buffer.strip()))
                buffer = ""
            segments.append(chunk)
        else:
            buffer += " " + chunk

    if buffer:
        segments.extend(re.split(r'(?<=[.?!])\s+(?=[A-Z])', buffer.strip()))

    # Step 2: Further split long paragraphs on sentence end or numbered headers
    final_segments = []
    for seg in segments:
        if not seg or not isinstance(seg, str):
            continue
        numbered = re.split(r'(?=\s*\d{1,2}[.)]\s+)', seg)
        for part in numbered:
            part = part.strip()
            if part:
                final_segments.append(part.replace("” ", "”").replace("“ ", "“").strip())

    return [s for s in final_segments if s]


def find_best_window_match(page_text, full_snippet, window_margin=10, threshold=60):
    full_words = full_snippet.strip().split()
    text_words = page_text.strip().split()
    target_len = len(full_words)
    best_score = 0
    best_trimmed = ""

    for size in range(target_len - window_margin, target_len + window_margin + 1, 2):
        if size <= 0 or size > len(text_words):
            continue
        for i in range(0, len(text_words) - size + 1, 2):
            window_words = text_words[i:i + size]
            window = " ".join(window_words)
            score = fuzz.ratio(full_snippet, window)
            if score > best_score:
                best_score = score
                best_trimmed = window
            if best_score >= 95:
                return best_trimmed

    return best_trimmed if best_score >= threshold else None


def highlight_text(page, text):
    found = page.search_for(text)
    if not found:
        return False
    for rect in found:
        page.add_highlight_annot(rect)
    return True


def fuzzy_match(seg, block, threshold=70):
    seg = seg.strip()
    block = block.strip()
    if seg in block:
        return True
    if len(seg) < 10:
        return False
    for i in range(0, len(block) - len(seg) + 1, max(10, len(seg) // 4 or 1)):
        window = block[i:i + len(seg) + 30]
        score = fuzz.ratio(seg, window)
        if score >= threshold:
            return True
    return False


def find_and_highlight(pdf_filename, snippet_text, target_page, output_path):
    # Determine input source: use highlighted version if available
    highlighted_path = os.path.join("output", "highlighted", pdf_filename)
    fallback_path = os.path.join("documenten-import", pdf_filename)

    if os.path.exists(highlighted_path):
        pdf_path = highlighted_path
        print(f"📂 Using existing highlighted file: {highlighted_path}")
    elif os.path.exists(fallback_path):
        pdf_path = fallback_path
        print(f"📂 Using original input file: {fallback_path}")
    else:
        raise FileNotFoundError(f"❌ Neither highlighted nor original file found for: {pdf_filename}")

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    segments = split_snippet(snippet_text)
    full_snippet = " ".join(segments)

    best_page_index = -1
    best_text_block = ""
    best_score = 0

    for i in range(target_page - 1, target_page + 2):
        if i < 0 or i >= total_pages:
            continue
        page = doc[i]
        page_text = page.get_text()
        text_block = find_best_window_match(page_text, full_snippet)
        if text_block:
            score = fuzz.ratio(full_snippet, text_block)
            if score > best_score:
                best_score = score
                best_page_index = i
                best_text_block = text_block
            if best_score >= 95:
                break

    if best_page_index == -1 or not best_text_block:
        print("❌ No matching block found.")
        doc.close()
        return

    matched = []
    for i, seg in enumerate(segments):
        if fuzzy_match(seg, best_text_block):
            matched.append(i)

    if not matched:
        print("❌ No segments matched inside best text block.")
        doc.close()
        return

    first_idx = matched[0]
    last_idx = matched[-1]

    highlight_start = first_idx
    while highlight_start > 0 and fuzzy_match(segments[highlight_start - 1], best_text_block):
        highlight_start -= 1

    highlight_end = last_idx
    while highlight_end + 1 < len(segments) and fuzzy_match(segments[highlight_end + 1], best_text_block):
        highlight_end += 1

    highlight_start = max(0, highlight_start - 1)
    highlight_end = min(len(segments) - 1, highlight_end + 1)

    highlight_text_to_search = " ".join(segments[highlight_start:highlight_end + 1])

    best_page = doc[best_page_index]
    if not highlight_text(best_page, highlight_text_to_search):
        print("⚠️ Exact match not found. Falling back to fuzzy window match.")
        fallback = find_best_window_match(best_page.get_text(), highlight_text_to_search)
        if fallback:
            highlight_text(best_page, fallback)
            print("✅ Fuzzy fallback matched and highlighted.")
        else:
            print("❌ Highlight failed.")
    else:
        print("✅ Highlight successful.")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Handle save overwrite case
    if os.path.abspath(output_path) == os.path.abspath(pdf_path):
        # Avoid saving over the opened input: write to temp and replace
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            temp_path = tmp.name
        doc.save(temp_path, incremental=False, garbage=1, deflate=False)
        doc.close()
        shutil.move(temp_path, output_path)
    else:
        if os.path.exists(output_path):
            os.remove(output_path)
        doc.save(output_path, incremental=False, garbage=1, deflate=False)
        doc.close()



# --- Usage ---
# if __name__ == "__main__":
#     pdf_path = "documenten-import/Beleidstuk Normen en Waarden.pdf"
#     output_path = "output/highlighted_output.pdf"
#     target_page = 6  # zero-based index

#     snippet_text = "kinderopvang organisatie kan bieden binnen redelijke kaders. 6.25 Een Kind Wil Geen Groepsactiviteiten Doen • Handelwijze: Zoek alternatieven die aansluiten bij de interesses van het kind. 6.26 Een Oudere Broer of Zus Vertoont Grensoverschrijdend Gedrag • Handelwijze: Bespreek dit met de ouders en evalueer de veiligheid van andere kinderen. 6.27 Een Collega Vertoont Ongewenst Gedrag Zie eerder hoofdstuk. 6.28 Ouders Hebben Tegengestelde Opvoedideeën • Handelwijze: Zoek naar een middenweg die het welzijn van het kind centraal stelt. 6.29 Een Kind Is Vaak Afwezig • Handelwijze: Bespreek dit met de ouders en onderzoek of er onderliggende problemen zijn. oorbeeldgesprekken tussen collega’s V 7. Voorbeeldgesprekken\f1. Nieuwe collega heeft vragen A: “Hoe vul jij het dagritme in?” B: “We starten met een kringmoment, daarna spelen de kinderen vrij.” A: “En hoe plan je activiteiten?” B: “Ik kijk wat aansluit bij hun interesses. Heb je ideeën?”"

#     find_and_highlight(pdf_path, snippet_text, target_page, output_path)

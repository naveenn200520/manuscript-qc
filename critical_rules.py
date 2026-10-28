"""
Critical Rules — Job-Sheet-Driven Manuscript Validation
========================================================
Six mandatory critical rules that run ONLY when explicitly enabled
in the active job sheet. No AI inference, no fallback rules.

Each rule function returns a list of error dicts compatible with
ErrorRecord (see models.py) and the existing qc_engine.make_error format.

The approved original manuscript is the baseline for comparison rules.
"""

import re
import difflib


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _offset_to_line_col(text, offset):
    """Convert character offset to 1-indexed (line, col)."""
    if not text or offset < 0:
        return (1, 1)
    offset = min(offset, len(text))
    before = text[:offset]
    line = before.count('\n') + 1
    last_nl = before.rfind('\n')
    col = offset - last_nl
    return (line, col)


def _make_critical_error(error_code, original_value, current_value,
                         page_or_line, column_or_field, rule_id,
                         suggested_action, description,
                         start_offset=0, end_offset=0,
                         full_text=''):
    """Build a standardised critical error dict.

    Every critical error includes:
      - Original value, Current value
      - page/line, column or field
      - Rule ID (error_code)
      - Suggested action
    """
    # If offset not explicitly provided or is 0, try to locate current_value or original_value in full_text
    if full_text and (start_offset is None or start_offset <= 0):
        if current_value and isinstance(current_value, str) and current_value in full_text:
            pos = full_text.find(current_value)
            if pos >= 0:
                start_offset = pos
                end_offset = pos + len(current_value)
        elif original_value and isinstance(original_value, str) and original_value in full_text:
            pos = full_text.find(original_value)
            if pos >= 0:
                start_offset = pos
                end_offset = pos + len(original_value)

    if full_text and start_offset is not None and start_offset >= 0:
        line, col = _offset_to_line_col(full_text, start_offset)
    else:
        line = page_or_line if isinstance(page_or_line, int) else 1
        col = column_or_field if isinstance(column_or_field, int) else 1

    return {
        'error_type': error_code,
        'severity': 'CRITICAL',
        'category': 'CRITICAL',
        'line_number': line,
        'column_number': col,
        'start_offset': start_offset if start_offset is not None else 0,
        'end_offset': end_offset if end_offset is not None else 0,
        'original_text': str(original_value) if original_value is not None else '',
        'edited_text': str(current_value) if current_value is not None else '',
        'context_before': '',
        'context_after': '',
        'description': description,
        'rule_id': rule_id,
        'suggested_action': suggested_action,
        'field': column_or_field if isinstance(column_or_field, str) else None,
    }


# ─────────────────────────────────────────────
# SECTION EXTRACTION HELPERS
# ─────────────────────────────────────────────

_EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
)

_REFERENCE_SECTION_HEADERS = [
    r'^[ \t#*]*(?:\d+[\.\)]\s+)?(?:REFERENCES?|References?|Bibliography|BIBLIOGRAPHY|Literature\s+[Cc]ited|Reference\s+List|References\s+and\s+Notes)[ \t\r:*]*$',
]


def _find_reference_section(text):
    """Return (start_offset, ref_section_text) or (None, None). Truncates before Tables/Figures."""
    best_match = None
    for pat in _REFERENCE_SECTION_HEADERS:
        matches = list(re.finditer(pat, text, re.MULTILINE))
        if matches:
            # Pick the last match to avoid Table of Contents matches
            m = matches[-1]
            if not best_match or m.start() > best_match.start():
                best_match = m
    
    if best_match:
        ref_text = text[best_match.start():]
        header_len = best_match.end() - best_match.start()
        after_header = ref_text[header_len:]
        end_match = re.search(
            r'(?im)^\s*(?:Table\s+\d+|Figure\s+\d+|Fig\.\s*\d+|Tables\b|Figures\b|Appendix|Supplementary|SUPPLEMENTARY|APPENDIX|Author\s+Contributions?|Funding|Conflicts?\s+of\s+Interest|Declarations?|Data\s+Availability)\b',
            after_header
        )
        if end_match:
            trunc_point = header_len + end_match.start()
            return best_match.start(), ref_text[:trunc_point]
        return best_match.start(), ref_text
    return None, None


def _extract_reference_entries(ref_text):
    """Extract individual reference entries from reference section text.

    Handles numbered references like:
      [1] Author ...
      [1]. Author ...
      1. Author ...
      1) Author ...
      (1) Author ...
      1\tAuthor ...
      ¹ Author ...
    And filters out non-citation text like table rows.
    """
    entries = []
    num_pat = re.compile(
        r'^[ \t]*(?:\[(\d{1,4})\]|\((\d{1,4})\)|(\d{1,4})[.)\t])[.\s]*\s*(\S.+)',
        re.MULTILINE
    )
    for m in num_pat.finditer(ref_text):
        num_str = m.group(1) or m.group(2) or m.group(3)
        content = m.group(4).strip()
        alpha_count = sum(1 for c in content if c.isalpha())
        if len(content) < 15 or alpha_count < 10:
            continue
        entries.append({
            'number': int(num_str),
            'text': content,
            'offset': m.start(),
            'full_match': m.group(0),
        })

    if not entries:
        # Also check for superscript numbered references like: ¹ Author ... or [¹] Author ...
        sup_pat = re.compile(
            r'^[ \t]*(?:\[([¹²³⁴⁵⁶⁷⁸⁹⁰]{1,4})\]|([¹²³⁴⁵⁶⁷⁸⁹⁰]{1,4}))[.\s]*\s*(\S.+)',
            re.MULTILINE
        )
        for m in sup_pat.finditer(ref_text):
            raw = m.group(1) or m.group(2)
            num_str = ''.join(SUPERSCRIPT_CHAR_MAP.get(c, c) for c in raw)
            if not num_str.isdigit():
                continue
            content = m.group(3).strip()
            alpha_count = sum(1 for c in content if c.isalpha())
            if len(content) < 15 or alpha_count < 10:
                continue
            entries.append({
                'number': int(num_str),
                'text': content,
                'offset': m.start(),
                'full_match': m.group(0),
            })

    return entries


def _extract_body_text(full_text):
    """Return the manuscript body text (everything before the References section)."""
    ref_start, _ = _find_reference_section(full_text)
    if ref_start is not None:
        return full_text[:ref_start]
    return full_text


def _extract_citation_order_from_body(body_text):
    """Scan the body text and return citation numbers in the order they first appear.

    Only matches square brackets [1], [1, 2], [1-3] or superscripts.
    Does NOT match parentheses (1) to prevent treating measurements/counts like (4 mm) as citations.
    """
    seen = set()
    order = []
    bracket_pat = re.compile(r'\[(\d+(?:[,;\s]+\d+|\s*[-–]\s*\d+)*)\]')
    for m in bracket_pat.finditer(body_text):
        inner = m.group(1)
        parts = re.split(r'[,;\s]+', inner)
        for part in parts:
            range_m = re.match(r'(\d+)\s*[-–]\s*(\d+)', part)
            if range_m:
                lo, hi = int(range_m.group(1)), int(range_m.group(2))
                for n in range(lo, hi + 1):
                    if n not in seen:
                        seen.add(n)
                        order.append(n)
            elif part.strip().isdigit():
                n = int(part.strip())
                if n not in seen:
                    seen.add(n)
                    order.append(n)

    if not order:
        sup_pat = re.compile(r'([¹²³⁴⁵⁶⁷⁸⁹⁰]+(?:[,;\s]+[¹²³⁴⁵⁶⁷⁸⁹⁰]+|\s*[-–]\s*[¹²³⁴⁵⁶⁷⁸⁹⁰]+)*)')
        for m in sup_pat.finditer(body_text):
            raw = m.group(1)
            asc = ''.join(SUPERSCRIPT_CHAR_MAP.get(c, c) for c in raw)
            parts = re.split(r'[,;\s]+', asc)
            for part in parts:
                range_m = re.match(r'(\d+)\s*[-–]\s*(\d+)', part)
                if range_m:
                    lo, hi = int(range_m.group(1)), int(range_m.group(2))
                    for n in range(lo, hi + 1):
                        if n not in seen:
                            seen.add(n)
                            order.append(n)
                elif part.strip().isdigit():
                    n = int(part.strip())
                    if n not in seen:
                        seen.add(n)
                        order.append(n)

    return order


def _extract_author_year_references_enhanced(ref_section):
    """Parse author-year (non-numbered) reference entries from the reference section.

    Returns a list of dicts:
      {'text': str, 'offset': int, 'surname': str, 'author_count': int, 'year': int}
    """
    ref_lines = ref_section.split('\n')
    references = []
    current_ref = ''
    current_offset = 0

    for line in ref_lines[1:]:  # Skip the header line
        stripped = line.strip()
        if not stripped:
            if current_ref:
                references.append({'text': current_ref.strip(), 'offset': current_offset})
                current_ref = ''
            continue

        # A new reference typically starts with an author surname (capital letter)
        is_new_ref = bool(re.match(r'^[A-Z][a-z]', stripped))
        if is_new_ref and current_ref:
            references.append({'text': current_ref.strip(), 'offset': current_offset})
            current_ref = stripped
            current_offset = ref_section.find(stripped, current_offset + len(current_ref) if current_ref else 0)
            if current_offset < 0:
                current_offset = ref_section.find(stripped)
        elif not current_ref:
            current_ref = stripped
            current_offset = ref_section.find(stripped)
        else:
            current_ref += ' ' + stripped

    if current_ref:
        references.append({'text': current_ref.strip(), 'offset': current_offset})

    # Enrich each entry with sort-key fields
    enriched = []
    for ref in references:
        txt = ref['text']
        # Extract primary author surname
        surname_match = re.match(r'^([A-Z][a-z]+)', txt)
        surname = surname_match.group(1).lower() if surname_match else ''

        # Extract year
        year_match = re.search(r'\b((?:19|20)\d{2})[a-z]?\b', txt)
        year = int(year_match.group(1)) if year_match else 9999

        # Determine author count from the text before the year
        pre_year = txt[:year_match.start()].strip() if year_match else txt
        # Count "and" / "&" separators to estimate author count
        # Single author:  "Smith (2020)"
        # Two authors:    "Smith and Jones (2020)" or "Smith & Jones (2020)"
        # 3+ authors:     "Smith, Jones, and Lee (2020)" or "Smith et al. (2020)"
        if re.search(r'\bet\s+al', pre_year, re.IGNORECASE):
            author_count = 3  # treat "et al." as 3+ authors
        else:
            # Count commas and "and"/"&" to estimate
            separators = len(re.findall(r'\band\b|&', pre_year, re.IGNORECASE))
            commas = pre_year.count(',')
            author_count = max(1, separators + 1, commas + 1 if commas else 1)
            if author_count > 3:
                author_count = 3  # bucket as "3+"

        enriched.append({
            'text': txt,
            'offset': ref['offset'],
            'surname': surname,
            'author_count': min(author_count, 3),  # 1, 2, or 3+
            'year': year,
        })

    return enriched


def _detect_citation_style_from_job_sheet(job_sheet_instructions):
    """Detect the citation style from job sheet instructions.

    Returns one of: 'numbered', 'author_year', 'unnumbered', or None.
    """
    if not job_sheet_instructions:
        return None

    citation_sections = ['citation', 'reference', 'bibliography', 'ref style', 'cite style']

    for instr in job_sheet_instructions:
        section = (instr.section if hasattr(instr, 'section') else instr.get('section', '')).lower().strip()
        instruction = (instr.instruction if hasattr(instr, 'instruction') else instr.get('instruction', '')).lower().strip()
        status = instr.status if hasattr(instr, 'status') else instr.get('status', '')
        if status == 'Manual Review Required':
            continue

        combined = f'{section} {instruction}'

        is_relevant_section = any(k in section for k in citation_sections)
        if not is_relevant_section and not any(k in instruction for k in ['citation', 'reference', 'bibliography']):
            continue

        # Detect numbered style
        if re.search(r'\b(?:numbered|numeric|numerical|vancouver|order\s+of\s+appearance|ama|ieee|nlm)\b', combined):
            return 'numbered'

        # Detect author-year style
        if ('author' in combined and 'year' in combined) or \
           re.search(r'\b(?:author[\s\-]year|alphabetical|harvard|apa(?:\s*\d+)?|chicago|mla)\b', combined):
            return 'author_year'

        # Detect unnumbered style
        if re.search(r'\bunnumbered\b', combined):
            return 'unnumbered'

    return None


def _find_section_headings(text):
    """Find section headings in the manuscript body text.

    Returns list of dicts with 'text', 'offset', 'end_offset'.
    """
    headings = []
    heading_keywords = {
        'abstract', 'introduction', 'background', 'methods', 'materials',
        'results', 'discussion', 'conclusion', 'conclusions', 'case',
        'presentation', 'findings', 'treatment', 'outcome', 'objective',
        'objectives', 'acknowledgments', 'acknowledgements', 'references',
        'bibliography', 'appendix', 'supplementary', 'funding',
        'declarations', 'ethics', 'consent', 'availability',
        'competing', 'conflict', 'author', 'contributions',
    }
    for m in re.finditer(r'^[ \t]*([A-Za-z0-9][^\n]{0,150})[ \t\r]*$', text, re.MULTILINE):
        line = m.group(1).strip()
        # Skip sentences ending with period (unless it's a numbered heading like "1.")
        if line.endswith('.') and not re.match(r'^\d+\.$', line):
            continue
        # Skip very long lines (likely paragraphs)
        if len(line) > 100:
            continue
        line_lower = line.lower()
        is_heading = (
            line.isupper() or
            any(kw == w.strip('.,:;').lower() for w in line.split() for kw in heading_keywords)
        )
        if is_heading:
            headings.append({
                'text': line,
                'offset': m.start(1),
                'end_offset': m.end(1),
            })
    return headings


def _extract_authors_block(text):
    """Extract author names from the beginning of the manuscript.

    Returns list of author name strings in order.
    """
    authors = _parse_authors_with_markers(text)
    return [a['name'] for a in authors]


def _extract_affiliations_block(text):
    """Extract affiliations from the manuscript header.

    Returns list of dicts with 'text' and 'marker' keys.
    """
    affils = _parse_affiliations_with_markers(text)
    return [{'text': a['text'], 'marker': a['marker'], 'raw': a['raw_line']} for a in affils]


def _extract_title(text):
    """Extract the article title from the manuscript."""
    if not text:
        return ''

    # First, look for an explicit "Title:" or "Article Title:" prefix
    m = re.search(r'(?im)^\s*(?:title|article\s+title|manuscript\s+title)\s*[:\-]\s*(.+)$', text)
    if m:
        title = m.group(1).strip()
        lines = text[m.end():].split('\n')
        for l in lines:
            s = l.strip()
            if not s or re.match(r'(?i)^(?:authors?|affiliations?|abstract|running)\b', s) or '@' in s:
                break
            if len(title.split()) > 40:
                break
            title += ' ' + s
        return title.strip()

    # Fallback: scan lines before authors/abstract
    lines = text.split('\n')
    title_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if title_lines:
                break
            continue
        if re.match(r'(?i)^\s*title\s+page\b', stripped):
            continue
        if re.match(r'(?i)^\s*(?:authors?|affiliations?|abstract|introduction)\b', stripped):
            break
        affil_kws = {'university', 'hospital', 'department', 'institute', 'faculty',
                     'abstract', 'introduction', 'keywords'}
        if any(kw in stripped.lower() for kw in affil_kws):
            break
        if '@' in stripped:
            break
        title_lines.append(stripped)
        if len(title_lines) >= 4:
            break

    title = ' '.join(title_lines)
    title = re.sub(r'^(?i:title|article[\s\-]title|authors?|affiliations?)\s*[:\-]?\s*', '', title).strip()
    return title


def _extract_corresponding_email(text):
    """Extract the corresponding author's email address."""
    corr_patterns = [
        r'[Cc]orrespond(?:ing|ence)\s*(?:author)?\s*[:\-]?\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        r'\*\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        r'[Ee]-?mail\s*[:\-]\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    ]
    for pat in corr_patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    abstract_match = re.search(r'\b(?:Abstract|ABSTRACT)\b', text)
    header = text[:abstract_match.start()] if abstract_match else text[:3000]
    m = _EMAIL_PATTERN.search(header)
    return m.group() if m else None


SUPERSCRIPT_CHAR_MAP = {
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
    '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9'
}
DIGIT_TO_SUPERSCRIPT = {v: k for k, v in SUPERSCRIPT_CHAR_MAP.items()}

def to_superscript_str(n):
    """Convert int or digit string to unicode superscript characters."""
    return ''.join(DIGIT_TO_SUPERSCRIPT.get(c, c) for c in str(n))


_AFFIL_KEYWORDS = {
    'university', 'hospital', 'department', 'institute', 'faculty',
    'school', 'college', 'centre', 'center', 'clinic', 'research',
    'foundation', 'laboratory', 'medicine', 'science', 'division', 'unit'
}

_DISALLOWED_NAME_WORDS = {
    'contributed', 'author', 'authors', 'editor', 'writing', 'curation',
    'analysis', 'investigation', 'supervision', 'validation', 'visualization',
    'administration', 'resources', 'software', 'declaration', 'disclosure',
    'funding', 'conflict', 'study', 'prospective', 'observational', 'firmness',
    'cardiopulmonary', 'bypass', 'ultrafiltration', 'increases', 'viscoelastic',
    'clot', 'title', 'technologies', 'proficiency', 'redundancy', 'responsibility'
}


def _parse_authors_with_markers(text):
    """Parse author names along with their raw markers and numbers.

    Returns a list of dicts:
      {
        'name': str,
        'raw_token': str,
        'marker': str,
        'sup_nums': list[int],
        'reg_nums': list[int],
        'all_nums': list[int],
        'is_sup': bool,
        'is_corr': bool,
        'start_offset': int,
        'end_offset': int
      }
    """
    if not text:
        return []
    section_m = re.search(r'(?im)^\s*(?:Abstract|Introduction|Background|Methods|Results|Discussion|References|Bibliography)\b', text)
    header = text[:section_m.start()] if section_m else text[:4000]
    lines = header.split('\n')

    author_lines = []
    curr_off = 0
    seen_first_title_line = False

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        line_start = curr_off + (len(raw_line) - len(raw_line.lstrip()))
        curr_off += len(raw_line) + 1

        if not line:
            continue

        line_lower = line.lower()

        # Stop conditions once author lines have been detected
        if author_lines:
            if re.match(r'^[¹²³⁴⁵⁶⁷⁸⁹⁰0-9]+[.\-:\s\t]+', line):
                break
            if any(line_lower.startswith(k) for k in ['department', 'division', 'institute', 'faculty', 'school', 'hospital', 'university']):
                break
            if line_lower.startswith('corresponding') or line_lower.startswith('*corresponding') or '@' in line:
                break
            if re.match(r'(?i)^\s*(?:funding|trial\s+registration|disclosure|declaration|author\s+contributions?|contributions?|running\s+title|abstract|introduction)\b', line):
                break
            if 'contributed equally' in line_lower:
                break

        # Skip headers / titles before author line
        if re.match(r'(?i)^\s*(?:title\s+page|title\s*[:\-]|article\s+title\s*[:\-]|running\s+title\s*[:\-])', line):
            seen_first_title_line = True
            continue
        if line_lower.startswith('corresponding') or line_lower.startswith('*corresponding') or '@' in line:
            continue
        if re.match(r'^[¹²³⁴⁵⁶⁷⁸⁹⁰0-9]+[.\-:\s\t]+', line):
            continue
        if any(line_lower.startswith(k) for k in ['department', 'division', 'institute', 'faculty', 'school', 'hospital', 'university']):
            continue
        if re.match(r'(?i)^\s*(?:funding|trial\s+registration|disclosure|declaration|author\s+contributions?|contributions?)\b', line):
            continue
        if 'contributed equally' in line_lower:
            continue

        is_explicit_author_line = bool(re.match(r'^(?i:authors?|author(?:[\s\-]name)?)\s*[:\-]', line))

        # If we haven't seen a title yet and this isn't an explicit "Authors:" line, the first line is the title
        if not seen_first_title_line and not is_explicit_author_line and not author_lines:
            seen_first_title_line = True
            continue

        cleaned = re.sub(r'[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡§\d]', '', line).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        if not cleaned:
            continue

        words = cleaned.split()
        title_words = sum(1 for w in words if w[0:1].isupper() and w.strip('.,;') not in {'and', 'And', 'AND', '&'})
        if title_words >= 1:
            author_lines.append((raw_line, line_start))

    if not author_lines:
        return []

    authors = []
    for raw_line, l_start in author_lines:
        line_str = re.sub(r'^(?i:authors?|author(?:[\s\-]name)?)\s*[:\-]?\s*', '', raw_line).strip()
        prefix_len = len(raw_line) - len(line_str)
        l_start += prefix_len

        parts = re.split(r',\s*(?=[A-Z]|and\s+|&\s+)|\s+and\s+|\s+&\s+', line_str)
        search_pos = 0
        for p in parts:
            p_strip = p.strip().strip('.,;')
            if not p_strip or len(p_strip) < 2:
                continue

            if ':' in p_strip or ';' in p_strip:
                continue

            p_idx = line_str.find(p, search_pos)
            if p_idx >= 0:
                tok_start = l_start + p_idx
                search_pos = p_idx + len(p)
            else:
                tok_start = l_start

            tok_end = tok_start + len(p)

            m = re.match(r'^(.*?)([\s]*[¹²³⁴⁵⁶⁷⁸⁹⁰0-9*†‡§, \-]+)$', p_strip)
            if m:
                name = m.group(1).strip()
                marker = m.group(2).strip()
            else:
                name = p_strip
                marker = ''

            name_words = {w.lower().strip('.,;:()') for w in name.split()}
            if any(w in _DISALLOWED_NAME_WORDS for w in name_words):
                continue

            clusters = re.findall(r'[¹²³⁴⁵⁶⁷⁸⁹⁰]+|[0-9]+', marker)
            sup_nums = []
            reg_nums = []
            for c in clusters:
                if all(ch in '0123456789' for ch in c):
                    reg_nums.append(int(c))
                else:
                    n_str = ''.join(SUPERSCRIPT_CHAR_MAP.get(ch, '') for ch in c)
                    if n_str:
                        sup_nums.append(int(n_str))

            all_nums = sup_nums + reg_nums
            is_sup = len(sup_nums) > 0 and len(reg_nums) == 0
            is_corr = '*' in marker

            authors.append({
                'name': name,
                'raw_token': p_strip,
                'marker': marker,
                'sup_nums': sup_nums,
                'reg_nums': reg_nums,
                'all_nums': all_nums,
                'is_sup': is_sup,
                'is_corr': is_corr,
                'start_offset': tok_start,
                'end_offset': tok_end,
            })

    return authors


def _parse_affiliations_with_markers(text):
    """Parse affiliation lines and identify leading numbers and superscript status.

    Returns a list of dicts:
      {
        'num': int or None,
        'is_sup': bool,
        'text': str,
        'marker': str,
        'raw_line': str,
        'start_offset': int,
        'end_offset': int
      }
    """
    if not text:
        return []
    section_m = re.search(r'(?im)^\s*(?:Abstract|Introduction|Background|Methods|Results|Discussion|References|Bibliography)\b', text)
    header = text[:section_m.start()] if section_m else text[:4000]
    lines = header.split('\n')
    affils = []
    curr_off = 0

    _NON_AFFIL_HEADINGS = re.compile(
        r'(?i)^\s*(?:funding|trial\s+registration|disclosure|declaration|author\s+contributions?|contributions?|running\s+title|title|corresponding|abstract|introduction|conflict|competing)\b'
    )

    for raw_line in lines:
        line = raw_line.strip()
        line_start = curr_off + (len(raw_line) - len(raw_line.lstrip()))
        line_end = curr_off + len(raw_line)
        curr_off += len(raw_line) + 1

        if not line:
            continue

        if _NON_AFFIL_HEADINGS.match(line):
            if affils:
                break
            continue

        line_lower = line.lower()
        if line_lower.startswith('corresponding') or line_lower.startswith('*corresponding') or '@' in line:
            if affils:
                break
            continue

        if 'contributed equally' in line_lower:
            continue

        has_num_prefix = bool(re.match(r'^[¹²³⁴⁵⁶⁷⁸⁹⁰0-9]+[.\-:\s\t]+', line))
        has_inst_prefix = any(line_lower.startswith(k) for k in [
            'department', 'division', 'institute', 'faculty', 'school',
            'college', 'hospital', 'center', 'centre', 'clinic', 'university', 'laboratory'
        ])

        if not (has_num_prefix or has_inst_prefix):
            continue

        clean_line = re.sub(r'^(?i:affiliations?)\s*[:\-]?\s*', '', line).strip()
        m = re.match(r'^[\s\[\(]*([¹²³⁴⁵⁶⁷⁸⁹⁰]+|[0-9]+)[\s\]\)\.\-:]*(.*)$', clean_line)
        if m:
            raw_num = m.group(1)
            affil_text = m.group(2).strip()
            if all(ch in '0123456789' for ch in raw_num):
                num = int(raw_num)
                is_sup = False
            else:
                n_str = ''.join(SUPERSCRIPT_CHAR_MAP.get(ch, '') for ch in raw_num)
                num = int(n_str) if n_str else None
                is_sup = True
            marker = raw_num
        else:
            num = None
            is_sup = False
            affil_text = clean_line
            marker = ''

        affils.append({
            'num': num,
            'is_sup': is_sup,
            'text': affil_text,
            'marker': marker,
            'raw_line': raw_line,
            'start_offset': line_start,
            'end_offset': line_end,
        })
    return affils


def _extract_corresponding_author_name(text):
    """Extract the corresponding author's personal name from the manuscript."""
    if not text:
        return None
    corr_patterns = [
        r'(?im)^\s*\*?\s*correspond(?:ing|ence)\s*(?:author)?\s*[:\-]?\s*\n*\s*([A-Z][a-zA-Z\s\.\-]+?)(?=[,\n\(]|\s+at\b|[a-zA-Z0-9._%+\-]+@|$)',
        r'(?i)\*?\s*correspondence\s+to\s*[:\-]?\s*\n*\s*([A-Z][a-zA-Z\s\.\-]+?)(?=[,\n\(]|\s+at\b|[a-zA-Z0-9._%+\-]+@|$)',
        r'(?i)address\s+correspondence\s+to\s*[:\-]?\s*\n*\s*([A-Z][a-zA-Z\s\.\-]+?)(?=[,\n\(]|\s+at\b|[a-zA-Z0-9._%+\-]+@|$)',
    ]
    for pat in corr_patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip()
            val = re.sub(r'^(?i:dr\.?|prof\.?)\s*', '', val).strip()
            if val and len(val) > 2 and any(c.isalpha() for c in val):
                return val

    # Fallback to author marked with asterisk (*)
    authors = _parse_authors_with_markers(text)
    for a in authors:
        if a.get('is_corr'):
            return a['name']

    return None


# ─────────────────────────────────────────────
# RULE 1: AUTHOR NAME
# ─────────────────────────────────────────────

RULE_ID_AUTHOR = 'AUTHOR_NAME_CHANGED'

def check_author_names(original_text, edited_text):
    """Compare author names between original and edited manuscript.

    Detects: missing, added, removed, reordered, or spelling-changed author names.
    """
    errors = []
    if not original_text or not edited_text:
        return errors

    orig_authors = _extract_authors_block(original_text)
    edit_authors = _extract_authors_block(edited_text)

    if not orig_authors and not edit_authors:
        return errors

    # Normalize for comparison
    def normalize(name):
        return re.sub(r'\s+', ' ', name.strip().lower())

    orig_norm = [normalize(a) for a in orig_authors]
    edit_norm = [normalize(a) for a in edit_authors]

    # Check for missing authors (in original but not in edited)
    for i, (on, oa) in enumerate(zip(orig_norm, orig_authors)):
        if on not in edit_norm:
            # Check if it's a spelling change (close match)
            close = difflib.get_close_matches(on, edit_norm, n=1, cutoff=0.6)
            if close:
                # Spelling change
                idx = edit_norm.index(close[0])
                errors.append(_make_critical_error(
                    error_code=RULE_ID_AUTHOR,
                    original_value=oa,
                    current_value=edit_authors[idx],
                    page_or_line=1,
                    column_or_field='Author Name',
                    rule_id=RULE_ID_AUTHOR,
                    suggested_action=f'Restore original author name: "{oa}"',
                    description=f'Author name spelling changed: "{oa}" → "{edit_authors[idx]}"',
                    full_text=edited_text,
                ))
            else:
                # Author removed
                errors.append(_make_critical_error(
                    error_code=RULE_ID_AUTHOR,
                    original_value=oa,
                    current_value='[REMOVED]',
                    page_or_line=1,
                    column_or_field='Author Name',
                    rule_id=RULE_ID_AUTHOR,
                    suggested_action=f'Restore removed author: "{oa}"',
                    description=f'Author removed from manuscript: "{oa}"',
                    full_text=edited_text,
                ))

    # Check for added authors (in edited but not in original)
    for i, (en, ea) in enumerate(zip(edit_norm, edit_authors)):
        if en not in orig_norm:
            close = difflib.get_close_matches(en, orig_norm, n=1, cutoff=0.6)
            if not close:
                # Find offset in edited text
                offset = edited_text.find(ea)
                errors.append(_make_critical_error(
                    error_code=RULE_ID_AUTHOR,
                    original_value='[NOT IN ORIGINAL]',
                    current_value=ea,
                    page_or_line=1,
                    column_or_field='Author Name',
                    rule_id=RULE_ID_AUTHOR,
                    suggested_action=f'Remove added author or get approval: "{ea}"',
                    description=f'Author added that is not in the approved original: "{ea}"',
                    start_offset=offset if offset >= 0 else 0,
                    end_offset=(offset + len(ea)) if offset >= 0 else 0,
                    full_text=edited_text,
                ))

    # Check for reordered authors
    if not errors:
        # Only check order if no additions/removals/spelling changes
        if len(orig_authors) == len(edit_authors):
            for i, (oa, ea) in enumerate(zip(orig_authors, edit_authors)):
                if normalize(oa) != normalize(ea):
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_AUTHOR,
                        original_value=', '.join(orig_authors),
                        current_value=', '.join(edit_authors),
                        page_or_line=1,
                        column_or_field='Author Name',
                        rule_id=RULE_ID_AUTHOR,
                        suggested_action='Restore original author order',
                        description=f'Author order changed. Position {i+1}: expected "{oa}", found "{ea}"',
                        full_text=edited_text,
                    ))
                    break  # One reorder error is enough

    return errors


# ─────────────────────────────────────────────
# RULE 2: AFFILIATION
# ─────────────────────────────────────────────

RULE_ID_AFFILIATION = 'AFFILIATION_CHANGED'
RULE_ID_AFFILIATION_SUPERSCRIPT = 'AFFILIATION_SUPERSCRIPT_INVALID'

def _check_affiliation_text_diffs(original_text, edited_text):
    """Compare affiliation text content between original and edited manuscript."""
    errors = []
    if not original_text or not edited_text:
        return errors

    orig_affils = _extract_affiliations_block(original_text)
    edit_affils = _extract_affiliations_block(edited_text)

    if not orig_affils and not edit_affils:
        return errors

    def normalize(t):
        return re.sub(r'\s+', ' ', t.strip().lower())

    matched_orig = set()
    matched_edit = set()

    for oi, oa in enumerate(orig_affils):
        for ei, ea in enumerate(edit_affils):
            if ei in matched_edit:
                continue
            if oa['marker'] and ea['marker'] and oa['marker'] == ea['marker']:
                if normalize(oa['text']) != normalize(ea['text']):
                    offset = edited_text.find(ea['raw'])
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_AFFILIATION,
                        original_value=oa['text'],
                        current_value=ea['text'],
                        page_or_line=1,
                        column_or_field='Affiliation(s)',
                        rule_id=RULE_ID_AFFILIATION,
                        suggested_action=f'Restore original affiliation for marker {oa["marker"]}',
                        description=f'Affiliation changed (marker {oa["marker"]}): "{oa["text"]}" → "{ea["text"]}"',
                        start_offset=offset if offset >= 0 else 0,
                        end_offset=(offset + len(ea['raw'])) if offset >= 0 else 0,
                        full_text=edited_text,
                    ))
                matched_orig.add(oi)
                matched_edit.add(ei)
                break

    for oi, oa in enumerate(orig_affils):
        if oi in matched_orig:
            continue
        if oi < len(edit_affils) and oi not in matched_edit:
            ea = edit_affils[oi]
            if normalize(oa['text']) != normalize(ea['text']):
                offset = edited_text.find(ea['raw'])
                errors.append(_make_critical_error(
                    error_code=RULE_ID_AFFILIATION,
                    original_value=oa['text'],
                    current_value=ea['text'],
                    page_or_line=1,
                    column_or_field='Affiliation(s)',
                    rule_id=RULE_ID_AFFILIATION,
                    suggested_action='Restore original affiliation',
                    description=f'Affiliation changed: "{oa["text"]}" → "{ea["text"]}"',
                    start_offset=offset if offset >= 0 else 0,
                    end_offset=(offset + len(ea['raw'])) if offset >= 0 else 0,
                    full_text=edited_text,
                ))
            matched_orig.add(oi)
            matched_edit.add(oi)

    for oi, oa in enumerate(orig_affils):
        if oi not in matched_orig:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION,
                original_value=oa['text'],
                current_value='[MISSING]',
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION,
                suggested_action=f'Restore missing affiliation: "{oa["text"]}"',
                description=f'Affiliation removed from manuscript: "{oa["text"]}"',
                full_text=edited_text,
            ))

    return errors


def check_affiliation_superscripts(original_text, edited_text):
    """Validate affiliation superscripts and author-to-affiliation mapping.

    Rules:
      - Each author must have one or more affiliation numbers as superscripts.
      - Each affiliation block must begin with a matching superscript number.
      - Validate the author-to-affiliation mapping.

    Raises Critical error when:
      - an author has no affiliation superscript number;
      - an affiliation number is not superscript;
      - an author number has no matching affiliation;
      - an affiliation number has no matching author;
      - duplicate or invalid affiliation numbers exist;
      - the author-to-affiliation mapping changed from the approved original.

    Error code: AFFILIATION_SUPERSCRIPT_INVALID
    Severity: Critical
    Suggested action: Restore the correct superscript affiliation number and mapping.
    """
    errors = []
    if not edited_text:
        return errors

    edit_authors = _parse_authors_with_markers(edited_text)
    edit_affils = _parse_affiliations_with_markers(edited_text)

    if not edit_authors and not edit_affils:
        return errors

    SUGGESTED_ACTION = "Restore the correct superscript affiliation number and mapping."

    # 1. An author has no affiliation superscript number
    for author in edit_authors:
        if not author['all_nums']:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"{author['name']} [superscript affiliation required]",
                current_value=author['name'],
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Author "{author["name"]}" has no affiliation superscript number.',
                start_offset=author['start_offset'],
                end_offset=author['end_offset'],
                full_text=edited_text,
            ))

    # 2. An affiliation number is not superscript
    # Check authors
    for author in edit_authors:
        if author['reg_nums']:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"{author['name']}{to_superscript_str(author['reg_nums'][0])}",
                current_value=author['raw_token'],
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Affiliation number for author "{author["name"]}" is not superscript (found regular number "{author["marker"]}").',
                start_offset=author['start_offset'],
                end_offset=author['end_offset'],
                full_text=edited_text,
            ))

    # Check affiliation blocks
    for affil in edit_affils:
        if not affil['is_sup']:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"{to_superscript_str(affil['num'] or 1)} {affil['text']}",
                current_value=affil['raw_line'],
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Affiliation block begins with non-superscript number: "{affil["marker"] or affil["raw_line"][:10]}". Each affiliation block must begin with a matching superscript number.',
                start_offset=affil['start_offset'],
                end_offset=affil['end_offset'],
                full_text=edited_text,
            ))

    # 3. An author number has no matching affiliation
    available_affil_nums = {a['num'] for a in edit_affils if a['num'] is not None}
    for author in edit_authors:
        for n in author['all_nums']:
            if n not in available_affil_nums:
                errors.append(_make_critical_error(
                    error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                    original_value=f"Affiliation block {to_superscript_str(n)}",
                    current_value=f"Author {author['name']} references {to_superscript_str(n)}",
                    page_or_line=1,
                    column_or_field='Affiliation(s)',
                    rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                    suggested_action=SUGGESTED_ACTION,
                    description=f'Author "{author["name"]}" references affiliation number {n}, but no matching affiliation block with superscript {to_superscript_str(n)} exists.',
                    start_offset=author['start_offset'],
                    end_offset=author['end_offset'],
                    full_text=edited_text,
                ))

    # 4. An affiliation number has no matching author
    author_referenced_nums = set()
    for author in edit_authors:
        author_referenced_nums.update(author['all_nums'])

    for affil in edit_affils:
        if affil['num'] is not None and affil['num'] not in author_referenced_nums:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"Matching author for affiliation {to_superscript_str(affil['num'])}",
                current_value=affil['raw_line'],
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Affiliation block begins with superscript {to_superscript_str(affil["num"])}, but has no matching author referencing it.',
                start_offset=affil['start_offset'],
                end_offset=affil['end_offset'],
                full_text=edited_text,
            ))

    # 5. Duplicate or invalid affiliation numbers exist
    from collections import Counter
    block_num_counts = Counter(a['num'] for a in edit_affils if a['num'] is not None)
    for num, count in block_num_counts.items():
        if count > 1:
            dup_blocks = [a for a in edit_affils if a['num'] == num]
            first_dup = dup_blocks[1]
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"Unique affiliation block number {to_superscript_str(num)}",
                current_value=f"Duplicate affiliation block {to_superscript_str(num)} ({count} occurrences)",
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Duplicate affiliation block number detected: multiple blocks begin with superscript {to_superscript_str(num)}.',
                start_offset=first_dup['start_offset'],
                end_offset=first_dup['end_offset'],
                full_text=edited_text,
            ))

    for author in edit_authors:
        if len(author['sup_nums']) != len(set(author['sup_nums'])):
            dup_nums = [n for n, c in Counter(author['sup_nums']).items() if c > 1]
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                original_value=f"Unique affiliation numbers for {author['name']}",
                current_value=author['raw_token'],
                page_or_line=1,
                column_or_field='Affiliation(s)',
                rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                suggested_action=SUGGESTED_ACTION,
                description=f'Duplicate affiliation number on author "{author["name"]}": superscript {to_superscript_str(dup_nums[0])} appears more than once.',
                start_offset=author['start_offset'],
                end_offset=author['end_offset'],
                full_text=edited_text,
            ))

    # 6. Author-to-affiliation mapping changed from the approved original
    if original_text:
        orig_authors = _parse_authors_with_markers(original_text)
        orig_affils = _parse_affiliations_with_markers(original_text)

        orig_affil_map = {a['num']: a['text'].strip().lower() for a in orig_affils if a['num'] is not None}
        edit_affil_map = {a['num']: a['text'].strip().lower() for a in edit_affils if a['num'] is not None}

        orig_author_map = {}
        for a in orig_authors:
            norm_name = re.sub(r'\s+', ' ', a['name'].strip().lower())
            nums = tuple(sorted(set(a['all_nums'])))
            texts = tuple(sorted(set(orig_affil_map.get(n, '') for n in nums if n in orig_affil_map)))
            orig_author_map[norm_name] = (nums, texts)

        for a in edit_authors:
            norm_name = re.sub(r'\s+', ' ', a['name'].strip().lower())
            if norm_name in orig_author_map:
                orig_nums, orig_texts = orig_author_map[norm_name]
                edit_nums = tuple(sorted(set(a['all_nums'])))
                edit_texts = tuple(sorted(set(edit_affil_map.get(n, '') for n in edit_nums if n in edit_affil_map)))

                if orig_nums and edit_nums and (orig_nums != edit_nums or (orig_texts and edit_texts and orig_texts != edit_texts)):
                    orig_disp = ', '.join(to_superscript_str(n) for n in orig_nums)
                    edit_disp = ', '.join(to_superscript_str(n) for n in edit_nums)
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_AFFILIATION_SUPERSCRIPT,
                        original_value=f"{a['name']}{''.join(to_superscript_str(n) for n in orig_nums)}",
                        current_value=a['raw_token'],
                        page_or_line=1,
                        column_or_field='Affiliation(s)',
                        rule_id=RULE_ID_AFFILIATION_SUPERSCRIPT,
                        suggested_action=SUGGESTED_ACTION,
                        description=f'Author-to-affiliation mapping changed from the approved original for author "{a["name"]}" (original was {orig_disp}, current is {edit_disp}).',
                        start_offset=a['start_offset'],
                        end_offset=a['end_offset'],
                        full_text=edited_text,
                    ))

    return errors


def check_affiliations(original_text, edited_text):
    """Run Affiliation(s) rule group checks:
      1. Content comparison against approved original (AFFILIATION_CHANGED)
      2. Strict affiliation superscript and mapping validation (AFFILIATION_SUPERSCRIPT_INVALID)
    """
    errors = []
    # 1. Text difference comparison
    errors.extend(_check_affiliation_text_diffs(original_text, edited_text))
    # 2. Superscript and mapping validation
    errors.extend(check_affiliation_superscripts(original_text, edited_text))
    return errors


# ─────────────────────────────────────────────
# RULE 3: EMAIL ID
# ─────────────────────────────────────────────

RULE_ID_EMAIL_MISSING = 'EMAIL_MISSING'
RULE_ID_EMAIL_INVALID = 'EMAIL_INVALID'
RULE_ID_EMAIL_CHANGED = 'EMAIL_CHANGED'

def check_email(original_text, edited_text):
    """Validate the corresponding-author email.

    Checks:
      1. Email exists in edited manuscript
      2. Email format is valid
      3. Email matches the approved original (when baseline exists)
    """
    errors = []
    if not edited_text:
        return errors

    edit_email = _extract_corresponding_email(edited_text)
    orig_email = _extract_corresponding_email(original_text) if original_text else None

    # 1. Check email exists
    if not edit_email:
        errors.append(_make_critical_error(
            error_code=RULE_ID_EMAIL_MISSING,
            original_value=orig_email or '[Expected]',
            current_value='[MISSING]',
            page_or_line=1,
            column_or_field='Corresponding Author Email',
            rule_id=RULE_ID_EMAIL_MISSING,
            suggested_action='Add corresponding author email address',
            description='Corresponding author email is missing from the manuscript.',
            full_text=edited_text,
        ))
        return errors

    # 2. Validate email format
    email_format_re = re.compile(
        r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    )
    if not email_format_re.match(edit_email):
        offset = edited_text.find(edit_email)
        errors.append(_make_critical_error(
            error_code=RULE_ID_EMAIL_INVALID,
            original_value=orig_email or '[Valid email required]',
            current_value=edit_email,
            page_or_line=1,
            column_or_field='Corresponding Author Email',
            rule_id=RULE_ID_EMAIL_INVALID,
            suggested_action=f'Fix invalid email format: "{edit_email}"',
            description=f'Corresponding author email has invalid format: "{edit_email}"',
            start_offset=offset if offset >= 0 else 0,
            end_offset=(offset + len(edit_email)) if offset >= 0 else 0,
            full_text=edited_text,
        ))

    # 3. Compare against original
    if orig_email and edit_email and orig_email.lower() != edit_email.lower():
        offset = edited_text.find(edit_email)
        errors.append(_make_critical_error(
            error_code=RULE_ID_EMAIL_CHANGED,
            original_value=orig_email,
            current_value=edit_email,
            page_or_line=1,
            column_or_field='Corresponding Author Email',
            rule_id=RULE_ID_EMAIL_CHANGED,
            suggested_action=f'Restore original email: "{orig_email}"',
            description=f'Corresponding author email changed: "{orig_email}" → "{edit_email}"',
            start_offset=offset if offset >= 0 else 0,
            end_offset=(offset + len(edit_email)) if offset >= 0 else 0,
            full_text=edited_text,
        ))

    return errors


RULE_ID_CORRESPONDING_NAME_CHANGED = 'CORRESPONDING_AUTHOR_NAME_CHANGED'
RULE_ID_CORRESPONDING_NAME_MISSING = 'CORRESPONDING_AUTHOR_NAME_MISSING'
RULE_ID_CORRESPONDING_NAME_MISMATCH = 'CORRESPONDING_AUTHOR_NAME_MISMATCH'

def check_corresponding_author_name(original_text, edited_text):
    """Validate Corresponding Author Name.

    Checks:
      1. Corresponding author exists in edited manuscript
      2. Corresponding author matches approved original
      3. Corresponding author marker (*) matches correspondence section name
      4. Corresponding author email format and match with original

    All issues produced have Severity = Critical.
    """
    errors = []
    if not edited_text:
        return errors

    edit_corr_name = _extract_corresponding_author_name(edited_text)
    orig_corr_name = _extract_corresponding_author_name(original_text) if original_text else None

    # Check authors in edited
    edit_authors = _parse_authors_with_markers(edited_text)
    asterisk_authors = [a['name'] for a in edit_authors if a.get('is_corr')]

    # 1. Missing corresponding author
    if not edit_corr_name and not asterisk_authors:
        errors.append(_make_critical_error(
            error_code=RULE_ID_CORRESPONDING_NAME_MISSING,
            original_value=orig_corr_name or '[Designated Corresponding Author]',
            current_value='[MISSING]',
            page_or_line=1,
            column_or_field='Corresponding Author Name',
            rule_id=RULE_ID_CORRESPONDING_NAME_MISSING,
            suggested_action='Designate the corresponding author name with an asterisk and correspondence section.',
            description='Corresponding author designation or name is missing from the manuscript.',
            full_text=edited_text,
        ))
        return errors

    # 2. Compare against original
    active_edit_name = edit_corr_name or (asterisk_authors[0] if asterisk_authors else None)
    if orig_corr_name and active_edit_name:
        def norm(n):
            return re.sub(r'\s+', ' ', n.strip().lower())
        if norm(orig_corr_name) != norm(active_edit_name):
            ratio = difflib.SequenceMatcher(None, norm(orig_corr_name), norm(active_edit_name)).ratio()
            desc = (f'Corresponding author name spelling changed: "{orig_corr_name}" → "{active_edit_name}"'
                    if ratio >= 0.7 else
                    f'Corresponding author name changed: expected "{orig_corr_name}", found "{active_edit_name}"')
            errors.append(_make_critical_error(
                error_code=RULE_ID_CORRESPONDING_NAME_CHANGED,
                original_value=orig_corr_name,
                current_value=active_edit_name,
                page_or_line=1,
                column_or_field='Corresponding Author Name',
                rule_id=RULE_ID_CORRESPONDING_NAME_CHANGED,
                suggested_action=f'Restore original corresponding author name: "{orig_corr_name}"',
                description=desc,
                full_text=edited_text,
            ))

    # 3. Mismatch between asterisk author and named note
    if edit_corr_name and asterisk_authors:
        def norm(n):
            return re.sub(r'\s+', ' ', n.strip().lower())
        if not any(norm(edit_corr_name) == norm(a) for a in asterisk_authors):
            errors.append(_make_critical_error(
                error_code=RULE_ID_CORRESPONDING_NAME_MISMATCH,
                original_value=edit_corr_name,
                current_value=', '.join(asterisk_authors),
                page_or_line=1,
                column_or_field='Corresponding Author Name',
                rule_id=RULE_ID_CORRESPONDING_NAME_MISMATCH,
                suggested_action='Ensure the corresponding author asterisk matches the named corresponding author.',
                description=f'Corresponding author asterisk is on "{", ".join(asterisk_authors)}" but correspondence section names "{edit_corr_name}".',
                full_text=edited_text,
            ))

    # 4. Email check
    email_errs = check_email(original_text, edited_text)
    for ee in email_errs:
        ee['field'] = 'Corresponding Author Name'
        ee['severity'] = 'CRITICAL'
        ee['category'] = 'CRITICAL'
        errors.append(ee)

    return errors


# ─────────────────────────────────────────────
# RULE 4: ARTICLE TITLE
# ─────────────────────────────────────────────

RULE_ID_TITLE = 'ARTICLE_TITLE_CHANGED'

RULE_ID_TITLE_CASE = 'ARTICLE_TITLE_CASE_INVALID'

def _format_initial_word(t):
    """InitialWord (Sentence case): First word capitalized, rest lowercase except acronyms."""
    words = re.split(r'(\s+)', t)
    res = []
    is_start = True
    for w in words:
        if not w.strip():
            res.append(w)
            continue
        clean = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if clean.isupper() and len(clean) > 1:
            res.append(w)
        elif is_start:
            res.append(w[0].upper() + w[1:].lower() if len(w) > 0 else w)
            is_start = False
        else:
            res.append(w.lower())
        if w.rstrip().endswith(':'):
            is_start = True
    return ''.join(res)

def _format_initial_letter(t):
    """InitialLetter (Title case): Each significant word's initial letter capitalized."""
    minor = {'a', 'an', 'the', 'and', 'but', 'or', 'for', 'nor', 'on', 'at', 'to', 'from', 'by', 'in', 'of', 'with'}
    words = re.split(r'(\s+)', t)
    res = []
    is_start = True
    for w in words:
        if not w.strip():
            res.append(w)
            continue
        clean = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if clean.isupper() and len(clean) > 1:
            res.append(w)
        elif is_start or clean.lower() not in minor:
            res.append(w[0].upper() + w[1:] if len(w) > 0 else w)
            is_start = False
        else:
            res.append(w.lower())
        if w.rstrip().endswith(':'):
            is_start = True
    return ''.join(res)

def check_article_title(original_text, edited_text, job_sheet_instructions=None):
    """Compare article title between original and edited manuscript and validate Job Sheet casing.

    Detects: changed, missing, shortened, added, or reordered title text,
    as well as InitialWord / InitialLetter casing violations from the Job Sheet.
    """
    errors = []
    if not original_text or not edited_text:
        return errors

    orig_title = _extract_title(original_text)
    edit_title = _extract_title(edited_text)

    if not orig_title and not edit_title:
        return errors

    if not edit_title and orig_title:
        errors.append(_make_critical_error(
            error_code=RULE_ID_TITLE,
            original_value=orig_title,
            current_value='[MISSING]',
            page_or_line=1,
            column_or_field='Article Title',
            rule_id=RULE_ID_TITLE,
            suggested_action=f'Restore original article title: "{orig_title}"',
            description=f'Article title is missing from the edited manuscript: "{orig_title}"',
            full_text=edited_text,
        ))
        return errors

    if not orig_title and edit_title:
        offset = edited_text.find(edit_title)
        errors.append(_make_critical_error(
            error_code=RULE_ID_TITLE,
            original_value='[NOT IN ORIGINAL]',
            current_value=edit_title,
            page_or_line=1,
            column_or_field='Article Title',
            rule_id=RULE_ID_TITLE,
            suggested_action='Verify article title against approved source',
            description=f'Article title added in edited manuscript: "{edit_title}"',
            start_offset=offset if offset >= 0 else 0,
            end_offset=(offset + len(edit_title)) if offset >= 0 else 0,
            full_text=edited_text,
        ))
        return errors

    def normalize_ws(t):
        return re.sub(r'\s+', ' ', t.strip()) if t else ''

    if normalize_ws(orig_title) != normalize_ws(edit_title):
        # Determine the nature of the change
        if len(edit_title) < len(orig_title) * 0.5:
            change_type = 'shortened'
        elif len(edit_title) > len(orig_title) * 1.5:
            change_type = 'expanded/added text'
        elif orig_title.lower() == edit_title.lower():
            change_type = 'capitalization/casing changed'
        else:
            change_type = 'changed'

        offset = edited_text.find(edit_title)
        end_offset = (offset + len(edit_title)) if offset >= 0 else 0
        if offset < 0:
            pattern = re.escape(edit_title)
            pattern = re.sub(r'\\ ', r'\\s+', pattern)
            m = re.search(pattern, edited_text)
            if m:
                offset = m.start()
                end_offset = m.end()
            else:
                first_chunk = edit_title[:30].strip()
                offset = edited_text.find(first_chunk) if first_chunk else 0
                end_offset = (offset + len(edit_title)) if offset >= 0 else 0

        errors.append(_make_critical_error(
            error_code=RULE_ID_TITLE,
            original_value=orig_title,
            current_value=edit_title,
            page_or_line=1,
            column_or_field='Article Title',
            rule_id=RULE_ID_TITLE,
            suggested_action=f'Restore original article title: "{orig_title}"',
            description=f'Article title {change_type}: "{orig_title}" → "{edit_title}"',
            start_offset=offset if offset >= 0 else 0,
            end_offset=end_offset,
            full_text=edited_text,
        ))

    # 2. Check Job Sheet Title Casing (InitialWord or InitialLetter)
    if edit_title and job_sheet_instructions:
        case_rule = None
        for instr in job_sheet_instructions:
            sec = (getattr(instr, 'section', None) or (instr.get('section') if isinstance(instr, dict) else '') or '').lower()
            val = (getattr(instr, 'instruction', None) or (instr.get('instruction') if isinstance(instr, dict) else '') or '')
            sec_clean = sec.replace(' ', '')
            val_clean = val.lower().replace(' ', '')
            if ('title' in sec_clean and ('upper' in sec_clean or 'case' in sec_clean)) or 'titleupper' in sec_clean:
                if 'initialword' in val_clean:
                    case_rule = 'InitialWord'
                    break
                elif 'initialletter' in val_clean:
                    case_rule = 'InitialLetter'
                    break
            elif 'titleupper' in val_clean or 'initialword' in val_clean:
                case_rule = 'InitialWord'
                break
            elif 'initialletter' in val_clean:
                case_rule = 'InitialLetter'
                break

        if case_rule:
            expected_cased = _format_initial_word(edit_title) if case_rule == 'InitialWord' else _format_initial_letter(edit_title)
            if edit_title != expected_cased:
                offset = edited_text.find(edit_title)
                end_offset = (offset + len(edit_title)) if offset >= 0 else 0
                errors.append(_make_critical_error(
                    error_code=RULE_ID_TITLE_CASE,
                    original_value=expected_cased,
                    current_value=edit_title,
                    page_or_line=1,
                    column_or_field='Article Title',
                    rule_id=RULE_ID_TITLE_CASE,
                    suggested_action=f'Apply Job Sheet TitleUpperCase ({case_rule}): "{expected_cased}"',
                    description=f'Article title does not match Job Sheet {case_rule} casing: expected "{expected_cased}", found "{edit_title}"',
                    start_offset=offset if offset >= 0 else 0,
                    end_offset=end_offset,
                    full_text=edited_text,
                ))

    return errors


# ─────────────────────────────────────────────
# RULE 5: NUMBERED REFERENCES
# ─────────────────────────────────────────────

RULE_ID_REF_DUPLICATE = 'REFERENCE_NUMBER_DUPLICATE'
RULE_ID_REF_GAP = 'REFERENCE_NUMBER_GAP'
RULE_ID_REF_ORDER = 'REFERENCE_NUMBER_OUT_OF_ORDER'
RULE_ID_REF_CITATION_ORDER = 'REFERENCE_CITATION_ORDER_INVALID'

def check_numbered_references(edited_text, original_text=None):
    """Validate reference numbering is sequential with no duplicates or gaps.

    For numbered references, the reference list MUST be in citation order:
    references are numbered in the order they are first cited in the body text.
    Numbers must be consecutive (1, 2, 3, …) with no gaps or duplicates.
    """
    errors = []
    if not edited_text:
        return errors

    ref_start, ref_section = _find_reference_section(edited_text)
    if ref_section is None:
        return errors

    entries = _extract_reference_entries(ref_section)
    if not entries:
        return errors

    # Map original reference entries if available to provide the exact missing line
    orig_entries_map = {}
    if original_text:
        orig_ref_start, orig_ref_section = _find_reference_section(original_text)
        if orig_ref_section:
            for oe in _extract_reference_entries(orig_ref_section):
                orig_entries_map[oe['number']] = oe

    numbers = [e['number'] for e in entries]

    # Check for duplicates
    seen = {}
    for e in entries:
        n = e['number']
        if n in seen:
            offset = ref_start + e['offset']
            errors.append(_make_critical_error(
                error_code=RULE_ID_REF_DUPLICATE,
                original_value=f'Unique reference number {n}',
                current_value=f'Duplicate reference number {n}',
                page_or_line=None,
                column_or_field='References',
                rule_id=RULE_ID_REF_DUPLICATE,
                suggested_action=f'Remove or renumber duplicate reference [{n}]',
                description=f'Duplicate reference number: [{n}] appears more than once.',
                start_offset=offset,
                end_offset=offset + len(e['full_match']),
                full_text=edited_text,
            ))
        seen[n] = e

    # Check for sequential order and gaps
    if numbers:
        unique_sorted = sorted(set(numbers))

        # Check that numbers start at 1
        if unique_sorted[0] != 1:
            first_entry = entries[0]
            offset = ref_start + first_entry['offset']
            found_orig_start = False
            for m_num in range(1, unique_sorted[0]):
                if m_num in orig_entries_map:
                    found_orig_start = True
                    oe = orig_entries_map[m_num]
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_REF_GAP,
                        original_value=oe['full_match'],
                        current_value=f'[MISSING: Reference {m_num}]',
                        page_or_line=None,
                        column_or_field='References',
                        rule_id=RULE_ID_REF_GAP,
                        suggested_action=f'Restore missing reference [{m_num}] from approved original: "{oe["text"][:80]}"',
                        description=f'Reference [{m_num}] line is missing from the reference list: "{oe["text"][:120]}"',
                        start_offset=offset,
                        end_offset=offset + len(first_entry['full_match']),
                        full_text=edited_text,
                    ))
            if not found_orig_start:
                errors.append(_make_critical_error(
                    error_code=RULE_ID_REF_GAP,
                    original_value='1',
                    current_value=str(unique_sorted[0]),
                    page_or_line=None,
                    column_or_field='References',
                    rule_id=RULE_ID_REF_GAP,
                    suggested_action='Reference numbering should start at 1',
                    description=f'Reference numbering starts at [{unique_sorted[0]}] instead of [1].',
                    start_offset=offset,
                    end_offset=offset + len(first_entry['full_match']),
                    full_text=edited_text,
                ))

        # Check for gaps between numbers
        for i in range(1, len(unique_sorted)):
            if unique_sorted[i] != unique_sorted[i-1] + 1:
                gap_start = unique_sorted[i-1]
                gap_end = unique_sorted[i]
                gap_entry = next((e for e in entries if e['number'] == gap_end), entries[0])
                offset = ref_start + gap_entry['offset']
                found_orig_gap = False
                for m_num in range(gap_start + 1, gap_end):
                    if m_num in orig_entries_map:
                        found_orig_gap = True
                        oe = orig_entries_map[m_num]
                        errors.append(_make_critical_error(
                            error_code=RULE_ID_REF_GAP,
                            original_value=oe['full_match'],
                            current_value=f'[MISSING: Reference {m_num}]',
                            page_or_line=None,
                            column_or_field='References',
                            rule_id=RULE_ID_REF_GAP,
                            suggested_action=f'Restore missing reference [{m_num}] from approved original: "{oe["text"][:80]}"',
                            description=f'Reference [{m_num}] line is missing from the reference list: "{oe["text"][:120]}"',
                            start_offset=offset,
                            end_offset=offset + len(gap_entry['full_match']),
                            full_text=edited_text,
                        ))
                if not found_orig_gap:
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_REF_GAP,
                        original_value=f'Sequential: {gap_start}, {gap_start+1}, ..., {gap_end}',
                        current_value=f'Gap: {gap_start} → {gap_end}',
                        page_or_line=None,
                        column_or_field='References',
                        rule_id=RULE_ID_REF_GAP,
                        suggested_action=f'Fill gap in reference numbering between [{gap_start}] and [{gap_end}]',
                        description=f'Gap in reference numbering: [{gap_start}] → [{gap_end}] (missing {gap_end - gap_start - 1} reference(s)).',
                        start_offset=offset,
                        end_offset=offset + len(gap_entry['full_match']),
                        full_text=edited_text,
                    ))

        # Check for out-of-order (numbers not in ascending order as they appear)
        for i in range(1, len(numbers)):
            if numbers[i] < numbers[i-1] and numbers[i] not in [e['number'] for e in entries[:i]]:
                entry = entries[i]
                offset = ref_start + entry['offset']
                errors.append(_make_critical_error(
                    error_code=RULE_ID_REF_ORDER,
                    original_value=f'Reference [{numbers[i]}] after [{numbers[i-1]}]',
                    current_value=f'[{numbers[i]}] appears after [{numbers[i-1]}]',
                    page_or_line=None,
                    column_or_field='References',
                    rule_id=RULE_ID_REF_ORDER,
                    suggested_action=f'Reorder reference [{numbers[i]}] to correct sequential position',
                    description=f'Reference out of order: [{numbers[i]}] appears after [{numbers[i-1]}].',
                    start_offset=offset,
                    end_offset=offset + len(entry['full_match']),
                    full_text=edited_text,
                ))

    # ── Citation-order validation ──
    # For numbered references, the reference list must follow the order
    # in which citations first appear in the body text.
    body_text = _extract_body_text(edited_text)
    citation_order = _extract_citation_order_from_body(body_text)
    if citation_order and entries:
        # Build a map: reference number → position in reference list (first occurrence)
        ref_list_order = {}
        for idx, e in enumerate(entries):
            if e['number'] not in ref_list_order:
                ref_list_order[e['number']] = idx
        # Walk citations in their body-text order and check that the
        # corresponding reference list entry appears in the same sequence
        expected_list_position = 0
        for cite_num in citation_order:
            if cite_num not in ref_list_order:
                continue  # Missing ref already flagged above
            list_pos = ref_list_order[cite_num]
            if list_pos < expected_list_position:
                # This reference appears in the list BEFORE a reference that
                # was cited earlier in the text — citation-order violation
                entry = entries[list_pos]
                offset = ref_start + entry['offset']
                # Find which earlier citation caused the conflict
                errors.append(_make_critical_error(
                    error_code=RULE_ID_REF_CITATION_ORDER,
                    original_value=f'Reference [{cite_num}] should appear later in list (citation order)',
                    current_value=f'Reference [{cite_num}] at list position {list_pos + 1}',
                    page_or_line=None,
                    column_or_field='References',
                    rule_id=RULE_ID_REF_CITATION_ORDER,
                    suggested_action=(
                        f'Reorder the reference list so that [{cite_num}] appears in the order '
                        f'it is first cited in the text.'
                    ),
                    description=(
                        f'Citation-order violation: Reference [{cite_num}] is cited in the text '
                        f'after a reference that appears later in the reference list. '
                        f'For numbered references, the list must follow first-citation order.'
                    ),
                    start_offset=offset,
                    end_offset=offset + len(entry['full_match']),
                    full_text=edited_text,
                ))
            else:
                expected_list_position = list_pos

    return errors


# ─────────────────────────────────────────────
# RULE 6: AUTHOR–YEAR REFERENCE ORDER
# ─────────────────────────────────────────────

RULE_ID_ALPHA_REF = 'REFERENCE_ALPHABETICAL_ORDER_INVALID'

def check_author_year_reference_order(edited_text):
    """Validate author-year reference order.

    Per requirement: In references, author name and publication year are NOT checked.
    Reference validation checks sequence alone. Returns empty list.
    """
    return []


# ─────────────────────────────────────────────
# RULE 7: UNNUMBERED REFERENCES — no spurious
# numbering in reference list or section headings
# ─────────────────────────────────────────────

RULE_ID_UNNUMBERED_REF_HAS_NUMBER = 'UNNUMBERED_REFERENCE_HAS_NUMBER'
RULE_ID_HEADING_HAS_NUMBER = 'SECTION_HEADING_HAS_NUMBER'

def check_unnumbered_references(edited_text):
    """Validate that an unnumbered reference list does not contain numbers.

    When the job sheet specifies "unnumbered" citations:
      1. The reference list must NOT have leading numbers ("1.", "[2]", etc.)
      2. Section headings must NOT have leading numbers ("1. Introduction", etc.)
    """
    errors = []
    if not edited_text:
        return errors

    # ── Check reference list for spurious numbering ──
    ref_start, ref_section = _find_reference_section(edited_text)
    if ref_section is not None:
        # Look for numbered entries in the reference section
        num_pat = re.compile(r'^[ \t]*\[?(\d+)\]?[.\):]\s+', re.MULTILINE)
        for m in num_pat.finditer(ref_section):
            ref_num = m.group(1)
            offset = ref_start + m.start()
            errors.append(_make_critical_error(
                error_code=RULE_ID_UNNUMBERED_REF_HAS_NUMBER,
                original_value='[Unnumbered reference expected]',
                current_value=f'Reference has number: "{ref_num}"',
                page_or_line=None,
                column_or_field='References',
                rule_id=RULE_ID_UNNUMBERED_REF_HAS_NUMBER,
                suggested_action=(
                    f'Remove the number "{ref_num}." from this reference entry. '
                    f'The job sheet specifies unnumbered references.'
                ),
                description=(
                    f'Unnumbered reference style required, but reference entry '
                    f'starts with number "{ref_num}". Remove all leading numbers '
                    f'from the reference list.'
                ),
                start_offset=offset,
                end_offset=offset + len(m.group(0)),
                full_text=edited_text,
            ))

    # ── Check section headings for spurious numbering ──
    body_text = _extract_body_text(edited_text)
    headings = _find_section_headings(body_text)
    # Pattern: "1." or "1 " or "1.1" at the start of a heading
    heading_num_pat = re.compile(r'^(\d+(?:\.\d+)*)\.?\s+')
    for heading in headings:
        hm = heading_num_pat.match(heading['text'])
        if hm:
            num_str = hm.group(1)
            errors.append(_make_critical_error(
                error_code=RULE_ID_HEADING_HAS_NUMBER,
                original_value=heading['text'][len(hm.group(0)):].strip(),
                current_value=heading['text'],
                page_or_line=None,
                column_or_field='Section Heading',
                rule_id=RULE_ID_HEADING_HAS_NUMBER,
                suggested_action=(
                    f'Remove the number "{num_str}" from the section heading. '
                    f'The job sheet specifies unnumbered format.'
                ),
                description=(
                    f'Unnumbered format required, but section heading starts with '
                    f'number "{num_str}": "{heading["text"]}". Remove the leading '
                    f'number to comply with the job sheet.'
                ),
                start_offset=heading['offset'],
                end_offset=heading['end_offset'],
                full_text=edited_text,
            ))

    return errors


def check_references(edited_text, job_sheet_instructions=None, original_text=None):
    """Validate References rule group: sequence alone.

    Per requirement: In references, author name and year are NOT checked.
    Evaluates sequence alone:
      - Sequential reference numbering (1, 2, 3...)
      - No gaps in numbering
      - No duplicate numbers
      - References in ascending sequential order
      - First-citation sequence in body text
    All issues produced have Severity = Critical.
    """
    errors = []
    if not edited_text:
        return errors

    # Check reference numbering sequence and citation order alone
    errors.extend(check_numbered_references(edited_text, original_text=original_text))

    for e in errors:
        e['field'] = 'References'
        e['severity'] = 'CRITICAL'
        e['category'] = 'CRITICAL'

    return errors


# ─────────────────────────────────────────────
# CRITICAL RULES REGISTRY — THE FIVE RULE GROUPS
# ─────────────────────────────────────────────

# Strictly configured for the allowed five rule groups:
# 1. Article Title
# 2. Author Name(s)
# 3. Affiliation(s)
# 4. Corresponding Author Name
# 5. References

CRITICAL_RULES_REGISTRY = {
    'article_title': {
        'id': RULE_ID_TITLE,
        'name': 'Article Title',
        'severity': 'Critical',
        'description': 'Compare with original',
        'needs_original': True,
        'keywords': ['article title', 'title', 'manuscript title'],
        'func': check_article_title,
    },
    'author_name': {
        'id': RULE_ID_AUTHOR,
        'name': 'Author Name(s)',
        'severity': 'Critical',
        'description': 'Compare with original',
        'needs_original': True,
        'keywords': ['author name', 'author names', 'authors'],
        'func': check_author_names,
    },
    'affiliation': {
        'id': RULE_ID_AFFILIATION,
        'name': 'Affiliation(s)',
        'severity': 'Critical',
        'description': 'Compare with original + superscript and mapping validation',
        'needs_original': True,
        'keywords': ['affiliation', 'affiliations', 'institution', 'superscript'],
        'func': check_affiliations,
    },
    'corresponding_author_name': {
        'id': RULE_ID_CORRESPONDING_NAME_CHANGED,
        'name': 'Corresponding Author Name',
        'severity': 'Critical',
        'description': 'Required + compare with original',
        'needs_original': True,
        'keywords': ['corresponding author name', 'corresponding author', 'correspondence', 'email'],
        'func': check_corresponding_author_name,
    },
    'references': {
        'id': 'REFERENCES_CHECK',
        'name': 'References',
        'severity': 'Critical',
        'description': 'Sequential numbering and citation order sequence alone (name and year check disabled)',
        'needs_original': False,
        'keywords': ['reference', 'references', 'numbered references', 'citation'],
        'func': check_references,
    },
    # Aliases for backward compatibility
    'email': {
        'id': 'EMAIL_CHECK',
        'name': 'Corresponding Author Email',
        'severity': 'Critical',
        'description': 'Required + compare with original',
        'needs_original': True,
        'keywords': ['email', 'e-mail'],
        'func': check_email,
    },
    'numbered_references': {
        'id': 'NUMBERED_REFERENCES_CHECK',
        'name': 'Numbered References (Citation Order)',
        'severity': 'Critical',
        'description': 'Sequential numbering in first-citation order',
        'needs_original': False,
        'keywords': ['numbered references', 'reference numbering'],
        'func': check_numbered_references,
    },
    'author_year_references': {
        'id': RULE_ID_ALPHA_REF,
        'name': 'Author–Year Reference Order',
        'severity': 'Critical',
        'description': 'Disabled (references validate sequence alone)',
        'needs_original': False,
        'keywords': ['alphabetical order', 'author-year'],
        'func': check_author_year_reference_order,
    },
    'unnumbered_references': {
        'id': 'UNNUMBERED_REFERENCES_CHECK',
        'name': 'Unnumbered References',
        'severity': 'Critical',
        'description': 'No numbers in reference list or section headings',
        'needs_original': False,
        'keywords': ['unnumbered reference'],
        'func': check_unnumbered_references,
    },
}


def _match_job_sheet_to_critical_rule(instruction_section, instruction_text):
    """Check if a job sheet instruction activates a critical rule.

    Returns a list of rule keys.
    """
    import re
    section_lower = instruction_section.lower().strip()
    text_lower = instruction_text.lower().strip()
    combined = f'{section_lower} {text_lower}'
    matched_keys = []

    has_author_year = (('author' in combined and 'year' in combined) or 
                       ('name' in combined and 'year' in combined) or
                       'alphabetical' in combined or 'author-year' in combined or 
                       'author–year' in combined)
    
    is_citation_instruction = ('citation' in combined or 'reference' in combined or has_author_year)
    if is_citation_instruction:
        matched_keys.append('numbered_references')
        matched_keys.append('references')

    for rule_key, rule_def in CRITICAL_RULES_REGISTRY.items():
        if rule_key in ('numbered_references', 'author_year_references', 'unnumbered_references'):
            continue

        for kw in rule_def['keywords']:
            if kw in section_lower or kw in text_lower or kw in combined:
                if rule_key not in matched_keys:
                    matched_keys.append(rule_key)
                break

    return matched_keys


def normalize_manuscript_text(text):
    """Normalize manuscript text. If text contains XML tags (e.g. JATS/NLM/PMC XML),
    extract structured manuscript sections (Title, Authors with affiliations,
    Affiliations, Corresponding Author, References) and strip XML markup.
    """
    if not text or not isinstance(text, str):
        return text

    xml_markers = ['<article', '<front', '<title-group', '<contrib-group', '<ref-list', '<article-title', '<?xml']
    lower_head = text[:2000].lower()
    if not ('<' in text and '>' in text and any(m in lower_head for m in xml_markers)):
        return text

    try:
        import xml.etree.ElementTree as ET
        clean_xml = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', '', text)
        root = ET.fromstring(clean_xml)

        parts = []

        # 1. Title
        title_elem = None
        for path in ['.//article-title', './/title-group/article-title', './/title']:
            found = root.find(path)
            if found is not None:
                title_elem = found
                break
        if title_elem is not None:
            title_text = ''.join(title_elem.itertext()).strip()
            if title_text:
                parts.append(title_text)
                parts.append('')

        # 2. Authors
        contribs = root.findall('.//contrib')
        authors = []
        for c in contribs:
            c_type = c.attrib.get('contrib-type', 'author')
            if c_type not in ('author', 'editor') and contribs:
                continue
            surname = c.find('.//surname')
            given = c.find('.//given-names')
            name_elem = c.find('.//string-name') or c.find('.//name')

            name_str = ''
            if surname is not None and given is not None:
                g_txt = (given.text or '').strip()
                s_txt = (surname.text or '').strip()
                name_str = f"{g_txt} {s_txt}".strip()
            elif name_elem is not None:
                name_str = ''.join(name_elem.itertext()).strip()

            if not name_str:
                continue

            xrefs = c.findall('.//xref')
            aff_nums = []
            is_corr = c.attrib.get('corresp') == 'yes'
            for x in xrefs:
                r_type = x.attrib.get('ref-type', '')
                txt = ''.join(x.itertext()).strip()
                if r_type == 'aff' or (txt.isdigit() or all(ch in '0123456789,' for ch in txt)):
                    aff_nums.append(txt)
                if r_type == 'corresp' or '*' in txt:
                    is_corr = True

            if aff_nums:
                clean_digits = ','.join(aff_nums)
                sup_digits = ''.join(DIGIT_TO_SUPERSCRIPT.get(ch, ch) for ch in clean_digits)
                name_str += sup_digits
            if is_corr:
                name_str += '*'

            authors.append(name_str)

        if authors:
            parts.append(', '.join(authors))
            parts.append('')

        # 3. Affiliations
        affs = root.findall('.//aff')
        for aff in affs:
            lbl = aff.find('.//label')
            lbl_txt = ''.join(lbl.itertext()).strip() if lbl is not None else ''
            aff_full = ''.join(aff.itertext()).strip()
            if lbl_txt and aff_full.startswith(lbl_txt):
                aff_full = aff_full[len(lbl_txt):].strip()
            if lbl_txt:
                sup_lbl = ''.join(DIGIT_TO_SUPERSCRIPT.get(ch, ch) for ch in lbl_txt)
                parts.append(f"{sup_lbl} {aff_full}")
            else:
                parts.append(aff_full)
        if affs:
            parts.append('')

        # 4. Corresponding info
        corresp = root.find('.//author-notes/corresp')
        if corresp is None:
            corresp = root.find('.//corresp')
        if corresp is not None:
            parts.append(''.join(corresp.itertext()).strip())
            parts.append('')
        else:
            em = root.find('.//email')
            if em is not None and em.text:
                parts.append(f"Corresponding author: {em.text.strip()}")
                parts.append('')

        # 5. Body
        body = root.find('.//body')
        if body is not None:
            body_text = ''.join(body.itertext()).strip()
            if body_text:
                parts.append(body_text)
                parts.append('')

        # 6. References
        refs = root.findall('.//ref-list/ref')
        if not refs:
            refs = root.findall('.//ref')
        if refs:
            parts.append('References')
            for idx, r in enumerate(refs, 1):
                lbl = r.find('.//label')
                lbl_txt = ''.join(lbl.itertext()).strip() if lbl is not None else ''
                cit = r.find('.//mixed-citation')
                if cit is None:
                    cit = r.find('.//element-citation')
                if cit is not None:
                    ref_txt = ''.join(cit.itertext()).strip()
                else:
                    ref_txt = ''.join(r.itertext()).strip()

                if lbl_txt and ref_txt.startswith(lbl_txt):
                    ref_txt = ref_txt[len(lbl_txt):].strip()
                ref_num = lbl_txt.strip('.[]() ') or str(idx)
                parts.append(f"[{ref_num}] {ref_txt}")

        return '\n'.join(parts)
    except Exception:
        clean = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'[ \t]+', ' ', clean).strip()


def run_critical_rules(original_text, edited_text, job_sheet_instructions=None):
    """Execute the strict five mandatory critical rule groups:
      1. Article Title
      2. Author Name(s)
      3. Affiliation(s)
      4. Corresponding Author Name
      5. References

    Validation Policy:
      - Reminder Only instructions must NEVER create an error.
      - Only instructions configured as Validation Rule and enabled in the project job sheet may create QC errors.
      - Support thousands of journals and instructions through indexed queries by journalNumber and active status.

    Returns:
      (errors_list, active_rule_ids, rule_run_log)
    """
    # Normalize XML manuscripts (e.g. JATS/NLM/PMC XML) into clean manuscript text
    original_text = normalize_manuscript_text(original_text)
    edited_text = normalize_manuscript_text(edited_text)

    errors = []
    active_rule_ids = []
    rule_run_log = []

    # Safeguard: strictly filter out any Reminder Only instruction so it can NEVER produce an error
    if job_sheet_instructions:
        job_sheet_instructions = [
            inst for inst in job_sheet_instructions
            if not getattr(inst, 'type', None) or getattr(inst, 'type', None) != 'Reminder Only'
        ]

    # If specific instructions exist, they can inform reference style
    MANDATORY_RULES = [
        ('article_title', RULE_ID_TITLE, 'Article Title', lambda o, e: check_article_title(o, e, job_sheet_instructions), [original_text, edited_text]),
        ('author_name', RULE_ID_AUTHOR, 'Author Name(s)', check_author_names, [original_text, edited_text]),
        ('affiliation', RULE_ID_AFFILIATION, 'Affiliation(s)', check_affiliations, [original_text, edited_text]),
        ('corresponding_author_name', RULE_ID_CORRESPONDING_NAME_CHANGED, 'Corresponding Author Name', check_corresponding_author_name, [original_text, edited_text]),
        ('references', 'REFERENCES_CHECK', 'References', lambda o, e: check_references(e, job_sheet_instructions, o), [original_text, edited_text]),
    ]

    for rule_key, rule_id, rule_name, rule_func, args in MANDATORY_RULES:
        active_rule_ids.append(rule_id)
        log_entry = {
            'rule_key': rule_key,
            'rule_id': rule_id,
            'rule_name': rule_name,
            'triggered_by': f'Mandatory Check: {rule_name}',
            'status': 'RUNNING',
            'errors_found': 0,
        }

        try:
            rule_errors = rule_func(*args)
            # Enforce Severity = Critical on every single issue
            for e in rule_errors:
                e['severity'] = 'CRITICAL'
                e['category'] = 'CRITICAL'
                if not e.get('field'):
                    e['field'] = rule_name
            errors.extend(rule_errors)
            log_entry['status'] = 'COMPLETED'
            log_entry['errors_found'] = len(rule_errors)
        except Exception as e:
            log_entry['status'] = 'ERROR'
            log_entry['error_message'] = str(e)

        rule_run_log.append(log_entry)

    return errors, active_rule_ids, rule_run_log

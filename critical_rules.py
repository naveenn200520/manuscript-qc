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
    line, col = _offset_to_line_col(full_text, start_offset) if full_text else (page_or_line, column_or_field)

    return {
        'error_type': error_code,
        'severity': 'CRITICAL',
        'category': 'CRITICAL',
        'line_number': page_or_line if page_or_line else line,
        'column_number': column_or_field if column_or_field else col,
        'start_offset': start_offset,
        'end_offset': end_offset,
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
    r'^[ \t]*(?:REFERENCES|References|Bibliography|BIBLIOGRAPHY|Literature\s+[Cc]ited)[ \t]*$',
]


def _find_reference_section(text):
    """Return (start_offset, ref_section_text) or (None, None)."""
    best_match = None
    for pat in _REFERENCE_SECTION_HEADERS:
        matches = list(re.finditer(pat, text, re.MULTILINE))
        if matches:
            # Pick the last match to avoid Table of Contents matches
            m = matches[-1]
            if not best_match or m.start() > best_match.start():
                best_match = m
    
    if best_match:
        return best_match.start(), text[best_match.start():]
    return None, None


def _extract_reference_entries(ref_text):
    """Extract individual reference entries from reference section text.

    Handles numbered references like:
      [1] Author ...
      1. Author ...
      1  Author ...
    And author-year references (un-numbered paragraphs).
    """
    entries = []
    # Numbered: [1], 1., 1)  at the start of a line
    num_pat = re.compile(r'^[ \t]*\[?(\d+)\]?[.\):]?\s+(.+)', re.MULTILINE)
    for m in num_pat.finditer(ref_text):
        entries.append({
            'number': int(m.group(1)),
            'text': m.group(2).strip(),
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

    Handles: [1], [1,2], [1-3], (1), superscript-like patterns.
    Returns a list of ints in first-appearance order (deduplicated).
    """
    seen = set()
    order = []
    # Match patterns like [1], [1,2,3], [1-5], (1), etc.
    bracket_pat = re.compile(r'[\[\(](\d+(?:[,;\s]+\d+|\s*[-–]\s*\d+)*)[\]\)]')
    for m in bracket_pat.finditer(body_text):
        inner = m.group(1)
        # Expand ranges like 1-5 → 1,2,3,4,5
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

    for instr in job_sheet_instructions:
        section = (instr.section if hasattr(instr, 'section') else instr.get('section', '')).lower().strip()
        instruction = (instr.instruction if hasattr(instr, 'instruction') else instr.get('instruction', '')).lower().strip()
        status = instr.status if hasattr(instr, 'status') else instr.get('status', '')
        if status == 'Manual Review Required':
            continue

        combined = f'{section} {instruction}'

        # Detect numbered style
        if ('citation' in combined or 'reference' in combined) and 'numbered' in combined:
            return 'numbered'

        # Detect author-year style
        if ('author' in combined and 'year' in combined) or \
           ('alphabetical' in combined and ('reference' in combined or 'citation' in combined)):
            return 'author_year'

        # Detect unnumbered style
        if ('citation' in combined or 'reference' in combined) and 'unnumbered' in combined:
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
    for m in re.finditer(r'^[ \t]*([A-Za-z0-9][^\n]{0,150})[ \t]*$', text, re.MULTILINE):
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
    """Heuristically extract author names from the beginning of the manuscript.

    Returns list of author name strings in order.
    Looks for comma-separated or newline-separated proper-noun sequences
    before the Abstract / Introduction.
    """
    # Find where the abstract/intro starts to bound the search
    abstract_match = re.search(
        r'\b(?:Abstract|ABSTRACT|Introduction|INTRODUCTION)\b', text
    )
    header_section = text[:abstract_match.start()] if abstract_match else text[:3000]

    # Try to find author lines — proper noun sequences separated by commas or "and"
    # Common patterns:
    #   John Smith, Jane Doe, and Bob Lee
    #   John Smith¹, Jane Doe², Bob Lee³
    author_lines = []
    for line in header_section.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Skip title-like lines (all caps or very long)
        if line.isupper() and len(line) > 10:
            continue
        # Skip affiliation lines
        affil_kws = {'university', 'hospital', 'department', 'institute', 'faculty',
                     'school', 'college', 'centre', 'center', 'clinic', 'research',
                     'foundation', 'laboratory', 'medicine', 'science'}
        if any(kw in line.lower() for kw in affil_kws):
            continue
        # Skip email lines
        if '@' in line:
            continue
        # Check if line looks like author names (Title Case words separated by commas)
        # Remove superscripts and special markers
        cleaned = re.sub(r'[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡§\d]', '', line).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        if not cleaned:
            continue
        words = cleaned.split()
        title_words = sum(1 for w in words if w[0:1].isupper() and w.strip('.,;') not in {'and', 'And', 'AND'})
        if title_words >= 2 and ',' in line:
            author_lines.append(line)

    # Parse individual author names from the collected lines
    authors = []
    for al in author_lines:
        # Remove superscript markers
        al = re.sub(r'[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡§]', '', al)
        # Split by comma, then by "and"
        parts = re.split(r',\s*(?:and\s+)?|\s+and\s+', al)
        for p in parts:
            name = p.strip().strip('.,;')
            name = re.sub(r'^\d+\s*', '', name)  # remove leading numbers
            if name and len(name) > 2 and any(c.isalpha() for c in name):
                authors.append(name)

    return authors


def _extract_affiliations_block(text):
    """Extract affiliations from the manuscript header.

    Returns list of dicts with 'text' and 'marker' keys.
    """
    abstract_match = re.search(
        r'\b(?:Abstract|ABSTRACT|Introduction|INTRODUCTION)\b', text
    )
    header_section = text[:abstract_match.start()] if abstract_match else text[:5000]

    affiliations = []
    affil_kws = {'university', 'hospital', 'department', 'institute', 'faculty',
                 'school', 'college', 'centre', 'center', 'clinic', 'research',
                 'foundation', 'laboratory', 'medicine', 'science'}

    for line in header_section.split('\n'):
        line = line.strip()
        if not line:
            continue
        if any(kw in line.lower() for kw in affil_kws):
            # Extract superscript marker if present
            marker_match = re.match(r'^[\s]*([¹²³⁴⁵⁶⁷⁸⁹⁰\d*†‡§]+)\s*', line)
            marker = marker_match.group(1) if marker_match else ''
            affil_text = line[marker_match.end():].strip() if marker_match else line
            affiliations.append({'text': affil_text, 'marker': marker, 'raw': line})

    return affiliations


def _extract_title(text):
    """Extract the article title from the manuscript.

    Assumes the title is the first non-blank line(s) before the author block.
    """
    lines = text.split('\n')
    title_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if title_lines:
                break  # title ends at first blank after content
            continue
        # Stop at author-like content (comma-separated names)
        affil_kws = {'university', 'hospital', 'department', 'institute',
                     'abstract', 'introduction', 'keywords'}
        if any(kw in stripped.lower() for kw in affil_kws):
            break
        if '@' in stripped:
            break
        title_lines.append(stripped)
        # Title is usually 1-3 lines
        if len(title_lines) >= 4:
            break

    return ' '.join(title_lines)


def _extract_corresponding_email(text):
    """Extract the corresponding author's email address."""
    # Look for explicit corresponding author marker
    corr_patterns = [
        r'[Cc]orrespond(?:ing|ence)\s*(?:author)?\s*[:\-]?\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        r'\*\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        r'[Ee]-?mail\s*[:\-]\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    ]
    for pat in corr_patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    # Fallback: first email found in the header area
    abstract_match = re.search(r'\b(?:Abstract|ABSTRACT)\b', text)
    header = text[:abstract_match.start()] if abstract_match else text[:3000]
    m = _EMAIL_PATTERN.search(header)
    return m.group() if m else None


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

def check_affiliations(original_text, edited_text):
    """Compare affiliations between original and edited manuscript.

    Detects: changed institution, department, city, country, superscript mapping,
             or missing affiliation.
    """
    errors = []
    if not original_text or not edited_text:
        return errors

    orig_affils = _extract_affiliations_block(original_text)
    edit_affils = _extract_affiliations_block(edited_text)

    if not orig_affils and not edit_affils:
        return errors

    def normalize(t):
        return re.sub(r'\s+', ' ', t.strip().lower())

    # Match by marker first, then by position
    matched_orig = set()
    matched_edit = set()

    for oi, oa in enumerate(orig_affils):
        for ei, ea in enumerate(edit_affils):
            if ei in matched_edit:
                continue
            # Match by marker
            if oa['marker'] and ea['marker'] and oa['marker'] == ea['marker']:
                if normalize(oa['text']) != normalize(ea['text']):
                    ratio = difflib.SequenceMatcher(
                        None, normalize(oa['text']), normalize(ea['text'])
                    ).ratio()
                    if ratio < 0.95:
                        offset = edited_text.find(ea['raw'])
                        errors.append(_make_critical_error(
                            error_code=RULE_ID_AFFILIATION,
                            original_value=oa['text'],
                            current_value=ea['text'],
                            page_or_line=1,
                            column_or_field='Affiliation',
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

    # Positional matching for unmatched
    for oi, oa in enumerate(orig_affils):
        if oi in matched_orig:
            continue
        if oi < len(edit_affils) and oi not in matched_edit:
            ea = edit_affils[oi]
            if normalize(oa['text']) != normalize(ea['text']):
                ratio = difflib.SequenceMatcher(
                    None, normalize(oa['text']), normalize(ea['text'])
                ).ratio()
                if ratio < 0.95:
                    offset = edited_text.find(ea['raw'])
                    errors.append(_make_critical_error(
                        error_code=RULE_ID_AFFILIATION,
                        original_value=oa['text'],
                        current_value=ea['text'],
                        page_or_line=1,
                        column_or_field='Affiliation',
                        rule_id=RULE_ID_AFFILIATION,
                        suggested_action='Restore original affiliation',
                        description=f'Affiliation changed: "{oa["text"]}" → "{ea["text"]}"',
                        start_offset=offset if offset >= 0 else 0,
                        end_offset=(offset + len(ea['raw'])) if offset >= 0 else 0,
                        full_text=edited_text,
                    ))
            matched_orig.add(oi)
            matched_edit.add(oi)

    # Missing affiliations
    for oi, oa in enumerate(orig_affils):
        if oi not in matched_orig:
            errors.append(_make_critical_error(
                error_code=RULE_ID_AFFILIATION,
                original_value=oa['text'],
                current_value='[MISSING]',
                page_or_line=1,
                column_or_field='Affiliation',
                rule_id=RULE_ID_AFFILIATION,
                suggested_action=f'Restore missing affiliation: "{oa["text"]}"',
                description=f'Affiliation removed from manuscript: "{oa["text"]}"',
                full_text=edited_text,
            ))

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


# ─────────────────────────────────────────────
# RULE 4: ARTICLE TITLE
# ─────────────────────────────────────────────

RULE_ID_TITLE = 'ARTICLE_TITLE_CHANGED'

def check_article_title(original_text, edited_text):
    """Compare article title between original and edited manuscript.

    Detects: changed, missing, shortened, added, or reordered title text.
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
            suggested_action=f'Restore original article title',
            description=f'Article title is missing from the edited manuscript.',
            full_text=edited_text,
        ))
        return errors

    def normalize(t):
        return re.sub(r'\s+', ' ', t.strip().lower())

    if normalize(orig_title) != normalize(edit_title):
        ratio = difflib.SequenceMatcher(
            None, normalize(orig_title), normalize(edit_title)
        ).ratio()

        if ratio < 0.95:
            # Determine the nature of the change
            if len(edit_title) < len(orig_title) * 0.5:
                change_type = 'shortened'
            elif len(edit_title) > len(orig_title) * 1.5:
                change_type = 'expanded/added text'
            else:
                change_type = 'changed'

            offset = edited_text.find(edit_title)
            errors.append(_make_critical_error(
                error_code=RULE_ID_TITLE,
                original_value=orig_title,
                current_value=edit_title,
                page_or_line=1,
                column_or_field='Article Title',
                rule_id=RULE_ID_TITLE,
                suggested_action=f'Restore original article title',
                description=f'Article title {change_type}: "{orig_title}" → "{edit_title}"',
                start_offset=offset if offset >= 0 else 0,
                end_offset=(offset + len(edit_title)) if offset >= 0 else 0,
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

def check_numbered_references(edited_text):
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

        # Check for gaps
        for i in range(1, len(unique_sorted)):
            if unique_sorted[i] != unique_sorted[i-1] + 1:
                gap_start = unique_sorted[i-1]
                gap_end = unique_sorted[i]
                # Find the entry after the gap
                gap_entry = next((e for e in entries if e['number'] == gap_end), entries[0])
                offset = ref_start + gap_entry['offset']
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
        # Build a map: reference number → position in reference list
        ref_list_order = {e['number']: idx for idx, e in enumerate(entries)}
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
    """Validate author-year references are in alphabetical order.

    Sorting rules (per standard academic style):
      1. Primary sort: first author's surname, A → Z
      2. Single-author works first, then two-author, then three-or-more
      3. Within the same author group, sort by publication year (oldest → newest)
    """
    errors = []
    if not edited_text:
        return errors

    ref_start, ref_section = _find_reference_section(edited_text)
    if ref_section is None:
        return errors

    references = _extract_author_year_references_enhanced(ref_section)

    if len(references) < 2:
        return errors

    # Build composite sort key: (surname, author_count, year)
    def sort_key(ref):
        return (ref['surname'], ref['author_count'], ref['year'])

    # Check order
    for i in range(1, len(references)):
        prev = references[i - 1]
        curr = references[i]
        prev_key = sort_key(prev)
        curr_key = sort_key(curr)

        if prev_key > curr_key and prev_key[0] and curr_key[0]:
            offset = ref_start + curr['offset']

            # Build a human-readable explanation
            if prev_key[0] != curr_key[0]:
                detail = (
                    f'"{curr["surname"].title()}" should come before '
                    f'"{prev["surname"].title()}" (alphabetical order)'
                )
            elif prev_key[1] != curr_key[1]:
                count_labels = {1: 'single-author', 2: 'two-author', 3: 'three-or-more-author'}
                detail = (
                    f'Same first author "{curr["surname"].title()}": '
                    f'{count_labels.get(curr_key[1], "multi-author")} works should come before '
                    f'{count_labels.get(prev_key[1], "multi-author")} works'
                )
            else:
                detail = (
                    f'Same author "{curr["surname"].title()}", year {curr["year"]} '
                    f'should come before {prev["year"]}'
                )

            errors.append(_make_critical_error(
                error_code=RULE_ID_ALPHA_REF,
                original_value=f'Expected order: "{prev["surname"].title()}" then "{curr["surname"].title()}"',
                current_value=curr['text'][:120],
                page_or_line=None,
                column_or_field='References',
                rule_id=RULE_ID_ALPHA_REF,
                suggested_action=(
                    'Reorder references alphabetically by first author surname, '
                    'then single-author → two-author → three-or-more, then by year'
                ),
                description=f'Reference order incorrect: {detail}.',
                start_offset=offset,
                end_offset=offset + min(len(curr['text']), 200),
                full_text=edited_text,
            ))

    return errors


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


# ─────────────────────────────────────────────
# CRITICAL RULES REGISTRY
# ─────────────────────────────────────────────

# Maps job-sheet section/instruction keywords to rule functions
CRITICAL_RULES_REGISTRY = {
    'author_name': {
        'id': RULE_ID_AUTHOR,
        'name': 'Author Name',
        'severity': 'Critical',
        'description': 'Compare with original',
        'needs_original': True,
        'keywords': ['author name', 'author names', 'authors'],
        'func': check_author_names,
    },
    'affiliation': {
        'id': RULE_ID_AFFILIATION,
        'name': 'Affiliation',
        'severity': 'Critical',
        'description': 'Compare with original',
        'needs_original': True,
        'keywords': ['affiliation', 'affiliations', 'institution'],
        'func': check_affiliations,
    },
    'email': {
        'id': 'EMAIL_CHECK',
        'name': 'Corresponding Author Email',
        'severity': 'Critical',
        'description': 'Required + compare with original',
        'needs_original': True,
        'keywords': ['email', 'e-mail', 'corresponding author email',
                     'corresponding email', 'email id'],
        'func': check_email,
    },
    'article_title': {
        'id': RULE_ID_TITLE,
        'name': 'Article Title',
        'severity': 'Critical',
        'description': 'Compare with original',
        'needs_original': True,
        'keywords': ['article title', 'title', 'manuscript title'],
        'func': check_article_title,
    },
    'numbered_references': {
        'id': 'NUMBERED_REFERENCES_CHECK',
        'name': 'Numbered References (Citation Order)',
        'severity': 'Critical',
        'description': 'Sequential numbering in first-citation order',
        'needs_original': False,
        'keywords': ['numbered references', 'reference numbering',
                     'sequential numbering', 'numbered citation',
                     'citation order'],
        'func': check_numbered_references,
    },
    'author_year_references': {
        'id': RULE_ID_ALPHA_REF,
        'name': 'Author–Year Reference Order',
        'severity': 'Critical',
        'description': 'Alphabetical by surname → author count → year',
        'needs_original': False,
        'keywords': ['alphabetical order', 'author-year', 'author–year',
                     'author year style', 'alphabetical author',
                     'author year', 'alphabetical reference'],
        'func': check_author_year_reference_order,
    },
    'unnumbered_references': {
        'id': 'UNNUMBERED_REFERENCES_CHECK',
        'name': 'Unnumbered References',
        'severity': 'Critical',
        'description': 'No numbers in reference list or section headings',
        'needs_original': False,
        'keywords': ['unnumbered reference', 'unnumbered citation',
                     'unnumbered'],
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
        has_unnumbered = 'unnumbered' in combined
        has_numbered = bool(re.search(r'(?<!un)numbered', combined))

        if has_author_year:
            matched_keys.append('author_year_references')
            
        if has_numbered:
            matched_keys.append('numbered_references')
            
        if has_unnumbered and not has_numbered:
            matched_keys.append('unnumbered_references')

    # ── Generic keyword matching ──
    for rule_key, rule_def in CRITICAL_RULES_REGISTRY.items():
        if rule_key in ('numbered_references', 'author_year_references', 'unnumbered_references'):
            continue

        for kw in rule_def['keywords']:
            if kw in section_lower or kw in text_lower or kw in combined:
                if rule_key not in matched_keys:
                    matched_keys.append(rule_key)
                break

    return matched_keys


def run_critical_rules(original_text, edited_text, job_sheet_instructions):
    """Execute critical rules that are explicitly enabled in the active job sheet.

    Strict execution policy:
      - Run ONLY rules that are explicitly enabled in the job sheet
      - Do NOT run rules not assigned to this job sheet
      - Do NOT use AI inference or fallback rules
      - Use original manuscript as baseline for comparison rules

    Returns:
      (errors_list, active_rule_ids, rule_run_log)
    """
    errors = []
    active_rule_ids = []
    rule_run_log = []

    if not job_sheet_instructions:
        return errors, active_rule_ids, rule_run_log

    # Determine which critical rules are activated by the job sheet
    activated_rules = {}
    for instr in job_sheet_instructions:
        section = instr.section if hasattr(instr, 'section') else instr.get('section', '')
        instruction = instr.instruction if hasattr(instr, 'instruction') else instr.get('instruction', '')
        status = instr.status if hasattr(instr, 'status') else instr.get('status', '')

        if status == 'Manual Review Required':
            continue

        rule_keys = _match_job_sheet_to_critical_rule(section, instruction)
        for rule_key in rule_keys:
            if rule_key and rule_key not in activated_rules:
                activated_rules[rule_key] = {
                    'instruction_section': section,
                    'instruction_text': instruction,
                }

    # Execute each activated rule
    for rule_key, activation_info in activated_rules.items():
        rule_def = CRITICAL_RULES_REGISTRY[rule_key]
        rule_func = rule_def['func']
        active_rule_ids.append(rule_def['id'])

        log_entry = {
            'rule_key': rule_key,
            'rule_id': rule_def['id'],
            'rule_name': rule_def['name'],
            'triggered_by': f"{activation_info['instruction_section']}: {activation_info['instruction_text']}",
            'status': 'RUNNING',
            'errors_found': 0,
        }

        try:
            if rule_def['needs_original']:
                rule_errors = rule_func(original_text, edited_text)
            else:
                rule_errors = rule_func(edited_text)

            errors.extend(rule_errors)
            log_entry['status'] = 'COMPLETED'
            log_entry['errors_found'] = len(rule_errors)
        except Exception as e:
            log_entry['status'] = 'ERROR'
            log_entry['error_message'] = str(e)

        rule_run_log.append(log_entry)

    return errors, active_rule_ids, rule_run_log

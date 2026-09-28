"""
QC Engine - Content-First Manuscript Quality Control

Compares Original Manuscript vs Edited Manuscript to detect meaningful
content errors. Uses Job Sheet instructions to validate style compliance.

CORE PRINCIPLE: This engine ONLY detects and reports errors.
It NEVER modifies the manuscript text automatically.
"""

import re
import difflib


# ─────────────────────────────────────────────
# PROTECTED TERMS — built dynamically per manuscript
# ─────────────────────────────────────────────

MEDICAL_PATTERNS = [
    r'\b\w+emia\b', r'\b\w+itis\b', r'\b\w+osis\b', r'\b\w+ectomy\b',
    r'\b\w+oscopy\b', r'\b\w+ology\b', r'\b\w+opathy\b', r'\b\w+oma\b',
    r'\b\w+ase\b', r'\b\w+ide\b', r'\b\w+ine\b', r'\b\w+ole\b',
    r'\b\w+azole\b', r'\b\w+mycin\b', r'\b\w+cillin\b', r'\b\w+statin\b',
    r'\b\w+pril\b', r'\b\w+sartan\b', r'\b\w+olol\b', r'\b\w+dipine\b',
    r'\b\w+vir\b', r'\b\w+mab\b', r'\b\w+nib\b', r'\b\w+zumab\b',
]

COMMON_ABBREVIATIONS = {
    'CT', 'MRI', 'ECG', 'EKG', 'ICU', 'IV', 'IM', 'SC', 'PO', 'BID', 'TID',
    'QID', 'PRN', 'STAT', 'BMI', 'BP', 'HR', 'RR', 'SpO2', 'GCS', 'APACHE',
    'SOFA', 'INR', 'PT', 'PTT', 'aPTT', 'CBC', 'WBC', 'RBC', 'Hb', 'Hgb',
    'Hct', 'PLT', 'ESR', 'CRP', 'AST', 'ALT', 'ALP', 'GGT', 'LDH', 'BUN',
    'eGFR', 'GFR', 'HbA1c', 'TSH', 'T3', 'T4', 'FSH', 'LH', 'PSA', 'CEA',
    'AFP', 'CA', 'HIV', 'HBV', 'HCV', 'COVID', 'SARS', 'PCR', 'RT-PCR',
    'ELISA', 'DNA', 'RNA', 'mRNA', 'tRNA', 'rRNA', 'ATP', 'ADP', 'AMP',
    'NAD', 'NADH', 'NADPH', 'pH', 'pCO2', 'pO2', 'SaO2', 'FiO2', 'PEEP',
    'CPAP', 'BiPAP', 'NIV', 'ETT', 'LMA', 'CVP', 'PAP', 'PCWP', 'SVR',
    'PVR', 'CO', 'CI', 'EF', 'LVEF', 'RVEF', 'LA', 'LV', 'RA', 'RV',
    'AV', 'MV', 'TV', 'PV', 'IVC', 'SVC', 'DVT', 'PE', 'DIC', 'TTP',
    'HUS', 'ITP', 'SLE', 'OA', 'AS', 'IBD', 'IBS', 'GERD', 'NAFLD',
    'COPD', 'ARDS', 'CHF', 'ACS', 'STEMI', 'NSTEMI', 'PCI', 'CABG', 'AVR',
    'MVR', 'TAVR', 'CKD', 'AKI', 'UTI', 'CAUTI', 'CLABSI', 'SSI', 'VAP',
    'MRSA', 'VRE', 'ESBL', 'MDR', 'XDR', 'PDR', 'ICH', 'SAH', 'SDH',
    'TBI', 'CVA', 'TIA', 'MS', 'PD', 'AD', 'ALS', 'GBS', 'MG', 'NMJ',
    'EMG', 'NCS', 'EEG', 'CSF', 'LP', 'ICP', 'VP', 'EVD', 'BMD', 'DEXA',
    'vs', 'et', 'al', 'etc', 'eg', 'ie', 'Fig', 'Tab', 'Eq', 'Ref',
    'Vol', 'No', 'pp', 'doi', 'DOI', 'PMID', 'ISSN', 'ISBN',
    'USA', 'UK', 'EU', 'WHO', 'FDA', 'EMA', 'NIH', 'CDC', 'NHS',
}

AFFILIATION_KWS = {'university', 'hospital', 'centre', 'center', 'institute', 'department',
                   'faculty', 'school', 'college', 'clinic', 'medical', 'research',
                   'foundation', 'academy', 'national', 'laboratory'}


def build_protected_terms(original_text, edited_text):
    """Build set of protected terms that should not be flagged as spelling errors."""
    protected = set()
    protected.update(COMMON_ABBREVIATIONS)

    # Do not flag any words that were already present in the original manuscript
    if original_text:
        orig_words = re.findall(r'\b[A-Za-z]{3,}\b', original_text)
        protected.update(orig_words)
        protected.update(w.lower() for w in orig_words)

    for text in [original_text, edited_text]:
        if not text:
            continue
        words = re.findall(r'(?<=[.!?]\s)[A-Z][a-z]+|(?<=,\s)[A-Z][a-z]+|(?<=\s)[A-Z][a-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xef\xee\xf4\xf6\xe7\xf1]+', text)
        protected.update(words)
        caps = re.findall(r'\b[A-Z]{2,}\b', text)
        protected.update(caps)
        accented = re.findall(r'\b[A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xef\xee\xf4\xf6\xe7\xf1\xc9\xc8\xca\xcb\xc0\xc2\xc4\xd9\xdb\xdc\xcf\xce\xd4\xd6\xc7\xd1]+(?:-[A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xef\xee\xf4\xf6\xe7\xf1]+)*\b', text)
        for w in accented:
            if any(ord(c) > 127 for c in w):
                protected.add(w)
        for pat in MEDICAL_PATTERNS:
            matches = re.findall(pat, text, re.IGNORECASE)
            protected.update(matches)
        units = re.findall(r'\b(?:mg|mL|ng|pg|mmol|nmol|mmHg|cmH2O|U/L|IU|mEq|dL|g/dL|mg/dL|ng/mL|pg/mL|mIU|kPa)\b', text)
        protected.update(units)

    return protected


# ─────────────────────────────────────────────
# LOCATION HELPERS
# ─────────────────────────────────────────────

def offset_to_line_col(text, offset):
    """Convert a character offset to (line_number, column_number). 1-indexed."""
    if not text or offset < 0:
        return (1, 1)
    offset = min(offset, len(text))
    before = text[:offset]
    line = before.count('\n') + 1
    last_nl = before.rfind('\n')
    col = offset - last_nl
    return (line, col)


def get_context(text, start, end, ctx_len=60):
    """Get context snippets before and after a range in text."""
    before = text[max(0, start - ctx_len):start]
    after = text[end:end + ctx_len]
    if before and start - ctx_len > 0:
        space_idx = before.find(' ')
        if space_idx > 0:
            before = before[space_idx + 1:]
    if after:
        space_idx = after.rfind(' ')
        if space_idx > 0:
            after = after[:space_idx]
    return (before, after)


def make_error(error_type, severity, start_offset, end_offset, original_text,
               edited_text, description, full_edited_text, category=None, rule_id=None):
    """Create a standardized error dict."""
    line, col = offset_to_line_col(full_edited_text, start_offset)
    ctx_before, ctx_after = get_context(full_edited_text, start_offset, end_offset)
    return {
        'error_type': error_type,
        'severity': severity,
        'category': category or severity,
        'line_number': line,
        'column_number': col,
        'start_offset': start_offset,
        'end_offset': end_offset,
        'original_text': original_text,
        'edited_text': edited_text,
        'context_before': ctx_before,
        'context_after': ctx_after,
        'description': description,
        'rule_id': rule_id,
    }


def tokenize_text(text):
    """Tokenize text into list of dicts with text/start/end."""
    tokens = []
    for m in re.finditer(r'\S+', text):
        tokens.append({'text': m.group(), 'start': m.start(), 'end': m.end()})
    return tokens


def is_meaningful_content(text):
    """Return True if text has meaningful (non-trivial) words."""
    trivial = {
        'the', 'a', 'an', 'of', 'to', 'in', 'on', 'at', 'by', 'for',
        'with', 'from', 'and', 'or', 'but', 'is', 'was', 'were', 'are',
        'been', 'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
        'would', 'could', 'should', 'may', 'might', 'can', 'shall',
        'that', 'which', 'who', 'whom', 'this', 'these', 'those', 'it', 'its',
        'not', 'no', 'nor', 'so', 'yet', 'both', 'either', 'neither',
    }
    words = set(re.findall(r'\w+', text.lower()))
    return len(words - trivial) > 0


# ─────────────────────────────────────────────
# MAIN CONTENT COMPARISON ENGINE
# ─────────────────────────────────────────────

def check_content_changes(original_text, edited_text):
    """
    Compare original vs edited manuscript at token level.

    Uses word-by-word diffing to avoid positional false-positives.
    Classifies each meaningful difference into specific error types.
    Does NOT flag trivial editorial changes.
    """
    errors = []
    if not original_text or not edited_text:
        return errors

    orig_tokens = tokenize_text(original_text)
    edit_tokens = tokenize_text(edited_text)
    orig_words = [t['text'] for t in orig_tokens]
    edit_words = [t['text'] for t in edit_tokens]

    if not orig_tokens or not edit_tokens:
        return errors

    matcher = difflib.SequenceMatcher(None, orig_words, edit_words, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        # --- Extract text chunks ---
        orig_chunk = ' '.join(orig_words[i1:i2])
        edit_chunk = ' '.join(edit_words[j1:j2])
        orig_chunk_str = original_text[orig_tokens[i1]['start']:orig_tokens[i2-1]['end']] if i1 < i2 else ''
        edit_chunk_str = edited_text[edit_tokens[j1]['start']:edit_tokens[j2-1]['end']] if j1 < j2 else ''
        start_offset = edit_tokens[j1]['start'] if j1 < j2 else (edit_tokens[j1-1]['end'] if j1 > 0 else 0)
        end_offset = edit_tokens[j2-1]['end'] if j1 < j2 else start_offset

        # === RULE 1: Numerical value changes (CRITICAL) ===
        o_nums = re.findall(r'\b\d+(?:\.\d+)?\b', orig_chunk)
        e_nums = re.findall(r'\b\d+(?:\.\d+)?\b', edit_chunk)
        if o_nums != e_nums and (o_nums or e_nums):
            errors.append(make_error(
                'NUMERICAL_VALUE_CHANGED', 'CRITICAL', start_offset, end_offset,
                orig_chunk_str, edit_chunk_str,
                'Numerical value changed: "{}" -> "{}"'.format(orig_chunk_str, edit_chunk_str),
                edited_text))
            continue

        # === RULE 2: Unit / Dosage / Percentage changes (CRITICAL) ===
        unit_pat = r'\b\d+(?:\.\d+)?\s*(?:mg|mL|U/L|%|kg|g|L|mmol|IU)\b'
        o_units = re.findall(unit_pat, orig_chunk, re.IGNORECASE)
        e_units = re.findall(unit_pat, edit_chunk, re.IGNORECASE)
        if o_units != e_units and (o_units or e_units):
            errors.append(make_error(
                'UNIT_CHANGED', 'CRITICAL', start_offset, end_offset,
                orig_chunk_str, edit_chunk_str,
                'Unit/Dosage/Percentage changed: "{}" -> "{}"'.format(orig_chunk_str, edit_chunk_str),
                edited_text))
            continue

        # === RULE 3: DOI changes (CRITICAL) ===
        doi_pat = r'\b10\.\d{4,}/\S+'
        if re.search(doi_pat, orig_chunk):
            errors.append(make_error(
                'DOI_CHANGED', 'CRITICAL', start_offset, end_offset,
                orig_chunk_str, edit_chunk_str,
                'DOI changed: "{}" -> "{}"'.format(orig_chunk_str, edit_chunk_str),
                edited_text))
            continue

        # === RULE 4: Author name / Affiliation proper-noun swaps (CRITICAL) ===
        if tag == 'replace':
            is_o_proper = sum(1 for w in orig_words[i1:i2] if w.istitle() and w.lower() not in {'the', 'a', 'an', 'in', 'of', 'at', 'by', 'for', 'to', 'on', 'with', 'and', 'or', 'but', 'was', 'were', 'is', 'are', 'has', 'had', 'have', 'be'}) > 0
            is_e_proper = sum(1 for w in edit_words[j1:j2] if w.istitle() and w.lower() not in {'the', 'a', 'an', 'in', 'of', 'at', 'by', 'for', 'to', 'on', 'with', 'and', 'or', 'but', 'was', 'were', 'is', 'are', 'has', 'had', 'have', 'be'}) > 0
            # Only trigger for short name-like replacements, not whole sentence restructurings
            # A full sentence restructuring usually has verbs in the original
            has_verb_in_orig = bool(re.search(r'\b(was|were|is|are|has|had|have|presented|admitted|treated|diagnosed|showed|found)\b', orig_chunk, re.IGNORECASE))
            if is_o_proper and is_e_proper and (i2-i1) <= 5 and (j2-j1) <= 5 and not has_verb_in_orig:
                is_affil = any(kw in original_text.lower() for kw in AFFILIATION_KWS) or ',' in orig_chunk_str
                error_type = 'AFFILIATION_CHANGED' if is_affil else 'AUTHOR_NAME_CHANGED'
                ratio = difflib.SequenceMatcher(None, orig_chunk_str.lower(), edit_chunk_str.lower()).ratio()
                if ratio > 0.3:
                    errors.append(make_error(
                        error_type, 'CRITICAL', start_offset, end_offset,
                        orig_chunk_str, edit_chunk_str,
                        '{} changed: "{}" -> "{}"'.format(
                            error_type.replace('_', ' ').title(), orig_chunk_str, edit_chunk_str),
                        edited_text))
                    continue


        # === RULE 5: Important content deleted (pure deletion) ===
        if tag == 'delete' and is_meaningful_content(orig_chunk_str):
            has_medical = any(re.search(pat, orig_chunk_str, re.IGNORECASE) for pat in MEDICAL_PATTERNS)
            if has_medical or len(orig_words[i1:i2]) >= 3:
                errors.append(make_error(
                    'IMPORTANT_CONTENT_REMOVED', 'CRITICAL', start_offset, end_offset,
                    orig_chunk_str, '[DELETED]',
                    'Important content removed: "{}"'.format(orig_chunk_str),
                    edited_text))
                continue

        # === RULE 6: Important content inserted ===
        if tag == 'insert' and is_meaningful_content(edit_chunk_str):
            has_medical = any(re.search(pat, edit_chunk_str, re.IGNORECASE) for pat in MEDICAL_PATTERNS)
            if has_medical or len(edit_words[j1:j2]) >= 4:
                errors.append(make_error(
                    'IMPORTANT_CONTENT_ADDED', 'REVIEW', start_offset, end_offset,
                    '[NOT IN ORIGINAL]', edit_chunk_str,
                    'Important factual content added: "{}"'.format(edit_chunk_str),
                    edited_text))
                continue

        # === RULE 7: Replace where much content was lost ===
        if tag == 'replace' and is_meaningful_content(orig_chunk_str):
            orig_wc = len(orig_words[i1:i2])
            edit_wc = len(edit_words[j1:j2])
            has_orig_medical = any(re.search(pat, orig_chunk_str, re.IGNORECASE) for pat in MEDICAL_PATTERNS)
            if (orig_wc >= 3 and edit_wc <= 1) or (has_orig_medical and edit_wc < orig_wc // 2):
                # Check if original words are still present elsewhere in edited text (just reorganized)
                trivial_words = {'the', 'a', 'an', 'is', 'was', 'were', 'with', 'and', 'or', 'to', 'of', 'patient'}
                orig_meaningful_words = [w.lower().rstrip('.,;:') for w in orig_words[i1:i2]
                                         if w.lower().rstrip('.,;:') not in trivial_words]
                edit_words_lower = [w.lower().rstrip('.,;:') for w in edit_words]
                if orig_meaningful_words:
                    words_preserved = sum(1 for w in orig_meaningful_words if w in edit_words_lower) / len(orig_meaningful_words)
                else:
                    words_preserved = 0.0
                if words_preserved < 0.5:
                    errors.append(make_error(
                        'IMPORTANT_CONTENT_REMOVED', 'CRITICAL', start_offset, end_offset,
                        orig_chunk_str, edit_chunk_str if edit_chunk_str else '[DELETED]',
                        'Important content removed/replaced: "{}" -> "{}"'.format(orig_chunk_str, edit_chunk_str),
                        edited_text))
                    continue

            # Small semantic difference - flag as REVIEW only if semantically distinct
            if is_meaningful_content(edit_chunk_str):
                # Strip punctuation for comparison to avoid false positives
                orig_alpha = re.sub(r'[^a-z0-9\s]', '', orig_chunk_str.lower())
                edit_alpha = re.sub(r'[^a-z0-9\s]', '', edit_chunk_str.lower())
                if orig_alpha == edit_alpha:
                    pass  # Only punctuation changed - not an error
                else:
                    # Check if the original words are still present in the full edited text
                    # (sentence was restructured, not content changed)
                    trivial_set = {'the', 'a', 'an', 'is', 'was', 'were', 'with', 'and',
                                   'or', 'to', 'of', 'in', 'on', 'at', 'by', 'for', 'from',
                                   'patient', 'presented'}
                    orig_mw = [w.lower().rstrip('.,;:!?') for w in orig_words[i1:i2]
                               if w.lower().rstrip('.,;:!?') not in trivial_set and len(w) > 2]
                    edit_all_lower = [w.lower().rstrip('.,;:!?') for w in edit_words]
                    if orig_mw:
                        preserved = sum(1 for w in orig_mw if w in edit_all_lower) / len(orig_mw)
                    else:
                        preserved = 1.0
                    if preserved >= 0.5:
                        pass  # Words reorganized, not truly changed
                    else:
                        ratio = difflib.SequenceMatcher(None, orig_chunk_str.lower(), edit_chunk_str.lower()).ratio()
                        if ratio < 0.6:
                            errors.append(make_error(
                                'CONTENT_CHANGE', 'REVIEW', start_offset, end_offset,
                                orig_chunk_str, edit_chunk_str,
                                'Possible content change: "{}" -> "{}"'.format(orig_chunk_str, edit_chunk_str),
                                edited_text))



    return errors


# ─────────────────────────────────────────────
# CHECK: SPELLING
# ─────────────────────────────────────────────

def check_spelling(edited_text, protected_terms):
    """
    Check for spelling errors in the edited manuscript.
    Uses pyspellchecker. Skips protected terms.
    """
    errors = []
    try:
        from spellchecker import SpellChecker
        spell = SpellChecker()
    except ImportError:
        return errors

    protected_lower = {t.lower() for t in protected_terms}
    word_pattern = re.compile(r'\b([a-zA-Z]{3,})\b')

    for match in word_pattern.finditer(edited_text):
        word = match.group(1)
        word_lower = word.lower()

        if word_lower in protected_lower or word in protected_terms:
            continue
        if word.isupper():
            continue
        if re.match(r'^[a-z]+[A-Z]', word):
            continue

        if word_lower not in spell and spell.unknown([word_lower]):
            correction = spell.correction(word_lower)
            if correction and correction != word_lower:
                start = match.start()
                end = match.end()
                errors.append(make_error(
                    error_type='SPELLING_ERROR',
                    severity='REVIEW',
                    start_offset=start,
                    end_offset=end,
                    original_text=correction,
                    edited_text=word,
                    description='Possible spelling mistake: "{}" — did you mean "{}"?'.format(word, correction),
                    full_edited_text=edited_text,
                ))

    return errors


# ─────────────────────────────────────────────
# CHECK: JOB SHEET COMPLIANCE
# ─────────────────────────────────────────────

def check_job_sheet_compliance(edited_text, instructions):
    """Check edited manuscript against Job Sheet instructions."""
    errors = []
    if not instructions:
        return errors

    for instr in instructions:
        section = instr.section if hasattr(instr, 'section') else instr.get('section', '')
        instruction = instr.instruction if hasattr(instr, 'instruction') else instr.get('instruction', '')
        status = instr.status if hasattr(instr, 'status') else instr.get('status', '')

        if status == 'Manual Review Required':
            continue

        section_lower = section.lower().strip()
        instruction_lower = instruction.lower().strip()

        # Citation style check  (section: 'Citation Style')
        if 'citation' in section_lower:
            if instruction_lower == 'numbered':
                author_year_pat = r'\([A-Z][a-z]+(?:\s+(?:et\s+al\.?|and\s+[A-Z][a-z]+))?,?\s*\d{4}\)'
                for m in re.finditer(author_year_pat, edited_text):
                    errors.append(make_error(
                        error_type='JOB_SHEET_STYLE',
                        severity='REVIEW',
                        start_offset=m.start(),
                        end_offset=m.end(),
                        original_text='[Numbered citation required]',
                        edited_text=m.group(),
                        description='Job Sheet requires numbered citations, but author-year format found: "{}"'.format(m.group()),
                        full_edited_text=edited_text,
                    ))

        # Full stop after figure caption check  (section: 'Full Stop After Figure Caption')
        if ('full stop' in section_lower or 'fullstop' in section_lower.replace(' ', '')) and ('figure' in section_lower or 'caption' in section_lower):
            if instruction_lower in ('no', 'false', '0'):
                for m in re.finditer(r'(Fig(?:ure)?\.?\s*\d+[.:]\s*[^\n]+)\.\s*(?:\n|$)', edited_text):
                    errors.append(make_error(
                        error_type='JOB_SHEET_STYLE',
                        severity='WARNING',
                        start_offset=m.start(),
                        end_offset=m.end(),
                        original_text=m.group().rstrip('. \n'),
                        edited_text=m.group().rstrip('\n'),
                        description='Job Sheet says no full stop after figure caption, but one was found.',
                        full_edited_text=edited_text,
                    ))

        # Competing interests check  (section: 'Include Competing Interests')
        if ('competing' in section_lower and 'interest' in section_lower) or section_lower == 'include competing interests':
            if instruction_lower in ('required', 'yes', 'true', '1'):
                if 'competing interest' not in edited_text.lower() and 'conflict of interest' not in edited_text.lower():
                    errors.append(make_error(
                        error_type='JOB_SHEET_STYLE',
                        severity='REVIEW',
                        start_offset=len(edited_text) - 1,
                        end_offset=len(edited_text),
                        original_text='[Competing interests statement required]',
                        edited_text='[Not found in manuscript]',
                        description='Job Sheet requires a competing interests statement, but none was found.',
                        full_edited_text=edited_text,
                    ))


        # InitialWord Heading Case check  (section: 'Title Upper Case', 'TitleUpperCase', etc.)
        _is_title_section = ('title' in section_lower or 'heading' in section_lower or 'uppercase' in section_lower.replace(' ', '') or 'upper case' in section_lower)
        is_initial_word = 'initial word' in instruction_lower or 'initialword' in instruction_lower.replace(' ', '')
        is_initial_letter = 'initial letter' in instruction_lower or 'initialletter' in instruction_lower.replace(' ', '')
        if (is_initial_word or is_initial_letter) and _is_title_section:
            # Find likely heading lines
            for m in re.finditer(r'^[ \t]*([A-Za-z0-9][^\n]{0,150})[ \t]*$', edited_text, re.MULTILINE):
                line = m.group(1).strip()
                # Skip likely normal sentences ending in period
                if line.endswith('.') and not re.match(r'^\d+\.$', line):
                    continue
                # Skip heuristic author/affiliation lines
                skip_words = {'department', 'university', 'hospital', 'institute', 'clinic', 'author', 'journal', 'doi:', 'correspondence'}
                line_words = {w.strip('.,:;').lower() for w in re.split(r'\s+', line)}
                if skip_words.intersection(line_words) or line.count(',') > 2:
                    continue
                
                is_all_caps = line.isupper()
                heading_keywords = {'abstract', 'introduction', 'background', 'methods', 'results', 'discussion', 'conclusion', 'case', 'presentation', 'findings', 'treatment', 'outcome', 'objective', 'materials', 'acknowledgments', 'references'}
                has_keyword = any(kw in line.lower() for w in re.split(r'\s+', line.lower()) for kw in heading_keywords if kw == w.strip('.,:;'))
                
                # Only process if it's all caps or contains a common heading keyword
                if not is_all_caps and not has_keyword:
                    continue
                
                words = re.split(r'(\s+)', line)
                expected_words = []
                first_word = True
                is_changed = False
                
                for w in words:
                    if not w.strip():
                        expected_words.append(w)
                        continue
                    
                    word_clean = w.strip('.,;:()[]{}"\'')
                    if not any(c.isalpha() for c in word_clean):
                        expected_words.append(w)
                        continue

                    # Heuristic for acronym: uppercase, length > 1, and the whole line isn't uppercase
                    is_acronym = word_clean.isupper() and len(word_clean) > 1 and not is_all_caps
                    # Heuristic for mixed case proper nouns (e.g., McDonald, iPad)
                    is_mixed_case = any(c.isupper() for c in word_clean[1:]) and not is_all_caps

                    if is_acronym or is_mixed_case:
                        expected_words.append(w)
                    else:
                        if is_initial_word:
                            expected_w = w.capitalize() if first_word else w.lower()
                        elif is_initial_letter:
                            minor_words = {'a', 'an', 'the', 'and', 'but', 'or', 'for', 'nor', 'on', 'at', 'to', 'from', 'by', 'in', 'of', 'with'}
                            expected_w = w.capitalize() if (first_word or w.lower() not in minor_words) else w.lower()
                        else:
                            expected_w = w
                            
                        expected_words.append(expected_w)
                        if expected_w != w:
                            is_changed = True
                    
                    if any(c.isalpha() for c in w):
                        first_word = False
                
                expected_line = "".join(expected_words)
                    
                if is_changed and expected_line != line:
                    rule_name = "Initial Letter" if is_initial_letter else "Initial Word"
                    desc = (
                        f"Rule:\nTitleUpperCase = {rule_name}\n\n"
                        f"Element:\n{section}\n\n"
                        f"Current:\n{line}\n\n"
                        f"Expected:\n{expected_line}\n\n"
                        "Location:\nExact line/section"
                    )
                    errors.append(make_error(
                        error_type='JOB_SHEET_STYLE_ERROR',
                        severity='REVIEW',
                        start_offset=m.start(1),
                        end_offset=m.end(1),
                        original_text=expected_line,
                        edited_text=line,
                        description=desc,
                        full_edited_text=edited_text,
                    ))

    return errors


# Global reference to protected terms for use in sub-checks
protected_terms_global = set()


# ─────────────────────────────────────────────
# CHECK: REFERENCES
# ─────────────────────────────────────────────

def check_references(edited_text, reference_rules):
    """Validate manuscript references against the configured reference style rules."""
    errors = []
    if not reference_rules:
        return errors

    ref_section_start = -1
    ref_patterns = [r'\bReferences\b', r'\bBibliography\b', r'\bREFERENCES\b', r'\bLiterature\s+[Cc]ited\b']
    for pat in ref_patterns:
        m = re.search(pat, edited_text)
        if m:
            ref_section_start = m.start()
            break

    if ref_section_start < 0:
        return errors

    ref_section = edited_text[ref_section_start:]

    for rule in reference_rules:
        if not hasattr(rule, 'is_active') or not rule.is_active:
            continue
        cat = rule.category.lower() if hasattr(rule, 'category') else ''
        rule_text = rule.rule_text if hasattr(rule, 'rule_text') else ''

        if 'doi' in cat and 'required' in rule_text.lower():
            ref_entries = re.findall(r'^\s*\[?\d+\]?.+$', ref_section, re.MULTILINE)
            for entry in ref_entries:
                if entry.strip() and not re.search(r'doi|DOI|https?://doi\.org', entry):
                    offset = ref_section_start + ref_section.find(entry)
                    if offset >= ref_section_start:
                        errors.append(make_error(
                            error_type='REFERENCE',
                            severity='WARNING',
                            start_offset=offset,
                            end_offset=offset + len(entry),
                            original_text='[DOI required]',
                            edited_text=entry[:200],
                            description='Reference may be missing DOI (required by reference style).',
                            full_edited_text=edited_text,
                        ))

    return errors


# ─────────────────────────────────────────────
# CHECK: AUTHOR & AFFILIATION FORMATTING
# ─────────────────────────────────────────────

def check_author_affiliation_formatting(edited_text):
    """Check for missing superscripts and asterisks in the author/affiliation section."""
    errors = []
    
    # Try to find the start of the main text to limit our search for authors/affiliations
    header_end = 4000
    for keyword in ['\nAbstract\n', '\nIntroduction\n', '\nCase Presentation\n', '\nBackground\n']:
        idx = edited_text.lower().find(keyword.lower())
        if idx != -1:
            header_end = min(header_end, idx)
            
    header = edited_text[:header_end]
    
    # 1. Affiliations starting with regular digits (1, 2, 3...) instead of superscript
    affil_pat = r'^[ \t]*([1-9]+)\s*[-.]?\s*[A-Z]'
    for m in re.finditer(affil_pat, header, re.MULTILINE):
        marker = m.group(1)
        errors.append(make_error(
            error_type='JOB_SHEET_STYLE_ERROR',
            severity='REVIEW',
            start_offset=m.start(1),
            end_offset=m.end(1),
            original_text=marker,
            edited_text=marker,
            description=f'Affiliation number not in superscript. Found regular number: "{marker}"',
            full_edited_text=edited_text,
        ))

    # 2. Authors ending with regular digits instead of superscript
    # Matches names followed by comma/space and a digit 1-9 (e.g., "John Smith, 1" or "Jane Doe 2")
    author_marker_pat = r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)*),?\s*([1-9]+[*,]*|[*,]+[1-9]+)(?!\s*(?:st|nd|rd|th))'
    for m in re.finditer(author_marker_pat, header):
        marker = m.group(2)
        if any(c.isdigit() for c in marker):
            errors.append(make_error(
                error_type='JOB_SHEET_STYLE_ERROR',
                severity='REVIEW',
                start_offset=m.start(2),
                end_offset=m.end(2),
                original_text=marker,
                edited_text=marker,
                description=f'Affiliation number not in superscript. Found regular number: "{marker}"',
                full_edited_text=edited_text,
            ))

    # 3. Email ID and Corresponding Author Asterisk Logic
    email_match = re.search(r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', header)
    has_asterisk = '*' in header

    # If there is a corresponding author (*), there must be an email.
    if has_asterisk and not email_match:
        errors.append(make_error(
            error_type='JOB_SHEET_STYLE_ERROR',
            severity='REVIEW',
            start_offset=0,
            end_offset=10,
            original_text='[Email]',
            edited_text='[Missing]',
            description='Missing email ID for corresponding author (author marked with *).',
            full_edited_text=edited_text,
        ))

    # If there is an email, there should be a corresponding author (*)
    if email_match and not has_asterisk:
        first_author_match = re.search(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b', header)
        start_off = first_author_match.start() if first_author_match else 0
        errors.append(make_error(
            error_type='JOB_SHEET_STYLE_ERROR',
            severity='REVIEW',
            start_offset=start_off,
            end_offset=start_off + 15,
            original_text='[Author]*',
            edited_text='[No Asterisk]',
            description='Corresponding author name should have an asterisk (*). No asterisk found in author list, but email ID is present.',
            full_edited_text=edited_text,
        ))

    return errors


# ─────────────────────────────────────────────
# MAIN QC ENGINE ENTRY POINT
# ─────────────────────────────────────────────

def run_content_qc(original_text, edited_text, job_sheet_instructions=None,
                   reference_rules=None, style_rules=None, run_spelling=True, run_content=True, run_references=True, run_job_sheet=True):
    """
    Main QC engine entry point.

    Args:
        original_text: The original manuscript content (source of truth)
        edited_text: The edited manuscript content (being checked)
        job_sheet_instructions: List of JobSheetInstruction objects
        reference_rules: List of ReferenceRule objects
        style_rules: List of StyleRule objects (legacy rule-based checks)

    Returns:
        List of error dicts ready to be stored as ErrorRecord rows.

    IMPORTANT: This function NEVER modifies manuscript text.
    """
    global protected_terms_global

    if not edited_text:
        return []

    all_errors = []

    # Build protected terms from both manuscripts
    protected = build_protected_terms(original_text or '', edited_text)
    protected_terms_global = protected

    # 1. Content comparison (Original vs Edited)
    if run_content and original_text and original_text != edited_text:
        content_errors = check_content_changes(original_text, edited_text)
        all_errors.extend(content_errors)

    # 2. Spelling check on edited manuscript
    if run_spelling:
        spelling_errors = check_spelling(edited_text, protected)
        content_ranges = [(e['start_offset'], e['end_offset']) for e in all_errors]
        for se in spelling_errors:
            overlaps = any(
                se['start_offset'] >= cr[0] and se['end_offset'] <= cr[1]
                for cr in content_ranges
            )
            if not overlaps:
                all_errors.append(se)

    # 3. Job Sheet compliance
    if run_job_sheet and job_sheet_instructions:
        js_errors = check_job_sheet_compliance(edited_text, job_sheet_instructions)
        all_errors.extend(js_errors)

    # 4. Reference style validation
    if run_references and reference_rules:
        ref_errors = check_references(edited_text, reference_rules)
        all_errors.extend(ref_errors)

    # 4.5. Author/Affiliation Formatting (always runs on header)
    affil_errors = check_author_affiliation_formatting(edited_text)
    all_errors.extend(affil_errors)

    js_instrs = [i for i in job_sheet_instructions if i.status != 'Manual Review Required'] if job_sheet_instructions else []
    total_available_rules = 0
    if style_rules and js_instrs:
        total_available_rules = len([r for r in style_rules if r.is_active and next((i for i in js_instrs if i.section.lower().strip() in (r.name.lower().strip(), (r.section or '').lower().strip())), None)])

    applied_rule_ids = []

    # 5. Legacy style rules (filtered by Job Sheet)
    if style_rules and job_sheet_instructions:
        # Extract active job sheet instructions
        js_instrs = [i for i in job_sheet_instructions if i.status != 'Manual Review Required']
        
        def matches_job_sheet(rule, instr):
            r_name = rule.name.lower().strip()
            r_name_no = r_name.replace(' ', '')
            r_sec = (rule.section or '').lower().strip()
            i_sec = instr.section.lower().strip()
            i_val = (instr.instruction if hasattr(instr, 'instruction') else instr.get('instruction', '')).lower().strip()
            i_val_no = i_val.replace(' ', '')
            
            if i_sec in (r_name, r_sec): return True
            if i_val_no and i_val_no in (r_name_no, r_sec.replace(' ', '')): return True
            if r_name and (r_name in i_sec or r_name_no in i_val_no): return True
            return False

        for rule in style_rules:
            if not rule.is_active:
                continue

            # Check if this rule is explicitly assigned in the job sheet
            matching_instr = next((i for i in js_instrs if matches_job_sheet(rule, i)), None)
            
            if not matching_instr:
                # Rule not selected in job sheet -> skip
                continue
            
            applied_rule_ids.append(rule.id)
            instr_val = matching_instr.instruction.lower().strip()

            # Special case: Heading sentence case
            if rule.scope and rule.scope.lower() == 'all section headings':
                # Find all potential headings in the text
                for match in re.finditer(r'^[ \t]*([A-Za-z0-9][^\n]{1,150}?)[ \t]*$', edited_text, re.MULTILINE):
                    found = match.group(1)
                    if found.endswith('.'):
                        continue # Probably a short sentence, not a heading
                    
                    sentence_cased = found[0].upper() + found[1:].lower() if found else ""
                    
                    if rule.replacement == '{sentence_case}':
                        expected = sentence_cased
                    else:
                        expected = rule.replacement
                        
                    if found != expected:
                        all_errors.append(make_error(
                            error_type='SECTION_HEADING_SENTENCE_CASE' if rule.replacement == '{sentence_case}' else 'JOB_SHEET_STYLE',
                            severity='Style',
                            start_offset=match.start(1),
                            end_offset=match.end(1),
                            original_text=expected,
                            edited_text=found,
                            description=f'Section heading must use sentence case' if rule.replacement == '{sentence_case}' else f'Style rule "{rule.name}": expected "{expected}", found "{found}"',
                            full_edited_text=edited_text,
                            rule_id=rule.id,
                        ))
                continue

            try:
                if instr_val == 'initial word' or instr_val == 'initialword':
                    # Must appear as the first word of the scope (e.g. beginning of a sentence/line)
                    # Use a regex that matches at start of string or after a punctuation + space or newline
                    pattern_str = r'(?:^|\n|(?<=[.!?]\s))\s*\b(' + re.escape(rule.find_pattern) + r')\b'
                else:
                    pattern_str = r'\b(' + re.escape(rule.find_pattern) + r')\b'
                pattern = re.compile(pattern_str)
            except Exception:
                pattern = re.compile(re.escape(rule.find_pattern))

            for match in pattern.finditer(edited_text):
                found = match.group(1) if match.lastindex else match.group()
                if found != rule.replacement:
                    all_errors.append(make_error(
                        error_type='JOB_SHEET_STYLE',
                        severity='Style',
                        start_offset=match.start(1) if match.lastindex else match.start(),
                        end_offset=match.end(1) if match.lastindex else match.end(),
                        original_text=rule.replacement,
                        edited_text=found,
                        description='Style rule "{}": expected "{}", found "{}"'.format(rule.name, rule.replacement, found),
                        full_edited_text=edited_text,
                        rule_id=rule.id,
                    ))

    # Sort by position in document
    all_errors.sort(key=lambda e: (e['start_offset'], e['end_offset']))

    # Assign sequential IDs
    for i, err in enumerate(all_errors):
        err['_seq'] = i + 1

    return all_errors, applied_rule_ids, total_available_rules


# ─────────────────────────────────────────────
# RESTORE ORIGINAL — targeted replacement
# ─────────────────────────────────────────────

def restore_original(working_text, error_record):
    """
    Replace ONLY the exact error range in working_text with the original_text.
    Does NOT rewrite the surrounding sentence, paragraph, or formatting.

    Args:
        working_text: Current manuscript working text
        error_record: ErrorRecord ORM object with start_offset, end_offset, original_text

    Returns:
        Updated working_text with only the targeted replacement made.
        Returns unchanged working_text if original is not restorable.
    """
    start = error_record.start_offset
    end = error_record.end_offset
    original = error_record.original_text

    if original and original not in ('[NOT IN ORIGINAL]', '[DELETED]'):
        new_text = working_text[:start] + original + working_text[end:]
        return new_text

    return working_text

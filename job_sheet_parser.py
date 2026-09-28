import xml.etree.ElementTree as ET
import re
import io


def _format_section(raw_tag):
    """Convert camelCase or snake_case tag to a readable section name."""
    # Remove namespace prefix if present
    if '}' in raw_tag:
        raw_tag = raw_tag.split('}', 1)[1]
    # Insert spaces before capital letters (camelCase → Title Case words)
    return re.sub(r'(?<!^)(?=[A-Z])', ' ', raw_tag).strip()


def _parse_xml(file_bytes):
    instructions = []
    try:
        root = ET.fromstring(file_bytes)
        for elem in root.iter():
            if elem == root:
                continue
            section = _format_section(elem.tag)
            if elem.attrib:
                for attr_name, attr_value in elem.attrib.items():
                    instructions.append({
                        'section': section,
                        'instruction': attr_value,
                        'source': 'Job Sheet',
                        'status': 'Detected'
                    })
            elif elem.text and elem.text.strip():
                instructions.append({
                    'section': section,
                    'instruction': elem.text.strip(),
                    'source': 'Job Sheet',
                    'status': 'Detected'
                })
    except ET.ParseError:
        instructions.append({
            'section': 'XML Parsing',
            'instruction': 'Unable to parse valid XML structure.',
            'source': 'System',
            'status': 'Manual Review Required'
        })
    return instructions


def _parse_pdf(file_bytes):
    """Extract key-value instructions from a PDF job sheet using pdfplumber."""
    instructions = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                # 1. Extract from tables (most structured job sheets use tables)
                for table in page.extract_tables():
                    for row in table:
                        if not row:
                            continue
                        # Filter out None cells
                        cells = [str(c).strip() for c in row if c and str(c).strip()]
                        if len(cells) >= 2:
                            key = cells[0]
                            value = cells[1]
                            if key and value:
                                instructions.append({
                                    'section': key,
                                    'instruction': value,
                                    'source': 'Job Sheet',
                                    'status': 'Detected'
                                })
                        elif len(cells) == 1:
                            # Might be a header row — skip
                            pass

                # 2. Also extract from plain text lines using "Key: Value" or "Key = Value" patterns
                text = page.extract_text() or ''
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                
                known_keys = [
                    'Article Title', 'Title Upper Case', 'TitleUpperCase', 'Title Case', 
                    'Heading Case', 'Citation Style', 'CitationStyle', 'Bibliography Style', 'BibliographyStyle'
                ]
                
                for i, line in enumerate(lines):
                    # Match patterns like:  TitleUpperCase: InitialWord
                    m = re.match(r'^([A-Za-z][A-Za-z0-9 _/\-]{2,60}?)\s*[:=]\s*(.+)$', line)
                    if not m:
                        m = re.match(r'^([A-Za-z][A-Za-z0-9 _/\-]{2,60}?)(?:\s{2,}|\t+)(.+)$', line)
                    
                    key = None
                    value = None
                    
                    if m:
                        key = m.group(1).strip()
                        value = m.group(2).strip()
                    else:
                        # Check if line starts with a known key followed by space or end of string
                        for k in known_keys:
                            if line.lower().startswith(k.lower()):
                                remainder = line[len(k):].strip(' :-=')
                                if remainder:
                                    key = k
                                    value = remainder
                                    break
                                elif i + 1 < len(lines):
                                    key = k
                                    value = lines[i + 1]
                                    break
                    
                    if key and value:
                        # Avoid duplicates already found from tables
                        already = any(
                            inst['section'].lower() == key.lower() and inst['instruction'].lower() == value.lower()
                            for inst in instructions
                        )
                        if not already:
                            instructions.append({
                                'section': key,
                                'instruction': value,
                                'source': 'Job Sheet',
                                'status': 'Detected'
                            })

    except ImportError:
        instructions.append({
            'section': 'PDF Parsing',
            'instruction': 'pdfplumber is not installed. Cannot parse PDF job sheet.',
            'source': 'System',
            'status': 'Manual Review Required'
        })
    except Exception as e:
        instructions.append({
            'section': 'PDF Parsing',
            'instruction': f'Error reading PDF: {e}',
            'source': 'System',
            'status': 'Manual Review Required'
        })

    if not instructions:
        instructions.append({
            'section': 'PDF Parsing',
            'instruction': 'No structured data could be extracted from this PDF. Please review manually.',
            'source': 'System',
            'status': 'Manual Review Required'
        })

    return instructions


def _parse_docx(file_bytes):
    """Extract key-value instructions from a DOCX job sheet."""
    instructions = []
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))

        # Try tables first
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if len(cells) >= 2:
                    instructions.append({
                        'section': cells[0],
                        'instruction': cells[1],
                        'source': 'Job Sheet',
                        'status': 'Detected'
                    })

        # Also scan paragraphs for key: value lines
        for para in doc.paragraphs:
            line = para.text.strip()
            m = re.match(r'^([A-Za-z][A-Za-z0-9 _/\-]{2,60}?)\s*[:=]\s*(.+)$', line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                already = any(
                    i['section'].lower() == key.lower() and i['instruction'].lower() == value.lower()
                    for i in instructions
                )
                if not already:
                    instructions.append({
                        'section': key,
                        'instruction': value,
                        'source': 'Job Sheet',
                        'status': 'Detected'
                    })

    except ImportError:
        instructions.append({
            'section': 'DOCX Parsing',
            'instruction': 'python-docx is not installed.',
            'source': 'System',
            'status': 'Manual Review Required'
        })
    except Exception as e:
        instructions.append({
            'section': 'DOCX Parsing',
            'instruction': f'Error reading DOCX: {e}',
            'source': 'System',
            'status': 'Manual Review Required'
        })

    if not instructions:
        instructions.append({
            'section': 'DOCX Parsing',
            'instruction': 'No structured data found in DOCX. Please review manually.',
            'source': 'System',
            'status': 'Manual Review Required'
        })
    return instructions


def parse_job_sheet(filename, file_bytes):
    """
    Parses a Job Sheet and extracts structured instructions.
    Supports: .xml, .pdf, .docx, .doc, .txt
    Returns a list of dicts:
    [
        {
            'section': 'Title Upper Case',
            'instruction': 'InitialWord',
            'source': 'Job Sheet',
            'status': 'Detected'
        }, ...
    ]
    """
    fname = filename.lower()

    if fname.endswith('.xml'):
        return _parse_xml(file_bytes)

    elif fname.endswith('.pdf'):
        return _parse_pdf(file_bytes)

    elif fname.endswith('.docx') or fname.endswith('.doc'):
        return _parse_docx(file_bytes)

    elif fname.endswith('.txt') or fname.endswith('.csv'):
        # Plain text: parse "Key: Value" or "Key = Value" lines
        instructions = []
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            
            known_keys = [
                'Article Title', 'Title Upper Case', 'TitleUpperCase', 'Title Case', 
                'Heading Case', 'Citation Style', 'CitationStyle', 'Bibliography Style', 'BibliographyStyle'
            ]
            
            for i, line in enumerate(lines):
                m = re.match(r'^([A-Za-z][A-Za-z0-9 _/\-]{2,60}?)\s*[:=]\s*(.+)$', line)
                if not m:
                    # Fallback for 2+ spaces or tabs
                    m = re.match(r'^([A-Za-z][A-Za-z0-9 _/\-]{2,60}?)(?:\s{2,}|\t+)(.+)$', line)
                
                key = None
                value = None
                if m:
                    key = m.group(1).strip()
                    value = m.group(2).strip()
                else:
                    for k in known_keys:
                        if line.lower().startswith(k.lower()):
                            remainder = line[len(k):].strip(' :-=')
                            if remainder:
                                key = k
                                value = remainder
                                break
                            elif i + 1 < len(lines):
                                key = k
                                value = lines[i + 1]
                                break
                
                if key and value:
                    already = any(
                        inst['section'].lower() == key.lower() and inst['instruction'].lower() == value.lower()
                        for inst in instructions
                    )
                    if not already:
                        instructions.append({
                            'section': key,
                            'instruction': value,
                            'source': 'Job Sheet',
                            'status': 'Detected'
                        })
        except Exception:
            pass
        if not instructions:
            instructions.append({
                'section': 'Text Parsing',
                'instruction': 'No key-value pairs found in text file.',
                'source': 'System',
                'status': 'Manual Review Required'
            })
        return instructions

    else:
        return [{
            'section': 'File Type',
            'instruction': f'Unsupported file format: {filename}',
            'source': 'System',
            'status': 'Manual Review Required'
        }]

"""
Test suite validating the strict validation output policy and affiliation superscript validation.
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

from critical_rules import (
    run_critical_rules,
    check_affiliation_superscripts,
    check_affiliations,
    check_corresponding_author_name,
)

CLEAN_ORIGINAL = """Effects of Novel Therapeutic Compounds on Health Outcomes

John Smith¹,²

¹ Department of Medicine, University A
² Institute of Research, University B

* Corresponding author: John Smith, john.smith@university.edu

References
[1] Smith J. Medicine 2020.
[2] Research B. Institute 2021.
"""

def test_clean_manuscript():
    print("Testing clean valid manuscript...")
    errors, rule_ids, run_log = run_critical_rules(CLEAN_ORIGINAL, CLEAN_ORIGINAL)
    assert len(errors) == 0, f"Expected 0 errors, got {len(errors)}: {errors}"
    assert len(rule_ids) == 5, f"Expected 5 mandatory checks, got {len(rule_ids)}"
    print("  ✓ Clean manuscript produced 0 errors on 5 mandatory checks.")

def test_no_affiliation_superscript():
    print("\nTesting author with no affiliation superscript...")
    edited = CLEAN_ORIGINAL.replace("John Smith¹,²", "John Smith")
    errors = check_affiliation_superscripts(CLEAN_ORIGINAL, edited)
    no_sup_errors = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'no affiliation superscript' in e['description']]
    assert len(no_sup_errors) > 0, "Expected error for author with no affiliation superscript"
    err = no_sup_errors[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

def test_affiliation_not_superscript_on_author():
    print("\nTesting non-superscript number on author...")
    edited = CLEAN_ORIGINAL.replace("John Smith¹,²", "John Smith1,2")
    errors = check_affiliation_superscripts(CLEAN_ORIGINAL, edited)
    not_sup_errors = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'not superscript' in e['description']]
    assert len(not_sup_errors) > 0, "Expected error for non-superscript number on author"
    err = not_sup_errors[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

def test_affiliation_block_not_superscript():
    print("\nTesting affiliation block beginning with non-superscript number...")
    edited = CLEAN_ORIGINAL.replace("¹ Department of Medicine", "1 Department of Medicine")
    errors = check_affiliation_superscripts(CLEAN_ORIGINAL, edited)
    block_errors = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'non-superscript number' in e['description']]
    assert len(block_errors) > 0, "Expected error for non-superscript affiliation block"
    err = block_errors[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

def test_author_number_has_no_matching_affiliation():
    print("\nTesting author number with no matching affiliation...")
    edited = CLEAN_ORIGINAL.replace("John Smith¹,²", "John Smith¹,²,³")
    errors = check_affiliation_superscripts(CLEAN_ORIGINAL, edited)
    unmatched_errors = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'no matching affiliation block' in e['description']]
    assert len(unmatched_errors) > 0, "Expected error for author referencing nonexistent affiliation"
    err = unmatched_errors[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

def test_affiliation_number_has_no_matching_author():
    print("\nTesting affiliation block with no matching author...")
    edited = CLEAN_ORIGINAL.replace("John Smith¹,²", "John Smith¹")
    errors = check_affiliation_superscripts(CLEAN_ORIGINAL, edited)
    unmatched_affil_errors = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'no matching author referencing it' in e['description']]
    assert len(unmatched_affil_errors) > 0, "Expected error for affiliation without matching author"
    err = unmatched_affil_errors[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

def test_mapping_changed_from_original():
    print("\nTesting author-to-affiliation mapping change from approved original...")
    original_two_authors = """Title

John Smith¹ and Jane Doe²

¹ Department of Medicine, University A
² Institute of Research, University B

* Corresponding author: John Smith, john.smith@university.edu
"""
    # Swap mappings in edited: John Smith² and Jane Doe¹
    edited_swapped = """Title

John Smith² and Jane Doe¹

¹ Department of Medicine, University A
² Institute of Research, University B

* Corresponding author: John Smith, john.smith@university.edu
"""
    errors = check_affiliation_superscripts(original_two_authors, edited_swapped)
    mapping_changed = [e for e in errors if e['error_type'] == 'AFFILIATION_SUPERSCRIPT_INVALID' and 'mapping changed' in e['description']]
    assert len(mapping_changed) > 0, "Expected error for changed author-to-affiliation mapping"
    err = mapping_changed[0]
    assert err['severity'] == 'CRITICAL'
    assert err['suggested_action'] == 'Restore the correct superscript affiliation number and mapping.'
    print(f"  ✓ Caught: {err['description']}")

if __name__ == '__main__':
    print("=" * 60)
    print("STRICT VALIDATION OUTPUT POLICY & AFFILIATION TESTS")
    print("=" * 60)
    test_clean_manuscript()
    test_no_affiliation_superscript()
    test_affiliation_not_superscript_on_author()
    test_affiliation_block_not_superscript()
    test_author_number_has_no_matching_affiliation()
    test_affiliation_number_has_no_matching_author()
    test_mapping_changed_from_original()
    print("\n" + "=" * 60)
    print("ALL STRICT POLICY TESTS PASSED SUCCESSFULLY! ✓")
    print("=" * 60)

"""Quick smoke test for the critical rules engine."""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

from critical_rules import (
    run_critical_rules,
    CRITICAL_RULES_REGISTRY,
    check_author_names,
    check_affiliations,
    check_email,
    check_article_title,
    check_numbered_references,
    check_author_year_reference_order,
)

print("=" * 60)
print("CRITICAL RULES ENGINE — Smoke Test")
print("=" * 60)

# Registry check
print(f"\nRegistry contains {len(CRITICAL_RULES_REGISTRY)} critical rules:")
for key, rule in CRITICAL_RULES_REGISTRY.items():
    print(f"  [{rule['id']}] {rule['name']} — {rule['severity']} — {rule['description']}")

# Test manuscripts
ORIGINAL = """Effects of Aspirin on Cardiovascular Mortality

John Smith, Jane Doe, Robert Johnson

1 Department of Cardiology, Harvard University, Boston, MA, USA
2 Department of Medicine, Johns Hopkins Hospital, Baltimore, MD, USA

Corresponding author: john.smith@harvard.edu

Abstract
This study examines the effects of aspirin...

References
[1] Anderson J. Aspirin use in cardiology. NEJM 2020.
[2] Baker L. Platelet aggregation studies. Lancet 2021.
[3] Chen W. Cardiovascular outcomes. BMJ 2022.
"""

EDITED_CHANGED = """Effects of Asprin on Cardiovascular Mortality

John Smyth, Jane Doe, Alex Williams

1 Department of Cardiology, Stanford University, Palo Alto, CA, USA
2 Department of Medicine, Johns Hopkins Hospital, Baltimore, MD, USA

Corresponding author: john.smyth@stanford.edu

Abstract
This study examines the effects of aspirin...

References
[1] Anderson J. Aspirin use in cardiology. NEJM 2020.
[2] Baker L. Platelet aggregation studies. Lancet 2021.
[2] Chen W. Cardiovascular outcomes. BMJ 2022.
[5] Davis R. Additional reference. Nature 2023.
"""

# Simulate job sheet instructions
class MockInstruction:
    def __init__(self, section, instruction, status='Detected'):
        self.section = section
        self.instruction = instruction
        self.status = status

instructions = [
    MockInstruction('Author Name', 'Compare with original'),
    MockInstruction('Affiliation', 'Compare with original'),
    MockInstruction('Email', 'Required'),
    MockInstruction('Article Title', 'Compare with original'),
    MockInstruction('Numbered References', 'Sequential numbering'),
]

print("\n" + "=" * 60)
print("Running critical rules against test manuscripts...")
print("=" * 60)

errors, rule_ids, run_log = run_critical_rules(ORIGINAL, EDITED_CHANGED, instructions)

print(f"\nActivated rules: {rule_ids}")
print(f"Total errors found: {len(errors)}")
print()

for i, err in enumerate(errors, 1):
    print(f"Error #{i}:")
    print(f"  Code:     {err['error_type']}")
    print(f"  Severity: {err['severity']}")
    print(f"  Field:    {err.get('field', '—')}")
    print(f"  Original: {err['original_text'][:80]}")
    print(f"  Current:  {err['edited_text'][:80]}")
    print(f"  Action:   {err.get('suggested_action', '—')}")
    print(f"  Desc:     {err['description'][:100]}")
    print()

print("\nRun log:")
for entry in run_log:
    print(f"  [{entry['status']}] {entry['rule_name']} — {entry['errors_found']} error(s) — triggered by: {entry['triggered_by']}")

# Test author-year ordering (name and year check disabled, sequence alone checked)
print("\n" + "=" * 60)
print("Testing that reference author name and year are NOT checked...")
print("=" * 60)

ALPHA_TEXT = """Some manuscript text

References
Anderson J. Cardiovascular studies. 2020.
Baker L. Platelet studies. 2021.
Adams K. Earlier work. 2019.
Baker L. Follow-up. 2020.
"""

alpha_instructions = [
    MockInstruction('References', 'alphabetical order, author-year style'),
]

alpha_errors, alpha_ids, alpha_log = run_critical_rules(ORIGINAL, ALPHA_TEXT, alpha_instructions)
alpha_ref_errors = [e for e in alpha_errors if e.get('field') == 'References']
print(f"Alpha reference order errors (should be 0): {len(alpha_ref_errors)}")
assert len(alpha_ref_errors) == 0, f"Expected 0 reference errors (name/year check disabled), got {len(alpha_ref_errors)}"
print("  ✓ Reference author name and year correctly NOT checked.")

# Test reference sequence check alone
print("\n" + "=" * 60)
print("Testing reference sequence check alone (numbered sequence & citation order)...")
print("=" * 60)

SEQ_MANUSCRIPT = """Study examining treatments cited in body text [2], and then [1].

References
[1] Anderson J. Cardiovascular studies. 2020.
[2] Baker L. Platelet studies. 2021.
[4] Davis R. Additional study. 2023.
"""
seq_instructions = [
    MockInstruction('References', 'Sequential numbering'),
]
seq_errors, seq_ids, seq_log = run_critical_rules(ORIGINAL, SEQ_MANUSCRIPT, seq_instructions)
seq_ref_errors = [e for e in seq_errors if e.get('field') == 'References']
print(f"Sequence errors found: {len(seq_ref_errors)}")
for err in seq_ref_errors:
    print(f"  {err['error_type']}: {err['description'][:100]}")
# Should catch gap (missing 3) and citation order ([2] cited before [1])
assert any(e['error_type'] == 'REFERENCE_NUMBER_GAP' for e in seq_ref_errors), "Expected REFERENCE_NUMBER_GAP"
assert any(e['error_type'] == 'REFERENCE_CITATION_ORDER_INVALID' for e in seq_ref_errors), "Expected REFERENCE_CITATION_ORDER_INVALID"
print("  ✓ Reference sequence (gaps, citation order) properly validated alone.")

print("\n" + "=" * 60)
print("ALL SMOKE TESTS PASSED ✓")
print("=" * 60)

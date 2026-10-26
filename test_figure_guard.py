import os
import sys
import io
import json

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app import app, db
from models import ImageCriterion, FigureInspection, Manuscript, Project, Team, User
import image_qc

def create_synthetic_test_image(filename, width=1200, height=800, dpi=(300, 300), blur=False, color_mode='RGB'):
    """Generate a synthetic test image with crisp text/shapes or simulated blur."""
    img = Image.new(color_mode, (width, height), color=(255, 255, 255) if color_mode != 'CMYK' else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw sharp geometric patterns and high-contrast lines
    for i in range(0, width, 40):
        draw.line([(i, 0), (i, height)], fill=(0, 0, 0) if color_mode != 'CMYK' else (0, 0, 0, 255), width=3)
    for j in range(0, height, 40):
        draw.line([(0, j), (width, j)], fill=(0, 0, 0) if color_mode != 'CMYK' else (0, 0, 0, 255), width=3)
    
    if blur:
        # Apply heavy blur filter to simulate out-of-focus capture
        img = img.filter(ImageFilter.GaussianBlur(radius=8))

    os.makedirs('static/uploads/figures', exist_ok=True)
    out_path = os.path.join('static/uploads/figures', filename)
    img.save(out_path, dpi=dpi)
    return out_path

def test_image_quality_suite():
    print("============================================================")
    print("JOURNAL GUARD - FIGURE QUALITY & CRUD TEST SUITE")
    print("============================================================")

    with app.app_context():
        # 1. Test ImageCriterion CRUD Operations
        print("\n[TEST 1] Testing ImageCriterion CRUD Operations...")
        
        # CREATE
        crit = ImageCriterion(
            journal_number="JG-TEST-999",
            category="LINE_ART",
            min_dpi=600,
            recommended_dpi=1000,
            min_width_px=1800,
            min_height_px=1200,
            max_file_size_mb=25,
            allowed_color_modes="RGB,Grayscale",
            allowed_formats="TIFF,EPS,PNG",
            min_sharpness_score=150.0,
            notes="Automated test criteria",
            is_active=True
        )
        db.session.add(crit)
        db.session.commit()
        crit_id = crit.id
        print(f"  ✓ CREATE: Added criterion ID {crit_id} for JG-TEST-999 (min_dpi={crit.min_dpi})")

        # READ
        fetched = db.session.get(ImageCriterion, crit_id)
        assert fetched is not None, "Criterion should exist in database"
        assert fetched.journal_number == "JG-TEST-999"
        print(f"  ✓ READ: Successfully fetched criterion {fetched.journal_number}")

        # UPDATE
        fetched.min_dpi = 1200
        fetched.notes = "Updated notes during CRUD test"
        db.session.commit()
        updated = db.session.get(ImageCriterion, crit_id)
        assert updated.min_dpi == 1200, "min_dpi should be updated to 1200"
        print(f"  ✓ UPDATE: Successfully modified min_dpi to {updated.min_dpi}")

        # TOGGLE ACTIVE
        updated.is_active = False
        db.session.commit()
        toggled = db.session.get(ImageCriterion, crit_id)
        assert toggled.is_active is False
        print(f"  ✓ TOGGLE: Successfully toggled active status to {toggled.is_active}")

        # DELETE
        db.session.delete(toggled)
        db.session.commit()
        deleted = db.session.get(ImageCriterion, crit_id)
        assert deleted is None, "Criterion should be deleted"
        print("  ✓ DELETE: Successfully deleted test criterion")

        # Fetch active criteria
        active_criteria = ImageCriterion.query.filter_by(is_active=True).all()

        # 2. Test Image Inspection: Sharp High-DPI Image
        print("\n[TEST 2] Testing High-DPI Sharp Image Inspection...")
        sharp_path = create_synthetic_test_image("test_sharp_300dpi.png", width=2400, height=1600, dpi=(300, 300), blur=False)
        callouts_1 = image_qc.find_figure_callouts("As shown in Figure 1, the results are clear.")
        sharp_result = image_qc.inspect_figure(sharp_path, criteria_list=active_criteria, text_callouts=callouts_1, figure_label="Figure 1")
        
        print(f"  Specs: {sharp_result['pixel_width']}x{sharp_result['pixel_height']} px, DPI={sharp_result['dpi_x']}x{sharp_result['dpi_y']}")
        print(f"  Sharpness Score: {sharp_result['sharpness_score']:.1f}, Is Blurry: {sharp_result['is_blurry']}")
        print(f"  Status: {sharp_result['status']}, Issues: {len(sharp_result['issues'])}")
        assert sharp_result['dpi_x'] >= 300, "DPI should be 300+"
        assert not sharp_result['is_blurry'], "Sharp image should not be blurry"
        assert sharp_result['status'] == 'PASS', "Crisp 300 DPI image should pass"
        print("  ✓ PASS: High-DPI sharp image passed all criteria.")

        # 3. Test Image Inspection: Low DPI Image (e.g. 72 DPI web graphic)
        print("\n[TEST 3] Testing Low DPI Image Detection (72 DPI)...")
        low_dpi_path = create_synthetic_test_image("test_low_72dpi.jpg", width=800, height=600, dpi=(72, 72), blur=False)
        callouts_2 = image_qc.find_figure_callouts("Refer to Fig. 2.")
        low_result = image_qc.inspect_figure(low_dpi_path, criteria_list=active_criteria, text_callouts=callouts_2, figure_label="Figure 2")
        
        print(f"  DPI: {low_result['dpi_x']}x{low_result['dpi_y']}, Status: {low_result['status']}")
        dpi_issues = [i for i in low_result['issues'] if i['field'] == 'dpi']
        assert len(dpi_issues) > 0, "Low DPI issue must be reported"
        assert low_result['status'] in ('WARNING', 'FAIL'), "Low DPI should trigger warning or fail"
        print(f"  ✓ CAUGHT: Low DPI correctly flagged: '{dpi_issues[0]['message']}'")

        # 4. Test Image Inspection: Blurry / Out-of-Focus Image
        print("\n[TEST 4] Testing Blurred Image Detection (Laplacian Variance)...")
        blur_path = create_synthetic_test_image("test_blurry.jpg", width=1200, height=800, dpi=(300, 300), blur=True)
        callouts_3 = image_qc.find_figure_callouts("See Figure 3.")
        blur_result = image_qc.inspect_figure(blur_path, criteria_list=active_criteria, text_callouts=callouts_3, figure_label="Figure 3")
        
        print(f"  Sharpness Score: {blur_result['sharpness_score']:.1f}, Is Blurry: {blur_result['is_blurry']}")
        blur_issues = [i for i in blur_result['issues'] if i['field'] == 'sharpness']
        assert blur_result['is_blurry'], "Gaussian blurred image must be flagged as blurry"
        assert len(blur_issues) > 0, "Sharpness issue must be recorded"
        print(f"  ✓ CAUGHT: Blur correctly detected: '{blur_issues[0]['message']}'")

        # 5. Test Citation Cross-referencing
        print("\n[TEST 5] Testing Manuscript Citation Cross-Referencing...")
        callouts_99 = image_qc.find_figure_callouts("Only mentions Figure 1 and Table 2.")
        uncited_result = image_qc.inspect_figure(sharp_path, criteria_list=active_criteria, text_callouts=callouts_99, figure_label="Figure 99")
        citation_issues = [i for i in uncited_result['issues'] if i['field'] == 'citation']
        assert len(citation_issues) > 0, "Uncited figure must produce citation warning"
        print(f"  ✓ CAUGHT: Uncited figure flagged: '{citation_issues[0]['message']}'")

        # 6. Test FigureInspection Model and Override
        print("\n[TEST 6] Testing FigureInspection Record & Override Workflow...")
        # Get or create a sample manuscript
        ms = Manuscript.query.first()
        if ms:
            fig_rec = FigureInspection(
                manuscript_id=ms.id,
                figure_label="Figure 3",
                original_filename="test_blurry.jpg",
                file_path=blur_path,
                thumbnail_path=blur_path,
                format="JPEG",
                color_mode="RGB",
                dpi_x=300,
                dpi_y=300,
                pixel_width=1200,
                pixel_height=800,
                print_width_inches=4.0,
                print_height_inches=2.67,
                file_size_kb=85,
                sharpness_score=blur_result['sharpness_score'],
                is_blurry=True,
                status="WARNING",
                issues_json=json.dumps(blur_result['issues'])
            )
            db.session.add(fig_rec)
            db.session.commit()
            fig_id = fig_rec.id

            # Apply Override
            fig_rec.status = 'OVERRIDDEN'
            fig_rec.override_reason = "Author confirmed archival micrograph; original cannot be re-acquired."
            fig_rec.reviewed_by = "Lead Production Editor"
            db.session.commit()

            reloaded = db.session.get(FigureInspection, fig_id)
            assert reloaded.status == 'OVERRIDDEN'
            assert "micrograph" in reloaded.override_reason
            print(f"  ✓ OVERRIDE: Figure {reloaded.figure_label} overridden with recorded audit note.")

            # Cleanup test figure record
            db.session.delete(reloaded)
            db.session.commit()
        else:
            print("  (Skipped DB manuscript link - no manuscripts currently in test database)")

        # Cleanup test files
        for f in [sharp_path, low_dpi_path, blur_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        print("\n============================================================")
        print("ALL FIGURE GUARD TESTS PASSED SUCCESSFULLY! (100% CLEAN)")
        print("============================================================")

if __name__ == '__main__':
    test_image_quality_suite()

import re

with open('templates/review.html', 'r', encoding='utf-8') as f:
    html = f.read()

drawer_html_old = '''            <!-- Detail Drawer -->
            <div class="fv-detail-drawer" id="detailDrawer">
                <div class="fv-detail-inner">
                    <div style="flex:1;min-width:0">
                        <div class="fv-detail-desc" id="drawerDesc"></div>
                        <div class="fv-detail-diff">
                            <div class="fv-diff-block fv-diff-original">
                                <div class="fv-diff-label">? Expected (Original)</div>
                                <div class="fv-diff-content" id="drawerOriginal"></div>
                            </div>
                            <div class="fv-diff-block fv-diff-edited">
                                <div class="fv-diff-label">? Found (Current)</div>
                                <div class="fv-diff-content" id="drawerEdited"></div>
                            </div>
                        </div>
                    </div>
                    <div class="fv-detail-actions" id="drawerActions"></div>
                </div>
            </div>'''

drawer_html_new = '''            <!-- Comparison Action Bar -->
            <div class="fv-detail-drawer" id="detailDrawer" style="padding: 10px 16px; border-bottom: 1px solid var(--fv-border); align-items: center; flex-direction: row; justify-content: space-between; gap: 16px;">
                <div style="display: flex; gap: 16px; flex: 1; min-width: 0; font-size: 13px; font-family: monospace;">
                    <div style="color: var(--fv-success); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;" title="Original"><strong style="color:var(--fv-text);">Original:</strong> <span id="drawerOriginal"></span></div>
                    <div style="color: var(--fv-error); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;" title="Current"><strong style="color:var(--fv-text);">Current:</strong> <span id="drawerEdited"></span></div>
                </div>
                <div class="fv-detail-actions" id="drawerActions" style="display: flex; flex-direction: row; gap: 8px;"></div>
            </div>'''

html = html.replace(drawer_html_old, drawer_html_new)

# Update .fv-detail-drawer.active in CSS
css_old = '.fv-detail-drawer.active { display: block; }'
css_new = '.fv-detail-drawer.active { display: flex !important; }'
html = html.replace(css_old, css_new)

with open('templates/review.html', 'w', encoding='utf-8') as f:
    f.write(html)

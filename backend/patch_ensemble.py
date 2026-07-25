path = r'c:\Users\parth\OneDrive\Desktop\OceanHub\backend\master_agent.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

old = '    # 2. A/B Ensemble probability blending (50/50 weighted blend between Model A & Model B)\n    if ml_score is not None and ml_score_b is not None:\n        prob_a = ml_score.get("probabilities", {})\n        prob_b = ml_score_b.get("probabilities", {})\n        blended = ensemble_probabilities(prob_a, prob_b, weight_a=0.5, weight_b=0.5)'

new = '''    # 2. A/B Ensemble probability blending — BUG-22 FIX: weighted by CV accuracy (not fixed 50/50)
    if ml_score is not None and ml_score_b is not None:
        prob_a = ml_score.get("probabilities", {})
        prob_b = ml_score_b.get("probabilities", {})
        try:
            from ml_brain import get_brain as _gb22
            _brain22 = _gb22()
            cv_a = _brain22.cv_score.get(f"{symbol}_A", 0.5)
            cv_b = _brain22.cv_score.get(f"{symbol}_B", 0.5)
            total_cv = cv_a + cv_b
            w_a = (cv_a / total_cv) if total_cv > 0 else 0.5
            w_b = (cv_b / total_cv) if total_cv > 0 else 0.5
        except Exception:
            w_a, w_b = 0.5, 0.5
        blended = ensemble_probabilities(prob_a, prob_b, weight_a=w_a, weight_b=w_b)'''

if old in content:
    content = content.replace(old, new, 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('BUG-22 CV weighting applied OK')
else:
    print('Pattern NOT FOUND')
    idx = content.find('50/50 weighted blend')
    print(repr(content[max(0,idx-50):idx+250]))

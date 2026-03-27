# app/appetite_scoring.py
"""
Premium Finance Appetite Scoring Engine

Calculates PF appetite score (0-100) based on:
- Premium size (40 points)
- Down payment percentage (30 points)
- State risk (30 points)
"""

import json
from app.database import Database
from app.models import AppetiteRule


def get_rules_from_db():
    """Fetch enabled appetite rules from database"""
    db = Database()
    session = db.get_session()
    try:
        rules = {}
        max_scores = {}
        db_rules = session.query(AppetiteRule).filter_by(enabled=True).all()

        for rule in db_rules:
            rules[rule.rule_type] = json.loads(rule.rule_data)
            max_scores[rule.rule_type] = rule.max_score

        return rules, max_scores
    finally:
        session.close()


def calculate_appetite_score(submission_data, quotes_data):
    """
    Calculate PF appetite score for a submission.

    Args:
        submission_data: dict with submission info (state, etc.)
        quotes_data: list of quote dicts with extracted_json

    Returns:
        dict with score and breakdown
    """
    rules, max_scores = get_rules_from_db()
    score_breakdown = {}
    total_score = 0
    
    # Extract financial data from quotes
    total_premium = 0
    total_down_payment = 0
    
    for quote in quotes_data:
        if quote.get('extracted_json'):
            try:
                data = json.loads(quote['extracted_json'])
                
                # Get grand total or total premium
                if data.get('totals', {}).get('grand_total'):
                    total_premium += data['totals']['grand_total']
                elif data.get('totals', {}).get('total_premium'):
                    total_premium += data['totals']['total_premium']
                
                # Get down payment
                if data.get('financing', {}).get('down_payment'):
                    total_down_payment += data['financing']['down_payment']
                    
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    
    # 1. Premium Size Scoring (only if enabled)
    premium_score = 0
    premium_label = 'Unknown'

    if 'premium_size' in rules and total_premium > 0:
        for range_def in rules['premium_size']['ranges']:
            if range_def['min'] <= total_premium < range_def['max']:
                premium_score = range_def['score']
                premium_label = range_def['label']
                break

    # Always include premium_size in breakdown, even if 0/null
    if 'premium_size' in max_scores:
        score_breakdown['premium_size'] = {
            'score': premium_score,
            'max': max_scores['premium_size'],
            'value': total_premium,
            'label': premium_label
        }
        total_score += premium_score

    # 2. Down Payment Percentage Scoring (only if enabled)
    down_payment_score = 0
    down_payment_pct = 0
    down_payment_label = 'No Down Payment'

    if 'down_payment_pct' in rules and total_premium > 0 and total_down_payment > 0:
        down_payment_pct = (total_down_payment / total_premium) * 100

        for range_def in rules['down_payment_pct']['ranges']:
            if range_def['min'] <= down_payment_pct < range_def['max']:
                down_payment_score = range_def['score']
                down_payment_label = range_def['label']
                break

    if 'down_payment_pct' in max_scores:
        score_breakdown['down_payment'] = {
            'score': down_payment_score,
            'max': max_scores['down_payment_pct'],
            'percentage': round(down_payment_pct, 1),
            'value': total_down_payment,
            'label': down_payment_label
        }
        total_score += down_payment_score

    # 3. State Risk Scoring (only if enabled)
    if 'state_risk' in rules:
        state_score = rules['state_risk']['default']['score']
        state_label = 'Unknown State'
        state = submission_data.get('state', '').upper()

        if state:
            for risk_level, risk_data in rules['state_risk'].items():
                if risk_level == 'default':
                    continue
                if state in risk_data.get('states', []):
                    state_score = risk_data['score']
                    state_label = risk_level.replace('_', ' ').title()
                    break

        if 'state_risk' in max_scores:
            score_breakdown['state_risk'] = {
                'score': state_score,
                'max': max_scores['state_risk'],
                'state': state,
                'label': state_label
            }
            total_score += state_score
    
    # Determine overall rating
    if total_score >= 80:
        rating = 'Excellent'
        rating_color = 'green'
    elif total_score >= 60:
        rating = 'Good'
        rating_color = 'blue'
    elif total_score >= 40:
        rating = 'Fair'
        rating_color = 'yellow'
    else:
        rating = 'Poor'
        rating_color = 'red'
    
    return {
        'total_score': total_score,
        'max_score': 100,
        'rating': rating,
        'rating_color': rating_color,
        'breakdown': score_breakdown,
        'total_premium': total_premium,
        'total_down_payment': total_down_payment
    }


def get_score_label(score):
    """Get a simple label for a score"""
    if score >= 80:
        return 'Excellent'
    elif score >= 60:
        return 'Good'
    elif score >= 40:
        return 'Fair'
    else:
        return 'Poor'


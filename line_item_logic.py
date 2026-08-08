"""
Line item calculation logic for split labor/material cost structure.
"""

def calc_component(cost_per_unit, quantity, markup_pct, min_markup, can_override):
    """Calculate price and margin for a single cost component."""
    if not can_override:
        markup_pct = max(markup_pct, min_markup)
    total_cost = round(quantity * cost_per_unit, 2)
    total_price = round(total_cost * (1 + markup_pct / 100), 2)
    margin_pct = round((markup_pct / (100 + markup_pct)) * 100, 1) if markup_pct else 0
    return total_cost, total_price, margin_pct

def build_line_item(data, wt, sub_name, product_label, material_label, can_override, quote):
    """Build a complete line item dict from form/json data."""
    work_type_id = data.get('work_type_id')
    cost_structure = wt['cost_structure'] if wt else 'labor_only'
    quantity = float(data.get('quantity', 0) or 0)
    labor_unit = data.get('unit') or (wt['unit'] if wt else '')
    labor_min = float(wt['min_markup'] if wt else 10.0)

    # Labor
    labor_cpu = float(data.get('labor_cost_per_unit', 0) or 0)
    labor_markup = float(data.get('labor_markup_pct', wt['default_markup'] if wt else 30) or 30)
    l_cost, l_price, l_margin = calc_component(labor_cpu, quantity, labor_markup, labor_min, can_override)

    # Material
    mat_cpu = float(data.get('material_cost_per_unit', 0) or 0)
    mat_markup = float(data.get('material_markup_pct', wt['default_markup'] if wt else 30) or 30)
    mat_min = float(data.get('material_min_markup', 10) or 10)
    mat_qty = float(data.get('material_quantity', quantity) or quantity)  # may differ due to waste
    m_cost, m_price, m_margin = calc_component(mat_cpu, mat_qty, mat_markup, mat_min, can_override)

    total_cost = round(l_cost + m_cost, 2)
    total_price = round(l_price + m_price, 2)
    total_margin = round(((total_price - total_cost) / total_price * 100) if total_price else 0, 1)

    description = data.get('description')
    if description is None:
        description = wt.get('description', '') if wt else ''

    return {
        'work_type_id': work_type_id,
        'work_type_label': wt['work_type'] if wt else '',
        'cost_structure': cost_structure,
        'description': description,
        'sub_id': data.get('sub_id', ''),
        'sub_name': sub_name,
        'labor_quantity': quantity,
        'labor_unit': labor_unit,
        'labor_cost_per_unit': labor_cpu,
        'labor_total_cost': l_cost,
        'labor_markup_pct': labor_markup,
        'labor_margin_pct': l_margin,
        'labor_total_price': l_price,
        'labor_min_markup': labor_min,
        'material_id': data.get('material_id'),
        'material_label': material_label,
        'material_quantity': mat_qty,
        'material_unit': labor_unit,
        'material_cost_per_unit': mat_cpu,
        'material_total_cost': m_cost,
        'material_markup_pct': mat_markup,
        'material_margin_pct': m_margin,
        'material_total_price': m_price,
        'material_min_markup': mat_min,
        'product_id': data.get('product_id'),
        'product_label': product_label,
        'total_cost': total_cost,
        'total_price': total_price,
        'total_margin_pct': total_margin,
    }

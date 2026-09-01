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

def price_component(cost_per_unit, quantity, markup_pct, min_markup, can_override, is_passthrough):
    """The one place that knows how pass-through affects a component's markup and floor --
    every call site that prices a line item's labor or material component should call this
    instead of calc_component directly. Pass-through items are billed at exactly cost, zero
    margin: markup and its floor both get zeroed here, and can_override is forced True so
    calc_component's "not can_override -> floor to min_markup" branch can't reactivate --
    setting can_override=False alone (without also zeroing min_markup) ACTIVATES that floor
    rather than preventing it, which caused three real pricing bugs before this existed.

    Returns (total_cost, total_price, margin_pct, markup_pct, min_markup) -- the resolved
    markup_pct/min_markup are included so a caller storing them on the line item doesn't
    need its own parallel "0.0 if is_passthrough else ..." branch to figure out what to
    persist; it's the same values price_component actually priced with."""
    if is_passthrough:
        markup_pct, min_markup, can_override = 0.0, 0.0, True
    total_cost, total_price, margin_pct = calc_component(cost_per_unit, quantity, markup_pct, min_markup, can_override)
    return total_cost, total_price, margin_pct, markup_pct, min_markup

def build_line_item(data, wt, sub_name, product_label, material_label, can_override, quote):
    """Build a complete line item dict from form/json data."""
    work_type_id = data.get('work_type_id')
    cost_structure = wt['cost_structure'] if wt else 'labor_only'
    quantity = float(data.get('quantity', 0) or 0)
    labor_unit = data.get('unit') or (wt['unit'] if wt else '')
    # Pass-through work types (Electrician, Deck Stabilization, ...) are billed through at
    # exactly what the sub charges -- zero margin, and LOCKED, not just defaulted: this
    # overrides whatever markup a caller passed in. The floor itself (min_markup) has to be
    # zeroed too, not just the markup% -- passing can_override=False into calc_component
    # ACTIVATES its "markup_pct = max(markup_pct, min_markup)" floor rather than preventing
    # it, so a nonzero min_markup (the work type's own, or a stale/contaminated one already
    # stored on this line item) would silently re-inflate the just-zeroed markup right back
    # up. Zeroing min_markup here makes that floor a no-op regardless of can_override.
    is_passthrough = bool(wt and wt['is_passthrough'] == 'Y')
    labor_min = 0.0 if is_passthrough else float(wt['min_markup'] if wt else 10.0)

    # Labor
    labor_cpu = float(data.get('labor_cost_per_unit', 0) or 0)
    if is_passthrough:
        labor_markup_raw = 0
    else:
        # `or 30` here would be wrong -- a work type whose real default_markup is 0 needs
        # that 0 to stick, not get silently bumped back up to 30% because 0 is falsy.
        # None/missing is the only case that should fall back to a default.
        labor_markup_raw = data.get('labor_markup_pct')
        if labor_markup_raw is None or labor_markup_raw == '':
            labor_markup_raw = wt['default_markup'] if wt else 30
    labor_markup = float(labor_markup_raw)
    l_cost, l_price, l_margin = calc_component(labor_cpu, quantity, labor_markup, labor_min, can_override and not is_passthrough)

    # Material
    mat_cpu = float(data.get('material_cost_per_unit', 0) or 0)
    if is_passthrough:
        mat_markup_raw = 0
    else:
        mat_markup_raw = data.get('material_markup_pct')
        if mat_markup_raw is None or mat_markup_raw == '':
            mat_markup_raw = wt['default_markup'] if wt else 30
    mat_markup = float(mat_markup_raw)
    mat_min = 0.0 if is_passthrough else float(data.get('material_min_markup', 10) or 10)
    mat_qty = float(data.get('material_quantity', quantity) or quantity)  # may differ due to waste
    m_cost, m_price, m_margin = calc_component(mat_cpu, mat_qty, mat_markup, mat_min, can_override and not is_passthrough)

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
        'is_passthrough': 1 if is_passthrough else 0,
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

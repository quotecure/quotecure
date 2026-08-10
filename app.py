import json
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, g
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from database import init_db, init_auth, init_materials, init_company_settings, generate_payment_schedule, run_migrations, init_pebble_pros_surfaces, init_skimmer_material, get_db
from line_item_logic import build_line_item, calc_component
import hashlib

app = Flask(__name__)

# Local secrets folder (used only for local-dev file fallbacks — production sets SECRET_KEY via env var)
_secrets_dir = Path.home() / 'Documents' / 'quotecure_data'

def _load_secret_key():
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    _secrets_dir.mkdir(parents=True, exist_ok=True)
    key_path = _secrets_dir / '.secret_key'
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    key = os.urandom(32).hex()
    with open(key_path, 'w') as f:
        f.write(key)
    return key

app.secret_key = _load_secret_key()

def _load_gemini_key():
    env_key = os.environ.get('GEMINI_API_KEY')
    if env_key:
        return env_key
    key_path = _secrets_dir / '.gemini_key'
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    return None

def hash_pw(p):
    """New password hashes use werkzeug's salted PBKDF2. check_pw() below still
    accepts the old unsalted-SHA256 hashes and transparently upgrades them on login."""
    return generate_password_hash(p)

def check_pw(password, stored_hash):
    if stored_hash.startswith('pbkdf2:') or stored_hash.startswith('scrypt:'):
        return check_password_hash(stored_hash, password)
    # Legacy unsalted SHA-256 hash from before the werkzeug migration
    return hashlib.sha256(password.encode()).hexdigest() == stored_hash

# ── Init ──────────────────────────────────────────────────────────────────────
_initialized = False

@app.before_request
def setup():
    global _initialized
    if not _initialized:
        init_db()
        db = get_db()
        run_migrations(db)
        init_auth(db)
        init_materials(db)
        init_company_settings(db)
        init_pebble_pros_surfaces(db)
        db.close()
        _initialized = True
    # Load current user into g
    g.user = None
    g.role = None
    if 'user_id' in session:
        db = get_db()
        user = db.execute("SELECT u.*, r.* FROM users u JOIN roles r ON u.role_id=r.role_id WHERE u.user_id=?",
                          (session['user_id'],)).fetchone()
        if user:
            g.user = user
            g.role = user

# ── Auth decorator ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def require_permission(perm):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not g.user:
                return redirect(url_for('login'))
            if not g.role[perm]:
                return render_template('denied.html'), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ── Login / Logout ────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        db = get_db()
        candidate = db.execute("SELECT * FROM users WHERE username=? AND active=1",
                               (request.form['username'],)).fetchone()
        user = candidate if candidate and check_pw(request.form['password'], candidate['password']) else None
        if user:
            if not user['password'].startswith(('pbkdf2:', 'scrypt:')):
                db.execute("UPDATE users SET password=? WHERE user_id=?",
                           (hash_pw(request.form['password']), user['user_id']))
                db.commit()
            session['user_id'] = user['user_id']
            return redirect(url_for('quotes_list'))
        error = 'Invalid username or password'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Home ──────────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    return redirect(url_for('quotes_list'))

# ══════════════════════════════════════════════════════════════════════════════
# QUOTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/quotes')
@login_required
def quotes_list():
    db = get_db()
    quotes = db.execute("SELECT * FROM quotes ORDER BY created_at DESC").fetchall()
    commission_policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    return render_template('quotes.html', quotes=quotes, commission_policy=commission_policy)

def _dims_totals(dims):
    """Shared lf/sqft totals from a dims dict (works for both real quotes.Row and plain dicts)."""
    total_lf = ((dims.get('pool_perimeter') or 0) + (dims.get('spa_perimeter') if dims.get('has_spa') else 0)
                + (dims.get('steps_lf') or 0) + (dims.get('benches_lf') or 0) + (dims.get('swimouts_lf') or 0))
    total_sqft = dims.get('total_surface_sqft') or dims.get('pool_sqft') or 0
    return total_lf, total_sqft

def _compute_line_item_pricing(db, wt, default_sub_id, default_material_id, dims, can_override):
    """Full pricing for one work-type item given a sub/material default and quote dimensions.
    wt: dict with work_type_id, work_type, unit, cost_structure, default_markup, min_markup, description.
    dims: dict with pool_perimeter, has_spa, spa_perimeter, steps_lf, benches_lf, swimouts_lf,
          total_surface_sqft (or pool_sqft), has_shelf.
    Returns a dict with every quote_line_items column this produces."""
    wt_id = wt['work_type_id']
    sub_name = ''
    labor_cpu = 0.0
    product_id = None
    product_label = ''
    material_id = None
    material_label = ''
    rate_row = None

    if default_sub_id:
        s = db.execute("SELECT name FROM subs WHERE sub_id=?", (default_sub_id,)).fetchone()
        if not s:
            s = db.execute("SELECT name FROM surface_applicators WHERE sub_id=?", (default_sub_id,)).fetchone()
        if s:
            sub_name = s['name']
        rate_row = db.execute(
            "SELECT rate, unit FROM sub_rates WHERE sub_id=? AND work_type_id=?",
            (default_sub_id, wt_id)
        ).fetchone()
        if rate_row:
            labor_cpu = float(rate_row['rate'])

    wt_unit = wt.get('unit', 'each')
    unit = rate_row['unit'] if rate_row else wt_unit
    total_lf, total_sqft = _dims_totals(dims)
    if unit == 'sqft':
        qty = total_sqft
    elif unit == 'lf':
        qty = total_lf
    else:
        qty = 1.0

    mat_cpu = 0.0
    mat_qty = qty

    if wt_id == 1 and default_material_id and default_sub_id:
        sar = db.execute(
            "SELECT sar.rate, sp.finish, sm.manufacturer_name, sp.product_line "
            "FROM surface_applicator_rates sar "
            "JOIN surface_products sp ON sar.product_id=sp.product_id "
            "JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id "
            "WHERE sar.sub_id=? AND sar.product_id=?",
            (default_sub_id, default_material_id)
        ).fetchone()
        if sar:
            labor_cpu = float(sar['rate'])
            product_id = default_material_id
            product_label = f"{sar['manufacturer_name']} {sar['product_line']} – {sar['finish']}"
    elif default_material_id and wt_id != 1:
        mat_row = db.execute(
            "SELECT cost_per_quote_unit, series, supplier_id FROM materials WHERE material_id=?",
            (default_material_id,)
        ).fetchone()
        if mat_row:
            mat_cpu = float(mat_row['cost_per_quote_unit'])
            sup = db.execute("SELECT name FROM suppliers WHERE supplier_id=?", (mat_row['supplier_id'],)).fetchone()
            material_id = default_material_id
            material_label = f"{sup['name'] if sup else ''} — {mat_row['series']}"

    wt_label = wt.get('work_type', '')
    if wt_label in ['Surface Application', 'Surface Removal']:
        parts = ['Pool']
        if dims.get('has_spa'):
            parts.append('Spa')
        if dims.get('has_shelf'):
            parts.append('Sunshelf')
        if len(parts) > 1:
            wt_label = wt_label + ' — ' + ' & '.join(parts)

    markup = float(wt.get('default_markup', 30))
    min_markup = float(wt.get('min_markup', 10))
    l_cost, l_price, l_margin = calc_component(labor_cpu, qty, markup, min_markup, can_override)
    m_cost, m_price, m_margin = calc_component(mat_cpu, mat_qty, markup, 10, can_override)
    total_cost = round(l_cost + m_cost, 2)
    total_price = round(l_price + m_price, 2)
    total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)

    return {
        'work_type_id': wt_id, 'work_type_label': wt_label, 'cost_structure': wt.get('cost_structure', 'labor_only'),
        'sub_id': default_sub_id or '', 'sub_name': sub_name,
        'qty': qty, 'unit': unit,
        'labor_cpu': labor_cpu, 'l_cost': l_cost, 'markup': markup, 'l_margin': l_margin, 'l_price': l_price, 'min_markup': min_markup,
        'material_id': material_id, 'material_label': material_label, 'mat_qty': mat_qty,
        'mat_cpu': mat_cpu, 'm_cost': m_cost, 'm_margin': m_margin, 'm_price': m_price,
        'product_id': product_id, 'product_label': product_label,
        'total_cost': total_cost, 'total_price': total_price, 'total_margin': total_margin,
        'description': wt.get('description', ''),
    }

def _insert_line_item_from_pricing(db, qid, p, sort_order, is_optional=0):
    db.execute(
        "INSERT INTO quote_line_items "
        "(quote_id, work_type_id, work_type_label, cost_structure, "
        "sub_id, sub_name, labor_quantity, labor_unit, "
        "labor_cost_per_unit, labor_total_cost, labor_markup_pct, labor_margin_pct, labor_total_price, labor_min_markup, "
        "material_id, material_label, material_quantity, material_unit, "
        "material_cost_per_unit, material_total_cost, material_markup_pct, material_margin_pct, material_total_price, material_min_markup, "
        "product_id, product_label, total_cost, total_price, total_margin_pct, sort_order, description, is_optional) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, p['work_type_id'], p['work_type_label'], p['cost_structure'],
         p['sub_id'], p['sub_name'], p['qty'], p['unit'],
         p['labor_cpu'], p['l_cost'], p['markup'], p['l_margin'], p['l_price'], p['min_markup'],
         p['material_id'], p['material_label'], p['mat_qty'], p['unit'],
         p['mat_cpu'], p['m_cost'], p['markup'], p['m_margin'], p['m_price'], 10,
         p['product_id'], p['product_label'],
         p['total_cost'], p['total_price'], p['total_margin'], sort_order, p['description'], is_optional)
    )

def _eligible_tiles_for_package(db, package_id):
    """Waterline tile priced above the previous tier's threshold (by sort_order), at or
    below this tier's own threshold -- unbounded above if this tier has no threshold set."""
    pkg = db.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
    if not pkg:
        return []
    prev_threshold = db.execute(
        "SELECT MAX(tile_price_threshold) FROM packages WHERE sort_order < ?", (pkg['sort_order'],)
    ).fetchone()[0]
    upper = pkg['tile_price_threshold']
    tile_query = ("SELECT material_id, category, series, cost_per_quote_unit, product_url "
                   "FROM materials WHERE work_type_id=4 AND active='Y'")
    params = []
    if prev_threshold is not None:
        tile_query += " AND cost_per_quote_unit > ?"
        params.append(prev_threshold)
    if upper is not None:
        tile_query += " AND cost_per_quote_unit <= ?"
        params.append(upper)
    tile_query += " ORDER BY category, series"
    return db.execute(tile_query, tuple(params)).fetchall()

@app.route('/api/estimate_packages', methods=['POST'])
@login_required
def estimate_packages():
    db = get_db()
    data = request.json or {}
    dims = {
        'pool_perimeter': float(data.get('pool_perimeter', 0) or 0),
        'has_spa': bool(data.get('has_spa')),
        'spa_perimeter': float(data.get('spa_perimeter', 0) or 0),
        'has_shelf': bool(data.get('has_shelf')),
        'steps_lf': float(data.get('steps_lf', 0) or 0),
        'benches_lf': float(data.get('benches_lf', 0) or 0),
        'swimouts_lf': float(data.get('swimouts_lf', 0) or 0),
        'total_surface_sqft': float(data.get('total_surface_sqft', 0) or 0),
    }
    packages = db.execute("SELECT * FROM packages WHERE active='Y' ORDER BY sort_order").fetchall()
    results = {}
    for pkg in packages:
        items = db.execute(
            "SELECT pi.*, wt.work_type, wt.unit, wt.cost_structure, wt.default_markup, wt.min_markup, wt.description "
            "FROM package_items pi JOIN work_types wt ON pi.work_type_id=wt.work_type_id "
            "WHERE pi.package_id=? AND pi.active='Y' AND pi.is_optional=0", (pkg['package_id'],)
        ).fetchall()
        total = 0.0
        for item in items:
            p = _compute_line_item_pricing(db, dict(item), item['default_sub_id'], item['default_material_id'], dims, can_override=False)
            total += p['total_price']
        results[pkg['package_id']] = round(total, 2)
    return jsonify(results)

@app.route('/quotes/new', methods=['GET','POST'])
@login_required
def new_quote():
    if request.method == 'POST':
        db = get_db()
        db.execute("""INSERT INTO quotes (customer_name,address,salesperson,
                      pool_perimeter,pool_shallow,pool_deep,pool_sqft,
                      has_spa,spa_perimeter,spa_depth,spa_sqft,
                      has_shelf,shelf_sqft,total_surface_sqft,
                      steps_lf,benches_lf,swimouts_lf,customer_email,water_surface_sqft)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (request.form['customer_name'], request.form.get('address',''),
                    request.form.get('salesperson',''),
                    float(request.form.get('pool_perimeter',0) or 0),
                    float(request.form.get('pool_shallow',0) or 0),
                    float(request.form.get('pool_deep',0) or 0),
                    float(request.form.get('pool_sqft',0) or 0),
                    1 if request.form.get('has_spa') else 0,
                    float(request.form.get('spa_perimeter',0) or 0),
                    float(request.form.get('spa_depth',0) or 0),
                    float(request.form.get('spa_sqft',0) or 0),
                    1 if request.form.get('has_shelf') else 0,
                    float(request.form.get('shelf_sqft_calc',0) or 0),
                    float(request.form.get('total_surface_sqft',0) or 0),
                    float(request.form.get('steps_lf',0) or 0),
                    float(request.form.get('benches_lf',0) or 0),
                    float(request.form.get('swimouts_lf',0) or 0),
                    request.form.get('customer_email','').strip(),
                    float(request.form.get('water_surface_sqft',0) or 0)))
        db.commit()
        qid = db.execute("SELECT lastval()").fetchone()[0]

        # Auto-populate line items — from a selected package, or the default quote template
        quote = dict(db.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone())
        package_id = request.form.get('package_id')
        if package_id:
            items = db.execute(
                "SELECT pi.default_sub_id, pi.default_material_id, pi.sort_order, pi.is_optional, "
                "wt.work_type_id, wt.work_type, wt.unit, wt.cost_structure, wt.default_markup, wt.min_markup, wt.description "
                "FROM package_items pi JOIN work_types wt ON pi.work_type_id=wt.work_type_id "
                "WHERE pi.package_id=? AND pi.active='Y' ORDER BY pi.sort_order", (package_id,)
            ).fetchall()
        else:
            items = db.execute(
                "SELECT wt.default_sub_id, wt.default_material_id, qt.sort_order, 0 as is_optional, "
                "wt.work_type_id, wt.work_type, wt.unit, wt.cost_structure, wt.default_markup, wt.min_markup, wt.description "
                "FROM quote_template qt JOIN work_types wt ON qt.work_type_id=wt.work_type_id "
                "WHERE qt.active='Y' ORDER BY qt.sort_order"
            ).fetchall()

        can_override = bool(g.role and g.role['can_override_min_markup'])

        for i, item in enumerate(items):
            wt = dict(item)
            p = _compute_line_item_pricing(db, wt, wt['default_sub_id'], wt['default_material_id'], quote, can_override)
            _insert_line_item_from_pricing(db, qid, p, i, is_optional=1 if wt['is_optional'] else 0)
        db.commit()
        _recalc_quote(db, qid)
        if package_id:
            return redirect(url_for('select_materials', quote_id=qid, package_id=package_id))
        return redirect(url_for('edit_quote', quote_id=qid))
    salesperson_name = g.user['display_name'] if g.user and g.user['display_name'] else ''
    packages = get_db().execute("SELECT * FROM packages WHERE active='Y' ORDER BY sort_order").fetchall()
    return render_template('new_quote.html', salesperson_name=salesperson_name, packages=packages)

@app.route('/quotes/<int:quote_id>/select_materials')
@login_required
def select_materials(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return redirect(url_for('quotes_list'))
    package_id = request.args.get('package_id')
    package = db.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone() if package_id else None
    tile_item = db.execute(
        "SELECT * FROM quote_line_items WHERE quote_id=? AND work_type_id=4", (quote_id,)
    ).fetchone()
    eligible_tiles = _eligible_tiles_for_package(db, package_id) if package_id else []
    return render_template('select_materials.html', quote=quote, package=package,
                           tile_item=tile_item, eligible_tiles=eligible_tiles)

@app.route('/quotes/<int:quote_id>/select_materials/tile', methods=['POST'])
@login_required
def select_material_tile(quote_id):
    db = get_db()
    item_id = request.form.get('item_id')
    material_id = request.form.get('material_id')
    row = db.execute("SELECT * FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return redirect(url_for('select_materials', quote_id=quote_id))
    m = db.execute("SELECT series, category FROM materials WHERE material_id=?", (material_id,)).fetchone()
    if m:
        label = f"{m['category']} — {m['series']}"
        can_override = bool(g.role and g.role['can_override_min_markup'])
        mat_row = db.execute("SELECT cost_per_quote_unit FROM materials WHERE material_id=?", (material_id,)).fetchone()
        mat_cpu = float(mat_row['cost_per_quote_unit']) if mat_row else 0.0
        qty = float(row['labor_quantity'])
        markup = float(row['labor_markup_pct'])
        min_m = float(row['material_min_markup'] or 10)
        m_cost, m_price, m_margin = calc_component(mat_cpu, qty, markup, min_m, can_override)
        l_cost = float(row['labor_total_cost'])
        l_price = float(row['labor_total_price'])
        total_cost = round(l_cost + m_cost, 2)
        total_price = round(l_price + m_price, 2)
        total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
        db.execute("""UPDATE quote_line_items SET
                      material_id=?, material_label=?, material_cost_per_unit=?,
                      material_total_cost=?, material_total_price=?, material_margin_pct=?,
                      total_cost=?, total_price=?, total_margin_pct=? WHERE id=?""",
                   (material_id, label, mat_cpu, m_cost, m_price, m_margin,
                    total_cost, total_price, total_margin, item_id))
        db.commit()
        _recalc_quote(db, quote_id)
    package_id = request.form.get('package_id', '')
    return redirect(url_for('select_materials', quote_id=quote_id, package_id=package_id))

@app.route('/quotes/<int:quote_id>')
@login_required
def edit_quote(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    line_items = db.execute("SELECT * FROM quote_line_items WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' AND is_modifier='N' ORDER BY work_type").fetchall()
    subs = db.execute("SELECT * FROM subs WHERE active='Y' ORDER BY name").fetchall()
    commission_policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    schedule = db.execute("SELECT * FROM payment_schedules WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    schedule_json = json.dumps([{'id':s['id'],'label':s['label'],'amount':s['amount'],'pct':s['pct']} for s in schedule])
    manufacturers = db.execute("SELECT * FROM surface_manufacturers WHERE active='Y' ORDER BY manufacturer_name").fetchall()
    applicators = db.execute("SELECT * FROM surface_applicators WHERE active='Y' ORDER BY name").fetchall()
    # Add manufacturer_id to surface application line items
    line_items_enriched = []
    for item in line_items:
        item_dict = dict(item)
        if item_dict.get('product_id'):
            p = db.execute("""SELECT sp.product_line, sm.manufacturer_id
                               FROM surface_products sp
                               JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                               WHERE sp.product_id=?""", (item_dict['product_id'],)).fetchone()
            if p:
                item_dict['mfr_id'] = p['manufacturer_id']
                item_dict['product_line_name'] = p['product_line']
        item_dict['applied_qualifier_ids'] = {q['id'] for q in json.loads(item_dict.get('qualifiers_json') or '[]')}
        line_items_enriched.append(item_dict)
    line_items = [i for i in line_items_enriched if not i.get('is_optional')]
    optional_items = [i for i in line_items_enriched if i.get('is_optional') and not i.get('is_declined')]
    declined_items = [i for i in line_items_enriched if i.get('is_optional') and i.get('is_declined')]
    optional_total = round(sum(float(i['total_price'] or 0) for i in optional_items), 2)

    materials = db.execute("""SELECT m.*, s.name as supplier_name FROM materials m
                              JOIN suppliers s ON m.supplier_id=s.supplier_id
                              WHERE m.active='Y' AND m.work_type_id IN (4,5,6)
                              ORDER BY m.category, m.series""").fetchall()
    qualifier_rows = db.execute("SELECT * FROM qualifiers WHERE active='Y' ORDER BY label").fetchall()
    qualifiers_by_wt = {}
    for q in qualifier_rows:
        qualifiers_by_wt.setdefault(q['work_type_id'], []).append(dict(q))
    additives = db.execute("SELECT * FROM surface_additives WHERE active='Y' ORDER BY label").fetchall()
    return render_template('edit_quote.html', quote=quote, line_items=line_items,
                           optional_items=optional_items, optional_total=optional_total,
                           declined_items=declined_items,
                           work_types=work_types, subs=subs, commission_policy=commission_policy,
                           current_role=g.role, schedule=schedule, schedule_json=schedule_json,
                           manufacturers=manufacturers, applicators=applicators, materials=materials,
                           qualifiers_by_wt=qualifiers_by_wt, additives=additives)

@app.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def delete_quote(quote_id):
    db = get_db()
    db.execute("DELETE FROM quote_line_items WHERE quote_id=?", (quote_id,))
    db.execute("DELETE FROM quotes WHERE quote_id=?", (quote_id,))
    db.commit()
    return redirect(url_for('quotes_list'))

# ── Line Items ────────────────────────────────────────────────────────────────

@app.route('/quotes/<int:quote_id>/edit_details', methods=['GET','POST'])
@login_required
def edit_quote_details(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if request.method == 'POST':
        db.execute("""UPDATE quotes SET customer_name=?,address=?,salesperson=?,
                      pool_perimeter=?,pool_shallow=?,pool_deep=?,pool_sqft=?,
                      has_spa=?,spa_perimeter=?,spa_depth=?,spa_sqft=?,
                      has_shelf=?,shelf_sqft=?,total_surface_sqft=?,
                      steps_lf=?,benches_lf=?,swimouts_lf=?,customer_email=?,water_surface_sqft=?
                      WHERE quote_id=?""",
                   (request.form['customer_name'], request.form.get('address',''),
                    request.form.get('salesperson',''),
                    float(request.form.get('pool_perimeter',0) or 0),
                    float(request.form.get('pool_shallow',0) or 0),
                    float(request.form.get('pool_deep',0) or 0),
                    float(request.form.get('pool_sqft',0) or 0),
                    1 if request.form.get('has_spa') else 0,
                    float(request.form.get('spa_perimeter',0) or 0),
                    float(request.form.get('spa_depth',0) or 0),
                    float(request.form.get('spa_sqft',0) or 0),
                    1 if request.form.get('has_shelf') else 0,
                    float(request.form.get('shelf_sqft_calc',0) or 0),
                    float(request.form.get('total_surface_sqft',0) or 0),
                    float(request.form.get('steps_lf',0) or 0),
                    float(request.form.get('benches_lf',0) or 0),
                    float(request.form.get('swimouts_lf',0) or 0),
                    request.form.get('customer_email','').strip(),
                    float(request.form.get('water_surface_sqft',0) or 0),
                    quote_id))
        db.commit()
        return redirect(url_for('edit_quote', quote_id=quote_id))
    return render_template('edit_quote_details.html', quote=quote)

@app.route('/quotes/<int:quote_id>/line_items', methods=['POST'])
@login_required
def add_line_item(quote_id):
    db = get_db()
    data = request.json
    work_type_id = data.get('work_type_id')
    wt = db.execute("SELECT * FROM work_types WHERE work_type_id=?", (work_type_id,)).fetchone()

    sub_id = data.get('sub_id','')
    sub_name = ''
    if sub_id:
        s = db.execute("SELECT name FROM subs WHERE sub_id=?", (sub_id,)).fetchone()
        if not s:
            s = db.execute("SELECT name FROM surface_applicators WHERE sub_id=?", (sub_id,)).fetchone()
        if s:
            sub_name = s['name']

    product_label = ''
    product_id = data.get('product_id')
    if product_id:
        p = db.execute(
            "SELECT sm.manufacturer_name, sp.product_line, sp.finish "
            "FROM surface_products sp "
            "JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id "
            "WHERE sp.product_id=?", (product_id,)).fetchone()
        if p:
            product_label = p['manufacturer_name'] + ' ' + p['product_line'] + ' \u2013 ' + p['finish']

    material_label = ''
    material_id = data.get('material_id')
    if material_id:
        m = db.execute(
            "SELECT m.series, s.name as supplier_name FROM materials m "
            "JOIN suppliers s ON m.supplier_id=s.supplier_id "
            "WHERE m.material_id=?", (material_id,)).fetchone()
        if m:
            material_label = m['supplier_name'] + ' \u2014 ' + m['series']

    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    wt_dict = dict(wt) if wt else {}
    wt_label = wt_dict.get('work_type','')
    if wt_label in ['Surface Application', 'Surface Removal']:
        parts = ['Pool']
        if quote and quote['has_spa']:
            parts.append('Spa')
        if quote and quote['has_shelf']:
            parts.append('Sunshelf')
        if len(parts) > 1:
            wt_label = wt_label + ' \u2014 ' + ' & '.join(parts)
    if wt_dict:
        wt_dict['work_type'] = wt_label

    can_override = bool(g.role and g.role['can_override_min_markup'])
    item = build_line_item(data, wt_dict, sub_name, product_label, material_label, can_override, quote)
    sort_order = db.execute("SELECT COUNT(*) FROM quote_line_items WHERE quote_id=?", (quote_id,)).fetchone()[0]

    db.execute(
        "INSERT INTO quote_line_items "
        "(quote_id, work_type_id, work_type_label, cost_structure, "
        "sub_id, sub_name, labor_quantity, labor_unit, "
        "labor_cost_per_unit, labor_total_cost, labor_markup_pct, labor_margin_pct, labor_total_price, labor_min_markup, "
        "material_id, material_label, material_quantity, material_unit, "
        "material_cost_per_unit, material_total_cost, material_markup_pct, material_margin_pct, material_total_price, material_min_markup, "
        "product_id, product_label, total_cost, total_price, total_margin_pct, sort_order, description, is_optional) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (quote_id, item['work_type_id'], item['work_type_label'], item['cost_structure'],
         item['sub_id'], item['sub_name'], item['labor_quantity'], item['labor_unit'],
         item['labor_cost_per_unit'], item['labor_total_cost'], item['labor_markup_pct'],
         item['labor_margin_pct'], item['labor_total_price'], item['labor_min_markup'],
         item['material_id'], item['material_label'], item['material_quantity'], item['material_unit'],
         item['material_cost_per_unit'], item['material_total_cost'], item['material_markup_pct'],
         item['material_margin_pct'], item['material_total_price'], item['material_min_markup'],
         item['product_id'], item['product_label'],
         item['total_cost'], item['total_price'], item['total_margin_pct'], sort_order, item['description'],
         1 if data.get('is_optional') else 0))
    db.commit()
    _recalc_quote(db, quote_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/markup', methods=['POST'])
@login_required
def update_markup(quote_id, item_id):
    db = get_db()
    data = request.json
    component = data.get('component', 'labor')  # 'labor' or 'material'
    new_markup = float(data.get('markup_pct', 30))
    row = db.execute("SELECT * FROM quote_line_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    can_override = bool(g.role and g.role['can_override_min_markup'])

    if component == 'labor':
        min_m = row['labor_min_markup']
        if not can_override:
            new_markup = max(new_markup, min_m)
        qty = row['labor_quantity']
        cpu = row['labor_cost_per_unit']
        total_cost, total_price, margin = calc_component(cpu, qty, new_markup, min_m, can_override)
        db.execute("UPDATE quote_line_items SET labor_markup_pct=?,labor_margin_pct=?,labor_total_price=? WHERE id=?",
                   (new_markup, margin, total_price, item_id))
    else:
        min_m = row['material_min_markup']
        if not can_override:
            new_markup = max(new_markup, min_m)
        qty = row['material_quantity']
        cpu = row['material_cost_per_unit']
        total_cost, total_price, margin = calc_component(cpu, qty, new_markup, min_m, can_override)
        db.execute("UPDATE quote_line_items SET material_markup_pct=?,material_margin_pct=?,material_total_price=? WHERE id=?",
                   (new_markup, margin, total_price, item_id))

    # Recalc item rollup (labor + material + qualifiers)
    new_total_cost, new_total_price, new_total_margin = _rollup_item_totals(db, item_id)
    row = db.execute("SELECT * FROM quote_line_items WHERE id=?", (item_id,)).fetchone()
    db.commit()
    _recalc_quote(db, quote_id)

    updated_quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    gross_profit = updated_quote['total_price'] - updated_quote['total_cost']
    commission = updated_quote['commission']
    min_m = row['labor_min_markup'] if component == 'labor' else row['material_min_markup']
    return jsonify({
        'component': component,
        'markup_pct': new_markup,
        'margin_pct': margin,
        'component_price': round(total_price, 2),
        'item_total_price': new_total_price,
        'item_total_cost': new_total_cost,
        'item_total_margin': new_total_margin,
        'quote_total_cost': updated_quote['total_cost'],
        'quote_total_price': updated_quote['total_price'],
        'quote_total_margin': updated_quote['total_margin'],
        'quote_commission': commission,
        'quote_gross_profit': round(gross_profit, 2),
        'quote_net_profit': round(gross_profit - commission, 2),
        'at_floor': new_markup <= min_m and not can_override
    })

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_line_item(quote_id, item_id):
    db = get_db()
    db.execute("DELETE FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id))
    db.commit()
    _recalc_quote(db, quote_id)
    return jsonify({'success': True})

def _rollup_item_totals(db, item_id):
    """Recompute a line item's total_cost/total_price/total_margin_pct from labor + material +
    qualifiers, using the item's labor markup to price the qualifier cost. Call after any edit
    that changes labor/material components or qualifiers. Returns (total_cost, total_price, total_margin)."""
    row = db.execute("SELECT * FROM quote_line_items WHERE id=?", (item_id,)).fetchone()
    quals = json.loads(row['qualifiers_json'] or '[]')
    pass_through_ids = {r['qualifier_id'] for r in db.execute(
        "SELECT qualifier_id FROM qualifiers WHERE is_freight=1").fetchall()}
    markup_cost = sum(q['amount'] for q in quals if q['id'] not in pass_through_ids)
    pass_through_cost = sum(q['amount'] for q in quals if q['id'] in pass_through_ids)
    q_price = round(markup_cost * (1 + float(row['labor_markup_pct'] or 0) / 100), 2) + round(pass_through_cost, 2)
    q_cost = round(markup_cost + pass_through_cost, 2)

    tax_cost = 0.0
    if row['tax_included']:
        settings = db.execute("SELECT tax_rate_pct FROM company_settings WHERE id=1").fetchone()
        rate = float(settings['tax_rate_pct'] or 0) if settings and settings['tax_rate_pct'] is not None else 0
        tax_cost = round(float(row['material_total_cost'] or 0) * rate / 100, 2)

    total_cost = round(float(row['labor_total_cost'] or 0) + float(row['material_total_cost'] or 0) + q_cost + tax_cost, 2)
    total_price = round(float(row['labor_total_price'] or 0) + float(row['material_total_price'] or 0) + q_price + tax_cost, 2)
    total_margin = round(((total_price - total_cost) / total_price * 100) if total_price else 0, 1)
    db.execute("UPDATE quote_line_items SET total_cost=?,total_price=?,total_margin_pct=? WHERE id=?",
               (total_cost, total_price, total_margin, item_id))
    return total_cost, total_price, total_margin

def _recalc_quote(db, quote_id):
    items = db.execute(
        "SELECT total_cost,total_price FROM quote_line_items WHERE quote_id=? AND (is_optional=0 OR is_optional IS NULL)",
        (quote_id,)
    ).fetchall()
    total_cost = sum(float(i['total_cost'] or 0) for i in items)
    total_price = sum(float(i['total_price'] or 0) for i in items)
    margin = ((total_price - total_cost) / total_price * 100) if total_price else 0
    policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    commission = 0
    if policy:
        if policy['method'] == 'pct_of_price':
            commission = total_price * policy['rate'] / 100
        elif policy['method'] == 'pct_of_margin':
            commission = (total_price - total_cost) * policy['rate'] / 100
    db.execute("UPDATE quotes SET total_cost=?,total_price=?,total_margin=?,commission=? WHERE quote_id=?",
               (round(total_cost,2), round(total_price,2), round(margin,1), round(commission,2), quote_id))
    db.commit()
    generate_payment_schedule(db, quote_id, round(total_price,2))

# ── API ───────────────────────────────────────────────────────────────────────
@app.route('/api/subs_for_work_type')
@login_required
def subs_for_work_type():
    db = get_db()
    work_type_id = request.args.get('work_type_id')
    if int(work_type_id) == 1:
        rows = db.execute("""SELECT DISTINCT sa.sub_id, sa.name FROM surface_applicators sa
                             JOIN surface_applicator_rates sar ON sa.sub_id=sar.sub_id
                             WHERE sa.active='Y'""").fetchall()
    else:
        rows = db.execute("""SELECT DISTINCT s.sub_id, s.name FROM subs s
                             JOIN sub_rates sr ON s.sub_id=sr.sub_id
                             WHERE sr.work_type_id=? AND s.active='Y'""", (work_type_id,)).fetchall()
    return jsonify([{'sub_id': r['sub_id'], 'name': r['name']} for r in rows])

@app.route('/api/rate')
@login_required
def get_rate():
    db = get_db()
    sub_id = request.args.get('sub_id')
    work_type_id = request.args.get('work_type_id')
    product_id = request.args.get('product_id')
    if product_id and sub_id:
        row = db.execute("SELECT rate FROM surface_applicator_rates WHERE sub_id=? AND product_id=?",
                         (sub_id, product_id)).fetchone()
    elif sub_id and work_type_id:
        row = db.execute("SELECT rate, unit FROM sub_rates WHERE sub_id=? AND work_type_id=?",
                         (sub_id, work_type_id)).fetchone()
    else:
        row = None
    return jsonify({'rate': row['rate'] if row else None, 'unit': row['unit'] if row else None})

@app.route('/api/work_type_defaults')
@login_required
def work_type_defaults():
    db = get_db()
    wt_id = request.args.get('work_type_id')
    wt = db.execute("SELECT default_markup,min_markup,min_margin,unit FROM work_types WHERE work_type_id=?", (wt_id,)).fetchone()
    if wt:
        return jsonify({'default_markup': wt['default_markup'], 'min_markup': wt['min_markup'],
                        'min_margin': wt['min_margin'], 'unit': wt['unit']})
    return jsonify({'default_markup': 30, 'min_markup': 10, 'min_margin': 9.1, 'unit': ''})

@app.route('/api/products_for_applicator')
@login_required
def products_for_applicator():
    db = get_db()
    sub_id = request.args.get('sub_id')
    rows = db.execute("""SELECT sp.product_id, sm.manufacturer_name, sp.product_line, sp.finish, sar.rate
                         FROM surface_applicator_rates sar
                         JOIN surface_products sp ON sar.product_id=sp.product_id
                         JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                         WHERE sar.sub_id=? AND sp.active='Y'
                         ORDER BY sm.manufacturer_name, sp.product_line, sp.finish""", (sub_id,)).fetchall()
    return jsonify([{'product_id': r['product_id'],
                     'label': f"{r['manufacturer_name']} {r['product_line']} \u2013 {r['finish']}",
                     'rate': r['rate']} for r in rows])

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/subs')
@require_permission('can_access_admin')
def admin_subs():
    db = get_db()
    subs = db.execute("SELECT * FROM subs ORDER BY sub_id").fetchall()
    applicators = db.execute("SELECT * FROM surface_applicators ORDER BY sub_id").fetchall()
    all_sub_rates = db.execute("""SELECT sr.*, wt.work_type FROM sub_rates sr
                                  JOIN work_types wt ON sr.work_type_id=wt.work_type_id
                                  ORDER BY sr.sub_id, wt.work_type""").fetchall()
    all_applicator_rates = db.execute("""SELECT sar.*, sm.manufacturer_name, sp.product_line, sp.finish
                                         FROM surface_applicator_rates sar
                                         JOIN surface_products sp ON sar.product_id=sp.product_id
                                         JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                                         ORDER BY sar.sub_id""").fetchall()
    surface_products = db.execute("""SELECT sp.product_id, sm.manufacturer_name, sp.product_line, sp.finish
                                     FROM surface_products sp
                                     JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                                     WHERE sp.active='Y' ORDER BY sm.manufacturer_name, sp.product_line, sp.finish""").fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    # Calculate next IDs
    sub_nums = []
    for s in subs:
        try: sub_nums.append(int(s['sub_id'].replace('S','').replace('s','')))
        except: pass
    app_nums = []
    for a in applicators:
        try: app_nums.append(int(a['sub_id'].replace('P','').replace('p','')))
        except: pass
    next_sub_id = max(sub_nums) + 1 if sub_nums else 1
    next_app_id = max(app_nums) + 1 if app_nums else 1
    return render_template('admin_subs.html', subs=subs, applicators=applicators,
                           all_sub_rates=all_sub_rates, all_applicator_rates=all_applicator_rates,
                           surface_products=surface_products, work_types=work_types,
                           next_sub_id=next_sub_id, next_app_id=next_app_id)

@app.route('/admin/subs/add', methods=['POST'])
@require_permission('can_edit_subs')
def add_sub():
    db = get_db()
    table = request.form.get('table','subs')
    name = request.form['name'].strip()
    if table == 'applicators':
        # Auto-increment P prefix
        rows = db.execute("SELECT sub_id FROM surface_applicators").fetchall()
        nums = []
        for r in rows:
            try: nums.append(int(r['sub_id'].replace('P','').replace('p','')))
            except: pass
        next_id = 'P' + str(max(nums) + 1 if nums else 1)
        db.execute("INSERT INTO surface_applicators VALUES (?,?,'Y')", (next_id, name))
    else:
        # Auto-increment S prefix
        rows = db.execute("SELECT sub_id FROM subs").fetchall()
        nums = []
        for r in rows:
            try: nums.append(int(r['sub_id'].replace('S','').replace('s','')))
            except: pass
        next_id = 'S' + str(max(nums) + 1 if nums else 1)
        db.execute("INSERT INTO subs VALUES (?,?,'Y')", (next_id, name))
    db.commit()
    return redirect(url_for('admin_subs'))

@app.route('/admin/subs/toggle', methods=['POST'])
@require_permission('can_edit_subs')
def toggle_sub():
    db = get_db()
    sub_id = request.form['sub_id']
    table = request.form.get('table','subs')
    current = db.execute(f"SELECT active FROM {table} WHERE sub_id=?", (sub_id,)).fetchone()
    new_val = 'N' if current['active'] == 'Y' else 'Y'
    db.execute(f"UPDATE {table} SET active=? WHERE sub_id=?", (new_val, sub_id))
    db.commit()
    return redirect(url_for('admin_subs'))

@app.route('/admin/work_types')
@require_permission('can_access_admin')
def admin_work_types():
    db = get_db()
    wts = db.execute("SELECT * FROM work_types ORDER BY work_type_id").fetchall()
    return render_template('admin_work_types.html', work_types=wts)

@app.route('/admin/work_types/add', methods=['POST'])
@require_permission('can_edit_work_types')
def add_work_type():
    db = get_db()
    db.execute("""INSERT INTO work_types (work_type,unit,cost_structure,default_markup,min_markup,min_margin,
                  is_modifier,modified_by,show_on_quote,active,description) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (request.form['work_type'], request.form['unit'], request.form['cost_structure'],
                float(request.form.get('default_markup',30) or 30),
                float(request.form.get('min_markup',10) or 10),
                float(request.form.get('min_margin',9.1) or 9.1),
                request.form.get('is_modifier','N'), request.form.get('modified_by',''),
                request.form.get('show_on_quote','Y'), 'Y', request.form.get('description','')))
    db.commit()
    return redirect(url_for('admin_work_types'))

@app.route('/admin/work_types/edit/<int:wt_id>', methods=['POST'])
@require_permission('can_edit_work_types')
def edit_work_type(wt_id):
    db = get_db()
    db.execute("""UPDATE work_types SET work_type=?,default_markup=?,min_markup=?,min_margin=?,active=?,description=?
                  WHERE work_type_id=?""",
               (request.form['work_type'],
                float(request.form.get('default_markup',30) or 30),
                float(request.form.get('min_markup',10) or 10),
                float(request.form.get('min_margin',9.1) or 9.1),
                request.form.get('active','Y'), request.form.get('description',''), wt_id))
    db.commit()
    return redirect(url_for('admin_work_types'))

@app.route('/admin/sub_rates')
@require_permission('can_access_admin')
def admin_sub_rates():
    db = get_db()
    rates = db.execute("""SELECT sr.*, s.name as sub_name, wt.work_type
                          FROM sub_rates sr
                          JOIN subs s ON sr.sub_id=s.sub_id
                          JOIN work_types wt ON sr.work_type_id=wt.work_type_id
                          ORDER BY s.name, wt.work_type""").fetchall()
    subs = db.execute("SELECT * FROM subs WHERE active='Y' ORDER BY name").fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    return render_template('admin_sub_rates.html', rates=rates, subs=subs, work_types=work_types)

@app.route('/admin/sub_rates/add', methods=['POST'])
@require_permission('can_edit_sub_rates')
def add_sub_rate():
    db = get_db()
    wt = db.execute("SELECT unit FROM work_types WHERE work_type_id=?", (request.form['work_type_id'],)).fetchone()
    db.execute("INSERT INTO sub_rates (sub_id,work_type_id,rate,unit,notes) VALUES (?,?,?,?,?)",
               (request.form['sub_id'], request.form['work_type_id'],
                float(request.form['rate']), wt['unit'] if wt else '', request.form.get('notes','')))
    db.commit()
    return redirect(url_for('admin_sub_rates'))

@app.route('/admin/sub_rates/delete/<int:rate_id>', methods=['POST'])
@require_permission('can_edit_sub_rates')
def delete_sub_rate(rate_id):
    db = get_db()
    db.execute("DELETE FROM sub_rates WHERE id=?", (rate_id,))
    db.commit()
    return redirect(url_for('admin_sub_rates'))

@app.route('/admin/surfaces')
@require_permission('can_access_admin')
def admin_surfaces():
    db = get_db()
    manufacturers = db.execute("SELECT * FROM surface_manufacturers ORDER BY manufacturer_name").fetchall()
    products = db.execute("""SELECT sp.*, sm.manufacturer_name FROM surface_products sp
                             JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                             ORDER BY sm.manufacturer_name, sp.product_line, sp.finish""").fetchall()
    rates = db.execute("""SELECT sar.*, sa.name as applicator_name, sm.manufacturer_name, sp.product_line, sp.finish
                          FROM surface_applicator_rates sar
                          JOIN surface_applicators sa ON sar.sub_id=sa.sub_id
                          JOIN surface_products sp ON sar.product_id=sp.product_id
                          JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                          ORDER BY sa.name, sm.manufacturer_name, sp.finish""").fetchall()
    applicators = db.execute("SELECT * FROM surface_applicators WHERE active='Y' ORDER BY name").fetchall()
    additives = db.execute("SELECT * FROM surface_additives WHERE active='Y' ORDER BY label").fetchall()
    return render_template('admin_surfaces.html', manufacturers=manufacturers, products=products,
                           rates=rates, applicators=applicators, additives=additives)

@app.route('/admin/surfaces/add_manufacturer', methods=['POST'])
@require_permission('can_edit_surfaces')
def add_manufacturer():
    db = get_db()
    db.execute("INSERT INTO surface_manufacturers (manufacturer_name) VALUES (?)", (request.form['manufacturer_name'],))
    db.commit()
    return redirect(url_for('admin_surfaces'))

@app.route('/admin/surfaces/add_product', methods=['POST'])
@require_permission('can_edit_surfaces')
def add_product():
    db = get_db()
    db.execute("INSERT INTO surface_products (manufacturer_id,product_line,finish) VALUES (?,?,?)",
               (request.form['manufacturer_id'], request.form['product_line'], request.form['finish']))
    db.commit()
    return redirect(url_for('admin_surfaces'))

@app.route('/admin/surfaces/add_rate', methods=['POST'])
@require_permission('can_edit_surfaces')
def add_applicator_rate():
    db = get_db()
    db.execute("""INSERT INTO surface_applicator_rates (sub_id,product_id,rate,notes,min_sqft,min_spa_price)
                  VALUES (?,?,?,?,?,?)""",
               (request.form['sub_id'], request.form['product_id'],
                float(request.form['rate']), request.form.get('notes',''),
                float(request.form.get('min_sqft', 0) or 0),
                float(request.form.get('min_spa_price', 0) or 0)))
    db.commit()
    return redirect(url_for('admin_surfaces'))

@app.route('/admin/surfaces/delete_rate/<int:rate_id>', methods=['POST'])
@require_permission('can_edit_surfaces')
def delete_applicator_rate(rate_id):
    db = get_db()
    db.execute("DELETE FROM surface_applicator_rates WHERE id=?", (rate_id,))
    db.commit()
    return redirect(url_for('admin_surfaces'))

@app.route('/admin/commission')
@require_permission('can_edit_commission_policy')
def admin_commission():
    db = get_db()
    policies = db.execute("SELECT * FROM commission_policy ORDER BY id").fetchall()
    return render_template('admin_commission.html', policies=policies)

@app.route('/admin/commission/update/<int:policy_id>', methods=['POST'])
@require_permission('can_edit_commission_policy')
def update_commission(policy_id):
    db = get_db()
    db.execute("UPDATE commission_policy SET method=?,rate=?,active=? WHERE id=?",
               (request.form['method'], float(request.form['rate']),
                request.form.get('active','Y'), policy_id))
    db.commit()
    return redirect(url_for('admin_commission'))

# ── ADMIN PERMISSIONS ─────────────────────────────────────────────────────────
@app.route('/admin/permissions')
@require_permission('can_edit_commission_policy')
def admin_permissions():
    db = get_db()
    roles = db.execute("SELECT * FROM roles ORDER BY role_id").fetchall()
    users = db.execute("""SELECT u.*, r.role_name FROM users u
                          JOIN roles r ON u.role_id=r.role_id
                          ORDER BY u.user_id""").fetchall()
    return render_template('admin_permissions.html', roles=roles, users=users)

@app.route('/admin/permissions/update_role', methods=['POST'])
@require_permission('can_edit_commission_policy')
def update_role_permissions():
    db = get_db()
    role_id = request.form['role_id']
    fields = ['show_commission','show_margin_floors','can_override_min_markup',
              'can_edit_commission_policy','can_access_admin','can_edit_work_types',
              'can_edit_sub_rates','can_edit_surfaces','can_edit_subs','show_gross_profit']
    updates = {f: 1 if request.form.get(f) else 0 for f in fields}
    db.execute("""UPDATE roles SET show_commission=?,show_margin_floors=?,can_override_min_markup=?,
                  can_edit_commission_policy=?,can_access_admin=?,can_edit_work_types=?,
                  can_edit_sub_rates=?,can_edit_surfaces=?,can_edit_subs=?,show_gross_profit=?
                  WHERE role_id=?""",
               [updates[f] for f in fields] + [role_id])
    db.commit()
    return redirect(url_for('admin_permissions'))

@app.route('/admin/permissions/change_password', methods=['POST'])
@require_permission('can_edit_commission_policy')
def change_password():
    db = get_db()
    user_id = request.form['user_id']
    new_pw = request.form['new_password'].strip()
    if new_pw:
        db.execute("UPDATE users SET password=? WHERE user_id=?", (hash_pw(new_pw), user_id))
        db.commit()
    return redirect(url_for('admin_permissions'))

# ── Context processor — inject role into all templates ────────────────────────
@app.context_processor
def inject_user():
    return {'current_user': g.user, 'current_role': g.role}

# ══════════════════════════════════════════════════════════════════════════════
# MATERIALS
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/materials')
@require_permission('can_access_admin')
def admin_materials():
    db = get_db()
    suppliers = db.execute("SELECT * FROM suppliers WHERE active='Y' ORDER BY name").fetchall()
    selected_supplier = request.args.get('supplier_id', '')
    selected_category = request.args.get('category', '')

    q = "SELECT m.*, s.name as supplier_name FROM materials m JOIN suppliers s ON m.supplier_id=s.supplier_id WHERE 1=1"
    params = []
    if selected_supplier:
        q += " AND m.supplier_id=?"
        params.append(selected_supplier)
    if selected_category:
        q += " AND m.category=?"
        params.append(selected_category)
    q += " ORDER BY s.name, m.category, m.series"
    materials = db.execute(q, params).fetchall()

    categories = db.execute("SELECT DISTINCT category FROM materials ORDER BY category").fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    return render_template('admin_materials.html', suppliers=suppliers, materials=materials,
                           categories=categories, work_types=work_types,
                           selected_supplier=selected_supplier, selected_category=selected_category)

@app.route('/admin/materials/add', methods=['POST'])
@require_permission('can_edit_sub_rates')
def add_material():
    db = get_db()
    raw_price = float(request.form['raw_price'])
    conv = float(request.form['conversion_factor'])
    cost_per_qu = raw_price * conv
    db.execute("""INSERT INTO materials (supplier_id,category,series,item_code,raw_price,
                  price_unit,conversion_factor,quote_unit,cost_per_quote_unit,work_type_id,active)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (request.form['supplier_id'], request.form['category'], request.form['series'],
                request.form.get('item_code',''), raw_price, request.form['price_unit'],
                conv, request.form['quote_unit'], round(cost_per_qu,4),
                request.form.get('work_type_id') or None, 'Y'))
    db.commit()
    return redirect(url_for('admin_materials'))

@app.route('/admin/materials/toggle/<int:mat_id>', methods=['POST'])
@require_permission('can_edit_sub_rates')
def toggle_material(mat_id):
    db = get_db()
    cur = db.execute("SELECT active FROM materials WHERE material_id=?", (mat_id,)).fetchone()
    new_val = 'N' if cur['active'] == 'Y' else 'Y'
    db.execute("UPDATE materials SET active=? WHERE material_id=?", (new_val, mat_id))
    db.commit()
    return redirect(url_for('admin_materials'))

@app.route('/admin/materials/upload_csv', methods=['POST'])
@require_permission('can_edit_sub_rates')
def upload_materials_csv():
    import csv, io
    db = get_db()
    supplier_id = request.form.get('supplier_id')
    category = request.form.get('category','')
    price_unit = request.form.get('price_unit','per_sqft')
    conv = float(request.form.get('conversion_factor', 0.5))
    quote_unit = request.form.get('quote_unit','lf')
    work_type_id = request.form.get('work_type_id') or None
    f = request.files.get('csv_file')
    if not f:
        return redirect(url_for('admin_materials'))
    content = f.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    updated = 0
    added = 0
    for row in reader:
        series = (row.get('Series') or row.get('series') or row.get('Product Name') or '').strip()
        item_code = (row.get('Item Code') or row.get('item_code') or row.get('Product Code') or '').strip()
        price_str = (row.get('Cost/SHEET') or row.get('Cost/Sq. Ft.') or row.get('CONTR. Price') or row.get('Price') or '0').strip()
        price_str = price_str.replace('$','').replace(',','').strip()
        try:
            raw_price = float(price_str)
        except:
            continue
        if not series or raw_price == 0:
            continue
        cost_per_qu = round(raw_price * conv, 4)
        existing = db.execute("SELECT material_id FROM materials WHERE supplier_id=? AND item_code=? AND item_code!=''",
                              (supplier_id, item_code)).fetchone()
        if existing:
            db.execute("""UPDATE materials SET series=?,raw_price=?,cost_per_quote_unit=?,category=?
                          WHERE material_id=?""",
                       (series, raw_price, cost_per_qu, category, existing['material_id']))
            updated += 1
        else:
            db.execute("""INSERT INTO materials (supplier_id,category,series,item_code,raw_price,
                          price_unit,conversion_factor,quote_unit,cost_per_quote_unit,work_type_id,active)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (supplier_id, category, series, item_code, raw_price,
                        price_unit, conv, quote_unit, cost_per_qu, work_type_id, 'Y'))
            added += 1
    db.commit()
    return redirect(url_for('admin_materials') + f'?supplier_id={supplier_id}&msg={added}+added+{updated}+updated')

@app.route('/api/materials_for_work_type')
@login_required
def materials_for_work_type():
    db = get_db()
    work_type_id = request.args.get('work_type_id')
    supplier_id = request.args.get('supplier_id','')
    q = """SELECT m.*, s.name as supplier_name FROM materials m
           JOIN suppliers s ON m.supplier_id=s.supplier_id
           WHERE m.work_type_id=? AND m.active='Y'"""
    params = [work_type_id]
    if supplier_id:
        q += " AND m.supplier_id=?"
        params.append(supplier_id)
    q += " ORDER BY m.category, m.series"
    rows = db.execute(q, params).fetchall()
    return jsonify([{
        'material_id': r['material_id'],
        'label': f"{r['supplier_name']} | {r['category']} | {r['series']}",
        'series': r['series'],
        'item_code': r['item_code'],
        'cost': r['cost_per_quote_unit'],
        'quote_unit': r['quote_unit'],
        'category': r['category']
    } for r in rows])

@app.route('/admin/materials/update_price/<int:mat_id>', methods=['POST'])
@require_permission('can_edit_sub_rates')
def update_material_price(mat_id):
    db = get_db()
    raw_price = float(request.form['raw_price'])
    mat = db.execute("SELECT conversion_factor FROM materials WHERE material_id=?", (mat_id,)).fetchone()
    cost_per_qu = round(raw_price * mat['conversion_factor'], 4)
    db.execute("UPDATE materials SET raw_price=?,cost_per_quote_unit=? WHERE material_id=?",
               (raw_price, cost_per_qu, mat_id))
    db.commit()
    return redirect(url_for('admin_materials'))

# ── Admin: update display name ─────────────────────────────────────────────────
@app.route('/admin/permissions/update_display_name', methods=['POST'])
@require_permission('can_edit_commission_policy')
def update_display_name():
    db = get_db()
    db.execute("UPDATE users SET display_name=? WHERE user_id=?",
               (request.form['display_name'], request.form['user_id']))
    db.commit()
    return redirect(url_for('admin_permissions'))


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/quotes/<int:quote_id>/payment_schedule/update', methods=['POST'])
@login_required
def update_payment_schedule(quote_id):
    db = get_db()
    data = request.json
    for item in data.get('items', []):
        db.execute("UPDATE payment_schedules SET label=?,amount=?,pct=? WHERE id=? AND quote_id=?",
                   (item['label'], float(item['amount']), float(item['pct']), item['id'], quote_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/toggle_terms', methods=['POST'])
@login_required
def toggle_terms(quote_id):
    db = get_db()
    include_terms = 1 if request.json.get('include_terms') else 0
    db.execute("UPDATE quotes SET include_terms=? WHERE quote_id=?", (include_terms, quote_id))
    db.commit()
    return jsonify({'include_terms': bool(include_terms)})

@app.route('/quotes/<int:quote_id>/payment_schedule/regenerate', methods=['POST'])
@login_required
def regenerate_payment_schedule(quote_id):
    db = get_db()
    quote = db.execute("SELECT total_price FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if quote:
        generate_payment_schedule(db, quote_id, quote['total_price'])
    schedule = db.execute("SELECT * FROM payment_schedules WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    return jsonify([dict(s) for s in schedule])

# ══════════════════════════════════════════════════════════════════════════════
# COMPANY SETTINGS
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/settings', methods=['GET','POST'])
@require_permission('can_edit_commission_policy')
def admin_settings():
    db = get_db()
    if request.method == 'POST':
        db.execute("""UPDATE company_settings SET company_name=?,address=?,phone=?,email=?,
                      license_number=?,quote_validity_days=?,terms_text=?,tax_rate_pct=? WHERE id=1""",
                   (request.form['company_name'], request.form.get('address',''),
                    request.form.get('phone',''), request.form.get('email',''),
                    request.form.get('license_number',''),
                    int(request.form.get('quote_validity_days', 30)),
                    request.form.get('terms_text',''),
                    float(request.form.get('tax_rate_pct', 7.0) or 0)))
        db.commit()
        return redirect(url_for('admin_settings'))
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    return render_template('admin_settings.html', settings=settings)

@app.route('/admin/settings/email_config', methods=['POST'])
@require_permission('can_edit_commission_policy')
def update_email_config():
    db = get_db()
    gmail_address = request.form.get('gmail_address', '').strip()
    new_app_password = request.form.get('gmail_app_password', '').strip()
    if new_app_password:
        db.execute("UPDATE company_settings SET gmail_address=?,gmail_app_password=? WHERE id=1",
                   (gmail_address, new_app_password))
    else:
        db.execute("UPDATE company_settings SET gmail_address=? WHERE id=1", (gmail_address,))
    db.commit()
    return redirect(url_for('admin_settings'))

# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER QUOTE VIEW (print-to-PDF)
# ══════════════════════════════════════════════════════════════════════════════
def _quote_preview_html(quote_id):
    """Shared renderer for the customer quote page — used by the /preview route and email PDF export."""
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    all_items = db.execute("SELECT * FROM quote_line_items WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    all_items = [dict(i) for i in all_items]
    for i in all_items:
        i['qualifiers'] = json.loads(i.get('qualifiers_json') or '[]')
    line_items = [i for i in all_items if not i['is_optional']]
    optional_items = [i for i in all_items if i['is_optional'] and not i['is_declined']]
    declined_items = [i for i in all_items if i['is_optional'] and i['is_declined']]
    schedule = db.execute("SELECT * FROM payment_schedules WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    from datetime import date, timedelta
    today = date.today()
    valid_until = today + timedelta(days=settings['quote_validity_days'] if settings else 30)
    return render_template('quote_preview.html', quote=quote, line_items=line_items,
                           optional_items=optional_items, declined_items=declined_items,
                           schedule=schedule, settings=settings, current_role=g.role,
                           today=today.strftime('%B %d, %Y'),
                           valid_until=valid_until.strftime('%B %d, %Y'))

@app.route('/quotes/<int:quote_id>/preview')
@login_required
def quote_preview(quote_id):
    return _quote_preview_html(quote_id)

def _resolve_materials_from_quote(db, quote_id):
    """Materials currently selected on this quote's actual line items."""
    items = db.execute(
        "SELECT work_type_id, product_label, material_label FROM quote_line_items "
        "WHERE quote_id=? AND COALESCE(is_declined,0)=0", (quote_id,)
    ).fetchall()

    materials = {}
    for item in items:
        label = (item['product_label'] or item['material_label'] or '').strip()
        if not label:
            continue
        wt_id = item['work_type_id']
        if wt_id == 1:
            materials['surface'] = label
        elif wt_id in (4, 5) and 'tile' not in materials:
            materials['tile'] = label
        elif wt_id == 6:
            materials['coping'] = label
        elif wt_id == 7:
            materials['pavers'] = label
    return materials


def _resolve_materials_from_package(db, package_id):
    """A package tier's default materials (e.g. Refresh/Signature/Resort), regardless of
    what's actually on the quote — lets staff preview 'what would this tier look like'."""
    items = db.execute(
        "SELECT pi.*, wt.work_type, wt.unit, wt.cost_structure, wt.default_markup, wt.min_markup, wt.description "
        "FROM package_items pi JOIN work_types wt ON pi.work_type_id=wt.work_type_id "
        "WHERE pi.package_id=? AND pi.active='Y'", (package_id,)
    ).fetchall()
    dims = {'pool_perimeter': 80, 'has_spa': False, 'has_shelf': False,
            'steps_lf': 0, 'benches_lf': 0, 'swimouts_lf': 0, 'total_surface_sqft': 400}

    materials = {}
    for item in items:
        wt_id = item['work_type_id']
        p = _compute_line_item_pricing(db, dict(item), item['default_sub_id'], item['default_material_id'], dims, can_override=False)
        label = (p['product_label'] or p['material_label'] or '').strip()
        if not label:
            continue
        if wt_id == 1:
            materials['surface'] = label
        elif wt_id in (4, 5) and 'tile' not in materials:
            materials['tile'] = label
        elif wt_id == 6:
            materials['coping'] = label
        elif wt_id == 7:
            materials['pavers'] = label
    return materials


def _build_visualization_prompt(db, quote_id, package_id=None):
    """Compose the Gemini prompt from either the quote's actual selected materials, or a
    specific package tier's defaults if package_id is given — so staff can preview what a
    tier would look like even before the customer has committed to it."""
    if package_id:
        materials = _resolve_materials_from_package(db, package_id)
    else:
        materials = _resolve_materials_from_quote(db, quote_id)

    lines = []
    if 'surface' in materials:
        lines.append(f"Pool interior surface finish: {materials['surface']}.")
    if 'tile' in materials:
        lines.append(f"Waterline tile band, roughly 6 inches tall around the pool perimeter at the top edge: {materials['tile']}.")
    if 'coping' in materials:
        lines.append(f"Pool coping (the cap/edge material right at the pool's rim): {materials['coping']}.")
    if 'pavers' in materials:
        lines.append(f"Deck pavers surrounding the pool: {materials['pavers']}.")
    if not lines:
        lines.append("Clean, refreshed pool surface, tile, and coping in tasteful modern colors.")
    renovation_text = '\n'.join(f"{i+1}. {line}" for i, line in enumerate(lines))

    return (
        "This is a photo of a residential pool, taken from a fixed camera position.\n\n"
        "Generate an ARCHITECTURAL RENDERING / ILLUSTRATION of this exact same pool renovated — NOT a "
        "photorealistic photo. Style: clean 3D architectural visualization / concept rendering, similar "
        "to what a pool designer would present to a client — smooth shading, simplified but accurate "
        "materials, slightly stylized lighting, clearly a rendering rather than a real photograph.\n\n"
        "Keep the exact same pool shape, footprint, steps/tanning ledge layout, and camera angle/perspective "
        "as the source photo. Keep the house, landscaping, and other background elements recognizable "
        "but rendered in the same illustrated style.\n\n"
        f"Renovate with:\n{renovation_text}\n\n"
        "The pool is FULL, at normal operating water level — this is the single most important change "
        "to get right. A full pool's water line sits just below the coping, with only the top one or two "
        "rows of waterline tile peeking above the water's surface — almost the entire tile band is "
        "SUBMERGED and visible only as a faint band under the water, not as dry tile in open air. The water "
        "surface itself must be far higher in the frame than the source photo's water line — this is a big, "
        "obvious compositional change, not a subtle one. Water is clear, bright turquoise-blue, with a "
        "visible reflective surface, no algae or debris.\n\n"
        "Do not change the pool's shape, size, or the steps/ledge layout. Do not add elements that aren't "
        "requested (no umbrellas, furniture, or people)."
    )


def _generate_visualization_image(photo_bytes, mime_type, prompt):
    from google import genai
    from google.genai import types
    api_key = _load_gemini_key()
    if not api_key:
        raise RuntimeError("Gemini API key isn't configured — add GEMINI_API_KEY in Admin or as an environment variable.")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3-pro-image",
        contents=[types.Part.from_bytes(data=photo_bytes, mime_type=mime_type), prompt],
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            return part.inline_data.data, part.inline_data.mime_type
    return None, None


def _generate_quote_pdf_bytes(html):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.emulate_media(media='print')
        page.set_content(html, wait_until='networkidle')
        pdf_bytes = page.pdf(format='Letter', print_background=True)
        browser.close()
    return pdf_bytes

def _send_quote_email(to_email, subject, body_text, pdf_bytes, quote_id, gmail_address, gmail_app_password):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    msg = MIMEMultipart()
    msg['From'] = gmail_address
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text))
    part = MIMEApplication(pdf_bytes, Name=f'Quote_{quote_id:04d}.pdf')
    part['Content-Disposition'] = f'attachment; filename="Quote_{quote_id:04d}.pdf"'
    msg.attach(part)
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=20) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_email], msg.as_string())

@app.route('/quotes/<int:quote_id>/visualize', methods=['GET'])
@login_required
def quote_visualize(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return redirect(url_for('quotes_list'))
    visualizations = db.execute(
        "SELECT * FROM quote_visualizations WHERE quote_id=? ORDER BY id DESC", (quote_id,)
    ).fetchall()
    packages = db.execute("SELECT package_id, name FROM packages WHERE active='Y' ORDER BY sort_order").fetchall()
    return render_template('quote_visualize.html', quote=quote, visualizations=visualizations, packages=packages)

@app.route('/quotes/<int:quote_id>/visualize/generate', methods=['POST'])
@login_required
def generate_visualization(quote_id):
    import base64
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return jsonify({'error': 'Quote not found'}), 404

    file = request.files.get('photo')
    if not file or not file.filename:
        return jsonify({'error': 'No photo uploaded'}), 400

    package_id = request.form.get('package_id') or None
    package_label = ''
    if package_id:
        pkg = db.execute("SELECT name FROM packages WHERE package_id=?", (package_id,)).fetchone()
        package_label = pkg['name'] if pkg else ''

    photo_bytes = file.read()
    mime_type = file.mimetype or 'image/jpeg'
    original_b64 = f"data:{mime_type};base64,{base64.b64encode(photo_bytes).decode()}"
    prompt = _build_visualization_prompt(db, quote_id, package_id)

    db.execute(
        "INSERT INTO quote_visualizations (quote_id,original_image,prompt_used,package_label,status) VALUES (?,?,?,?,'generating')",
        (quote_id, original_b64, prompt, package_label)
    )
    db.commit()
    viz_id = db.execute("SELECT lastval()").fetchone()[0]

    try:
        img_bytes, out_mime = _generate_visualization_image(photo_bytes, mime_type, prompt)
        if img_bytes:
            generated_b64 = f"data:{out_mime};base64,{base64.b64encode(img_bytes).decode()}"
            db.execute("UPDATE quote_visualizations SET generated_image=?,status='done' WHERE id=?",
                       (generated_b64, viz_id))
        else:
            db.execute("UPDATE quote_visualizations SET status='error',error_message=? WHERE id=?",
                       ('No image was returned by the model.', viz_id))
    except Exception as e:
        db.execute("UPDATE quote_visualizations SET status='error',error_message=? WHERE id=?",
                   (str(e), viz_id))
    db.commit()

    result = db.execute("SELECT * FROM quote_visualizations WHERE id=?", (viz_id,)).fetchone()
    return jsonify({
        'id': result['id'],
        'status': result['status'],
        'generated_image': result['generated_image'],
        'package_label': result['package_label'],
        'error_message': result['error_message'],
    })

@app.route('/quotes/<int:quote_id>/visualize/<int:viz_id>/delete', methods=['POST'])
@login_required
def delete_visualization(quote_id, viz_id):
    db = get_db()
    db.execute("DELETE FROM quote_visualizations WHERE id=? AND quote_id=?", (viz_id, quote_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/email', methods=['POST'])
@login_required
def email_quote(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return jsonify({'error': 'Quote not found'}), 404
    to_email = (quote['customer_email'] or '').strip()
    if not to_email:
        return jsonify({'error': 'No customer email on file. Add one in Edit Details.'}), 400
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    gmail_address = settings['gmail_address'] if settings else ''
    gmail_app_password = settings['gmail_app_password'] if settings else ''
    if not gmail_address or not gmail_app_password:
        return jsonify({'error': 'Email sending isn\'t set up yet. Add your Gmail address and App Password in Admin → Company Settings.'}), 400
    try:
        html = _quote_preview_html(quote_id)
        pdf_bytes = _generate_quote_pdf_bytes(html)
        company_name = settings['company_name'] if settings else 'Your Company'
        subject = f"Your Quote from {company_name} — QT-{quote_id:04d}"
        body = (f"Hi {quote['customer_name']},\n\n"
                f"Please find your quote attached.\n\n"
                f"Thank you,\n{quote['salesperson'] or company_name}")
        _send_quote_email(to_email, subject, body, pdf_bytes, quote_id, gmail_address, gmail_app_password)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'success': True, 'sent_to': to_email})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/decline', methods=['POST'])
@login_required
def decline_optional_item(quote_id, item_id):
    db = get_db()
    row = db.execute("SELECT is_optional FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row or not row['is_optional']:
        return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE quote_line_items SET is_declined=1 WHERE id=?", (item_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/include', methods=['POST'])
@login_required
def include_optional_item(quote_id, item_id):
    db = get_db()
    row = db.execute("SELECT is_optional FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row or not row['is_optional']:
        return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE quote_line_items SET is_optional=0,is_declined=0 WHERE id=?", (item_id,))
    db.commit()
    _recalc_quote(db, quote_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/restore_declined', methods=['POST'])
@login_required
def restore_declined_item(quote_id, item_id):
    if not (g.role and g.role['can_override_min_markup']):
        return jsonify({'error': 'Not permitted'}), 403
    db = get_db()
    db.execute("UPDATE quote_line_items SET is_declined=0 WHERE id=? AND quote_id=?", (item_id, quote_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/sign', methods=['POST'])
@login_required
def sign_quote(quote_id):
    db = get_db()
    data = request.json
    signature_data = data.get('signature', '')
    printed_name = (data.get('printed_name') or '').strip()
    if not signature_data or not printed_name:
        return jsonify({'error': 'Signature and printed name are required'}), 400
    from datetime import datetime
    signed_at = datetime.now().strftime('%B %d, %Y %I:%M %p')
    db.execute("UPDATE quotes SET signature_data=?,signed_at=?,signed_name=? WHERE quote_id=?",
               (signature_data, signed_at, printed_name, quote_id))
    db.commit()
    return jsonify({'success': True, 'signed_at': signed_at})

@app.route('/quotes/<int:quote_id>/unsign', methods=['POST'])
@login_required
def unsign_quote(quote_id):
    if not (g.role and g.role['can_override_min_markup']):
        return jsonify({'error': 'Not permitted'}), 403
    db = get_db()
    db.execute("UPDATE quotes SET signature_data='',signed_at='',signed_name='' WHERE quote_id=?", (quote_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin/settings/upload_logo', methods=['POST'])
@require_permission('can_edit_commission_policy')
def upload_logo():
    import base64
    f = request.files.get('logo')
    if not f:
        return redirect(url_for('admin_settings'))
    # Store as base64 in db — keeps it simple, no file system dependency
    data = base64.b64encode(f.read()).decode('utf-8')
    mime = f.content_type or 'image/png'
    logo_data = f'data:{mime};base64,{data}'
    db = get_db()
    db.execute("UPDATE company_settings SET logo_path=? WHERE id=1", (logo_data,))
    db.commit()
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/remove_logo', methods=['POST'])
@require_permission('can_edit_commission_policy')
def remove_logo():
    db = get_db()
    db.execute("UPDATE company_settings SET logo_path='' WHERE id=1")
    db.commit()
    return redirect(url_for('admin_settings'))


@app.route('/api/surface_manufacturers')
@login_required
def surface_manufacturers_api():
    db = get_db()
    rows = db.execute("""SELECT DISTINCT sm.manufacturer_id, sm.manufacturer_name
                         FROM surface_manufacturers sm
                         JOIN surface_products sp ON sm.manufacturer_id=sp.manufacturer_id
                         JOIN surface_applicator_rates sar ON sp.product_id=sar.product_id
                         WHERE sm.active='Y'
                         ORDER BY sm.manufacturer_name""").fetchall()
    return jsonify([{'id': r['manufacturer_id'], 'name': r['manufacturer_name']} for r in rows])

@app.route('/api/surface_product_lines')
@login_required
def surface_product_lines_api():
    db = get_db()
    mfr_id = request.args.get('manufacturer_id')
    sub_id = request.args.get('sub_id', '')
    q = """SELECT DISTINCT sp.product_line FROM surface_products sp
           JOIN surface_applicator_rates sar ON sp.product_id=sar.product_id
           WHERE sp.manufacturer_id=? AND sp.active='Y'"""
    params = [mfr_id]
    if sub_id:
        q += " AND sar.sub_id=?"
        params.append(sub_id)
    q += " ORDER BY sp.product_line"
    rows = db.execute(q, params).fetchall()
    return jsonify([r['product_line'] for r in rows])

@app.route('/api/surface_finishes')
@login_required
def surface_finishes_api():
    db = get_db()
    mfr_id = request.args.get('manufacturer_id')
    product_line = request.args.get('product_line')
    sub_id = request.args.get('sub_id', '')
    q = """SELECT sp.product_id, sp.finish, sar.rate FROM surface_products sp
           JOIN surface_applicator_rates sar ON sp.product_id=sar.product_id
           WHERE sp.manufacturer_id=? AND sp.product_line=? AND sp.active='Y'"""
    params = [mfr_id, product_line]
    if sub_id:
        q += " AND sar.sub_id=?"
        params.append(sub_id)
    q += " ORDER BY sp.finish"
    rows = db.execute(q, params).fetchall()
    return jsonify([{'product_id': r['product_id'], 'finish': r['finish'], 'rate': r['rate']} for r in rows])


# ══════════════════════════════════════════════════════════════════════════════
# QUOTE TEMPLATE ADMIN
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/quote_template')
@require_permission('can_access_admin')
def admin_quote_template():
    db = get_db()
    template_items = db.execute("""
        SELECT qt.*, wt.work_type, wt.unit, wt.default_sub_id,
               s.name as default_sub_name
        FROM quote_template qt
        JOIN work_types wt ON qt.work_type_id=wt.work_type_id
        LEFT JOIN subs s ON wt.default_sub_id=s.sub_id
        ORDER BY qt.sort_order
    """).fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    all_work_types = work_types
    subs = db.execute("SELECT * FROM subs WHERE active='Y' ORDER BY name").fetchall()
    return render_template('admin_quote_template.html',
                           template_items=template_items,
                           work_types=work_types,
                           all_work_types=all_work_types,
                           subs=subs)

@app.route('/admin/quote_template/toggle/<int:item_id>', methods=['POST'])
@require_permission('can_access_admin')
def toggle_template_item(item_id):
    db = get_db()
    cur = db.execute("SELECT active FROM quote_template WHERE id=?", (item_id,)).fetchone()
    new_val = 'N' if cur['active'] == 'Y' else 'Y'
    db.execute("UPDATE quote_template SET active=? WHERE id=?", (new_val, item_id))
    db.commit()
    return redirect(url_for('admin_quote_template'))

@app.route('/admin/quote_template/add', methods=['POST'])
@require_permission('can_access_admin')
def add_template_item():
    db = get_db()
    db.execute("INSERT INTO quote_template (work_type_id, sort_order, active) VALUES (?,?,'Y')",
               (request.form['work_type_id'], request.form.get('sort_order', 99)))
    db.commit()
    return redirect(url_for('admin_quote_template'))

@app.route('/admin/quote_template/defaults', methods=['POST'])
@require_permission('can_access_admin')
def save_template_defaults():
    db = get_db()
    work_types = db.execute("SELECT work_type_id FROM work_types WHERE active='Y'").fetchall()
    for wt in work_types:
        key = f"default_sub_{wt['work_type_id']}"
        val = request.form.get(key, '')
        db.execute("UPDATE work_types SET default_sub_id=? WHERE work_type_id=?",
                   (val, wt['work_type_id']))
    db.commit()
    return redirect(url_for('admin_quote_template'))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: PACKAGES (Refresh / Signature / Resort)
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/packages')
@require_permission('can_access_admin')
def admin_packages():
    db = get_db()
    packages = db.execute("SELECT * FROM packages ORDER BY sort_order").fetchall()
    packages_with_items = []
    for pkg in packages:
        items = db.execute("""
            SELECT pi.*, wt.work_type, wt.unit,
                   COALESCE(s.name, sa.name) as sub_name,
                   m.series as material_series,
                   sp.finish as product_finish, sm.manufacturer_name
            FROM package_items pi
            JOIN work_types wt ON pi.work_type_id=wt.work_type_id
            LEFT JOIN subs s ON pi.default_sub_id=s.sub_id
            LEFT JOIN surface_applicators sa ON pi.default_sub_id=sa.sub_id
            LEFT JOIN materials m ON pi.default_material_id=m.material_id
            LEFT JOIN surface_products sp ON pi.default_material_id=sp.product_id AND wt.work_type_id=1
            LEFT JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
            WHERE pi.package_id=?
            ORDER BY pi.sort_order
        """, (pkg['package_id'],)).fetchall()

        eligible_tiles = _eligible_tiles_for_package(db, pkg['package_id'])
        packages_with_items.append({'pkg': pkg, 'line_items': items, 'eligible_tiles': eligible_tiles})
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    return render_template('admin_packages.html', packages_with_items=packages_with_items, work_types=work_types)

@app.route('/admin/packages/<int:package_id>/edit', methods=['POST'])
@require_permission('can_access_admin')
def edit_package(package_id):
    db = get_db()
    threshold = request.form.get('tile_price_threshold', '').strip()
    db.execute("""UPDATE packages SET name=?,badge=?,price_range_label=?,life_expectancy_label=?,description=?,tile_price_threshold=?
                  WHERE package_id=?""",
               (request.form['name'], request.form.get('badge',''),
                request.form.get('price_range_label',''), request.form.get('life_expectancy_label',''),
                request.form.get('description',''), float(threshold) if threshold else None, package_id))
    db.commit()
    return redirect(url_for('admin_packages'))

@app.route('/admin/packages/<int:package_id>/items/add', methods=['POST'])
@require_permission('can_access_admin')
def add_package_item(package_id):
    db = get_db()
    work_type_id = request.form['work_type_id']
    sub_id = request.form.get('sub_id', '')
    material_id = request.form.get('material_id') or None
    is_optional = 1 if request.form.get('is_optional') else 0
    sort_order = db.execute("SELECT COUNT(*) FROM package_items WHERE package_id=?", (package_id,)).fetchone()[0] + 1
    db.execute("""INSERT INTO package_items (package_id,work_type_id,sort_order,default_sub_id,default_material_id,active,is_optional)
                  VALUES (?,?,?,?,?,'Y',?)""", (package_id, work_type_id, sort_order, sub_id, material_id, is_optional))
    db.commit()
    return redirect(url_for('admin_packages'))

@app.route('/admin/packages/items/<int:item_id>/toggle', methods=['POST'])
@require_permission('can_access_admin')
def toggle_package_item(item_id):
    db = get_db()
    cur = db.execute("SELECT active FROM package_items WHERE id=?", (item_id,)).fetchone()
    new_val = 'N' if cur['active'] == 'Y' else 'Y'
    db.execute("UPDATE package_items SET active=? WHERE id=?", (new_val, item_id))
    db.commit()
    return redirect(url_for('admin_packages'))

@app.route('/admin/packages/items/<int:item_id>/toggle_optional', methods=['POST'])
@require_permission('can_access_admin')
def toggle_package_item_optional(item_id):
    db = get_db()
    cur = db.execute("SELECT is_optional FROM package_items WHERE id=?", (item_id,)).fetchone()
    new_val = 0 if cur['is_optional'] else 1
    db.execute("UPDATE package_items SET is_optional=? WHERE id=?", (new_val, item_id))
    db.commit()
    return redirect(url_for('admin_packages'))

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/reorder', methods=['POST'])
@login_required
def reorder_line_item(quote_id, item_id):
    db = get_db()
    data = request.json
    direction = data.get('direction', 1)
    row = db.execute("SELECT is_optional FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    is_optional = 1 if row['is_optional'] else 0
    items = db.execute(
        "SELECT id, sort_order FROM quote_line_items WHERE quote_id=? AND COALESCE(is_optional,0)=? ORDER BY sort_order",
        (quote_id, is_optional)
    ).fetchall()
    ids = [r['id'] for r in items]
    if item_id not in ids:
        return jsonify({'error': 'Not found'}), 404
    idx = ids.index(item_id)
    swap_idx = idx + direction
    if swap_idx < 0 or swap_idx >= len(ids):
        return jsonify({'success': True})
    swap_id = ids[swap_idx]
    my_sort = items[idx]['sort_order']
    swap_sort = items[swap_idx]['sort_order']
    db.execute("UPDATE quote_line_items SET sort_order=? WHERE id=?", (swap_sort, item_id))
    db.execute("UPDATE quote_line_items SET sort_order=? WHERE id=?", (my_sort, swap_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/update', methods=['POST'])
@login_required
def update_line_item(quote_id, item_id):
    db = get_db()
    data = request.json
    field = data.get('field')
    value = data.get('value')
    row = db.execute("SELECT * FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    can_override = bool(g.role and g.role['can_override_min_markup'])

    if field == 'sub':
        sub_name = ''
        if value:
            s = db.execute("SELECT name FROM subs WHERE sub_id=?", (value,)).fetchone()
            if not s: s = db.execute("SELECT name FROM surface_applicators WHERE sub_id=?", (value,)).fetchone()
            if s: sub_name = s['name']
        # Try to get rate for this sub + work type
        rate_row = db.execute("SELECT rate FROM sub_rates WHERE sub_id=? AND work_type_id=?",
                              (value, row['work_type_id'])).fetchone()
        new_cpu = float(rate_row['rate']) if rate_row else float(row['labor_cost_per_unit'])
        from line_item_logic import calc_component
        qty = float(row['labor_quantity'])
        markup = float(row['labor_markup_pct'])
        min_m = float(row['labor_min_markup'])
        l_cost, l_price, l_margin = calc_component(new_cpu, qty, markup, min_m, can_override)
        total_cost = round(l_cost + float(row['material_total_cost']), 2)
        total_price = round(l_price + float(row['material_total_price']), 2)
        total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
        db.execute("""UPDATE quote_line_items SET sub_id=?,sub_name=?,labor_cost_per_unit=?,
                      labor_total_cost=?,labor_total_price=?,labor_margin_pct=?,
                      total_cost=?,total_price=?,total_margin_pct=? WHERE id=?""",
                   (value, sub_name, new_cpu, l_cost, l_price, l_margin,
                    total_cost, total_price, total_margin, item_id))

    elif field in ('labor_qty', 'labor_cpu', 'mat_qty', 'mat_cpu'):
        from line_item_logic import calc_component
        l_qty = float(value) if field == 'labor_qty' else float(row['labor_quantity'])
        l_cpu = float(value) if field == 'labor_cpu' else float(row['labor_cost_per_unit'])
        m_qty = float(value) if field == 'mat_qty' else float(row['material_quantity'] or 0)
        m_cpu = float(value) if field == 'mat_cpu' else float(row['material_cost_per_unit'] or 0)
        l_markup = float(row['labor_markup_pct'])
        l_min = float(row['labor_min_markup'])
        m_markup = float(row['material_markup_pct'] or 30)
        m_min = float(row['material_min_markup'] or 10)
        l_cost, l_price, l_margin = calc_component(l_cpu, l_qty, l_markup, l_min, can_override)
        m_cost, m_price, m_margin = calc_component(m_cpu, m_qty, m_markup, m_min, can_override)
        total_cost = round(l_cost + m_cost, 2)
        total_price = round(l_price + m_price, 2)
        total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
        col_map = {'labor_qty':'labor_quantity','labor_cpu':'labor_cost_per_unit',
                   'mat_qty':'material_quantity','mat_cpu':'material_cost_per_unit'}
        update_col = col_map[field]
        db.execute(f"""UPDATE quote_line_items SET {update_col}=?,
                       labor_total_cost=?,labor_total_price=?,labor_margin_pct=?,
                       material_total_cost=?,material_total_price=?,material_margin_pct=?,
                       total_cost=?,total_price=?,total_margin_pct=? WHERE id=?""",
                   (float(value), l_cost, l_price, l_margin,
                    m_cost, m_price, m_margin,
                    total_cost, total_price, total_margin, item_id))

    elif field == 'material':
        material_id = int(value) if value else None
        mat_cpu = float(data.get('cost', 0) or 0)
        material_label = ''
        if material_id:
            m = db.execute("""SELECT m.series, s.name as supplier_name FROM materials m
                              JOIN suppliers s ON m.supplier_id=s.supplier_id
                              WHERE m.material_id=?""", (material_id,)).fetchone()
            if m:
                material_label = f"{m['supplier_name']} — {m['series']}"
        from line_item_logic import calc_component
        qty = float(row['labor_quantity'])
        markup = float(row['labor_markup_pct'])
        min_m = float(row['material_min_markup'] or 10)
        m_cost, m_price, m_margin = calc_component(mat_cpu, qty, markup, min_m, can_override)
        l_cost = float(row['labor_total_cost'])
        l_price = float(row['labor_total_price'])
        total_cost = round(l_cost + m_cost, 2)
        total_price = round(l_price + m_price, 2)
        total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
        db.execute("""UPDATE quote_line_items SET
                      material_id=?, material_label=?, material_cost_per_unit=?,
                      material_total_cost=?, material_total_price=?, material_margin_pct=?,
                      total_cost=?, total_price=?, total_margin_pct=? WHERE id=?""",
                   (material_id, material_label, mat_cpu, m_cost, m_price, m_margin,
                    total_cost, total_price, total_margin, item_id))

    elif field == 'description':
        db.execute("UPDATE quote_line_items SET description=? WHERE id=?", (value or '', item_id))

    elif field == 'surface_product':
        product_id = value
        sub_id = data.get('sub_id') or row['sub_id']
        p = db.execute("""SELECT sar.rate, sar.min_sqft, sp.finish, sm.manufacturer_name, sp.product_line
                          FROM surface_applicator_rates sar
                          JOIN surface_products sp ON sar.product_id=sp.product_id
                          JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id
                          WHERE sar.product_id=? AND sar.sub_id=?""", (product_id, sub_id)).fetchone()
        applicator = db.execute("SELECT * FROM surface_applicators WHERE sub_id=?", (sub_id,)).fetchone()
        if p:
            from line_item_logic import calc_component

            # Most applicators price off the wetted/expanded area already stored on the line
            # item (labor_quantity). A few (e.g. Southwest Pool Finishers) price off the flat
            # water surface area instead, entered separately on the quote.
            if applicator and applicator['uses_water_surface_area']:
                quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
                actual_qty = float(quote['water_surface_sqft'] or 0)
            else:
                actual_qty = float(row['labor_quantity'])

            min_sqft = float(p['min_sqft'] or 0)
            effective_qty = max(actual_qty, min_sqft) if min_sqft else actual_qty
            base_cost = effective_qty * float(p['rate'])

            depth_surcharge_cost = 0.0
            if applicator and applicator['depth_surcharge_per_sqft_per_ft']:
                quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
                threshold = float(applicator['depth_surcharge_threshold_ft'] or 0)
                depth_over = max(0.0, float(quote['pool_deep'] or 0) - threshold)
                depth_surcharge_cost = actual_qty * float(applicator['depth_surcharge_per_sqft_per_ft']) * depth_over

            combined_cost = base_cost + depth_surcharge_cost
            cpu = round(combined_cost / actual_qty, 4) if actual_qty else float(p['rate'])

            markup = float(row['labor_markup_pct'])
            min_m = float(row['labor_min_markup'])
            l_cost, l_price, l_margin = calc_component(cpu, actual_qty, markup, min_m, can_override)
            label = f"{p['manufacturer_name']} {p['product_line']} – {p['finish']}"
            additive_label = data.get('additive_label', '')
            if additive_label:
                label += f" with {additive_label}"
            db.execute("""UPDATE quote_line_items SET product_id=?,product_label=?,
                          labor_quantity=?,labor_cost_per_unit=?,labor_total_cost=?,labor_total_price=?,
                          labor_margin_pct=?,total_cost=?,total_price=?,total_margin_pct=? WHERE id=?""",
                       (product_id, label, actual_qty, cpu, l_cost, l_price, l_margin,
                        l_cost, l_price, l_margin, item_id))

    if field != 'description':
        _rollup_item_totals(db, item_id)
    db.commit()
    _recalc_quote(db, quote_id)
    updated = db.execute("SELECT * FROM quote_line_items WHERE id=?", (item_id,)).fetchone()
    q = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    gross = float(q['total_price']) - float(q['total_cost'])
    commission = float(q['commission'])
    return jsonify({
        'labor_quantity': float(updated['labor_quantity'] or 0),
        'labor_total_price': float(updated['labor_total_price']),
        'labor_cost_per_unit': float(updated['labor_cost_per_unit'] or 0),
        'total_margin_pct': float(updated['total_margin_pct']),
        'total_cost': float(updated['total_cost']),
        'total_price': float(updated['total_price']),
        'material_total_price': float(updated['material_total_price'] or 0),
        'material_total_cost': float(updated['material_total_cost'] or 0),
        'material_margin_pct': float(updated['material_margin_pct'] or 0),
        'product_label': updated['product_label'] or '',
        'material_label': updated['material_label'] or '',
        'description': updated['description'] or '',
        'quote_total_cost': float(q['total_cost']),
        'quote_total_price': float(q['total_price']),
        'quote_total_margin': float(q['total_margin']),
        'quote_gross_profit': round(gross, 2),
        'quote_commission': round(commission, 2),
        'quote_net_profit': round(gross - commission, 2),
    })

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/qualifiers/toggle', methods=['POST'])
@login_required
def toggle_qualifier(quote_id, item_id):
    db = get_db()
    data = request.json
    qualifier_id = int(data.get('qualifier_id'))
    checked = bool(data.get('checked'))
    row = db.execute("SELECT qualifiers_json FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    quals = json.loads(row['qualifiers_json'] or '[]')
    quals = [q for q in quals if q['id'] != qualifier_id]
    qrow = db.execute("SELECT * FROM qualifiers WHERE qualifier_id=?", (qualifier_id,)).fetchone()
    if checked:
        if qrow:
            quals.append({'id': qualifier_id, 'label': qrow['label'], 'amount': float(qrow['amount'])})
    qualifiers_total_cost = round(sum(q['amount'] for q in quals), 2)
    db.execute("UPDATE quote_line_items SET qualifiers_json=?,qualifiers_total_cost=? WHERE id=?",
               (json.dumps(quals), qualifiers_total_cost, item_id))
    total_cost, total_price, total_margin = _rollup_item_totals(db, item_id)

    # Freight can only be charged once per quote — checking it here clears it from
    # any other line item on this same quote.
    other_item_cleared = False
    if checked and qrow and qrow['is_freight']:
        other_items = db.execute(
            "SELECT id, qualifiers_json FROM quote_line_items WHERE quote_id=? AND id!=?",
            (quote_id, item_id)
        ).fetchall()
        for other in other_items:
            other_quals = json.loads(other['qualifiers_json'] or '[]')
            other_freight_ids = {q['id'] for q in other_quals} & {
                r['qualifier_id'] for r in db.execute(
                    "SELECT qualifier_id FROM qualifiers WHERE is_freight=1"
                ).fetchall()
            }
            if other_freight_ids:
                filtered = [q for q in other_quals if q['id'] not in other_freight_ids]
                filtered_cost = round(sum(q['amount'] for q in filtered), 2)
                db.execute("UPDATE quote_line_items SET qualifiers_json=?,qualifiers_total_cost=? WHERE id=?",
                           (json.dumps(filtered), filtered_cost, other['id']))
                _rollup_item_totals(db, other['id'])
                other_item_cleared = True

    db.commit()
    _recalc_quote(db, quote_id)
    q = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    gross = float(q['total_price']) - float(q['total_cost'])
    commission = float(q['commission'])
    return jsonify({
        'qualifiers': quals,
        'total_cost': total_cost,
        'total_price': total_price,
        'total_margin_pct': total_margin,
        'reload': other_item_cleared,
        'quote_total_cost': float(q['total_cost']),
        'quote_total_price': float(q['total_price']),
        'quote_total_margin': float(q['total_margin']),
        'quote_gross_profit': round(gross, 2),
        'quote_commission': round(commission, 2),
        'quote_net_profit': round(gross - commission, 2),
    })

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/tax/toggle', methods=['POST'])
@login_required
def toggle_tax(quote_id, item_id):
    db = get_db()
    data = request.json
    checked = bool(data.get('checked'))
    row = db.execute("SELECT id FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE quote_line_items SET tax_included=? WHERE id=?", (1 if checked else 0, item_id))
    total_cost, total_price, total_margin = _rollup_item_totals(db, item_id)
    db.commit()
    _recalc_quote(db, quote_id)
    q = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    gross = float(q['total_price']) - float(q['total_cost'])
    commission = float(q['commission'])
    return jsonify({
        'tax_included': checked,
        'total_cost': total_cost,
        'total_price': total_price,
        'total_margin_pct': total_margin,
        'quote_total_cost': float(q['total_cost']),
        'quote_total_price': float(q['total_price']),
        'quote_total_margin': float(q['total_margin']),
        'quote_gross_profit': round(gross, 2),
        'quote_commission': round(commission, 2),
        'quote_net_profit': round(gross - commission, 2),
    })

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: QUALIFIERS
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/qualifiers')
@require_permission('can_edit_work_types')
def admin_qualifiers():
    db = get_db()
    qualifiers = db.execute("""SELECT q.*, wt.work_type FROM qualifiers q
                               JOIN work_types wt ON q.work_type_id=wt.work_type_id
                               ORDER BY wt.work_type, q.label""").fetchall()
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    return render_template('admin_qualifiers.html', qualifiers=qualifiers, work_types=work_types)

@app.route('/admin/qualifiers/add', methods=['POST'])
@require_permission('can_edit_work_types')
def add_qualifier():
    db = get_db()
    db.execute("INSERT INTO qualifiers (work_type_id,label,amount,active) VALUES (?,?,?,'Y')",
               (request.form['work_type_id'], request.form['label'], float(request.form.get('amount', 0) or 0)))
    db.commit()
    return redirect(url_for('admin_qualifiers'))

@app.route('/admin/qualifiers/edit/<int:qualifier_id>', methods=['POST'])
@require_permission('can_edit_work_types')
def edit_qualifier(qualifier_id):
    db = get_db()
    db.execute("UPDATE qualifiers SET label=?,amount=?,active=? WHERE qualifier_id=?",
               (request.form['label'], float(request.form.get('amount', 0) or 0),
                request.form.get('active', 'Y'), qualifier_id))
    db.commit()
    return redirect(url_for('admin_qualifiers'))

@app.route('/admin/qualifiers/delete/<int:qualifier_id>', methods=['POST'])
@require_permission('can_edit_work_types')
def delete_qualifier(qualifier_id):
    db = get_db()
    db.execute("DELETE FROM qualifiers WHERE qualifier_id=?", (qualifier_id,))
    db.commit()
    return redirect(url_for('admin_qualifiers'))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: SURFACE ADDITIVES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/surface_additives/add', methods=['POST'])
@require_permission('can_edit_surfaces')
def add_surface_additive():
    db = get_db()
    db.execute("INSERT INTO surface_additives (label,active) VALUES (?,'Y')", (request.form['label'],))
    db.commit()
    return redirect(url_for('admin_surfaces'))

@app.route('/admin/surface_additives/delete/<int:additive_id>', methods=['POST'])
@require_permission('can_edit_surfaces')
def delete_surface_additive(additive_id):
    db = get_db()
    db.execute("UPDATE surface_additives SET active='N' WHERE additive_id=?", (additive_id,))
    db.commit()
    return redirect(url_for('admin_surfaces'))

if __name__ == '__main__':
    init_db()
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, port=int(os.environ.get('PORT', 5000)))

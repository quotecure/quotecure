import json
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, g, Response
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from database import init_db, init_auth, init_materials, init_company_settings, generate_payment_schedule, run_migrations, init_pebble_pros_surfaces, init_skimmer_material, init_cap_tile_trim_materials, init_finishing_flooring_pros_sub, get_db
from line_item_logic import build_line_item, calc_component
import hashlib
import secrets

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
        init_skimmer_material(db)
        init_cap_tile_trim_materials(db)
        init_finishing_flooring_pros_sub(db)
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
LOCKOUT_THRESHOLD = 3

def _send_lockout_email(db, user):
    """Best-effort -- a missing email on file or unconfigured Gmail creds just means the
    account stays locked until an Owner/Coordinator clears it from /admin/locked_accounts;
    it never blocks the lockout itself from taking effect."""
    if not user['email']:
        return
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    gmail_address = settings['gmail_address'] if settings else ''
    gmail_app_password = settings['gmail_app_password'] if settings else ''
    if not (gmail_address and gmail_app_password):
        return
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO password_resets (token, user_id, created_at) VALUES (?,?,now()::text)",
               (token, user['user_id']))
    db.commit()
    reset_url = url_for('reset_password', token=token, _external=True)
    try:
        _send_plain_email(
            user['email'],
            'QuoteCure account locked',
            (f"Hi {user['display_name'] or user['username']},\n\n"
             f"Your QuoteCure account was locked after {LOCKOUT_THRESHOLD} incorrect password attempts in a row. "
             f"If that wasn't you, no action is needed on their part -- but you'll need to set a new password to log in again.\n\n"
             f"Reset your password: {reset_url}\n\n"
             f"This link works once and expires in 1 hour."),
            gmail_address, gmail_app_password
        )
    except Exception:
        pass

@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        db = get_db()
        candidate = db.execute("SELECT * FROM users WHERE username=? AND active=1",
                               (request.form['username'],)).fetchone()
        if candidate and candidate['locked_at']:
            error = 'This account is locked. Check your email for a reset link, or ask an admin to unlock it.'
        elif candidate and check_pw(request.form['password'], candidate['password']):
            if not candidate['password'].startswith(('pbkdf2:', 'scrypt:')):
                db.execute("UPDATE users SET password=? WHERE user_id=?",
                           (hash_pw(request.form['password']), candidate['user_id']))
            if candidate['failed_attempts']:
                db.execute("UPDATE users SET failed_attempts=0 WHERE user_id=?", (candidate['user_id'],))
            db.commit()
            session['user_id'] = candidate['user_id']
            return redirect(url_for('quotes_list'))
        elif candidate:
            attempts = (candidate['failed_attempts'] or 0) + 1
            if attempts >= LOCKOUT_THRESHOLD:
                db.execute("UPDATE users SET failed_attempts=?, locked_at=now()::text WHERE user_id=?",
                           (attempts, candidate['user_id']))
                db.commit()
                _send_lockout_email(db, candidate)
                error = 'This account is locked. Check your email for a reset link, or ask an admin to unlock it.'
            else:
                db.execute("UPDATE users SET failed_attempts=? WHERE user_id=?", (attempts, candidate['user_id']))
                db.commit()
                error = 'Invalid username or password'
        else:
            error = 'Invalid username or password'
    return render_template('login.html', error=error)

@app.route('/reset_password/<token>', methods=['GET','POST'])
def reset_password(token):
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_resets WHERE token=? AND used=0 AND created_at > (now() - interval '1 hour')::text",
        (token,)
    ).fetchone()
    if not row:
        return render_template('reset_password.html', error='This reset link is invalid or has expired.', token=None)
    if request.method == 'POST':
        new_pw = request.form.get('new_password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        if len(new_pw) < 8:
            return render_template('reset_password.html', error='Password must be at least 8 characters.', token=token)
        if new_pw != confirm:
            return render_template('reset_password.html', error='Passwords do not match.', token=token)
        db.execute("UPDATE users SET password=?, failed_attempts=0, locked_at='' WHERE user_id=?",
                   (hash_pw(new_pw), row['user_id']))
        db.execute("UPDATE password_resets SET used=1 WHERE token=?", (token,))
        db.commit()
        return redirect(url_for('login'))
    return render_template('reset_password.html', error=None, token=token)

@app.route('/admin/locked_accounts')
@require_permission('can_access_admin')
def admin_locked_accounts():
    db = get_db()
    locked = db.execute(
        "SELECT u.*, r.role_name FROM users u JOIN roles r ON u.role_id=r.role_id "
        "WHERE u.locked_at != '' ORDER BY u.locked_at DESC"
    ).fetchall()
    return render_template('admin_locked_accounts.html', locked=locked)

@app.route('/admin/locked_accounts/<int:user_id>/unlock', methods=['POST'])
@require_permission('can_access_admin')
def unlock_account(user_id):
    db = get_db()
    db.execute("UPDATE users SET failed_attempts=0, locked_at='' WHERE user_id=?", (user_id,))
    db.commit()
    return redirect(url_for('admin_locked_accounts'))

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
    tab = request.args.get('tab', 'quotes')
    if tab == 'contracts':
        quotes = db.execute(
            "SELECT * FROM quotes WHERE status IN ('contract','in_progress','complete') ORDER BY signed_at DESC"
        ).fetchall()
    else:
        tab = 'quotes'
        quotes = db.execute(
            "SELECT * FROM quotes WHERE status='draft' OR status='sent' ORDER BY created_at DESC"
        ).fetchall()
    commission_policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    return render_template('quotes.html', quotes=quotes, commission_policy=commission_policy, active_tab=tab)

@app.route('/customers')
@login_required
def customers_list():
    db = get_db()
    customers = db.execute(
        "SELECT c.*, COUNT(q.quote_id) as quote_count "
        "FROM customers c LEFT JOIN quotes q ON q.customer_id=c.customer_id "
        "GROUP BY c.customer_id ORDER BY c.name"
    ).fetchall()
    return render_template('customers.html', customers=customers)

@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    db = get_db()
    name = request.form.get('name','').strip()
    if name:
        db.execute("INSERT INTO customers (name,address,email,phone) VALUES (?,?,?,?)",
                   (name, request.form.get('address','').strip(), request.form.get('email','').strip(),
                    request.form.get('phone','').strip()))
        db.commit()
    return redirect(url_for('customers_list'))

@app.route('/customers/<int:customer_id>')
@login_required
def customer_detail(customer_id):
    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if not customer:
        return redirect(url_for('customers_list'))
    quotes = db.execute(
        "SELECT * FROM quotes WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)
    ).fetchall()
    timeline = _customer_timeline(db, customer_id)
    return render_template('customer_detail.html', customer=customer, quotes=quotes, timeline=timeline)

def _customer_timeline(db, customer_id):
    """Notes and attachments are two separate tables (plain text vs. base64 blob+mime) but
    always shown merged as one chronological history -- the split is a storage detail, not
    something the UI exposes."""
    notes = db.execute(
        "SELECT id, 'note' as kind, note_text, NULL as filename, NULL as mime_type, NULL as file_data, "
        "created_by, created_at FROM customer_notes WHERE customer_id=?", (customer_id,)
    ).fetchall()
    files = db.execute(
        "SELECT id, 'file' as kind, NULL as note_text, filename, mime_type, file_data, "
        "created_by, created_at FROM customer_attachments WHERE customer_id=?", (customer_id,)
    ).fetchall()
    combined = [dict(r) for r in notes] + [dict(r) for r in files]
    combined.sort(key=lambda r: r['created_at'] or '', reverse=True)
    return combined

@app.route('/customers/<int:customer_id>/notes/add', methods=['POST'])
@login_required
def add_customer_note(customer_id):
    db = get_db()
    note_text = request.form.get('note_text', '').strip()
    if note_text:
        author = g.user['display_name'] if g.user and g.user['display_name'] else (g.user['username'] if g.user else '')
        db.execute("INSERT INTO customer_notes (customer_id, note_text, created_by) VALUES (?,?,?)",
                   (customer_id, note_text, author))
        db.commit()
    return redirect(url_for('customer_detail', customer_id=customer_id))

@app.route('/customers/<int:customer_id>/notes/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_customer_note(customer_id, note_id):
    db = get_db()
    db.execute("DELETE FROM customer_notes WHERE id=? AND customer_id=?", (note_id, customer_id))
    db.commit()
    return redirect(url_for('customer_detail', customer_id=customer_id))

@app.route('/customers/<int:customer_id>/attachments/add', methods=['POST'])
@login_required
def add_customer_attachment(customer_id):
    db = get_db()
    f = request.files.get('file')
    if f and f.filename:
        file_bytes = f.read()
        ext = _validate_file_upload(file_bytes, f.filename)
        if ext:
            import base64
            author = g.user['display_name'] if g.user and g.user['display_name'] else (g.user['username'] if g.user else '')
            db.execute(
                "INSERT INTO customer_attachments (customer_id, filename, mime_type, file_data, created_by) VALUES (?,?,?,?,?)",
                (customer_id, f.filename, ATTACHMENT_MIME_TYPES[ext], base64.b64encode(file_bytes).decode('utf-8'), author)
            )
            db.commit()
    return redirect(url_for('customer_detail', customer_id=customer_id))

@app.route('/customers/attachments/<int:attachment_id>/download')
@login_required
def download_customer_attachment(attachment_id):
    db = get_db()
    row = db.execute("SELECT * FROM customer_attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        return redirect(url_for('customers_list'))
    import base64
    data = base64.b64decode(row['file_data'])
    return Response(data, mimetype=row['mime_type'],
                     headers={'Content-Disposition': f'attachment; filename="{row["filename"]}"'})

@app.route('/customers/<int:customer_id>/attachments/<int:attachment_id>/delete', methods=['POST'])
@login_required
def delete_customer_attachment(customer_id, attachment_id):
    db = get_db()
    db.execute("DELETE FROM customer_attachments WHERE id=? AND customer_id=?", (attachment_id, customer_id))
    db.commit()
    return redirect(url_for('customer_detail', customer_id=customer_id))

@app.route('/customers/<int:customer_id>/edit', methods=['POST'])
@login_required
def edit_customer(customer_id):
    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if not customer:
        return redirect(url_for('customers_list'))
    name = request.form.get('name','').strip()
    email = request.form.get('email','').strip()
    phone = request.form.get('phone','').strip()
    address = request.form.get('address','').strip()
    if name:
        _cascade_customer_contact(db, customer_id, name, email, phone)
    # Address is customer-record-only (see _cascade_customer_contact) -- never pushed to quotes.
    db.execute("UPDATE customers SET address=? WHERE customer_id=?", (address, customer_id))
    db.commit()
    return redirect(url_for('customer_detail', customer_id=customer_id))

def _dims_totals(dims):
    """Shared lf/sqft totals from a dims dict (works for both real quotes.Row and plain dicts)."""
    total_lf = ((dims.get('pool_perimeter') or 0) + (dims.get('spa_perimeter') if dims.get('has_spa') else 0)
                + (dims.get('steps_lf') or 0) + (dims.get('benches_lf') or 0) + (dims.get('swimouts_lf') or 0))
    total_sqft = dims.get('total_surface_sqft') or dims.get('pool_sqft') or 0
    return total_lf, total_sqft

def _apply_min_job_price(db, work_type_id, total_cost, total_price):
    """Enforce a work type's flat-dollar minimum job price, if one is set -- keeps a
    deliberately thin-margin work type (e.g. a loss-leader) from pricing an individual job
    below what it costs to run, regardless of markup. Returns (total_cost, total_price, total_margin).
    Skipped for a negative total_price -- a floor is meaningless for a credit/offset line
    (see 'replacement items' in Optional Add-Ons) and would otherwise clamp it back up to a
    positive number, silently destroying the credit."""
    wt = db.execute("SELECT min_job_price FROM work_types WHERE work_type_id=?", (work_type_id,)).fetchone()
    floor = float(wt['min_job_price']) if wt and wt['min_job_price'] else 0
    if floor and total_price >= 0 and total_price < floor:
        total_price = floor
    total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
    return total_cost, total_price, total_margin

DECK_SQFT_WORK_TYPES = {
    'Paver Installation', 'Paver Sealing', 'Flagstone Pavers',
    'Textured Decking – Knockdown', 'Textured Decking – Variegated',
}
# Exposed to Jinja so templates can flag these work types (e.g. a data-deck attribute on
# the manual Add Item form's work-type options) without duplicating this list a second
# time -- one set, shared by the automatic pricing path below and any template that needs it.
app.jinja_env.globals['DECK_SQFT_WORK_TYPES'] = DECK_SQFT_WORK_TYPES

def _compute_line_item_pricing(db, wt, default_sub_id, default_material_id, dims, can_override):
    """Full pricing for one work-type item given a sub/material default and quote dimensions.
    wt: dict with work_type_id, work_type, unit, cost_structure, default_markup, min_markup, description.
    dims: dict with pool_perimeter, has_spa, spa_perimeter, steps_lf, benches_lf, swimouts_lf,
          total_surface_sqft (or pool_sqft), has_shelf, deck_sqft (Paver Installation/Sealing only).
    A sub_rates row can also carry a min_total_cost -- a real minimum charge from that sub
    for that work type (e.g. Pro Hydroblasters' $2,600 Surface Removal minimum), applied
    after the normal rate*qty calculation, price rescaled at the same markup% -- and a
    markup_pct override, letting one sub carry a different margin than others doing the
    same work type (e.g. Robert's 100% Surface Prep markup vs. another sub's default).
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
            "SELECT rate, unit, min_total_cost, markup_pct FROM sub_rates WHERE sub_id=? AND work_type_id=?",
            (default_sub_id, wt_id)
        ).fetchone()
        if rate_row:
            labor_cpu = float(rate_row['rate'])

    wt_unit = wt.get('unit', 'each')
    unit = rate_row['unit'] if rate_row else wt_unit
    total_lf, total_sqft = _dims_totals(dims)
    if wt.get('work_type', '') in DECK_SQFT_WORK_TYPES:
        # Paver Installation/Sealing, Textured Decking (Knockdown/Variegated) -- deck sqft,
        # not the pool's own interior surface sqft. Matched by name, not ID, since the
        # Textured Decking work types are seeded by a later migration and don't have a
        # guaranteed-stable ID across environments the way the original seeded catalog does.
        qty = float(dims.get('deck_sqft') or 0)
    elif unit == 'sqft':
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

    # A sub can carry its own markup% (sub_rates.markup_pct), overriding the work type's
    # shared default -- e.g. Robert's Surface Prep prices at 100% markup while another sub
    # doing the same work type keeps a different margin. NULL (the common case) falls back
    # to the work type's default_markup exactly as before.
    if rate_row and rate_row['markup_pct'] is not None:
        markup = float(rate_row['markup_pct'])
    else:
        markup = float(wt.get('default_markup', 30))
    min_markup = float(wt.get('min_markup', 10))
    l_cost, l_price, l_margin = calc_component(labor_cpu, qty, markup, min_markup, can_override)
    # A sub-specific cost floor (e.g. Pro Hydroblasters always charges at least $2,600 for
    # Surface Removal, no matter how small the pool) -- represents a real minimum charge
    # from the sub, not a markup policy, so it's not can_override-bypassable like
    # min_markup/min_job_price are. Rescales price at the same markup% so margin doesn't
    # erode just because a small job got floored up to the sub's real minimum.
    if rate_row and rate_row['min_total_cost'] and l_cost < float(rate_row['min_total_cost']):
        l_cost = float(rate_row['min_total_cost'])
        l_price = round(l_cost * (1 + markup / 100), 2)
    m_cost, m_price, m_margin = calc_component(mat_cpu, mat_qty, markup, 10, can_override)
    total_cost = round(l_cost + m_cost, 2)
    total_price = round(l_price + m_price, 2)
    total_cost, total_price, total_margin = _apply_min_job_price(db, wt_id, total_cost, total_price)

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

def _contract_effective_items(db, quote_id):
    """Current effective state of each work type on a contract: the original signed
    line item, unless a later SIGNED Change Order added or removed it, in which case
    the latest signed CO's version wins. Draft/unsent COs never count toward this.
    Amend isn't a separate case -- it's just a 'remove' row for the old spec followed
    by an 'add' row for the new spec, both tagged to the same work_type_id, so walking
    add/remove in signed order handles it automatically. Returns {work_type_id: row_dict}.
    Freeform CO lines never key by work_type_id and aren't part of this resolver."""
    rows = db.execute(
        "SELECT * FROM quote_line_items WHERE quote_id=? AND (is_optional=0 OR is_optional IS NULL)",
        (quote_id,)
    ).fetchall()
    effective = {r['work_type_id']: dict(r) for r in rows}
    co_items = db.execute(
        "SELECT coi.* FROM change_order_items coi "
        "JOIN change_orders co ON coi.change_order_id = co.id "
        "WHERE co.quote_id=? AND co.status='signed' AND coi.kind='catalog' "
        "ORDER BY co.co_number, coi.sort_order",
        (quote_id,)
    ).fetchall()
    for item in co_items:
        wt_id = item['work_type_id']
        if item['action'] == 'remove':
            effective.pop(wt_id, None)
        elif item['action'] == 'add':
            effective[wt_id] = dict(item)
    return effective

def _ledger_items(db, quote_id):
    """Every work item on a signed contract that needs actual-cost tracking: the current
    effective catalog items (_contract_effective_items) plus every freeform line from
    signed Change Orders (freeform items aren't keyed by work_type_id, so the resolver
    doesn't cover them). Each dict gets 'source' ('quote_line_item' or 'change_order_item',
    telling the save route which table to UPDATE) and 'has_material' (whether the original
    quote priced a material component at all -- freeform items never do -- so the UI shows
    one actual-cost field or two, mirroring the split the item already has)."""
    def _tag(row):
        d = dict(row)
        d['source'] = 'change_order_item' if 'change_order_id' in d else 'quote_line_item'
        d['has_material'] = float(d.get('material_total_cost') or 0) > 0
        return d

    items = [_tag(row) for row in _contract_effective_items(db, quote_id).values()]
    freeform = db.execute(
        "SELECT coi.* FROM change_order_items coi "
        "JOIN change_orders co ON coi.change_order_id = co.id "
        "WHERE co.quote_id=? AND co.status='signed' AND coi.kind='freeform' "
        "ORDER BY co.co_number, coi.sort_order",
        (quote_id,)
    ).fetchall()
    items += [_tag(row) for row in freeform]
    return items

def _ledger_item_actuals(item):
    """(actual_labor, actual_material, actual_total, fully_entered) for one ledger item.
    Falls back to the quoted amount for any component not yet entered, so the running total
    is always a current best estimate rather than all-or-nothing -- an item with a labor
    invoice in but no material invoice yet still contributes its real labor cost.

    Freeform Change Order items never populate labor_total_cost/material_total_cost (only
    total_cost/total_price -- see add_freeform_change_order_item) but still surface a single
    actual-cost field via has_material=False, same as any other one-part item. Detect that
    "blended" shape and fall back to total_cost instead of the (always-zero) labor_total_cost,
    or the freeform item would silently price at $0 until its actual is entered."""
    quoted_labor = float(item.get('labor_total_cost') or 0)
    quoted_material = float(item.get('material_total_cost') or 0)
    quoted_total = float(item.get('total_cost') or 0)
    if quoted_labor == 0 and quoted_material == 0 and quoted_total != 0:
        quoted_labor = quoted_total
    actual_labor = item.get('actual_labor_cost')
    actual_material = item.get('actual_material_cost')
    labor_entered = actual_labor is not None
    material_entered = (actual_material is not None) or not item['has_material']
    resolved_labor = float(actual_labor) if labor_entered else quoted_labor
    resolved_material = float(actual_material) if actual_material is not None else quoted_material
    total = resolved_labor + (resolved_material if item['has_material'] else 0)
    return resolved_labor, resolved_material, round(total, 2), (labor_entered and material_entered)

def _ledger_totals(db, quote_id, items):
    """Job-level ledger summary: quoted vs running-actual cost/diff/margin/tier/gross-profit,
    and Expected vs Actual commission. Expected commission is whatever's already frozen on
    the quote + its signed Change Orders (never recomputed here); quoted_tier/margin are
    instead freshly computed off the blended quoted cost/price, so on a contract with a
    Change Order at a very different margin than the original quote, the tier badge and the
    frozen commission figure can technically disagree slightly -- same known tier-blending
    nuance as expected_commission vs actual_commission below. Actual commission/tier/margin
    all rerun fresh against the running actual cost, holding the signed sale price fixed --
    price doesn't change after signing, only cost does."""
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    signed_cos = db.execute(
        "SELECT total_cost, total_price, commission FROM change_orders WHERE quote_id=? AND status='signed'",
        (quote_id,)
    ).fetchall()

    total_quoted_cost = float(quote['total_cost'] or 0) + sum(float(c['total_cost'] or 0) for c in signed_cos)
    total_price = float(quote['total_price'] or 0) + sum(float(c['total_price'] or 0) for c in signed_cos)
    expected_commission = float(quote['commission'] or 0) + sum(float(c['commission'] or 0) for c in signed_cos)

    total_actual_cost = 0.0
    entered_count = 0
    for item in items:
        _, _, item_total, fully_entered = _ledger_item_actuals(item)
        total_actual_cost += item_total
        if fully_entered:
            entered_count += 1

    policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    actual_commission = _compute_commission(policy, total_actual_cost, total_price)

    quoted_gross_profit = total_price - total_quoted_cost
    actual_gross_profit = total_price - total_actual_cost
    quoted_margin_pct = (quoted_gross_profit / total_price * 100) if total_price else 0
    actual_margin_pct = (actual_gross_profit / total_price * 100) if total_price else 0

    def tier_for(margin_pct):
        # Tier is a tiered_gp-specific concept -- flat-rate policies (pct_of_price/
        # pct_of_margin) don't have one, so the UI hides the Tier row entirely for those.
        if not policy or policy['method'] != 'tiered_gp':
            return None
        if margin_pct < float(policy['tier1_threshold']):
            return 1
        elif margin_pct < float(policy['tier2_threshold']):
            return 2
        return 3

    return {
        'total_quoted_cost': round(total_quoted_cost, 2),
        'total_actual_cost': round(total_actual_cost, 2),
        'cost_diff': round(total_actual_cost - total_quoted_cost, 2),
        'total_price': round(total_price, 2),
        'quoted_gross_profit': round(quoted_gross_profit, 2),
        'actual_gross_profit': round(actual_gross_profit, 2),
        'gross_profit_diff': round(actual_gross_profit - quoted_gross_profit, 2),
        'quoted_margin_pct': round(quoted_margin_pct, 1),
        'actual_margin_pct': round(actual_margin_pct, 1),
        'quoted_tier': tier_for(quoted_margin_pct),
        'actual_tier': tier_for(actual_margin_pct),
        'expected_commission': round(expected_commission, 2),
        'actual_commission': round(actual_commission, 2),
        'commission_diff': round(actual_commission - expected_commission, 2),
        'entered_count': entered_count,
        'total_items': len(items),
        'pct_entered': round(entered_count / len(items) * 100) if items else 0,
    }

@app.route('/quotes/<int:quote_id>/ledger')
@login_required
def job_ledger(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote or not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    items = _ledger_items(db, quote_id)
    for item in items:
        item['actual_labor'], item['actual_material'], item['actual_total'], item['fully_entered'] = _ledger_item_actuals(item)
    totals = _ledger_totals(db, quote_id, items)
    can_edit = bool(g.role and g.role['can_enter_actuals'])
    return render_template('job_ledger.html', quote=quote, items=items, totals=totals, can_edit=can_edit)

@app.route('/quotes/<int:quote_id>/ledger/<int:item_id>/actual', methods=['POST'])
@require_permission('can_enter_actuals')
def save_ledger_actual(quote_id, item_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote or not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    source = request.form.get('source')
    if source == 'change_order_item':
        owned = db.execute(
            "SELECT coi.id FROM change_order_items coi JOIN change_orders co ON coi.change_order_id=co.id "
            "WHERE coi.id=? AND co.quote_id=?", (item_id, quote_id)
        ).fetchone()
        table = 'change_order_items'
    else:
        owned = db.execute("SELECT id FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
        table = 'quote_line_items'
    if owned:
        labor_raw = request.form.get('actual_labor_cost', '').strip()
        material_raw = request.form.get('actual_material_cost', '').strip()
        actual_labor = float(labor_raw) if labor_raw != '' else None
        actual_material = float(material_raw) if material_raw != '' else None
        entered_by = g.user['display_name'] if g.user and g.user['display_name'] else (g.user['username'] if g.user else '')
        db.execute(
            f"UPDATE {table} SET actual_labor_cost=?, actual_material_cost=?, actual_entered_by=?, actual_entered_at=now()::text WHERE id=?",
            (actual_labor, actual_material, entered_by, item_id)
        )
        db.commit()
    return redirect(url_for('job_ledger', quote_id=quote_id))

@app.route('/quotes/<int:quote_id>/schedule')
@login_required
def job_schedule(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote or not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    items = _schedule_items(db, quote_id)
    subs = db.execute("SELECT sub_id, name FROM subs WHERE active='Y' ORDER BY name").fetchall()
    can_edit = bool(g.role and g.role['can_enter_actuals'])
    return render_template('job_schedule.html', quote=quote, items=items, subs=subs, can_edit=can_edit)

def _owned_schedule_item(db, quote_id, item_id, source):
    """Same ownership-verification pattern as save_ledger_actual: confirms item_id actually
    belongs to this quote (directly, or via one of its change orders) before any schedule
    field gets written, and returns which table to write to."""
    if source == 'change_order_item':
        owned = db.execute(
            "SELECT coi.id FROM change_order_items coi JOIN change_orders co ON coi.change_order_id=co.id "
            "WHERE coi.id=? AND co.quote_id=?", (item_id, quote_id)
        ).fetchone()
        return ('change_order_items', bool(owned))
    owned = db.execute("SELECT id FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    return ('quote_line_items', bool(owned))

@app.route('/quotes/<int:quote_id>/schedule/<int:item_id>', methods=['POST'])
@require_permission('can_enter_actuals')
def save_schedule(quote_id, item_id):
    db = get_db()
    if not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    source = request.form.get('source')
    table, owned = _owned_schedule_item(db, quote_id, item_id, source)
    if owned:
        start_date = request.form.get('scheduled_start_date', '').strip()
        sub_id = request.form.get('sub_id', '').strip()
        sub_name = ''
        if sub_id:
            s = db.execute("SELECT name FROM subs WHERE sub_id=?", (sub_id,)).fetchone()
            sub_name = s['name'] if s else ''
        db.execute(
            f"UPDATE {table} SET scheduled_start_date=?, sub_id=?, sub_name=? WHERE id=?",
            (start_date, sub_id, sub_name, item_id)
        )
        db.commit()
    return redirect(url_for('job_schedule', quote_id=quote_id))

@app.route('/quotes/<int:quote_id>/schedule/<int:item_id>/actual', methods=['POST'])
@require_permission('can_enter_actuals')
def save_schedule_actual(quote_id, item_id):
    db = get_db()
    if not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    source = request.form.get('source')
    table, owned = _owned_schedule_item(db, quote_id, item_id, source)
    if owned:
        actual_start = request.form.get('actual_start_date', '').strip()
        actual_finish = request.form.get('actual_finish_date', '').strip()
        status = 'done' if actual_finish else ('in_progress' if actual_start else 'not_started')
        db.execute(
            f"UPDATE {table} SET actual_start_date=?, actual_finish_date=?, schedule_status=? WHERE id=?",
            (actual_start, actual_finish, status, item_id)
        )
        _maybe_advance_quote_status(db, quote_id)
        db.commit()
    return redirect(url_for('job_schedule', quote_id=quote_id))

@app.route('/quotes/<int:quote_id>/schedule/<int:item_id>/confirm', methods=['POST'])
@require_permission('can_enter_actuals')
def confirm_schedule(quote_id, item_id):
    db = get_db()
    if not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    source = request.form.get('source')
    table, owned = _owned_schedule_item(db, quote_id, item_id, source)
    if owned:
        item = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        confirmed_by = g.user['display_name'] if g.user and g.user['display_name'] else (g.user['username'] if g.user else '')
        db.execute(
            f"UPDATE {table} SET confirmed_at=now()::text, confirmed_by=? WHERE id=?",
            (confirmed_by, item_id)
        )
        db.commit()
        sub = db.execute("SELECT name, email FROM subs WHERE sub_id=?", (item['sub_id'],)).fetchone() if item['sub_id'] else None
        if sub and sub['email']:
            quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
            settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
            gmail_address = settings['gmail_address'] if settings else ''
            gmail_app_password = settings['gmail_app_password'] if settings else ''
            if gmail_address and gmail_app_password:
                try:
                    _send_plain_email(
                        sub['email'],
                        f"Scheduled: {item['work_type_label']} — {quote['customer_name']}",
                        (f"Hi {sub['name']},\n\n"
                         f"You're confirmed to start {item['work_type_label']} at {quote['customer_name']} "
                         f"on {item['scheduled_start_date']}.\n\n"
                         f"Thanks,\n{confirmed_by}"),
                        gmail_address, gmail_app_password
                    )
                except Exception:
                    pass
    return redirect(url_for('job_schedule', quote_id=quote_id))

@app.route('/schedule')
@login_required
def schedule_overview():
    """Every scheduled work item across every active contract, sorted by sub then start
    date -- deliberately a sorted list, not a calendar grid (no calendar UI exists anywhere
    in this app yet, and a list already surfaces the double-booking problem this exists to
    solve: two rows for the same sub with overlapping dates land right next to each other)."""
    db = get_db()
    quotes = db.execute(
        "SELECT quote_id, customer_name FROM quotes WHERE status IN ('contract','in_progress','complete')"
    ).fetchall()
    all_items = []
    reminders = []
    for q in quotes:
        for item in _schedule_items(db, q['quote_id']):
            if not item.get('scheduled_start_date'):
                continue
            item['quote_id'] = q['quote_id']
            item['customer_name'] = q['customer_name']
            all_items.append(item)
            if item['needs_confirmation']:
                reminders.append(item)
    all_items.sort(key=lambda i: (i.get('sub_name') or '~', i['scheduled_start_date']))
    reminders.sort(key=lambda i: i['scheduled_start_date'])
    return render_template('schedule_overview.html', items=all_items, reminders=reminders)

def _insert_change_order_item(db, co_id, item, kind, action, label, sort_order,
                               source_line_item_id=None, source_change_order_item_id=None):
    """item is a dict shaped like _price_catalog_item's output, quote_line_items, or a
    prior change_order_items row -- all three share the same column names, so no
    key-mapping is needed. For a 'remove' row, the caller is expected to have already
    negated item['total_cost']/['total_price']; the breakdown columns (labor_*/material_*)
    are stored as the removed item's own original (positive) magnitudes, for audit."""
    db.execute(
        "INSERT INTO change_order_items "
        "(change_order_id, sort_order, kind, action, label, description, work_type_id, "
        "sub_id, sub_name, labor_quantity, labor_unit, labor_cost_per_unit, labor_total_cost, "
        "labor_markup_pct, labor_margin_pct, labor_total_price, labor_min_markup, "
        "material_id, material_label, material_quantity, material_unit, material_cost_per_unit, "
        "material_total_cost, material_markup_pct, material_margin_pct, material_total_price, material_min_markup, "
        "product_id, product_label, total_cost, total_price, total_margin_pct, "
        "source_line_item_id, source_change_order_item_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (co_id, sort_order, kind, action, label, item.get('description',''), item.get('work_type_id'),
         item.get('sub_id',''), item.get('sub_name',''), item.get('labor_quantity',0), item.get('labor_unit',''),
         item.get('labor_cost_per_unit',0), item.get('labor_total_cost',0),
         item.get('labor_markup_pct',0), item.get('labor_margin_pct',0),
         item.get('labor_total_price',0), item.get('labor_min_markup',0),
         item.get('material_id'), item.get('material_label',''), item.get('material_quantity',0), item.get('material_unit',''),
         item.get('material_cost_per_unit',0), item.get('material_total_cost',0),
         item.get('material_markup_pct',0), item.get('material_margin_pct',0),
         item.get('material_total_price',0), item.get('material_min_markup',10),
         item.get('product_id'), item.get('product_label',''),
         item['total_cost'], item['total_price'], item.get('total_margin_pct',0),
         source_line_item_id, source_change_order_item_id)
    )

def _compute_commission(policy, total_cost, total_price):
    """The one commission formula, shared by _recalc_quote and _recalc_change_order
    so a quote and a Change Order are always paid out under the same rules. 'tiered_gp'
    rewards margin, not just volume: the rate applied depends on gross-profit margin %,
    not a flat cut of price. Thresholds/rates all come from the active commission_policy
    row (editable in Admin), stored as plain percentages."""
    if not policy:
        return 0
    gross_profit = total_price - total_cost
    method = policy['method']
    if method == 'pct_of_price':
        return total_price * float(policy['rate']) / 100
    if method == 'pct_of_margin':
        return gross_profit * float(policy['rate']) / 100
    if method == 'tiered_gp':
        margin_pct = (gross_profit / total_price * 100) if total_price else 0
        if margin_pct < float(policy['tier1_threshold']):
            rate = policy['tier1_rate']
        elif margin_pct < float(policy['tier2_threshold']):
            rate = policy['tier2_rate']
        else:
            rate = policy['tier3_rate']
        return gross_profit * float(rate) / 100
    return 0

def _recalc_change_order(db, co_id):
    """Mirrors _recalc_quote's commission math exactly, summing change_order_items
    instead of quote_line_items. Never touches payment_schedules -- a Change Order
    doesn't get its own predicted schedule; its net amount gets appended as one row
    only once it's signed (see the /sign route in Phase 6)."""
    items = db.execute("SELECT total_cost, total_price FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchall()
    total_cost = sum(float(i['total_cost'] or 0) for i in items)
    total_price = sum(float(i['total_price'] or 0) for i in items)
    margin = ((total_price - total_cost) / total_price * 100) if total_price else 0
    policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    commission = _compute_commission(policy, total_cost, total_price)
    db.execute("UPDATE change_orders SET total_cost=?,total_price=?,total_margin=?,commission=? WHERE id=?",
               (round(total_cost,2), round(total_price,2), round(margin,1), round(commission,2), co_id))
    db.commit()

# ── Change Orders ────────────────────────────────────────────────────────────

@app.route('/quotes/<int:quote_id>/change_orders', methods=['POST'])
@login_required
def create_change_order(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote or not _is_locked_contract(db, quote_id):
        return redirect(url_for('edit_quote', quote_id=quote_id))
    max_co = db.execute("SELECT MAX(co_number) FROM change_orders WHERE quote_id=?", (quote_id,)).fetchone()[0]
    co_number = (max_co or 0) + 1
    db.execute("INSERT INTO change_orders (quote_id, co_number, status) VALUES (?,?,'draft')", (quote_id, co_number))
    db.commit()
    co_id = db.execute("SELECT lastval()").fetchone()[0]
    return redirect(url_for('change_order_builder', quote_id=quote_id, co_id=co_id))

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>')
@login_required
def change_order_builder(quote_id, co_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not quote or not co:
        return redirect(url_for('quotes_list'))
    items = db.execute("SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY sort_order", (co_id,)).fetchall()
    effective = _contract_effective_items(db, quote_id)
    # Also apply this draft CO's own items on top, purely for the Remove/Amend dropdowns --
    # so a work type already staged for removal earlier in this same draft doesn't still
    # show up as removable. The authoritative resolver (signed-only) is untouched.
    for it in items:
        if it['kind'] != 'catalog':
            continue
        if it['action'] == 'remove':
            effective.pop(it['work_type_id'], None)
        elif it['action'] == 'add':
            effective[it['work_type_id']] = dict(it)
    removable = list(effective.values())
    work_types = [dict(wt) for wt in db.execute(
        "SELECT * FROM work_types WHERE active='Y' AND is_modifier='N' ORDER BY work_type").fetchall()]
    subs = db.execute("SELECT * FROM subs WHERE active='Y' ORDER BY name").fetchall()
    materials = db.execute("""SELECT m.*, s.name as supplier_name FROM materials m
                              JOIN suppliers s ON m.supplier_id=s.supplier_id
                              WHERE m.active='Y'
                              ORDER BY m.category, m.series""").fetchall()
    manufacturers = db.execute("SELECT * FROM surface_manufacturers WHERE active='Y' ORDER BY manufacturer_name").fetchall()
    prior_cos = db.execute("SELECT * FROM change_orders WHERE quote_id=? AND id!=? ORDER BY co_number", (quote_id, co_id)).fetchall()
    return render_template('change_order_builder.html', quote=quote, co=co, items=items,
                           removable=removable, work_types=work_types, subs=subs, materials=materials,
                           manufacturers=manufacturers, prior_cos=prior_cos)

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/items', methods=['POST'])
@login_required
def add_change_order_item(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] != 'draft':
        return jsonify({'error': 'Not found or not editable'}), 404
    data = request.json
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    item = _price_catalog_item(db, quote, data)
    sort_order = db.execute("SELECT COUNT(*) FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchone()[0]
    _insert_change_order_item(db, co_id, item, 'catalog', 'add', item['work_type_label'], sort_order)
    db.commit()
    _recalc_change_order(db, co_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/items/remove', methods=['POST'])
@login_required
def remove_change_order_item(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] != 'draft':
        return jsonify({'error': 'Not found or not editable'}), 404
    data = request.json or {}
    work_type_id = int(data.get('work_type_id'))
    effective = _contract_effective_items(db, quote_id)
    current = effective.get(work_type_id)
    if not current:
        return jsonify({'error': 'That work type is not currently on this contract.'}), 400
    negated = dict(current)
    negated['total_cost'] = -abs(float(current['total_cost'] or 0))
    negated['total_price'] = -abs(float(current['total_price'] or 0))
    is_from_co = 'change_order_id' in current
    src_line = current.get('id') if not is_from_co else None
    src_co_item = current.get('id') if is_from_co else None
    label = current.get('work_type_label') or current.get('label') or ''
    sort_order = db.execute("SELECT COUNT(*) FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchone()[0]
    _insert_change_order_item(db, co_id, negated, 'catalog', 'remove', label + ' (removed)', sort_order,
                               source_line_item_id=src_line, source_change_order_item_id=src_co_item)
    db.commit()
    _recalc_change_order(db, co_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/items/amend', methods=['POST'])
@login_required
def amend_change_order_item(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] != 'draft':
        return jsonify({'error': 'Not found or not editable'}), 404
    data = request.json or {}
    work_type_id = int(data.get('work_type_id'))
    effective = _contract_effective_items(db, quote_id)
    current = effective.get(work_type_id)
    if not current:
        return jsonify({'error': 'That work type is not currently on this contract.'}), 400

    negated = dict(current)
    negated['total_cost'] = -abs(float(current['total_cost'] or 0))
    negated['total_price'] = -abs(float(current['total_price'] or 0))
    is_from_co = 'change_order_id' in current
    src_line = current.get('id') if not is_from_co else None
    src_co_item = current.get('id') if is_from_co else None
    old_label = current.get('work_type_label') or current.get('label') or ''
    sort_order = db.execute("SELECT COUNT(*) FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchone()[0]
    _insert_change_order_item(db, co_id, negated, 'catalog', 'remove', old_label + ' (previous)', sort_order,
                               source_line_item_id=src_line, source_change_order_item_id=src_co_item)

    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    new_item = _price_catalog_item(db, quote, data)
    sort_order2 = db.execute("SELECT COUNT(*) FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchone()[0]
    _insert_change_order_item(db, co_id, new_item, 'catalog', 'add', new_item['work_type_label'], sort_order2)

    db.commit()
    _recalc_change_order(db, co_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/items/freeform', methods=['POST'])
@login_required
def add_freeform_change_order_item(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] != 'draft':
        return jsonify({'error': 'Not found or not editable'}), 404
    data = request.json or {}
    label = (data.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'A label is required.'}), 400
    if data.get('price') in (None, '') or data.get('cost') in (None, ''):
        return jsonify({'error': 'Both a price and a cost are required.'}), 400
    price = float(data['price'])
    cost = float(data['cost'])
    margin = ((price - cost) / price * 100) if price else 0
    sort_order = db.execute("SELECT COUNT(*) FROM change_order_items WHERE change_order_id=?", (co_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO change_order_items (change_order_id, sort_order, kind, action, label, description, "
        "total_cost, total_price, total_margin_pct) VALUES (?,?,?,?,?,?,?,?,?)",
        (co_id, sort_order, 'freeform', '', label, data.get('description',''), cost, price, round(margin,1))
    )
    db.commit()
    _recalc_change_order(db, co_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_change_order_item(quote_id, co_id, item_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] != 'draft':
        return jsonify({'error': 'Not found or not editable'}), 404
    db.execute("DELETE FROM change_order_items WHERE id=? AND change_order_id=?", (item_id, co_id))
    db.commit()
    _recalc_change_order(db, co_id)
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/delete', methods=['POST'])
@login_required
def delete_change_order(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if co and co['status'] == 'draft':
        db.execute("DELETE FROM change_order_items WHERE change_order_id=?", (co_id,))
        db.execute("DELETE FROM change_orders WHERE id=?", (co_id,))
        db.commit()
    return redirect(url_for('edit_quote', quote_id=quote_id))

def _change_order_money(db, quote_id, co_id):
    """The running-balance math shown on a signed Change Order document -- confirmed
    against a real example: Original Contract Total (frozen at signing) plus the net of
    all PRIOR SIGNED Change Orders on this contract, minus Total Paid so far (one running
    pool against the whole job, not tracked per-CO), gives Remaining Balance on Original;
    add this CO's own net to get the New Remaining Balance. 'Previous Change Orders' is
    omitted entirely when this is CO #1."""
    quote = db.execute("SELECT total_price FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    co = db.execute("SELECT * FROM change_orders WHERE id=?", (co_id,)).fetchone()
    original_total = float(quote['total_price'] or 0)
    prior_net = float(db.execute(
        "SELECT COALESCE(SUM(total_price),0) FROM change_orders WHERE quote_id=? AND status='signed' AND co_number < ?",
        (quote_id, co['co_number'])
    ).fetchone()[0] or 0)
    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(collected_amount),0) FROM payment_schedules WHERE quote_id=? AND collected=1",
        (quote_id,)
    ).fetchone()[0] or 0)
    remaining_on_original = original_total + prior_net - total_paid
    this_co_net = float(co['total_price'] or 0)
    new_remaining = remaining_on_original + this_co_net
    return {
        'original_total': round(original_total, 2),
        'prior_net': round(prior_net, 2),
        'show_prior': co['co_number'] > 1,
        'total_paid': round(total_paid, 2),
        'remaining_on_original': round(remaining_on_original, 2),
        'this_co_net': round(this_co_net, 2),
        'new_remaining': round(new_remaining, 2),
    }

def _change_order_preview_html(quote_id, co_id):
    """Shared renderer for the signable Change Order document -- used by the /preview
    route and the email PDF export, same one-function-two-callers pattern as
    _quote_preview_html."""
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    items = db.execute("SELECT * FROM change_order_items WHERE change_order_id=? ORDER BY sort_order", (co_id,)).fetchall()
    money = _change_order_money(db, quote_id, co_id)
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    from datetime import date
    created_date = date.today().strftime('%B %d, %Y')
    return render_template('change_order_preview.html', quote=quote, co=co, items=items,
                           money=money, settings=settings, created_date=created_date)

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/preview')
@login_required
def change_order_preview(quote_id, co_id):
    return _change_order_preview_html(quote_id, co_id)

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/sign', methods=['POST'])
@login_required
def sign_change_order(quote_id, co_id):
    db = get_db()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not co or co['status'] == 'signed':
        return jsonify({'error': 'Not found or already signed'}), 404
    data = request.json
    signature_data = data.get('signature', '')
    printed_name = (data.get('printed_name') or '').strip()
    if not signature_data or not printed_name:
        return jsonify({'error': 'Signature and printed name are required'}), 400
    from datetime import datetime
    signed_at = datetime.now().strftime('%B %d, %Y %I:%M %p')
    db.execute("UPDATE change_orders SET signature_data=?,signed_at=?,signed_name=?,status='signed' WHERE id=?",
               (signature_data, signed_at, printed_name, co_id))
    # Append this CO's net amount to the payment ledger -- a plain INSERT, never
    # generate_payment_schedule() (which would wipe collected-payment history).
    max_sort = db.execute("SELECT COALESCE(MAX(sort_order),-1) FROM payment_schedules WHERE quote_id=?", (quote_id,)).fetchone()[0]
    db.execute("INSERT INTO payment_schedules (quote_id, label, amount, pct, sort_order) VALUES (?,?,?,?,?)",
               (quote_id, f"Change Order #{co['co_number']}", co['total_price'], 0, max_sort + 1))
    db.commit()
    return jsonify({'success': True, 'signed_at': signed_at})

@app.route('/quotes/<int:quote_id>/change_orders/<int:co_id>/email', methods=['POST'])
@login_required
def email_change_order(quote_id, co_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    co = db.execute("SELECT * FROM change_orders WHERE id=? AND quote_id=?", (co_id, quote_id)).fetchone()
    if not quote or not co:
        return jsonify({'error': 'Not found'}), 404
    to_email = (quote['customer_email'] or '').strip()
    if not to_email:
        return jsonify({'error': 'No customer email on file. Add one in Edit Details.'}), 400
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    gmail_address = settings['gmail_address'] if settings else ''
    gmail_app_password = settings['gmail_app_password'] if settings else ''
    if not gmail_address or not gmail_app_password:
        return jsonify({'error': 'Email sending isn\'t set up yet. Add your Gmail address and App Password in Admin → Company Settings.'}), 400
    try:
        html = _change_order_preview_html(quote_id, co_id)
        pdf_bytes = _generate_quote_pdf_bytes(html)
        company_name = settings['company_name'] if settings else 'Your Company'
        subject = f"Change Order #{co['co_number']} from {company_name} — QT-{quote_id:04d}"
        body = (f"Hi {quote['customer_name']},\n\n"
                f"Please find your Change Order attached.\n\n"
                f"Thank you,\n{quote['salesperson'] or company_name}")
        _send_quote_email(to_email, subject, body, pdf_bytes, quote_id, gmail_address, gmail_app_password)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if co['status'] == 'draft':
        db.execute("UPDATE change_orders SET status='sent' WHERE id=?", (co_id,))
        db.commit()
    return jsonify({'success': True, 'sent_to': to_email})

AIM_CATEGORY = 'Glass (Artistry In Mosaics)'

CAP_TILE_CATEGORY = 'Cap Tile Trim'

def _eligible_tiles_for_package(db, package_id, manufacturer='luv', cap_tile=False):
    """Waterline tile priced above the previous tier's threshold (by sort_order), at or
    below this tier's own threshold -- unbounded above if this tier has no threshold set.
    manufacturer='luv' (default) excludes Artistry In Mosaics' Glass line entirely --
    manufacturers are shown as distinct sets, never mixed in one list -- pass 'aim' to
    see only that line instead.

    cap_tile=True switches entirely to the Cap Tile Trim category and ignores both the
    package price-tier banding and the luv/aim split -- Cap Tile Trim is a separate product
    line from waterline tile (different pricing scale, no AIM equivalent), so package tiers
    that band waterline tile by price don't mean anything for it. Without this, Cap Tile
    Trim rows (added 2026-08-24) would otherwise get swept into the price-banded waterline
    list by coincidence of cost_per_quote_unit falling in a tier's range."""
    if cap_tile:
        return db.execute(
            "SELECT material_id, category, series, cost_per_quote_unit, product_url "
            "FROM materials WHERE work_type_id=4 AND active='Y' AND category=? ORDER BY series",
            (CAP_TILE_CATEGORY,)
        ).fetchall()
    pkg = db.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
    if not pkg:
        return []
    prev_threshold = db.execute(
        "SELECT MAX(tile_price_threshold) FROM packages WHERE sort_order < ?", (pkg['sort_order'],)
    ).fetchone()[0]
    upper = pkg['tile_price_threshold']
    tile_query = ("SELECT material_id, category, series, cost_per_quote_unit, product_url "
                   "FROM materials WHERE work_type_id=4 AND active='Y' AND category != ?")
    params = [CAP_TILE_CATEGORY]
    if manufacturer == 'aim':
        tile_query += " AND category = ?"
        params.append(AIM_CATEGORY)
    elif manufacturer == 'luv':
        tile_query += " AND category != ?"
        params.append(AIM_CATEGORY)
    # manufacturer == 'all': no filter, used by the internal admin reference view
    if prev_threshold is not None:
        tile_query += " AND cost_per_quote_unit > ?"
        params.append(prev_threshold)
    if upper is not None:
        tile_query += " AND cost_per_quote_unit <= ?"
        params.append(upper)
    tile_query += " ORDER BY category, series"
    return db.execute(tile_query, tuple(params)).fetchall()

def _eligible_surfaces_for_package(db, package_id, manufacturer_id=None):
    """Surface finishes priced above the previous tier's threshold (by sort_order), at or
    below this tier's own threshold -- unbounded above if this tier has no threshold set.
    Mirrors _eligible_tiles_for_package exactly, banding on the Pebble Pros ('P1') rate
    instead of tile's material cost. Only finishes with a real P1 rate are eligible -- of
    172 catalog finishes, 89 have no rate yet and are excluded entirely (not sellable).
    manufacturer_id filters to one manufacturer for a tab; omitted, returns all eligible."""
    pkg = db.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
    if not pkg:
        return []
    prev_threshold = db.execute(
        "SELECT MAX(surface_price_threshold) FROM packages WHERE sort_order < ?", (pkg['sort_order'],)
    ).fetchone()[0]
    upper = pkg['surface_price_threshold']
    q = ("SELECT sp.product_id, sp.product_line, sp.finish, sp.product_url, sar.rate, "
         "sm.manufacturer_id, sm.manufacturer_name "
         "FROM surface_products sp "
         "JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id "
         "JOIN surface_applicator_rates sar ON sp.product_id=sar.product_id AND sar.sub_id='P1' "
         "WHERE sp.active='Y' AND sm.active='Y'")
    params = []
    if manufacturer_id:
        q += " AND sm.manufacturer_id=?"
        params.append(manufacturer_id)
    if prev_threshold is not None:
        q += " AND sar.rate > ?"
        params.append(prev_threshold)
    if upper is not None:
        q += " AND sar.rate <= ?"
        params.append(upper)
    q += " ORDER BY sm.manufacturer_name, sp.product_line, sp.finish"
    return db.execute(q, tuple(params)).fetchall()

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
        'deck_sqft': float(data.get('deck_sqft', 0) or 0),
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

def _get_or_create_customer(db, name, address, email, phone=''):
    """Normalized-name match against existing customers -- typing the same customer's name
    again on a new quote links to the same record instead of creating a duplicate. Case/
    whitespace-insensitive only; doesn't try to fuzzy-match near-spellings, since a wrong
    auto-merge (two different people who happen to share a name) is worse than an
    occasional duplicate a human can merge by hand later."""
    name = (name or '').strip()
    existing = db.execute(
        "SELECT customer_id FROM customers WHERE LOWER(TRIM(name))=?", (name.lower(),)
    ).fetchone()
    if existing:
        return existing['customer_id']
    cur = db.execute(
        "INSERT INTO customers (name, address, email, phone) VALUES (?,?,?,?) RETURNING customer_id",
        (name, address or '', email or '', phone or '')
    )
    return cur.fetchone()[0]

def _cascade_customer_contact(db, customer_id, name, email, phone):
    """Writes a customer's name/phone/email to the customers record and pushes name/email
    out to every quote linked to that customer_id -- the 'edit once, updates everywhere'
    behavior. Address is deliberately excluded: a quote's address is the job site, which can
    legitimately differ across jobs for the same repeat customer, so it's never cascaded."""
    db.execute("UPDATE customers SET name=?, email=?, phone=? WHERE customer_id=?",
               (name, email or '', phone or '', customer_id))
    db.execute("UPDATE quotes SET customer_name=?, customer_email=? WHERE customer_id=?",
               (name, email or '', customer_id))

@app.route('/quotes/new', methods=['GET','POST'])
@login_required
def new_quote():
    if request.method == 'POST':
        db = get_db()
        default_terms = db.execute("SELECT id FROM terms_documents WHERE is_default=1 AND active=1 LIMIT 1").fetchone()
        default_terms_id = default_terms[0] if default_terms else None
        customer_name = request.form['customer_name']
        address = request.form.get('address','')
        customer_email = request.form.get('customer_email','').strip()
        customer_id = _get_or_create_customer(db, customer_name, address, customer_email)
        db.execute("""INSERT INTO quotes (customer_name,address,salesperson,
                      pool_perimeter,pool_shallow,pool_deep,pool_sqft,
                      has_spa,spa_perimeter,spa_depth,spa_sqft,
                      has_shelf,shelf_sqft,total_surface_sqft,
                      steps_lf,benches_lf,swimouts_lf,customer_email,water_surface_sqft,deck_sqft,
                      terms_document_id,customer_id)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (customer_name, address,
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
                    customer_email,
                    float(request.form.get('water_surface_sqft',0) or 0),
                    float(request.form.get('deck_sqft',0) or 0),
                    default_terms_id, customer_id))
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
    tile_items = db.execute(
        "SELECT * FROM quote_line_items WHERE quote_id=? AND work_type_id IN (4,5) ORDER BY work_type_id",
        (quote_id,)
    ).fetchall()
    requested_item_id = request.args.get('item_id', type=int)
    tile_item = None
    if requested_item_id:
        tile_item = next((t for t in tile_items if t['id'] == requested_item_id), None)
    if not tile_item and tile_items:
        tile_item = tile_items[0]
    manufacturer = request.args.get('manufacturer', 'luv')
    if manufacturer not in ('luv', 'aim'):
        manufacturer = 'luv'
    is_cap_tile_item = bool(tile_item and tile_item['work_type_id'] == 5)
    all_tiles = _eligible_tiles_for_package(db, package_id, manufacturer, cap_tile=is_cap_tile_item) if (package_id or is_cap_tile_item) else []
    aim_count = 0 if is_cap_tile_item else (len(_eligible_tiles_for_package(db, package_id, 'aim')) if package_id else 0)
    luv_count = len(all_tiles) if is_cap_tile_item else (len(_eligible_tiles_for_package(db, package_id, 'luv')) if package_id else 0)
    PER_PAGE = 6
    page = max(1, int(request.args.get('page', 1) or 1))
    total_pages = max(1, (len(all_tiles) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    eligible_tiles = all_tiles[start:start + PER_PAGE]

    # Surface finish (Surface Application, work_type_id=1) -- a single pick per quote, so
    # no item selector like tile's cap/waterline choice. Defaults to a "here's your surface"
    # reveal rather than a browsing grid -- most jobs just confirm the package's own default.
    surface_item = db.execute(
        "SELECT * FROM quote_line_items WHERE quote_id=? AND work_type_id=1 ORDER BY id LIMIT 1",
        (quote_id,)
    ).fetchone()
    surface_view = request.args.get('surface_view', 'reveal')
    if surface_view not in ('reveal', 'browse'):
        surface_view = 'reveal'
    surface_manufacturers = db.execute("SELECT * FROM surface_manufacturers WHERE active='Y' ORDER BY manufacturer_name").fetchall()
    surface_mfr_counts = {}
    if package_id:
        for mfr in surface_manufacturers:
            surface_mfr_counts[mfr['manufacturer_id']] = len(_eligible_surfaces_for_package(db, package_id, mfr['manufacturer_id']))
    surface_manufacturer_id = request.args.get('surface_manufacturer_id', type=int)
    if not surface_manufacturer_id or surface_mfr_counts.get(surface_manufacturer_id, 0) == 0:
        surface_manufacturer_id = next((mid for mid, cnt in surface_mfr_counts.items() if cnt > 0), None)
    all_surfaces = _eligible_surfaces_for_package(db, package_id, surface_manufacturer_id) if (package_id and surface_manufacturer_id) else []
    surface_page = max(1, int(request.args.get('surface_page', 1) or 1))
    surface_total_pages = max(1, (len(all_surfaces) + PER_PAGE - 1) // PER_PAGE)
    surface_page = min(surface_page, surface_total_pages)
    s_start = (surface_page - 1) * PER_PAGE
    eligible_surfaces = all_surfaces[s_start:s_start + PER_PAGE]

    return render_template('select_materials.html', quote=quote, package=package,
                           tile_items=tile_items, tile_item=tile_item, eligible_tiles=eligible_tiles,
                           page=page, total_pages=total_pages, total_tiles=len(all_tiles),
                           manufacturer=manufacturer, luv_count=luv_count, aim_count=aim_count,
                           surface_item=surface_item, surface_view=surface_view,
                           surface_manufacturers=surface_manufacturers, surface_mfr_counts=surface_mfr_counts,
                           surface_manufacturer_id=surface_manufacturer_id, eligible_surfaces=eligible_surfaces,
                           surface_page=surface_page, surface_total_pages=surface_total_pages,
                           total_surfaces=len(all_surfaces))

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
    page = request.form.get('page', '')
    manufacturer = request.form.get('manufacturer', '')
    return redirect(url_for('select_materials', quote_id=quote_id, package_id=package_id, page=page, manufacturer=manufacturer, item_id=item_id))

@app.route('/quotes/<int:quote_id>/select_materials/surface', methods=['POST'])
@login_required
def select_material_surface(quote_id):
    """Mirrors select_material_tile, but Surface Application prices through the labor side
    (a Pebble Pros $/sqft rate), not a separate material component -- see the wt_id==1
    special case in _compute_line_item_pricing. Swapping finish only touches labor_* and
    product_*; material_* stays whatever it already was (always 0 for this work type)."""
    db = get_db()
    item_id = request.form.get('item_id')
    product_id = request.form.get('product_id')
    row = db.execute("SELECT * FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return redirect(url_for('select_materials', quote_id=quote_id))
    sar = db.execute(
        "SELECT sar.rate, sp.finish, sp.product_line, sm.manufacturer_name "
        "FROM surface_applicator_rates sar "
        "JOIN surface_products sp ON sar.product_id=sp.product_id "
        "JOIN surface_manufacturers sm ON sp.manufacturer_id=sm.manufacturer_id "
        "WHERE sar.sub_id=? AND sar.product_id=?",
        (row['sub_id'] or 'P1', product_id)
    ).fetchone()
    if sar:
        label = f"{sar['manufacturer_name']} {sar['product_line']} – {sar['finish']}"
        can_override = bool(g.role and g.role['can_override_min_markup'])
        qty = float(row['labor_quantity'])
        markup = float(row['labor_markup_pct'])
        min_m = float(row['labor_min_markup'] or 15)
        l_cost, l_price, l_margin = calc_component(float(sar['rate']), qty, markup, min_m, can_override)
        m_cost = float(row['material_total_cost'] or 0)
        m_price = float(row['material_total_price'] or 0)
        total_cost = round(l_cost + m_cost, 2)
        total_price = round(l_price + m_price, 2)
        total_margin = round((total_price - total_cost) / total_price * 100 if total_price else 0, 1)
        db.execute("""UPDATE quote_line_items SET
                      product_id=?, product_label=?, labor_cost_per_unit=?,
                      labor_total_cost=?, labor_total_price=?, labor_margin_pct=?,
                      total_cost=?, total_price=?, total_margin_pct=? WHERE id=?""",
                   (product_id, label, float(sar['rate']),
                    l_cost, l_price, l_margin,
                    total_cost, total_price, total_margin, item_id))
        db.commit()
        _recalc_quote(db, quote_id)
    package_id = request.form.get('package_id', '')
    return redirect(url_for('select_materials', quote_id=quote_id, package_id=package_id, surface_view='reveal'))

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
    signed_co_net = float(db.execute(
        "SELECT COALESCE(SUM(total_price),0) FROM change_orders WHERE quote_id=? AND status='signed'", (quote_id,)
    ).fetchone()[0] or 0)
    contract_total = float(quote['total_price'] or 0) + signed_co_net
    change_orders = db.execute("SELECT * FROM change_orders WHERE quote_id=? ORDER BY co_number", (quote_id,)).fetchall()
    versions = db.execute("SELECT * FROM quote_versions WHERE quote_id=? ORDER BY version_number DESC", (quote_id,)).fetchall()
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
    id_to_label = {i['id']: i['work_type_label'] for i in line_items_enriched}
    for item_dict in line_items_enriched:
        item_dict['replaces_label'] = id_to_label.get(item_dict.get('replaces_item_id'))
    line_items = [i for i in line_items_enriched if not i.get('is_optional')]
    optional_items = [i for i in line_items_enriched if i.get('is_optional') and not i.get('is_declined')]
    declined_items = [i for i in line_items_enriched if i.get('is_optional') and i.get('is_declined')]
    optional_total = round(sum(float(i['total_price'] or 0) for i in optional_items), 2)
    already_replaced_ids = {i['replaces_item_id'] for i in optional_items + declined_items if i.get('replaces_item_id')}
    replaceable_items = [i for i in line_items if i['id'] not in already_replaced_ids]
    estimated_days_total, estimated_days_margin, missing_estimate_labels = _estimated_timeline_days(db, line_items)

    materials = db.execute("""SELECT m.*, s.name as supplier_name FROM materials m
                              JOIN suppliers s ON m.supplier_id=s.supplier_id
                              WHERE m.active='Y'
                              ORDER BY m.category, m.series""").fetchall()
    qualifier_rows = db.execute("SELECT * FROM qualifiers WHERE active='Y' ORDER BY label").fetchall()
    qualifiers_by_wt = {}
    for q in qualifier_rows:
        # Leak Detection has two variants (pool vs. pool & spa) that only differ by
        # whether the job has a spa — show just the one that matches this quote's
        # dimensions instead of making staff pick the right one manually.
        if q['work_type_id'] == 1 and q['label'] in ('Leak Detection', 'Leak Detection – Pool & Spa'):
            if bool(quote['has_spa']) != ('Spa' in q['label']):
                continue
        qualifiers_by_wt.setdefault(q['work_type_id'], []).append(dict(q))
    additives = db.execute("SELECT * FROM surface_additives WHERE active='Y' ORDER BY label").fetchall()
    terms_docs = db.execute("SELECT id,label FROM terms_documents WHERE active=1 ORDER BY is_default DESC, label").fetchall()
    return render_template('edit_quote.html', quote=quote, line_items=line_items,
                           optional_items=optional_items, optional_total=optional_total,
                           declined_items=declined_items, replaceable_items=replaceable_items,
                           work_types=work_types, subs=subs, commission_policy=commission_policy,
                           current_role=g.role, schedule=schedule, schedule_json=schedule_json,
                           contract_total=contract_total, change_orders=change_orders, versions=versions,
                           manufacturers=manufacturers, applicators=applicators, materials=materials,
                           qualifiers_by_wt=qualifiers_by_wt, additives=additives,
                           estimated_days_total=estimated_days_total, estimated_days_margin=estimated_days_margin,
                           missing_estimate_labels=missing_estimate_labels, terms_docs=terms_docs)

@app.route('/quotes/<int:quote_id>/customer_view')
@login_required
def customer_view(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return redirect(url_for('quotes_list'))
    line_items = db.execute(
        "SELECT * FROM quote_line_items WHERE quote_id=? AND COALESCE(is_declined,0)=0 ORDER BY sort_order",
        (quote_id,)
    ).fetchall()
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
        line_items_enriched.append(item_dict)
    manufacturers = db.execute("SELECT * FROM surface_manufacturers WHERE active='Y' ORDER BY manufacturer_name").fetchall()
    materials = db.execute("""SELECT material_id, category, series, work_type_id FROM materials
                              WHERE active='Y' AND work_type_id IN (4,5,6,7)
                              ORDER BY category, series""").fetchall()
    schedule = db.execute("SELECT * FROM payment_schedules WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    addable_work_types = db.execute(
        "SELECT work_type_id, work_type FROM work_types WHERE active='Y' AND is_modifier='N' ORDER BY work_type"
    ).fetchall()
    return render_template('customer_view.html', quote=quote, line_items=line_items_enriched,
                           manufacturers=manufacturers, materials=materials, schedule=schedule,
                           addable_work_types=addable_work_types)

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
        if _is_locked_contract(db, quote_id):
            return redirect(url_for('edit_quote', quote_id=quote_id))
        customer_name = request.form['customer_name']
        address = request.form.get('address','')
        customer_email = request.form.get('customer_email','').strip()
        db.execute("""UPDATE quotes SET customer_name=?,address=?,salesperson=?,
                      pool_perimeter=?,pool_shallow=?,pool_deep=?,pool_sqft=?,
                      has_spa=?,spa_perimeter=?,spa_depth=?,spa_sqft=?,
                      has_shelf=?,shelf_sqft=?,total_surface_sqft=?,
                      steps_lf=?,benches_lf=?,swimouts_lf=?,customer_email=?,water_surface_sqft=?,deck_sqft=?
                      WHERE quote_id=?""",
                   (customer_name, address,
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
                    customer_email,
                    float(request.form.get('water_surface_sqft',0) or 0),
                    float(request.form.get('deck_sqft',0) or 0),
                    quote_id))
        # Cascade name/email (not address -- see _cascade_customer_contact) to every other
        # quote for this same customer, so editing it here matches editing it from /customers.
        customer_id = quote['customer_id'] if quote and quote['customer_id'] else _get_or_create_customer(db, customer_name, address, customer_email)
        current_phone = db.execute("SELECT phone FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
        _cascade_customer_contact(db, customer_id, customer_name, customer_email, current_phone['phone'] if current_phone else '')
        if not (quote and quote['customer_id']):
            db.execute("UPDATE quotes SET customer_id=? WHERE quote_id=?", (customer_id, quote_id))
        db.commit()
        return redirect(url_for('edit_quote', quote_id=quote_id))
    return render_template('edit_quote_details.html', quote=quote)

def _price_catalog_item(db, quote, data):
    """Shared pricing body for a catalog-based line item: resolves sub/material/product
    labels, composes the Surface Application/Removal pool+spa+sunshelf label, and prices
    it through build_line_item() + a sub-specific cost floor (sub_rates.min_total_cost) +
    the min-job-price floor. Used by both add_line_item (a normal quote's line items) and
    a Change Order's catalog Add, so both price identically -- this is the one pricing
    engine, not two."""
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

    # Sub-specific cost floor (e.g. Pro Hydroblasters' $2,600 Surface Removal minimum) --
    # build_line_item() takes labor_cost_per_unit straight from staff-entered data and never
    # looks at sub_rates itself, so this floor has to be re-applied here, mirroring how
    # _compute_line_item_pricing (the OTHER pricing path, for quick-add/package auto-
    # populate) already enforces it. Rescales price at the item's own labor markup%, not the
    # work type's default, since staff may have typed in a different markup.
    if sub_id and work_type_id:
        rate_row = db.execute(
            "SELECT min_total_cost FROM sub_rates WHERE sub_id=? AND work_type_id=?", (sub_id, work_type_id)
        ).fetchone()
        if rate_row and rate_row['min_total_cost'] and item['labor_total_cost'] < float(rate_row['min_total_cost']):
            floored_cost = float(rate_row['min_total_cost'])
            floored_price = round(floored_cost * (1 + item['labor_markup_pct'] / 100), 2)
            item['total_cost'] = round(item['total_cost'] - item['labor_total_cost'] + floored_cost, 2)
            item['total_price'] = round(item['total_price'] - item['labor_total_price'] + floored_price, 2)
            item['labor_total_cost'] = floored_cost
            item['labor_total_price'] = floored_price

    item['total_cost'], item['total_price'], item['total_margin_pct'] = _apply_min_job_price(
        db, item['work_type_id'], item['total_cost'], item['total_price'])
    return item

@app.route('/quotes/<int:quote_id>/line_items', methods=['POST'])
@login_required
def add_line_item(quote_id):
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    data = request.json
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    item = _price_catalog_item(db, quote, data)
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

@app.route('/quotes/<int:quote_id>/line_items/replacement', methods=['POST'])
@login_required
def add_replacement_item(quote_id):
    """A 'replacement item' is a second copy of a base item with its quantity negated,
    added to Optional Add-Ons so staff can price an alternative (e.g. pavers instead of
    the acrylic decking already in the quote) as a true net cost: the positive add-on
    items (Pavers, Coping) plus the negative replacement items (credit for the decking/tile
    they replace) sum to the real incremental price with no separate 'net' calculation
    needed anywhere -- Optional Add-Ons' total is already a plain sum.

    Negating quantity (not directly overwriting total_cost/total_price) matters: it means
    this row behaves like any ordinary line item under every later edit path (editCell ->
    update_line_item -> _rollup_item_totals) -- a markup or cost/unit tweak recomputes from
    quantity * rate and the negative sign survives naturally. Deliberately bypasses
    _price_catalog_item/build_line_item -- that path's sub-rate cost floor assumes a
    positive cost and would clamp a credit back up."""
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    data = request.json
    source_id = data.get('source_item_id')
    src = db.execute(
        "SELECT * FROM quote_line_items WHERE id=? AND quote_id=? AND (is_optional=0 OR is_optional IS NULL)",
        (source_id, quote_id)
    ).fetchone()
    if not src:
        return jsonify({'error': 'Item not found'}), 404

    can_override = bool(g.role and g.role['can_override_min_markup'])
    labor_total_cost, labor_total_price, labor_margin_pct = calc_component(
        src['labor_cost_per_unit'], -float(src['labor_quantity'] or 0),
        src['labor_markup_pct'], src['labor_min_markup'], can_override)
    material_total_cost, material_total_price, material_margin_pct = calc_component(
        src['material_cost_per_unit'], -float(src['material_quantity'] or 0),
        src['material_markup_pct'], src['material_min_markup'], can_override)
    total_cost = round(labor_total_cost + material_total_cost, 2)
    total_price = round(labor_total_price + material_total_price, 2)
    total_margin_pct = round(((total_price - total_cost) / total_price * 100) if total_price else 0, 1)
    sort_order = db.execute("SELECT COUNT(*) FROM quote_line_items WHERE quote_id=?", (quote_id,)).fetchone()[0]

    db.execute(
        "INSERT INTO quote_line_items "
        "(quote_id, work_type_id, work_type_label, cost_structure, "
        "sub_id, sub_name, labor_quantity, labor_unit, "
        "labor_cost_per_unit, labor_total_cost, labor_markup_pct, labor_margin_pct, labor_total_price, labor_min_markup, "
        "material_id, material_label, material_quantity, material_unit, "
        "material_cost_per_unit, material_total_cost, material_markup_pct, material_margin_pct, material_total_price, material_min_markup, "
        "product_id, product_label, total_cost, total_price, total_margin_pct, sort_order, description, "
        "is_optional, replaces_item_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (quote_id, src['work_type_id'], src['work_type_label'], src['cost_structure'],
         src['sub_id'], src['sub_name'], -float(src['labor_quantity'] or 0), src['labor_unit'],
         src['labor_cost_per_unit'], labor_total_cost, src['labor_markup_pct'], labor_margin_pct,
         labor_total_price, src['labor_min_markup'],
         src['material_id'], src['material_label'], -float(src['material_quantity'] or 0), src['material_unit'],
         src['material_cost_per_unit'], material_total_cost, src['material_markup_pct'], material_margin_pct,
         material_total_price, src['material_min_markup'],
         src['product_id'], src['product_label'],
         total_cost, total_price, total_margin_pct, sort_order, src['description'],
         1, source_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/markup', methods=['POST'])
@login_required
def update_markup(quote_id, item_id):
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    db.execute("DELETE FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id))
    db.commit()
    _recalc_quote(db, quote_id)
    updated_quote = db.execute("SELECT total_price FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return jsonify({'success': True, 'quote_total_price': updated_quote['total_price'] if updated_quote else None})

@app.route('/quotes/<int:quote_id>/line_items/quick_add', methods=['POST'])
@login_required
def quick_add_line_item(quote_id):
    """Add a line item priced entirely from the work type's own defaults (sub, markup,
    material) -- used by the customer-facing quote screen, which never sees or sends
    cost/markup data, unlike the staff add-item flow on the full edit screen."""
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    data = request.json or {}
    wt = db.execute("SELECT * FROM work_types WHERE work_type_id=?", (data.get('work_type_id'),)).fetchone()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not wt or not quote:
        return jsonify({'error': 'Not found'}), 404
    can_override = bool(g.role and g.role['can_override_min_markup'])
    p = _compute_line_item_pricing(db, dict(wt), wt['default_sub_id'], wt['default_material_id'], dict(quote), can_override)
    sort_order = db.execute("SELECT COUNT(*) FROM quote_line_items WHERE quote_id=?", (quote_id,)).fetchone()[0]
    _insert_line_item_from_pricing(db, quote_id, p, sort_order, is_optional=0)
    db.commit()
    _recalc_quote(db, quote_id)
    new_item = db.execute("SELECT * FROM quote_line_items WHERE quote_id=? ORDER BY id DESC LIMIT 1", (quote_id,)).fetchone()
    updated_quote = db.execute("SELECT total_price FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return jsonify({
        'success': True,
        'item_id': new_item['id'],
        'work_type_id': new_item['work_type_id'],
        'work_type_label': new_item['work_type_label'],
        'product_label': new_item['product_label'],
        'material_label': new_item['material_label'],
        'total_price': new_item['total_price'],
        'quote_total_price': updated_quote['total_price'],
    })

def _qualifier_cost(q, item_row):
    """A qualifier snapshot's 'amount' is either a flat dollar figure, or -- when 'per_unit'
    is set -- a $/unit rate multiplied by the item's own labor_quantity (e.g. Tile Removal
    at $3/lf). Shared by every place that sums an item's qualifiers_json, so a per-unit
    qualifier stays in sync if the item's quantity is edited after it was applied, rather
    than a dollar amount snapshotted once and going stale."""
    if q.get('per_unit'):
        return float(q['amount']) * float(item_row['labor_quantity'] or 0)
    return float(q['amount'])

def _rollup_item_totals(db, item_id):
    """Recompute a line item's total_cost/total_price/total_margin_pct from labor + material +
    qualifiers, using the item's labor markup to price the qualifier cost. Call after any edit
    that changes labor/material components or qualifiers. Returns (total_cost, total_price, total_margin)."""
    row = db.execute("SELECT * FROM quote_line_items WHERE id=?", (item_id,)).fetchone()
    quals = json.loads(row['qualifiers_json'] or '[]')
    pass_through_ids = {r['qualifier_id'] for r in db.execute(
        "SELECT qualifier_id FROM qualifiers WHERE is_freight=1").fetchall()}
    markup_cost = sum(_qualifier_cost(q, row) for q in quals if q['id'] not in pass_through_ids)
    pass_through_cost = sum(_qualifier_cost(q, row) for q in quals if q['id'] in pass_through_ids)
    q_price = round(markup_cost * (1 + float(row['labor_markup_pct'] or 0) / 100), 2) + round(pass_through_cost, 2)
    q_cost = round(markup_cost + pass_through_cost, 2)

    tax_cost = 0.0
    if row['tax_included']:
        settings = db.execute("SELECT tax_rate_pct FROM company_settings WHERE id=1").fetchone()
        rate = float(settings['tax_rate_pct'] or 0) if settings and settings['tax_rate_pct'] is not None else 0
        tax_cost = round(float(row['material_total_cost'] or 0) * rate / 100, 2)

    total_cost = round(float(row['labor_total_cost'] or 0) + float(row['material_total_cost'] or 0) + q_cost + tax_cost, 2)
    total_price = round(float(row['labor_total_price'] or 0) + float(row['material_total_price'] or 0) + q_price + tax_cost, 2)
    total_cost, total_price, total_margin = _apply_min_job_price(db, row['work_type_id'], total_cost, total_price)
    db.execute("UPDATE quote_line_items SET total_cost=?,total_price=?,total_margin_pct=? WHERE id=?",
               (total_cost, total_price, total_margin, item_id))
    return total_cost, total_price, total_margin

def _is_locked_contract(db, quote_id):
    """True once a quote has been signed, for as long as it stays in the signed lifecycle
    (contract -> in_progress -> complete) -- line items stay locked, Change Orders stay the
    only way to change scope, the whole way through, not just at the moment of signing."""
    q = db.execute("SELECT status FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return bool(q and q['status'] in ('contract', 'in_progress', 'complete'))

def _estimated_timeline_days(db, line_items):
    """Rough job duration: sum of each distinct work type's estimated_days across a
    quote's accepted line items. Not a real schedule -- just a summed estimate shown
    on the quote/contract, since work types don't run one at a time in practice.
    Also sums each work type's estimated_days_margin (a '+/- N days' variance, e.g. Paver
    Installation might typically run 3 days but stretch to 2-4) into a total_margin, so the
    quote can show a range instead of a single falsely-precise number.
    Also returns the distinct work-type labels on this quote that have no
    estimated_days set at all, so the total can be flagged as understated rather than
    silently treating 'no data' the same as 'confirmed zero days'. A work type flagged
    timeline_exempt (e.g. Startup, which genuinely runs ~28 days but passively alongside
    other work rather than as blocking sequential labor) is skipped entirely -- not summed,
    not flagged as missing -- since 'exempt' and 'nobody filled this in yet' are different
    things and only the second one is worth calling out."""
    wt_rows = {r['work_type_id']: r for r in db.execute(
        "SELECT work_type_id, estimated_days, estimated_days_margin, timeline_exempt FROM work_types").fetchall()}
    seen = set()
    total = 0.0
    total_margin = 0.0
    missing_labels = []
    for item in line_items:
        wt_id = item.get('work_type_id') if isinstance(item, dict) else item['work_type_id']
        if wt_id and wt_id not in seen:
            seen.add(wt_id)
            wt = wt_rows.get(wt_id)
            if wt and wt['timeline_exempt'] == 'Y':
                continue
            days = float(wt['estimated_days'] or 0) if wt else 0.0
            total += days
            total_margin += float(wt['estimated_days_margin'] or 0) if wt else 0.0
            if not days:
                label = item.get('work_type_label') if isinstance(item, dict) else item['work_type_label']
                missing_labels.append(label)
    return round(total, 1), round(total_margin, 1), missing_labels

SCHEDULE_REMINDER_LEAD_DAYS = 1

def _add_business_days(start_date, n):
    """start_date + n business days, skipping Sat/Sun -- not a holiday calendar, just
    'skip weekends', matching the level of precision the PM actually asked for."""
    from datetime import timedelta
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d

def _business_days_before(target_date, n):
    """The date that's n business days before target_date -- the mirror of
    _add_business_days, used to compute when a start-date reminder should first appear
    (e.g. a Monday start with n=1 reminds the previous Friday)."""
    from datetime import timedelta
    d = target_date
    subtracted = 0
    while subtracted < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            subtracted += 1
    return d

def _schedule_items(db, quote_id):
    """Every schedulable work item on a signed contract: the current effective catalog
    items (_contract_effective_items), tagged with 'source' like _ledger_items, plus a live-
    computed estimated_end_date (start + that work type's estimated_days, business days --
    not stored, so it stays correct if estimated_days is edited later) and whether it
    currently needs a PM reminder. Freeform Change Order items are excluded -- they have no
    work_type_id/estimated_days to schedule against, unlike the Ledger which does track
    freeform items for cost."""
    from datetime import date
    wt_days = {r['work_type_id']: float(r['estimated_days'] or 0)
               for r in db.execute("SELECT work_type_id, estimated_days FROM work_types").fetchall()}
    today = date.today()
    items = []
    for row in _contract_effective_items(db, quote_id).values():
        d = dict(row)
        d['source'] = 'change_order_item' if 'change_order_id' in d else 'quote_line_item'
        d['estimated_end_date'] = ''
        d['sub_email'] = ''
        if d.get('sub_id'):
            s = db.execute("SELECT email FROM subs WHERE sub_id=?", (d['sub_id'],)).fetchone()
            d['sub_email'] = s['email'] if s else ''
        if d.get('scheduled_start_date'):
            start = date.fromisoformat(d['scheduled_start_date'])
            days = wt_days.get(d['work_type_id'], 0.0)
            end = _add_business_days(start, int(days)) if days else start
            d['estimated_end_date'] = end.isoformat()
        d['needs_confirmation'] = bool(
            d.get('scheduled_start_date')
            and d.get('schedule_status', 'not_started') == 'not_started'
            and not d.get('confirmed_at')
            and today >= _business_days_before(date.fromisoformat(d['scheduled_start_date']), SCHEDULE_REMINDER_LEAD_DAYS)
        )
        items.append(d)
    return items

def _maybe_advance_quote_status(db, quote_id):
    """contract -> in_progress the first time any schedulable item gets an actual start
    date; in_progress -> complete once every schedulable item has an actual finish date.
    Called after any write that sets actual_start_date/actual_finish_date, same
    'recompute after every mutating write' style as _recalc_quote."""
    quote = db.execute("SELECT status FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote or quote['status'] not in ('contract', 'in_progress'):
        return
    items = _schedule_items(db, quote_id)
    if not items:
        return
    if quote['status'] == 'contract' and any(i.get('actual_start_date') for i in items):
        db.execute("UPDATE quotes SET status='in_progress' WHERE quote_id=?", (quote_id,))
    elif quote['status'] == 'in_progress' and all(i.get('actual_finish_date') for i in items):
        db.execute("UPDATE quotes SET status='complete' WHERE quote_id=?", (quote_id,))

def _recalc_quote(db, quote_id):
    if _is_locked_contract(db, quote_id):
        # Defensive: a signed Contract's totals (and payment_schedules rows, which may
        # carry collected-payment data by now) must stay frozen forever. Every route that
        # mutates quote_line_items is guarded from running against a locked contract, so
        # this should be unreachable -- but this is the one choke point every caller
        # passes through, so guard here too rather than trust every call site forever.
        return
    items = db.execute(
        "SELECT total_cost,total_price FROM quote_line_items WHERE quote_id=? AND (is_optional=0 OR is_optional IS NULL)",
        (quote_id,)
    ).fetchall()
    total_cost = sum(float(i['total_cost'] or 0) for i in items)
    total_price = sum(float(i['total_price'] or 0) for i in items)
    margin = ((total_price - total_cost) / total_price * 100) if total_price else 0
    policy = db.execute("SELECT * FROM commission_policy WHERE active='Y' LIMIT 1").fetchone()
    commission = _compute_commission(policy, total_cost, total_price)
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
        row = db.execute("SELECT rate, unit, markup_pct FROM sub_rates WHERE sub_id=? AND work_type_id=?",
                         (sub_id, work_type_id)).fetchone()
    else:
        row = None
    return jsonify({
        'rate': row['rate'] if row else None,
        'unit': row['unit'] if row else None,
        'markup_pct': row['markup_pct'] if row and 'markup_pct' in row.keys() else None,
    })

@app.route('/api/work_type_defaults')
@login_required
def work_type_defaults():
    db = get_db()
    wt_id = request.args.get('work_type_id')
    wt = db.execute("SELECT default_markup,min_markup,min_margin,unit,default_material_id FROM work_types WHERE work_type_id=?", (wt_id,)).fetchone()
    if not wt:
        return jsonify({'default_markup': 30, 'min_markup': 10, 'min_margin': 9.1, 'unit': '',
                        'material_id': None, 'material_cost': 0})
    material_id = None
    material_cost = 0
    if wt['default_material_id']:
        mat = db.execute("SELECT cost_per_quote_unit FROM materials WHERE material_id=?", (wt['default_material_id'],)).fetchone()
        if mat:
            material_id = wt['default_material_id']
            material_cost = mat['cost_per_quote_unit']
    return jsonify({'default_markup': wt['default_markup'], 'min_markup': wt['min_markup'],
                    'min_margin': wt['min_margin'], 'unit': wt['unit'],
                    'material_id': material_id, 'material_cost': material_cost})

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
    all_attachments = db.execute("SELECT * FROM sub_attachments ORDER BY sub_id, uploaded_at DESC").fetchall()
    return render_template('admin_subs.html', subs=subs, applicators=applicators,
                           all_sub_rates=all_sub_rates, all_applicator_rates=all_applicator_rates,
                           surface_products=surface_products, work_types=work_types,
                           next_sub_id=next_sub_id, next_app_id=next_app_id,
                           all_attachments=all_attachments)

@app.route('/admin/subs/add', methods=['POST'])
@require_permission('can_edit_subs')
def add_sub():
    db = get_db()
    table = request.form.get('table','subs')
    name = request.form['name'].strip()
    phone = request.form.get('phone','').strip()
    if table == 'applicators':
        # Auto-increment P prefix
        rows = db.execute("SELECT sub_id FROM surface_applicators").fetchall()
        nums = []
        for r in rows:
            try: nums.append(int(r['sub_id'].replace('P','').replace('p','')))
            except: pass
        next_id = 'P' + str(max(nums) + 1 if nums else 1)
        db.execute("INSERT INTO surface_applicators (sub_id,name,active,phone) VALUES (?,?,'Y',?)", (next_id, name, phone))
    else:
        # Auto-increment S prefix
        rows = db.execute("SELECT sub_id FROM subs").fetchall()
        nums = []
        for r in rows:
            try: nums.append(int(r['sub_id'].replace('S','').replace('s','')))
            except: pass
        next_id = 'S' + str(max(nums) + 1 if nums else 1)
        poc_name = request.form.get('poc_name','').strip()
        email = request.form.get('email','').strip()
        db.execute("INSERT INTO subs (sub_id,name,active,phone,poc_name,email) VALUES (?,?,'Y',?,?,?)",
                   (next_id, name, phone, poc_name, email))
    db.commit()
    return redirect(url_for('admin_subs'))

@app.route('/admin/subs/toggle', methods=['POST'])
@require_permission('can_edit_subs')
def toggle_sub():
    db = get_db()
    sub_id = request.form['sub_id']
    table = request.form.get('table','subs')
    if table not in ('subs', 'surface_applicators'):
        table = 'subs'
    current = db.execute(f"SELECT active FROM {table} WHERE sub_id=?", (sub_id,)).fetchone()
    new_val = 'N' if current['active'] == 'Y' else 'Y'
    db.execute(f"UPDATE {table} SET active=? WHERE sub_id=?", (new_val, sub_id))
    db.commit()
    return redirect(url_for('admin_subs'))

@app.route('/admin/subs/edit', methods=['POST'])
@require_permission('can_edit_subs')
def edit_sub():
    db = get_db()
    sub_id = request.form['sub_id']
    table = request.form.get('table','subs')
    if table not in ('subs', 'surface_applicators'):
        table = 'subs'
    name = request.form['name'].strip()
    phone = request.form.get('phone','').strip()
    if table == 'subs':
        poc_name = request.form.get('poc_name','').strip()
        email = request.form.get('email','').strip()
        db.execute("UPDATE subs SET name=?, phone=?, poc_name=?, email=? WHERE sub_id=?",
                   (name, phone, poc_name, email, sub_id))
    else:
        db.execute(f"UPDATE {table} SET name=?, phone=? WHERE sub_id=?", (name, phone, sub_id))
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
                  is_modifier,modified_by,show_on_quote,active,description,estimated_days,estimated_days_margin,timeline_exempt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (request.form['work_type'], request.form['unit'], request.form['cost_structure'],
                float(request.form.get('default_markup',30) or 30),
                float(request.form.get('min_markup',10) or 10),
                float(request.form.get('min_margin',9.1) or 9.1),
                request.form.get('is_modifier','N'), request.form.get('modified_by',''),
                request.form.get('show_on_quote','Y'), 'Y', request.form.get('description',''),
                float(request.form.get('estimated_days',0) or 0),
                float(request.form.get('estimated_days_margin',0) or 0),
                request.form.get('timeline_exempt','N')))
    db.commit()
    return redirect(url_for('admin_work_types'))

@app.route('/admin/work_types/edit/<int:wt_id>', methods=['POST'])
@require_permission('can_edit_work_types')
def edit_work_type(wt_id):
    db = get_db()
    db.execute("""UPDATE work_types SET work_type=?,unit=?,cost_structure=?,default_markup=?,min_markup=?,min_margin=?,min_job_price=?,estimated_days=?,estimated_days_margin=?,timeline_exempt=?,active=?,description=?
                  WHERE work_type_id=?""",
               (request.form['work_type'],
                request.form.get('unit','each'), request.form.get('cost_structure','labor_only'),
                float(request.form.get('default_markup',30) or 30),
                float(request.form.get('min_markup',10) or 10),
                float(request.form.get('min_margin',9.1) or 9.1),
                float(request.form.get('min_job_price',0) or 0),
                float(request.form.get('estimated_days',0) or 0),
                float(request.form.get('estimated_days_margin',0) or 0),
                request.form.get('timeline_exempt','N'),
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

@app.route('/admin/sub_rates/edit/<int:rate_id>', methods=['POST'])
@require_permission('can_edit_sub_rates')
def edit_sub_rate(rate_id):
    db = get_db()
    min_total_cost = request.form.get('min_total_cost', '').strip()
    db.execute("UPDATE sub_rates SET rate=?, notes=?, min_total_cost=? WHERE id=?",
               (float(request.form['rate']), request.form.get('notes', ''),
                float(min_total_cost) if min_total_cost else None, rate_id))
    db.commit()
    return redirect(request.form.get('return_to') or url_for('admin_sub_rates'))

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
    db.execute("""UPDATE commission_policy SET method=?,rate=?,active=?,
                  tier1_threshold=?,tier2_threshold=?,tier1_rate=?,tier2_rate=?,tier3_rate=?
                  WHERE id=?""",
               (request.form['method'], float(request.form.get('rate',0) or 0),
                request.form.get('active','Y'),
                float(request.form.get('tier1_threshold',20) or 20),
                float(request.form.get('tier2_threshold',30) or 30),
                float(request.form.get('tier1_rate',8) or 8),
                float(request.form.get('tier2_rate',10) or 10),
                float(request.form.get('tier3_rate',12) or 12),
                policy_id))
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

@app.route('/admin/permissions/update_email', methods=['POST'])
@require_permission('can_edit_commission_policy')
def update_email():
    db = get_db()
    db.execute("UPDATE users SET email=? WHERE user_id=?",
               (request.form['email'].strip(), request.form['user_id']))
    db.commit()
    return redirect(url_for('admin_permissions'))


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/quotes/<int:quote_id>/payment_schedule/update', methods=['POST'])
@login_required
def update_payment_schedule(quote_id):
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    data = request.json
    for item in data.get('items', []):
        db.execute("UPDATE payment_schedules SET label=?,amount=?,pct=? WHERE id=? AND quote_id=?",
                   (item['label'], float(item['amount']), float(item['pct']), item['id'], quote_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/quotes/<int:quote_id>/payment_schedule/<int:schedule_id>/collect', methods=['POST'])
@login_required
def collect_payment_schedule_item(quote_id, schedule_id):
    """Marks a payment_schedules row collected/uncollected. Deliberately NOT guarded by
    _is_locked_contract -- tracking what's actually been paid must keep working on a
    locked Contract, that's the whole point of this existing alongside the lock."""
    db = get_db()
    data = request.json or {}
    collected = bool(data.get('collected'))
    if collected:
        collected_amount = float(data.get('collected_amount') or 0)
        collected_date = data.get('collected_date') or ''
        db.execute("UPDATE payment_schedules SET collected=1,collected_amount=?,collected_date=? WHERE id=? AND quote_id=?",
                   (collected_amount, collected_date, schedule_id, quote_id))
    else:
        db.execute("UPDATE payment_schedules SET collected=0,collected_amount=NULL,collected_date='' WHERE id=? AND quote_id=?",
                   (schedule_id, quote_id))
    db.commit()
    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(collected_amount),0) FROM payment_schedules WHERE quote_id=? AND collected=1", (quote_id,)
    ).fetchone()[0] or 0)
    return jsonify({'success': True, 'total_paid': round(total_paid, 2)})

@app.route('/quotes/<int:quote_id>/set_terms_document', methods=['POST'])
@login_required
def set_quote_terms_document(quote_id):
    db = get_db()
    terms_document_id = request.json.get('terms_document_id') or None
    db.execute("UPDATE quotes SET terms_document_id=? WHERE quote_id=?", (terms_document_id, quote_id))
    db.commit()
    return jsonify({'terms_document_id': terms_document_id})

@app.route('/quotes/<int:quote_id>/payment_schedule/regenerate', methods=['POST'])
@login_required
def regenerate_payment_schedule(quote_id):
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    terms_docs = db.execute("SELECT * FROM terms_documents WHERE active=1 ORDER BY is_default DESC, label").fetchall()
    return render_template('admin_settings.html', settings=settings, terms_docs=terms_docs)

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

@app.route('/admin/settings/terms/add', methods=['POST'])
@require_permission('can_edit_commission_policy')
def add_terms_document():
    db = get_db()
    label = request.form.get('label', '').strip()
    body_text = request.form.get('body_text', '')
    if not label:
        return redirect(url_for('admin_settings'))
    pdf_data = ''
    f = request.files.get('pdf')
    if f and f.filename:
        file_bytes = f.read()
        if _validate_pdf_upload(file_bytes, f.filename):
            import base64
            pdf_data = base64.b64encode(file_bytes).decode('utf-8')
    db.execute("INSERT INTO terms_documents (label,body_text,pdf_data) VALUES (?,?,?)",
               (label, body_text, pdf_data))
    db.commit()
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/terms/<int:doc_id>/set_default', methods=['POST'])
@require_permission('can_edit_commission_policy')
def set_default_terms_document(doc_id):
    db = get_db()
    db.execute("UPDATE terms_documents SET is_default=0")
    db.execute("UPDATE terms_documents SET is_default=1 WHERE id=?", (doc_id,))
    db.commit()
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/terms/<int:doc_id>/delete', methods=['POST'])
@require_permission('can_edit_commission_policy')
def delete_terms_document(doc_id):
    db = get_db()
    row = db.execute("SELECT is_default FROM terms_documents WHERE id=?", (doc_id,)).fetchone()
    if row and not row['is_default']:
        db.execute("UPDATE terms_documents SET active=0 WHERE id=?", (doc_id,))
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
    terms_doc = db.execute("SELECT * FROM terms_documents WHERE id=?", (quote['terms_document_id'],)).fetchone() if quote['terms_document_id'] else None
    visualization = db.execute(
        "SELECT * FROM quote_visualizations WHERE quote_id=? AND status='done' AND generated_image IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1", (quote_id,)
    ).fetchone()
    from datetime import date, timedelta
    today = date.today()
    valid_until = today + timedelta(days=settings['quote_validity_days'] if settings else 30)
    return render_template('quote_preview.html', quote=quote, line_items=line_items,
                           optional_items=optional_items, declined_items=declined_items,
                           schedule=schedule, settings=settings, current_role=g.role,
                           terms_doc=terms_doc, visualization=visualization,
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

def _append_terms_pdf(pdf_bytes, terms_doc):
    """Appends a terms document's own PDF pages onto the end of a generated quote PDF.
    A no-op when the quote has no terms document selected or that document has no uploaded
    PDF (the plain-text fallback renders inline in the HTML before this ever runs, so
    there's nothing to append in that case)."""
    if not terms_doc or not terms_doc['pdf_data']:
        return pdf_bytes
    from pypdf import PdfReader, PdfWriter
    import base64, io
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes)))
    writer.append(PdfReader(io.BytesIO(base64.b64decode(terms_doc['pdf_data']))))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

def _validate_pdf_upload(file_bytes, filename):
    """No upload validation precedent exists anywhere in this codebase (upload_logo has
    none) -- this is the first, so kept simple: extension, magic bytes, and a size cap."""
    if not filename or not filename.lower().endswith('.pdf'):
        return False
    if not file_bytes.startswith(b'%PDF-'):
        return False
    if len(file_bytes) > 10 * 1024 * 1024:
        return False
    return True

ATTACHMENT_MIME_TYPES = {
    'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'heic': 'image/heic', 'heif': 'image/heif',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'xls': 'application/vnd.ms-excel', 'csv': 'text/csv',
}
# Browsers reliably render these as an inline <img> from a data URI -- HEIC (the default
# iPhone photo format) is NOT included: Safari renders it but Chrome/Firefox don't, so a
# HEIC upload gets a plain file/download treatment instead of a thumbnail rather than
# silently showing a broken image for most viewers.
INLINE_IMAGE_MIME_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

def _validate_file_upload(file_bytes, filename):
    """Shared upload validation for sub attachments (COI/Workers-Comp/price-sheet) and
    customer attachments (pool photos, documents from a sales conversation) -- both can be a
    PDF, a photo, or a spreadsheet, unlike terms_documents, which is always a PDF. Extension
    allowlist + a 15MB cap (a bit above the original 10MB PDF-only precedent, to give
    scanned/photographed docs headroom); PDF also gets the same magic-byte check as
    _validate_pdf_upload since that's cheap and catches a mislabeled file.
    Returns the matched extension (for the mime-type lookup) or None if invalid."""
    if not filename or '.' not in filename:
        return None
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in ATTACHMENT_MIME_TYPES:
        return None
    if len(file_bytes) > 15 * 1024 * 1024:
        return None
    if ext == 'pdf' and not file_bytes.startswith(b'%PDF-'):
        return None
    return ext

@app.route('/admin/subs/<sub_id>/attachments/add', methods=['POST'])
@require_permission('can_edit_subs')
def add_sub_attachment(sub_id):
    db = get_db()
    f = request.files.get('file')
    category = request.form.get('category', 'other')
    if f and f.filename:
        file_bytes = f.read()
        ext = _validate_file_upload(file_bytes, f.filename)
        if ext:
            import base64
            db.execute(
                "INSERT INTO sub_attachments (sub_id, category, filename, mime_type, file_data) VALUES (?,?,?,?,?)",
                (sub_id, category, f.filename, ATTACHMENT_MIME_TYPES[ext], base64.b64encode(file_bytes).decode('utf-8'))
            )
            db.commit()
    return redirect(url_for('admin_subs'))

@app.route('/admin/subs/attachments/<int:attachment_id>/download')
@require_permission('can_access_admin')
def download_sub_attachment(attachment_id):
    db = get_db()
    row = db.execute("SELECT * FROM sub_attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        return redirect(url_for('admin_subs'))
    import base64
    data = base64.b64decode(row['file_data'])
    return Response(data, mimetype=row['mime_type'],
                     headers={'Content-Disposition': f'attachment; filename="{row["filename"]}"'})

@app.route('/admin/subs/attachments/<int:attachment_id>/delete', methods=['POST'])
@require_permission('can_edit_subs')
def delete_sub_attachment(attachment_id):
    db = get_db()
    db.execute("DELETE FROM sub_attachments WHERE id=?", (attachment_id,))
    db.commit()
    return redirect(url_for('admin_subs'))

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

def _send_plain_email(to_email, subject, body_text, gmail_address, gmail_app_password):
    """Same delivery mechanism as _send_quote_email, without the PDF attachment it hard-
    requires -- used for the schedule confirm-and-notify step, which has nothing to attach."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart()
    msg['From'] = gmail_address
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text))
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

def _default_quote_email(quote, settings, quote_id):
    company_name = settings['company_name'] if settings else 'Your Company'
    subject = f"Your Quote from {company_name} — QT-{quote_id:04d}"
    body = (f"Hi {quote['customer_name']},\n\n"
            f"Please find your quote attached.\n\n"
            f"Thank you,\n{quote['salesperson'] or company_name}")
    return subject, body

@app.route('/quotes/<int:quote_id>/email_defaults')
@login_required
def quote_email_defaults(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if not quote:
        return jsonify({'error': 'Quote not found'}), 404
    settings = db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    subject, body = _default_quote_email(quote, settings, quote_id)
    return jsonify({'subject': subject, 'body': body})

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
    default_subject, default_body = _default_quote_email(quote, settings, quote_id)
    data = request.get_json(silent=True) or {}
    subject = (data.get('subject') or '').strip() or default_subject
    body = data.get('body') if data.get('body') is not None and data.get('body').strip() else default_body
    try:
        html = _quote_preview_html(quote_id)
        pdf_bytes = _generate_quote_pdf_bytes(html)
        terms_doc = db.execute("SELECT * FROM terms_documents WHERE id=?", (quote['terms_document_id'],)).fetchone() if quote['terms_document_id'] else None
        pdf_bytes = _append_terms_pdf(pdf_bytes, terms_doc)
        _send_quote_email(to_email, subject, body, pdf_bytes, quote_id, gmail_address, gmail_app_password)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    db.execute("UPDATE quotes SET status='sent' WHERE quote_id=? AND status='draft'", (quote_id,))
    if not _is_locked_contract(db, quote_id):
        _snapshot_quote_version(db, quote_id)
    db.commit()
    return jsonify({'success': True, 'sent_to': to_email})

def _snapshot_quote_version(db, quote_id):
    """Freezes the quote's current state as a new numbered version -- called every time
    email_quote() successfully (re-)sends, first send included, so 'Version 1' always
    exists once a customer has seen anything. Guarded to pre-signature quotes only
    (_is_locked_contract) at the call site -- Change Orders own versioning after signing,
    a re-sent contract PDF (e.g. for the customer's records) shouldn't create a quote
    version of its own."""
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    next_num = db.execute(
        "SELECT COALESCE(MAX(version_number),0)+1 as n FROM quote_versions WHERE quote_id=?", (quote_id,)
    ).fetchone()['n']
    sent_by = g.user['display_name'] if g.user and g.user['display_name'] else (g.user['username'] if g.user else '')
    cur = db.execute(
        "INSERT INTO quote_versions (quote_id, version_number, total_cost, total_price, total_margin, commission, sent_by) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (quote_id, next_num, quote['total_cost'], quote['total_price'], quote['total_margin'], quote['commission'], sent_by)
    )
    version_id = cur.fetchone()[0]
    items = db.execute("SELECT * FROM quote_line_items WHERE quote_id=? ORDER BY sort_order", (quote_id,)).fetchall()
    for item in items:
        db.execute(
            "INSERT INTO quote_version_line_items (version_id, work_type_label, sub_name, "
            "labor_quantity, labor_unit, labor_cost_per_unit, labor_total_cost, labor_markup_pct, labor_margin_pct, labor_total_price, "
            "material_label, material_quantity, material_unit, material_cost_per_unit, material_total_cost, material_markup_pct, material_margin_pct, material_total_price, "
            "product_label, total_cost, total_price, total_margin_pct, sort_order, description, is_optional, is_declined) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, item['work_type_label'], item['sub_name'],
             item['labor_quantity'], item['labor_unit'], item['labor_cost_per_unit'], item['labor_total_cost'],
             item['labor_markup_pct'], item['labor_margin_pct'], item['labor_total_price'],
             item['material_label'], item['material_quantity'], item['material_unit'], item['material_cost_per_unit'],
             item['material_total_cost'], item['material_markup_pct'], item['material_margin_pct'], item['material_total_price'],
             item['product_label'], item['total_cost'], item['total_price'], item['total_margin_pct'],
             item['sort_order'], item['description'], item['is_optional'], item['is_declined'])
        )

@app.route('/quotes/<int:quote_id>/versions/<int:version_id>')
@login_required
def quote_version_detail(quote_id, version_id):
    db = get_db()
    quote = db.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    version = db.execute("SELECT * FROM quote_versions WHERE id=? AND quote_id=?", (version_id, quote_id)).fetchone()
    if not quote or not version:
        return redirect(url_for('edit_quote', quote_id=quote_id))
    items = db.execute(
        "SELECT * FROM quote_version_line_items WHERE version_id=? ORDER BY sort_order", (version_id,)
    ).fetchall()
    line_items = [dict(i) for i in items if not i['is_optional']]
    optional_items = [dict(i) for i in items if i['is_optional'] and not i['is_declined']]
    declined_items = [dict(i) for i in items if i['is_optional'] and i['is_declined']]
    return render_template('quote_version.html', quote=quote, version=version,
                           line_items=line_items, optional_items=optional_items, declined_items=declined_items)

@app.route('/quotes/<int:quote_id>/line_items/<int:item_id>/decline', methods=['POST'])
@login_required
def decline_optional_item(quote_id, item_id):
    db = get_db()
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    db.execute("UPDATE quotes SET signature_data=?,signed_at=?,signed_name=?,status='contract' WHERE quote_id=?",
               (signature_data, signed_at, printed_name, quote_id))
    db.commit()
    return jsonify({'success': True, 'signed_at': signed_at})

@app.route('/quotes/<int:quote_id>/unsign', methods=['POST'])
@login_required
def unsign_quote(quote_id):
    """Also doubles as "unlock" for a signed Contract — reverting status lets its
    line items and details be edited again. Signed Change Orders are unaffected;
    they remain standalone signed history regardless of the parent's lock state."""
    if not (g.role and g.role['can_override_min_markup']):
        return jsonify({'error': 'Not permitted'}), 403
    db = get_db()
    db.execute("UPDATE quotes SET signature_data='',signed_at='',signed_name='',status='sent' WHERE quote_id=?", (quote_id,))
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

        eligible_tiles = _eligible_tiles_for_package(db, pkg['package_id'], manufacturer='all')
        packages_with_items.append({'pkg': pkg, 'line_items': items, 'eligible_tiles': eligible_tiles})
    work_types = db.execute("SELECT * FROM work_types WHERE active='Y' ORDER BY work_type").fetchall()
    return render_template('admin_packages.html', packages_with_items=packages_with_items, work_types=work_types)

@app.route('/admin/packages/<int:package_id>/edit', methods=['POST'])
@require_permission('can_access_admin')
def edit_package(package_id):
    db = get_db()
    threshold = request.form.get('tile_price_threshold', '').strip()
    surface_threshold = request.form.get('surface_price_threshold', '').strip()
    db.execute("""UPDATE packages SET name=?,badge=?,price_range_label=?,life_expectancy_label=?,description=?,
                  tile_price_threshold=?,surface_price_threshold=?
                  WHERE package_id=?""",
               (request.form['name'], request.form.get('badge',''),
                request.form.get('price_range_label',''), request.form.get('life_expectancy_label',''),
                request.form.get('description',''), float(threshold) if threshold else None,
                float(surface_threshold) if surface_threshold else None, package_id))
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

@app.route('/admin/packages/items/<int:item_id>/edit', methods=['POST'])
@require_permission('can_access_admin')
def edit_package_item(item_id):
    db = get_db()
    sub_id = request.form.get('sub_id', '')
    material_id = request.form.get('material_id') or None
    db.execute("UPDATE package_items SET default_sub_id=?, default_material_id=? WHERE id=?",
               (sub_id, material_id, item_id))
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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
        mat_cpu = 0.0
        material_label = ''
        if material_id:
            m = db.execute("""SELECT m.series, m.cost_per_quote_unit, s.name as supplier_name FROM materials m
                              JOIN suppliers s ON m.supplier_id=s.supplier_id
                              WHERE m.material_id=?""", (material_id,)).fetchone()
            if m:
                material_label = f"{m['supplier_name']} — {m['series']}"
                mat_cpu = float(m['cost_per_quote_unit'])
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
        'labor_total_cost': float(updated['labor_total_cost'] or 0),
        'labor_cost_per_unit': float(updated['labor_cost_per_unit'] or 0),
        'labor_margin_pct': float(updated['labor_margin_pct'] or 0),
        'total_margin_pct': float(updated['total_margin_pct']),
        'total_cost': float(updated['total_cost']),
        'total_price': float(updated['total_price']),
        'material_total_price': float(updated['material_total_price'] or 0),
        'material_total_cost': float(updated['material_total_cost'] or 0),
        'material_cost_per_unit': float(updated['material_cost_per_unit'] or 0),
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
    data = request.json
    qualifier_id = int(data.get('qualifier_id'))
    checked = bool(data.get('checked'))
    row = db.execute("SELECT * FROM quote_line_items WHERE id=? AND quote_id=?", (item_id, quote_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    quals = json.loads(row['qualifiers_json'] or '[]')
    quals = [q for q in quals if q['id'] != qualifier_id]
    qrow = db.execute("SELECT * FROM qualifiers WHERE qualifier_id=?", (qualifier_id,)).fetchone()
    if checked:
        if qrow:
            quals.append({'id': qualifier_id, 'label': qrow['label'], 'amount': float(qrow['amount']), 'per_unit': bool(qrow['per_unit'])})
    qualifiers_total_cost = round(sum(_qualifier_cost(q, row) for q in quals), 2)
    db.execute("UPDATE quote_line_items SET qualifiers_json=?,qualifiers_total_cost=? WHERE id=?",
               (json.dumps(quals), qualifiers_total_cost, item_id))
    total_cost, total_price, total_margin = _rollup_item_totals(db, item_id)

    # Freight can only be charged once per quote — checking it here clears it from
    # any other line item on this same quote.
    other_item_cleared = False
    if checked and qrow and qrow['is_freight']:
        other_items = db.execute(
            "SELECT * FROM quote_line_items WHERE quote_id=? AND id!=?",
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
                filtered_cost = round(sum(_qualifier_cost(q, other) for q in filtered), 2)
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
    if _is_locked_contract(db, quote_id):
        return jsonify({'error': 'This quote is a signed contract — changes go through a Change Order.'}), 409
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

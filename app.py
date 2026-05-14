import os
import io
import re
import csv
import json
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic
import requests as http_requests

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INVITE_CODE       = os.environ.get("INVITE_CODE", "")

# Railway PostgreSQL sets DATABASE_URL automatically
_db_url = os.environ.get("DATABASE_URL", "")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
DATABASE_URL = _db_url

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def _exec(db, sql, args=()):
    cur = db.cursor()
    cur.execute(sql, args)
    return cur

def _fetchone(db, sql, args=()):
    cur = _exec(db, sql, args)
    return cur.fetchone()

def _fetchall(db, sql, args=()):
    cur = _exec(db, sql, args)
    return cur.fetchall()

def _insert(db, sql, args=()):
    """Execute INSERT ... RETURNING id and return the new id."""
    cur = _exec(db, sql, args)
    row = cur.fetchone()
    return row["id"] if row else None

def init_db():
    with app.app_context():
        db = get_db()

        # Users
        _exec(db, """
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                email         TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Meals
        _exec(db, """
            CREATE TABLE IF NOT EXISTS meals (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL DEFAULT 1,
                eaten_at    TEXT    NOT NULL,
                description TEXT    NOT NULL,
                image_b64   TEXT,
                calories    REAL,
                protein_g   REAL,
                carbs_g     REAL,
                fat_g       REAL,
                fiber_g     REAL,
                analysis    TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _exec(db, "ALTER TABLE meals ADD COLUMN IF NOT EXISTS user_id INTEGER NOT NULL DEFAULT 1")

        # Weights
        _exec(db, """
            CREATE TABLE IF NOT EXISTS weights (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL DEFAULT 1,
                weighed_at TEXT    NOT NULL,
                weight_kg  REAL    NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _exec(db, "ALTER TABLE weights ADD COLUMN IF NOT EXISTS user_id INTEGER NOT NULL DEFAULT 1")

        # Goals — one row per user
        _exec(db, """
            CREATE TABLE IF NOT EXISTS goals (
                user_id    INTEGER PRIMARY KEY,
                calories   REAL,
                protein_g  REAL,
                carbs_g    REAL,
                fat_g      REAL,
                fiber_g    REAL,
                water_ml   INTEGER NOT NULL DEFAULT 2000,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _exec(db, "ALTER TABLE goals ADD COLUMN IF NOT EXISTS water_ml INTEGER NOT NULL DEFAULT 2000")

        # Water tracking
        _exec(db, """
            CREATE TABLE IF NOT EXISTS water (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                logged_at  TEXT    NOT NULL,
                amount_ml  INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Favorites (usein syödyt ateriat)
        _exec(db, """
            CREATE TABLE IF NOT EXISTS favorites (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT,
                calories    REAL,
                protein_g   REAL,
                carbs_g     REAL,
                fat_g       REAL,
                fiber_g     REAL,
                image_b64   TEXT,
                analysis    TEXT,
                use_count   INTEGER NOT NULL DEFAULT 0,
                last_used   TIMESTAMPTZ,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        db.commit()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Kirjaudu sisään"}), 401
        return f(*args, **kwargs)
    return decorated

def current_user_id():
    return session["user_id"]

# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
def register():
    data     = request.get_json(force=True)
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    code     = data.get("invite_code") or ""

    if not email or not password:
        return jsonify({"error": "Sähköposti ja salasana vaaditaan"}), 400
    if INVITE_CODE and code != INVITE_CODE:
        return jsonify({"error": "Väärä kutsukoodi"}), 403
    if len(password) < 6:
        return jsonify({"error": "Salasanan oltava vähintään 6 merkkiä"}), 400

    db = get_db()
    if _fetchone(db, "SELECT id FROM users WHERE email=%s", (email,)):
        return jsonify({"error": "Sähköposti on jo käytössä"}), 409

    new_id = _insert(db,
        "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
        (email, generate_password_hash(password))
    )
    # Default goals for new user
    _exec(db, """
        INSERT INTO goals (user_id, calories, protein_g, carbs_g, fat_g, fiber_g)
        VALUES (%s, 2000, 120, 220, 65, 30)
        ON CONFLICT (user_id) DO NOTHING
    """, (new_id,))
    db.commit()

    session["user_id"] = new_id
    session["email"]   = email
    return jsonify({"id": new_id, "email": email}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.get_json(force=True)
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    db  = get_db()
    row = _fetchone(db, "SELECT * FROM users WHERE email=%s", (email,))
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Väärä sähköposti tai salasana"}), 401

    session["user_id"] = row["id"]
    session["email"]   = row["email"]
    return jsonify({"id": row["id"], "email": row["email"]})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def me():
    if session.get("user_id"):
        return jsonify({"id": session["user_id"], "email": session.get("email")})
    return jsonify({"user": None}), 200

# ---------------------------------------------------------------------------
# AI food analysis
# ---------------------------------------------------------------------------

ITEMS_SCHEMA = """
{
  "description": "Lyhyt kuvaus annoksesta suomeksi",
  "items": [{"name": "Ruoka-aineen nimi suomeksi", "weight_g": <paino grammoina>}],
  "calories": <kcal>, "protein_g": <g>, "carbs_g": <g>, "fat_g": <g>, "fiber_g": <g>,
  "notes": "Epävarmuudet tms"
}"""

VISION_PROMPT = f"Olet ravitsemustieteilijä-AI. Analysoi kuva, tunnista ruoka-aineet ja arvioi painot grammoina.\nPalauta JSON:{ITEMS_SCHEMA}\nPalauta VAIN JSON."
TEXT_PROMPT   = f"Olet ravitsemustieteilijä-AI. Arvioi ravintoarvot tekstikuvauksen perusteella.\nPalauta JSON:{ITEMS_SCHEMA}\nPalauta VAIN JSON."
RECALC_PROMPT = f"Olet ravitsemustieteilijä-AI. Laske ravintoarvot annetulle ruoka-ainelistalle.\nPalauta JSON:{ITEMS_SCHEMA}\nSäilytä items-lista sellaisenaan. Palauta VAIN JSON."

def parse_ai_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

def _claude(system, messages):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=1024,
        system=system, messages=messages
    ).content[0].text

def analyze_food_image(image_b64, media_type="image/jpeg"):
    raw = _claude(VISION_PROMPT, [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
        {"type": "text", "text": "Analysoi tämä ruoka-annos."}
    ]}])
    return parse_ai_json(raw)

def analyze_food_text(desc):
    return parse_ai_json(_claude(TEXT_PROMPT, [{"role": "user", "content": f"Arvioi: {desc}"}]))

def recalculate_from_items(items):
    txt = "\n".join(f"- {it['name']}: {it['weight_g']} g" for it in items)
    result = parse_ai_json(_claude(RECALC_PROMPT, [{"role": "user", "content": f"Ruoka-aineet:\n{txt}"}]))
    result["items"] = items
    return result

# ---------------------------------------------------------------------------
# Meals API
# ---------------------------------------------------------------------------

@app.route("/api/meals", methods=["POST"])
@auth_required
def add_meal():
    uid  = current_user_id()
    data = request.get_json(force=True)
    image_b64   = data.get("image_b64")
    media_type  = data.get("media_type", "image/jpeg")
    text_desc   = data.get("text_description")
    eaten_at    = data.get("eaten_at", datetime.now().isoformat())
    precomputed = data.get("_precomputed")

    if precomputed:
        analysis = precomputed
    else:
        if not ANTHROPIC_API_KEY:
            return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500
        if not image_b64 and not text_desc:
            return jsonify({"error": "image_b64 or text_description required"}), 400
        try:
            analysis = analyze_food_image(image_b64, media_type) if image_b64 else analyze_food_text(text_desc)
        except Exception as e:
            return jsonify({"error": f"AI analysis failed: {str(e)}"}), 500

    db = get_db()
    new_id = _insert(db, """
        INSERT INTO meals (user_id, eaten_at, description, image_b64, calories, protein_g, carbs_g, fat_g, fiber_g, analysis)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (uid, eaten_at, analysis.get("description", ""), image_b64,
          analysis.get("calories"), analysis.get("protein_g"),
          analysis.get("carbs_g"), analysis.get("fat_g"), analysis.get("fiber_g"),
          json.dumps(analysis, ensure_ascii=False)))
    db.commit()
    return jsonify({"id": new_id, "analysis": analysis}), 201


@app.route("/api/meals", methods=["GET"])
@auth_required
def list_meals():
    uid = current_user_id()
    day = request.args.get("date", date.today().isoformat())
    db  = get_db()
    rows = _fetchall(db,
        "SELECT * FROM meals WHERE user_id=%s AND eaten_at::date=%s::date ORDER BY eaten_at DESC",
        (uid, day)
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/meals/<int:meal_id>/recalculate", methods=["POST"])
@auth_required
def recalculate_meal(meal_id):
    uid  = current_user_id()
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items required"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500
    try:
        analysis = recalculate_from_items(items)
    except Exception as e:
        return jsonify({"error": f"Recalculation failed: {str(e)}"}), 500
    db = get_db()
    _exec(db, """
        UPDATE meals
        SET description=%s, calories=%s, protein_g=%s, carbs_g=%s, fat_g=%s, fiber_g=%s, analysis=%s
        WHERE id=%s AND user_id=%s
    """, (analysis.get("description", ""), analysis.get("calories"), analysis.get("protein_g"),
          analysis.get("carbs_g"), analysis.get("fat_g"), analysis.get("fiber_g"),
          json.dumps(analysis, ensure_ascii=False), meal_id, uid))
    db.commit()
    row = _fetchone(db, "SELECT * FROM meals WHERE id=%s AND user_id=%s", (meal_id, uid))
    return jsonify({"meal": dict(row), "analysis": analysis})


@app.route("/api/meals/<int:meal_id>", methods=["PATCH"])
@auth_required
def update_meal(meal_id):
    uid  = current_user_id()
    data = request.get_json(force=True)
    allowed = ["description", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g"]
    fields = {k: data[k] for k in allowed if k in data}
    if not fields:
        return jsonify({"error": "No valid fields"}), 400
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    db = get_db()
    _exec(db, f"UPDATE meals SET {set_clause} WHERE id=%s AND user_id=%s",
          (*fields.values(), meal_id, uid))
    db.commit()
    row = _fetchone(db, "SELECT * FROM meals WHERE id=%s AND user_id=%s", (meal_id, uid))
    return jsonify(dict(row))


@app.route("/api/meals/<int:meal_id>", methods=["DELETE"])
@auth_required
def delete_meal(meal_id):
    uid = current_user_id()
    db  = get_db()
    _exec(db, "DELETE FROM meals WHERE id=%s AND user_id=%s", (meal_id, uid))
    db.commit()
    return jsonify({"deleted": meal_id})


@app.route("/api/summary")
@auth_required
def daily_summary():
    uid = current_user_id()
    day = request.args.get("date", date.today().isoformat())
    db  = get_db()
    row = _fetchone(db, """
        SELECT COUNT(*) AS meal_count,
               ROUND(SUM(calories)::numeric, 1)   AS calories,
               ROUND(SUM(protein_g)::numeric, 1)  AS protein_g,
               ROUND(SUM(carbs_g)::numeric, 1)    AS carbs_g,
               ROUND(SUM(fat_g)::numeric, 1)      AS fat_g,
               ROUND(SUM(fiber_g)::numeric, 1)    AS fiber_g
        FROM meals WHERE user_id=%s AND eaten_at::date=%s::date
    """, (uid, day))
    return jsonify({"date": day, **dict(row)})


@app.route("/api/history")
@auth_required
def history():
    uid = current_user_id()
    db  = get_db()
    rows = _fetchall(db, """
        SELECT eaten_at::date AS day, COUNT(*) AS meal_count,
               ROUND(SUM(calories)::numeric, 0) AS calories
        FROM meals WHERE user_id=%s
        GROUP BY eaten_at::date
        ORDER BY eaten_at::date DESC
        LIMIT 30
    """, (uid,))
    return jsonify([{**dict(r), "day": str(r["day"])} for r in rows])

# ---------------------------------------------------------------------------
# Goals API
# ---------------------------------------------------------------------------

@app.route("/api/goals", methods=["GET", "POST"])
@auth_required
def goals_endpoint():
    uid = current_user_id()
    db  = get_db()
    if request.method == "POST":
        data    = request.get_json(force=True)
        allowed = ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g"]
        fields  = {k: data[k] for k in allowed if k in data}
        _exec(db, """
            INSERT INTO goals (user_id, calories, protein_g, carbs_g, fat_g, fiber_g, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                calories=EXCLUDED.calories, protein_g=EXCLUDED.protein_g,
                carbs_g=EXCLUDED.carbs_g, fat_g=EXCLUDED.fat_g,
                fiber_g=EXCLUDED.fiber_g, updated_at=NOW()
        """, (uid, fields.get("calories"), fields.get("protein_g"),
              fields.get("carbs_g"), fields.get("fat_g"), fields.get("fiber_g")))
        db.commit()
    row = _fetchone(db, "SELECT * FROM goals WHERE user_id=%s", (uid,))
    return jsonify(dict(row) if row else {})

# ---------------------------------------------------------------------------
# Weight API
# ---------------------------------------------------------------------------

@app.route("/api/weight", methods=["GET", "POST"])
@auth_required
def weight_endpoint():
    uid = current_user_id()
    db  = get_db()
    if request.method == "POST":
        data       = request.get_json(force=True)
        weight_kg  = data.get("weight_kg")
        weighed_at = data.get("weighed_at", datetime.now().isoformat())
        if weight_kg is None:
            return jsonify({"error": "weight_kg required"}), 400
        new_id = _insert(db,
            "INSERT INTO weights (user_id, weighed_at, weight_kg) VALUES (%s, %s, %s) RETURNING id",
            (uid, weighed_at, weight_kg)
        )
        db.commit()
        return jsonify({"id": new_id, "weight_kg": weight_kg}), 201
    days = int(request.args.get("days", 30))
    rows = _fetchall(db, """
        SELECT id, weighed_at::date AS day, weight_kg
        FROM weights WHERE user_id=%s
        ORDER BY weighed_at DESC LIMIT %s
    """, (uid, days))
    return jsonify([{**dict(r), "day": str(r["day"])} for r in rows])


@app.route("/api/weight/<int:entry_id>", methods=["DELETE"])
@auth_required
def delete_weight(entry_id):
    uid = current_user_id()
    db  = get_db()
    _exec(db, "DELETE FROM weights WHERE id=%s AND user_id=%s", (entry_id, uid))
    db.commit()
    return jsonify({"deleted": entry_id})

# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------

@app.route("/api/export/meals.csv")
@auth_required
def export_meals_csv():
    uid  = current_user_id()
    db   = get_db()
    rows = _fetchall(db,
        "SELECT eaten_at, description, calories, protein_g, carbs_g, fat_g, fiber_g FROM meals WHERE user_id=%s ORDER BY eaten_at",
        (uid,)
    )
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["Päivämäärä", "Kellonaika", "Kuvaus", "Kalorit (kcal)", "Proteiini (g)", "Hiilihydraatit (g)", "Rasva (g)", "Kuitu (g)"])
    for r in rows:
        try:
            parsed = datetime.fromisoformat(r["eaten_at"])
            day, time = parsed.strftime("%d.%m.%Y"), parsed.strftime("%H:%M")
        except Exception:
            day, time = r["eaten_at"], ""
        w.writerow([day, time, r["description"], r["calories"], r["protein_g"], r["carbs_g"], r["fat_g"], r["fiber_g"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=ruokapaivakirja.csv"})


@app.route("/api/import/meals", methods=["POST"])
@auth_required
def import_meals_csv():
    uid = current_user_id()
    if "file" not in request.files:
        return jsonify({"error": "Tiedosto puuttuu"}), 400
    f = request.files["file"]
    try:
        content = f.read().decode("utf-8-sig")
        reader  = csv.DictReader(io.StringIO(content))
        db = get_db()
        imported = skipped = 0
        for row in reader:
            try:
                day  = row.get("Päivämäärä", "").strip()
                time = row.get("Kellonaika", "00:00").strip() or "00:00"
                desc = row.get("Kuvaus", "").strip()
                if not day or not desc:
                    skipped += 1; continue
                d, m, y = day.split(".")
                eaten_at = f"{y}-{m.zfill(2)}-{d.zfill(2)}T{time}:00"
                def flt(k):
                    v = row.get(k, "").strip()
                    return float(v) if v else None
                _exec(db,
                    "INSERT INTO meals (user_id, eaten_at, description, calories, protein_g, carbs_g, fat_g, fiber_g) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (uid, eaten_at, desc, flt("Kalorit (kcal)"), flt("Proteiini (g)"), flt("Hiilihydraatit (g)"), flt("Rasva (g)"), flt("Kuitu (g)"))
                )
                imported += 1
            except Exception:
                skipped += 1
        db.commit()
        return jsonify({"imported": imported, "skipped": skipped})
    except Exception as e:
        return jsonify({"error": f"Tuonti epäonnistui: {e}"}), 500

# ---------------------------------------------------------------------------
# Barcode API  (Open Food Facts)
# ---------------------------------------------------------------------------

OFF_URL     = "https://world.openfoodfacts.org/api/v0/product/{}.json"
OFF_HEADERS = {"User-Agent": "Ruokapaivakirja/1.0 (marko.sorvamaa@qtec.fi)"}

def _parse_serving(s):
    m = re.search(r'(\d+[\.,]?\d*)\s*g', str(s), re.IGNORECASE)
    return float(m.group(1).replace(',', '.')) if m else None

@app.route("/api/barcode/<barcode>")
@auth_required
def lookup_barcode(barcode):
    try:
        r    = http_requests.get(OFF_URL.format(barcode), headers=OFF_HEADERS, timeout=8)
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Tietokantayhteys epäonnistui: {e}"}), 502
    if data.get("status") != 1:
        return jsonify({"found": False, "error": "Tuotetta ei löydy tietokannasta"}), 404
    p = data["product"]
    n = p.get("nutriments", {})
    def per100(key):
        for sfx in ("_100g", "_serving"):
            v = n.get(key + sfx)
            if v is not None: return round(float(v), 1)
        return None
    name      = (p.get("product_name_fi") or p.get("product_name") or "Tuntematon tuote").strip()
    brand     = (p.get("brands") or "").strip()
    serving_g = _parse_serving(p.get("serving_size", "")) or 100
    return jsonify({"found": True, "barcode": barcode, "name": name, "brand": brand,
                    "serving_g": serving_g,
                    "per_100g": {"calories": per100("energy-kcal"), "protein_g": per100("proteins"),
                                 "carbs_g": per100("carbohydrates"), "fat_g": per100("fat"),
                                 "fiber_g": per100("fiber")}})

# ---------------------------------------------------------------------------
# Water API
# ---------------------------------------------------------------------------

@app.route("/api/water", methods=["GET", "POST"])
@auth_required
def water_endpoint():
    uid = current_user_id()
    db  = get_db()
    if request.method == "POST":
        data      = request.get_json(force=True)
        amount_ml = data.get("amount_ml")
        logged_at = data.get("logged_at", datetime.now().isoformat())
        if not amount_ml:
            return jsonify({"error": "amount_ml required"}), 400
        new_id = _insert(db,
            "INSERT INTO water (user_id, logged_at, amount_ml) VALUES (%s, %s, %s) RETURNING id",
            (uid, logged_at, amount_ml)
        )
        db.commit()
        return jsonify({"id": new_id, "amount_ml": amount_ml}), 201
    day  = request.args.get("date", date.today().isoformat())
    rows = _fetchall(db,
        "SELECT id, logged_at, amount_ml FROM water WHERE user_id=%s AND logged_at::date=%s::date ORDER BY logged_at",
        (uid, day)
    )
    total = sum(r["amount_ml"] for r in rows)
    goal_row = _fetchone(db, "SELECT water_ml FROM goals WHERE user_id=%s", (uid,))
    goal_ml  = goal_row["water_ml"] if goal_row and goal_row["water_ml"] else 2000
    return jsonify({"entries": [dict(r) for r in rows], "total_ml": total, "goal_ml": goal_ml})


@app.route("/api/water/<int:entry_id>", methods=["DELETE"])
@auth_required
def delete_water_entry(entry_id):
    uid = current_user_id()
    db  = get_db()
    _exec(db, "DELETE FROM water WHERE id=%s AND user_id=%s", (entry_id, uid))
    db.commit()
    return jsonify({"deleted": entry_id})

# ---------------------------------------------------------------------------
# Favorites API
# ---------------------------------------------------------------------------

@app.route("/api/favorites", methods=["GET"])
@auth_required
def list_favorites():
    uid  = current_user_id()
    db   = get_db()
    rows = _fetchall(db,
        "SELECT id, name, description, calories, protein_g, carbs_g, fat_g, fiber_g, use_count FROM favorites WHERE user_id=%s ORDER BY use_count DESC, created_at DESC",
        (uid,)
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/favorites", methods=["POST"])
@auth_required
def save_favorite():
    uid  = current_user_id()
    data = request.get_json(force=True)
    name = (data.get("name") or data.get("description") or "Suosikki")[:80]
    db   = get_db()
    new_id = _insert(db, """
        INSERT INTO favorites (user_id, name, description, calories, protein_g, carbs_g, fat_g, fiber_g, image_b64, analysis)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (uid, name, data.get("description"), data.get("calories"), data.get("protein_g"),
          data.get("carbs_g"), data.get("fat_g"), data.get("fiber_g"),
          data.get("image_b64"), json.dumps(data.get("analysis", {}), ensure_ascii=False)))
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/favorites/<int:fav_id>", methods=["DELETE"])
@auth_required
def delete_favorite(fav_id):
    uid = current_user_id()
    db  = get_db()
    _exec(db, "DELETE FROM favorites WHERE id=%s AND user_id=%s", (fav_id, uid))
    db.commit()
    return jsonify({"deleted": fav_id})


@app.route("/api/favorites/<int:fav_id>/use", methods=["POST"])
@auth_required
def use_favorite(fav_id):
    uid = current_user_id()
    db  = get_db()
    fav = _fetchone(db, "SELECT * FROM favorites WHERE id=%s AND user_id=%s", (fav_id, uid))
    if not fav:
        return jsonify({"error": "Not found"}), 404
    try:
        analysis = json.loads(fav["analysis"]) if fav["analysis"] else {}
    except Exception:
        analysis = {}
    analysis.update({
        "description": fav["description"] or fav["name"],
        "calories":    fav["calories"],
        "protein_g":   fav["protein_g"],
        "carbs_g":     fav["carbs_g"],
        "fat_g":       fav["fat_g"],
        "fiber_g":     fav["fiber_g"],
    })
    data     = request.get_json(force=True) or {}
    eaten_at = data.get("eaten_at", datetime.now().isoformat())
    new_id   = _insert(db, """
        INSERT INTO meals (user_id, eaten_at, description, image_b64, calories, protein_g, carbs_g, fat_g, fiber_g, analysis)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (uid, eaten_at, fav["description"] or fav["name"], fav["image_b64"],
          fav["calories"], fav["protein_g"], fav["carbs_g"], fav["fat_g"], fav["fiber_g"],
          json.dumps(analysis, ensure_ascii=False)))
    _exec(db, "UPDATE favorites SET use_count=use_count+1, last_used=NOW() WHERE id=%s", (fav_id,))
    db.commit()
    return jsonify({"meal_id": new_id, "analysis": analysis}), 201

# ---------------------------------------------------------------------------
# Trends API
# ---------------------------------------------------------------------------

@app.route("/api/trends")
@auth_required
def trends():
    from datetime import timedelta
    uid    = current_user_id()
    period = request.args.get("period", "week")
    days   = 7 if period == "week" else 30
    db     = get_db()
    start  = (date.today() - timedelta(days=days - 1)).isoformat()
    rows   = _fetchall(db, """
        SELECT eaten_at::date AS day,
               ROUND(SUM(calories)::numeric, 0)  AS calories,
               ROUND(SUM(protein_g)::numeric, 1) AS protein_g,
               ROUND(SUM(carbs_g)::numeric, 1)   AS carbs_g,
               ROUND(SUM(fat_g)::numeric, 1)     AS fat_g,
               ROUND(SUM(fiber_g)::numeric, 1)   AS fiber_g
        FROM meals
        WHERE user_id=%s AND eaten_at::date >= %s::date
        GROUP BY eaten_at::date
        ORDER BY eaten_at::date
    """, (uid, start))
    return jsonify([{**dict(r), "day": str(r["day"])} for r in rows])

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.template_folder, "index.html")

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

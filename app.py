import os
import io
import csv
import json
import sqlite3
from datetime import date, datetime
from flask import Flask, request, jsonify, send_from_directory, g, Response
import anthropic

app = Flask(__name__, static_folder="static", template_folder="templates")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "meals.db")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                eaten_at    TEXT    NOT NULL,
                description TEXT    NOT NULL,
                image_b64   TEXT,
                calories    REAL,
                protein_g   REAL,
                carbs_g     REAL,
                fat_g       REAL,
                fiber_g     REAL,
                analysis    TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS weights (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                weighed_at TEXT    NOT NULL,
                weight_kg  REAL    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                calories   REAL,
                protein_g  REAL,
                carbs_g    REAL,
                fat_g      REAL,
                fiber_g    REAL,
                updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Insert default goals row if not exists
        db.execute("""
            INSERT OR IGNORE INTO goals (id, calories, protein_g, carbs_g, fat_g, fiber_g)
            VALUES (1, 2000, 120, 220, 65, 30)
        """)
        db.commit()

# ---------------------------------------------------------------------------
# AI food analysis
# ---------------------------------------------------------------------------

ITEMS_SCHEMA = """
{
  "description": "Lyhyt kuvaus annoksesta suomeksi",
  "items": [
    {"name": "Ruoka-aineen nimi suomeksi", "weight_g": <paino grammoina, numero>}
  ],
  "calories": <kokonaiskalorit kcal, numero>,
  "protein_g": <proteiini g, numero>,
  "carbs_g": <hiilihydraatit g, numero>,
  "fat_g": <rasva g, numero>,
  "fiber_g": <kuitu g, numero>,
  "notes": "Mahdolliset huomiot tai epävarmuudet"
}"""

VISION_PROMPT = f"""Olet ravitsemustieteilijä-AI, joka analysoi ruoka-annoksia kuvista.
Tunnista kaikki ruoka-aineet kuvasta ja arvioi niiden painot grammoina.
Palauta JSON seuraavassa muodossa:{ITEMS_SCHEMA}
Palauta VAIN JSON, ei muuta tekstiä."""

TEXT_PROMPT = f"""Olet ravitsemustieteilijä-AI, joka arvioi ravintoarvoja tekstikuvauksen perusteella.
Tunnista kaikki mainitut ruoka-aineet ja arvioi niiden painot grammoina.
Palauta JSON seuraavassa muodossa:{ITEMS_SCHEMA}
Palauta VAIN JSON, ei muuta tekstiä."""

RECALC_PROMPT = f"""Olet ravitsemustieteilijä-AI. Sinulle annetaan lista ruoka-aineista ja niiden painot grammoina.
Laske yhteenlasketut ravintoarvot ja palauta JSON seuraavassa muodossa:{ITEMS_SCHEMA}
Säilytä annettu items-lista sellaisenaan, laske vain ravintoarvosummat.
Palauta VAIN JSON, ei muuta tekstiä."""

def parse_ai_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

def analyze_food_image(image_b64: str, media_type: str = "image/jpeg") -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=VISION_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": "Analysoi tämä ruoka-annos ja palauta ravintoarvot JSON-muodossa."}
            ],
        }],
    )
    return parse_ai_json(message.content[0].text)

def recalculate_from_items(items: list) -> dict:
    """Re-calculate nutrition totals from an edited list of {name, weight_g} items."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    items_text = "\n".join(f"- {it['name']}: {it['weight_g']} g" for it in items)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=RECALC_PROMPT,
        messages=[{"role": "user", "content": f"Ruoka-aineet:\n{items_text}"}],
    )
    result = parse_ai_json(message.content[0].text)
    # Always keep the user's items list, not Claude's re-interpretation
    result["items"] = items
    return result

def analyze_food_text(description: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=TEXT_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Arvioi ravintoarvot: {description}",
        }],
    )
    return parse_ai_json(message.content[0].text)

# ---------------------------------------------------------------------------
# Meals API
# ---------------------------------------------------------------------------

@app.route("/api/meals", methods=["POST"])
def add_meal():
    data = request.get_json(force=True)
    image_b64  = data.get("image_b64")
    media_type = data.get("media_type", "image/jpeg")
    text_desc  = data.get("text_description")
    eaten_at   = data.get("eaten_at", datetime.now().isoformat())

    # Barcode path: pre-computed values, no AI needed
    precomputed = data.get("_precomputed")
    if precomputed:
        analysis = precomputed
    else:
        if not ANTHROPIC_API_KEY:
            return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500
        if not image_b64 and not text_desc:
            return jsonify({"error": "image_b64 or text_description required"}), 400
        try:
            if image_b64:
                analysis = analyze_food_image(image_b64, media_type)
            else:
                analysis = analyze_food_text(text_desc)
        except Exception as e:
            return jsonify({"error": f"AI analysis failed: {str(e)}"}), 500

    db = get_db()
    cur = db.execute(
        """INSERT INTO meals
           (eaten_at, description, image_b64, calories, protein_g, carbs_g, fat_g, fiber_g, analysis)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (eaten_at, analysis.get("description", ""), image_b64,
         analysis.get("calories"), analysis.get("protein_g"),
         analysis.get("carbs_g"), analysis.get("fat_g"), analysis.get("fiber_g"),
         json.dumps(analysis, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "analysis": analysis}), 201


@app.route("/api/meals", methods=["GET"])
def list_meals():
    day = request.args.get("date", date.today().isoformat())
    db = get_db()
    rows = db.execute(
        "SELECT * FROM meals WHERE date(eaten_at) = ? ORDER BY eaten_at DESC", (day,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/meals/<int:meal_id>/recalculate", methods=["POST"])
def recalculate_meal(meal_id):
    """Recalculate nutrition from an edited food items list."""
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items list required"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500
    try:
        analysis = recalculate_from_items(items)
    except Exception as e:
        return jsonify({"error": f"Recalculation failed: {str(e)}"}), 500

    db = get_db()
    db.execute(
        """UPDATE meals SET description=?, calories=?, protein_g=?, carbs_g=?,
           fat_g=?, fiber_g=?, analysis=? WHERE id=?""",
        (analysis.get("description", ""), analysis.get("calories"),
         analysis.get("protein_g"), analysis.get("carbs_g"),
         analysis.get("fat_g"), analysis.get("fiber_g"),
         json.dumps(analysis, ensure_ascii=False), meal_id)
    )
    db.commit()
    row = db.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
    return jsonify({"meal": dict(row), "analysis": analysis})


@app.route("/api/meals/<int:meal_id>", methods=["PATCH"])
def update_meal(meal_id):
    """Update nutritional values or description of a meal."""
    data = request.get_json(force=True)
    allowed = ["description", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g"]
    fields = {k: data[k] for k in allowed if k in data}
    if not fields:
        return jsonify({"error": "No valid fields to update"}), 400
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db = get_db()
    db.execute(f"UPDATE meals SET {set_clause} WHERE id = ?", (*fields.values(), meal_id))
    db.commit()
    row = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/meals/<int:meal_id>", methods=["DELETE"])
def delete_meal(meal_id):
    db = get_db()
    db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    db.commit()
    return jsonify({"deleted": meal_id})


@app.route("/api/summary", methods=["GET"])
def daily_summary():
    day = request.args.get("date", date.today().isoformat())
    db = get_db()
    row = db.execute(
        """SELECT COUNT(*) AS meal_count,
               ROUND(SUM(calories),1) AS calories,
               ROUND(SUM(protein_g),1) AS protein_g,
               ROUND(SUM(carbs_g),1) AS carbs_g,
               ROUND(SUM(fat_g),1) AS fat_g,
               ROUND(SUM(fiber_g),1) AS fiber_g
           FROM meals WHERE date(eaten_at) = ?""", (day,)
    ).fetchone()
    return jsonify({"date": day, **dict(row)})


@app.route("/api/history", methods=["GET"])
def history():
    db = get_db()
    rows = db.execute(
        """SELECT date(eaten_at) AS day, COUNT(*) AS meal_count,
               ROUND(SUM(calories),0) AS calories
           FROM meals GROUP BY day ORDER BY day DESC LIMIT 30"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------------------------------------------------------------------------
# Goals API
# ---------------------------------------------------------------------------

@app.route("/api/goals", methods=["GET", "POST"])
def goals_endpoint():
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        allowed = ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g"]
        fields = {k: data[k] for k in allowed if k in data}
        db.execute(
            """INSERT INTO goals (id, calories, protein_g, carbs_g, fat_g, fiber_g, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                   calories=excluded.calories, protein_g=excluded.protein_g,
                   carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,
                   fiber_g=excluded.fiber_g, updated_at=excluded.updated_at""",
            (fields.get("calories"), fields.get("protein_g"), fields.get("carbs_g"),
             fields.get("fat_g"), fields.get("fiber_g"))
        )
        db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    return jsonify(dict(row))

# ---------------------------------------------------------------------------
# Weight API
# ---------------------------------------------------------------------------

@app.route("/api/weight", methods=["GET", "POST"])
def weight_endpoint():
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        weight_kg  = data.get("weight_kg")
        weighed_at = data.get("weighed_at", datetime.now().isoformat())
        if weight_kg is None:
            return jsonify({"error": "weight_kg required"}), 400
        cur = db.execute(
            "INSERT INTO weights (weighed_at, weight_kg) VALUES (?, ?)", (weighed_at, weight_kg)
        )
        db.commit()
        return jsonify({"id": cur.lastrowid, "weight_kg": weight_kg, "weighed_at": weighed_at}), 201
    days = int(request.args.get("days", 30))
    rows = db.execute(
        "SELECT id, date(weighed_at) AS day, weight_kg FROM weights ORDER BY weighed_at DESC LIMIT ?", (days,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/weight/<int:entry_id>", methods=["DELETE"])
def delete_weight(entry_id):
    db = get_db()
    db.execute("DELETE FROM weights WHERE id = ?", (entry_id,))
    db.commit()
    return jsonify({"deleted": entry_id})

# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@app.route("/api/import/meals", methods=["POST"])
def import_meals_csv():
    if "file" not in request.files:
        return jsonify({"error": "Tiedosto puuttuu"}), 400
    f = request.files["file"]
    try:
        content = f.read().decode("utf-8-sig")  # strips BOM if present
        reader = csv.DictReader(io.StringIO(content))
        db = get_db()
        imported = 0
        skipped = 0
        for row in reader:
            try:
                day  = row.get("Päivämäärä", "").strip()
                time = row.get("Kellonaika", "00:00").strip() or "00:00"
                desc = row.get("Kuvaus", "").strip()
                if not day or not desc:
                    skipped += 1
                    continue
                # Parse "DD.MM.YYYY" → "YYYY-MM-DD"
                d, m, y = day.split(".")
                eaten_at = f"{y}-{m.zfill(2)}-{d.zfill(2)}T{time}:00"

                def flt(key):
                    v = row.get(key, "").strip()
                    return float(v) if v else None

                db.execute(
                    """INSERT INTO meals (eaten_at, description, calories, protein_g, carbs_g, fat_g, fiber_g)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (eaten_at, desc, flt("Kalorit (kcal)"), flt("Proteiini (g)"),
                     flt("Hiilihydraatit (g)"), flt("Rasva (g)"), flt("Kuitu (g)"))
                )
                imported += 1
            except Exception:
                skipped += 1
        db.commit()
        return jsonify({"imported": imported, "skipped": skipped})
    except Exception as e:
        return jsonify({"error": f"Tuonti epäonnistui: {e}"}), 500

@app.route("/api/export/meals.csv")
def export_meals_csv():
    db = get_db()
    rows = db.execute(
        "SELECT eaten_at, description, calories, protein_g, carbs_g, fat_g, fiber_g FROM meals ORDER BY eaten_at"
    ).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Päivämäärä", "Kellonaika", "Kuvaus", "Kalorit (kcal)", "Proteiini (g)", "Hiilihydraatit (g)", "Rasva (g)", "Kuitu (g)"])
    for r in rows:
        dt = r["eaten_at"]
        try:
            parsed = datetime.fromisoformat(dt)
            day = parsed.strftime("%d.%m.%Y")
            time = parsed.strftime("%H:%M")
        except Exception:
            day, time = dt, ""
        w.writerow([day, time, r["description"], r["calories"], r["protein_g"], r["carbs_g"], r["fat_g"], r["fiber_g"]])

    output = buf.getvalue()
    return Response(
        "﻿" + output,  # BOM for Excel UTF-8 compatibility
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ruokapaivakirja.csv"}
    )

# ---------------------------------------------------------------------------
# Barcode API  (Open Food Facts)
# ---------------------------------------------------------------------------

import requests as http_requests

OFF_URL = "https://world.openfoodfacts.org/api/v0/product/{}.json"
OFF_HEADERS = {"User-Agent": "Ruokapaivakirja/1.0 (marko.sorvamaa@qtec.fi)"}

def _parse_serving(s: str) -> float | None:
    """Extract grams from serving size string like '200g' or '1 portion (250g)'."""
    import re
    m = re.search(r'(\d+[\.,]?\d*)\s*g', str(s), re.IGNORECASE)
    return float(m.group(1).replace(',', '.')) if m else None

@app.route("/api/barcode/<barcode>")
def lookup_barcode(barcode):
    try:
        r = http_requests.get(OFF_URL.format(barcode), headers=OFF_HEADERS, timeout=8)
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Tietokantayhteys epäonnistui: {e}"}), 502

    if data.get("status") != 1:
        return jsonify({"found": False, "error": "Tuotetta ei löydy tietokannasta"}), 404

    p = data["product"]
    n = p.get("nutriments", {})

    def per100(key):
        for suffix in ("_100g", "_serving"):
            v = n.get(key + suffix)
            if v is not None:
                return round(float(v), 1)
        return None

    # Prefer Finnish name, fall back to generic
    name = (p.get("product_name_fi") or p.get("product_name") or "Tuntematon tuote").strip()
    brand = (p.get("brands") or "").strip()
    serving_g = _parse_serving(p.get("serving_size", "")) or 100

    return jsonify({
        "found": True,
        "barcode": barcode,
        "name": name,
        "brand": brand,
        "serving_g": serving_g,
        "per_100g": {
            "calories":  per100("energy-kcal"),
            "protein_g": per100("proteins"),
            "carbs_g":   per100("carbohydrates"),
            "fat_g":     per100("fat"),
            "fiber_g":   per100("fiber"),
        },
    })

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

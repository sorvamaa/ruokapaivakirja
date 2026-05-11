import os
import json
import base64
import sqlite3
from datetime import date, datetime
from flask import Flask, request, jsonify, send_from_directory, g
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
        db.commit()

# ---------------------------------------------------------------------------
# AI food analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Olet ravitsemustieteilijä-AI, joka analysoi ruoka-annoksia kuvista.
Analysoi kuva ja palauta JSON-objekti seuraavilla kentillä:
{
  "description": "Lyhyt kuvaus annoksesta suomeksi (esim. 'Broileria ja riisiä salaatilla')",
  "foods": ["lista", "tunnistetuista", "ruoista"],
  "calories": <kokonaiskalorimäärä kcal, numero>,
  "protein_g": <proteiini grammoina, numero>,
  "carbs_g": <hiilihydraatit grammoina, numero>,
  "fat_g": <rasva grammoina, numero>,
  "fiber_g": <kuitu grammoina, numero>,
  "notes": "Mahdolliset huomiot tai epävarmuudet arviossa"
}
Palauta VAIN JSON, ei muuta tekstiä."""

def analyze_food_image(image_b64: str, media_type: str = "image/jpeg") -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analysoi tämä ruoka-annos ja palauta ravintoarvot JSON-muodossa."
                    }
                ],
            }
        ],
        system=SYSTEM_PROMPT,
    )
    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/meals", methods=["POST"])
def add_meal():
    """Add a new meal with an optional image."""
    data = request.get_json(force=True)
    image_b64 = data.get("image_b64")
    media_type = data.get("media_type", "image/jpeg")
    eaten_at = data.get("eaten_at", datetime.now().isoformat())

    if not image_b64:
        return jsonify({"error": "image_b64 required"}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    try:
        analysis = analyze_food_image(image_b64, media_type)
    except Exception as e:
        return jsonify({"error": f"AI analysis failed: {str(e)}"}), 500

    db = get_db()
    cur = db.execute(
        """INSERT INTO meals
           (eaten_at, description, image_b64, calories, protein_g, carbs_g, fat_g, fiber_g, analysis)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            eaten_at,
            analysis.get("description", ""),
            image_b64,
            analysis.get("calories"),
            analysis.get("protein_g"),
            analysis.get("carbs_g"),
            analysis.get("fat_g"),
            analysis.get("fiber_g"),
            json.dumps(analysis, ensure_ascii=False),
        ),
    )
    db.commit()

    return jsonify({"id": cur.lastrowid, "analysis": analysis}), 201


@app.route("/api/meals", methods=["GET"])
def list_meals():
    """List meals, optionally filtered by date (YYYY-MM-DD)."""
    day = request.args.get("date", date.today().isoformat())
    db = get_db()
    rows = db.execute(
        "SELECT * FROM meals WHERE date(eaten_at) = ? ORDER BY eaten_at DESC",
        (day,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/meals/<int:meal_id>", methods=["DELETE"])
def delete_meal(meal_id):
    db = get_db()
    db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    db.commit()
    return jsonify({"deleted": meal_id})


@app.route("/api/summary", methods=["GET"])
def daily_summary():
    """Return aggregated nutritional totals for a given date."""
    day = request.args.get("date", date.today().isoformat())
    db = get_db()
    row = db.execute(
        """SELECT
               COUNT(*)        AS meal_count,
               ROUND(SUM(calories),1)  AS calories,
               ROUND(SUM(protein_g),1) AS protein_g,
               ROUND(SUM(carbs_g),1)   AS carbs_g,
               ROUND(SUM(fat_g),1)     AS fat_g,
               ROUND(SUM(fiber_g),1)   AS fiber_g
           FROM meals WHERE date(eaten_at) = ?""",
        (day,),
    ).fetchone()
    return jsonify({"date": day, **dict(row)})


@app.route("/api/history", methods=["GET"])
def history():
    """Return dates that have meals, with daily totals — last 30 days."""
    db = get_db()
    rows = db.execute(
        """SELECT date(eaten_at) AS day,
                  COUNT(*)              AS meal_count,
                  ROUND(SUM(calories),0) AS calories
           FROM meals
           GROUP BY day
           ORDER BY day DESC
           LIMIT 30"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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

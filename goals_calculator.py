"""
BiteLog — Tavoitelaskuri
========================
Laskentamoduuli kalori- ja makrotavoitteiden määrittämiseksi.

Kaavat:
  BMR  : Mifflin-St Jeor (1990) — tarkin yleiskäyttöinen kaava
  TDEE : PAL-kerroin × BMR, korjattuna treenimäärällä
  Makrot: proteiini painon mukaan, rasva minimillä, hiilihydraatit lopusta
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Vakiot ja taulukot
# ---------------------------------------------------------------------------

# Aktiviteettitasot (PAL = Physical Activity Level)
ACTIVITY_LEVELS = {
    "sedentary":     {"label": "Istumatyö / hyvin vähän liikuntaa", "pal": 1.2},
    "light":         {"label": "Kevyt arki (kevyt kävelytyö, satunnainen liikkuminen)", "pal": 1.375},
    "moderate":      {"label": "Kohtalainen arki (seisomapalvelu, vanhemmuus)", "pal": 1.55},
    "active":        {"label": "Aktiivinen arki (fyysinen työ, rakennusala)", "pal": 1.725},
    "very_active":   {"label": "Erittäin aktiivinen arki (raskas fyysinen työ)", "pal": 1.9},
}

# Viikoittaisten treenien lisäkerroin BMR:ään (lisätään TDEE:hen)
# Perustuu MET-arvoihin ja treenin kestoon
WORKOUT_EXTRA_KCAL = {
    # (treeniä/vko, kesto) → kcal/pv lisäys
    (0, "any"):        0,
    (2, "short"):     60,   # 1-2 treeniä, alle 30 min
    (2, "medium"):   100,   # 1-2 treeniä, 30-60 min
    (2, "long"):     140,   # 1-2 treeniä, 60-90 min
    (2, "very_long"):180,   # 1-2 treeniä, 90+ min
    (4, "short"):    100,
    (4, "medium"):   160,
    (4, "long"):     220,
    (4, "very_long"):280,
    (6, "short"):    140,
    (6, "medium"):   210,
    (6, "long"):     300,
    (6, "very_long"):380,
    (7, "short"):    160,
    (7, "medium"):   240,
    (7, "long"):     340,
    (7, "very_long"):430,
}

# Tavoitetyypit: kalorimuutos TDEE:stä
GOAL_TYPES = {
    "cut_slow":    {"label": "Laihdutus — rauhallinen (-250 kcal/pv)",  "delta": -250, "kg_per_week": 0.25},
    "cut_normal":  {"label": "Laihdutus — normaali (-500 kcal/pv)",     "delta": -500, "kg_per_week": 0.5},
    "cut_fast":    {"label": "Laihdutus — nopea (-750 kcal/pv)",        "delta": -750, "kg_per_week": 0.75},
    "maintain":    {"label": "Painon ylläpito",                          "delta":    0, "kg_per_week": 0},
    "bulk_lean":   {"label": "Lihasmassan kasvatus — vähän (+150 kcal)", "delta":  150, "kg_per_week": -0.15},
    "bulk_normal": {"label": "Lihasmassan kasvatus — perus (+300 kcal)", "delta":  300, "kg_per_week": -0.3},
    "bulk_fast":   {"label": "Lihasmassan kasvatus — nopea (+500 kcal)", "delta":  500, "kg_per_week": -0.5},
}

# Turvalliset minimikalorit
MIN_KCAL = {"male": 1500, "female": 1200}

# Proteiinisuositukset (g/kg) tavoitteen mukaan
PROTEIN_G_PER_KG = {
    "cut_slow":    2.2,
    "cut_normal":  2.2,
    "cut_fast":    2.4,
    "maintain":    1.8,
    "bulk_lean":   2.2,
    "bulk_normal": 2.0,
    "bulk_fast":   2.0,
}

# Rasvan minimi (g/kg)
FAT_MIN_G_PER_KG = 0.8
FAT_MIN_PCT      = 0.20   # vähintään 20 % kokonaiskaloreista


# ---------------------------------------------------------------------------
# Dataclass tuloksille
# ---------------------------------------------------------------------------

@dataclass
class BMRResult:
    bmr: float
    formula_str: str       # kaava tekstinä
    calculation_str: str   # laskutoimitus arvoilla

@dataclass
class TDEEResult:
    tdee: float
    pal: float
    workout_extra: float
    formula_str: str
    calculation_str: str

@dataclass
class MacroResult:
    protein_g: float
    fat_g: float
    carbs_g: float
    protein_kcal: float
    fat_kcal: float
    carbs_kcal: float
    protein_pct: float
    fat_pct: float
    carbs_pct: float

@dataclass
class GoalResult:
    # Peruslaskelmat
    bmr: BMRResult
    tdee: TDEEResult
    # Tavoite
    goal_type: str
    goal_label: str
    calorie_target: float
    calorie_delta: int
    macros: MacroResult
    # Varoitukset
    warnings: list
    # Aikataulu
    estimated_weeks: Optional[float]
    estimated_date: Optional[str]


# ---------------------------------------------------------------------------
# Laskentafunktiot
# ---------------------------------------------------------------------------

def calculate_bmr(gender: str, age: int, weight_kg: float, height_cm: float) -> BMRResult:
    """
    Mifflin-St Jeor BMR-kaava (1990).
    Miehet:  BMR = 10×paino + 6.25×pituus − 5×ikä + 5
    Naiset:  BMR = 10×paino + 6.25×pituus − 5×ikä − 161
    """
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if gender == "male":
        bmr = base + 5
        constant = "+5"
    else:
        bmr = base - 161
        constant = "−161"

    formula = (
        "BMR = (10 × paino) + (6,25 × pituus) − (5 × ikä) " + constant
    )
    calc = (
        f"BMR = (10 × {weight_kg}) + (6,25 × {height_cm}) − (5 × {age}) {constant} "
        f"= {round(bmr, 1)} kcal/pv"
    )
    return BMRResult(bmr=round(bmr, 1), formula_str=formula, calculation_str=calc)


def _workout_extra(weekly_workouts: int, duration_key: str) -> float:
    """Palauttaa treenien lisäkalorit per päivä."""
    # Pyöristetään lähimpään taulukon arvoon
    wk_buckets = [0, 2, 4, 6, 7]
    wk = min(wk_buckets, key=lambda x: abs(x - weekly_workouts))
    if wk == 0:
        return 0
    key = (wk, duration_key)
    return WORKOUT_EXTRA_KCAL.get(key, WORKOUT_EXTRA_KCAL.get((wk, "medium"), 0))


def calculate_tdee(
    bmr: float,
    activity_level: str,
    weekly_workouts: int,
    workout_duration: str,   # "short"|"medium"|"long"|"very_long"
    workout_type: str = "combo",
) -> TDEEResult:
    """
    TDEE = BMR × PAL + treenilisä (kcal/pv)
    PAL otetaan activity_levels-taulukosta, treenilisä workout_extra-taulukosta.
    """
    pal = ACTIVITY_LEVELS.get(activity_level, ACTIVITY_LEVELS["sedentary"])["pal"]
    extra = _workout_extra(weekly_workouts, workout_duration)
    tdee = round(bmr * pal + extra, 1)

    formula = "TDEE = BMR × aktiivisuuskerroin (PAL) + treenilisä"
    calc = (
        f"TDEE = {bmr} × {pal} + {extra} = {tdee} kcal/pv"
    )
    return TDEEResult(
        tdee=tdee, pal=pal, workout_extra=extra,
        formula_str=formula, calculation_str=calc,
    )


def calculate_macros(
    calorie_target: float,
    weight_kg: float,
    goal_type: str,
) -> MacroResult:
    """
    1. Proteiini: tavoitteen mukainen g/kg × paino
    2. Rasva: max(0.8 g/kg × paino, 20 % kaloreista)
    3. Hiilihydraatit: loppuosa kaloreista
    """
    protein_g = round(PROTEIN_G_PER_KG.get(goal_type, 2.0) * weight_kg)
    protein_kcal = protein_g * 4

    fat_by_weight = FAT_MIN_G_PER_KG * weight_kg
    fat_by_pct    = (calorie_target * FAT_MIN_PCT) / 9
    fat_g         = round(max(fat_by_weight, fat_by_pct))
    fat_kcal      = fat_g * 9

    remaining_kcal = calorie_target - protein_kcal - fat_kcal
    carbs_g        = round(max(remaining_kcal / 4, 0))
    carbs_kcal     = carbs_g * 4

    total_kcal = protein_kcal + fat_kcal + carbs_kcal

    def pct(part):
        return round(part / total_kcal * 100) if total_kcal else 0

    return MacroResult(
        protein_g=protein_g, fat_g=fat_g, carbs_g=carbs_g,
        protein_kcal=protein_kcal, fat_kcal=fat_kcal, carbs_kcal=carbs_kcal,
        protein_pct=pct(protein_kcal),
        fat_pct=pct(fat_kcal),
        carbs_pct=pct(carbs_kcal),
    )


def calculate_goals(
    gender: str,
    age: int,
    weight_kg: float,
    height_cm: float,
    activity_level: str,
    weekly_workouts: int,
    workout_duration: str,
    workout_type: str,
    goal_type: str,
    target_weight_kg: Optional[float] = None,
    target_date: Optional[str] = None,
) -> GoalResult:
    """
    Pääfunktio: laskee kaikki kerralla.
    Palauttaa GoalResult-dataluokan, jossa kaikki välitulokset ja selitykset.
    """
    warnings = []

    # 1. BMR
    bmr_result = calculate_bmr(gender, age, weight_kg, height_cm)

    # 2. TDEE
    tdee_result = calculate_tdee(
        bmr_result.bmr, activity_level,
        weekly_workouts, workout_duration, workout_type,
    )

    # 3. Kaloritavoite
    goal_info  = GOAL_TYPES.get(goal_type, GOAL_TYPES["maintain"])
    delta      = goal_info["delta"]
    raw_target = tdee_result.tdee + delta
    min_kcal   = MIN_KCAL.get(gender, 1500)

    if raw_target < min_kcal:
        warnings.append(
            f"Laskettu kalorimäärä ({round(raw_target)} kcal) alittaa turvarajan "
            f"({min_kcal} kcal). Tavoite nostettu minimiin."
        )
        calorie_target = float(min_kcal)
    else:
        calorie_target = round(raw_target)

    # 4. Makrot
    macros = calculate_macros(calorie_target, weight_kg, goal_type)

    # 5. Aikataulu
    estimated_weeks = None
    estimated_date  = None

    if target_weight_kg and goal_type != "maintain":
        weight_diff = weight_kg - target_weight_kg           # positiivinen = laihdutus
        kg_per_week = abs(goal_info["kg_per_week"])
        if kg_per_week > 0:
            estimated_weeks = round(abs(weight_diff) / kg_per_week, 1)
            target_day = date.today() + timedelta(weeks=estimated_weeks)
            estimated_date = target_day.strftime("%d.%m.%Y")
    elif target_date:
        try:
            td = date.fromisoformat(target_date)
            days_left = (td - date.today()).days
            estimated_weeks = round(days_left / 7, 1) if days_left > 0 else None
        except ValueError:
            pass

    return GoalResult(
        bmr=bmr_result,
        tdee=tdee_result,
        goal_type=goal_type,
        goal_label=goal_info["label"],
        calorie_target=calorie_target,
        calorie_delta=delta,
        macros=macros,
        warnings=warnings,
        estimated_weeks=estimated_weeks,
        estimated_date=estimated_date,
    )


def goal_result_to_dict(r: GoalResult) -> dict:
    """Muuntaa GoalResult JSON-kelpoiseksi dictiksi."""
    return {
        "bmr": {
            "value": r.bmr.bmr,
            "formula": r.bmr.formula_str,
            "calculation": r.bmr.calculation_str,
        },
        "tdee": {
            "value": r.tdee.tdee,
            "pal": r.tdee.pal,
            "workout_extra": r.tdee.workout_extra,
            "formula": r.tdee.formula_str,
            "calculation": r.tdee.calculation_str,
        },
        "goal_type":       r.goal_type,
        "goal_label":      r.goal_label,
        "calorie_target":  r.calorie_target,
        "calorie_delta":   r.calorie_delta,
        "macros": {
            "protein_g":   r.macros.protein_g,
            "fat_g":       r.macros.fat_g,
            "carbs_g":     r.macros.carbs_g,
            "protein_kcal": r.macros.protein_kcal,
            "fat_kcal":     r.macros.fat_kcal,
            "carbs_kcal":   r.macros.carbs_kcal,
            "protein_pct":  r.macros.protein_pct,
            "fat_pct":      r.macros.fat_pct,
            "carbs_pct":    r.macros.carbs_pct,
        },
        "warnings":         r.warnings,
        "estimated_weeks":  r.estimated_weeks,
        "estimated_date":   r.estimated_date,
    }


# ---------------------------------------------------------------------------
# Apufunktiot API:lle
# ---------------------------------------------------------------------------

def get_activity_levels() -> list:
    return [
        {"key": k, "label": v["label"], "pal": v["pal"]}
        for k, v in ACTIVITY_LEVELS.items()
    ]

def get_goal_types() -> list:
    return [
        {"key": k, "label": v["label"], "delta": v["delta"]}
        for k, v in GOAL_TYPES.items()
    ]


# ---------------------------------------------------------------------------
# Pika-testi komentoriviltä: python goals_calculator.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    # Esimerkkiprofiili: 35-v mies, 80 kg, 178 cm, istumatyö, 3 treeniä/vko
    r = calculate_goals(
        gender="male", age=35, weight_kg=80, height_cm=178,
        activity_level="sedentary",
        weekly_workouts=3, workout_duration="medium", workout_type="strength",
        goal_type="cut_normal",
        target_weight_kg=73,
    )
    print("=== BiteLog Tavoitelaskuri - testilaskenta ===\n")
    print(f"BMR:  {r.bmr.calculation_str}")
    print(f"TDEE: {r.tdee.calculation_str}")
    print(f"\nTavoite: {r.goal_label}")
    print(f"Kaloritavoite: {r.calorie_target} kcal/pv  (TDEE {r.tdee.tdee} {'+' if r.calorie_delta >= 0 else ''}{r.calorie_delta})")
    print(f"\nMakrot:")
    print(f"  Proteiini:     {r.macros.protein_g} g  ({r.macros.protein_pct}%)")
    print(f"  Hiilihydraatit:{r.macros.carbs_g} g  ({r.macros.carbs_pct}%)")
    print(f"  Rasva:         {r.macros.fat_g} g  ({r.macros.fat_pct}%)")
    if r.estimated_weeks:
        print(f"\nAikataulu: ~{r.estimated_weeks} viikkoa → {r.estimated_date}")
    if r.warnings:
        print(f"\nVaroitukset: {r.warnings}")

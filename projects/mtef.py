"""मध्यकालीन खर्च संरचना (MTEF) — तीन वर्षको चक्रीय योजना.

  * नयाँ MTEF: अघिल्लोको स्रोत सीमा र आयोजनाको वर्षगत रकम एक वर्ष सारेर सुरु; प्रतिबद्धता आफैँ थपिन्छ
  * प्रतिबद्धता (पहिलो दाबी): पहिलो वर्षमा क्रमागत भई आएका र बजेटमा परिसकेका आयोजना;
    सम्झौता बाँकी रकम पहिलो वर्षभन्दा बढी भए दोस्रो वर्षमा आफैँ
  * जाँच: वर्षगत जम्मा ≤ स्रोत सीमा (रोक्ने); आयोजनाको बाँकी लागत नपुग्ने / बढी, P1 हिस्सा, वडा सन्तुलन (चेतावनी)
  * स्वीकृतपछि लक; पहिलो वर्षका रकम “वार्षिक बजेटमा पठाउने” बाट Project बन्ने / अद्यावधिक हुने
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0")
DIMENSIONS = (("pillar", "रणनीतिक स्तम्भ"), ("sector", "क्षेत्र"), ("sdg", "SDG"), ("gender", "लैङ्गिक"), ("climate", "जलवायु"),
              ("ward", "वडा"), ("priority", "P1 / P2"))


def D(v):
    return Decimal(str(v)) if v not in (None, "") else ZERO


def next_name(name, k=1):
    t = str.maketrans("०१२३४५६७८९", "0123456789")
    try:
        a = int(name.translate(t).split("/")[0])
        a += k
        return f"{a}/{str(a + 1)[-2:]}"
    except Exception:
        return f"{name}+{k}"


def year_names(start_name):
    return [start_name, next_name(start_name, 1), next_name(start_name, 2)]


def _shift(prev_years, prev_vals, new_years):
    """अघिल्लो योजनाका वर्षगत मान नयाँ वर्षमा (नभए अन्तिम वर्षकै मान ceiling मा, लाइनमा शून्य)."""
    m = dict(zip(prev_years, prev_vals))
    return [m.get(y) for y in new_years]


def create_plan(start_fy, user=None):
    from .models import MTEFCeiling, MTEFLine, MTEFPlan, ResourceHead

    names = year_names(start_fy.name)
    prev = MTEFPlan.objects.filter(start_fy__name__lt=start_fy.name).order_by("-start_fy__name").first()
    with transaction.atomic():
        plan = MTEFPlan.objects.create(start_fy=start_fy, y2_name=names[1], y3_name=names[2], previous=prev, created_by=user)
        prev_ceil = {c.head_id: c for c in prev.ceilings.all()} if prev else {}
        for h in ResourceHead.objects.filter(is_active=True):
            c = prev_ceil.get(h.id)
            vals = [ZERO, ZERO, ZERO]
            if c:
                got = _shift(prev.years, [c.y1, c.y2, c.y3], names)
                last = c.y3
                vals = [v if v is not None else last for v in got]
            MTEFCeiling.objects.create(plan=plan, head=h, y1=vals[0], y2=vals[1], y3=vals[2],
                                       revised=c.y1 if c else ZERO, basis=c.basis if c else "")
        if prev:
            # चालु खर्च: अघिल्लो MTEF को वर्षहरू सारेर (नभएको वर्ष अन्तिम वर्षकै रकम)
            got = _shift(prev.years, [prev.recurrent_y1, prev.recurrent_y2, prev.recurrent_y3], names)
            rec = [v if v is not None else prev.recurrent_y3 for v in got]
            plan.recurrent_y1, plan.recurrent_y2, plan.recurrent_y3 = rec
            plan.save(update_fields=["recurrent_y1", "recurrent_y2", "recurrent_y3"])
            for ln in prev.lines.select_related("bank"):
                got = _shift(prev.years, [ln.y1, ln.y2, ln.y3], names)
                vals = [v or ZERO for v in got]
                if sum(vals) > 0 and not ln.bank.hold:
                    MTEFLine.objects.create(plan=plan, bank=ln.bank, kind="new", y1=vals[0], y2=vals[1], y3=vals[2],
                                            note="अघिल्लो MTEF बाट")
        refresh_commitments(plan)
    return plan


def refresh_commitments(plan):
    """पहिलो वर्षमा बजेटमा परिसकेका (क्रमागत सहित) बैंक आयोजना → प्रतिबद्धता. फर्काउँछ: थपिएको / अद्यावधिक गन्ती."""
    from . import bank as bk
    from .models import MTEFLine, Project

    if plan.status != "draft":
        return 0
    projects = list(Project.objects.filter(fiscalyear=plan.start_fy, bank_project__isnull=False).select_related("bank_project"))
    banks = [p.bank_project for p in projects]
    ex = bk.exec_map(banks)
    n = 0
    existing = {ln.bank_id: ln for ln in plan.lines.all()}
    for p in projects:
        alloc = D(p.budget) - D(p.spent_before)          # यस आ.व. को रकम
        info = ex.get(p.bank_project_id, {})
        contract, spent = D(info.get("contract")), D(info.get("spent"))
        after = contract - spent - alloc if contract else ZERO
        ln = existing.get(p.bank_project_id)
        if ln is None:
            ln = MTEFLine(plan=plan, bank=p.bank_project, kind="commitment")
        changed = ln.pk is None or ln.kind != "commitment" or ln.y1 != alloc
        ln.kind, ln.y1 = "commitment", alloc
        if after > 0 and ln.y2 < after:
            ln.y2, changed = after, True
            ln.note = "सम्झौता बाँकी"
        elif p.carried_from_id and not ln.note:
            ln.note = "क्रमागत"
        if changed:
            ln.save()
            n += 1
    return n


def phase(b, spent=ZERO):
    """बाँकी लागत अवधि (बढीमा ३ वर्ष) मा बराबर बाँड्ने — अन्तिम वर्षमा मिलान."""
    need = max(D(b.cost) - D(spent), ZERO)
    yrs = max(1, min(3, b.duration_years or 1))
    share = (need / yrs).quantize(Decimal("1"))
    vals = [share if i < yrs else ZERO for i in range(3)]
    vals[yrs - 1] = need - share * (yrs - 1)
    return vals


def add_bank(plan, b, spent=ZERO):
    from .models import MTEFLine

    y1, y2, y3 = phase(b, spent)
    return MTEFLine.objects.create(plan=plan, bank=b, kind="new", y1=y1, y2=y2, y3=y3)


def totals(plan):
    lines = list(plan.lines.all())
    ceil = list(plan.ceilings.select_related("head"))
    liabs = list(plan.liabilities.all())
    t = {}
    for i, k in enumerate(("y1", "y2", "y3")):
        resources = sum((D(getattr(c, k)) for c in ceil), ZERO)
        recurrent = D(getattr(plan, f"recurrent_{k}"))
        liab = sum((D(getattr(x, k)) for x in liabs), ZERO)          # समपूरक, भुक्तानी बाँकी, क्रमागत (बैंक बाहिर), अन्य
        cap = max(resources - recurrent - liab, ZERO) if resources else ZERO   # आयोजनाका लागि पुँजीगत सीमा = स्रोत − चालु − दायित्व
        com = sum((D(getattr(ln, k)) for ln in lines if ln.kind == "commitment"), ZERO)
        new = sum((D(getattr(ln, k)) for ln in lines if ln.kind == "new"), ZERO)
        used = com + new
        t[k] = {"cap": cap, "resources": resources, "recurrent": recurrent, "liab": liab, "commitment": com, "new": new, "used": used, "left": cap - used,
                "pct": int(used * 100 / cap) if cap else None, "name": plan.years[i]}
    return t


def checks(plan, ex=None):
    """(problems, warnings) — problems ले स्वीकृत गर्न रोक्छ."""
    from . import bank as bk

    lines = list(plan.lines.select_related("bank__ward", "bank__sector"))
    t = totals(plan)
    problems, warnings = [], []
    if not any(t[k]["cap"] for k in t):
        problems.append("स्रोत सीमा राखिएको छैन।")
    for k in ("y1", "y2", "y3"):
        if t[k]["cap"] and t[k]["used"] > t[k]["cap"]:
            problems.append(f"आ.व. {t[k]['name']}: जम्मा रु. {bk.money(t[k]['used'])} स्रोत सीमा रु. {bk.money(t[k]['cap'])} भन्दा "
                            f"रु. {bk.money(t[k]['used'] - t[k]['cap'])} बढी।")
    if not lines:
        problems.append("कुनै आयोजना छानिएको छैन।")
    ex = ex if ex is not None else bk.exec_map([ln.bank for ln in lines])
    for ln in lines:
        b = ln.bank
        if b.hold:
            problems.append(f"{b.code} {b.get_hold_display()} छ — हटाउनुहोस्।")
            continue
        if ln.kind == "new" and not b.ready_at:
            problems.append(f"{b.code} तयार घोषणा भएको छैन।")
        spent = D(ex.get(b.id, {}).get("spent"))
        need = max(D(ex.get(b.id, {}).get("contract")) or D(b.cost), D(b.cost)) - spent
        if ln.total + 1 < need and ln.kind == "new":
            warnings.append(f"{b.code}: तीन वर्षको जम्मा रु. {bk.money(ln.total)} — बाँकी लागत रु. {bk.money(need)} पुग्दैन (MTEF पछि पनि रकम चाहिन्छ)।")
        if need > 0 and ln.total > need * Decimal("1.1"):
            warnings.append(f"{b.code}: तीन वर्षको जम्मा बाँकी लागतभन्दा १०% भन्दा बढी।")
        if not b.coded:
            warnings.append(f"{b.code}: MTEF कोड अधुरो।")
    p1 = sum((ln.y1 for ln in lines if ln.bank.priority == "P1"), ZERO)
    y1 = t["y1"]["used"]
    if y1 and p1 * 100 / y1 < 60:
        warnings.append(f"पहिलो वर्षमा P1 आयोजनाको हिस्सा {bk.np(int(p1 * 100 / y1))}% मात्र।")
    for r in sector_rows(plan):             # विषयगत क्षेत्र सीमा नाघेको — चेतावनी
        if r["over"]:
            warnings.append(f"{r['sc'].sector.name}: पालिका स्तरीय छनौट रु. {bk.money(r['chosen'])}, क्षेत्र सीमा रु. {bk.money(r['sc'].amount)} भन्दा बढी।")
    from accounts.models import Ward

    wards_in = {ln.bank.ward_id for ln in lines if ln.bank.ward_id}
    missing = [w.name for w in Ward.objects.all() if w.id not in wards_in]
    if lines and missing and len(missing) <= 10:
        warnings.append("यी वडाबाट एउटा पनि आयोजना छैन: " + ", ".join(f"वडा {bk.np(m)}" for m in missing))
    return problems, warnings


def _dim_key(b, dim):
    if dim == "pillar":
        return (b.pillar.sort if b.pillar else 999, b.pillar.name if b.pillar else "")
    if dim == "sector":
        return (b.sector.sort if b.sector else 999, b.sector.name if b.sector else "")
    if dim == "sdg":
        return (b.sdg or 99, f"SDG {b.sdg}" if b.sdg else "")
    if dim == "gender":
        return ({"direct": 1, "indirect": 2, "neutral": 3}.get(b.gender, 9), b.get_gender_display() if b.gender else "")
    if dim == "climate":
        return ({"high": 1, "relevant": 2, "neutral": 3}.get(b.climate, 9), b.get_climate_display() if b.climate else "")
    if dim == "ward":
        try:
            n = int(str(b.ward.name).translate(str.maketrans("०१२३४५६७८९", "0123456789"))) if b.ward else 999
        except ValueError:
            n = 500
        return (n, f"वडा {b.ward.name}" if b.ward else "पालिका स्तर")
    return ({"P1": 1, "P2": 2}.get(b.priority, 9), b.priority or "")


def summary(lines, dim):
    groups = {}
    for ln in lines:
        k = _dim_key(ln.bank, dim)
        g = groups.setdefault(k, {"label": k[1] or "कोड नभएको", "n": 0, "y1": ZERO, "y2": ZERO, "y3": ZERO})
        g["n"] += 1
        for y in ("y1", "y2", "y3"):
            g[y] += D(getattr(ln, y))
    rows = [groups[k] for k in sorted(groups)]
    tot = {y: sum((r[y] for r in rows), ZERO) for y in ("y1", "y2", "y3")}
    for r in rows:
        r["pct"] = int(r["y1"] * 100 / tot["y1"]) if tot["y1"] else 0
    return rows


# ---------------------------------------------------------------- वार्षिक बजेटमा
def send_preview(plan):
    from .models import Project

    rows = []
    for ln in plan.lines.select_related("bank__ward", "bank__sector").order_by("bank__ward__name", "bank__name"):
        if ln.y1 <= 0:
            continue
        b = ln.bank
        p = Project.objects.filter(bank_project=b, fiscalyear=plan.start_fy).first() or \
            Project.objects.filter(bank_project=b).order_by("-id").first()
        if p and p.fiscalyear_id == plan.start_fy_id:
            new_budget = D(p.spent_before) + ln.y1
            action = "same" if abs(D(p.budget) - new_budget) < 1 else "update"
            rows.append({"ln": ln, "b": b, "p": p, "action": action, "old": D(p.budget), "new": new_budget})
        elif p and p.status != 2:
            rows.append({"ln": ln, "b": b, "p": p, "action": "other_fy", "old": D(p.budget), "new": ln.y1})
        else:
            rows.append({"ln": ln, "b": b, "p": None, "action": "create", "old": None, "new": ln.y1})
    return rows


def send(plan, budget_type=None, project_type=None, user=None):
    """पहिलो वर्षका रकम → Project (नयाँ बनाउने / बजेट अद्यावधिक). फर्काउँछ गन्ती."""
    from .budget import closed_fy_error
    from .models import Project

    c = {"create": 0, "update": 0, "same": 0, "other_fy": 0}
    if (err := closed_fy_error(fiscalyear_id=plan.start_fy_id)):
        raise ValueError(err)
    with transaction.atomic():
        for r in send_preview(plan):
            b, a = r["b"], r["action"]
            if a == "create":
                Project.objects.create(fiscalyear=plan.start_fy, name=b.name[:250], ward=b.ward, sector=b.sector, subsector=b.subsector,
                                       address=b.location or (f"वडा नं. {b.ward.name}" if b.ward else "-"), budget=float(r["new"]),
                                       budget_type=budget_type, project_type=project_type, bank_project=b)
            elif a == "update":
                p = r["p"]
                p.budget = float(r["new"])
                p.save(update_fields=["budget"])
            c[a] += 1
        plan.sent_at = timezone.now()
        plan.save(update_fields=["sent_at"])
    return c


# ---------------------------------------------------------------- तर्जुमा चक्र: वडा सीमा र पेश
def current_plan():
    """अहिले तर्जुमा भइरहेको MTEF (सबैभन्दा नयाँ मस्यौदा, नभए सबैभन्दा नयाँ)."""
    from .models import MTEFPlan

    qs = MTEFPlan.objects.select_related("start_fy").order_by("-start_fy__name")
    return qs.filter(status__in=("draft", "recommended", "executive")).first() or qs.first()


def ensure_ceilings(plan):
    """मस्यौदा MTEF मा सेटिङबाट थपिएका नयाँ सक्रिय स्रोत शीर्षकको पङ्क्ति थप्ने."""
    from .models import MTEFCeiling, ResourceHead

    if plan.status != "draft":
        return
    have = set(plan.ceilings.values_list("head_id", flat=True))
    for h in ResourceHead.objects.filter(is_active=True).exclude(id__in=have):
        MTEFCeiling.objects.create(plan=plan, head=h)


def ward_ceilings(plan):
    """{ward_id: WardCeiling} — सबै वडाका लागि (नभएको बनाइन्छ, रकम ०)."""
    from accounts.models import Ward

    from .models import WardCeiling

    have = {w.ward_id: w for w in plan.ward_ceilings.all()}
    for ward in Ward.objects.all():
        if ward.id not in have:
            have[ward.id] = WardCeiling.objects.create(plan=plan, ward=ward)
    return have


def ward_used(ward_id):
    """वडाका यस चक्रमा सक्रिय प्रस्तावको लागत (प्राविधिक लागत, नभए प्रस्तावित) — बजेटमा नगएका, स्थगित/अस्वीकृत बाहेक."""
    from .models import BankProject

    total = ZERO
    for b in BankProject.objects.filter(ward_id=ward_id, source="ward", hold="", converted=False, projects__isnull=True):
        total += D(b.tech_cost or b.proposed_cost)
    return total


def sector_ceilings(plan):
    """{sector_id: SectorCeiling} — सबै सक्रिय क्षेत्रका लागि (नभएको बनाइन्छ, रकम ०)."""
    from .models import Sector, SectorCeiling

    have = {c.sector_id: c for c in plan.sector_ceilings.select_related("sector")}
    for sec in Sector.objects.filter(is_active=True):
        if sec.id not in have:
            have[sec.id] = SectorCeiling.objects.create(plan=plan, sector=sec)
    return have


def sector_rows(plan):
    """हरेक क्षेत्रको सीमा, पालिका स्तरीय (वडाबाहेक) प्रस्तावको लागत र MTEF पहिलो वर्षमा छानिएको रकम."""
    from .models import BankProject

    cs = sector_ceilings(plan)
    proposed, chosen = {}, {}
    for b in BankProject.objects.filter(hold="", converted=False, projects__isnull=True).exclude(source="ward"):
        proposed[b.sector_id] = proposed.get(b.sector_id, ZERO) + D(b.tech_cost or b.proposed_cost)
    for ln in plan.lines.filter(kind="new").exclude(bank__source="ward").select_related("bank"):
        chosen[ln.bank.sector_id] = chosen.get(ln.bank.sector_id, ZERO) + D(ln.y1)
    rows = []
    for sc in sorted(cs.values(), key=lambda c: (c.sector.sort, c.sector_id)):
        amt, used = D(sc.amount), chosen.get(sc.sector_id, ZERO)
        rows.append({"sc": sc, "proposed": proposed.get(sc.sector_id, ZERO), "chosen": used,
                     "over": bool(amt) and used > amt, "pct": min(int(used * 100 / amt), 100) if amt else None,
                     "left": amt - used})
    return rows


def ward_locked(plan, ward_id):
    if plan is None or plan.status == "approved":
        return False
    wc = plan.ward_ceilings.filter(ward_id=ward_id).first()
    return bool(wc and wc.submitted_at)


STEP_TITLES = {1: "स्रोत अनुमान र बजेट सीमा", 2: "टोल / बस्ती स्तरमा छनौट", 3: "वडा प्राथमिकता", 4: "विषयगत समिति",
               5: "बजेट तथा कार्यक्रम तर्जुमा", 6: "कार्यपालिका", 7: "सभा"}


def cycle_step(plan):
    """तर्जुमा चक्रको अहिलेको चरण (१–७; सभाबाट पारितपछि ८ = कार्यान्वयन)."""
    if plan is None:
        return 2
    if plan.status == "approved":
        return 8
    if plan.status == "executive":
        return 7
    if plan.status == "recommended":
        return 6
    t = totals(plan)
    if not (plan.resource_ref and t["y1"]["cap"]):
        return 1
    wcs = list(plan.ward_ceilings.all())
    sub = sum(1 for w in wcs if w.submitted_at)
    if not sub:
        return 2
    if sub < len(wcs):
        return 3
    return 5 if plan.lines.filter(kind="new").exists() else 4


def liabilities(plan):
    """{kind: PlanLiability} — सबै प्रकार (नभएको बनाइन्छ, रकम ०)."""
    from .models import PlanLiability

    have = {x.kind: x for x in plan.liabilities.all()}
    for k, _l in PlanLiability.KINDS:
        if k not in have:
            have[k] = PlanLiability.objects.create(plan=plan, kind=k)
    return have


def payable_suggestion(plan):
    """भुक्तानी बाँकीको सुझाव: बजेट वर्षअघिका सम्झौता भएका (चालु / सम्पन्न) योजनामा सम्झौता वा बजेटबाट भुक्तानी घटाउँदा बाँकी.
    फर्काउँछ (रकम, योजना सङ्ख्या)."""
    from usercommittee.reports import all_rows

    from .models import Project

    ids = list(Project.objects.filter(fiscalyear__name__lt=plan.start_fy.name).values_list("id", flat=True))
    total, n = ZERO, 0
    if not ids:
        return total, n
    for r in all_rows(ids):
        if r["status"] not in ("running", "completed"):
            continue
        base = D(r.get("contract")) or D(r.get("allocated"))
        base = min(base, D(r.get("allocated"))) if D(r.get("allocated")) else base
        left = base - D(r.get("gross"))
        if left > 0:
            total += left
            n += 1
    return total.quantize(Decimal("1")), n


def sector_used(sector_id):
    """शाखा / पालिका स्तरीय (वडाबाहेक) सक्रिय प्रस्तावको लागत — शाखाको सीमासँग दाँज्न."""
    from .models import BankProject

    total = ZERO
    for b in BankProject.objects.filter(sector_id=sector_id, hold="", converted=False, projects__isnull=True).exclude(source="ward"):
        total += D(b.tech_cost or b.proposed_cost)
    return total


def sector_locked(plan, sector_id):
    if plan is None or plan.status == "approved":
        return False
    sc = plan.sector_ceilings.filter(sector_id=sector_id).first()
    return bool(sc and sc.submitted_at)

"""आयोजना बैंक — प्रस्तावदेखि सम्पन्नसम्म एउटै आयोजनाको अभिलेख.

चरण:
    प्रस्ताव → वडा प्राथमिकता (सेटिङबाट बन्द गर्न मिल्ने) → प्राविधिक जाँच → अङ्क र कोड → तयार
    → MTEF मा चयन → बजेट / कार्यान्वयन → सम्पन्न
    (कुनै पनि चरणमा अस्वीकृत / स्थगित — कारणसहित; पछि फेरि खोल्न मिल्ने)

नियम:
  * MTEF र कार्यान्वयनका चरण हातैले होइन — MTEF लाइन र जोडिएको Project बाट आफैँ
  * तयार घोषणा: प्राविधिक जाँच, अङ्क र पाँचै कोड (P1/P2, स्तम्भ, SDG, लैङ्गिक, जलवायु) चाहिन्छ
  * MTEF मा परेपछि प्रस्ताव र लागत बदल्न मिल्दैन (योजना अधिकृतले मात्र कारणसहित स्थगित गर्न सक्ने)
"""
from datetime import date
from decimal import Decimal

from django.db.models import Max
from django.urls import reverse

from usercommittee.workflow import _link, _modal, add_next, bs_date

ZERO = Decimal("0")
LABEL = "आयोजना बैंक"
NP = str.maketrans("0123456789", "०१२३४५६७८९")
STEP_INFO = {
    "proposal": ("प्रस्ताव", "प्रस्ताव दर्ता"),
    "ward": ("वडा", "वडा प्राथमिकता"),
    "technical": ("जाँच", "प्राविधिक जाँच"),
    "score": ("अङ्क", "अङ्क र कोड"),
    "ready": ("तयार", "तयार घोषणा"),
    "mtef": ("MTEF", "MTEF मा चयन"),
    "budget": ("बजेट", "बजेट / कार्यान्वयन"),
    "done": ("सम्पन्न", "सम्पन्न"),
}
SDG_NAMES = {1: "गरिबी अन्त्य", 2: "भोकमरी अन्त्य", 3: "स्वास्थ्य र कल्याण", 4: "गुणस्तरीय शिक्षा", 5: "लैङ्गिक समानता",
             6: "स्वच्छ पानी र सरसफाइ", 7: "स्वच्छ र सुलभ ऊर्जा", 8: "मर्यादित काम र आर्थिक वृद्धि",
             9: "उद्योग, नवप्रवर्तन र पूर्वाधार", 10: "असमानता न्यूनीकरण", 11: "दिगो सहर र बस्ती",
             12: "दिगो उपभोग र उत्पादन", 13: "जलवायु कार्य", 14: "जलमुनिको जीवन", 15: "जमिनमाथिको जीवन",
             16: "शान्ति, न्याय र सबल संस्था", 17: "लक्ष्यका लागि साझेदारी"}


def sdg_options():
    return [(i, f"{np(i)} — {n}") for i, n in SDG_NAMES.items()]


CHECKS = (("dpr", "DPR / लागत अनुमान"), ("land", "जग्गा उपलब्ध"), ("env", "वातावरणीय पक्ष ठीक"),
          ("feasible", "प्राविधिक रूपमा सम्भव"), ("no_duplicate", "दोहोरो छैन"))


def D(v):
    return Decimal(str(v)) if v not in (None, "") else ZERO


def np(v):
    return str(v).translate(NP)


def money(v):
    from .templatetags.outputs import money as m

    return m(v, 0)


def lakh(v):
    """रु. → 'xx.x लाख' (MTEF तालिकामा छोटो)."""
    v = D(v) / 100000
    return np(f"{v:,.1f}".rstrip("0").rstrip(".")) + " लाख"


def settings_():
    from .models import PlanSettings

    return PlanSettings.get()


def steps_keys():
    keys = list(STEP_INFO)
    if not settings_().ward_priority:
        keys.remove("ward")
    return keys


def columns():
    return [(k, STEP_INFO[k][0], STEP_INFO[k][1]) for k in steps_keys()]


def new_code():
    """PB-2083-0001 — वि.सं. वर्षअनुसार क्रम."""
    from biddings.views_workflow import to_bs

    from .models import BankProject

    prefix = settings_().prefix or "PB"
    year = (to_bs(date.today()) or str(date.today().year + 57))[:4]
    head = f"{prefix}-{year}-"
    last = BankProject.objects.filter(code__startswith=head).aggregate(m=Max("code"))["m"]
    n = int(last.rsplit("-", 1)[1]) + 1 if last and last.rsplit("-", 1)[1].isdigit() else 1
    while BankProject.objects.filter(code=f"{head}{n:04d}").exists():
        n += 1
    return f"{head}{n:04d}"


def apply_sector_defaults(b, overwrite=False):
    """क्षेत्रको सुझाइएका कोड (खाली भएमा मात्र, वा overwrite)."""
    d = getattr(b.sector, "plan_default", None) if b.sector_id else None
    if d is None:
        return False
    changed = False
    for f in ("priority", "pillar_id", "program_id", "sdg", "gender", "climate"):
        v = getattr(d, f)
        if v not in (None, "") and (overwrite or getattr(b, f) in (None, "")):
            setattr(b, f, v)
            changed = True
    return changed


# ---------------------------------------------------------------- अङ्क
def criteria():
    from .models import ScoreCriterion

    return list(ScoreCriterion.objects.filter(is_active=True))


def suggest(b, c):
    """एउटा आधारको सुझाव अङ्क (हातैले भर्ने आधारमा None)."""
    w = Decimal(c.weight)
    pop = b.population or 0
    if c.auto == "beneficiaries":
        return (w * min(Decimal(1), Decimal(pop) / 2000)).quantize(Decimal("0.1")) if pop else ZERO
    if c.auto == "readiness":
        return (w * sum(1 for k, _l in CHECKS if getattr(b, k)) / len(CHECKS)).quantize(Decimal("0.1"))
    if c.auto == "cost":
        if not pop or not b.cost:
            return ZERO
        per = D(b.cost) / pop                      # प्रतिव्यक्ति लागत: २,००० सम्म पूरा, २०,००० भन्दा बढी शून्य
        frac = Decimal(1) if per <= 2000 else (ZERO if per >= 20000 else (20000 - per) / 18000)
        return (w * frac).quantize(Decimal("0.1"))
    if c.auto == "inclusion":
        if not pop:
            return ZERO
        share = Decimal((b.women or 0) + (b.marginalized or 0)) / pop      # ८०% (महिला + सीमान्तकृत) मा पूरा
        return (w * min(Decimal(1), share / Decimal("0.8"))).quantize(Decimal("0.1"))
    if c.auto == "continuity":
        return w if b.nature in ("continuation", "upgrade") else ZERO
    if c.auto == "climate":
        return w if b.climate == "high" else (w / 2 if b.climate == "relevant" else ZERO)
    return None


def max_score():
    return sum(c.weight for c in criteria()) or 100


# ---------------------------------------------------------------- कार्यान्वयन (जोडिएको Project)
def exec_map(banks):
    """{bank_id: {...}} — जोडिएका Project को बजेट, सम्झौता, खर्च, अवस्था, प्रगति."""
    from usercommittee.reports import all_rows

    from . import field as fd
    from .models import Project

    ids = [b.id for b in banks]
    projects = list(Project.objects.filter(bank_project_id__in=ids).select_related("fiscalyear", "procurement_type", "ward"))
    if not projects:
        return {}
    rows = {r["p"].id: r for r in all_rows([p.id for p in projects])}
    out = {}
    for p in projects:
        r = rows.get(p.id)
        info = out.setdefault(p.bank_project_id, {"projects": [], "allocated": ZERO, "contract": ZERO, "spent": ZERO,
                                                  "status": "not_started", "status_label": "प्रक्रिया सुरु नभएको",
                                                  "progress": None})
        info["projects"].append(p)
        info["allocated"] += D(p.budget)
        if r:
            info["contract"] += D(r.get("contract"))
            info["spent"] += D(r["gross"])
            rank = {"not_started": 0, "running": 1, "paid": 2, "completed": 3}
            if rank.get(r["status"], 0) >= rank.get(info["status"], 0):
                info["status"], info["status_label"] = r["status"], r["status_label"]
        prog = fd.latest_progress(p)
        if prog is not None:
            info["progress"] = prog
        elif p.status == 2:
            info["progress"] = 100
    for info in out.values():
        if info["status"] in ("completed", "paid") and info["progress"] is None:
            info["progress"] = 100
    return out


def project_url(p):
    from .procurement import WORKFLOW_URL

    code = p.procurement_type.code if p.procurement_type_id else ""
    if code in WORKFLOW_URL:
        return reverse(WORKFLOW_URL[code]) + f"?open={p.id}"
    if code in ("direct", "amanat"):
        return reverse("minor_list") + f"?open={p.id}"
    return reverse("field_project", args=[p.id])


# ---------------------------------------------------------------- चरण
def workflow(b, user=None, ex=None, lines=None):
    """(steps, summary). ex: exec_map को यो आयोजनाको भाग; lines: MTEF लाइनहरू."""
    from accounts.permissions import user_perms

    P = user_perms(user) if user is not None else set()
    propose, wardp, screen, manage = ("bank.propose" in P, "bank.ward_priority" in P, "bank.screen" in P, "bank.manage" in P)
    keys = steps_keys()
    if lines is None:
        lines = list(b.mtef_lines.select_related("plan__start_fy").all())
    ex = ex or {}
    bid = b.id
    in_mtef = bool(lines)
    held = bool(b.hold)
    st = {}

    actions = []
    if not in_mtef and not held and ((propose and not b.ready_at) or manage):
        actions.append(_link("प्रस्ताव सम्पादन", "bank_edit", bid))
    note = f"{b.get_nature_display()} · {b.get_source_display()}" + (f" · {b.requested_by}" if b.requested_by else "")
    if b.converted:
        note += " · पुरानो योजनाबाट रूपान्तरण"
    st["proposal"] = {"done": True, "date": b.created_at.date() if b.created_at else None, "note": note, "actions": actions}

    if "ward" in keys:
        actions = []
        if wardp and not b.ward_approved_at and not held and b.ward_id:
            actions.append(_link("वडाको प्राथमिकता क्रम तोक्नुहोस्", "bank_ward_priority", query=f"ward={b.ward_id}", primary=True))
        note = (f"वडाको प्राथमिकता क्रम {np(b.ward_rank)}" if b.ward_rank else "") + (f" · {b.ward_approver}" if b.ward_approver else "")
        st["ward"] = {"done": bool(b.ward_approved_at), "date": b.ward_approved_at.date() if b.ward_approved_at else None,
                      "note": note or "वडा अध्यक्षले वडाका सबै प्रस्तावको क्रम तोक्नुहुन्छ", "actions": actions}

    ward_ok = "ward" not in keys or bool(b.ward_approved_at)
    actions = []
    if screen and ward_ok and not held and not in_mtef:
        actions.append(_link("प्राविधिक जाँच सम्पादन" if b.tech_at else "प्राविधिक जाँच गर्नुहोस्", "bank_technical", bid,
                             primary=not b.tech_at))
    note = ""
    if b.tech_at:
        ok = [lbl for k, lbl in CHECKS if getattr(b, k)]
        note = f"{np(len(ok))}/{np(len(CHECKS))} तयारी" + (f" · लागत रु. {money(b.tech_cost)}" if b.tech_cost else "") + \
               (f" · {b.tech_by}" if b.tech_by else "")
    st["technical"] = {"done": bool(b.tech_at), "date": b.tech_at.date() if b.tech_at else None, "note": note, "actions": actions}

    actions = []
    if manage and b.tech_at and not held and not in_mtef:
        actions.append(_link("अङ्क र कोड सम्पादन" if b.scored_at else "अङ्क र कोड दिनुहोस्", "bank_score", bid,
                             primary=not b.scored_at))
    note = ""
    if b.scored_at:
        note = f"अङ्क {np(f'{D(b.score):g}')}/{np(max_score())}" + (f" · {b.priority}" if b.priority else "") + \
               (f" · SDG {np(b.sdg)}" if b.sdg else "")
        if not b.coded:
            note += " · ⚠ कोड अधुरो"
    st["score"] = {"done": bool(b.scored_at and b.coded), "date": b.scored_at.date() if b.scored_at else None, "note": note,
                   "actions": actions}

    actions = []
    if manage and b.scored_at and not in_mtef:
        actions.append(_modal("तयार / स्थगित / अस्वीकृत" if not b.ready_at and not held else "निर्णय बदल्नुहोस्", "bank_decide", bid,
                              icon="stamp", primary=not b.ready_at and not held))
    note = (f"निर्णय {b.ready_ref}" if b.ready_ref else "योजना शाखाबाट") if b.ready_at else ""
    st["ready"] = {"done": bool(b.ready_at), "date": b.ready_at.date() if b.ready_at else None, "note": note, "actions": actions}

    actions = []
    note = ""
    if lines:
        last = sorted(lines, key=lambda x: x.plan.start_fy.name)[-1]
        actions.append(_link(f"MTEF {last.plan}", "mtef_plan", last.plan_id))
        note = " / ".join(lakh(v) for v in (last.y1, last.y2, last.y3)) + f" ({last.plan.get_status_display()})"
    elif b.ready_at and not held and "mtef.manage" in P:
        actions.append(_modal("MTEF मा थप्नुहोस्", "bank_to_mtef", bid, icon="layer-group"))
        note = "योजना शाखाले मस्यौदा MTEF मा थप्छ"
    elif b.ready_at and not held:
        note = "योजना शाखाले MTEF मा छान्न बाँकी"
    st["mtef"] = {"done": in_mtef, "date": None, "note": note, "actions": actions}

    projects = ex.get("projects", [])
    actions = [{"label": f"{p.name[:30]} — आ.व. {p.fiscalyear.name if p.fiscalyear else ''}", "url": project_url(p),
                "primary": False, "new_tab": False} for p in projects[:3]]
    note = ""
    if projects:
        note = f"बजेट रु. {money(ex['allocated'])}" + (f" · सम्झौता {money(ex['contract'])}" if ex["contract"] else "") + \
               f" · खर्च {money(ex['spent'])}" + (f" · प्रगति {np(ex['progress'])}%" if ex.get("progress") is not None else "")
    if not projects and lines and not held:
        # बजेटमा जान बाँकी — अर्को काम र कसले गर्ने देखाउने
        last = sorted(lines, key=lambda x: x.plan.start_fy.name)[-1]
        plan = last.plan
        if plan.status != "approved":
            note = {"draft": f"MTEF {plan} मस्यौदामा — बजेट तथा कार्यक्रम तर्जुमा समितिको सिफारिस बाँकी",
                    "recommended": f"MTEF {plan} समितिबाट सिफारिस — कार्यपालिकाको स्वीकृति बाँकी",
                    "executive": f"MTEF {plan} कार्यपालिकाबाट स्वीकृत — सभाबाट पारित हुन बाँकी"}[plan.status]
            mine = (plan.status == "draft" and "mtef.manage" in P) or (plan.status != "draft" and "mtef.approve" in P)
            if mine:
                actions.append(_modal({"draft": "समितिको सिफारिस प्रविष्ट गर्नुहोस्", "recommended": "कार्यपालिकाको स्वीकृति प्रविष्ट गर्नुहोस्",
                                       "executive": "सभाबाट पारित प्रविष्ट गर्नुहोस्"}[plan.status], "mtef_approve", plan.id, icon="stamp"))
            else:
                actions.append(_link("MTEF हेर्नुहोस्", "mtef_plan", plan.id))
        elif last.y1 <= 0:
            note = f"MTEF {plan} मा पहिलो वर्षको रकम छैन — त्यो वर्षको बजेटमा पर्दैन"
            actions.append(_link("MTEF हेर्नुहोस्", "mtef_plan", plan.id))
        else:
            note = f"MTEF {plan} स्वीकृत — योजना शाखाले “वार्षिक बजेटमा पठाउने” गरेपछि योजना बन्छ"
            if "mtef.manage" in P:
                actions.append(_link("वार्षिक बजेटमा पठाउनुहोस्", "mtef_send", plan.id, primary=True))
            else:
                actions.append(_link("MTEF हेर्नुहोस्", "mtef_plan", plan.id))
    st["budget"] = {"done": bool(projects), "date": None, "note": note, "actions": actions}

    finished = bool(projects) and ex.get("status") in ("completed", "paid")
    done_note = ex.get("status_label", "") if finished else (
        "खरिद, सम्झौता र अन्तिम भुक्तानी सकिएपछि आफैँ सम्पन्न हुन्छ" + (f" · अहिले: {ex.get('status_label')}" if projects else "")
        if projects else "")
    st["done"] = {"done": finished, "date": None, "note": done_note,
                  "actions": [{"label": "योजनाको कार्यप्रवाह खोल्नुहोस्", "url": project_url(projects[0]), "primary": True,
                               "new_tab": False}] if projects and not finished else []}

    steps, current = [], None
    for n, key in enumerate(keys, start=1):
        info = st[key]
        if info["done"]:
            state = "done"
        elif current is None:
            current = n
            state = "warning" if held else "current"
        else:
            state = "pending"
        short, label = STEP_INFO[key]
        steps.append({"n": n, "key": key, "short": short, "label": label, "state": state, "date_bs": bs_date(info.get("date")),
                      **info})
    cur = next((x for x in steps if x["n"] == current), None)
    if held:
        status_key, status_label = b.hold, b.get_hold_display()
    elif current is None:
        status_key, status_label = "done", "सम्पन्न"
    else:
        status_key = cur["key"]
        status_label = {"proposal": "प्रस्ताव", "ward": "वडा प्राथमिकता बाँकी", "technical": "प्राविधिक जाँच बाँकी",
                        "score": "अङ्क / कोड बाँकी", "ready": "तयार घोषणा बाँकी", "mtef": "तयार — MTEF कुरेको",
                        "budget": "MTEF मा — बजेट कुरेको", "done": "कार्यान्वयनमा"}[cur["key"]]
    return steps, {
        "b": b, "current": current, "current_key": status_key, "current_label": status_label,
        "done_count": sum(1 for x in steps if x["state"] == "done"), "total": len(steps), "held": held,
        "exec": ex, "lines": lines,
    }


def row_cells(steps):
    by = {x["key"]: x for x in steps}
    return [by.get(k) for k, _s, _l in columns()]


def with_next(steps, return_to):
    if not return_to:
        return
    for x in steps:
        for a in x.get("actions", []):
            if not a["new_tab"] and not a.get("modal") and "/Bank/" in a["url"]:
                a["url"] = add_next(a["url"], return_to)


def queryset():
    from .models import BankProject

    return BankProject.objects.select_related("ward", "sector", "subsector", "pillar", "program", "fiscalyear")

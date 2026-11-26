"""योजना तर्जुमा — थप सुविधा: टोल भेला, बजेट पुस्तिका, SuTRA निर्यात, लैङ्गिक / जलवायु / SDG प्रतिवेदन,
त्रैमासिक प्रगति (अनुगमन), नक्सा र नागरिकका लागि स्वीकृत कार्यक्रम."""
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.models import Ward
from accounts.permissions import user_perms
from biddings.views_workflow import to_ad, to_bs

from . import bank as bk
from . import mtef as mt
from .models import FiscalYear, MTEFPlan, Project, ToleMeeting

ZERO = Decimal("0")
NPD = str.maketrans("०१२३४५६७८९", "0123456789")
PLAN_VIEW = {"bank.view", "bank.manage", "mtef.manage", "mtef.approve", "dashboard.view", "report.uc"}


def _wards(user):
    from usercommittee.views_workflow import _user_wards

    return _user_wards(user)


def _int(v):
    try:
        n = int(str(v or "0").translate(NPD).strip() or 0)
        return n if n >= 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------- टोल / बस्ती भेला
@login_required
def tole_list(request):
    P = user_perms(request.user)
    if not P & {"bank.view", "bank.propose", "bank.manage"}:
        return redirect("unauthorized")
    can = bool(P & {"bank.propose", "bank.manage"}) and request.user.user_type != "11"
    wards = _wards(request.user)
    allowed = Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all()
    edit = None
    if request.GET.get("edit"):
        edit = get_object_or_404(ToleMeeting.objects.filter(ward__in=allowed), id=request.GET["edit"])
    errors = []
    if request.method == "POST":
        if not can:
            return redirect("unauthorized")
        if request.POST.get("act") == "delete":
            m = get_object_or_404(ToleMeeting.objects.filter(ward__in=allowed), id=request.POST.get("id"))
            if m.projects.exists():
                messages.error(request, "यो भेलाबाट आएका आयोजना बैंकमा छन् — मेटाउन मिल्दैन।")
            else:
                m.delete()
                messages.success(request, "भेला मेटाइयो।")
            return redirect("tole_list")
        m = edit or ToleMeeting(created_by=request.user)
        ward = allowed.filter(id=request.POST.get("ward") or 0).first()
        d = to_ad(request.POST.get("date"))
        tole = (request.POST.get("tole") or "").strip()
        nums = {k: _int(request.POST.get(k)) for k in ("total", "women", "dalit", "janajati", "disabled")}
        if ward is None:
            errors.append("वडा छान्नुहोस्।")
        if not tole:
            errors.append("टोल / बस्तीको नाम लेख्नुहोस्।")
        if d is None:
            errors.append("भेला मिति (वि.सं. २०८३-११-२० जस्तो) लेख्नुहोस्।")
        if any(v is None for v in nums.values()):
            errors.append("उपस्थिति सङ्ख्या पूर्ण अङ्कमा लेख्नुहोस्।")
        elif any(nums[k] > nums["total"] for k in ("women", "dalit", "janajati", "disabled")) and nums["total"]:
            errors.append("महिला / दलित / जनजाति / अपाङ्गता सङ्ख्या जम्मा उपस्थितिभन्दा बढी भयो।")
        for k, f in (("minute", request.FILES.get("minute")), ("photo", request.FILES.get("photo"))):
            if f and f.size > 10 * 1024 * 1024:
                errors.append("फाइल १० MB भन्दा सानो हुनुपर्छ।")
        if not errors:
            m.ward, m.tole, m.date = ward, tole[:200], d
            m.venue = (request.POST.get("venue") or "").strip()[:200]
            m.chair = (request.POST.get("chair") or "").strip()[:200]
            m.decision = (request.POST.get("decision") or "").strip()
            for k, v in nums.items():
                setattr(m, k, v)
            from .fiscal import current_fy
            m.fiscalyear = m.fiscalyear or current_fy()
            if request.FILES.get("minute"):
                m.minute = request.FILES["minute"]
            if request.FILES.get("photo"):
                m.photo = request.FILES["photo"]
            m.save()
            messages.success(request, f"टोल भेला “{m.tole}” सुरक्षित भयो — आयोजना बैंकमा प्रस्ताव गर्दा यो भेला छान्नुहोस्।")
            return redirect("tole_list")
    qs = ToleMeeting.objects.filter(ward__in=allowed).select_related("ward").prefetch_related("projects")
    wsel = request.GET.get("ward", "")
    if wsel.isdigit():
        qs = qs.filter(ward_id=wsel)
    rows = list(qs[:200])
    tot = {k: sum(getattr(r, k) for r in rows) for k in ("total", "women", "dalit", "janajati", "disabled")}
    tot["n"], tot["projects"] = len(rows), sum(r.projects.count() for r in rows)
    form = request.POST if request.method == "POST" else ({
        "ward": str(edit.ward_id), "tole": edit.tole, "date": to_bs(edit.date), "venue": edit.venue, "chair": edit.chair,
        "total": edit.total, "women": edit.women, "dalit": edit.dalit, "janajati": edit.janajati, "disabled": edit.disabled,
        "decision": edit.decision} if edit else {})
    return render(request, "projects/tole_list.html", {"rows": rows, "wards": allowed, "can": can, "edit": edit,
                                                       "errors": errors, "form": form, "tot": tot, "wsel": wsel})


# ---------------------------------------------------------------- साझा: MTEF छान्ने र पहिलो वर्षका आयोजना
def _pick_plan(request):
    plans = list(MTEFPlan.objects.select_related("start_fy").order_by("-start_fy__name"))
    sel = request.GET.get("plan", "")
    plan = next((p for p in plans if str(p.id) == sel), None)
    if plan is None:
        plan = next((p for p in plans if p.status == "approved"), None) or mt.current_plan()
    return plan, plans


def _plan_lines(plan, wards=None):
    """पहिलो वर्षमा रकम भएका MTEF पङ्क्ति (आयोजना बैंकसहित)."""
    qs = plan.lines.select_related("bank__ward", "bank__sector", "bank__subsector", "bank__pillar").filter(y1__gt=0)
    if wards is not None:
        qs = qs.filter(bank__ward_id__in=wards)
    return list(qs)


# ---------------------------------------------------------------- बजेट / वार्षिक कार्यक्रम पुस्तिका
@login_required
def budget_book(request):
    P = user_perms(request.user)
    if not P & PLAN_VIEW:
        return redirect("unauthorized")
    plan, plans = _pick_plan(request)
    if plan is None:
        messages.error(request, "MTEF बनेको छैन।")
        return redirect("mtef_list")
    lines = _plan_lines(plan)
    t = mt.totals(plan)
    wards, palika = {}, {}
    for ln in lines:
        b = ln.bank
        if b.source == "ward" and b.ward_id:
            g = wards.setdefault(b.ward_id, {"name": b.ward.name, "rows": [], "sum": ZERO})
        else:
            key = b.sector_id or 0
            g = palika.setdefault(key, {"name": b.sector.name if b.sector else "क्षेत्र नतोकिएको", "rows": [], "sum": ZERO})
        g["rows"].append(ln)
        g["sum"] += ln.y1
    def wkey(w):
        n = str(w["name"]).translate(NPD)
        return int(n) if n.isdigit() else 999
    ward_groups = sorted(wards.values(), key=wkey)
    sector_groups = sorted(palika.values(), key=lambda g: g["name"])
    ceilings = mt.ward_ceilings(plan)
    for g in ward_groups:
        wc = next((c for c in ceilings.values() if c.ward.name == g["name"]), None)
        g["ceiling"] = wc.amount if wc else None
    heads = list(plan.ceilings.select_related("head").order_by("head__sort"))
    from .models import PlanLiability
    L = mt.liabilities(plan)
    liabs = [(lbl, L[k]) for k, lbl in PlanLiability.KINDS]
    total_y1 = sum((ln.y1 for ln in lines), ZERO)
    return render(request, "projects/plan_budget_book.html", {
        "plan": plan, "plans": plans, "t": t, "ward_groups": ward_groups, "sector_groups": sector_groups,
        "heads": heads, "liabs": liabs, "total_y1": total_y1, "n": len(lines),
        "ward_total": sum((g["sum"] for g in ward_groups), ZERO), "sector_total": sum((g["sum"] for g in sector_groups), ZERO),
    })


# ---------------------------------------------------------------- SuTRA का लागि Excel
@login_required
def sutra_export(request):
    from .models import WorkCategory
    from .views_bank import _xlsx, _xlsx_response

    P = user_perms(request.user)
    if not P & {"budget.view", "budget.manage", "mtef.manage", "report.uc"}:
        return redirect("unauthorized")
    can_edit = bool(P & {"budget.manage", "mtef.manage"})
    cats = list(WorkCategory.objects.filter(is_active=True))
    if request.method == "POST":
        if not can_edit:
            return redirect("unauthorized")
        for c in cats:
            v = (request.POST.get(f"e{c.id}") or "").strip().translate(NPD)[:20]
            if v != c.econ_code:
                c.econ_code = v
                c.save(update_fields=["econ_code"])
        messages.success(request, "खर्च शीर्षक (आर्थिक संकेत) सुरक्षित भयो।")
        return redirect(reverse("sutra_export") + (f"?fiscalyear={request.POST.get('fiscalyear')}" if request.POST.get("fiscalyear") else ""))
    from .fiscal import current_fy
    fys = list(FiscalYear.objects.order_by("-name"))
    fy = next((f for f in fys if str(f.id) == request.GET.get("fiscalyear", "")), None) or current_fy()
    qs = Project.objects.filter(fiscalyear=fy).select_related("budget_head", "work_category", "budget_type", "ward", "sector", "bank_project")
    rows = list(qs.order_by("budget_head__code", "ward_id", "name"))
    missing = {"head": sum(1 for p in rows if not p.budget_head_id), "econ": sum(1 for p in rows if not (p.work_category and p.work_category.econ_code))}
    if request.GET.get("export") == "xlsx":
        data = []
        for i, p in enumerate(rows, 1):
            data.append([i, fy.name if fy else "", p.budget_head.code if p.budget_head else "", p.budget_head.name if p.budget_head else "",
                         p.name, p.work_category.econ_code if p.work_category else "", p.work_category.name if p.work_category else "",
                         p.budget_type.name if p.budget_type else "", p.ward.name if p.ward else "", p.sector.name if p.sector else "",
                         float(p.budget or 0), p.bank_project.code if p.bank_project else ""])
        wb = _xlsx("कार्यक्रम", ["क्र.सं.", "आ.व.", "बजेट उपशीर्षक नं.", "बजेट उपशीर्षक", "कार्यक्रम / आयोजना", "खर्च शीर्षक (आर्थिक संकेत)",
                                 "कामको प्रकार", "स्रोत", "वडा", "क्षेत्र", "विनियोजित रकम", "आयोजना बैंक नं."], data,
                   [6, 9, 14, 26, 40, 14, 18, 16, 6, 18, 14, 14])
        ws = wb.create_sheet("सारांश")
        ws.append(["बजेट उपशीर्षक नं.", "खर्च शीर्षक", "आयोजना सङ्ख्या", "रकम"])
        agg = {}
        for p in rows:
            k = (p.budget_head.code if p.budget_head else "", p.work_category.econ_code if p.work_category else "")
            a = agg.setdefault(k, [0, 0.0])
            a[0] += 1
            a[1] += float(p.budget or 0)
        for (h, e), (n, amt) in sorted(agg.items()):
            ws.append([h, e, n, amt])
        return _xlsx_response(wb, f"sutra-{(fy.name if fy else '').replace('/', '-')}.xlsx")
    return render(request, "projects/plan_sutra.html", {"fy": fy, "fys": fys, "rows": rows[:30], "n": len(rows), "cats": cats,
                                                       "missing": missing, "can_edit": can_edit,
                                                       "total": sum((Decimal(str(p.budget or 0)) for p in rows), ZERO)})


# ---------------------------------------------------------------- लैङ्गिक / जलवायु / SDG / स्तम्भ प्रतिवेदन
@login_required
def coding_report(request):
    from .models import BankProject

    P = user_perms(request.user)
    if not P & PLAN_VIEW:
        return redirect("unauthorized")
    plan, plans = _pick_plan(request)
    if plan is None:
        messages.error(request, "MTEF बनेको छैन।")
        return redirect("mtef_list")
    lines = _plan_lines(plan)
    total = sum((ln.y1 for ln in lines), ZERO)

    def group(label_of, order=None):
        g = {}
        for ln in lines:
            k = label_of(ln.bank) or "कोड नदिइएको"
            a = g.setdefault(k, {"label": k, "n": 0, "amt": ZERO})
            a["n"] += 1
            a["amt"] += ln.y1
        out = list(g.values())
        if order:
            out.sort(key=lambda a: order.index(a["label"]) if a["label"] in order else 99)
        else:
            out.sort(key=lambda a: -a["amt"])
        for a in out:
            a["pct"] = round(float(a["amt"] * 100 / total), 1) if total else 0
        return out

    G = dict(BankProject.GENDER)
    C = dict(BankProject.CLIMATE)
    sections = [
        {"title": "लैङ्गिक उत्तरदायी बजेट", "icon": "venus-mars", "rows": group(lambda b: G.get(b.gender), list(G.values()) + ["कोड नदिइएको"])},
        {"title": "जलवायु सान्दर्भिकता", "icon": "leaf", "rows": group(lambda b: C.get(b.climate), list(C.values()) + ["कोड नदिइएको"])},
        {"title": "दिगो विकास लक्ष्य (SDG)", "icon": "globe", "rows": group(lambda b: f"SDG {b.sdg}" if b.sdg else None,
                                                                         [f"SDG {i}" for i in range(1, 18)] + ["कोड नदिइएको"])},
        {"title": "रणनीतिक स्तम्भ", "icon": "columns-3", "rows": group(lambda b: b.pillar.name if b.pillar else None)},
        {"title": "प्राथमिकता", "icon": "flag", "rows": group(lambda b: b.priority or None, ["P1", "P2", "कोड नदिइएको"])},
        {"title": "क्षेत्र", "icon": "layers", "rows": group(lambda b: b.sector.name if b.sector else None)},
    ]
    gender_direct = next((r for r in sections[0]["rows"] if r["label"] == G.get("direct")), None)
    climate_rel = sum((r["amt"] for r in sections[1]["rows"] if r["label"] in (C.get("high"), C.get("relevant"))), ZERO)
    return render(request, "projects/plan_coding.html", {
        "plan": plan, "plans": plans, "sections": sections, "total": total, "n": len(lines),
        "gender_pct": gender_direct["pct"] if gender_direct else 0,
        "climate_pct": round(float(climate_rel * 100 / total), 1) if total else 0,
        "uncoded": sum(1 for ln in lines if not ln.bank.coded),
    })


def _rows_all(qs):
    """सबै योजना — खरिद विधि नतोकिएका (कार्यप्रवाहमा नआएका) पनि 'सुरु नभएको' भनी समावेश."""
    from usercommittee.reports import STATUS, all_rows

    projects = list(qs.select_related("ward", "sector"))
    got = {r["p"].id: r for r in all_rows([p.id for p in projects])}
    out = []
    for p in projects:
        r = got.get(p.id) or {"p": p, "status": "not_started", "allocated": Decimal(str(p.budget or 0)), "gross": ZERO}
        r["status_label"] = STATUS.get(r["status"], r["status"])
        out.append(r)
    return out


# ---------------------------------------------------------------- त्रैमासिक प्रगति / अनुगमन
def _fy_span(fy):
    """आ.व. '2083/84' → (साउन १ को date, असार मसान्तको date)."""
    try:
        y = int(fy.name.translate(NPD).split("/")[0])
    except Exception:
        return None, None
    start, nxt = to_ad(f"{y}-04-01"), to_ad(f"{y + 1}-04-01")
    if not start or not nxt:
        return None, None
    from datetime import timedelta
    return start, nxt - timedelta(days=1)


QUARTERS = ("पहिलो (साउन–असोज)", "दोस्रो (कात्तिक–पुस)", "तेस्रो (माघ–चैत)", "चौथो (वैशाख–असार)")


@login_required
def progress_report(request):
    from usercommittee.reports import STATUS, all_rows

    from .fiscal import current_fy
    from .models import ProjectTimeline

    P = user_perms(request.user)
    if not P & (PLAN_VIEW | {"report.outputs", "field.view"}):
        return redirect("unauthorized")
    wards = _wards(request.user)
    fys = list(FiscalYear.objects.order_by("-name"))
    fy = next((f for f in fys if str(f.id) == request.GET.get("fiscalyear", "")), None) or current_fy()
    start, end = _fy_span(fy) if fy else (None, None)
    today = date.today()
    if start and end and today >= start:
        frac = max(0.0, min(1.0, (today - start).days / max((end - start).days, 1)))
        quarter = QUARTERS[min(int(frac * 4), 3)] if today <= end else "आ.व. सकिएको"
    elif start:
        frac, quarter = None, "आ.व. सुरु भएको छैन"
    else:
        frac, quarter = None, ""
    expected = round(frac * 100) if frac is not None else None
    qs = Project.objects.filter(fiscalyear=fy) if fy else Project.objects.none()
    if wards is not None:
        qs = qs.filter(ward_id__in=wards)
    ids = list(qs.values_list("id", flat=True))
    phys = {}
    for tl in ProjectTimeline.objects.filter(project_id__in=ids, progress_pct__isnull=False).order_by("created_at"):
        phys[tl.project_id] = tl.progress_pct
    rows = []
    for r in _rows_all(qs):
        p = r["p"]
        alloc = Decimal(r["allocated"] or 0)
        paid = Decimal(r["gross"] or 0)
        fin = round(float(paid * 100 / alloc)) if alloc else 0
        done = r["status"] in ("completed", "paid")
        ph = 100 if done else phys.get(p.id)
        basis = ph if ph is not None else fin
        lag = (not done and expected is not None and expected >= 25 and basis < expected - 20)
        rows.append({"p": p, "status": r["status"], "status_label": STATUS.get(r["status"], r["status"]), "alloc": alloc, "paid": paid,
                     "fin": fin, "phys": ph, "lag": lag, "gap": (expected - basis) if expected is not None else None})
    wsel, only_lag = request.GET.get("ward", ""), request.GET.get("lag") == "1"
    all_r = rows
    if wsel.isdigit():
        rows = [r for r in rows if str(r["p"].ward_id) == wsel]
    if only_lag:
        rows = [r for r in rows if r["lag"]]
    rows.sort(key=lambda r: (not r["lag"], -(r["gap"] or 0)))
    by_ward = {}
    for r in all_r:
        w = by_ward.setdefault(r["p"].ward_id, {"name": r["p"].ward.name if r["p"].ward else "—", "n": 0, "alloc": ZERO, "paid": ZERO,
                                                  "done": 0, "lag": 0, "phys": []})
        w["n"] += 1
        w["alloc"] += r["alloc"]
        w["paid"] += r["paid"]
        w["done"] += 1 if r["status"] in ("completed", "paid") else 0
        w["lag"] += 1 if r["lag"] else 0
        if r["phys"] is not None:
            w["phys"].append(r["phys"])
    ward_rows = []
    for w in by_ward.values():
        w["fin"] = round(float(w["paid"] * 100 / w["alloc"])) if w["alloc"] else 0
        w["phys_avg"] = round(sum(w["phys"]) / len(w["phys"])) if w["phys"] else None
        ward_rows.append(w)
    ward_rows.sort(key=lambda w: int(str(w["name"]).translate(NPD)) if str(w["name"]).translate(NPD).isdigit() else 999)
    alloc = sum((r["alloc"] for r in all_r), ZERO)
    paid = sum((r["paid"] for r in all_r), ZERO)
    summary = {"n": len(all_r), "alloc": alloc, "paid": paid, "fin": round(float(paid * 100 / alloc)) if alloc else 0,
               "done": sum(1 for r in all_r if r["status"] in ("completed", "paid")),
               "not_started": sum(1 for r in all_r if r["status"] == "not_started"),
               "lag": sum(1 for r in all_r if r["lag"]),
               "no_phys": sum(1 for r in all_r if r["phys"] is None)}
    return render(request, "projects/plan_progress.html", {
        "fy": fy, "fys": fys, "rows": rows[:300], "ward_rows": ward_rows, "summary": summary, "expected": expected,
        "quarter": quarter, "wards": Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all(),
        "wsel": wsel, "only_lag": only_lag})


# ---------------------------------------------------------------- योजनाको नक्सा
@login_required
def project_map(request):
    from usercommittee.reports import STATUS, all_rows

    from .fiscal import current_fy
    from .models import ProjectTimeline

    P = user_perms(request.user)
    if not P & (PLAN_VIEW | {"field.view", "report.outputs"}):
        return redirect("unauthorized")
    wards = _wards(request.user)
    fys = list(FiscalYear.objects.order_by("-name"))
    fy = next((f for f in fys if str(f.id) == request.GET.get("fiscalyear", "")), None) or current_fy()
    qs = Project.objects.filter(fiscalyear=fy) if fy else Project.objects.none()
    if wards is not None:
        qs = qs.filter(ward_id__in=wards)
    wsel = request.GET.get("ward", "")
    if wsel.isdigit():
        qs = qs.filter(ward_id=wsel)
    ids = list(qs.values_list("id", flat=True))
    gps = {}
    for tl in ProjectTimeline.objects.filter(project_id__in=ids, latitude__isnull=False, longitude__isnull=False).order_by("created_at"):
        gps[tl.project_id] = (float(tl.latitude), float(tl.longitude))
    points, missing = [], 0
    for r in _rows_all(qs):
        p = r["p"]
        ll = (float(p.latitude), float(p.longitude)) if p.latitude is not None and p.longitude is not None else gps.get(p.id)
        if not ll:
            missing += 1
            continue
        points.append({"lat": ll[0], "lng": ll[1], "name": p.name, "ward": p.ward.name if p.ward else "",
                       "budget": float(r["allocated"] or 0), "status": r["status"], "label": STATUS.get(r["status"], ""),
                       "sector": p.sector.name if p.sector else "", "url": reverse("project_timeline", args=[p.id])})
    return render(request, "projects/plan_map.html", {
        "fy": fy, "fys": fys, "points": points, "missing": missing, "n": len(ids), "wsel": wsel,
        "wards": Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all()})


# ---------------------------------------------------------------- नागरिकका लागि: स्वीकृत वार्षिक कार्यक्रम
def public_plan(request):
    from .views_field import _citizen_on

    if not _citizen_on():
        raise Http404
    plans = [p for p in MTEFPlan.objects.select_related("start_fy").order_by("-start_fy__name") if p.status == "approved"]
    plan = next((p for p in plans if str(p.id) == request.GET.get("plan", "")), plans[0] if plans else None)
    groups, ward_rows, total = [], [], ZERO
    if plan:
        lines = _plan_lines(plan)
        total = sum((ln.y1 for ln in lines), ZERO)
        by = {}
        for ln in lines:
            b = ln.bank
            key = ("w", b.ward.name) if (b.source == "ward" and b.ward_id) else ("s", b.sector.name if b.sector else "पालिका स्तरीय")
            g = by.setdefault(key, {"title": f"वडा नं. {bk.np(key[1])}" if key[0] == "w" else f"पालिका स्तरीय — {key[1]}",
                                    "kind": key[0], "rows": [], "sum": ZERO, "sortkey": key})
            g["rows"].append(ln)
            g["sum"] += ln.y1
        groups = sorted(by.values(), key=lambda g: (g["kind"] != "w", int(str(g["sortkey"][1]).translate(NPD)) if str(g["sortkey"][1]).translate(NPD).isdigit() else 999, g["title"]))
        for wc in sorted(mt.ward_ceilings(plan).values(), key=lambda w: w.ward_id):
            if wc.amount:
                ward_rows.append(wc)
    return render(request, "projects/plan_public.html", {"plan": plan, "plans": plans, "groups": groups, "ward_rows": ward_rows,
                                                        "n": sum(len(g["rows"]) for g in groups),
                                                        "total": total, "t": mt.totals(plan) if plan else None})

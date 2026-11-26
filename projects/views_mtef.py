"""MTEF — योजना सूची / नयाँ, एउटा योजना (स्रोत सीमा, प्रतिबद्धता, नयाँ चयन, सारांश), बैंकबाट थप्ने,
स्वीकृति, वार्षिक बजेटमा पठाउने, Excel; र योजना vs खर्च प्रतिवेदन."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import user_perms
from biddings.views_workflow import plain, to_dec
from usercommittee.workflow import approver_identity

from . import bank as bk
from . import mtef as mt
from .models import BankProject, Budget_Type, FiscalYear, MTEFLine, MTEFPlan, Project, Project_Type
from .views_bank import VIEW, _xlsx, _xlsx_response

ZERO = Decimal("0")


def _log(request, plan, action, note):
    from usercommittee.models import ActivityLog

    try:
        ActivityLog.objects.create(user=request.user, action=action, model_name="MTEF", object_id=plan.id,
                                   object_repr=str(plan)[:250], note=note[:250])
    except Exception:
        pass


@login_required
def mtef_list(request):
    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    plans = list(MTEFPlan.objects.select_related("start_fy"))
    used = {p.start_fy_id for p in plans}
    fys = [f for f in FiscalYear.objects.order_by("-name") if f.id not in used]
    if request.method == "POST":
        if "mtef.manage" not in P:
            return redirect("unauthorized")
        fy = FiscalYear.objects.filter(id=request.POST.get("start_fy") or 0).first()
        name = (request.POST.get("new_fy") or "").strip()
        if fy is None and name:
            import re
            if not re.fullmatch(r"\d{4}/\d{2}", name.translate(str.maketrans("०१२३४५६७८९", "0123456789"))):
                messages.error(request, "आ.व. को नाम २०८४/८५ जस्तो लेख्नुहोस्।")
                return redirect("mtef_list")
            fy = FiscalYear.objects.filter(name=name).first() or FiscalYear.objects.create(name=name)
        if fy is None:
            messages.error(request, "पहिलो वर्ष (आ.व.) छान्नुहोस्।")
            return redirect("mtef_list")
        if MTEFPlan.objects.filter(start_fy=fy).exists():
            messages.error(request, f"आ.व. {fy.name} बाट सुरु हुने MTEF पहिले नै छ।")
            return redirect("mtef_list")
        plan = mt.create_plan(fy, request.user)
        _log(request, plan, "create", "नयाँ MTEF")
        messages.success(request, f"MTEF {plan} बन्यो — {bk.np(plan.lines.count())} आयोजना (प्रतिबद्धता र अघिल्लो MTEF बाट) सहित।"
                                  " स्रोत सीमा जाँचेर नयाँ आयोजना बैंकबाट थप्नुहोस्।")
        if request.POST.get("next") == "revenue":          # स्रोत अनुमान पेजबाट सुरु गरिएको — त्यहीँ फर्काउने
            return redirect("mtef_revenue", plan.id)
        return redirect("mtef_plan", plan.id)
    rows = [{"p": p, "t": mt.totals(p), "n": p.lines.count()} for p in plans]
    return render(request, "projects/mtef_list.html", {"rows": rows, "fys": fys, "can": "mtef.manage" in P,
                                                       "suggest": mt.next_name(plans[0].start_fy.name) if plans else ""})


@login_required
def mtef_plan(request, pid):
    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    plan = get_object_or_404(MTEFPlan.objects.select_related("start_fy"), id=pid)
    can = "mtef.manage" in P and plan.status == "draft"
    mt.ensure_ceilings(plan)
    if request.method == "POST":
        if not can:
            messages.error(request, "स्वीकृत MTEF बदल्न मिल्दैन (वा अनुमति छैन)।")
            return redirect("mtef_plan", plan.id)
        act = request.POST.get("act")
        if act == "ceilings":
            bad = 0
            for c in plan.ceilings.all():
                for k in ("y1", "y2", "y3"):
                    v = to_dec(request.POST.get(f"c{c.id}_{k}"))
                    if v in ("bad",) or (v is not None and v < 0):
                        bad += 1
                        continue
                    setattr(c, k, v or ZERO)
                c.save()
            plan.resource_ref = (request.POST.get("resource_ref") or "").strip()[:200]
            plan.save(update_fields=["resource_ref"])
            (messages.warning if bad else messages.success)(request, "स्रोत सीमा सुरक्षित भयो।" + (f" {bk.np(bad)} कोठा अङ्क नभएकाले छोडियो।" if bad else ""))
            _log(request, plan, "update", "स्रोत सीमा")
        elif act == "lines":
            bad, removed = 0, 0
            with transaction.atomic():
                for ln in plan.lines.select_related("bank"):
                    if request.POST.get(f"rm_{ln.id}"):
                        if ln.kind == "commitment" and Project.objects.filter(bank_project=ln.bank, fiscalyear=plan.start_fy).exists():
                            messages.error(request, f"{ln.bank.code} यस आ.व. को बजेटमा परिसकेको प्रतिबद्धता हो — हटाउन मिल्दैन।")
                            continue
                        ln.delete()
                        removed += 1
                        continue
                    for k in ("y1", "y2", "y3"):
                        v = to_dec(request.POST.get(f"l{ln.id}_{k}"))
                        if v == "bad" or (v is not None and v < 0):
                            bad += 1
                            continue
                        setattr(ln, k, v or ZERO)
                    ln.save()
            messages.success(request, "वर्षगत रकम सुरक्षित भयो।" + (f" {bk.np(removed)} हटाइयो।" if removed else "") +
                             (f" {bk.np(bad)} कोठा अङ्क नभएकाले छोडियो।" if bad else ""))
            _log(request, plan, "update", f"वर्षगत रकम, हटाइयो {removed}")
        elif act == "refresh":
            n = mt.refresh_commitments(plan)
            messages.success(request, f"प्रतिबद्धता अद्यावधिक भयो — {bk.np(n)} आयोजना।")
        elif act == "note":
            plan.note = (request.POST.get("note") or "").strip()
            plan.save(update_fields=["note"])
        return redirect(reverse("mtef_plan", args=[plan.id]) + (f"#{act}" if act else ""))
    lines = list(plan.lines.select_related("bank__ward", "bank__sector", "bank__pillar"))
    ex = bk.exec_map([ln.bank for ln in lines])
    problems, warnings = mt.checks(plan, ex)
    dim = request.GET.get("dim", "pillar")
    if dim not in dict(mt.DIMENSIONS):
        dim = "pillar"
    rows = []
    for ln in lines:
        e = ex.get(ln.bank_id, {})
        rows.append({"ln": ln, "b": ln.bank, "spent": e.get("spent", ZERO), "contract": e.get("contract", ZERO),
                     "progress": e.get("progress"), "status": e.get("status_label", "")})
    if request.GET.get("export") == "xlsx":
        return _export(plan, rows)
    ceilings = list(plan.ceilings.select_related("head"))
    return render(request, "projects/mtef_plan.html", {
        "plan": plan, "can": can, "P": P, "t": mt.totals(plan), "ceilings": ceilings, "sector_rows": mt.sector_rows(plan),
        "commit": [r for r in rows if r["ln"].kind == "commitment"], "new": [r for r in rows if r["ln"].kind == "new"],
        "problems": problems, "warnings": warnings, "dim": dim, "dims": mt.DIMENSIONS, "summary": mt.summary(lines, dim),
        "can_approve": "mtef.approve" in P, "can_send": "mtef.manage" in P and plan.status == "approved",
        "ready_n": BankProject.objects.filter(ready_at__isnull=False, hold="").exclude(mtef_lines__plan=plan).count(),
    })


def _export(plan, rows):
    y = plan.years
    head = ["बैंक नं.", "आयोजना", "वडा", "क्षेत्र", "प्रकार", "P1/P2", "स्तम्भ", "कार्यक्रम", "SDG", "लैङ्गिक", "जलवायु",
            "कुल लागत", f"आ.व. {y[0]}", f"आ.व. {y[1]}", f"आ.व. {y[2]}", "तीन वर्ष जम्मा", "खर्च (हालसम्म)", "प्रगति %"]
    data = []
    for r in rows:
        b, ln = r["b"], r["ln"]
        data.append([b.code, b.name, b.ward.name if b.ward else "", b.sector.name if b.sector else "", ln.get_kind_display(), b.priority,
                     b.pillar.name if b.pillar else "", b.program.name if b.program else "", b.sdg,
                     b.get_gender_display() if b.gender else "", b.get_climate_display() if b.climate else "", float(b.cost or 0),
                     float(ln.y1), float(ln.y2), float(ln.y3), float(ln.total), float(r["spent"] or 0), r["progress"]])
    wb = _xlsx("MTEF", head, data, [16, 40, 6, 16, 12, 6, 22, 22, 5, 12, 14, 14, 14, 14, 14, 14, 14, 8])
    ws = wb.create_sheet("स्रोत सीमा")
    ws.append(["स्रोत", f"आ.व. {y[0]}", f"आ.व. {y[1]}", f"आ.व. {y[2]}"])
    for c in plan.ceilings.select_related("head"):
        ws.append([c.head.name, float(c.y1), float(c.y2), float(c.y3)])
    for dim, label in mt.DIMENSIONS:
        ws = wb.create_sheet(label[:30].replace("/", "-"))
        ws.append([label, "सङ्ख्या", f"आ.व. {y[0]}", f"आ.व. {y[1]}", f"आ.व. {y[2]}", "पहिलो वर्ष %"])
        for g in mt.summary([r["ln"] for r in rows], dim):
            ws.append([g["label"], g["n"], float(g["y1"]), float(g["y2"]), float(g["y3"]), g["pct"]])
    return _xlsx_response(wb, f"MTEF-{plan.start_fy.name.replace('/', '-')}.xlsx")


@login_required
def mtef_add(request, pid):
    """modal: तयार आयोजना (अङ्कअनुसार) छानेर थप्ने."""
    P = user_perms(request.user)
    plan = get_object_or_404(MTEFPlan, id=pid)
    if "mtef.manage" not in P or plan.status != "draft":
        return HttpResponse("<div class='p-6 text-sm text-rose-600'>अनुमति छैन वा MTEF स्वीकृत भइसकेको छ।</div>", status=403)
    qs = (bk.queryset().filter(ready_at__isnull=False, hold="").exclude(mtef_lines__plan=plan)
          .order_by("-score", "ward__name"))
    if request.method == "POST":
        ids = [int(x) for x in request.POST.getlist("bid") if x.isdigit()]
        if not ids:
            return HttpResponse("<p class='text-sm text-rose-600'>कम्तीमा एउटा आयोजना छान्नुहोस्।</p>", status=400)
        ex = bk.exec_map(list(qs.filter(id__in=ids)))
        n = 0
        with transaction.atomic():
            for b in qs.filter(id__in=ids):
                mt.add_bank(plan, b, ex.get(b.id, {}).get("spent", ZERO) or ZERO)
                n += 1
        _log(request, plan, "update", f"बैंकबाट {n} आयोजना थपियो")
        messages.success(request, f"{bk.np(n)} आयोजना MTEF मा थपियो — वर्षगत रकम जाँच्नुहोस्।")
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = reverse("mtef_plan", args=[plan.id]) + "#new"
        return resp
    t = mt.totals(plan)
    return render(request, "projects/_mtef_add.html", {"plan": plan, "banks": list(qs[:300]), "t": t, "max_score": bk.max_score()})


@login_required
def bank_to_mtef(request, bid):
    """modal (आयोजना बैंकको चरण ६ बाट): यो आयोजना मस्यौदा MTEF मा थप्ने."""
    P = user_perms(request.user)
    if "mtef.manage" not in P:
        return HttpResponse("<div class='p-6 text-sm text-rose-600'>MTEF मा थप्ने अनुमति योजना शाखामा छ।</div>", status=403)
    b = get_object_or_404(bk.queryset(), id=bid)
    drafts = list(MTEFPlan.objects.filter(status="draft").select_related("start_fy").order_by("-start_fy__name"))
    problems = []
    if not b.ready_at or b.hold:
        problems.append("आयोजना “तयार” घोषणा भएपछि मात्र MTEF मा थप्न मिल्छ।")
    if not drafts:
        problems.append("मस्यौदा MTEF छैन — पहिले MTEF बनाउनुहोस् (स्वीकृत भइसकेको MTEF मा थप्न मिल्दैन)।")
    spent = bk.exec_map([b]).get(b.id, {}).get("spent", ZERO) or ZERO
    if request.method == "POST":
        plan = next((x for x in drafts if str(x.id) == request.POST.get("plan")), None)
        if problems or plan is None:
            return HttpResponse(f"<p class='text-sm text-rose-600'>{' '.join(problems) or 'MTEF छान्नुहोस्।'}</p>", status=400)
        if MTEFLine.objects.filter(plan=plan, bank=b).exists():
            return HttpResponse("<p class='text-sm text-rose-600'>यो आयोजना त्यो MTEF मा पहिले नै छ।</p>", status=400)
        mt.add_bank(plan, b, spent)
        _log(request, plan, "update", f"{b.code} थपियो")
        messages.success(request, f"{b.code} MTEF {plan} मा थपियो — वर्षगत रकम MTEF पेजमा जाँच्नुहोस्।")
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = reverse("mtef_plan", args=[plan.id]) + "#new"
        return resp
    return render(request, "projects/_bank_to_mtef.html", {"b": b, "drafts": drafts, "problems": problems,
                                                           "phase": mt.phase(b, spent), "spent": spent,
                                                           "can_create": not drafts})


STAGES = [("recommend", "draft", "recommended", "mtef.manage", "बजेट तथा कार्यक्रम तर्जुमा समितिको सिफारिस", "committee"),
          ("executive", "recommended", "executive", "mtef.approve", "कार्यपालिकाको स्वीकृति", "exec"),
          ("assembly", "executive", "approved", "mtef.approve", "सभाबाट पारित", "approval")]


@login_required
def mtef_approve(request, pid):
    """तीन निर्णय क्रमैसँग: समिति सिफारिस (योजना शाखाले प्रविष्ट) → कार्यपालिका → सभा (प्रमुख प्रशासकीय अधिकृतले प्रविष्ट)."""
    P = user_perms(request.user)
    plan = get_object_or_404(MTEFPlan, id=pid)
    if not P & VIEW:
        return HttpResponse("<div class='p-6 text-sm text-rose-600'>अनुमति छैन।</div>", status=403)
    problems, warnings = mt.checks(plan)
    if not plan.resource_ref:
        problems.append("स्रोत अनुमान तथा बजेट सीमा निर्धारण समितिको निर्णय (स्रोत सीमा खण्डमा) लेखिएको छैन।")
    nxt = next((x for x in STAGES if x[1] == plan.status), None)
    if request.method == "POST":
        def fail(msg, code=400):
            return HttpResponse(f"<p class='text-sm text-rose-600'>{msg}</p>", status=code)
        d = request.POST.get("decision")
        ref = (request.POST.get("ref") or "").strip()[:200]
        if d == "unlock":
            if "mtef.approve" not in P:
                return fail("लक खोल्ने अधिकार छैन।", 403)
            if plan.status == "draft":
                return fail("पहिले नै मस्यौदामा छ।")
            reason = (request.POST.get("reason") or "").strip()
            if not reason:
                return fail("मस्यौदामा फर्काउनुको कारण लेख्नुहोस्।")
            plan.status, plan.approved_at, plan.committee_at, plan.exec_at = "draft", None, None, None
            plan.note = (plan.note + f"\nमस्यौदामा फर्काइयो: {reason}").strip()
            plan.save()
            _log(request, plan, "update", f"मस्यौदामा फर्काइयो: {reason}")
            messages.warning(request, f"MTEF {plan} मस्यौदामा फर्कियो — सच्याएर फेरि समितिबाट सिफारिस गराउनुहोस्।")
        else:
            st = next((x for x in STAGES if x[0] == d), None)
            if st is None or st is not nxt:
                return fail("यो निर्णय अहिले गर्न मिल्दैन — क्रम: समिति सिफारिस → कार्यपालिका → सभा।")
            key, _frm, to, perm, label, field = st
            if perm not in P:
                return fail(f"“{label}” प्रविष्ट गर्ने अधिकार तपाईंलाई छैन।", 403)
            if key == "recommend" and problems:
                return fail(" ".join(problems))
            if not ref:
                return fail(f"{label} — निर्णय नं. / मिति लेख्नुहोस्।")
            now = timezone.now()
            if field == "committee":
                plan.committee_ref, plan.committee_at = ref, now
            elif field == "exec":
                plan.exec_ref, plan.exec_at = ref, now
            else:
                name, post = approver_identity(request.user, "municipal")
                plan.approval_ref, plan.approved_at, plan.approver_name = ref, now, f"{name} ({post})"
            plan.status = to
            plan.save()
            _log(request, plan, "update", f"{label}: {ref}")
            messages.success(request, {"recommended": "समितिको सिफारिस प्रविष्ट भयो — अब कार्यपालिकाको स्वीकृति।",
                                       "executive": "कार्यपालिकाको स्वीकृति प्रविष्ट भयो — अब सभामा पेश गरी पारित गराउनुहोस्।",
                                       "approved": "सभाबाट पारित — MTEF लक भयो। अब “वार्षिक बजेटमा पठाउने”।"}[to])
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = reverse("mtef_plan", args=[plan.id])
        return resp
    steps = [{"label": lbl, "ref": getattr(plan, {"committee": "committee_ref", "exec": "exec_ref", "approval": "approval_ref"}[f]),
              "at": getattr(plan, {"committee": "committee_at", "exec": "exec_at", "approval": "approved_at"}[f]),
              "done": [x[0] for x in STAGES].index(k) < ([x[0] for x in STAGES].index(nxt[0]) if nxt else 3), "next": nxt and nxt[0] == k}
             for k, _a, _b, _p, lbl, f in STAGES]
    return render(request, "projects/_mtef_approve.html", {
        "plan": plan, "problems": problems, "warnings": warnings, "t": mt.totals(plan), "steps": steps, "nxt": nxt,
        "can_next": bool(nxt) and nxt[3] in P, "can_unlock": "mtef.approve" in P and plan.status != "draft",
    })


@login_required
def mtef_send(request, pid):
    P = user_perms(request.user)
    plan = get_object_or_404(MTEFPlan.objects.select_related("start_fy"), id=pid)
    if "mtef.manage" not in P:
        return redirect("unauthorized")
    if plan.status != "approved":
        messages.error(request, "स्वीकृत MTEF मात्र वार्षिक बजेटमा पठाउन मिल्छ।")
        return redirect("mtef_plan", plan.id)
    rows = mt.send_preview(plan)
    if request.method == "POST" and request.POST.get("confirm") == "1":
        bt = Budget_Type.objects.filter(id=request.POST.get("budget_type") or 0).first()
        pt = Project_Type.objects.filter(id=request.POST.get("project_type") or 0).first()
        try:
            c = mt.send(plan, bt, pt, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("mtef_send", plan.id)
        _log(request, plan, "update", f"बजेटमा: नयाँ {c['create']}, अद्यावधिक {c['update']}")
        messages.success(request, f"आ.व. {plan.start_fy.name} को बजेटमा {bk.np(c['create'])} नयाँ योजना बने, "
                                  f"{bk.np(c['update'])} को बजेट अद्यावधिक भयो। अब खरिद विधि तोकेर अहिलेकै प्रक्रियामा अघि बढाउनुहोस्।")
        return redirect("mtef_plan", plan.id)
    counts = {k: sum(1 for r in rows if r["action"] == k) for k in ("create", "update", "same", "other_fy")}
    return render(request, "projects/mtef_send.html", {
        "plan": plan, "rows": rows, "counts": counts, "budget_types": Budget_Type.objects.all(),
        "project_types": Project_Type.objects.all(), "total": sum((r["ln"].y1 for r in rows), ZERO),
    })


# ---------------------------------------------------------------- प्रतिवेदन
@login_required
def plan_report(request):
    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    from usercommittee.reports import all_rows

    fys = list(FiscalYear.objects.order_by("-name"))
    sel = request.GET.get("fiscalyear", "")
    fy = next((f for f in fys if str(f.id) == sel), None)
    if fy is None:
        planned = MTEFPlan.objects.order_by("-start_fy__name").first()
        fy = planned.start_fy if planned else (fys[0] if fys else None)
    dim = request.GET.get("dim", "pillar")
    if dim not in dict(mt.DIMENSIONS):
        dim = "pillar"
    groups = {}

    def g_for(b):
        if b is None:
            k = (1000, "बैंकसँग नजोडिएको")
        else:
            k = mt._dim_key(b, dim)
            if not k[1]:
                k = (999, "कोड नभएको")
        return groups.setdefault(k, {"label": k[1], "plan": ZERO, "n_plan": 0, "budget": ZERO, "n": 0, "contract": ZERO, "spent": ZERO})

    plan = MTEFPlan.objects.filter(start_fy=fy).first() if fy else None
    if plan:
        for ln in plan.lines.select_related("bank__ward", "bank__sector", "bank__pillar"):
            g = g_for(ln.bank)
            g["plan"] += ln.y1
            g["n_plan"] += 1
    projects = list(Project.objects.filter(fiscalyear=fy).select_related("bank_project__ward", "bank_project__sector",
                                                                            "bank_project__pillar")) if fy else []
    rows_exec = {r["p"].id: r for r in all_rows([p.id for p in projects])} if projects else {}
    for p in projects:
        g = g_for(p.bank_project)
        g["budget"] += Decimal(str(p.budget or 0)) - Decimal(p.spent_before or 0)
        g["n"] += 1
        r = rows_exec.get(p.id)
        if r:
            g["contract"] += Decimal(r.get("contract") or 0)
            g["spent"] += Decimal(r["gross"] or 0)
    rows = [groups[k] for k in sorted(groups)]
    tot = {k: sum((r[k] for r in rows), ZERO) for k in ("plan", "budget", "contract", "spent")}
    tot["n"] = sum(r["n"] for r in rows)
    for r in rows:
        r["spent_pct"] = int(r["spent"] * 100 / r["budget"]) if r["budget"] else None
    if request.GET.get("export") == "xlsx":
        label = dict(mt.DIMENSIONS)[dim]
        wb = _xlsx("योजना vs खर्च", [label, "MTEF (पहिलो वर्ष)", "योजना सङ्ख्या", "वार्षिक बजेट", "सम्झौता", "खर्च", "खर्च %"],
                   [[r["label"], float(r["plan"]), r["n"], float(r["budget"]), float(r["contract"]), float(r["spent"]), r["spent_pct"]]
                    for r in rows], [30, 16, 10, 16, 16, 16, 8])
        return _xlsx_response(wb, f"plan-vs-actual-{fy.name.replace('/', '-') if fy else ''}.xlsx")
    # बैंक अवस्था (वडाअनुसार)
    from accounts.models import Ward

    ward_rows = []
    lined = set(MTEFLine.objects.values_list("bank_id", flat=True))
    funded = set(Project.objects.filter(bank_project__isnull=False).values_list("bank_project_id", flat=True))
    for w in Ward.objects.all():
        bs = list(BankProject.objects.filter(ward=w))
        if not bs:
            continue
        ward_rows.append({"w": w, "n": len(bs), "cost": sum((Decimal(b.cost or 0) for b in bs), ZERO),
                          "ready": sum(1 for b in bs if b.ready_at and not b.hold and b.id not in lined),
                          "mtef": sum(1 for b in bs if b.id in lined), "funded": sum(1 for b in bs if b.id in funded),
                          "held": sum(1 for b in bs if b.hold)})
    return render(request, "projects/plan_report.html", {
        "fy": fy, "fiscal_years": fys, "dim": dim, "dims": mt.DIMENSIONS, "rows": rows, "tot": tot, "plan": plan,
        "ward_rows": ward_rows, "outside": sum(1 for p in projects if p.bank_project_id is None),
    })


# ---------------------------------------------------------------- योजना तर्जुमा चक्र (एकै पेजमा ९ चरण)
@login_required
def plan_cycle(request):
    """वार्षिक योजना तर्जुमाका ७ चरण (स्थानीय तहको योजना तथा बजेट तर्जुमा क्रम) र त्यसपछि बजेट र कार्यान्वयन."""
    from .models import BankProject, PlanSettings

    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    plans = list(MTEFPlan.objects.select_related("start_fy"))
    cur = mt.current_plan()
    plan = next((x for x in plans if str(x.id) == request.GET.get("plan", "")), cur)
    from .views_bank import _user_wards

    wards = _user_wards(request.user)
    qs = BankProject.objects.filter(hold="")
    if wards is not None:
        qs = qs.filter(ward_id__in=wards)
    ward_on = PlanSettings.get().ward_priority
    total = qs.count()
    n_ward_src = qs.filter(source="ward").count()
    tech_q = qs.filter(tech_at__isnull=True, ready_at__isnull=True)
    if ward_on:
        tech_q = tech_q.exclude(source="ward", ward_approved_at__isnull=True)
    n_tech = tech_q.count()
    n_score = qs.filter(tech_at__isnull=False, ready_at__isnull=True).count()
    n_ready = qs.filter(ready_at__isnull=False).exclude(mtef_lines__isnull=False).count()
    n_late = qs.filter(entry_step__isnull=False, projects__isnull=True).count()
    t = mt.totals(plan) if plan else None
    lines = plan.lines.count() if plan else 0
    n_new = plan.lines.filter(kind="new").count() if plan else 0
    cap_ok = bool(t) and any(t[k]["cap"] for k in t) and all(t[k]["used"] <= t[k]["cap"] for k in t if t[k]["cap"])
    ward_rows, n_sub = [], 0
    if plan:
        for wc in sorted(mt.ward_ceilings(plan).values(), key=lambda w: w.ward_id):
            used = mt.ward_used(wc.ward_id)
            n_sub += 1 if wc.submitted_at else 0
            ward_rows.append({"wc": wc, "used": used, "over": bool(wc.amount) and used > wc.amount,
                              "pct": min(int(used * 100 / wc.amount), 100) if wc.amount else None})
    exe = {"n": 0, "running": 0, "done": 0, "spent": ZERO}
    if plan and plan.sent_at:
        from usercommittee.reports import all_rows

        ps = list(Project.objects.filter(fiscalyear=plan.start_fy, bank_project__isnull=False))
        for r in all_rows([p.id for p in ps]):
            exe["n"] += 1
            exe["spent"] += Decimal(r["gross"] or 0)
            exe["done" if r["status"] in ("completed", "paid") else "running"] += 1

    srows_c = mt.sector_rows(plan) if plan else []

    def btn(label, url, perm=None, primary=True, modal=False):
        if perm and not (P & ({perm} if isinstance(perm, str) else set(perm))):
            return None
        return {"label": label, "url": url, "primary": primary, "modal": modal}

    plan_url = reverse("mtef_plan", args=[plan.id]) if plan else reverse("mtef_list")
    rev_url = reverse("mtef_revenue", args=[plan.id]) if plan else reverse("mtef_list")
    st = plan.status if plan else None
    approve_url = reverse("mtef_approve", args=[plan.id]) if plan else ""
    stages = [
        {"title": "स्रोत अनुमान र बजेट सीमा", "who": "स्रोत अनुमान तथा बजेट सीमा निर्धारण समिति", "where": "स्रोत अनुमान",
         "what": "आगामी ३ वर्षको आम्दानी स्रोतअनुसार अनुमान, चालु खर्च घटाई पुँजीगत सीमा, र वडा तथा पालिका स्तरीय सीमा तोक्ने।",
         "metric": ((f"पुँजीगत सीमा रु. {bk.money(t['y1']['cap'])}" if t["y1"]["cap"] else "स्रोत अनुमान बाँकी") +
                    (f" · निर्णय: {plan.resource_ref}" if plan.resource_ref else " · समितिको निर्णय बाँकी")) if plan else "पहिले MTEF बनाउनुहोस्",
         "buttons": [btn("स्रोत अनुमान र सीमा", rev_url, VIEW)] if plan else [btn("नयाँ MTEF बनाउनुहोस्", reverse("mtef_list"), "mtef.manage")]},
        {"title": "टोल / बस्ती स्तरमा आयोजना छनौट", "who": "टोल / बस्ती भेला, वडा कार्यालय", "where": "आयोजना बैंक",
         "what": "टोल र बस्ती भेलाले मागेका आयोजना आयोजना बैंकमा दर्ता गर्ने। अघिल्लो वर्ष नपरेका तयार आयोजना बैंकमै रहन्छन्।",
         "metric": f"बैंकमा {bk.np(total)} आयोजना ({bk.np(n_ward_src)} वडाबाट)",
         "buttons": [btn("नयाँ प्रस्ताव", reverse("bank_add"), ("bank.propose", "bank.manage")),
                     btn("Excel बाट आयात", reverse("bank_import"), ("bank.propose", "bank.manage"), False)]},
        {"title": "वडा स्तरमा प्राथमिकीकरण", "who": "वडा समिति", "where": "वडा प्राथमिकता",
         "what": "वडाले आफ्नो सीमाभित्र प्रस्तावलाई १, २, ३… क्रम दिई पेश गर्ने। पेशपछि वडाको सूची लक हुन्छ।",
         "metric": (f"{bk.np(n_sub)} / {bk.np(len(ward_rows))} वडाले पेश गरे" if plan else "—") if ward_on else "सेटिङबाट बन्द",
         "wards": bool(ward_rows),
         "buttons": [btn("वडा प्राथमिकता", reverse("bank_ward_priority"), ("bank.ward_priority", "bank.view"))] if ward_on else []},
        {"title": "विषयगत समितिमा छलफल", "who": "विषयगत समिति, इन्जिनियर, योजना शाखा", "where": "आयोजना बैंक",
         "what": "प्राविधिक जाँच (DPR, जग्गा, वातावरण), १०० अङ्कमा मूल्याङ्कन, P1/P2 र SDG/लैङ्गिक/जलवायु कोड दिई “तयार” घोषणा। पालिका स्तरीय आयोजना यहीँ थपिन्छन्।",
         "metric": (f"{bk.np(sum(1 for r in srows_c if r['sc'].submitted_at))} / {bk.np(len(srows_c))} शाखाले पेश गरे · " if srows_c else "") +
                   f"जाँच बाँकी {bk.np(n_tech)} · अङ्क बाँकी {bk.np(n_score)} · तयार {bk.np(n_ready)}",
         "sectors": True,
         "buttons": [btn("जाँच / अङ्क बाँकी सूची", reverse("bank_list") + "?status=mine", ("bank.screen", "bank.manage"))]},
        {"title": "बजेट तथा कार्यक्रम तर्जुमा", "who": "बजेट तथा कार्यक्रम तर्जुमा समिति", "where": "MTEF",
         "what": "क्रमागत आयोजना पहिले, त्यसपछि तयार आयोजना अङ्कअनुसार ३ वर्षमा सीमाभित्र बाँड्ने। पहिलो वर्ष = वार्षिक बजेट मस्यौदा। समितिको सिफारिस प्रविष्ट।",
         "metric": ((f"{bk.np(lines)} आयोजना ({bk.np(n_new)} नयाँ) · पहिलो वर्ष रु. {bk.money(t['y1']['used'])}" +
                     ("" if cap_ok else " · ⚠ सीमा नाघेको / सीमा छैन")) if plan else "—") +
                   (f" · पछि थपिएका {bk.np(n_late)}" if n_late else ""),
         "buttons": [btn("MTEF", plan_url + "#new", VIEW)] +
                    ([btn("समितिको सिफारिस", approve_url, "mtef.manage", modal=True)] if plan and st == "draft" else [])},
        {"title": "कार्यपालिकाबाट स्वीकृति", "who": "गाउँ / नगर कार्यपालिका", "where": "MTEF → निर्णय",
         "what": "कार्यपालिकाको निर्णय नं. / मिति प्रविष्ट गर्ने। यसपछि मस्यौदा लक हुन्छ।",
         "metric": (f"निर्णय: {plan.exec_ref}" if plan and plan.exec_ref else "बाँकी"),
         "buttons": [btn("कार्यपालिकाको स्वीकृति", approve_url, "mtef.approve", modal=True)] if plan and st == "recommended" else []},
        {"title": "सभाबाट स्वीकृति", "who": "गाउँ / नगर सभा (असार १० भित्र पेश)", "where": "MTEF → निर्णय",
         "what": "सभाले पारित गरेको निर्णय प्रविष्ट गर्ने। त्यसपछि पहिलो वर्षका आयोजना वार्षिक बजेटमा पठाउने।",
         "metric": (f"पारित: {plan.approval_ref}" if plan and plan.approval_ref else "बाँकी"),
         "buttons": ([btn("सभाबाट पारित", approve_url, "mtef.approve", modal=True)] if plan and st == "executive" else []) +
                    ([btn("वार्षिक बजेटमा पठाउनुहोस्", reverse("mtef_send", args=[plan.id]), "mtef.manage")]
                     if plan and st == "approved" and not plan.sent_at else [])},
    ]
    after = {"title": "वार्षिक बजेट र कार्यान्वयन", "what": "पठाएपछि पहिलो वर्षका आयोजना आयोजना अभिलेखिकरणमा आफैँ बन्छन् (बैंक नम्बरसँग जोडिएर) — त्यसपछि खरिद, सम्झौता र भुक्तानी अहिलेकै पेजबाट।",
             "metric": (f"{bk.np(exe['n'])} योजना · चालु {bk.np(exe['running'])} · सम्पन्न {bk.np(exe['done'])} · खर्च रु. {bk.money(exe['spent'])}"
                        if plan and plan.sent_at else ("पठाउन बाँकी" if st == "approved" else "—")),
             "buttons": [b for b in [btn("योजना vs खर्च", reverse("plan_report") + (f"?fiscalyear={plan.start_fy_id}" if plan else ""), None, False)] if b]}
    WHEN = ['माघ – फागुन', 'फागुन – चैत', 'चैत', 'वैशाख', 'वैशाख – जेठ', 'जेठ', 'असार १० भित्र']
    current = mt.cycle_step(plan) if plan else 1
    for i, s_ in enumerate(stages, 1):
        s_["n"] = i
        s_["when"] = WHEN[i - 1]
        s_["buttons"] = [b for b in s_["buttons"] if b]
        s_["state"] = "done" if i < current else ("current" if i == current else "pending")
    return render(request, "projects/plan_cycle.html", {
        "stages": stages, "after": after, "plan": plan, "plans": plans, "current": current, "ward_rows": ward_rows,
        "sector_rows": srows_c,
        "after_state": "done" if plan and plan.sent_at else ("current" if current == 8 else "pending"),
    })


# ---------------------------------------------------------------- स्रोत अनुमान (तर्जुमा चक्र चरण १)
@login_required
def revenue_current(request):
    """साइडबारको “स्रोत अनुमान” — अहिलेको MTEF को स्रोत अनुमान पेज."""
    if not user_perms(request.user) & VIEW:
        return redirect("unauthorized")
    plan = mt.current_plan()
    if plan is None:
        messages.info(request, "स्रोत अनुमान MTEF अनुसार हुन्छ — पहिले आगामी आ.व. बाट सुरु हुने MTEF बनाउनुहोस्।")
        return redirect("mtef_list")
    return redirect("mtef_revenue", plan.id)


@login_required
def mtef_revenue(request, pid):
    """स्रोत अनुमान तथा बजेट सीमा: स्रोतअनुसार ३ वर्षको अनुमान → चालु खर्च घटाई पुँजीगत सीमा → वडा सीमा."""
    from .models import PlanLiability, ResourceHead

    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    plan = get_object_or_404(MTEFPlan.objects.select_related("start_fy"), id=pid)
    can = "mtef.manage" in P and plan.status == "draft"
    mt.ensure_ceilings(plan)
    wcs = mt.ward_ceilings(plan)
    if request.method == "POST":
        if not can:
            messages.error(request, "स्वीकृत भइसकेको MTEF को स्रोत अनुमान बदल्न मिल्दैन (वा अनुमति छैन)।")
            return redirect("mtef_revenue", plan.id)
        act = request.POST.get("act")
        bad = 0

        def amt(name):
            nonlocal bad
            v = to_dec(request.POST.get(name))
            if v == "bad" or (v is not None and v < 0):
                bad += 1
                return None
            return v or ZERO

        if act == "forecast":
            with transaction.atomic():
                for c in plan.ceilings.all():
                    for k in ("actual", "revised", "y1", "y2", "y3"):
                        v = amt(f"c{c.id}_{k}")
                        if v is not None:
                            setattr(c, k, v)
                    c.basis = (request.POST.get(f"c{c.id}_basis") or "").strip()[:150]
                    c.save()
                plan.resource_ref = (request.POST.get("resource_ref") or "").strip()[:200]
                plan.save(update_fields=["resource_ref"])
            _log(request, plan, "update", "स्रोत अनुमान")
            (messages.warning if bad else messages.success)(
                request, "स्रोत अनुमान सुरक्षित भयो।" + (f" {bk.np(bad)} कोठामा अङ्क नभएकाले छोडियो।" if bad else ""))
        elif act in ("wards", "split"):
            if act == "split":
                total = amt("ward_total")
                n = len(wcs)
                if total is not None and n:
                    each = (total / n).quantize(Decimal("1"))
                    for wc in wcs.values():
                        if not wc.submitted_at:
                            wc.amount = each
                            wc.save(update_fields=["amount"])
            else:
                for wc in wcs.values():
                    v = amt(f"w{wc.id}")
                    if v is not None and not wc.submitted_at:
                        wc.amount = v
                        wc.save(update_fields=["amount"])
            _log(request, plan, "update", "वडा सीमा")
            (messages.warning if bad else messages.success)(
                request, "वडा सीमा सुरक्षित भयो — वडाहरूले यही सीमाभित्र प्राथमिकता तोक्छन्।" + (f" {bk.np(bad)} कोठा छोडियो।" if bad else ""))
        elif act == "liabilities":
            with transaction.atomic():
                for k in ("y1", "y2", "y3"):
                    v = amt(f"rec_{k}")
                    if v is not None:
                        setattr(plan, f"recurrent_{k}", v)
                plan.save(update_fields=["recurrent_y1", "recurrent_y2", "recurrent_y3"])
                for kind, x in mt.liabilities(plan).items():
                    for k in ("y1", "y2", "y3"):
                        v = amt(f"l_{kind}_{k}")
                        if v is not None:
                            setattr(x, k, v)
                    x.note = (request.POST.get(f"l_{kind}_note") or "").strip()[:200]
                    x.save()
            _log(request, plan, "update", "दायित्व")
            (messages.warning if bad else messages.success)(
                request, "दायित्व सुरक्षित भयो — बाँकी रकम मात्र वडा र शाखामा बाँडिन्छ।" + (f" {bk.np(bad)} कोठा छोडियो।" if bad else ""))
        elif act == "sectors":
            for sc in mt.sector_ceilings(plan).values():
                v = amt(f"s{sc.id}")
                if v is not None:
                    sc.amount = v
                sc.note = (request.POST.get(f"s{sc.id}_note") or "").strip()[:150]
                sc.save(update_fields=["amount", "note"])
            _log(request, plan, "update", "विषयगत क्षेत्र सीमा")
            (messages.warning if bad else messages.success)(
                request, "विषयगत क्षेत्र / शाखा सीमा सुरक्षित भयो — विषयगत समितिले यही सीमाभित्र आयोजना सिफारिस गर्छ।" + (f" {bk.np(bad)} कोठा छोडियो।" if bad else ""))
        elif act == "unlock_sector":
            sc = next((x for x in mt.sector_ceilings(plan).values() if str(x.id) == request.POST.get("sc")), None)
            reason = (request.POST.get("reason") or "").strip()
            if sc and reason:
                sc.submitted_at, sc.submitted_by = None, ""
                sc.save(update_fields=["submitted_at", "submitted_by"])
                _log(request, plan, "update", f"{sc.sector.name} शाखाको सूची फेरि खोलियो: {reason}")
                messages.warning(request, f"{sc.sector.name} शाखाको प्रस्ताव सूची फेरि खोलियो।")
            else:
                messages.error(request, "सूची खोल्दा कारण लेख्नुहोस्।")
        elif act == "unlock_ward":
            wc = next((w for w in wcs.values() if str(w.id) == request.POST.get("wc")), None)
            reason = (request.POST.get("reason") or "").strip()
            if wc and reason:
                wc.submitted_at, wc.submitted_by = None, ""
                wc.save(update_fields=["submitted_at", "submitted_by"])
                _log(request, plan, "update", f"वडा {wc.ward.name} को सूची फेरि खोलियो: {reason}")
                messages.warning(request, f"वडा {wc.ward.name} को प्राथमिकता सूची फेरि खोलियो।")
            else:
                messages.error(request, "सूची खोल्दा कारण लेख्नुहोस्।")
        return redirect(reverse("mtef_revenue", args=[plan.id]) + (f"#{act}" if act else ""))

    ceil = list(plan.ceilings.select_related("head"))
    keys = ("actual", "revised", "y1", "y2", "y3")
    groups = []
    for g, label in ResourceHead.GROUPS:
        rows = [c for c in ceil if c.head.group == g]
        if rows:
            groups.append({"label": label, "rows": rows,
                           "sum": {k: sum((bk.D(getattr(c, k)) for c in rows), ZERO) for k in keys}})
    total = {k: sum((bk.D(getattr(c, k)) for c in ceil), ZERO) for k in keys}
    t = mt.totals(plan)
    ward_rows = []
    for wc in sorted(wcs.values(), key=lambda w: (bk.D(w.ward.name.translate(str.maketrans("०१२३४५६७८९", "0123456789"))) if w.ward.name.translate(str.maketrans("०१२३४५६७८९", "0123456789")).isdigit() else 999, w.ward_id)):
        used = mt.ward_used(wc.ward_id)
        ward_rows.append({"wc": wc, "used": used, "over": bool(wc.amount) and used > wc.amount,
                          "pct": int(used * 100 / wc.amount) if wc.amount else None})
    ward_total = sum((bk.D(w.amount) for w in wcs.values()), ZERO)
    fresh = t["y1"]["cap"] - t["y1"]["commitment"]
    liab_rows = [(k, lbl, mt.liabilities(plan)[k]) for k, lbl in PlanLiability.KINDS]
    payable_sugg = mt.payable_suggestion(plan) if can else (ZERO, 0)
    srows = mt.sector_rows(plan)
    sector_total = sum((bk.D(r["sc"].amount) for r in srows), ZERO)
    # सम्पादन गर्न नमिल्दा कारण र उपाय देखाउने
    next_fy, lock_reason = "", ""
    if not can:
        if "mtef.manage" not in P:
            lock_reason = "perm"
        else:
            lock_reason = "status"
            import re
            m = re.fullmatch(r"(\d{4})/(\d{2})", plan.start_fy.name.translate(str.maketrans("०१२३४५६७८९", "0123456789")))
            newer = MTEFPlan.objects.filter(status="draft").exclude(id=plan.id).first()
            if newer:
                lock_reason, next_fy = "draft_exists", newer.id
            elif m:
                y = int(m.group(1)) + 1
                next_fy = f"{y}/{str(y + 1)[-2:]}"
    return render(request, "projects/mtef_revenue.html", {
        "lock_reason": lock_reason, "next_fy": next_fy, "liab_rows": liab_rows, "payable_sugg": payable_sugg,
        "liab_total": {k: t[k]["recurrent"] + t[k]["liab"] + t[k]["commitment"] for k in ("y1", "y2", "y3")},
        "avail": {k: t[k]["cap"] - t[k]["commitment"] for k in ("y1", "y2", "y3")},
        "sector_rows": srows, "sector_total": sector_total, "palika_left": fresh - ward_total - sector_total,
        "plan": plan, "can": can, "groups": groups, "total": total, "t": t, "ward_rows": ward_rows,
        "ward_total": ward_total, "fresh": fresh, "palika_share": fresh - ward_total,
        "submitted": sum(1 for w in wcs.values() if w.submitted_at), "n_wards": len(wcs),
        "growth": int((total["y1"] / total["revised"] - 1) * 100) if total["revised"] else None,
    })

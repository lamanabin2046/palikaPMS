"""आयोजना बैंक — सूची, चरण panel, प्रस्ताव / वडा प्राथमिकता / प्राविधिक जाँच / अङ्क र कोड / निर्णय,
Excel आयात-निर्यात, प्रोफाइल प्रिन्ट, बैंक बाहिरका योजना, कोड बाँकी, रूपान्तरण, सेटिङ र प्रतिवेदन.

चरणको नियम: projects/bank.py. MTEF: projects/views_mtef.py.
"""
import io
import os
from decimal import Decimal
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import Ward
from accounts.permissions import has_perm, user_perms
from biddings.views_workflow import F, Form, plain, to_dec
from usercommittee.workflow import approver_identity

from . import bank as bk
from . import mtef as mt
from .models import (BankDoc, BankProject, FiscalYear, MTEFLine, PlanPillar, PlanProgram, PlanSettings, Project,
                     ResourceHead, ScoreCriterion, Sector, SectorPlanDefault, SubSector)

VIEW = {"bank.view", "bank.propose", "bank.ward_priority", "bank.screen", "bank.manage", "mtef.manage", "mtef.approve"}
UPLOAD_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
UPLOAD_MAX = 10 * 1024 * 1024
STATUS_FILTERS = [("pending", "प्रक्रियामा (तयार हुन बाँकी)"), ("ready", "तयार — MTEF कुरेको"), ("mtef", "MTEF मा"),
                  ("running", "बजेट / कार्यान्वयनमा"), ("done", "सम्पन्न"), ("held", "स्थगित / अस्वीकृत"),
                  ("uncoded", "कोड बाँकी")]


def _user_wards(user):
    from usercommittee.views_workflow import _user_wards as uw

    return uw(user)


def to_bs_np(d):
    from biddings.views_workflow import to_bs

    return bk.np(to_bs(d))


def _similar(name, ward, sector, exclude_id=None, limit=3):
    """नाम मिल्दोजुल्दो (≥ ७५%) सक्रिय आयोजना — उही वडामा, वडा नभए उही क्षेत्रमा."""
    from difflib import SequenceMatcher

    def norm(x):
        return " ".join((x or "").replace("–", " ").replace("-", " ").split()).lower()

    qs = BankProject.objects.filter(hold="").exclude(id=exclude_id)
    if ward is not None:
        qs = qs.filter(ward=ward)
    elif sector is not None:
        qs = qs.filter(sector=sector, ward__isnull=True)
    else:
        return []
    n = norm(name)
    hits = []
    for b in qs.only("id", "code", "name")[:2000]:
        r = SequenceMatcher(None, n, norm(b.name)).ratio()
        if r >= 0.75:
            hits.append((r, b))
    hits.sort(key=lambda x: -x[0])
    return [b for _r, b in hits[:limit]]


def _user_sectors(user):
    """विषयगत शाखा (भूमिका ११) का क्षेत्र; अरूका लागि None (सबै)."""
    if getattr(user, "user_type", None) != "11":
        return None
    from .models import SectionStaff

    st = SectionStaff.objects.filter(admin=user).first()
    return list(st.sectors.values_list("id", flat=True)) if st else []


def _scoped(request):
    qs = bk.queryset()
    wards = _user_wards(request.user)
    if wards is not None:
        qs = qs.filter(ward_id__in=wards)
    sectors = _user_sectors(request.user)
    if sectors is not None:                     # शाखाले आफ्नो क्षेत्रका पालिका स्तरीय आयोजना मात्र देख्छ
        qs = qs.filter(sector_id__in=sectors).exclude(source="ward")
    return qs, wards


def _bank(request, bid):
    return get_object_or_404(_scoped(request)[0], id=bid)


def _guard(request, *perms):
    if not user_perms(request.user) & set(perms):
        messages.error(request, "यो काम गर्ने अनुमति तपाईंलाई छैन।")
        return redirect("bank_list")
    return None


def with_open(url, bid):
    from urllib.parse import parse_qsl, urlencode

    parts = urlparse(url)
    if parts.path.rstrip("/") != reverse("bank_list").rstrip("/"):
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "open"]
    q.append(("open", str(bid)))
    return parts.path + "?" + urlencode(q)


def _next(request, b=None):
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return with_open(reverse("bank_list"), b.id) if b and b.id else reverse("bank_list")


def _return_to(request):
    current = urlparse(request.headers.get("HX-Current-URL", ""))
    if current.path:
        return current.path + ("?" + current.query if current.query else "")
    return reverse("bank_list")


def _log(request, b, action, note):
    from usercommittee.models import ActivityLog

    try:
        ActivityLog.objects.create(user=request.user, action=action, model_name="आयोजना बैंक", object_id=b.id,
                                   object_repr=b.code[:250], project_name=b.name[:250], note=note[:250])
    except Exception:
        pass


def _cards(b):
    cards = [{"label": "बैंक नं.", "value": b.code, "sub": b.get_nature_display()},
             {"label": "लागत", "value": f"रु. {bk.money(b.cost)}", "sub": "इन्जिनियर अनुमान" if b.tech_cost else "प्रस्तावित", "brand": True}]
    if b.population:
        cards.append({"label": "लाभान्वित", "value": f"{bk.np(b.population)} जना", "sub": f"{bk.np(b.households or 0)} घरधुरी"})
    if b.score is not None:
        cards.append({"label": "अङ्क", "value": f"{bk.np(f'{b.score:g}')}/{bk.np(bk.max_score())}", "sub": b.priority})
    return cards


def _render_form(request, b, ctx):
    return render(request, "biddings/wf_form.html", {
        "module_label": bk.LABEL, "project": b, "cards": _cards(b) if b.id else [],
        "next": request.POST.get("next") or request.GET.get("next", ""), "back": _next(request, b),
    } | ctx)


def _uploads(request, mapping, errors):
    files = []
    for field, kind in mapping.items():
        f = request.FILES.get(field)
        if not f:
            continue
        if os.path.splitext(f.name)[1].lower() not in UPLOAD_EXT:
            errors.append(f"{f.name}: PDF, JPG वा PNG मात्र।")
        elif f.size > UPLOAD_MAX:
            errors.append(f"{f.name}: १० MB भन्दा ठूलो।")
        else:
            files.append((kind, f))
    return files


def _int(v, lo=0, hi=10 ** 9):
    """पूर्ण अङ्क: खाली → None, नमिले → "bad"."""
    v = to_dec(v)
    if v is None:
        return None
    if v == "bad" or v != int(v) or not (lo <= v <= hi):
        return "bad"
    return int(v)


def _exec_cached(banks):
    key = f"bankexec:{connection.schema_name}:{cache.get(f'mywork:ver:{connection.schema_name}', 0)}"
    data = cache.get(key)
    if data is None:
        data = bk.exec_map(list(bk.queryset()))
        cache.set(key, data, 300)
    return data


def _lines_by_bank(ids=None):
    out = {}
    qs = MTEFLine.objects.select_related("plan__start_fy")
    if ids is not None:
        qs = qs.filter(bank_id__in=ids)
    for ln in qs:
        out.setdefault(ln.bank_id, []).append(ln)
    return out


# ---------------------------------------------------------------- सूची र panel
@login_required
def bank_list(request):
    P = user_perms(request.user)
    if not P & VIEW:
        return redirect("unauthorized")
    qs, wards = _scoped(request)
    f = {k: (request.GET.get(k) or "").strip() for k in ("q", "ward", "sector", "status", "priority", "fiscalyear", "dpr", "land")}
    if f["q"]:
        qs = qs.filter(Q(name__icontains=f["q"]) | Q(code__icontains=f["q"]) | Q(location__icontains=f["q"])
                       | Q(requested_by__icontains=f["q"]))
    if f["ward"].isdigit():
        qs = qs.filter(ward_id=f["ward"])
    if f["sector"].isdigit():
        qs = qs.filter(sector_id=f["sector"])
    if f["priority"] in ("P1", "P2"):
        qs = qs.filter(priority=f["priority"])
    if f["fiscalyear"].isdigit():
        qs = qs.filter(fiscalyear_id=f["fiscalyear"])
    if f["dpr"]:
        qs = qs.filter(dpr=True)
    if f["land"]:
        qs = qs.filter(land=True)
    banks = list(qs)
    ex = _exec_cached(banks)
    lines = _lines_by_bank()
    rows = []
    counts = {"all": 0, "pending": 0, "ready": 0, "ready_cost": bk.ZERO, "mtef": 0, "running": 0, "done": 0, "held": 0,
              "mine": 0, "cost": bk.ZERO}
    my_keys = set()
    if "bank.ward_priority" in P:
        my_keys.add("ward")
    if "bank.screen" in P:
        my_keys.add("technical")
    if "bank.manage" in P:
        my_keys |= {"score", "ready"}
    for b in banks:
        steps, sm = bk.workflow(b, request.user, ex.get(b.id), lines.get(b.id, []))
        k = sm["current_key"]
        if sm["held"]:
            group = "held"
        elif sm["current"] is None:
            group = "done"
        elif sm["exec"].get("projects"):
            group = "running"
        elif k == "budget":
            group = "mtef"
        elif k == "mtef":
            group = "ready"
        else:
            group = "pending"
        mine = k in my_keys and not sm["held"]
        counts["all"] += 1
        counts[group] += 1
        counts["mine"] += mine
        counts["cost"] += bk.D(b.cost)
        if group == "ready":
            counts["ready_cost"] += bk.D(b.cost)
        st = f["status"]
        if st == "mine" and not mine:
            continue
        if st == "uncoded" and b.coded:
            continue
        if st in ("pending", "ready", "mtef", "running", "done", "held") and group != st:
            continue
        rows.append({"b": b, "cells": bk.row_cells(steps), "summary": sm, "group": group, "mine": mine})
    sort = request.GET.get("sort", "")
    if sort == "score":
        rows.sort(key=lambda r: -(r["b"].score or 0))
    elif sort == "cost":
        rows.sort(key=lambda r: -bk.D(r["b"].cost))
    elif sort == "ward":
        rows.sort(key=lambda r: (r["b"].ward.name if r["b"].ward else "", r["b"].ward_rank or 999))
    if request.GET.get("export") == "xlsx":
        return _export(rows)
    try:
        per_page = int(request.GET.get("per_page", 25))
    except ValueError:
        per_page = 25
    f["per_page"] = per_page if per_page in (10, 25, 50, 100) else 25
    page = Paginator(rows, f["per_page"]).get_page(request.GET.get("page"))
    page_rows = list(page.object_list)
    open_id = request.GET.get("open", "")
    open_id = int(open_id) if open_id.isdigit() else None
    if open_id and not any(r["b"].id == open_id for r in page_rows):
        if (b := _scoped(request)[0].filter(id=open_id).first()):
            steps, sm = bk.workflow(b, request.user, ex.get(b.id), lines.get(b.id, []))
            page_rows.insert(0, {"b": b, "cells": bk.row_cells(steps), "summary": sm, "group": "", "mine": False})
        else:
            open_id = None
    keep = request.GET.copy()
    keep.pop("page", None)
    keep.pop("open", None)
    return render(request, "projects/bank_list.html", {
        "page": page, "rows": page_rows, "open_id": open_id, "columns": bk.columns(), "counts": counts, "f": f, "sort": sort,
        "wards": Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all(),
        "sectors": Sector.objects.filter(is_active=True), "fiscal_years": FiscalYear.objects.order_by("-name"),
        "status_filters": STATUS_FILTERS, "keep_qs": keep.urlencode(), "P": P, "max_score": bk.max_score(),
    })


@login_required
def bank_detail(request, bid):
    if not user_perms(request.user) & VIEW:
        return HttpResponse("<div class='p-6 text-sm text-rose-600'>अनुमति छैन।</div>", status=403)
    b = _bank(request, bid)
    ex = bk.exec_map([b]).get(b.id)
    steps, summary = bk.workflow(b, request.user, ex)
    bk.with_next(steps, with_open(_return_to(request), b.id))
    crit = bk.criteria()
    score_rows = [{"c": c, "v": (b.scores or {}).get(str(c.id))} for c in crit]
    focus = request.GET.get("step")
    return render(request, "projects/_bank_detail.html", {
        "b": b, "steps": steps, "summary": summary, "docs": list(b.docs.all()), "score_rows": score_rows,
        "checks": [(lbl, getattr(b, k)) for k, lbl in bk.CHECKS], "max_score": bk.max_score(),
        "focus": int(focus) if focus and focus.isdigit() else summary["current"],
        "lines": sorted(summary["lines"], key=lambda x: x.plan.start_fy.name),
    })


# ---------------------------------------------------------------- १. प्रस्ताव
def _proposal_form(request, b):
    is_new = b.id is None
    errors = []
    wards = _user_wards(request.user)
    my_sectors = _user_sectors(request.user)
    if request.method == "POST" and my_sectors is not None:
        request.POST = request.POST.copy()
        request.POST["source"] = "section"            # शाखाको प्रस्ताव सधैँ "पालिका विषयगत शाखा"
    if request.method == "POST":
        f = Form(request)
        name = f.text("name", "आयोजनाको नाम")
        ward = Ward.objects.filter(id=request.POST.get("ward") or 0).first()
        sector = Sector.objects.filter(id=request.POST.get("sector") or 0).first()
        sub = SubSector.objects.filter(id=request.POST.get("subsector") or 0, sector=sector).first() if sector else None
        fy = FiscalYear.objects.filter(id=request.POST.get("fiscalyear") or 0).first()
        cost = f.amount("proposed_cost", "प्रस्तावित लागत")
        share = f.amount("cost_share_pct", "जनसहभागिता %", required=False)
        errors = f.errors
        nums = {}
        for k, lbl in (("households", "घरधुरी"), ("population", "जनसंख्या"), ("women", "महिला"), ("marginalized", "दलित / सीमान्तकृत")):
            v = _int(request.POST.get(k))
            if v == "bad":
                errors.append(f"{lbl}: पूर्ण अङ्क लेख्नुहोस्।")
            nums[k] = v if v != "bad" else None
        years = _int(request.POST.get("duration_years"), 1, 5)
        if years in (None, "bad"):
            errors.append("अवधि १ देखि ५ वर्षसम्म।")
        if nums["population"] and ((nums["women"] or 0) > nums["population"] or (nums["marginalized"] or 0) > nums["population"]):
            errors.append("महिला / सीमान्तकृत सङ्ख्या जम्मा जनसंख्याभन्दा बढी भयो।")
        if share not in (None,) and share > 100:
            errors.append("जनसहभागिता १०० % भन्दा बढी हुँदैन।")
        nature = request.POST.get("nature") or ""
        source = request.POST.get("source") or ""
        if nature not in dict(BankProject.NATURE):
            errors.append("प्रकृति छान्नुहोस्।")
        if source not in dict(BankProject.SOURCE):
            errors.append("माग गर्ने छान्नुहोस्।")
        if ward is None and source == "ward":
            errors.append("वडा छान्नुहोस्।")
        if wards is not None and (ward is None or ward.id not in wards):
            errors.append("आफ्नो वडाको आयोजना मात्र प्रस्ताव गर्न मिल्छ।")
        if sector is None:
            errors.append("क्षेत्र छान्नुहोस्।")
        elif my_sectors is not None and sector.id not in my_sectors:
            errors.append("आफ्नो शाखाको क्षेत्रमा मात्र प्रस्ताव गर्न मिल्छ।")
        if sub and sector and sub.sector_id != sector.id:
            errors.append("उपक्षेत्र छानिएको क्षेत्रको होइन।")
        if name and ward and BankProject.objects.filter(name=name, ward=ward, hold="").exclude(id=b.id).exists():
            errors.append(f"वडा {ward.name} मा “{name}” नाममा आयोजना बैंकमा पहिले नै छ।")
        similar = _similar(name, ward, sector, b.id) if (name and not errors and not request.POST.get("dup_ok")) else []
        if similar:
            errors.append("मिल्दोजुल्दो आयोजना बैंकमा पहिले नै छन्: " + "; ".join(f"{x.code} “{x.name}”" for x in similar)
                          + " — दोहोरो होइन भने तल “दोहोरो होइन” मा ✓ गरी फेरि सुरक्षित गर्नुहोस्।")
        from .models import ToleMeeting
        meeting = ToleMeeting.objects.filter(id=request.POST.get("meeting") or 0).first()
        if meeting and (wards is not None and meeting.ward_id not in wards):
            meeting = None
        # तर्जुमा चक्र: वडाले सूची पेश गरिसकेपछि वडाबाट नयाँ प्रस्ताव थप्न पालिकाको योजना शाखा (bank.manage) मात्र
        plan = mt.current_plan()
        manage = "bank.manage" in user_perms(request.user)
        ward_locked = is_new and source == "ward" and ward is not None and mt.ward_locked(plan, ward.id)
        if ward_locked and not manage:
            errors.append(f"वडा {ward.name} को प्राथमिकता सूची पेश भइसकेको छ — थप्नुपरे पालिकाको योजना शाखामा अनुरोध गर्नुहोस्।")
        sector_locked = is_new and source != "ward" and sector is not None and mt.sector_locked(plan, sector.id)
        if sector_locked and not manage:
            errors.append(f"{sector.name} शाखाको प्रस्ताव सूची पेश भइसकेको छ — थप्नुपरे पालिकाको योजना शाखामा अनुरोध गर्नुहोस्।")
        files = _uploads(request, {"doc_decision": "decision", "doc_photo": "photo"}, errors)
        if not errors:
            with transaction.atomic():
                old_cost = b.proposed_cost
                b.name, b.ward, b.sector, b.subsector, b.fiscalyear = name, ward, sector, sub, fy
                b.location = f.text("location", "स्थान", required=False)
                b.nature, b.source = nature, source
                b.meeting = meeting
                b.requested_by = f.text("requested_by", "", required=False)[:200]
                b.request_ref = f.text("request_ref", "", required=False)[:200]
                b.description = f.text("description", "", required=False)
                b.need = f.text("need", "", required=False)
                b.proposed_cost, b.duration_years, b.cost_share_pct = cost, years, share or 0
                for k, v in nums.items():
                    setattr(b, k, v)
                if is_new:
                    b.code = bk.new_code()
                    b.created_by = request.user
                    # सामान्य क्रम: टोल/वडाको माग चरण २–३ मा, शाखा/विषयगत समितिको चरण ४ सम्म.
                    # त्यसपछि (वा वडा लक भएपछि, वा प्रमुख/कार्यपालिका/सभाबाट चरण ३ देखि) थपिए "पछि थपिएको" चिनो.
                    step = min(mt.cycle_step(plan), 7) if plan else 2
                    normal_until = {"ward": 3, "mayor": 2, "executive": 2, "assembly": 2}.get(source, 4)
                    if step > normal_until or ward_locked or sector_locked:
                        b.entry_step = max(step, 3)
                    bk.apply_sector_defaults(b)
                elif old_cost != cost and b.scored_at:
                    b.scored_at = None                    # लागत बदलिए अङ्क फेरि हेर्नुपर्छ
                b.save()
                for kind, fobj in files:
                    BankDoc.objects.create(bank=b, kind=kind, file=fobj)
            _log(request, b, "create" if is_new else "update", "प्रस्ताव")
            messages.success(request, f"{b.code} “{b.name}” {'आयोजना बैंकमा दर्ता' if is_new else 'सम्पादन'} भयो।")
            return redirect(_next(request, b))
    p = request.POST if request.method == "POST" else {}
    v = lambda k, d="": p.get(k, d)  # noqa: E731
    ward_opts = [("", "—")] + [(str(w.id), f"वडा नं. {w.name}") for w in
                               (Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all())]
    fy_default = b.fiscalyear_id or FiscalYear.objects.order_by("-name").values_list("id", flat=True).first()
    from .models import ToleMeeting
    mq = ToleMeeting.objects.select_related("ward")
    if wards is not None:
        mq = mq.filter(ward_id__in=wards)
    meeting_opts = [("", "— भेला छान्नुहोस् (ऐच्छिक) —")] + [
        (str(m.id), f"वडा {m.ward.name} · {m.tole} ({to_bs_np(m.date)})") for m in mq[:80]]
    sec_qs = Sector.objects.filter(is_active=True)
    if my_sectors is not None:
        sec_qs = sec_qs.filter(id__in=my_sectors)
    sectors = [("", "— छान्नुहोस् —")] + [(str(s.id), s.name) for s in sec_qs]
    subs = [("", "—")] + [(str(s.id), s.name) for s in SubSector.objects.select_related("sector")]
    sub_tree = {}                                    # क्षेत्र → उपक्षेत्र (छानिएको क्षेत्रका मात्र देखाउन)
    for x in SubSector.objects.order_by("sort", "id"):
        if getattr(x, "is_active", True) or x.id == b.subsector_id:
            sub_tree.setdefault(str(x.sector_id), []).append([x.id, x.name])
    s1 = [
        F("name", "आयोजनाको नाम", v("name", b.name), wide=True, hint="जस्तै: जनता टोल–स्वास्थ्य चौकी सडक स्तरोन्नति"),
        F("ward", "वडा" if my_sectors is None else "वडा (आयोजना रहने स्थान, ऐच्छिक)", v("ward", str(b.ward_id or "")), kind="select",
          options=ward_opts, required=my_sectors is None),
        F("location", "स्थान / टोल", v("location", b.location), required=False),
        F("fiscalyear", "प्रस्ताव गरिएको आ.व.", v("fiscalyear", str(fy_default or "")), kind="select",
          options=[(str(x.id), x.name) for x in FiscalYear.objects.order_by("-name")], required=False),
        F("sector", "क्षेत्र", v("sector", str(b.sector_id or "")), kind="select", options=sectors),
        F("subsector", "उपक्षेत्र", v("subsector", str(b.subsector_id or "")), kind="select", options=subs, required=False),
        F("nature", "प्रकृति", v("nature", b.nature), kind="select", options=list(BankProject.NATURE)),
        F("description", "विवरण / भौतिक लक्ष्य", v("description", b.description), kind="textarea", wide=True, required=False,
          hint="जस्तै: २.४ कि.मि. ग्राभेलबाट कालोपत्रे, दुवैतर्फ नाली"),
        F("need", "आवश्यकता / औचित्य", v("need", b.need), kind="textarea", wide=True, required=False),
    ]
    s2 = [
        F("meeting", "टोल / बस्ती भेला", v("meeting", str(b.meeting_id or "")), kind="select", options=meeting_opts, required=False,
          hint="भेला दर्ता: योजना तर्जुमा → टोल भेला"),
        F("source", "माग गर्ने", v("source", b.source), kind="select",
          options=[("section", "पालिका विषयगत शाखा")] if my_sectors is not None else list(BankProject.SOURCE)),
        F("requested_by", "बस्ती / टोल / संस्था", v("requested_by", b.requested_by), required=False),
        F("request_ref", "माग निर्णय / मिति", v("request_ref", b.request_ref), required=False, hint="जस्तै: बस्ती भेला निर्णय, २०८२-१०-०५"),
        F("households", "लाभान्वित घरधुरी", v("households", b.households or ""), required=False),
        F("population", "लाभान्वित जनसंख्या", v("population", b.population or ""), required=False),
        F("women", "मध्ये महिला", v("women", b.women or ""), required=False),
        F("marginalized", "मध्ये दलित / सीमान्तकृत", v("marginalized", b.marginalized or ""), required=False),
    ]
    s3 = [
        F("proposed_cost", "प्रस्तावित लागत (रु.)", v("proposed_cost", plain(b.proposed_cost) if b.proposed_cost else "")),
        F("duration_years", "कार्यान्वयन अवधि (वर्ष)", v("duration_years", b.duration_years or 1), hint="१ देखि ५"),
        F("cost_share_pct", "जनसहभागिता (%)", v("cost_share_pct", plain(b.cost_share_pct) if b.cost_share_pct else ""), required=False),
        F("doc_decision", "माग / निर्णय (फाइल)", kind="file", required=False),
        F("doc_photo", "स्थलको फोटो", kind="file", required=False),
    ]
    if request.method == "POST" and any("मिल्दोजुल्दो" in e for e in errors):
        s3.append(F("dup_ok", "दोहोरो होइन", False, kind="checkbox", required=False, hint="मैले जाँचेँ — यो फरक आयोजना हो"))
    return _render_form(request, b, {
        "title": "नयाँ आयोजना प्रस्ताव" if is_new else f"प्रस्ताव सम्पादन — {b.code}", "icon": "lightbulb", "errors": errors,
        "submit": "आयोजना बैंकमा सुरक्षित गर्नुहोस्", "upload": True, "sub_tree": sub_tree,
        "sections": [{"title": "आयोजना", "icon": "diagram-project", "fields": s1},
                     {"title": "माग र लाभान्वित", "icon": "people-group", "fields": s2},
                     {"title": "लागत र अवधि", "icon": "coins", "fields": s3}],
        "note": "दर्तापछि वडा अध्यक्षले प्राथमिकता क्रम तोक्नुहुन्छ, इन्जिनियरले प्राविधिक जाँच गर्नुहुन्छ र योजना शाखाले अङ्क र कोड दिएर तयार घोषणा गर्छ।"
        if bk.settings_().ward_priority else
        "दर्तापछि इन्जिनियरले प्राविधिक जाँच गर्नुहुन्छ र योजना शाखाले अङ्क र कोड दिएर तयार घोषणा गर्छ।",
    })


@login_required
def bank_add(request):
    if (r := _guard(request, "bank.propose", "bank.manage")):
        return r
    secs = _user_sectors(request.user)
    if secs is not None:                                        # विषयगत शाखा
        return _proposal_form(request, BankProject(duration_years=1, nature="new", source="section",
                                                   sector_id=secs[0] if len(secs) == 1 else None))
    return _proposal_form(request, BankProject(duration_years=1, nature="new", source="ward"))


@login_required
def bank_edit(request, bid):
    if (r := _guard(request, "bank.propose", "bank.manage")):
        return r
    b = _bank(request, bid)
    P = user_perms(request.user)
    if b.mtef_lines.exists():
        messages.error(request, "MTEF मा परिसकेको आयोजनाको प्रस्ताव बदल्न मिल्दैन।")
        return redirect(_next(request, b))
    if b.ready_at and "bank.manage" not in P:
        messages.error(request, "तयार घोषणा भइसकेपछि योजना शाखाले मात्र सम्पादन गर्न सक्छ।")
        return redirect(_next(request, b))
    return _proposal_form(request, b)


# ---------------------------------------------------------------- २. वडा प्राथमिकता
@login_required
def ward_priority(request):
    P = user_perms(request.user)
    if not P & {"bank.ward_priority", "bank.view"}:
        return redirect("unauthorized")
    wards = _user_wards(request.user)
    allowed = Ward.objects.filter(id__in=wards) if wards is not None else Ward.objects.all()
    ward = allowed.filter(id=request.GET.get("ward") or request.POST.get("ward") or 0).first() or allowed.first()
    if ward is None:
        messages.error(request, "वडा भेटिएन।")
        return redirect("bank_list")
    can = "bank.ward_priority" in P
    pending = list(bk.queryset().filter(ward=ward, ward_approved_at__isnull=True, hold="").order_by("ward_rank", "id"))
    done = list(bk.queryset().filter(ward=ward, ward_approved_at__isnull=False)
                .order_by("converted", "-ward_approved_at", "ward_rank", "-id")[:60])         # नयाँ प्रस्ताव माथि, पुराना तल
    errors = []
    if request.method == "POST":
        if not can:
            return redirect("unauthorized")
        ranks = {}
        for b in pending:
            r = _int(request.POST.get(f"rank_{b.id}"), 1, 999)
            if r in (None, "bad"):
                errors.append(f"“{b.name}”: प्राथमिकता क्रम (१, २, ३…) लेख्नुहोस्।")
            else:
                ranks[b.id] = r
        if len(set(ranks.values())) != len(ranks):
            errors.append("एउटै क्रम दुई आयोजनालाई दिन मिल्दैन।")
        if not pending:
            errors.append("स्वीकृत गर्न बाँकी प्रस्ताव छैन।")
        if not errors:
            name, post = approver_identity(request.user, "ward")
            now = timezone.now()
            with transaction.atomic():
                for b in pending:
                    b.ward_rank, b.ward_approved_at, b.ward_approver = ranks[b.id], now, f"{name} ({post})"
                    b.save(update_fields=["ward_rank", "ward_approved_at", "ward_approver"])
                    _log(request, b, "update", f"वडा प्राथमिकता {ranks[b.id]}")
                # तर्जुमा चक्र चरण ३: वडाले सूची पेश गरेको — यस चक्रका लागि वडाको सूची लक
                plan = mt.current_plan()
                if plan is not None and plan.status == "draft":
                    wc = mt.ward_ceilings(plan)[ward.id]
                    wc.submitted_at, wc.submitted_by = now, f"{name} ({post})"[:200]
                    wc.save(update_fields=["submitted_at", "submitted_by"])
            messages.success(request, f"वडा {ward.name} का {bk.np(len(pending))} प्रस्तावको प्राथमिकता क्रम स्वीकृत भई पेश भयो — अब प्राविधिक जाँचमा जान्छ।")
            return redirect(_next(request) if request.POST.get("next") else f"{reverse('bank_ward_priority')}?ward={ward.id}")
    posted = request.POST if request.method == "POST" else {}
    rows = [{"b": b, "rank": posted.get(f"rank_{b.id}", b.ward_rank or i)} for i, b in enumerate(pending, 1)]
    plan = mt.current_plan()
    wc = mt.ward_ceilings(plan).get(ward.id) if plan is not None and plan.status != "approved" else None
    used = mt.ward_used(ward.id)
    ceiling = {"wc": wc, "used": used, "over": bool(wc and wc.amount) and used > wc.amount,
               "left": (wc.amount - used) if wc and wc.amount else None,
               "pct": min(int(used * 100 / wc.amount), 100) if wc and wc.amount else None} if wc else None
    return render(request, "projects/bank_ward_priority.html", {
        "ward": ward, "wards": allowed, "pending": rows, "done": done, "errors": errors, "can": can, "ceiling": ceiling,
        "next": request.GET.get("next", ""),
    })


# ---------------------------------------------------------------- ३. प्राविधिक जाँच
@login_required
def technical(request, bid):
    if (r := _guard(request, "bank.screen")):
        return r
    b = _bank(request, bid)
    if b.mtef_lines.exists() or b.hold:
        messages.error(request, "MTEF मा परेको वा स्थगित / अस्वीकृत आयोजनाको जाँच बदल्न मिल्दैन।")
        return redirect(_next(request, b))
    if bk.settings_().ward_priority and b.source == "ward" and not b.ward_approved_at:
        messages.error(request, "वडा प्राथमिकता स्वीकृत भएपछि मात्र प्राविधिक जाँच।")
        return redirect(_next(request, b))
    errors = []
    if request.method == "POST":
        f = Form(request)
        cost = f.amount("tech_cost", "इन्जिनियरको लागत अनुमान")
        errors = f.errors
        files = _uploads(request, {"doc_dpr": "dpr", "doc_iee": "iee", "doc_land": "land"}, errors)
        if not errors:
            na = []
            for k, _l in bk.CHECKS:
                val = request.POST.get(k) or ""
                if k in bk.NA_OK and val == "na":       # DPR / IEE / जग्गा यो आयोजनामा आवश्यक नपर्ने
                    na.append(k)
                    setattr(b, k, False)
                else:
                    setattr(b, k, val in ("1", "yes"))
            b.not_required = ",".join(na)
            name, post = approver_identity(request.user, "municipal")
            if b.tech_cost != cost and b.scored_at:
                b.scored_at = None
            b.tech_cost, b.tech_note = cost, f.text("tech_note", "", required=False)
            b.tech_by, b.tech_at = f"{name} ({post})"[:200], timezone.now()
            b.save()
            for kind, fobj in files:
                BankDoc.objects.create(bank=b, kind=kind, file=fobj)
            _log(request, b, "update", f"प्राविधिक जाँच, लागत {cost}")
            messages.success(request, f"{b.code} को प्राविधिक जाँच सुरक्षित भयो।")
            return redirect(_next(request, b))
    p = request.POST if request.method == "POST" else {}
    na = bk.not_required(b)
    TRI = [("yes", "छ"), ("no", "छैन / बाँकी"), ("na", "आवश्यक छैन")]
    fields = []
    for k, lbl in bk.CHECKS:
        if k in bk.NA_OK:
            cur = p.get(k, "no") if p else ("na" if k in na else ("yes" if getattr(b, k) else "no"))
            fields.append(F(k, lbl, cur, kind="tri", options=TRI, required=False))
        else:
            fields.append(F(k, lbl, (k in p) if p else getattr(b, k), kind="checkbox", required=False, hint=lbl))
    fields += [
        F("tech_cost", "इन्जिनियरको लागत अनुमान (रु.)", p.get("tech_cost", plain(b.tech_cost) if b.tech_cost else plain(b.proposed_cost)),
          hint=f"प्रस्तावित: रु. {bk.money(b.proposed_cost)}"),
        F("doc_dpr", "DPR / लागत अनुमान (फाइल)", kind="file", required=False, hint="ऐच्छिक"),
        F("doc_iee", "IEE / वातावरणीय अध्ययन (फाइल)", kind="file", required=False, hint="ऐच्छिक"),
        F("doc_land", "जग्गा प्रमाण (फाइल)", kind="file", required=False, hint="ऐच्छिक"),
        F("tech_note", "प्राविधिक टिप्पणी", p.get("tech_note", b.tech_note), kind="textarea", wide=True, required=False),
    ]
    return _render_form(request, b, {"title": "प्राविधिक जाँच", "icon": "helmet-safety", "errors": errors, "upload": True,
                                     "submit": "जाँच सुरक्षित गर्नुहोस्",
                                     "sections": [{"title": "तयारी जाँचसूची र लागत", "icon": "list-check", "fields": fields}],
                                     "note": "सबै आयोजनामा DPR, IEE वा जग्गा प्रमाण चाहिँदैन — नचाहिनेमा “आवश्यक छैन” छान्नुहोस्; त्यो जाँच अङ्कमा गनिँदैन। फाइल राख्नु ऐच्छिक हो।"})


# ---------------------------------------------------------------- ४. अङ्क र कोड
@login_required
def score(request, bid):
    if (r := _guard(request, "bank.manage")):
        return r
    b = _bank(request, bid)
    if not b.tech_at or b.mtef_lines.exists() or b.hold:
        messages.error(request, "प्राविधिक जाँचपछि र MTEF मा पर्नुअघि मात्र अङ्क दिन मिल्छ।")
        return redirect(_next(request, b))
    crit = bk.criteria()
    errors = []
    if request.method == "POST":
        scores = {}
        for c in crit:
            v = to_dec(request.POST.get(f"c_{c.id}"))
            if v in (None, "bad") or v < 0 or v > c.weight:
                errors.append(f"{c.name}: ० देखि {c.weight} सम्म।")
            else:
                scores[str(c.id)] = float(v)
        pr = request.POST.get("priority") or ""
        pillar = PlanPillar.objects.filter(id=request.POST.get("pillar") or 0).first()
        program = PlanProgram.objects.filter(id=request.POST.get("program") or 0).first()
        sdg = _int(request.POST.get("sdg"), 1, 17)
        gender, climate = request.POST.get("gender") or "", request.POST.get("climate") or ""
        if pr not in ("P1", "P2"):
            errors.append("प्राथमिकता (P1 / P2) छान्नुहोस्।")
        if pillar is None:
            errors.append("रणनीतिक स्तम्भ छान्नुहोस्।")
        if program and pillar and program.pillar_id != pillar.id:
            errors.append("कार्यक्रम छानिएको स्तम्भको होइन।")
        if sdg in (None, "bad"):
            errors.append("SDG लक्ष्य छान्नुहोस्।")
        if gender not in dict(BankProject.GENDER):
            errors.append("लैङ्गिक कोड छान्नुहोस्।")
        if climate not in dict(BankProject.CLIMATE):
            errors.append("जलवायु कोड छान्नुहोस्।")
        if not errors:
            name, post = approver_identity(request.user, "municipal")
            b.scores, b.score = scores, Decimal(str(sum(scores.values()))).quantize(Decimal("0.01"))
            b.priority, b.pillar, b.program, b.sdg, b.gender, b.climate = pr, pillar, program, sdg, gender, climate
            b.scored_by, b.scored_at = f"{name} ({post})"[:200], timezone.now()
            b.save()
            _log(request, b, "update", f"अङ्क {b.score}, {pr}, SDG {sdg}")
            messages.success(request, f"{b.code}: अङ्क {bk.np(f'{b.score:g}')} र कोड सुरक्षित भयो। अब तयार घोषणा गर्नुहोस्।")
            return redirect(_next(request, b))
    p = request.POST if request.method == "POST" else {}
    s1 = []
    for c in crit:
        sug = bk.suggest(b, c)
        cur = (b.scores or {}).get(str(c.id))
        val = p.get(f"c_{c.id}", plain(Decimal(str(cur))) if cur is not None else (plain(sug) if sug is not None else ""))
        hint = f"पूर्णाङ्क {bk.np(c.weight)}" + (f" · सुझाव {bk.np(plain(sug) or '0')} ({c.get_auto_display()})" if sug is not None else "")
        s1.append(F(f"c_{c.id}", c.name, val, hint=hint))
    d = getattr(b.sector, "plan_default", None) if b.sector_id else None
    pillars = [("", "— छान्नुहोस् —")] + [(str(x.id), x.name) for x in PlanPillar.objects.filter(is_active=True)]
    programs = [("", "—")] + [(str(x.id), f"{x.pillar.name} › {x.name}") for x in PlanProgram.objects.filter(is_active=True).select_related("pillar")]
    sdgs = [("", "— छान्नुहोस् —")] + [(str(i), f"SDG {lbl}") for i, lbl in bk.sdg_options()]
    s2 = [
        F("priority", "प्राथमिकता", p.get("priority", b.priority), kind="select", options=[("", "—")] + list(BankProject.PRIORITY)),
        F("pillar", "रणनीतिक स्तम्भ", p.get("pillar", str(b.pillar_id or "")), kind="select", options=pillars),
        F("program", "कार्यक्रम", p.get("program", str(b.program_id or "")), kind="select", options=programs, required=False),
        F("sdg", "दिगो विकास लक्ष्य (SDG)", p.get("sdg", str(b.sdg or "")), kind="select", options=sdgs),
        F("gender", "लैङ्गिक उत्तरदायी कोड", p.get("gender", b.gender), kind="select", options=[("", "—")] + list(BankProject.GENDER)),
        F("climate", "जलवायु परिवर्तन कोड", p.get("climate", b.climate), kind="select", options=[("", "—")] + list(BankProject.CLIMATE)),
    ]
    return _render_form(request, b, {
        "title": "अङ्क र MTEF कोड", "icon": "ranking-star", "errors": errors, "submit": "अङ्क र कोड सुरक्षित गर्नुहोस्",
        "sections": [{"title": f"मूल्याङ्कनका आधार (जम्मा {bk.np(bk.max_score())})", "icon": "scale-balanced", "fields": s1},
                     {"title": "MTEF कोड", "icon": "tags", "fields": s2}],
        "note": "सुझाव अङ्क प्रस्ताव र प्राविधिक जाँचबाट आफैँ हिसाब हुन्छ — आवश्यक भए बदल्नुहोस्।" +
                (" कोड क्षेत्रको पूर्वनिर्धारितबाट भरिएको छ।" if d else ""),
    })


# ---------------------------------------------------------------- ५. तयार / स्थगित / अस्वीकृत (modal)
@login_required
def decide(request, bid):
    b = _bank(request, bid)
    P = user_perms(request.user)
    allowed = "bank.manage" in P
    if not allowed:
        return HttpResponse("<div class='p-6 text-sm text-rose-600'>तपाईंलाई निर्णय गर्ने अधिकार छैन।</div>", status=403)
    in_mtef = b.mtef_lines.exists()
    problems = []
    if not b.tech_at:
        problems.append("प्राविधिक जाँच भएको छैन।")
    if not b.scored_at:
        problems.append("अङ्क दिइएको छैन।")
    if not b.coded:
        problems.append("पाँचै कोड (P1/P2, स्तम्भ, SDG, लैङ्गिक, जलवायु) पूरा छैनन्।")
    min_s = bk.settings_().min_ready_score
    if min_s and (b.score or 0) < min_s:
        problems.append(f"अङ्क {bk.np(min_s)} भन्दा कम छ।")
    warnings = [lbl + " छैन" for k, lbl in bk.CHECKS if not getattr(b, k)]
    if request.method == "POST":
        def fail(msg, code=400):
            return HttpResponse(f"<p class='text-sm text-rose-600'>{msg}</p>", status=code)
        if in_mtef:
            return fail("MTEF मा परिसकेको आयोजनाको निर्णय यहाँबाट बदल्न मिल्दैन — MTEF बाट हटाउनुहोस्।")
        d = request.POST.get("decision")
        reason = (request.POST.get("reason") or "").strip()
        ref = (request.POST.get("ref") or "").strip()[:200]
        if d == "ready":
            if problems:
                return fail(" ".join(problems))
            b.ready_at, b.ready_ref, b.hold, b.hold_reason, b.hold_at = timezone.now(), ref, "", "", None
            msg = f"{b.code} तयार घोषणा भयो — अब MTEF मा चयन गर्न सकिन्छ।"
        elif d in ("postponed", "rejected"):
            if not reason:
                return fail("स्थगित / अस्वीकृत गर्दा कारण लेख्नुहोस्।")
            b.hold, b.hold_reason, b.hold_at, b.ready_at = d, reason, timezone.now(), None
            msg = f"{b.code} {'स्थगित' if d == 'postponed' else 'अस्वीकृत'} गरियो।"
        elif d == "reopen":
            b.hold, b.hold_reason, b.hold_at, b.ready_at = "", "", None, None
            msg = f"{b.code} फेरि प्रक्रियामा ल्याइयो।"
        else:
            return fail("निर्णय छान्नुहोस्।")
        b.save()
        _log(request, b, "update", f"निर्णय: {d} {ref} {reason}")
        messages.success(request, msg)
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = with_open(_return_to(request), b.id)
        return resp
    return render(request, "projects/_bank_decide.html", {"b": b, "problems": problems, "warnings": warnings, "in_mtef": in_mtef,
                                                          "max_score": bk.max_score()})


# ---------------------------------------------------------------- प्रिन्ट
@login_required
def bank_print(request, bid):
    if not user_perms(request.user) & VIEW:
        return redirect("unauthorized")
    b = _bank(request, bid)
    ex = bk.exec_map([b]).get(b.id)
    steps, summary = bk.workflow(b, request.user, ex)
    return render(request, "projects/bank_print.html", {
        "b": b, "steps": steps, "summary": summary, "checks": [(lbl, getattr(b, k)) for k, lbl in bk.CHECKS],
        "score_rows": [{"c": c, "v": (b.scores or {}).get(str(c.id))} for c in bk.criteria()], "max_score": bk.max_score(),
        "lines": sorted(summary["lines"], key=lambda x: x.plan.start_fy.name),
    })


# ---------------------------------------------------------------- Excel
HEAD = ["बैंक नं.", "आयोजना", "वडा", "स्थान", "क्षेत्र", "प्रकृति", "लागत (रु.)", "अवधि", "घरधुरी", "जनसंख्या", "महिला",
        "दलित / सीमान्तकृत", "वडा क्रम", "अङ्क", "प्राथमिकता", "स्तम्भ", "SDG", "लैङ्गिक", "जलवायु", "अवस्था", "बजेट", "खर्च", "प्रगति %"]


def _xlsx(title, head, rows, widths=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = title[:30]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8EEF9")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "B2"
    return wb


def _xlsx_response(wb, name):
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _export(rows):
    data = []
    for r in rows:
        b, ex = r["b"], r["summary"]["exec"]
        data.append([b.code, b.name, b.ward.name if b.ward else "", b.location, b.sector.name if b.sector else "",
                     b.get_nature_display(), float(b.cost or 0), b.duration_years, b.households, b.population, b.women,
                     b.marginalized, b.ward_rank, float(b.score) if b.score is not None else None, b.priority,
                     b.pillar.name if b.pillar else "", b.sdg, b.get_gender_display() if b.gender else "",
                     b.get_climate_display() if b.climate else "", r["summary"]["current_label"],
                     float(ex.get("allocated", 0) or 0), float(ex.get("spent", 0) or 0), ex.get("progress")])
    wb = _xlsx("आयोजना बैंक", HEAD, data, [16, 40, 6, 18, 16, 12, 14, 6, 8, 10, 8, 10, 7, 7, 8, 22, 6, 12, 14, 22, 14, 14, 8])
    return _xlsx_response(wb, "project-bank.xlsx")


IMPORT_COLS = ["आयोजना", "वडा", "स्थान", "क्षेत्र", "प्रकृति", "लागत", "अवधि", "घरधुरी", "जनसंख्या", "महिला", "दलित / सीमान्तकृत",
               "माग गर्ने", "विवरण"]


@login_required
def bank_import(request):
    if (r := _guard(request, "bank.propose", "bank.manage")):
        return r
    if request.GET.get("template"):
        wb = _xlsx("प्रस्ताव", IMPORT_COLS, [["जनता टोल–स्वास्थ्य चौकी सडक", "४", "जनता टोल", Sector.objects.values_list("name", flat=True).first() or "सडक",
                                             "स्तरोन्नति", 9000000, 2, 320, 1800, 920, 210, "जनता टोल बस्ती", "२.४ कि.मि. कालोपत्रे"]],
                   [36, 6, 16, 18, 12, 12, 6, 8, 10, 8, 12, 18, 30])
        return _xlsx_response(wb, "project-bank-template.xlsx")
    errors, created = [], []
    if request.method == "POST":
        from openpyxl import load_workbook

        fobj = request.FILES.get("file")
        if not fobj or not fobj.name.lower().endswith(".xlsx"):
            errors.append(".xlsx फाइल छान्नुहोस्।")
        else:
            try:
                ws = load_workbook(fobj, read_only=True, data_only=True).active
                rows = list(ws.iter_rows(values_only=True))
            except Exception:
                rows, errors = [], ["फाइल पढ्न सकिएन।"]
            wards = _user_wards(request.user)
            my_sectors = _user_sectors(request.user)
            plan = mt.current_plan()
            manage = "bank.manage" in user_perms(request.user)
            step = min(mt.cycle_step(plan), 7) if plan else 2
            ward_by = {bk.np(w.name).translate(str.maketrans("०१२३४५६७८९", "0123456789")).strip(): w for w in Ward.objects.all()}
            sectors = {s.name.strip(): s for s in Sector.objects.all()}
            natures = {v: k for k, v in BankProject.NATURE}
            parsed = []
            for i, row in enumerate(rows[1:], start=2):
                if not row or not any(row):
                    continue
                row = list(row) + [None] * (len(IMPORT_COLS) - len(row))
                cell = lambda j: ("" if row[j] is None else str(row[j]).strip())  # noqa: E731
                name = cell(0)
                wkey = cell(1).translate(str.maketrans("०१२३४५६७८९", "0123456789")).replace(".0", "")
                ward = ward_by.get(wkey)
                sector = sectors.get(cell(3))
                nature = natures.get(cell(4), "new" if not cell(4) else None)
                cost = to_dec(cell(5))
                yrs = to_dec(cell(6) or "1")
                bad = []
                if not name:
                    bad.append("नाम खाली")
                # वडा खाली भए पालिका / शाखा स्तरीय आयोजना; विषयगत शाखाको आयात सधैँ शाखा स्तरीय
                src = "section" if (my_sectors is not None or not cell(1)) else "ward"
                if cell(1) and ward is None:
                    bad.append(f"वडा '{cell(1)}' भेटिएन")
                elif src == "ward" and wards is not None and ward.id not in wards:
                    bad.append("आफ्नो वडा होइन")
                elif src == "section" and wards is not None:
                    bad.append("वडा नम्बर लेख्नुहोस्")
                if sector is None:
                    bad.append(f"क्षेत्र '{cell(3)}' भेटिएन")
                elif my_sectors is not None and sector.id not in my_sectors:
                    bad.append("आफ्नो शाखाको क्षेत्र होइन")
                late = False
                if src == "ward" and ward is not None and mt.ward_locked(plan, ward.id):
                    late = True
                    if not manage:
                        bad.append(f"वडा {ward.name} को सूची पेश भइसकेको")
                if src == "section" and sector is not None and mt.sector_locked(plan, sector.id):
                    late = True
                    if not manage:
                        bad.append(f"{sector.name} शाखाको सूची पेश भइसकेको")
                if nature is None:
                    bad.append(f"प्रकृति '{cell(4)}' मिलेन")
                if cost in (None, "bad") or cost <= 0:
                    bad.append("लागत मिलेन")
                if yrs in (None, "bad") or not (1 <= yrs <= 5):
                    bad.append("अवधि १–५")
                nums = []
                for j in (7, 8, 9, 10):
                    n = to_dec(cell(j))
                    nums.append(int(n) if n not in (None, "bad") and n >= 0 else None)
                if bad:
                    errors.append(f"पङ्क्ति {i}: " + ", ".join(bad))
                    continue
                normal_until = 3 if src == "ward" else 4
                entry = max(step, 3) if (step > normal_until or late) else None
                parsed.append(dict(source=src, entry_step=entry, name=name[:250], ward=ward, location=cell(2)[:250], sector=sector, nature=nature, proposed_cost=cost,
                                   duration_years=int(yrs), households=nums[0], population=nums[1], women=nums[2],
                                   marginalized=nums[3], requested_by=cell(11)[:200], description=cell(12)))
            if not parsed and not errors:
                errors.append("फाइलमा प्रस्ताव भेटिएन।")
            if not errors:
                fy = FiscalYear.objects.order_by("-name").first()
                with transaction.atomic():
                    for d in parsed:
                        b = BankProject(fiscalyear=fy, created_by=request.user, **d)
                        b.code = bk.new_code()
                        bk.apply_sector_defaults(b)
                        b.save()
                        created.append(b)
                from usercommittee.models import ActivityLog
                ActivityLog.objects.create(user=request.user, action="create", model_name="आयोजना बैंक", object_repr="Excel आयात",
                                           note=f"{len(created)} प्रस्ताव")
                messages.success(request, f"{bk.np(len(created))} प्रस्ताव आयोजना बैंकमा दर्ता भए।")
                return redirect("bank_list")
    return render(request, "projects/bank_import.html", {"errors": errors, "cols": IMPORT_COLS})


# ---------------------------------------------------------------- बैंक बाहिरका योजना
@login_required
def outside_bank(request):
    P = user_perms(request.user)
    if not P & {"bank.manage", "bank.view"}:
        return redirect("unauthorized")
    fys = list(FiscalYear.objects.order_by("-name"))
    sel = request.GET.get("fiscalyear") or request.POST.get("fiscalyear") or ""
    fy = next((f for f in fys if str(f.id) == sel), fys[0] if fys else None)
    qs = Project.objects.filter(fiscalyear=fy, bank_project__isnull=True).select_related("ward", "sector", "procurement_type")
    wards = _user_wards(request.user)
    if wards is not None:
        qs = qs.filter(ward_id__in=wards)
    if request.method == "POST" and "bank.manage" in P:
        ids = [int(x) for x in request.POST.getlist("pid") if x.isdigit()]
        act = request.POST.get("act")
        n = 0
        if act == "link":
            for p in qs.filter(id__in=ids):
                link_project(p, request.user)
                n += 1
            messages.success(request, f"{bk.np(n)} योजना आयोजना बैंकमा दर्ता गरी जोडियो।")
        elif act == "reason":
            reason = (request.POST.get("reason") or "").strip()[:250]
            if not reason:
                messages.error(request, "कारण लेख्नुहोस्।")
            else:
                n = qs.filter(id__in=ids).update(outside_bank_reason=reason)
                messages.success(request, f"{bk.np(n)} योजनामा कारण राखियो।")
        return redirect(f"{reverse('bank_outside')}?fiscalyear={fy.id if fy else ''}")
    rows = list(qs.order_by("ward__name", "name"))
    return render(request, "projects/bank_outside.html", {"fy": fy, "fiscal_years": fys, "rows": rows,
                                                          "can": "bank.manage" in P,
                                                          "no_reason": sum(1 for p in rows if not p.outside_bank_reason)})


def link_project(p, user=None, converted=False):
    """चालु / पुरानो Project बाट बैंक अभिलेख बनाई जोड्ने (रूपान्तरण र बैंक बाहिरका योजना)."""
    now = timezone.now()
    b = BankProject(fiscalyear=p.carried_from or p.fiscalyear, name=p.name[:250], ward=p.ward, location=(p.address or "")[:250],
                    sector=p.sector, subsector=p.subsector, nature="new", source="section", proposed_cost=Decimal(str(p.budget or 0)),
                    duration_years=2 if p.carried_from_id else 1, converted=converted, created_by=user,
                    ward_approved_at=now, ward_approver="पुरानो अभिलेख", tech_at=now, tech_by="पुरानो अभिलेख",
                    ready_at=now, ready_ref="पुरानो अभिलेख (रूपान्तरण)" if converted else "बैंक बाहिरबाट आएको योजना")
    b.code = bk.new_code()
    bk.apply_sector_defaults(b)
    if b.coded:
        b.scored_at, b.scored_by = now, "क्षेत्रको पूर्वनिर्धारित कोड"
    b.save()
    p.bank_project = b
    p.save(update_fields=["bank_project"])
    return b


# ---------------------------------------------------------------- कोड बाँकी (थोकमा)
@login_required
def codes_pending(request):
    if (r := _guard(request, "bank.manage")):
        return r
    qs, _w = _scoped(request)
    pending = [b for b in qs.filter(hold="").order_by("sector__sort", "ward__name", "id") if not b.coded]
    if request.method == "POST":
        ids = {int(x) for x in request.POST.getlist("bid") if x.isdigit()}
        n = 0
        for b in pending:
            if b.id in ids and bk.apply_sector_defaults(b):
                if b.coded and b.tech_at and not b.scored_at:
                    b.scored_at, b.scored_by = timezone.now(), "क्षेत्रको पूर्वनिर्धारित कोड"
                b.save()
                n += 1
        messages.success(request, f"{bk.np(n)} आयोजनामा क्षेत्रको पूर्वनिर्धारित कोड लगाइयो।" if n else
                         "छानिएका आयोजनाको क्षेत्रमा पूर्वनिर्धारित कोड छैन — सेटिङबाट राख्नुहोस्।")
        return redirect("bank_codes")
    rows = [{"b": b, "d": getattr(b.sector, "plan_default", None) if b.sector_id else None} for b in pending[:500]]
    return render(request, "projects/bank_codes.html", {"rows": rows, "total": len(pending)})


# ---------------------------------------------------------------- रूपान्तरण
def convert_preview():
    projects = list(Project.objects.filter(bank_project__isnull=True).select_related("sector", "fiscalyear"))
    with_default = sum(1 for p in projects if p.sector_id and SectorPlanDefault.objects.filter(sector_id=p.sector_id).exists())
    by_fy = {}
    for p in projects:
        k = p.fiscalyear.name if p.fiscalyear else "—"
        by_fy[k] = by_fy.get(k, 0) + 1
    return {"total": len(projects), "coded": with_default, "uncoded": len(projects) - with_default,
            "by_fy": sorted(by_fy.items(), reverse=True), "carried": sum(1 for p in projects if p.carried_from_id)}


@login_required
def convert(request):
    if not has_perm(request.user, "plan.settings"):
        return redirect("unauthorized")
    if request.method == "POST" and request.POST.get("confirm") == "1":
        n = 0
        with transaction.atomic():
            for p in Project.objects.filter(bank_project__isnull=True).select_related("sector", "fiscalyear").order_by("id"):
                link_project(p, request.user, converted=True)
                n += 1
        from usercommittee.models import ActivityLog
        ActivityLog.objects.create(user=request.user, action="create", model_name="आयोजना बैंक", object_repr="रूपान्तरण",
                                   note=f"{n} योजना")
        messages.success(request, f"{bk.np(n)} पुराना योजना आयोजना बैंकमा दर्ता गरी जोडिए।")
        return redirect("bank_list")
    return render(request, "projects/bank_convert.html", {"pv": convert_preview()})


# ---------------------------------------------------------------- सेटिङ
@login_required
def plan_settings(request):
    if not has_perm(request.user, "plan.settings"):
        return redirect("unauthorized")
    S = PlanSettings.get()
    if request.method == "POST":
        act = request.POST.get("act")
        g = request.POST.get
        if act == "general":
            S.ward_priority = bool(g("ward_priority"))
            S.prefix = (g("prefix") or "PB").strip()[:10] or "PB"
            v = _int(g("min_ready_score"), 0, 100)
            S.min_ready_score = v if v not in (None, "bad") else 0
            S.save()
        elif act == "pillars":
            for x in PlanPillar.objects.all():
                x.name = (g(f"pl_name_{x.id}") or x.name).strip()[:150]
                x.code = (g(f"pl_code_{x.id}") or "").strip()[:20]
                x.is_active = bool(g(f"pl_active_{x.id}"))
                x.save()
            for x in PlanProgram.objects.all():
                x.name = (g(f"pg_name_{x.id}") or x.name).strip()[:200]
                x.code = (g(f"pg_code_{x.id}") or "").strip()[:20]
                x.is_active = bool(g(f"pg_active_{x.id}"))
                x.save()
            if (n := (g("new_pillar") or "").strip()):
                PlanPillar.objects.create(name=n[:150], sort=PlanPillar.objects.count() + 1)
            pl = PlanPillar.objects.filter(id=g("new_program_pillar") or 0).first()
            if pl and (n := (g("new_program") or "").strip()):
                PlanProgram.objects.create(pillar=pl, name=n[:200], sort=pl.programs.count() + 1)
        elif act == "criteria":
            total = 0
            for c in ScoreCriterion.objects.all():
                c.name = (g(f"cr_name_{c.id}") or c.name).strip()[:150]
                w = _int(g(f"cr_w_{c.id}"), 0, 100)
                c.weight = w if w not in (None, "bad") else c.weight
                c.auto = g(f"cr_auto_{c.id}") if g(f"cr_auto_{c.id}") in dict(ScoreCriterion.AUTO) else ""
                c.is_active = bool(g(f"cr_active_{c.id}"))
                c.save()
                total += c.weight if c.is_active else 0
            if (n := (g("new_criterion") or "").strip()):
                w = _int(g("new_weight"), 1, 100)
                ScoreCriterion.objects.create(name=n[:150], weight=w if w not in (None, "bad") else 10, sort=ScoreCriterion.objects.count() + 1)
            if total != 100:
                messages.warning(request, f"सक्रिय आधारको जम्मा पूर्णाङ्क {total} छ — १०० राख्दा तुलना सजिलो हुन्छ।")
        elif act == "heads":
            for h in ResourceHead.objects.all():
                h.name = (g(f"rh_name_{h.id}") or h.name).strip()[:150]
                h.is_active = bool(g(f"rh_active_{h.id}"))
                h.save()
            if (n := (g("new_head") or "").strip()):
                ResourceHead.objects.create(name=n[:150], sort=ResourceHead.objects.count() + 1)
        elif act == "sectors":
            for s in Sector.objects.all():
                d, _ = SectorPlanDefault.objects.get_or_create(sector=s)
                d.priority = g(f"sd_pr_{s.id}") if g(f"sd_pr_{s.id}") in ("P1", "P2") else ""
                d.pillar = PlanPillar.objects.filter(id=g(f"sd_pl_{s.id}") or 0).first()
                d.program = PlanProgram.objects.filter(id=g(f"sd_pg_{s.id}") or 0).first()
                sdg = _int(g(f"sd_sdg_{s.id}"), 1, 17)
                d.sdg = sdg if sdg not in (None, "bad") else None
                d.gender = g(f"sd_g_{s.id}") if g(f"sd_g_{s.id}") in dict(BankProject.GENDER) else ""
                d.climate = g(f"sd_c_{s.id}") if g(f"sd_c_{s.id}") in dict(BankProject.CLIMATE) else ""
                d.save()
        from usercommittee.models import ActivityLog
        ActivityLog.objects.create(user=request.user, action="update", model_name="योजना तर्जुमा सेटिङ", object_repr=act or "", note="")
        messages.success(request, "सेटिङ सुरक्षित भयो।")
        return redirect(f"{reverse('plan_settings')}#{act}")
    sectors = [{"s": s, "d": getattr(s, "plan_default", None)} for s in Sector.objects.all()]
    return render(request, "projects/plan_settings.html", {
        "S": S, "pillars": PlanPillar.objects.prefetch_related("programs"), "criteria": ScoreCriterion.objects.all(),
        "heads": ResourceHead.objects.all(), "sectors": sectors, "AUTO": ScoreCriterion.AUTO, "programs": PlanProgram.objects.select_related("pillar"),
        "GENDER": BankProject.GENDER, "CLIMATE": BankProject.CLIMATE, "sdgs": bk.sdg_options(),
        "crit_total": sum(c.weight for c in ScoreCriterion.objects.filter(is_active=True)), "pv": convert_preview(),
    })


# ---------------------------------------------------------------- विषयगत शाखाको प्रस्ताव र पेश
@login_required
def section_priority(request):
    """विषयगत शाखा: आफ्नो क्षेत्रको सीमा, प्रस्तावको जम्मा लागत र सूची पेश गर्ने (पेशपछि लक)."""
    P = user_perms(request.user)
    if not P & {"bank.view", "bank.propose", "bank.manage"}:
        return redirect("unauthorized")
    mine = _user_sectors(request.user)
    allowed = Sector.objects.filter(is_active=True)
    if mine is not None:
        allowed = allowed.filter(id__in=mine)
    sector = allowed.filter(id=request.GET.get("sector") or request.POST.get("sector") or 0).first() or allowed.first()
    if sector is None:
        messages.error(request, "तपाईंलाई कुनै क्षेत्र / शाखा तोकिएको छैन — Admin लाई सम्पर्क गर्नुहोस्।")
        return redirect("bank_list")
    plan = mt.current_plan()
    sc = mt.sector_ceilings(plan).get(sector.id) if plan else None
    can_submit = bool(plan and plan.status == "draft" and sc and not sc.submitted_at
                      and P & ({"bank.propose"} if mine is not None else {"bank.manage"}))
    items = list(bk.queryset().filter(sector=sector, hold="", converted=False, projects__isnull=True)
                 .exclude(source="ward").order_by("-id"))
    used = mt.sector_used(sector.id)
    if request.method == "POST":
        if not can_submit:
            return redirect("unauthorized")
        if not items:
            messages.error(request, "पेश गर्न प्रस्ताव छैन — पहिले आयोजना प्रस्ताव गर्नुहोस्।")
        elif sc.amount and used > sc.amount:
            messages.error(request, f"प्रस्तावको जम्मा रु. {bk.money(used)} सीमा रु. {bk.money(sc.amount)} भन्दा बढी भयो — केही प्रस्ताव घटाउनुहोस् वा लागत मिलाउनुहोस्।")
        else:
            name, post = approver_identity(request.user, "municipal")
            sc.submitted_at, sc.submitted_by = timezone.now(), f"{name} ({post})"[:200]
            sc.save(update_fields=["submitted_at", "submitted_by"])
            for b in items:
                _log(request, b, "update", f"{sector.name} शाखाले सूची पेश गर्यो")
            messages.success(request, f"{sector.name} शाखाका {bk.np(len(items))} प्रस्ताव पेश भए — अब विषयगत समितिमा प्राविधिक जाँच र अङ्कका लागि जान्छ।")
        return redirect(f"{reverse('section_priority')}?sector={sector.id}")
    amount = bk.D(sc.amount) if sc else bk.ZERO
    return render(request, "projects/bank_section.html", {
        "sector": sector, "sectors": allowed, "plan": plan, "sc": sc, "items": items, "used": used,
        "over": bool(amount) and used > amount, "left": amount - used,
        "pct": min(int(used * 100 / amount), 100) if amount else None,
        "can_submit": can_submit, "can_add": bool(P & {"bank.propose", "bank.manage"}) and not (sc and sc.submitted_at and "bank.manage" not in P),
        "is_section": mine is not None,
    })

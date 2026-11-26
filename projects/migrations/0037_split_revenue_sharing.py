"""राजस्व बाँडफाँडलाई संघीय र प्रदेश गरी दुई शीर्षकमा छुट्याउने."""
from django.db import migrations


def split(apps, schema_editor):
    Head = apps.get_model("projects", "ResourceHead")
    Plan = apps.get_model("projects", "MTEFPlan")
    Ceil = apps.get_model("projects", "MTEFCeiling")
    old = Head.objects.filter(name="राजस्व बाँडफाँड (संघ / प्रदेश)").first()
    if old is not None:
        old.name, old.group = "संघीय राजस्व बाँडफाँड", "sharing"
        old.save(update_fields=["name", "group"])
    if Head.objects.filter(name__contains="प्रदेश", group="sharing").exclude(name__contains="संघ").exists():
        return
    fed = old or Head.objects.filter(group="sharing").order_by("sort", "id").first()
    sort = fed.sort if fed else 2
    prov = Head.objects.create(name="प्रदेश राजस्व बाँडफाँड", group="sharing", sort=sort)
    for p in Plan.objects.filter(status="draft"):           # मस्यौदा MTEF मा नयाँ शीर्षकको पङ्क्ति
        Ceil.objects.get_or_create(plan=p, head=prov)


def merge_back(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0036_liabilities_section_staff"),
    ]

    operations = [migrations.RunPython(split, merge_back)]

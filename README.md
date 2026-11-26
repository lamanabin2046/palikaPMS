# palikaPMS — पालिका योजना व्यवस्थापन प्रणाली

A web system for Nepali local governments (गाउँपालिका / नगरपालिका) to manage the whole life of a development project — from the first tole demand, through the 7-step annual planning process and MTEF, to procurement, agreements, payments and completion.

One installation serves many palikas. Each palika gets its own PostgreSQL schema (its own copy of every table) through `django-tenants`, so data never mixes.

**Live sites**
- बागमती गाउँपालिका — https://pms.bagmatimun.com
- बौधीमाई नगरपालिका — https://pms.baudhimai.com

---

## Modules

| Module | What it does |
|---|---|
| **योजना तर्जुमा** (planning) | 7-step annual planning cycle, स्रोत अनुमान (revenue forecast) and budget ceilings, ward ceilings, आयोजना बैंक (Project Bank), वडा प्राथमिकता, MTEF (3-year), approval by समिति → कार्यपालिका → सभा, and automatic creation of approved projects |
| **आयोजना अभिलेखिकरण** | Project register: budget, ward, sector, cost estimate, timeline, photos |
| **वार्षिक खरिद योजना** | Annual procurement plan |
| **उपभोक्ता समिति** | User committee formation, agreement (सम्झौता), advance, running bills and final payment, with ward-level delegation |
| **बोलपत्र / दरभाउपत्र** | Bidding and quotation workflows |
| **पत्र** | Letters and printable documents |
| **ड्यासबोर्ड / रिपोर्ट** | Dashboards, योजना vs खर्च, Excel import/export |
| **SaaS Manager** | Add palikas, manage subscriptions, back up and restore each palika |

### Planning flow (योजना तर्जुमा)

```
१ स्रोत अनुमान र बजेट सीमा  →  २ टोल/बस्ती माग  →  ३ वडा प्राथमिकता (सीमाभित्र, पेशपछि लक)
→  ४ विषयगत समिति (जाँच, अङ्क, तयार)  →  ५ बजेट तथा कार्यक्रम तर्जुमा (MTEF)
→  ६ कार्यपालिका  →  ७ सभा  →  वार्षिक बजेटमा पठाउने  →  आयोजना अभिलेखिकरण (कार्यान्वयन)
```

- Every project lives in the आयोजना बैंक with its proposer (ward, section, mayor/deputy mayor, executive, assembly).
- Projects added after their normal step get an orange **“चरण N मा थपिएको”** mark.
- Projects not selected stay in the bank for next year.
- Emergency / mid-year projects are marked **“आयोजना बैंक बाहिर”** with a reason.

### Roles

Admin, प्रमुख प्रशासकीय अधिकृत, योजना अधिकृत, इन्जिनियर, वडा कर्मचारी, वडा सचिव, वडा इन्जिनियर, पालिका इन्जिनियर, वडा अध्यक्ष, लेखा अधिकृत. Permissions per role are set in **सेटिङ → अनुमति** (defaults in `accounts/permissions.py`).

---

## Tech stack

- Python 3.10+, Django 5.0, django-tenants 3.6 (schema per palika)
- PostgreSQL
- HTMX + Tailwind (CDN) front end, Nepali date support (`nepali-datetime`)
- WhiteNoise for static files; deployed on cPanel (Passenger)

## Project layout

```
ProjectManagement/   settings, urls, wsgi
tenants/             palika (tenant) model, SaaS manager, create_palika
accounts/            users, roles, permissions, wards
projects/            projects, planning (bank.py, mtef.py, views_bank.py, views_mtef.py), reports
usercommittee/       उपभोक्ता समिति workflow and payments
biddings/  quatation/  letters/
templates/  static/
```

---

## Local setup (Windows, PowerShell)

```powershell
git clone https://github.com/lamanabin2046/palikaPMS.git
cd palikaPMS

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env        # then edit .env (secret key, DB details)
docker compose up -d          # PostgreSQL on port 5433 (or use your own PostgreSQL)

python manage.py migrate_schemas --shared
python manage.py setup_saas --mobile <manager-mobile> --password <strong-password>
python manage.py create_palika --code demo --name "डेमो गाउँपालिका" --district <district> --address <address> --admin-mobile <admin-mobile>
python manage.py runserver
```

- SaaS manager: http://localhost:8000/
- Palika site: http://demo.localhost:8000/

New palikas can also be added from the SaaS Manager screen.

## Deploying an update (cPanel)

On your PC:

```bash
git add <changed folders>
git commit -m "what changed"
git push
```

On each server (cPanel Terminal):

```bash
cd ~/ProjectManagement
source <virtualenv>/bin/activate        # path shown in cPanel → Setup Python App
git pull
python manage.py migrate_schemas --noinput   # when models changed
python manage.py collectstatic --noinput     # when static files changed
touch tmp/restart.txt
```

First-time server setup is described in [DEPLOY_CPANEL.md](DEPLOY_CPANEL.md); multi-palika details in [README_SAAS.md](README_SAAS.md).

## Useful commands

| Command | Purpose |
|---|---|
| `create_palika` | Add a palika (creates its schema and admin user) |
| `backup_palikas` / `restore_palika` | Back up / restore palika data |
| `import_sqlite` | Import data from the old single-palika SQLite database |
| `send_alerts` | Send deadline / payment alerts |

## Security

- Never commit `.env` — it holds the secret key and database password (already in `.gitignore`).
- Use a long random `DJANGO_SECRET_KEY` and `DJANGO_DEBUG=False` in production.
- Use strong passwords for admin users.

---

Developed by **AlfaBeta Engineer Pvt Ltd**.

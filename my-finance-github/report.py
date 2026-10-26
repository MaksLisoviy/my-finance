"""Локальний HTML-звіт із трьох збережених запитів: uv run python report.py."""

import calendar
from collections import defaultdict
from decimal import Decimal
from html import escape
from pathlib import Path

from beancount import loader
from beancount.core.data import Query
from beanquery.query import run_query

ROOT = Path(__file__).resolve().parent
QUERY_NAMES = ("Витрати за категоріями", "Норма заощаджень", "Фіксовані і змінні")
MONTHS = ("", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
          "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень")


def money(value):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " zł"


def percent(value):
    return "—" if value is None else f"{value:.2f}".replace(".", ",") + "%"


def bar(label, amount, maximum, color="teal"):
    width = min(100, float(abs(amount) / maximum * 100)) if maximum else 0
    return (f'<div class="bar-row"><div class="bar-label"><span>{escape(label)}</span>'
            f'<strong>{money(amount)}</strong></div><div class="track">'
            f'<div class="fill {color}" style="width:{width:.3f}%"></div></div></div>')


def card(title, metric, subtitle, chart, conclusion):
    return (f'<article><h2>{escape(title)}</h2><div class="metric">{escape(metric)}</div>'
            f'<p class="muted">{escape(subtitle)}</p><div class="chart">{chart}</div>'
            f'<p class="conclusion">{escape(conclusion)}</p></article>')


def load_results():
    from finance import locked, sync, results
    with locked():
        sync()
        return results('ola')


def render(results):
    categories, savings, fixed = results
    by_category = defaultdict(list)
    for row in categories:
        by_category[row["month"]].append(row)
    by_fixed = {row["month"]: row for row in fixed}
    if not savings:
        raise ValueError("Немає місячних даних для звіту")
    panels, choices = [], []
    for index, row in enumerate(sorted(savings, key=lambda item: item["month"])):
        month = row["month"]
        month_id = month.strftime("%Y-%m")
        title = f"{MONTHS[month.month]} {month.year}"
        last = row["last_operation"]
        incomplete = month.year == 2026 and month.month == 10
        if incomplete:
            last = last.replace(day=15)
        coverage = (f"Неповний місяць · дані до {last:%d.%m.%Y}" if incomplete
                    else f"Остання операція: {last:%d.%m.%Y}")
        choices.append(f'<option value="{month_id}">{title}'
                       f'{" · до " + last.strftime("%d.%m") if incomplete else ""}</option>')
        expenses = row["expenses"]
        items = sorted(by_category[month], key=lambda item: item["expenses"], reverse=True)
        if sum((item["expenses"] for item in items), Decimal(0)) != expenses:
            raise ValueError(f"{month_id}: суми категорій не збігаються з витратами")
        breakdown = by_fixed.get(month, {"fixed": Decimal(0), "variable": Decimal(0)})
        if breakdown["fixed"] + breakdown["variable"] != expenses:
            raise ValueError(f"{month_id}: фіксовані та змінні не збігаються з витратами")
        maximum = max((abs(item["expenses"]) for item in items), default=Decimal(0))
        category_chart = "".join(bar(item["category"].removeprefix("Expenses:").replace("-", " "),
                                     item["expenses"], maximum) for item in items)
        if items:
            largest = items[0]
            label = largest["category"].removeprefix("Expenses:").replace("-", " ")
            conclusion = f"Найбільша категорія витрат — {label}: {money(largest['expenses'])}."
        else:
            conclusion = "За цей місяць витрат немає."
        first = card("1. Куди йдуть гроші?", money(expenses), "Усього витрачено за період",
                     category_chart, conclusion)
        savings_max = max(abs(row["income"]), abs(expenses), abs(row["saved"]))
        savings_chart = bar("Дохід", row["income"], savings_max)
        savings_chart += bar("Витрати", expenses, savings_max, "amber")
        savings_chart += bar("Відкладено", row["saved"], savings_max,
                             "red" if row["saved"] < 0 else "teal")
        saved = row["saved"]
        if row["income"] == 0:
            conclusion = f"Доходу немає, тому частка заощаджень не визначена; різниця доходу й витрат — {money(saved)}."
        elif saved < 0:
            conclusion = f"Витрати перевищили дохід на {money(-saved)}."
        else:
            conclusion = f"Після витрат залишилося {money(saved)}, або {percent(row['savings_percent'])} доходу."
        second = card("2. Яку частку доходу я відкладаю?", percent(row["savings_percent"]),
                      f"Відкладено: {money(saved)}", savings_chart, conclusion)
        fixed_share = breakdown["fixed"] / expenses * 100 if expenses else None
        fixed_chart = bar("Фіксовані", breakdown["fixed"], max(abs(breakdown["fixed"]), abs(breakdown["variable"])))
        fixed_chart += bar("Змінні", breakdown["variable"], max(abs(breakdown["fixed"]), abs(breakdown["variable"])), "amber")
        third = card("3. Скільки витрат можна змінювати?", money(breakdown["variable"]),
                     "Змінні витрати за чинними правилами", fixed_chart,
                     f"Фіксовані витрати становлять {percent(fixed_share)} усіх витрат, а змінні — {money(breakdown['variable'])}.")
        panels.append(f'<section id="{month_id}" class="month-panel"'
                      f'{" hidden" if index else ""}><p class="coverage">{escape(coverage)}</p>'
                      f'<div class="cards">{first}{second}{third}</div></section>')
    return TEMPLATE.replace("<!--OPTIONS-->", "".join(choices)).replace("<!--PANELS-->", "".join(panels))


TEMPLATE = '''<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Фінансовий огляд Олі</title><style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#18352e;background:#f3f5f0;line-height:1.5}
*{box-sizing:border-box}body{margin:0}main{max-width:1360px;margin:auto;padding:40px 24px}
header{display:flex;align-items:center;justify-content:space-between;gap:24px;flex-wrap:wrap}
h1{font-size:clamp(28px,4vw,42px);letter-spacing:-1px;margin:0}header p{margin:6px 0;color:#52665e}
label{display:block;font-size:14px}select{font:inherit;padding:12px;border:1px solid #a8b9af;border-radius:12px;background:white;max-width:100%}
.coverage{margin:28px 0 16px;font-weight:600;color:#52665e}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}
article{background:white;border:1px solid #dce3db;border-radius:20px;padding:24px;min-width:0;display:flex;flex-direction:column}
h2{font-size:19px;margin:0 0 20px;min-height:58px}.metric{font-size:clamp(28px,3vw,38px);font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.muted{color:#52665e;font-size:14px;margin:4px 0 24px}.chart{margin-bottom:24px}.bar-row{margin-bottom:14px}
.bar-label{display:flex;gap:12px;justify-content:space-between;font-size:13px;margin-bottom:6px}.bar-label strong{white-space:nowrap}
.track{height:10px;border-radius:6px;background:#edf1ec;overflow:hidden}.fill{height:100%;border-radius:6px;background:#167867}.amber{background:#ba7021}.red{background:#bf4848}
.conclusion{margin:auto 0 0;padding-top:18px;border-top:1px solid #e4e9e1;font-size:15px}
footer{font-size:13px;color:#52665e;margin-top:28px;max-width:900px}[hidden]{display:none!important}
@media(max-width:1000px){.cards{grid-template-columns:1fr}h2{min-height:0}main{padding:24px 16px}}
@media print{select,label{display:none}.month-panel[hidden]{display:none!important}article{break-inside:avoid}}
</style></head><body><main><header><div><h1>Гроші під контролем</h1><p>Щомісячний фінансовий огляд Олі · PLN</p></div>
<div><label for="month">Місяць звіту</label><select id="month"><!--OPTIONS--></select></div></header>
<noscript><p>Для перемикання місяців увімкніть JavaScript; нижче показано перший місяць.</p></noscript>
<!--PANELS-->
<footer>Дані з локального обліку. Працює без інтернету. Категорії та фіксовані витрати — за чинними правилами: нерозпізнані витрати входять у «Без Категорії» та змінні. Банківська виписка охоплює 01.08–15.10.2026; готівкові витрати враховано за датою запису. Неповні періоди не прогнозуються.</footer>
</main><script>
const picker = document.getElementById('month');
function showMonth(){document.querySelectorAll('.month-panel').forEach(panel => {panel.hidden = panel.id !== picker.value;});}
picker.addEventListener('change', showMonth);
showMonth();
</script></body></html>'''


def main():
    results = load_results()
    html = render(results)
    output = ROOT / "report.html"
    output.write_text(html, encoding="utf-8")
    print(f"Створено {output}: {len(results[1])} місяці, три питання на місяць.")


if __name__ == "__main__":
    main()

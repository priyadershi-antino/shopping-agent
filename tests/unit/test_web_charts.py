import json
import re
from datetime import date
from decimal import Decimal

from shopping_agent.web.charts import Series, _nice_step, price_chart


def test_nice_steps() -> None:
    assert _nice_step(10_000) == 2_500
    assert _nice_step(4_000) == 1_000
    assert _nice_step(0) == 1


def test_single_series_chart_escapes_and_annotates() -> None:
    points = [
        (date(2026, 9, 1), Decimal(74990)),
        (date(2026, 9, 5), Decimal(71990)),
        (date(2026, 9, 9), Decimal(73490)),
    ]
    html = str(price_chart([Series(label="<b>x</b>", points=points)], title="Price <script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Low ₹71,990" in html and "High ₹74,990" in html
    assert 'class="legend"' not in html  # one series: the title names it
    data = json.loads(
        re.search(r"data-series='([^']*)'", html).group(1).replace("&quot;", '"')  # type: ignore[union-attr]
    )
    assert [p["v"] for p in data[0]["points"]] == [74990.0, 71990.0, 73490.0]
    assert html.count("<tr>") == 4  # header + 3 dates


def test_multi_series_gets_legend_and_empty_shows_message() -> None:
    a = Series(label="Amazon", points=[(date(2026, 9, 1), Decimal(100))])
    b = Series(label="Flipkart", points=[(date(2026, 9, 2), Decimal(90))])
    html = str(price_chart([a, b], title="t"))
    assert 'class="legend"' in html and "Flipkart" in html
    assert "No price observations" in str(price_chart([], title="t"))

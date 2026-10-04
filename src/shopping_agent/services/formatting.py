from decimal import Decimal

from shopping_agent.domain.common import money


def format_inr(amount: Decimal | int, *, paise: bool = False) -> str:
    """Format with Indian digit grouping: 100000 -> ₹1,00,000."""
    value = money(Decimal(amount))
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, _, frac = f"{value:.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    text = f"{sign}₹{whole}"
    if paise and frac != "00":
        text += f".{frac}"
    return text

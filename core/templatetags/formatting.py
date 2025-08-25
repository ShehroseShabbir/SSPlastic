# core/templatetags/formatting.py
from django import template
from django.contrib.humanize.templatetags.humanize import intcomma
from decimal import Decimal
from core.utils_money import from_rupees_int
from core.utils_weight import D

register = template.Library()

@register.filter
def money_int(value):
    """
    Value is rupees INT from DB. Show with thousands separators, no decimals.
    """
    try:
        n = int(value or 0)
    except Exception:
        return value
    return intcomma(n)

@register.filter
def money_int_pk(value):
    """Convenience: 'PKR 123,456'"""
    return f"PKR {money_int(value)}"

@register.filter
def kg3(value):
    """
    Value is Decimal kg with 3 places. Format as '12,345.678'.
    """
    try:
        v = D(value).quantize(Decimal("0.000"))
    except Exception:
        return value
    # manual comma format while keeping 3 decimals
    whole, dot, frac = f"{v:.3f}".partition(".")
    return f"{intcomma(int(whole))}.{frac}"


MICRON_CHOICES = [
    ("20/40", "20/40"),
    ("22/44", "22/44"),
    ("25/50", "25/50"),
    ("28/56", "28/56"),
    ("30/60", "30/60"),
    ("32/65", "32/65"),
    ("35/70", "35/70"),
    ("37/75", "37/75"),
    ("40/80", "40/80"),
    ("42/85", "42/85"),
    ("45/90", "45/90"),
    ("47/95", "47/95"),
    ("50/100", "50/100"),
    ("52/105", "52/105"),
    ("55/110", "55/110"),
    ("57/115", "57/115"),
    ("60/120", "60/120"),
    ("62/125", "62/125"),
    ("65/130", "65/130"),
    ("67/135", "67/135"),
    ("70/140", "70/140"),
    ("72/145", "72/145"),
    ("75/150", "75/150"),
    ("77/155", "77/155"),
    ("80/160", "80/160"),
    ("82/165", "82/165"),
    ("85/170", "85/170"),
    ("87/175", "87/175"),
    ("90/180", "90/180"),
    ("100/200", "100/200"),
]

MICRON_HELP = "Select micron from the list."
TWOPLACES = Decimal('0.01')
CONSUME_STATUSES = ("CONFIRMED", "INPROD", "READY", "DELIVERED", "SHIPPED", "CLOSED")

# If you want, keep other shared choices here too:
CURRENT_TYPES = [("NT", "NT"), ("DT", "DT"), ("ST", "ST")]

COUNTRIES = [("Pakistan", "Pakistan"), ("Australia", "Australia") , ("Germany", "Germany"), ("United States of America", "United States of America")]
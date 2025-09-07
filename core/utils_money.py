# core/utils_money.py
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_HALF_EVEN

# ---- Core helpers ----
def D(x) -> Decimal:
    """Coerce to Decimal safely."""
    return x if isinstance(x, Decimal) else Decimal(str(x or "0"))

def to_rupees_int(amount, mode: str = "half_up") -> int:
    """
    Convert Decimal PKR -> integer rupees.
    mode: 'half_up' (default) or 'ceil'
    """
    amt = D(amount)
    if mode == "ceil":
        return int(amt.to_integral_value(rounding=ROUND_CEILING))
    return int(amt.to_integral_value(rounding=ROUND_HALF_UP))

def from_rupees_int(n: int) -> Decimal:
    """Integer rupees -> Decimal PKR (for display/math only; do NOT store)."""
    return D(n)

def money_mul(kg, price_per_kg) -> Decimal:
    """Exact subtotal in Decimal PKR before rounding to rupees int."""
    return D(kg) * D(price_per_kg)

# ---- Compatibility shims (so older imports keep working) ----
def _to_decimal(x) -> Decimal:
    """Back-compat alias for D()."""
    return D(x)

def round_to(value, places=2, mode: str = "half_up") -> Decimal:
    """
    Back-compat rounding utility:
    - value: number-like
    - places: number of decimal places to keep
    - mode: 'half_up' (default) or 'bankers' (half_even)
    """
    q = Decimal(1).scaleb(-int(places))  # 10^-places
    rounding = ROUND_HALF_UP if mode != "bankers" else ROUND_HALF_EVEN
    return D(value).quantize(q, rounding=rounding)

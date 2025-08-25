# core/utils_weight.py
from decimal import Decimal, ROUND_HALF_UP
TWOKG = Decimal("0.001")

def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x or "0"))

def dkg(x) -> Decimal:
    """Quantize to 3 decimals for kg."""
    return D(x).quantize(TWOKG, rounding=ROUND_HALF_UP)

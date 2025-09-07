from django.db import models
from django.core.exceptions import ValidationError, NON_FIELD_ERRORS
from django.core.validators import MinValueValidator
from decimal import Decimal, ROUND_HALF_UP
# keep your existing imports/utilities
from .models_ar import *




def parse_weights_csv(text):
    """
    Accepts comma/space separated values like: '12.5, 13, 10.75'
    Returns list[Decimal] (>=0).
    """
    if not text:
        return []
    raw = [p.strip() for p in text.replace("\n", ",").split(",")]
    vals = []
    for s in raw:
        if not s:
            continue
        vals.append(Decimal(s))
    return vals







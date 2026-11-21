"""Recognize textual figure labels without treating their captions as images."""
import re

_LABEL = r"(?:图表?\s*|figure\s+|fig\.?\s*)(?:[A-Za-z]+[.\-–])?\d+(?:[.\-–]\d+)*[A-Za-z]?(?![0-9A-Za-z])"
FIGURE_LABEL = re.compile(_LABEL, re.IGNORECASE)
FIGURE_ONLY = re.compile(r"\s*(?:见|参见|如|see\s+|as shown in\s+)?" + _LABEL + r"\s*(?:所示)?[。.:：)]*\s*", re.IGNORECASE)

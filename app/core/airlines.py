"""Display names for common airline IATA codes. Unknown codes are shown as-is."""

AIRLINE_NAMES: dict[str, str] = {
    "FR": "Ryanair",
    "RK": "Ryanair UK",
    "W6": "Wizz Air",
    "W4": "Wizz Air Malta",
    "TP": "TAP Air Portugal",
    "LO": "LOT Polish Airlines",
    "U2": "easyJet",
    "EC": "easyJet Europe",
    "VY": "Vueling",
    "IB": "Iberia",
    "LH": "Lufthansa",
    "LX": "SWISS",
    "OS": "Austrian Airlines",
    "SN": "Brussels Airlines",
    "EW": "Eurowings",
    "KL": "KLM",
    "AF": "Air France",
    "TK": "Turkish Airlines",
    "BA": "British Airways",
    "AZ": "ITA Airways",
    "UX": "Air Europa",
    "TO": "Transavia France",
    "HV": "Transavia",
    "SK": "SAS",
    "DY": "Norwegian",
    "A3": "Aegean Airlines",
}


def airline_name(code: str | None) -> str:
    if not code:
        return "Unknown airline"
    name = AIRLINE_NAMES.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()
